"""
LAN Clipboard — FastAPI application.

Board routes:  /b/  (LAN-only, optional auth)
Shared routes: /s/  (internet-safe, mandatory auth, rate-limited)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

import app.redis_client as rc
from app.auth import generate_key, generate_strong_key, validate_slug
from app.models import Entry, EntryCreate, EntryResponse

DATA_DIR = Path("data")
IMAGES_DIR = DATA_DIR / "images"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_MB", "5")) * 1024 * 1024
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "5"))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    await rc.init_redis()
    await rc.cleanup_orphaned_images()
    yield
    await rc.close_redis()


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="LAN Clipboard", lifespan=lifespan)

# Vendor assets and fonts are baked into the Docker image at /app/vendor/ and /app/fonts/
# (outside the app source tree, so a dev bind-mount of ./app:/app/app never shadows them).
# These mounts must come BEFORE the generic /static mount so they take routing precedence.
app.mount("/static/vendor", StaticFiles(directory="/app/vendor"), name="vendor")
app.mount("/static/fonts", StaticFiles(directory="/app/fonts"), name="fonts")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def require_auth(slug: str, key: Optional[str], ns: str = "board") -> None:
    """Raise HTTP 401 if the board has a key and the provided key is wrong."""
    board_key = await rc.get_board_key(slug, ns)
    if board_key and board_key != key:
        raise HTTPException(status_code=401, detail="Invalid or missing board key.")


async def require_rate_limit(request: Request) -> None:
    """Raise HTTP 429 if rate limit exceeded."""
    ip = request.client.host if request.client else "unknown"
    if not await rc.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")


def _save_file(entry_id: str, file_bytes: bytes, ext: str) -> str:
    """Save file to disk, return relative path."""
    filename = f"{entry_id}.{ext}"
    file_path = f"images/{filename}"
    disk_path = IMAGES_DIR / filename
    disk_path.write_bytes(file_bytes)
    return file_path


def _generate_thumbnail(image_bytes: bytes) -> Optional[str]:
    """Generate a 200px-wide JPEG thumbnail, return base64 or None."""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            ratio = 200 / img.width if img.width > 200 else 1.0
            thumb_size = (int(img.width * ratio), int(img.height * ratio))
            thumb = img.resize(thumb_size, Image.LANCZOS)
            buf = BytesIO()
            thumb.save(buf, format="JPEG", quality=60)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


async def _create_entry_common(
    request: Request, slug: str, key: Optional[str], ns: str, prefix: str
) -> Response:
    """Shared logic for creating entries on /b/ and /s/ boards."""
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_auth(slug, key, ns)

    body = await request.json()
    entry_in = EntryCreate(**body)

    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    if entry_in.type == "text":
        if not entry_in.content:
            raise HTTPException(status_code=422, detail="Content required for text entries.")
        entry = Entry(
            id=entry_id,
            type="text",
            content=entry_in.content,
            created_at=now,
        )

    elif entry_in.type == "image":
        if not entry_in.content or not entry_in.mime:
            raise HTTPException(status_code=422, detail="content and mime required for image entries.")

        try:
            image_bytes = base64.b64decode(entry_in.content)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid base64 content.")

        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (max {MAX_UPLOAD_SIZE_MB} MB).",
            )

        if entry_in.mime not in ("image/png", "image/jpeg"):
            raise HTTPException(status_code=422, detail="Only image/png and image/jpeg are supported.")

        ext = "png" if entry_in.mime == "image/png" else "jpg"
        file_path = _save_file(entry_id, image_bytes, ext)
        thumbnail_b64 = _generate_thumbnail(image_bytes)

        entry = Entry(
            id=entry_id,
            type="image",
            image_path=file_path,
            thumbnail=thumbnail_b64,
            mime=entry_in.mime,
            created_at=now,
            file_size=len(image_bytes),
        )

    else:  # file
        if not entry_in.content:
            raise HTTPException(status_code=422, detail="content required for file entries.")

        try:
            file_bytes = base64.b64decode(entry_in.content)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid base64 content.")

        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (max {MAX_UPLOAD_SIZE_MB} MB).",
            )

        # Determine extension from original filename or mime
        original_name = entry_in.file_name or "file"
        ext = original_name.rsplit(".", 1)[-1] if "." in original_name else "bin"
        # Sanitize extension
        ext = ext[:10].lower()

        file_path = _save_file(entry_id, file_bytes, ext)

        entry = Entry(
            id=entry_id,
            type="file",
            image_path=file_path,
            mime=entry_in.mime or "application/octet-stream",
            file_name=original_name,
            file_size=len(file_bytes),
            created_at=now,
        )

    await rc.add_entry(slug, entry, ns)

    # Publish SSE event (HTML fragment)
    fragment_html = templates.get_template("partials/entry.html").render(
        entry=entry, slug=slug, key=key, prefix=prefix
    )
    await rc.publish_event(slug, "new_entry", fragment_html, ns)

    # HTMX request → return HTML fragment
    if request.headers.get("HX-Request"):
        return HTMLResponse(content=fragment_html)

    return JSONResponse(
        EntryResponse(id=entry_id, type=entry_in.type, created_at=now).model_dump()
    )


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    boards = await rc.list_boards()
    shared_boards = await rc.list_shared_boards()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "boards": boards,
        "shared_boards": shared_boards,
    })


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    try:
        await rc.redis.ping()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Redis unavailable")


# ═══════════════════════════════════════════════════════════════════════════
# BOARD ROUTES — /b/*
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/b/{slug}", response_class=HTMLResponse)
async def board(request: Request, slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    board_key = await rc.get_board_key(slug)
    if board_key and board_key != key:
        return templates.TemplateResponse(
            "auth_required.html",
            {"request": request, "slug": slug, "prefix": "b"},
            status_code=401,
        )

    entries = await rc.get_entries(slug)
    return templates.TemplateResponse(
        "board.html",
        {
            "request": request,
            "slug": slug,
            "key": key,
            "entries": entries,
            "has_key": bool(board_key),
            "prefix": "b",
            "max_upload_mb": MAX_UPLOAD_SIZE_MB,
        },
    )


@app.post("/b/{slug}/entries")
async def create_entry(request: Request, slug: str, key: Optional[str] = None):
    return await _create_entry_common(request, slug, key, ns="board", prefix="b")


@app.delete("/b/{slug}/entries/{entry_id}")
async def delete_entry(slug: str, entry_id: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_auth(slug, key)

    deleted = await rc.delete_entry(slug, entry_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Entry not found.")

    await rc.publish_event(slug, "delete_entry", json.dumps({"id": entry_id}))
    return Response(status_code=204)


@app.get("/b/{slug}/entries/{entry_id}/image")
async def get_image(slug: str, entry_id: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_auth(slug, key)

    raw = await rc.find_entry_raw(slug, entry_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Entry not found.")

    entry = Entry(**json.loads(raw))
    if not entry.image_path:
        raise HTTPException(status_code=404, detail="Not an image entry.")

    disk_path = DATA_DIR / entry.image_path
    if not disk_path.exists():
        raise HTTPException(status_code=404, detail="Image file not found.")

    return FileResponse(
        path=disk_path,
        media_type=entry.mime or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/b/{slug}/entries/{entry_id}/download")
async def download_image(slug: str, entry_id: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_auth(slug, key)

    raw = await rc.find_entry_raw(slug, entry_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Entry not found.")

    entry = Entry(**json.loads(raw))
    if not entry.image_path:
        raise HTTPException(status_code=404, detail="Not a file entry.")

    disk_path = DATA_DIR / entry.image_path
    if not disk_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")

    if entry.file_name:
        download_name = entry.file_name
    else:
        ext = entry.image_path.rsplit(".", 1)[-1]
        ts = entry.created_at.replace(":", "-").replace(".", "-")[:19]
        download_name = f"clipboard_{ts}.{ext}"

    return FileResponse(
        path=disk_path,
        media_type=entry.mime or "application/octet-stream",
        filename=download_name,
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.get("/b/{slug}/stream")
async def board_stream(slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_auth(slug, key)

    async def event_generator():
        pubsub = rc.redis.pubsub()
        await pubsub.subscribe(rc._channel(slug))
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    payload = json.loads(message["data"])
                    yield ServerSentEvent(
                        data=payload["data"],
                        event=payload["event"],
                    )
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(rc._channel(slug))
            await pubsub.aclose()

    return EventSourceResponse(event_generator())


@app.post("/b/{slug}/auth/generate")
async def generate_board_key(slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    existing_key = await rc.get_board_key(slug)

    if existing_key and existing_key != key:
        raise HTTPException(status_code=403, detail="Provide the existing key to regenerate.")

    new_key = generate_key()
    await rc.set_board_key(slug, new_key)
    return {"key": new_key}


@app.delete("/b/{slug}/auth")
async def remove_board_key(slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    existing_key = await rc.get_board_key(slug)
    if not existing_key:
        raise HTTPException(status_code=404, detail="This board has no key.")
    if existing_key != key:
        raise HTTPException(status_code=403, detail="Invalid key.")

    await rc.delete_board_key(slug)
    return Response(status_code=204)


# ═══════════════════════════════════════════════════════════════════════════
# SHARED BOARD ROUTES — /s/*  (internet-safe, mandatory auth, rate-limited)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/s/{slug}", response_class=HTMLResponse)
async def shared_board(request: Request, slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    board_key = await rc.get_board_key(slug, "shared")
    if not board_key:
        raise HTTPException(status_code=404, detail="Shared board not found.")

    if board_key != key:
        return templates.TemplateResponse(
            "auth_required.html",
            {"request": request, "slug": slug, "prefix": "s"},
            status_code=401,
        )

    entries = await rc.get_entries(slug, "shared")
    meta = await rc.get_shared_meta(slug)
    return templates.TemplateResponse(
        "board.html",
        {
            "request": request,
            "slug": slug,
            "key": key,
            "entries": entries,
            "has_key": True,
            "prefix": "s",
            "is_shared": True,
            "ttl_hours": meta.get("ttl_hours") if meta else None,
            "max_upload_mb": MAX_UPLOAD_SIZE_MB,
        },
    )


@app.post("/s/{slug}/entries")
async def create_shared_entry(request: Request, slug: str, key: Optional[str] = None):
    await require_rate_limit(request)
    # Verify shared board exists
    board_key = await rc.get_board_key(slug, "shared")
    if not board_key:
        raise HTTPException(status_code=404, detail="Shared board not found.")
    return await _create_entry_common(request, slug, key, ns="shared", prefix="s")


@app.delete("/s/{slug}/entries/{entry_id}")
async def delete_shared_entry(request: Request, slug: str, entry_id: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_rate_limit(request)
    await require_auth(slug, key, "shared")

    deleted = await rc.delete_entry(slug, entry_id, "shared")
    if deleted is None:
        raise HTTPException(status_code=404, detail="Entry not found.")

    await rc.publish_event(slug, "delete_entry", json.dumps({"id": entry_id}), "shared")
    return Response(status_code=204)


@app.get("/s/{slug}/entries/{entry_id}/image")
async def get_shared_image(request: Request, slug: str, entry_id: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_rate_limit(request)
    await require_auth(slug, key, "shared")

    raw = await rc.find_entry_raw(slug, entry_id, "shared")
    if raw is None:
        raise HTTPException(status_code=404, detail="Entry not found.")

    entry = Entry(**json.loads(raw))
    if not entry.image_path:
        raise HTTPException(status_code=404, detail="Not an image entry.")

    disk_path = DATA_DIR / entry.image_path
    if not disk_path.exists():
        raise HTTPException(status_code=404, detail="Image file not found.")

    return FileResponse(
        path=disk_path,
        media_type=entry.mime or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/s/{slug}/entries/{entry_id}/download")
async def download_shared_file(request: Request, slug: str, entry_id: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_rate_limit(request)
    await require_auth(slug, key, "shared")

    raw = await rc.find_entry_raw(slug, entry_id, "shared")
    if raw is None:
        raise HTTPException(status_code=404, detail="Entry not found.")

    entry = Entry(**json.loads(raw))
    if not entry.image_path:
        raise HTTPException(status_code=404, detail="Not a file entry.")

    disk_path = DATA_DIR / entry.image_path
    if not disk_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")

    if entry.file_name:
        download_name = entry.file_name
    else:
        ext = entry.image_path.rsplit(".", 1)[-1]
        ts = entry.created_at.replace(":", "-").replace(".", "-")[:19]
        download_name = f"clipboard_{ts}.{ext}"

    return FileResponse(
        path=disk_path,
        media_type=entry.mime or "application/octet-stream",
        filename=download_name,
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.get("/s/{slug}/stream")
async def shared_board_stream(request: Request, slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_auth(slug, key, "shared")

    async def event_generator():
        pubsub = rc.redis.pubsub()
        await pubsub.subscribe(rc._channel(slug, "shared"))
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    payload = json.loads(message["data"])
                    yield ServerSentEvent(
                        data=payload["data"],
                        event=payload["event"],
                    )
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(rc._channel(slug, "shared"))
            await pubsub.aclose()

    return EventSourceResponse(event_generator())


# ═══════════════════════════════════════════════════════════════════════════
# SHARED BOARD MANAGEMENT — /api/shared  (LAN-only)
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/shared")
async def create_shared_board(request: Request):
    body = await request.json()
    slug = body.get("slug", "").strip().lower()
    ttl_hours = int(body.get("ttl_hours", rc.SHARED_DEFAULT_TTL_HOURS))

    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    # Check if shared board already exists
    existing = await rc.get_shared_meta(slug)
    if existing:
        raise HTTPException(status_code=409, detail="Shared board already exists.")

    # Clamp TTL
    if ttl_hours < 1:
        ttl_hours = 1
    if ttl_hours > 168:  # max 7 days
        ttl_hours = 168

    new_key = generate_strong_key()
    await rc.set_shared_meta(slug, ttl_hours)
    await rc.set_board_key(slug, new_key, "shared")

    return {"slug": slug, "key": new_key, "ttl_hours": ttl_hours}


@app.delete("/api/shared/{slug}")
async def delete_shared_board_api(slug: str):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    meta = await rc.get_shared_meta(slug)
    if not meta:
        raise HTTPException(status_code=404, detail="Shared board not found.")

    await rc.delete_shared_board(slug)
    return Response(status_code=204)
