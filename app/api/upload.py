"""
Upload API endpoint.
Handles image upload, validation, and kicks off processing.
"""
import asyncio

from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from pydantic import BaseModel

from app.services.storage import save_upload
from app.services.task_store import create_task, update_task, TaskStatus
from app.services.ai_service import get_ai_service
from app.services.storage import RESULT_DIR

router = APIRouter()


class UploadResponse(BaseModel):
    task_id: str
    status: str
    message: str


@router.post("/upload", response_model=UploadResponse)
async def upload_image(
    file: UploadFile = File(...),
    colorize: bool = Form(False),
):
    """
    Upload an image for AI restoration.
    Accepts JPG, PNG, WEBP up to 20MB.
    Returns a task_id to poll for status.
    """
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/webp"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{file.content_type}'. Allowed: JPG, PNG, WEBP.",
        )

    # Read and validate size
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 20MB.")

    if len(content) < 100:
        raise HTTPException(status_code=400, detail="File too small or corrupt.")

    # Save to local storage
    file_id, upload_path = await save_upload(content, file.content_type)

    # Create task
    task = create_task(file_id=file_id, upload_path=upload_path, colorize=colorize)

    # Start background processing
    asyncio.create_task(_process_task(task.id))

    return UploadResponse(
        task_id=task.id,
        status="pending",
        message="Image uploaded. Processing will begin shortly.",
    )


async def _process_task(task_id: str):
    """Background task: run AI pipeline and update task status."""
    import logging

    logger = logging.getLogger("colorbyte.process")

    from app.services.task_store import get_task

    task = get_task(task_id)
    if task is None:
        return

    try:
        update_task(task_id, status=TaskStatus.PROCESSING, progress=0, stage="Starting...")

        result_path = str(RESULT_DIR / f"{task_id}_result.jpg")

        async def on_progress(stage: str, progress: int):
            update_task(task_id, stage=stage, progress=progress)

        ai = get_ai_service()
        result = await ai.process_photo(
            input_path=task.upload_path,
            output_path=result_path,
            colorize=task.colorize,
            progress_callback=on_progress,
        )

        if result.success:
            update_task(
                task_id,
                status=TaskStatus.COMPLETED,
                progress=100,
                stage="Complete",
                result_path=result.output_path,
            )
            logger.info("Task %s completed successfully", task_id)
        else:
            update_task(
                task_id,
                status=TaskStatus.FAILED,
                stage="Failed",
                error=result.error or "Processing failed",
            )
            logger.warning("Task %s failed: %s", task_id, result.error)
    except Exception as exc:
        logger.exception("Task %s crashed: %s", task_id, exc)
        update_task(
            task_id,
            status=TaskStatus.FAILED,
            stage="Failed",
            error=f"Unexpected error: {exc}",
        )
