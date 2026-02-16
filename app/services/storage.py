"""
Local file storage service for MVP.
Stores uploaded and processed images on local filesystem.
"""
import os
import uuid
import aiofiles
from pathlib import Path

UPLOAD_DIR = Path("uploads")
RESULT_DIR = Path("results")


def _ensure_dirs():
    UPLOAD_DIR.mkdir(exist_ok=True)
    RESULT_DIR.mkdir(exist_ok=True)


_ensure_dirs()


def _ext_from_content_type(content_type: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    return mapping.get(content_type, ".jpg")


async def save_upload(content: bytes, content_type: str) -> tuple[str, str]:
    """Save uploaded file. Returns (file_id, file_path)."""
    file_id = uuid.uuid4().hex
    ext = _ext_from_content_type(content_type)
    filename = f"{file_id}{ext}"
    filepath = UPLOAD_DIR / filename
    async with aiofiles.open(filepath, "wb") as f:
        await f.write(content)
    return file_id, str(filepath)


async def save_result(content: bytes, task_id: str, suffix: str = ".jpg") -> str:
    """Save processed result. Returns file path."""
    filename = f"{task_id}_result{suffix}"
    filepath = RESULT_DIR / filename
    async with aiofiles.open(filepath, "wb") as f:
        await f.write(content)
    return str(filepath)


def get_result_path(task_id: str) -> str | None:
    """Find result file for a task."""
    for ext in [".jpg", ".png", ".webp"]:
        path = RESULT_DIR / f"{task_id}_result{ext}"
        if path.exists():
            return str(path)
    return None


def get_upload_path(file_id: str) -> str | None:
    """Find uploaded file by ID."""
    for ext in [".jpg", ".png", ".webp"]:
        path = UPLOAD_DIR / f"{file_id}{ext}"
        if path.exists():
            return str(path)
    return None
