#!/usr/bin/env python3
"""
ArtImageHub Mac inference server — 192.168.68.221
Endpoints:
  POST /denoise   — NAFNet-SIDD (real-world noise removal)
  POST /deblur    — NAFNet-GoPro (motion/defocus deblur)
  POST /jpeg-fix  — SwinIR color_jpeg_car (JPEG artifact removal)
  POST /inpaint   — LaMa (object removal, if lama_server.py is merged here)
  GET  /health    — liveness probe

Auth: Authorization: Bearer <token>  (token in ~/.lama-server-token or $INFERENCE_TOKEN)
Exposed to Render backend via Cloudflare Tunnel (LAMA_INFERENCE_URL env var on Render).
"""
import io
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("inference_server")

# ── Configuration ──────────────────────────────────────────────────────────────

INFERENCE_ROOT = Path(os.environ.get("INFERENCE_ROOT", Path.home() / "inference-server"))
MODELS_DIR = INFERENCE_ROOT / "models"
NAFNET_DIR = INFERENCE_ROOT / "NAFNet"
SWINIR_DIR = INFERENCE_ROOT / "SwinIR"

_TOKEN_FILE = Path.home() / ".lama-server-token"
SERVER_TOKEN: str = os.environ.get("INFERENCE_TOKEN") or (
    _TOKEN_FILE.read_text().strip() if _TOKEN_FILE.exists() else ""
)

# ── Device ─────────────────────────────────────────────────────────────────────

if torch.backends.mps.is_available():
    DEVICE = torch.device("mps")
elif torch.cuda.is_available():
    DEVICE = torch.device("cuda")
else:
    DEVICE = torch.device("cpu")
logger.info("Using device: %s", DEVICE)

# ── Lazy model registry ────────────────────────────────────────────────────────

_models: dict = {}


def _load_nafnet(task: str):
    """Load NAFNet for SIDD (denoise) or GoPro (deblur). Requires basicsr installed."""
    if str(NAFNET_DIR) not in sys.path:
        sys.path.insert(0, str(NAFNET_DIR))

    from basicsr.models.archs.NAFNet_arch import NAFNet  # type: ignore[import]

    if task == "SIDD":
        model_path = MODELS_DIR / "NAFNet-SIDD-width64.pth"
        kwargs = dict(
            img_channel=3, width=64, middle_blk_num=12,
            enc_blk_nums=[2, 2, 4, 8], dec_blk_nums=[2, 2, 2, 2],
        )
    elif task == "GoPro":
        model_path = MODELS_DIR / "NAFNet-GoPro-width64.pth"
        kwargs = dict(
            img_channel=3, width=64, middle_blk_num=1,
            enc_blk_nums=[1, 1, 1, 28], dec_blk_nums=[1, 1, 1, 1],
        )
    else:
        raise ValueError(f"Unknown NAFNet task: {task}")

    if not model_path.exists():
        raise FileNotFoundError(f"NAFNet weights not found: {model_path}")

    logger.info("Loading NAFNet-%s from %s...", task, model_path)
    model = NAFNet(**kwargs)
    ckpt = torch.load(model_path, map_location=DEVICE, weights_only=True)
    model.load_state_dict(ckpt.get("params", ckpt))
    model.eval().to(DEVICE)
    logger.info("NAFNet-%s ready on %s", task, DEVICE)
    return model


def _load_swinir_jpeg():
    """Load SwinIR color_jpeg_car model. Requires SwinIR repo cloned."""
    swinir_models_dir = SWINIR_DIR / "models"
    if str(swinir_models_dir) not in sys.path and str(SWINIR_DIR) not in sys.path:
        sys.path.insert(0, str(SWINIR_DIR))

    from models.network_swinir import SwinIR  # type: ignore[import]

    model_path = MODELS_DIR / "006_CAR_DFWB_s126w7_SwinIR-M_jpeg40.pth"
    if not model_path.exists():
        raise FileNotFoundError(f"SwinIR weights not found: {model_path}")

    logger.info("Loading SwinIR JPEG from %s...", model_path)
    model = SwinIR(
        upscale=1, in_chans=1, img_size=126, window_size=7,
        img_range=255.0, depths=[6, 6, 6, 6, 6, 6], embed_dim=180,
        num_heads=[6, 6, 6, 6, 6, 6], mlp_ratio=2,
        upsampler=None, resi_connection="1conv",
    )
    ckpt = torch.load(model_path, map_location=DEVICE, weights_only=True)
    state_dict = ckpt.get("params", ckpt)
    # SwinIR checkpoint may have 'params_ema' preferred over 'params'
    if "params_ema" in ckpt:
        state_dict = ckpt["params_ema"]
    model.load_state_dict(state_dict)
    model.eval().to(DEVICE)
    logger.info("SwinIR JPEG ready on %s", DEVICE)
    return model


def _get_model(key: str):
    if key not in _models:
        if key == "nafnet_SIDD":
            _models[key] = _load_nafnet("SIDD")
        elif key == "nafnet_GoPro":
            _models[key] = _load_nafnet("GoPro")
        elif key == "swinir_jpeg":
            _models[key] = _load_swinir_jpeg()
        else:
            raise ValueError(f"Unknown model key: {key}")
    return _models[key]


