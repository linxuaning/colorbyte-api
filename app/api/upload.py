"""
Upload API endpoint.
Handles image upload and kicks off processing for the pay-first workflow.
"""
import asyncio
import io

from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from PIL import Image as PILImage
from pydantic import BaseModel

# Cap longest edge to reduce Render free-tier memory pressure
_MAX_INPUT_LONG_EDGE = 1200   # cap uploaded input before AI processing
_MAX_RESULT_LONG_EDGE = 1600  # cap AI result before saving (upscalers can 4× the size)


def _resize_if_needed(content: bytes) -> bytes:
    """Resize image so longest edge ≤ _MAX_INPUT_LONG_EDGE, re-encode as JPEG 85%.
    Returns original bytes unchanged on any error."""
    try:
        img = PILImage.open(io.BytesIO(content))
        w, h = img.size
        if max(w, h) <= _MAX_INPUT_LONG_EDGE:
            return content
        scale = _MAX_INPUT_LONG_EDGE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return content


def _cap_result_image(path: str) -> None:
    """Cap result image longest edge ≤ _MAX_RESULT_LONG_EDGE in-place. No-op on error."""
    try:
        img = PILImage.open(path)
        w, h = img.size
        if max(w, h) <= _MAX_RESULT_LONG_EDGE:
            return
        scale = _MAX_RESULT_LONG_EDGE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(path, format="JPEG", quality=85)
    except Exception:
        pass

from app.services.storage import save_upload
from app.services.task_store import create_task, update_task, TaskStatus
from app.services.ai_service import get_ai_service
from app.services.storage import RESULT_DIR
from app.services.database import is_feature_entitled, record_processing_complete, upsert_persistent_task
from app.services.alert_email import send_payment_failure_alert

router = APIRouter()


class UploadResponse(BaseModel):
    task_id: str
    status: str
    message: str


