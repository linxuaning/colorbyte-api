"""Internal synchronous JSON restore API.

Contract:
POST /api/restore
Header: X-Internal-Key: <internal key>
Body: {"image": "<base64>", "task": "restore"}
Success: {"ok": true, "result": "<base64>"}
Failure: {"ok": false, "error": "reason"}
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Header
from pydantic import BaseModel

from app.config import get_settings
from app.services.ai_service import get_ai_service
from app.services.storage import RESULT_DIR, UPLOAD_DIR

logger = logging.getLogger("artimagehub.restore")
router = APIRouter()

MAX_IMAGE_BYTES = 10 * 1024 * 1024
SUPPORTED_TASKS = {"restore", "colorize", "enhance"}


class RestoreRequest(BaseModel):
    image: str
    task: str = "restore"


def _failure(error: str) -> dict:
    return {"ok": False, "error": error}


def _decode_base64_image(value: str) -> bytes:
    payload = value.strip()
    if "," in payload and payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[1]

    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image must be a valid base64 string") from exc

    if not data:
        raise ValueError("image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image too large; maximum size is 10 MB")
    return data


def _detect_ext(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("unsupported image format; expected JPEG, PNG, or WEBP")


async def _run_restore(input_path: str, output_path: str, task: str):
    ai = get_ai_service()

    async def on_progress(stage: str, progress: int) -> None:
        logger.info("/api/restore %s%% %s", progress, stage)

    if task == "colorize":
        return await ai.process_photo(
            input_path=input_path,
            output_path=output_path,
            colorize=True,
            progress_callback=on_progress,
            email="internal@artimagehub.local",
        )

    # `enhance` currently maps to the restoration provider. The task value is
    # preserved at the API boundary so callers can route future variants without
    # changing the JSON contract.
    return await ai.process_photo(
        input_path=input_path,
        output_path=output_path,
        colorize=False,
        progress_callback=on_progress,
        email="internal@artimagehub.local",
    )


@router.post("/restore")
async def restore_image(
    request: RestoreRequest,
    x_internal_key: str | None = Header(default=None, alias="X-Internal-Key"),
) -> dict:
    settings = get_settings()
    if x_internal_key != settings.internal_api_key:
        return _failure("Unauthorized")

    task = (request.task or "restore").strip().lower()
    if task not in SUPPORTED_TASKS:
        return _failure(f"unsupported task '{request.task}'")

    input_path: Path | None = None
    output_path: Path | None = None

    try:
        image_bytes = _decode_base64_image(request.image)
        ext = _detect_ext(image_bytes)

        request_id = uuid.uuid4().hex
        input_path = UPLOAD_DIR / f"internal_{request_id}{ext}"
        output_path = RESULT_DIR / f"internal_{request_id}_result.jpg"
        input_path.write_bytes(image_bytes)

        result = await _run_restore(str(input_path), str(output_path), task)
        if not result.success or not result.output_path:
            return _failure(result.error or "restore failed")

        result_bytes = Path(result.output_path).read_bytes()
        encoded = base64.b64encode(result_bytes).decode("ascii")
        return {"ok": True, "result": encoded}
    except Exception as exc:
        logger.exception("/api/restore failed")
        return _failure(str(exc))
    finally:
        for path in (input_path, output_path):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
