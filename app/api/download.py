"""
Download API endpoint.
Free: 720p preview with watermark, 3/day limit.
Subscribers: original quality, unlimited.
"""
import logging
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont

from app.services.task_store import get_task, TaskStatus
from app.services.database import (
    check_download_limit,
    record_download,
)

logger = logging.getLogger("artimagehub.download")
router = APIRouter()

# 720p max dimensions (landscape or portrait)
MAX_FREE_WIDTH = 1280
MAX_FREE_HEIGHT = 720


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request (handles proxies)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _create_preview(source_path: str, task_id: str) -> str:
    """Create a 720p preview with watermark. Returns path to temp file."""
    img = Image.open(source_path)

    # Convert to RGB if needed (handles RGBA, P mode, etc.)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Resize to fit within 1280x720 while keeping aspect ratio
    img.thumbnail((MAX_FREE_WIDTH, MAX_FREE_HEIGHT), Image.LANCZOS)

    # Add watermark
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    watermark_text = "ArtImageHub.com"

    # Try to use a reasonable font size relative to image
    font_size = max(16, img.width // 25)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Get text bounding box
    bbox = draw.textbbox((0, 0), watermark_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # Position: bottom-right with padding
    padding = 15
    x = img.width - text_width - padding
    y = img.height - text_height - padding

    # Semi-transparent white text with dark shadow for readability
    draw.text((x + 1, y + 1), watermark_text, fill=(0, 0, 0, 100), font=font)
    draw.text((x, y), watermark_text, fill=(255, 255, 255, 150), font=font)

    # Composite watermark onto image
    img_rgba = img.convert("RGBA")
    img_rgba = Image.alpha_composite(img_rgba, overlay)
    img_final = img_rgba.convert("RGB")

    # Save to temp file
    temp_dir = Path(tempfile.gettempdir()) / "artimagehub_previews"
    temp_dir.mkdir(exist_ok=True)
    preview_path = str(temp_dir / f"{task_id}_720p.jpg")
    img_final.save(preview_path, "JPEG", quality=85)

    return preview_path


@router.get("/download/check-limit")
async def check_limit(request: Request, email: Optional[str] = Query(None)):
    """Check download limit for the current user (by IP + optional email)."""
    client_ip = _get_client_ip(request)
    result = check_download_limit(client_ip, email)
    return result


@router.get("/download/{task_id}")
async def download_result(
    request: Request,
    task_id: str,
    email: Optional[str] = Query(None),
    quality: Optional[str] = Query(None),
):
    """
    Download the processed result image.
    Free users: 720p preview with watermark, 3/day limit.
    Subscribers: original quality, unlimited.
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Task is not yet completed")

    if not task.result_path or not Path(task.result_path).exists():
        raise HTTPException(status_code=404, detail="Result file not found")

    client_ip = _get_client_ip(request)
    limit_check = check_download_limit(client_ip, email)

    # Subscriber with original quality requested → serve original
    if limit_check["is_subscriber"] and quality == "original":
        record_download(client_ip, task_id)
        return FileResponse(
            path=task.result_path,
            media_type="image/jpeg",
            filename=f"artimagehub-{task_id}.jpg",
            headers={
                "X-Subscriber": "true",
                "X-Remaining": "-1",
            },
        )

    # Free user — check daily limit
    if not limit_check["is_subscriber"] and not limit_check["allowed"]:
        raise HTTPException(
            status_code=429,
            detail="Daily download limit reached (3/day). Start a free trial for unlimited downloads.",
        )

    # Create 720p preview with watermark for free users
    if not limit_check["is_subscriber"]:
        preview_path = _create_preview(task.result_path, task_id)
        record_download(client_ip, task_id)
        remaining = limit_check["remaining"] - 1  # just used one
        return FileResponse(
            path=preview_path,
            media_type="image/jpeg",
            filename=f"artimagehub-{task_id}-preview.jpg",
            headers={
                "X-Subscriber": "false",
                "X-Remaining": str(remaining),
            },
        )

    # Subscriber requesting default (non-original) — still serve original
    record_download(client_ip, task_id)
    return FileResponse(
        path=task.result_path,
        media_type="image/jpeg",
        filename=f"artimagehub-{task_id}.jpg",
        headers={
            "X-Subscriber": "true",
            "X-Remaining": "-1",
        },
    )


@router.get("/preview/{task_id}")
async def preview_original(task_id: str):
    """Serve the original uploaded image for before/after comparison."""
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if not task.upload_path or not Path(task.upload_path).exists():
        raise HTTPException(status_code=404, detail="Original file not found")

    return FileResponse(
        path=task.upload_path,
        media_type="image/jpeg",
        filename=f"original-{task_id}.jpg",
    )