# ── Tensor / image utilities ───────────────────────────────────────────────────

def _to_tensor(arr: np.ndarray) -> torch.Tensor:
    """HWC uint8 → 1CHW float32 in [0,1]."""
    return (
        torch.from_numpy(arr.astype(np.float32))
        .div(255.0)
        .permute(2, 0, 1)
        .unsqueeze(0)
        .to(DEVICE)
    )


def _from_tensor(t: torch.Tensor) -> np.ndarray:
    """1CHW float32 → HWC uint8."""
    return (
        t.squeeze(0).permute(1, 2, 0).clamp(0.0, 1.0).cpu().numpy() * 255
    ).astype(np.uint8)


def _pad_multiple(arr: np.ndarray, mult: int) -> Tuple[np.ndarray, Tuple[int, int]]:
    """Reflect-pad HWC array so H and W are multiples of `mult`."""
    h, w = arr.shape[:2]
    ph = (mult - h % mult) % mult
    pw = (mult - w % mult) % mult
    if ph or pw:
        arr = np.pad(arr, ((0, ph), (0, pw), (0, 0)), mode="reflect")
    return arr, (h, w)


def _run_nafnet(model, img: Image.Image) -> Image.Image:
    arr = np.array(img)
    arr_pad, (oh, ow) = _pad_multiple(arr, 32)
    with torch.no_grad():
        out = model(_to_tensor(arr_pad))
    return Image.fromarray(_from_tensor(out)[:oh, :ow])


def _run_swinir_jpeg(model, img: Image.Image) -> Image.Image:
    """Run grayscale JPEG-CAR model on Y channel, reconstruct YCbCr → RGB."""
    ycbcr = img.convert("YCbCr")
    y, cb, cr = ycbcr.split()
    arr = np.array(y)[:, :, np.newaxis]  # HW1
    arr_pad, (oh, ow) = _pad_multiple(arr, 7)
    with torch.no_grad():
        t = torch.from_numpy(arr_pad.astype(np.float32)).div(255.0).permute(2, 0, 1).unsqueeze(0).to(DEVICE)
        out = model(t)
    y_out = (out.squeeze(0).squeeze(0).clamp(0.0, 1.0).cpu().numpy()[:oh, :ow] * 255).astype(np.uint8)
    y_img = Image.fromarray(y_out, mode="L")
    result = Image.merge("YCbCr", [y_img, cb, cr]).convert("RGB")
    return result


def _to_jpeg_bytes(img: Image.Image, quality: int = 95) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ── Auth ───────────────────────────────────────────────────────────────────────

def _require_auth(authorization: Optional[str]) -> None:
    if not SERVER_TOKEN:
        return  # no token → dev/open mode
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if authorization[len("Bearer "):] != SERVER_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="ArtImageHub Inference Server", version="1.0.0")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "device": str(DEVICE),
        "loaded_models": list(_models.keys()),
        "models_dir": str(MODELS_DIR),
    }


@app.post("/denoise")
async def denoise(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Denoise photo using NAFNet-SIDD (real-world noise removal)."""
    _require_auth(authorization)
    t0 = time.monotonic()
    try:
        img = Image.open(io.BytesIO(await file.read())).convert("RGB")
        result = _run_nafnet(_get_model("nafnet_SIDD"), img)
        body = _to_jpeg_bytes(result)
        ms = int((time.monotonic() - t0) * 1000)
        logger.info("denoise ok %dx%d → %d bytes in %dms", img.width, img.height, len(body), ms)
        return Response(
            content=body,
            media_type="image/jpeg",
            headers={"X-Inference-Duration-Ms": str(ms)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("denoise error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/deblur")
async def deblur(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Deblur photo using NAFNet-GoPro (motion/defocus blur removal)."""
    _require_auth(authorization)
    t0 = time.monotonic()
    try:
        img = Image.open(io.BytesIO(await file.read())).convert("RGB")
        result = _run_nafnet(_get_model("nafnet_GoPro"), img)
        body = _to_jpeg_bytes(result)
        ms = int((time.monotonic() - t0) * 1000)
        logger.info("deblur ok %dx%d → %d bytes in %dms", img.width, img.height, len(body), ms)
        return Response(
            content=body,
            media_type="image/jpeg",
            headers={"X-Inference-Duration-Ms": str(ms)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("deblur error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/jpeg-fix")
async def jpeg_fix(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Remove JPEG artifacts using SwinIR color_jpeg_car model."""
    _require_auth(authorization)
    t0 = time.monotonic()
    try:
        img = Image.open(io.BytesIO(await file.read())).convert("RGB")
        result = _run_swinir_jpeg(_get_model("swinir_jpeg"), img)
        body = _to_jpeg_bytes(result)
        ms = int((time.monotonic() - t0) * 1000)
        logger.info("jpeg-fix ok %dx%d → %d bytes in %dms", img.width, img.height, len(body), ms)
        return Response(
            content=body,
            media_type="image/jpeg",
            headers={"X-Inference-Duration-Ms": str(ms)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("jpeg-fix error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    logger.info("Starting inference server on 0.0.0.0:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
