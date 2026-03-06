from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, field_validator


class EntryCreate(BaseModel):
    type: Literal["text", "image", "file"]
    content: Optional[str] = None
    mime: Optional[str] = None
    file_name: Optional[str] = None  # Original filename for file uploads

    @field_validator("content")
    @classmethod
    def content_required_for_text(cls, v: Optional[str], info) -> Optional[str]:
        # Full validation happens in the route handler (size check etc.)
        return v


class Entry(BaseModel):
    id: str
    type: Literal["text", "image", "file"]
    content: Optional[str] = None      # Plaintext for text entries; None for images/files
    image_path: Optional[str] = None   # Relative path: "images/{uuid}.{ext}" (images & files)
    thumbnail: Optional[str] = None    # Base64-encoded JPEG thumbnail (~15 KB)
    mime: Optional[str] = None
    file_size: Optional[int] = None    # Original file size in bytes
    file_name: Optional[str] = None    # Original filename (file entries)
    created_at: str                    # ISO 8601 timestamp


class EntryResponse(BaseModel):
    id: str
    type: Literal["text", "image", "file"]
    created_at: str