@router.post("/upload", response_model=UploadResponse)
async def upload_image(
    file: UploadFile = File(...),
    colorize: bool = Form(False),
    email: str = Form(""),
    feature_key: str = Form("restoration"),
    landing_page: str = Form(""),
    cta_slot: str = Form(""),
    entry_variant: str = Form(""),
    checkout_source: str = Form(""),
    internal_key: str = Form(""),
):
    """
    Upload an image for AI restoration.
    Accepts JPG, PNG, WEBP up to 20MB.
    Upload and processing are only available after payment with the same email.
    Internal service-to-service calls may pass internal_key to bypass the payment check.
    """
    from app.config import get_settings
    settings = get_settings()
    is_internal = bool(internal_key and internal_key == settings.internal_api_key)

    normalized_email = email.strip().lower()

    if not is_internal:
        if not normalized_email:
            raise HTTPException(
                status_code=402,
                detail="Paid access is required before upload and processing. Complete checkout first, then return with the same email.",
            )

        if not is_feature_entitled(normalized_email, feature_key):
            raise HTTPException(
                status_code=402,
                detail=f"Access to '{feature_key}' requires a separate $4.99 unlock. Complete checkout for this feature, then return to start.",
            )

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

    # Resize to reduce memory pressure on Render free tier
    content = _resize_if_needed(content)

    # Save to local storage
    file_id, upload_path = await save_upload(content, file.content_type)

    # Create task
    task = create_task(
        file_id=file_id,
        upload_path=upload_path,
        colorize=colorize,
        email=normalized_email,
        feature_key=feature_key.strip() or "restoration",
        landing_page=landing_page.strip() or None,
        cta_slot=cta_slot.strip() or None,
        entry_variant=entry_variant.strip() or None,
        checkout_source=checkout_source.strip() or None,
    )
    try:
        import json
        from dataclasses import asdict

        upsert_persistent_task(
            task.id,
            json.dumps(asdict(task)),
            upload_bytes=content,
            upload_content_type=file.content_type,
        )
    except Exception:
        # Do not block paid users on the durability side-write; task_store still
        # has the local copy and processing can continue.
        pass

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

    logger = logging.getLogger("artimagehub.process")

    from app.services.task_store import get_task

    task = get_task(task_id)
    if task is None:
        return

    try:
        update_task(task_id, status=TaskStatus.PROCESSING, progress=0, stage="Starting...")

        result_path = str(RESULT_DIR / f"{task_id}_result.jpg")

        async def on_progress(stage: str, progress: int):
            update_task(task_id, stage=stage, progress=progress)

        from app.services.database import FEATURE_DENOISING, FEATURE_DEBLURRING, FEATURE_JPEG_FIX
        ai = get_ai_service()
        if task.feature_key == FEATURE_DENOISING:
            result = await ai.denoise_photo(
                input_path=task.upload_path,
                output_path=result_path,
                progress_callback=on_progress,
                email=task.email,
            )
        elif task.feature_key == FEATURE_DEBLURRING:
            result = await ai.deblur_photo(
                input_path=task.upload_path,
                output_path=result_path,
                progress_callback=on_progress,
                email=task.email,
            )
        elif task.feature_key == FEATURE_JPEG_FIX:
            result = await ai.fix_jpeg_artifacts(
                input_path=task.upload_path,
                output_path=result_path,
                progress_callback=on_progress,
                email=task.email,
            )
        else:
            result = await ai.process_photo(
                input_path=task.upload_path,
                output_path=result_path,
                colorize=task.colorize,
                progress_callback=on_progress,
                email=task.email,
            )

        if result.success:
            # Do NOT cap the AI output. The paid "HD Original" download must
            # serve the true AI output, not a 1600px JPEG q85 reread. Memory
            # safety on Render free during preview generation is handled in
            # download._create_preview via PIL.Image.draft() subsampling.

            mode = task.feature_key if task.feature_key != "restoration" else ("colorize" if task.colorize else "restore")
            record_processing_complete(
                task_id=task_id,
                mode=mode,
                landing_page=task.landing_page,
                cta_slot=task.cta_slot,
                entry_variant=task.entry_variant,
                checkout_source=task.checkout_source,
                provider_used=result.provider_used,
                provider_backend=result.provider_backend,
            )
            update_task(
                task_id,
                status=TaskStatus.COMPLETED,
                progress=100,
                stage="Complete",
                result_path=result.output_path,
                provider_used=result.provider_used,
                provider_backend=result.provider_backend,
            )
            try:
                import json
                from dataclasses import asdict
                from app.services.task_store import get_task

                completed_task = get_task(task_id)
                if completed_task and result.output_path:
                    upsert_persistent_task(
                        task_id,
                        json.dumps(asdict(completed_task)),
                        result_bytes=Path(result.output_path).read_bytes(),
                        result_content_type="image/jpeg",
                    )
            except Exception:
                logger.warning("Task %s persistent result side-write failed", task_id, exc_info=True)
            logger.info(
                "Task %s completed successfully provider=%s backend=%s",
                task_id,
                result.provider_used,
                result.provider_backend,
            )
        else:
            update_task(
                task_id,
                status=TaskStatus.FAILED,
                stage="Failed",
                error=result.error or "Processing failed",
            )
            logger.warning("Task %s failed: %s", task_id, result.error)
            send_payment_failure_alert(
                alert_type="processing_failed",
                customer_email=task.email,
                error_msg=result.error or "Processing failed",
                extra={"task_id": task_id},
            )
    except Exception as exc:
        logger.exception("Task %s crashed: %s", task_id, exc)
        update_task(
            task_id,
            status=TaskStatus.FAILED,
            stage="Failed",
            error=f"Unexpected error: {exc}",
        )
        send_payment_failure_alert(
            alert_type="processing_failed",
            customer_email=task.email,
            error_msg=str(exc),
            extra={"task_id": task_id},
        )
