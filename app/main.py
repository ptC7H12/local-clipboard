"""
LAN Clipboard — FastAPI application.

All board routes are prefixed with /b/ to avoid collisions with
framework routes (/docs, /health, /static, etc.).
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
from app.auth import generate_key, validate_slug
from app.models import Entry, EntryCreate, EntryResponse

DATA_DIR = Path("data")
IMAGES_DIR = DATA_DIR / "images"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_MB", "5")) * 1024 * 1024


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

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Helper: auth check
# ---------------------------------------------------------------------------

async def require_auth(slug: str, key: Optional[str]) -> None:
    """Raise HTTP 401 if the board has a key and the provided key is wrong."""
    board_key = await rc.get_board_key(slug)
    if board_key and board_key != key:
        raise HTTPException(status_code=401, detail="Invalid or missing board key.")


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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


# ---------------------------------------------------------------------------
# Board — main page
# ---------------------------------------------------------------------------

@app.get("/b/{slug}", response_class=HTMLResponse)
async def board(request: Request, slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    board_key = await rc.get_board_key(slug)
    if board_key and board_key != key:
        return templates.TemplateResponse(
            "auth_required.html",
            {"request": request, "slug": slug},
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
        },
    )


# ---------------------------------------------------------------------------
# Entries — create
# ---------------------------------------------------------------------------

@app.post("/b/{slug}/entries")
async def create_entry(request: Request, slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_auth(slug, key)

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

    else:  # image
        if not entry_in.content or not entry_in.mime:
            raise HTTPException(status_code=422, detail="content and mime required for image entries.")

        # Decode and size-check
        try:
            image_bytes = base64.b64decode(entry_in.content)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid base64 content.")

        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Image too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB).",
            )

        if entry_in.mime not in ("image/png", "image/jpeg"):
            raise HTTPException(status_code=422, detail="Only image/png and image/jpeg are supported.")

        ext = "png" if entry_in.mime == "image/png" else "jpg"
        filename = f"{entry_id}.{ext}"
        image_path = f"images/{filename}"
        disk_path = IMAGES_DIR / filename

        # Save original
        disk_path.write_bytes(image_bytes)

        # Generate thumbnail (200px wide, JPEG Q60)
        try:
            with Image.open(BytesIO(image_bytes)) as img:
                img = img.convert("RGB")
                ratio = 200 / img.width if img.width > 200 else 1.0
                thumb_size = (int(img.width * ratio), int(img.height * ratio))
                thumb = img.resize(thumb_size, Image.LANCZOS)
                buf = BytesIO()
                thumb.save(buf, format="JPEG", quality=60)
                thumbnail_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            # If thumbnail generation fails, store None — entry is still usable
            thumbnail_b64 = None

        entry = Entry(
            id=entry_id,
            type="image",
            image_path=image_path,
            thumbnail=thumbnail_b64,
            mime=entry_in.mime,
            created_at=now,
            file_size=len(image_bytes),
        )

    await rc.add_entry(slug, entry)

    # Publish SSE event (HTML fragment)
    fragment_html = templates.get_template("partials/entry.html").render(
        entry=entry, slug=slug, key=key
    )
    await rc.publish_event(slug, "new_entry", fragment_html)

    # HTMX request → return HTML fragment
    if request.headers.get("HX-Request"):
        return HTMLResponse(content=fragment_html)

    return JSONResponse(
        EntryResponse(id=entry_id, type=entry_in.type, created_at=now).model_dump()
    )


# ---------------------------------------------------------------------------
# Entries — delete
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Image endpoints
# ---------------------------------------------------------------------------

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
        raise HTTPException(status_code=404, detail="Not an image entry.")

    disk_path = DATA_DIR / entry.image_path
    if not disk_path.exists():
        raise HTTPException(status_code=404, detail="Image file not found.")

    ext = entry.image_path.rsplit(".", 1)[-1]
    # Derive a human-readable filename from the timestamp
    ts = entry.created_at.replace(":", "-").replace(".", "-")[:19]
    download_name = f"clipboard_{ts}.{ext}"

    return FileResponse(
        path=disk_path,
        media_type=entry.mime or "application/octet-stream",
        filename=download_name,
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

@app.get("/b/{slug}/stream")
async def board_stream(slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    await require_auth(slug, key)

    async def event_generator():
        pubsub = rc.redis.pubsub()
        await pubsub.subscribe(f"board:{slug}:channel")
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
            await pubsub.unsubscribe(f"board:{slug}:channel")
            await pubsub.aclose()

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/b/{slug}/auth/generate")
async def generate_board_key(slug: str, key: Optional[str] = None):
    if not validate_slug(slug):
        raise HTTPException(status_code=400, detail=f"Invalid board name: '{slug}'")

    existing_key = await rc.get_board_key(slug)

    # Allow if: board has no key, OR the correct key is provided
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
