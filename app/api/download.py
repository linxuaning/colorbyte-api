"""
Download API endpoint.
Serves processed images. MVP: 720p free download without watermark.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.task_store import get_task, TaskStatus

router = APIRouter()


@router.get("/download/{task_id}")
async def download_result(task_id: str):
    """
    Download the processed result image.
    MVP: returns the full result directly (watermark/resize deferred).
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Task is not yet completed")

    if not task.result_path or not Path(task.result_path).exists():
        raise HTTPException(status_code=404, detail="Result file not found")

    return FileResponse(
        path=task.result_path,
        media_type="image/jpeg",
        filename=f"artimagehub-{task_id}.jpg",
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
