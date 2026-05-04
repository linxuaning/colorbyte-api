"""
Render-side proxy endpoint for /api/inpaint/lama (object remover).

Behaviour:
  1. Payment gate — mirrors upload.py (`is_user_active` + 402 if not paid)
  2. Multipart in: image + mask (image bytes / png mask), email + funnel cols
  3. Primary: forward to 192.168.68.221 via Cloudflare Tunnel
       env LAMA_INFERENCE_URL + LAMA_INFERENCE_TOKEN
       12s connect timeout, 60s read timeout (LaMa inference can take 5-15s on GPU,
       up to 30-45s if downscaled CPU; cross-region added latency budget per memory
       feedback_timeout_calibration_for_global_users)
  4. Fallback (silent, never surfaced as error): HuggingFace Space `Carve/LaMa-Demo`
     via gradio_client. Best-effort; if it also fails, return 503.
  5. Returns image/png on success.

UI contract: a 503 from this endpoint means "model temporarily unavailable —
retry". The UI must show a generic retry banner, NOT any "tunnel" or "HF" detail.
"""
import asyncio
import io
import logging
import os
import tempfile
from typing import Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image as PILImage

from app.config import get_settings
from app.services.database import is_user_active

router = APIRouter()
logger = logging.getLogger("artimagehub.inpaint")

# Hardened timeouts per memory feedback_timeout_calibration_for_global_users.md
_CONNECT_TIMEOUT_S = 12.0  # cross-region China→US/SG RTT can spike to 8-10s
_READ_TIMEOUT_S = 60.0     # LaMa CPU inference body upper bound

_HF_FALLBACK_SPACES = [
    # (space_id, api_name)
    ("Carve/LaMa-Demo", "/predict"),
    # alt: generative inpainting (different model, but covers when LaMa space dead)
    # left blank for now; gradio API contract differs, add when first space is verified
]

_MAX_BYTES = 25 * 1024 * 1024


async def _try_primary(image_bytes: bytes, mask_bytes: bytes) -> Optional[bytes]:
    """POST to 192.168.68.221 via tunnel. Returns PNG bytes or None on any failure."""
    settings = get_settings()
    url = (settings.lama_inference_url or "").rstrip("/")
    token = settings.lama_inference_token or ""

    if not url or not token:
        logger.info("LaMa primary not configured (missing URL or token); skipping to fallback")
        return None

    timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT_S, read=_READ_TIMEOUT_S, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            files = {
                "image": ("image.bin", image_bytes, "application/octet-stream"),
                "mask": ("mask.png", mask_bytes, "image/png"),
            }
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.post(f"{url}/inpaint", files=files, headers=headers)
            if resp.status_code == 200:
                logger.info(
                    "LaMa primary ok (dur=%sms, size=%d bytes)",
                    resp.headers.get("X-Inference-Duration-Ms", "?"),
                    len(resp.content),
                )
                return resp.content
            logger.warning("LaMa primary returned %s: %s", resp.status_code, resp.text[:200])
            return None
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
        logger.warning("LaMa primary network error: %s", exc)
        return None
    except Exception as exc:
        logger.exception("LaMa primary unexpected error: %s", exc)
        return None


async def _try_hf_fallback(image_bytes: bytes, mask_bytes: bytes) -> Optional[bytes]:
    """Fallback to HF Spaces. Best-effort; returns None on failure."""
    try:
        from gradio_client import Client, file as gr_file
    except ImportError:
        logger.error("gradio_client not installed; HF fallback unavailable")
        return None

    for space_id, api_name in _HF_FALLBACK_SPACES:
        img_path = mask_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f_img:
                f_img.write(image_bytes)
                img_path = f_img.name
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f_mask:
                f_mask.write(mask_bytes)
                mask_path = f_mask.name

            logger.info("LaMa HF fallback: %s", space_id)

            def _call():
                client = Client(space_id, hf_token=os.environ.get("HF_TOKEN") or None)
                return client.predict(gr_file(img_path), gr_file(mask_path), api_name=api_name)

            result = await asyncio.wait_for(asyncio.to_thread(_call), timeout=_READ_TIMEOUT_S)
            # gradio_client returns a local file path
            if isinstance(result, str) and os.path.exists(result):
                with open(result, "rb") as f:
                    raw = f.read()
                # Re-encode to PNG to normalise
                img = PILImage.open(io.BytesIO(raw)).convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
            logger.warning("HF fallback %s returned unexpected: %s", space_id, type(result))
        except asyncio.TimeoutError:
            logger.warning("HF fallback %s timed out", space_id)
        except Exception as exc:
            logger.warning("HF fallback %s failed: %s", space_id, exc)
        finally:
            for p in (img_path, mask_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
    return None


@router.post("/inpaint/lama")
async def inpaint_lama(
    image: UploadFile = File(..., description="Source image"),
    mask: UploadFile = File(..., description="Mask PNG (white=remove)"),
    email: str = Form(""),
    landing_page: str = Form(""),
    cta_slot: str = Form(""),
    entry_variant: str = Form(""),
    checkout_source: str = Form(""),
    internal_key: str = Form(""),
):
    """LaMa-powered inpainting (object remover). Pay-first: requires active user."""
    settings = get_settings()
    is_internal = bool(internal_key and internal_key == settings.internal_api_key)
    normalized_email = email.strip().lower()

    # Payment gate (mirrors upload.py exactly — pay-first hard rule)
    if not is_internal:
        if not normalized_email:
            raise HTTPException(
                status_code=402,
                detail="Paid access required. Complete checkout first, then return with the same email.",
            )
        if not is_user_active(normalized_email):
            raise HTTPException(
                status_code=402,
                detail="Paid access required. Complete checkout with this email, then return to start.",
            )

    if image.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(status_code=400, detail=f"Invalid image type: {image.content_type}")

    image_bytes = await image.read()
    mask_bytes = await mask.read()

    if len(image_bytes) > _MAX_BYTES or len(mask_bytes) > _MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 25MB)")
    if len(image_bytes) < 100 or len(mask_bytes) < 100:
        raise HTTPException(status_code=400, detail="Image or mask is empty/corrupt")

    # Primary path
    result = await _try_primary(image_bytes, mask_bytes)
    if result is None:
        # Silent fallback
        result = await _try_hf_fallback(image_bytes, mask_bytes)

    if result is None:
        # Generic 503 — UI must show retry banner, no leakage of "tunnel" / "HF"
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please try again in a moment.",
        )

    return Response(content=result, media_type="image/png")
