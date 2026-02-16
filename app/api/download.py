"""
Download API endpoint.
Free: 720p preview. Subscribers: original quality, no watermark.
"""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.services.task_store import get_task, TaskStatus
from app.services.database import is_user_active

router = APIRouter()


@router.get("/download/{task_id}")
async def download_result(task_id: str, email: Optional[str] = Query(None)):
    """
    Download the processed result image.
    Free users: 720p preview (future: resize + watermark).
    Subscribers (email with active sub): original quality.
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Task is not yet completed")

    if not task.result_path or not Path(task.result_path).exists():
        raise HTTPException(status_code=404, detail="Result file not found")

    # Check subscription for original quality
    is_subscriber = False
    if email:
        is_subscriber = is_user_active(email.lower().strip())

    # TODO: For non-subscribers, resize to 720p and add watermark
    # For MVP, we serve the full image but track subscriber status
    return FileResponse(
        path=task.result_path,
        media_type="image/jpeg",
        filename=f"artimagehub-{task_id}.jpg",
        headers={
            "X-Subscriber": "true" if is_subscriber else "false",
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
