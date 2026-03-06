"""
Redis data-access layer.

Key schema:
  board:{slug}:entries  → Sorted Set  (score = unix timestamp, max 20 members)
  board:{slug}:authkey  → String      (optional, 48 h TTL)
  board:{slug}:channel  → Pub/Sub channel name (not a stored key)

  shared:{slug}:entries  → Sorted Set  (score = unix timestamp, max 20 members)
  shared:{slug}:authkey  → String      (mandatory)
  shared:{slug}:meta     → JSON        (ttl_hours, created_at)
  shared:{slug}:channel  → Pub/Sub channel name (not a stored key)

  ratelimit:{ip}         → Counter     (60s TTL, for /s/* rate limiting)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis

from app.models import Entry

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
ENTRY_TTL_SECONDS = int(os.getenv("ENTRY_TTL_HOURS", "48")) * 3600
MAX_ENTRIES = int(os.getenv("MAX_ENTRIES_PER_BOARD", "20"))
SHARED_DEFAULT_TTL_HOURS = int(os.getenv("SHARED_BOARD_DEFAULT_TTL_HOURS", "24"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
DATA_DIR = Path("data")

# Module-level Redis client (initialised in lifespan)
redis: aioredis.Redis | None = None


async def init_redis() -> None:
    """Create and verify the Redis connection. Crash loudly on failure."""
    global redis
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
    except Exception as exc:
        raise RuntimeError(
            f"Cannot connect to Redis at {REDIS_URL}: {exc}"
        ) from exc


async def close_redis() -> None:
    global redis
    if redis:
        await redis.aclose()
        redis = None


# ---------------------------------------------------------------------------
# Key helpers (namespace-aware)
# ---------------------------------------------------------------------------

def _entries_key(slug: str, ns: str = "board") -> str:
    return f"{ns}:{slug}:entries"


def _authkey_key(slug: str, ns: str = "board") -> str:
    return f"{ns}:{slug}:authkey"


def _channel(slug: str, ns: str = "board") -> str:
    return f"{ns}:{slug}:channel"


def _meta_key(slug: str) -> str:
    return f"shared:{slug}:meta"


# ---------------------------------------------------------------------------
# Entry CRUD
# ---------------------------------------------------------------------------

async def get_entries(slug: str, ns: str = "board") -> list[Entry]:
    """Return entries for a board, newest first."""
    raw_members = await redis.zrevrange(_entries_key(slug, ns), 0, -1)
    entries: list[Entry] = []
    for raw in raw_members:
        try:
            entries.append(Entry(**json.loads(raw)))
        except Exception:
            continue  # Skip malformed entries
    return entries


async def add_entry(slug: str, entry: Entry, ns: str = "board") -> None:
    """
    Atomically add an entry, trim to MAX_ENTRIES, and reset TTL.
    Deletes orphaned image files for trimmed entries.
    """
    entry_json = entry.model_dump_json()
    timestamp = time.time()

    # Determine TTL: for shared boards, use per-board TTL
    ttl = ENTRY_TTL_SECONDS
    if ns == "shared":
        meta = await get_shared_meta(slug)
        if meta and meta.get("ttl_hours"):
            ttl = int(meta["ttl_hours"]) * 3600

    # 1. Find members that will be trimmed BEFORE the transaction
    existing = await redis.zrange(_entries_key(slug, ns), 0, -(MAX_ENTRIES + 1))

    # 2. Atomic pipeline: add, trim, refresh TTL
    async with redis.pipeline(transaction=True) as pipe:
        await pipe.zadd(_entries_key(slug, ns), {entry_json: timestamp})
        await pipe.zremrangebyrank(_entries_key(slug, ns), 0, -(MAX_ENTRIES + 1))
        await pipe.expire(_entries_key(slug, ns), ttl)
        await pipe.execute()

    # 3. Delete image/file files for removed entries
    for raw in existing:
        try:
            old = json.loads(raw)
            if old.get("image_path"):
                (DATA_DIR / old["image_path"]).unlink(missing_ok=True)
        except Exception:
            pass


async def find_entry_raw(slug: str, entry_id: str, ns: str = "board") -> Optional[str]:
    """Return the raw JSON string for an entry by ID, or None."""
    members = await redis.zrange(_entries_key(slug, ns), 0, -1)
    for raw in members:
        try:
            data = json.loads(raw)
            if data.get("id") == entry_id:
                return raw
        except Exception:
            continue
    return None


async def delete_entry(slug: str, entry_id: str, ns: str = "board") -> Optional[Entry]:
    """Remove an entry from the sorted set. Returns the deleted entry or None."""
    raw = await find_entry_raw(slug, entry_id, ns)
    if raw is None:
        return None

    await redis.zrem(_entries_key(slug, ns), raw)

    entry = Entry(**json.loads(raw))
    if entry.image_path:
        (DATA_DIR / entry.image_path).unlink(missing_ok=True)

    return entry


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def get_board_key(slug: str, ns: str = "board") -> Optional[str]:
    return await redis.get(_authkey_key(slug, ns))


async def set_board_key(slug: str, key: str, ns: str = "board") -> None:
    ttl = ENTRY_TTL_SECONDS
    if ns == "shared":
        meta = await get_shared_meta(slug)
        if meta and meta.get("ttl_hours"):
            ttl = int(meta["ttl_hours"]) * 3600
    await redis.set(_authkey_key(slug, ns), key, ex=ttl)


async def delete_board_key(slug: str, ns: str = "board") -> None:
    await redis.delete(_authkey_key(slug, ns))


# ---------------------------------------------------------------------------
# Pub/Sub
# ---------------------------------------------------------------------------

async def publish_event(slug: str, event_type: str, data: str, ns: str = "board") -> None:
    """Publish an event to all SSE subscribers for a board."""
    message = json.dumps({"event": event_type, "data": data})
    await redis.publish(_channel(slug, ns), message)


# ---------------------------------------------------------------------------
# Shared board meta
# ---------------------------------------------------------------------------

async def get_shared_meta(slug: str) -> Optional[dict]:
    raw = await redis.get(_meta_key(slug))
    if raw:
        return json.loads(raw)
    return None


async def set_shared_meta(slug: str, ttl_hours: int) -> None:
    meta = {
        "ttl_hours": ttl_hours,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await redis.set(_meta_key(slug), json.dumps(meta), ex=ttl_hours * 3600)


async def delete_shared_board(slug: str) -> None:
    """Delete all data for a shared board."""
    # Delete image/file files first
    members = await redis.zrange(_entries_key(slug, "shared"), 0, -1)
    for raw in members:
        try:
            data = json.loads(raw)
            if data.get("image_path"):
                (DATA_DIR / data["image_path"]).unlink(missing_ok=True)
        except Exception:
            pass

    # Delete all Redis keys
    await redis.delete(
        _entries_key(slug, "shared"),
        _authkey_key(slug, "shared"),
        _meta_key(slug),
    )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

async def check_rate_limit(ip: str) -> bool:
    """Return True if the request is within rate limits, False if exceeded."""
    key = f"ratelimit:{ip}"
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, 60)
    return current <= RATE_LIMIT_PER_MINUTE


# ---------------------------------------------------------------------------
# Board listing
# ---------------------------------------------------------------------------

async def list_boards() -> list[dict]:
    """
    Return all boards that have at least one entry, sorted by most recent
    activity. Each dict contains: slug, entry_count, last_activity (datetime
    or None), has_key (bool).
    """
    slugs: list[str] = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="board:*:entries", count=100)
        for key in keys:
            slug = key[len("board:"):-len(":entries")]
            slugs.append(slug)
        if cursor == 0:
            break

    if not slugs:
        return []

    # Batch all per-board queries in a single pipeline
    async with redis.pipeline(transaction=False) as pipe:
        for slug in slugs:
            await pipe.zcard(_entries_key(slug))
            await pipe.zrevrange(_entries_key(slug), 0, 0, withscores=True)
            await pipe.exists(_authkey_key(slug))
        results = await pipe.execute()

    boards = []
    for i, slug in enumerate(slugs):
        count     = results[i * 3]
        top       = results[i * 3 + 1]   # [(member, score)] or []
        has_key   = bool(results[i * 3 + 2])

        if count == 0:
            continue

        last_ts = float(top[0][1]) if top else None
        last_activity = (
            datetime.fromtimestamp(last_ts, tz=timezone.utc) if last_ts else None
        )
        boards.append({
            "slug": slug,
            "entry_count": count,
            "last_activity": last_activity,
            "has_key": has_key,
        })

    boards.sort(
        key=lambda b: b["last_activity"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return boards


async def list_shared_boards() -> list[dict]:
    """
    Return all shared boards, sorted by most recent activity.
    Each dict contains: slug, entry_count, last_activity, ttl_hours, created_at.
    """
    slugs: list[str] = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="shared:*:meta", count=100)
        for key in keys:
            slug = key[len("shared:"):-len(":meta")]
            slugs.append(slug)
        if cursor == 0:
            break

    if not slugs:
        return []

    async with redis.pipeline(transaction=False) as pipe:
        for slug in slugs:
            await pipe.zcard(_entries_key(slug, "shared"))
            await pipe.zrevrange(_entries_key(slug, "shared"), 0, 0, withscores=True)
            await pipe.get(_meta_key(slug))
            await pipe.get(_authkey_key(slug, "shared"))
        results = await pipe.execute()

    boards = []
    for i, slug in enumerate(slugs):
        count   = results[i * 4]
        top     = results[i * 4 + 1]
        meta_raw = results[i * 4 + 2]
        board_key = results[i * 4 + 3]

        meta = json.loads(meta_raw) if meta_raw else {}

        last_ts = float(top[0][1]) if top else None
        last_activity = (
            datetime.fromtimestamp(last_ts, tz=timezone.utc) if last_ts else None
        )
        boards.append({
            "slug": slug,
            "entry_count": count,
            "last_activity": last_activity,
            "ttl_hours": meta.get("ttl_hours", SHARED_DEFAULT_TTL_HOURS),
            "created_at": meta.get("created_at"),
            "key": board_key,
        })

    boards.sort(
        key=lambda b: b["last_activity"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return boards


# ---------------------------------------------------------------------------
# Startup cleanup: remove orphaned image files
# ---------------------------------------------------------------------------

async def cleanup_orphaned_images() -> None:
    """Delete image files on disk that have no matching Redis entry."""
    images_dir = DATA_DIR / "images"
    if not images_dir.exists():
        return

    # Collect all referenced image paths from Redis (both board and shared)
    referenced: set[str] = set()
    for pattern in ("board:*:entries", "shared:*:entries"):
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=pattern, count=100)
            for key in keys:
                members = await redis.zrange(key, 0, -1)
                for raw in members:
                    try:
                        data = json.loads(raw)
                        if data.get("image_path"):
                            referenced.add(data["image_path"])
                    except Exception:
                        pass
            if cursor == 0:
                break

    for img_file in images_dir.iterdir():
        relative = f"images/{img_file.name}"
        if relative not in referenced:
            img_file.unlink(missing_ok=True)
