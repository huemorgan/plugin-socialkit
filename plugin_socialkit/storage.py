"""On-disk store for downloaded ad visuals, shared by the tool layer and route.

SocialKit's variant `imageUrl`s are Bearer-gated, so the chat embed cannot load
them directly, and inlining ~1.5MB of base64 into a tool result would blow the
model's context. Instead the tool downloads the bytes once, writes them here,
and hands the embed a tiny public URL (`/api/p/plugin-socialkit/file/<id>`).
Files live under a temp dir so already-rendered chat images keep working across
a server restart (until the OS clears temp).

No `luna_sdk` import here — pure stdlib so it unit-tests anywhere.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

URL_PREFIX = "/api/p/plugin-socialkit/file"

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def store_dir() -> Path:
    """Where images are written. Overridable via `LUNA_SOCIALKIT_DIR`."""
    override = os.environ.get("LUNA_SOCIALKIT_DIR")
    return Path(override) if override else Path(tempfile.gettempdir()) / "luna-socialkit"


def _ext_for(mime: str) -> str:
    return _EXT_BY_MIME.get((mime or "").lower().split(";")[0].strip(), "png")


def save_image(data: bytes, mime: str = "image/png") -> dict[str, str | int]:
    """Persist image bytes and return its id, absolute path, served URL, mime, size."""
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{_ext_for(mime)}"
    path = d / name
    path.write_bytes(data)
    return {
        "id": name,
        "path": str(path),
        "url": f"{URL_PREFIX}/{name}",
        "mime": (mime or "image/png").split(";")[0],
        "bytes": len(data),
    }


def resolve(name: str) -> Path | None:
    """Map a served file name back to a real path, rejecting traversal."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = store_dir() / name
    if path.is_file():
        return path
    return None
