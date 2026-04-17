"""
Download and preview API endpoints.
Processed results remain tied to a paid email. Original-quality export requires paid access.
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


def _get_completed_task_or_404(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Task is not yet completed")

    if not task.result_path or not Path(task.result_path).exists():
        raise HTTPException(status_code=404, detail="Result file not found")

    return task


def _create_preview(source_path: str, task_id: str) -> str:
    """Create a 720p preview with watermark. Returns path to temp file."""
    img = Image.open(source_path)

    # Memory safety on Render free: tell the JPEG decoder to subsample during
    # decode for large AI outputs (e.g. 4000px upscaler results). Without this,
    # loading the full raster for a 6000px JPEG can exceed 500MB. draft() is a
    # hint — PIL picks the closest power-of-2 subsampling factor. Only affects
    # JPEGs; no-op for PNG/WEBP.
    try:
        img.draft("RGB", (MAX_FREE_WIDTH * 2, MAX_FREE_HEIGHT * 2))
    except Exception:
        pass

    # Convert to RGB if needed (handles RGBA, P mode, etc.)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Resize to fit within 1280x720 while keeping aspect ratio
    img.thumbnail((MAX_FREE_WIDTH, MAX_FREE_HEIGHT), Image.LANCZOS)

    watermark_text = "ArtImageHub.com"
    font_size = max(16, img.width // 25)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Draw watermark directly on an RGBA copy, then flatten to RGB
    # This keeps peak memory to 2 image objects instead of 4
    img_rgba = img.convert("RGBA")
    del img  # free the RGB copy immediately

    overlay = Image.new("RGBA", img_rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    bbox = draw.textbbox((0, 0), watermark_text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    padding = 15
    x = img_rgba.width - text_w - padding
    y = img_rgba.height - text_h - padding
    draw.text((x + 1, y + 1), watermark_text, fill=(0, 0, 0, 100), font=font)
    draw.text((x, y), watermark_text, fill=(255, 255, 255, 150), font=font)

    composited = Image.alpha_composite(img_rgba, overlay)
    del img_rgba
    del overlay

    img_final = composited.convert("RGB")
    del composited

    temp_dir = Path(tempfile.gettempdir()) / "artimagehub_previews"
    temp_dir.mkdir(exist_ok=True)
    preview_path = str(temp_dir / f"{task_id}_720p.jpg")
    img_final.save(preview_path, "JPEG", quality=85)
    del img_final

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
    """Download the processed result image in original quality for Pro users only."""
    task = _get_completed_task_or_404(task_id)

    client_ip = _get_client_ip(request)
    limit_check = check_download_limit(client_ip, email)

    if not limit_check["is_subscriber"]:
        raise HTTPException(
            status_code=402,
            detail="Paid download access is required for this photo. Use the same paid email that unlocked upload and processing.",
        )

    if quality != "original":
        raise HTTPException(
            status_code=400,
            detail="Use quality=original when requesting a paid download.",
        )

    record_download(client_ip, task_id)
    return FileResponse(
        path=task.result_path,
        media_type="image/jpeg",
        filename=f"artimagehub-{task_id}.jpg",
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "X-Subscriber": "true",
            "X-Quality": "original",
        },
    )


@router.get("/result-preview/{task_id}")
async def preview_result(task_id: str):
    """Serve a watermarked preview for in-browser comparison without export access."""
    task = _get_completed_task_or_404(task_id)
    preview_path = _create_preview(task.result_path, task_id)
    return FileResponse(
        path=preview_path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "X-Subscriber": "false",
            "X-Quality": "preview",
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
