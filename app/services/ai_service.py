"""
AI Service - Strategy pattern with multiple AI backends.
"""
import asyncio
import io
import json
import mimetypes
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Callable, Awaitable, Sequence

from app.config import get_settings, get_effective_ai_provider


class ProcessingResult:
    def __init__(self, success: bool, output_path: Optional[str] = None, error: Optional[str] = None):
        self.success = success
        self.output_path = output_path
        self.error = error


ProgressCallback = Optional[Callable[[str, int], Awaitable[None]]]


class AIProvider(ABC):
    @abstractmethod
    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        ...


class MockProvider(AIProvider):
    """Returns original image after simulated delay. For testing UI flow."""

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        stages = [
            ("Analyzing image...", 20),
            ("Enhancing faces...", 50),
            ("Upscaling resolution...", 80),
        ]
        if colorize:
            stages.append(("Colorizing...", 90))
        stages.append(("Generating result...", 100))

        for stage, progress in stages:
            if progress_callback:
                await progress_callback(stage, progress)
            await asyncio.sleep(1.5)

        shutil.copy2(input_path, output_path)
        return ProcessingResult(success=True, output_path=output_path)


class HuggingFaceProvider(AIProvider):
    """Free API via HuggingFace Spaces Gradio endpoints with fallback.

    Strategy: try multiple face restoration approaches in order:
    1. CodeFormer (face restore + background enhance + upscale in one call)
    2. Multi-model spaces (GFPGAN/CodeFormer/RestoreFormer via avans06 forks)
    3. GFPGAN-only spaces (original + forks)
    If CodeFormer succeeds, skip separate ESRGAN step (it already upscales).
    """

    # Each entry: (space_id, space_type, api_endpoint)
    # Audited 2026-04-17: most GFPGAN-only spaces are dead (RUNTIME_ERROR) and the
    # CodeFormer / multimodel APIs added a required `face_align` parameter.
    RESTORE_SPACES: list[tuple[str, str, str]] = [
        # CodeFormer v2: (image, face_align, bg_enhance, face_upsample, upscale, fidelity)
        ("sczhou/CodeFormer", "codeformer_v2", "/inference"),
        ("PERCY001/CodeFormer", "codeformer_v2", "/predict"),
        # Multi-model spaces (gallery-based /inference API)
        ("avans06/Image_Face_Upscale_Restoration-GFPGAN-RestoreFormer-CodeFormer-GPEN", "multimodel_v2", "/inference"),
        ("titanito/Image_Face_Upscale_Restoration-GFPGAN-RestoreFormer-CodeFormer-GPEN", "multimodel_v2", "/inference"),
    ]

    # Colorization Spaces — jantic/DeOldify was deleted from HF; audited alternatives 2026-04-17.
    # gudada/DDColor (RUNNING cpu-basic verified 2026-05-08) is SOTA — DDColor 2023 model
    # outputs more natural colors than ialhashim's 2019 fast.ai model. Returns imageslider:
    # list[before_path, after_path] — caller takes result[-1].
    # Each entry: (space_id, call_style).
    DEOLDIFY_SPACES: list[tuple[str, str]] = [
        ("gudada/DDColor", "ddcolor_imageslider"),
        ("ialhashim/Colorizer", "single_arg"),
    ]

    # Cap input short-edge before sending to HF Spaces. Gradio Spaces frequently
    # downsample on their side to 512–1024px and then upscale, double-lossy. Pre-resizing
    # locally with Lanczos preserves more detail; CodeFormer's `face_upsample` and
    # ESRGAN-style models still scale ≥2× from this baseline.
    _INPUT_SHORT_EDGE_CAP = 1280

    # Hard ceiling on how long any single HF Space may block. HF free-tier
    # queues can take 5-10+ min during peak; blocking the task that long
    # leaves paying users staring at a stuck progress bar. Fall through to
    # the next Space instead.
    _SPACE_PREDICT_TIMEOUT_S = 90

    async def _try_space(
        self, space_id: str, space_type: str, input_path: str, api_endpoint: str = "/predict"
    ) -> tuple[str, bool]:
        """Try a single Space. Returns (output_path, includes_upscale)."""
        from gradio_client import Client, handle_file

        client = Client(space_id, verbose=False)
        img = handle_file(input_path)

        if space_type == "codeformer_v2":
            # Current CodeFormer signature (audited 2026-04-17):
            # predict(image, face_align, background_enhance, face_upsample, upscale, codeformer_fidelity)
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    client.predict,
                    img,
                    True,    # face_align
                    True,    # background_enhance
                    True,    # face_upsample
                    2,       # upscale
                    0.5,     # codeformer_fidelity (0=quality, 1=fidelity).
                             # 0.5 favors identity preservation over generative
                             # detail — fewer "uncanny" face swaps on portraits.
                    api_name=api_endpoint,
                ),
                timeout=self._SPACE_PREDICT_TIMEOUT_S,
            )
            # sczhou/CodeFormer returns (output, markdown), PERCY001 returns output only
            if isinstance(result, tuple):
                result = result[0]
            # Output may be a dict with path/url (new gradio) or raw path string
            if isinstance(result, dict):
                result = result.get("path") or result.get("url") or str(result)
            return str(result), True  # CodeFormer includes upscale

        elif space_type == "multimodel_v2":
            # avans06/titanito multi-model: gallery-based /inference
            # Signature: (gallery, face_restoration, upscale_model, scale, face_detection,
            #             threshold, center_only, output_with_name, [save_as_png])
            gallery = [{"image": img}]
            args = [
                gallery,
                "GFPGANv1.4.pth",                          # face_restoration
                "SRVGG, realesr-general-x4v3.pth",         # upscale_model
                2,                                          # scale
                "retinaface_resnet50",                     # face_detection
                10,                                         # threshold
                False,                                      # center_only
                False,                                      # output_with_model_name
            ]
            # avans06 variant also requires save_as_png; titanito does not
            if space_id.startswith("avans06/"):
                args.append(False)
            result = await asyncio.wait_for(
                asyncio.to_thread(client.predict, *args, api_name=api_endpoint),
                timeout=self._SPACE_PREDICT_TIMEOUT_S,
            )
            # Returns (gallery_output, download_file); grab first gallery entry
            if isinstance(result, tuple):
                result = result[0]
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, dict):
                    inner = first.get("image", first)
                    if isinstance(inner, dict):
                        result = inner.get("path") or inner.get("url") or ""
                    else:
                        result = inner
                else:
                    result = first
            elif isinstance(result, dict):
                result = result.get("path") or result.get("url") or str(result)
            if not result:
                raise RuntimeError("multimodel returned empty output")
            return str(result), True  # includes upscale

    async def _restore_face(self, input_path: str, progress_callback: ProgressCallback) -> tuple[str, bool]:
        """Try face restoration Spaces with fallback. Returns (path, did_upscale)."""
        import logging
        logger = logging.getLogger("artimagehub.hf")
        errors = []

        for space_id, space_type, api_endpoint in self.RESTORE_SPACES:
            try:
                logger.info("Trying %s (%s %s)...", space_id, space_type, api_endpoint)
                if progress_callback:
                    short_name = space_id.split("/")[-1][:20]
                    await progress_callback(f"Restoring faces ({short_name})...", 20)

                result_path, includes_upscale = await self._try_space(
                    space_id, space_type, input_path, api_endpoint
                )
                logger.info("Succeeded with: %s", space_id)
                return result_path, includes_upscale
            except asyncio.TimeoutError:
                logger.warning(
                    "%s timed out after %ss — queue likely full, trying next Space",
                    space_id, self._SPACE_PREDICT_TIMEOUT_S,
                )
                errors.append(f"{space_id.split('/')[-1]}: timeout {self._SPACE_PREDICT_TIMEOUT_S}s")
                continue
            except Exception as e:
                err_msg = str(e)
                logger.warning("%s failed: %s", space_id, err_msg[:200])
                errors.append(f"{space_id.split('/')[-1]}: {err_msg[:80]}")
                continue

        raise RuntimeError(f"All face restoration Spaces failed: {'; '.join(errors[-3:])}")

    # Real-ESRGAN alternatives (audited 2026-04-17). doevent/Face-Real-ESRGAN is
    # frequently down; Fabrice-TIERCELIN and guetLzy are more reliable.
    # Each entry: (space_id, call_style)
    ESRGAN_SPACES: list[tuple[str, str]] = [
        ("Fabrice-TIERCELIN/RealESRGAN", "size_modifier"),
        ("guetLzy/Real-ESRGAN-Demo", "enhance_full"),
        ("doevent/Face-Real-ESRGAN", "single_arg"),  # legacy, keep as final fallback
    ]

    async def _call_esrgan(self, input_path: str) -> str:
        """Try Real-ESRGAN for super resolution."""
        import logging
        from gradio_client import Client, handle_file

        logger = logging.getLogger("artimagehub.hf")

        for space_id, call_style in self.ESRGAN_SPACES:
            try:
                logger.info("Trying ESRGAN: %s (%s)", space_id, call_style)
                client = Client(space_id, verbose=False)
                img = handle_file(input_path)
                if call_style == "size_modifier":
                    result = await asyncio.wait_for(
                        asyncio.to_thread(client.predict, img, "2", api_name="/predict"),
                        timeout=self._SPACE_PREDICT_TIMEOUT_S,
                    )
                elif call_style == "enhance_full":
                    # (input_image, model_name, outscale, face_enhance)
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.predict,
                            img,
                            "RealESRGAN_x2plus",
                            2,
                            False,
                            api_name="/enhance",
                        ),
                        timeout=self._SPACE_PREDICT_TIMEOUT_S,
                    )
                else:  # legacy single_arg
                    result = await asyncio.wait_for(
                        asyncio.to_thread(client.predict, img, api_name="/predict"),
                        timeout=self._SPACE_PREDICT_TIMEOUT_S,
                    )
                if isinstance(result, tuple):
                    result = result[0]
                if isinstance(result, dict):
                    result = result.get("path") or result.get("url") or str(result)
                if result:
                    logger.info("ESRGAN succeeded with: %s", space_id)
                    return str(result)
            except Exception as e:
                logger.warning("ESRGAN %s failed: %s", space_id, str(e)[:200])
                continue

        raise RuntimeError("Real-ESRGAN unavailable")

    async def _call_deoldify(self, input_path: str) -> str:
        """Try DeOldify for colorization."""
        import logging
        from gradio_client import Client, handle_file

        logger = logging.getLogger("artimagehub.hf")

        for space_id, call_style in self.DEOLDIFY_SPACES:
            try:
                logger.info("Trying colorizer: %s (%s)", space_id, call_style)
                client = Client(space_id, verbose=False)
                if call_style == "ddcolor_imageslider":
                    # gudada/DDColor: api_name=/colorize, output is ImageSlider returning
                    # [before_path, after_path]. Take after.
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.predict,
                            handle_file(input_path),
                            api_name="/colorize",
                        ),
                        timeout=self._SPACE_PREDICT_TIMEOUT_S,
                    )
                    if isinstance(result, (list, tuple)) and len(result) >= 2:
                        result = result[-1]
                elif call_style == "single_arg":
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.predict,
                            handle_file(input_path),
                            api_name="/predict",
                        ),
                        timeout=self._SPACE_PREDICT_TIMEOUT_S,
                    )
                else:  # classic DeOldify signature
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.predict,
                            handle_file(input_path),
                            10,
                            api_name="/predict",
                        ),
                        timeout=self._SPACE_PREDICT_TIMEOUT_S,
                    )
                if isinstance(result, tuple):
                    result = result[0]
                if isinstance(result, dict):
                    result = result.get("path") or result.get("url") or str(result)
                logger.info("Colorizer succeeded: %s", space_id)
                return str(result)
            except Exception as e:
                logger.warning("Colorizer %s failed: %s", space_id, e)
                continue

        raise RuntimeError("No colorization Space available")

    async def _pre_resize_input(self, input_path: str) -> str:
        """Pre-cap input short-edge before HF Spaces; return possibly-rewritten path.

        Without this, large originals (e.g. 4000px scans) get downsampled inside
        the Space to 512–1024px and then upscaled, losing detail twice. We cap
        short-edge locally with Lanczos so CodeFormer / ESRGAN see the highest-
        quality possible baseline, and downstream face_upsample / x2 still expand
        from there.
        """
        import logging
        from PIL import Image
        logger = logging.getLogger("artimagehub.hf")

        try:
            with Image.open(input_path) as im:
                w, h = im.size
                short = min(w, h)
                if short <= self._INPUT_SHORT_EDGE_CAP:
                    return input_path
                ratio = self._INPUT_SHORT_EDGE_CAP / short
                new_w = int(round(w * ratio))
                new_h = int(round(h * ratio))
                resized = im.convert("RGB").resize((new_w, new_h), Image.LANCZOS)
                # Write next to the input so cleanup logic still applies.
                from pathlib import Path
                p = Path(input_path)
                out = p.with_name(p.stem + "_pre" + p.suffix.lower() if p.suffix else p.stem + "_pre.jpg")
                fmt = "JPEG" if out.suffix.lower() in {".jpg", ".jpeg"} else "PNG"
                save_kwargs = {"quality": 95, "optimize": True} if fmt == "JPEG" else {}
                resized.save(str(out), fmt, **save_kwargs)
                logger.info("Pre-resize %sx%s -> %sx%s (short-edge cap=%d)",
                            w, h, new_w, new_h, self._INPUT_SHORT_EDGE_CAP)
                return str(out)
        except Exception as exc:
            logger.warning("Pre-resize skipped (%s); using original input", exc)
            return input_path

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        try:
            import logging
            logger = logging.getLogger("artimagehub.hf")

            # Step 0: Pre-resize input to avoid double-lossy downsampling inside HF Spaces
            input_path = await self._pre_resize_input(input_path)

            # Step 1: Face restoration (tries CodeFormer → multi-model → GFPGAN)
            if progress_callback:
                await progress_callback("Starting face restoration...", 10)

            current_path, did_upscale = await self._restore_face(input_path, progress_callback)

            # Step 2: Super resolution (skip if restoration already upscaled)
            if not did_upscale:
                if progress_callback:
                    await progress_callback("Upscaling resolution (Real-ESRGAN)...", 55)
                try:
                    current_path = await self._call_esrgan(current_path)
                except Exception:
                    pass  # ESRGAN is nice-to-have, face restore is the core value

            # Step 3: Colorization (optional)
            if colorize:
                if progress_callback:
                    await progress_callback("Colorizing...", 80)
                try:
                    current_path = await self._call_deoldify(current_path)
                except Exception:
                    pass  # Colorization is optional

            if progress_callback:
                await progress_callback("Generating result...", 95)

            shutil.copy2(current_path, output_path)

            if progress_callback:
                await progress_callback("Complete", 100)

            return ProcessingResult(success=True, output_path=output_path)

        except Exception as e:
            # All HF Spaces failed — fall back to PIL-based basic enhancement so
            # the funnel never returns a hard error to a paying customer.
            logger.warning("All HF Spaces failed (%s); applying PIL enhance fallback", e)
            return await PILEnhanceProvider().process_photo(
                input_path, output_path, colorize, progress_callback, email=email
            )


class HFInferenceProvider(AIProvider):
    """Hugging Face image-to-image provider using the official InferenceClient."""

    DEFAULT_MODELS = (
        "black-forest-labs/FLUX.1-Kontext-dev",
    )

    RESTORE_PROMPT = (
        "Restore and enhance this old damaged photograph. "
        "Fix scratches, improve clarity, sharpen details, correct colors."
    )
    COLORIZE_PROMPT = (
        "Restore and colorize this old damaged photograph. "
        "Fix scratches, improve clarity, sharpen details, and produce natural realistic colors."
    )
    NEGATIVE_PROMPT = "blurry, low quality, distorted, artifacts, text, watermark, duplicate"

    def __init__(self, api_token: str, model_candidates: Optional[Sequence[str]] = None):
        self.api_token = api_token.strip()
        self.model_candidates = [
            candidate.strip()
            for candidate in (model_candidates or self.DEFAULT_MODELS)
            if candidate and candidate.strip()
        ]
        self.timeout = 180

    def _is_prompt_driven_model(self, model_id: str) -> bool:
        lowered = model_id.lower()
        return any(keyword in lowered for keyword in ("stable-diffusion", "flux", "kontext", "relighting"))

    async def _run_model(self, model_id: str, input_path: str, colorize: bool) -> bytes:
        from huggingface_hub import InferenceClient

        prompt = self.COLORIZE_PROMPT if colorize else self.RESTORE_PROMPT

        def _invoke() -> bytes:
            client = InferenceClient(provider="hf-inference", token=self.api_token, timeout=self.timeout)

            call_kwargs: dict = {"image": input_path, "model": model_id, "prompt": prompt}
            if self._is_prompt_driven_model(model_id):
                call_kwargs["negative_prompt"] = self.NEGATIVE_PROMPT

            image = client.image_to_image(**call_kwargs)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=95)
            return buffer.getvalue()

        return await asyncio.to_thread(_invoke)

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import logging

        logger = logging.getLogger("artimagehub.hf_inference")

        if not self.api_token:
            return ProcessingResult(
                success=False,
                error="HF_TOKEN is required when AI_PROVIDER=hf_inference",
            )

        if not self.model_candidates:
            return ProcessingResult(
                success=False,
                error="No Hugging Face inference models configured",
            )

        try:
            errors = []

            for index, model_id in enumerate(self.model_candidates, start=1):
                progress = min(15 + (index - 1) * 20, 75)
                try:
                    if progress_callback:
                        short_name = model_id.split("/")[-1][:24]
                        await progress_callback(
                            f"Enhancing photo ({short_name})...",
                            progress,
                        )

                    logger.info("Trying HF inference model via InferenceClient: %s", model_id)
                    result_bytes = await self._run_model(model_id, input_path, colorize)

                    if colorize:
                        logger.warning(
                            "HF inference colorization is best-effort only; "
                            "actual support depends on the selected model."
                        )

                    if progress_callback:
                        await progress_callback("Writing result...", 95)

                    Path(output_path).write_bytes(result_bytes)

                    if progress_callback:
                        await progress_callback("Complete", 100)

                    return ProcessingResult(success=True, output_path=output_path)
                except Exception as exc:
                    logger.warning("HF inference model %s failed: %s", model_id, exc)
                    errors.append(f"{model_id}: {str(exc)[:280]}")
        except Exception as exc:
            logger.error("HF inference processing failed: %s", exc)
            return ProcessingResult(success=False, error=str(exc))

        return ProcessingResult(
            success=False,
            error=f"All HF inference models failed: {'; '.join(errors[-3:])}",
        )


class ReplicateProvider(AIProvider):
    """Free tier API via Replicate using free models with fallback strategy.

    Uses free Replicate models for photo restoration:
    - GFPGAN: Best for old photo restoration and face enhancement
    - CodeFormer: Alternative face enhancement with quality/fidelity control
    - Real-ESRGAN: Upscaling for improved resolution

    Fallback strategy:
    1. Try GFPGAN first (tencentarc/gfpgan) - best for old photos
    2. If GFPGAN fails, try CodeFormer (sczhou/codeformer)
    3. If both fail, use Real-ESRGAN (nightmareai/real-esrgan) for upscaling only
    """

    REPLICATE_API = "https://api.replicate.com/v1/predictions"

    # FREE tier models - pinned versions for stability
    # https://replicate.com/tencentarc/gfpgan
    GFPGAN_VERSION = "0fbacf7afc6c144e5be9767cff80f25aff23e52b0708f17e20f9879b2f21516c"

    # https://replicate.com/sczhou/codeformer
    CODEFORMER_VERSION = "7de2ea26c616d5bf2245ad0d5e24f0ff9a6204578a5c876db53142edd9d2cd56"

    # https://replicate.com/nightmareai/real-esrgan
    REAL_ESRGAN_VERSION = "f121d640bd286e1fdc67f9799164c1d5be36ff74576ee11c803ae5b665dd46aa"

    def __init__(self, api_token: str):
        self.api_token = api_token

    async def _run_model(self, http: "httpx.AsyncClient", version: str, model_input: dict, model_name: str = "model") -> str:
        """Run a Replicate model and wait for result. Returns output URL."""
        import logging
        logger = logging.getLogger("artimagehub.replicate")

        headers = {"Authorization": f"Bearer {self.api_token}"}

        # Retry with backoff on 429 rate limit
        for attempt in range(4):
            resp = await http.post(
                self.REPLICATE_API, headers=headers,
                json={"version": version, "input": model_input},
            )
            if resp.status_code == 429:
                wait = (attempt + 1) * 5  # 5s, 10s, 15s, 20s
                logger.warning("%s rate limited (429), retrying in %ds...", model_name, wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 402:
                raise RuntimeError(f"{model_name} credits exhausted (402). Add billing at replicate.com/account/billing")
            resp.raise_for_status()
            break
        else:
            raise RuntimeError(f"{model_name} rate limit exceeded after retries")

        prediction = resp.json()

        poll_url = prediction["urls"]["get"]
        for _ in range(120):  # max 2 min poll
            await asyncio.sleep(1)
            poll_resp = await http.get(poll_url, headers={"Authorization": f"Bearer {self.api_token}"})
            poll_resp.raise_for_status()
            data = poll_resp.json()
            status = data["status"]
            if status == "succeeded":
                output = data["output"]
                return output if isinstance(output, str) else str(output)
            elif status in ("failed", "canceled"):
                error_msg = data.get('error', 'unknown')
                raise RuntimeError(f"{model_name} prediction {status}: {error_msg}")

        raise RuntimeError(f"{model_name} prediction timed out")

    async def _upload_file(self, http: "httpx.AsyncClient", file_path: str) -> str:
        """Upload file to Replicate and return the serving URL."""
        import mimetypes
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        filename = Path(file_path).name

        # Create upload
        resp = await http.post(
            "https://api.replicate.com/v1/files",
            headers={"Authorization": f"Bearer {self.api_token}"},
            files={"content": (filename, open(file_path, "rb"), content_type)},
        )
        resp.raise_for_status()
        return resp.json()["urls"]["get"]

    async def _try_gfpgan(self, http: "httpx.AsyncClient", image_url: str, progress_callback: ProgressCallback) -> str:
        """Try GFPGAN for face restoration. Best for old photos."""
        import logging
        logger = logging.getLogger("artimagehub.replicate")

        logger.info("Trying GFPGAN (tencentarc/gfpgan) for face restoration...")
        if progress_callback:
            await progress_callback("Enhancing faces (GFPGAN)...", 20)

        # GFPGAN parameters: img, version, scale
        result_url = await self._run_model(
            http,
            self.GFPGAN_VERSION,
            {"img": image_url, "version": "v1.4", "scale": 2},
            "GFPGAN"
        )
        logger.info("GFPGAN succeeded")
        return result_url

    async def _try_codeformer(self, http: "httpx.AsyncClient", image_url: str, progress_callback: ProgressCallback) -> str:
        """Try CodeFormer for face restoration. Alternative to GFPGAN."""
        import logging
        logger = logging.getLogger("artimagehub.replicate")

        logger.info("Trying CodeFormer (sczhou/codeformer) for face restoration...")
        if progress_callback:
            await progress_callback("Enhancing faces (CodeFormer)...", 25)

        # CodeFormer parameters: image, upscale, codeformer_fidelity
        # upscale: 1-4 (default 2)
        # codeformer_fidelity: 0-1 (0=better quality, 1=more identity preservation)
        result_url = await self._run_model(
            http,
            self.CODEFORMER_VERSION,
            {"image": image_url, "upscale": 2, "codeformer_fidelity": 0.7},
            "CodeFormer"
        )
        logger.info("CodeFormer succeeded")
        return result_url

    async def _try_real_esrgan(self, http: "httpx.AsyncClient", image_url: str, progress_callback: ProgressCallback) -> str:
        """Try Real-ESRGAN for upscaling. Fallback when face enhancement fails."""
        import logging
        logger = logging.getLogger("artimagehub.replicate")

        logger.info("Trying Real-ESRGAN (nightmareai/real-esrgan) for upscaling...")
        if progress_callback:
            await progress_callback("Upscaling (Real-ESRGAN)...", 30)

        # Real-ESRGAN parameters: image, scale, face_enhance
        result_url = await self._run_model(
            http,
            self.REAL_ESRGAN_VERSION,
            {"image": image_url, "scale": 2, "face_enhance": True},
            "Real-ESRGAN"
        )
        logger.info("Real-ESRGAN succeeded")
        return result_url

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import httpx
        import logging
        logger = logging.getLogger("artimagehub.replicate")

        try:
            if not self.api_token:
                return ProcessingResult(success=False, error="Replicate API token missing")

            async with httpx.AsyncClient(timeout=180) as http:
                if progress_callback:
                    await progress_callback("Uploading image...", 10)

                file_url = await self._upload_file(http, input_path)
                current_url = file_url
                restoration_success = False
                errors: list[str] = []

                # Fallback strategy for face restoration:
                # 1. Try GFPGAN first (best for old photos)
                # 2. If GFPGAN fails, try CodeFormer
                # 3. If both fail, use Real-ESRGAN for upscaling only

                # Try GFPGAN first (primary method)
                try:
                    current_url = await self._try_gfpgan(http, file_url, progress_callback)
                    restoration_success = True
                except Exception as e:
                    gfpgan_error = str(e)
                    logger.warning("GFPGAN failed: %s", gfpgan_error[:200])
                    errors.append(f"GFPGAN: {gfpgan_error[:160]}")

                    # Fallback to CodeFormer
                    try:
                        current_url = await self._try_codeformer(http, file_url, progress_callback)
                        restoration_success = True
                    except Exception as e2:
                        codeformer_error = str(e2)
                        logger.warning("CodeFormer failed: %s", codeformer_error[:200])
                        errors.append(f"CodeFormer: {codeformer_error[:160]}")

                        # Last resort: Real-ESRGAN for upscaling only
                        try:
                            current_url = await self._try_real_esrgan(http, file_url, progress_callback)
                            restoration_success = True
                            logger.info("Using Real-ESRGAN as fallback (upscaling only)")
                        except Exception as e3:
                            realesrgan_error = str(e3)
                            logger.error("All restoration methods failed. GFPGAN: %s, CodeFormer: %s, Real-ESRGAN: %s",
                                       gfpgan_error[:100], codeformer_error[:100], realesrgan_error[:100])
                            errors.append(f"Real-ESRGAN: {realesrgan_error[:160]}")
                            raise RuntimeError(f"All restoration methods failed: {'; '.join(errors)}")

                # Additional upscaling pass if we only did face restoration (not Real-ESRGAN)
                # Skip if colorization is requested to avoid too many steps
                if restoration_success and not colorize:
                    try:
                        if progress_callback:
                            await progress_callback("Additional upscaling (Real-ESRGAN)...", 60)

                        current_url = await self._run_model(
                            http,
                            self.REAL_ESRGAN_VERSION,
                            {"image": current_url, "scale": 2, "face_enhance": False},
                            "Real-ESRGAN"
                        )
                    except Exception as e:
                        logger.info("Additional upscaling skipped: %s", str(e)[:100])

                # Note: Colorization removed for free tier
                # DeOldify and DDColor are not in the free tier model list
                if colorize:
                    logger.warning("Colorization not available in free tier - skipping")
                    if progress_callback:
                        await progress_callback("Colorization not available in free tier", 80)

                if progress_callback:
                    await progress_callback("Downloading result...", 95)

                resp = await http.get(current_url)
                resp.raise_for_status()
                Path(output_path).write_bytes(resp.content)

                if progress_callback:
                    await progress_callback("Complete", 100)

                return ProcessingResult(success=True, output_path=output_path)

        except Exception as e:
            logger.error("Photo processing failed: %s", str(e))
            return ProcessingResult(success=False, error=str(e))


class NeroAIProvider(AIProvider):
    """Nero AI task API with restore and optional colorize chaining."""

    TASK_API = "https://api.nero.com/biz/api/task"
    POLL_INTERVAL_SECONDS = 2
    MAX_POLLS = 120
    FACE_RESTORATION_TASK = "FaceRestoration"
    COLORIZE_TASK = "ColorizePhoto"

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    def _headers(self) -> dict[str, str]:
        return {"x-neroai-api-key": self.api_key}

    def _extract_message(self, payload: dict) -> str:
        for key in ("msg", "message", "error"):
            value = payload.get(key)
            if value:
                return str(value)

        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("msg", "message", "error"):
                value = data.get(key)
                if value:
                    return str(value)

        return "unknown Nero API error"

    def _decode_api_payload(self, response: "httpx.Response") -> dict:
        try:
            payload = response.json()
        except Exception as exc:
            snippet = response.text.strip()[:200]
            raise RuntimeError(f"Nero returned a non-JSON response: {snippet or 'empty body'}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Nero returned an unexpected response body")

        code = payload.get("code")
        normalized_code = None if code is None else str(code)
        if normalized_code not in {"0", None}:
            message = self._extract_message(payload)
            if normalized_code == "11002":
                raise RuntimeError(f"Nero API key is invalid (11002): {message}")
            if normalized_code == "11003":
                raise RuntimeError(f"Nero API key is expired (11003): {message}")
            if normalized_code == "11004":
                raise RuntimeError(f"Nero credits are exhausted (11004): {message}")
            raise RuntimeError(f"Nero API error {normalized_code}: {message}")

        return payload

    async def _request_api_json(
        self,
        http: "httpx.AsyncClient",
        method: str,
        url: str,
        **kwargs,
    ) -> dict:
        for attempt in range(4):
            response = await http.request(method, url, headers=self._headers(), **kwargs)

            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                await asyncio.sleep(min(2 * (attempt + 1), 10))
                continue

            response.raise_for_status()
            return self._decode_api_payload(response)

        raise RuntimeError("Nero request retries exhausted")

    async def _create_task_from_file(
        self,
        http: "httpx.AsyncClient",
        input_path: str,
        task_type: str,
        body: Optional[dict] = None,
    ) -> str:
        content_type = mimetypes.guess_type(input_path)[0] or "application/octet-stream"
        file_path = Path(input_path)
        form_payload = json.dumps({"type": task_type, "body": body or {}})
        file_bytes = file_path.read_bytes()

        payload = await self._request_api_json(
            http,
            "POST",
            self.TASK_API,
            data={"payload": form_payload},
            files={"file": (file_path.name, file_bytes, content_type)},
        )

        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("task_id"):
            raise RuntimeError("Nero create-task response did not include task_id")

        return str(data["task_id"])

    async def _create_task_from_url(
        self,
        http: "httpx.AsyncClient",
        task_type: str,
        image_url: str,
        body: Optional[dict] = None,
    ) -> str:
        payload = await self._request_api_json(
            http,
            "POST",
            self.TASK_API,
            json={
                "type": task_type,
                "body": {
                    "image": image_url,
                    **(body or {}),
                },
            },
        )

        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("task_id"):
            raise RuntimeError("Nero create-task response did not include task_id")

        return str(data["task_id"])

    async def _poll_for_output(
        self,
        http: "httpx.AsyncClient",
        task_id: str,
        progress_callback: ProgressCallback,
        *,
        pending_stage: str,
        running_stage: str,
        progress_base: int,
        progress_span: int,
    ) -> str:
        for _ in range(self.MAX_POLLS):
            await asyncio.sleep(self.POLL_INTERVAL_SECONDS)

            payload = await self._request_api_json(
                http,
                "GET",
                self.TASK_API,
                params={"task_id": task_id},
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("Nero poll response did not include task data")

            status = str(data.get("status", "")).lower()
            progress_value = data.get("progress")
            if status == "done":
                result = data.get("result")
                output_url = result.get("output") if isinstance(result, dict) else None
                if not output_url:
                    raise RuntimeError("Nero finished without an output URL")
                return str(output_url)

            if status == "failed":
                raise RuntimeError(self._extract_message(payload))

            if status not in {"pending", "running"}:
                raise RuntimeError(f"Unexpected Nero task status: {status or 'missing'}")

            if progress_callback:
                if isinstance(progress_value, (int, float)):
                    progress = progress_base + int(
                        max(0, min(float(progress_value), 100)) * (progress_span / 100)
                    )
                else:
                    progress = progress_base + (5 if status == "pending" else max(progress_span // 2, 1))
                stage = pending_stage if status == "pending" else running_stage
                await progress_callback(stage, min(progress, 90))

        raise RuntimeError(f"Nero task {task_id} timed out after {self.MAX_POLLS * self.POLL_INTERVAL_SECONDS}s")

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import httpx
        import logging

        logger = logging.getLogger("artimagehub.nero")

        if not self.api_key:
            return ProcessingResult(
                success=False,
                error="NERO_API_KEY is required when AI_PROVIDER=nero",
            )

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=30.0, read=60.0, write=60.0, pool=60.0),
                follow_redirects=True,
            ) as http:
                if progress_callback:
                    await progress_callback("Uploading image...", 10)

                task_id = await self._create_task_from_file(
                    http,
                    input_path,
                    self.FACE_RESTORATION_TASK,
                )

                if progress_callback:
                    await progress_callback("Submitted to Nero AI...", 20)

                output_url = await self._poll_for_output(
                    http,
                    task_id,
                    progress_callback,
                    pending_stage="Queued at Nero AI...",
                    running_stage="Restoring faces...",
                    progress_base=25,
                    progress_span=45,
                )

                if colorize:
                    if progress_callback:
                        await progress_callback("Starting colorization...", 72)

                    colorize_task_id = await self._create_task_from_url(
                        http,
                        self.COLORIZE_TASK,
                        output_url,
                    )
                    output_url = await self._poll_for_output(
                        http,
                        colorize_task_id,
                        progress_callback,
                        pending_stage="Queued for colorization...",
                        running_stage="Colorizing photo...",
                        progress_base=72,
                        progress_span=18,
                    )

                if progress_callback:
                    await progress_callback("Downloading result...", 95)

                response = await http.get(output_url)
                response.raise_for_status()
                Path(output_path).write_bytes(response.content)

                if progress_callback:
                    await progress_callback("Complete", 100)

                return ProcessingResult(success=True, output_path=output_path)

        except Exception as exc:
            logger.error("Nero photo processing failed: %s", exc)
            return ProcessingResult(success=False, error=str(exc))


class LocalGFPGANProvider(AIProvider):
    """Local CodeFormer/GFPGAN + Real-ESRGAN + DDColor provider — no API key required.

    Runs inference in a subprocess using a dedicated Python env that has
    gfpgan/realesrgan/ddcolor installed (separate from the backend's venv to avoid
    heavy ML dependency conflicts).
    """

    def __init__(self, python_path: str, models_dir: str, inference_script: str,
                 scale: int = 2, face_model: str = "codeformer", fidelity: float = 0.7):
        self.python_path = python_path
        self.models_dir = models_dir
        self.inference_script = inference_script
        self.scale = scale
        self.face_model = face_model
        self.fidelity = fidelity

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import asyncio
        import logging

        logger = logging.getLogger("artimagehub.local_restore")

        face_label = "CodeFormer" if self.face_model == "codeformer" else "GFPGAN"

        try:
            if progress_callback:
                await progress_callback("Loading models...", 10)

            cmd = [
                self.python_path,
                self.inference_script,
                "--input", input_path,
                "--output", output_path,
                "--models-dir", self.models_dir,
                "--face-model", self.face_model,
                "--fidelity", str(self.fidelity),
                "--scale", str(self.scale),
            ]
            if colorize:
                cmd.append("--colorize")

            # Set PYTHONPATH so the script can find CodeFormer's custom modules
            env = os.environ.copy()
            codeformer_dir = os.path.join(os.path.dirname(self.models_dir), "CodeFormer")
            env["PYTHONPATH"] = codeformer_dir + ":" + env.get("PYTHONPATH", "")

            logger.info("Running local restore: %s", " ".join(cmd))

            if progress_callback:
                await progress_callback(f"Restoring faces ({face_label})...", 25)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            if progress_callback:
                await progress_callback(f"Upscaling (Real-ESRGAN)...", 50)

            stdout, stderr = await proc.communicate()
            stdout_text = stdout.decode().strip()
            stderr_text = stderr.decode().strip()

            if stderr_text:
                for line in stderr_text.splitlines():
                    if line.startswith("[info]") or line.startswith("[warn]"):
                        logger.info("restore: %s", line)

            last_line = stdout_text.splitlines()[-1].strip() if stdout_text else ""
            if proc.returncode != 0 or last_line != "SUCCESS":
                error_lines = [l for l in stdout_text.splitlines() if l.startswith("ERROR")]
                error_detail = error_lines[-1] if error_lines else (stderr_text.splitlines()[-1] if stderr_text else "unknown")
                raise RuntimeError(f"Local restore failed (exit {proc.returncode}): {error_detail}")

            if progress_callback:
                await progress_callback("Complete", 100)

            return ProcessingResult(success=True, output_path=output_path)

        except Exception as e:
            logger.error("Local restore processing failed: %s", e)
            return ProcessingResult(success=False, error=str(e))


class PILEnhanceProvider(AIProvider):
    """Last-resort fallback using only PIL for basic image enhancement.

    No external API needed. Applies sharpening, contrast boost, and upscaling.
    Not AI-based, but ensures the funnel never returns a hard error.
    """

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import logging
        logger = logging.getLogger("artimagehub.pil_enhance")

        try:
            from PIL import Image, ImageEnhance, ImageFilter

            if progress_callback:
                await progress_callback("Enhancing image...", 30)

            def _enhance() -> None:
                img = Image.open(input_path).convert("RGB")

                # Upscale 2x with Lanczos
                w, h = img.size
                img = img.resize((w * 2, h * 2), Image.LANCZOS)

                # Sharpen
                img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=3))

                # Contrast boost
                img = ImageEnhance.Contrast(img).enhance(1.3)

                # Brightness slight lift
                img = ImageEnhance.Brightness(img).enhance(1.05)

                img.save(output_path, "JPEG", quality=95)

            await asyncio.to_thread(_enhance)

            if progress_callback:
                await progress_callback("Complete", 100)

            logger.info("PIL enhance fallback succeeded")
            return ProcessingResult(success=True, output_path=output_path)

        except Exception as exc:
            logger.error("PIL enhance failed: %s", exc)
            return ProcessingResult(success=False, error=str(exc))


class PhotoFixProvider(AIProvider):
    """Delegates photo restoration to the PhotoFix backend via base64 JSON API.

    POST api_url with {"image": "<base64>", "task": "restore|colorize|enhance"}
    Headers: X-Internal-Key + Content-Type: application/json
    Response: {"ok": true, "result": "<base64>"} or {"ok": false, "error": "..."}
    """

    REQUEST_TIMEOUT = 6000.0  # backend can take up to ~100 min for large inputs

    def __init__(self, api_url: str, internal_api_key: str = ""):
        self.api_url = api_url
        self.internal_api_key = internal_api_key

    async def process_photo(
        self,
        input_path: str,
        output_path: str,
        colorize: bool,
        progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import base64
        import logging
        import httpx

        logger = logging.getLogger("artimagehub.photofix")

        if not self.api_url:
            return ProcessingResult(success=False, error="PHOTOFIX_API_URL is not configured.")

        task = "colorize" if colorize else "restore"

        try:
            if progress_callback:
                await progress_callback("Preparing image...", 10)

            image_bytes = Path(input_path).read_bytes()
            image_b64 = base64.b64encode(image_bytes).decode("ascii")

            if progress_callback:
                await progress_callback("Processing with PhotoFix...", 20)

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.REQUEST_TIMEOUT, connect=30.0),
                follow_redirects=True,
            ) as http:
                resp = await http.post(
                    self.api_url,
                    headers={
                        "Content-Type": "application/json",
                        "X-Internal-Key": self.internal_api_key,
                    },
                    json={"image": image_b64, "task": task},
                )
                resp.raise_for_status()
                data = resp.json()

            if not data.get("ok"):
                raise RuntimeError(data.get("error") or "PhotoFix returned ok=false")

            result_bytes = base64.b64decode(data["result"])
            Path(output_path).write_bytes(result_bytes)

            if progress_callback:
                await progress_callback("Complete", 100)

            logger.info("PhotoFix: done, output saved to %s", output_path)
            return ProcessingResult(success=True, output_path=output_path)

        except Exception as exc:
            logger.error("PhotoFix processing failed: %s", exc)
            return ProcessingResult(success=False, error=str(exc))


async def _try_local_mac(
    input_path: str,
    output_path: str,
    endpoint: str,
    progress_callback: "ProgressCallback",
    progress_msg: str,
    progress_pct: int,
) -> bool:
    """POST image to Mac inference server. Returns True on success, False on any failure.

    Uses lama_inference_url / lama_inference_token (same Cloudflare Tunnel as LaMa).
    12s connect + 90s read — matches cross-region RTT budget from memory.
    """
    import logging
    import httpx

    logger = logging.getLogger("artimagehub.local_mac")
    settings = get_settings()
    url = (settings.lama_inference_url or "").rstrip("/")
    token = settings.lama_inference_token or ""

    if not url or not token:
        return False

    if progress_callback:
        await progress_callback(progress_msg, progress_pct)

    timeout = httpx.Timeout(connect=12.0, read=90.0, write=15.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            with open(input_path, "rb") as fh:
                resp = await client.post(
                    f"{url}/{endpoint}",
                    files={"file": (Path(input_path).name, fh, "image/jpeg")},
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code == 200:
                Path(output_path).write_bytes(resp.content)
                ms = resp.headers.get("X-Inference-Duration-Ms", "?")
                logger.info("Local Mac %s ok (%sms, %d bytes)", endpoint, ms, len(resp.content))
                return True
            logger.warning("Local Mac %s returned %s: %s", endpoint, resp.status_code, resp.text[:200])
            return False
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
        logger.warning("Local Mac %s network error: %s — falling back to HF", endpoint, exc)
        return False
    except Exception as exc:
        logger.warning("Local Mac %s unexpected error: %s — falling back to HF", endpoint, exc)
        return False


class NAFNetDenoiseProvider:
    """Photo denoising via NAFNet (HF Space: chuxiaojie/NAFNet).

    Primary: local Mac inference server (192.168.68.221 via Cloudflare Tunnel).
    Fallback: HuggingFace Space chuxiaojie/NAFNet.
    """

    SPACE_ID = "chuxiaojie/NAFNet"
    TIMEOUT_S = 120

    async def denoise_photo(
        self,
        input_path: str,
        output_path: str,
        progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import logging
        logger = logging.getLogger("artimagehub.nafnet")

        if progress_callback:
            await progress_callback("Analyzing noise...", 10)

        # Primary: local Mac inference server
        ok = await _try_local_mac(
            input_path, output_path, "denoise",
            progress_callback, "Denoising with local NAFNet...", 30,
        )
        if ok:
            if progress_callback:
                await progress_callback("Complete", 100)
            return ProcessingResult(success=True, output_path=output_path)

        try:
            from gradio_client import Client, handle_file

            client = Client(self.SPACE_ID, verbose=False)
            img = handle_file(input_path)

            if progress_callback:
                await progress_callback("Denoising with NAFNet...", 30)

            result = await asyncio.wait_for(
                asyncio.to_thread(
                    client.predict,
                    img,
                    "SIDD",   # task: real-world noise removal
                    api_name="/predict",
                ),
                timeout=self.TIMEOUT_S,
            )

            if isinstance(result, tuple):
                result = result[0]
            if isinstance(result, dict):
                result = result.get("path") or result.get("url") or str(result)

            if not result:
                raise RuntimeError("NAFNet returned empty result")

            if progress_callback:
                await progress_callback("Writing result...", 90)

            shutil.copy2(str(result), output_path)

            if progress_callback:
                await progress_callback("Complete", 100)

            logger.info("NAFNet denoising succeeded")
            return ProcessingResult(success=True, output_path=output_path)

        except Exception as exc:
            logger.warning("NAFNet denoising failed (%s); falling back to HuggingFace restore approach", exc)
            return await HuggingFaceProvider().process_photo(
                input_path, output_path, False, progress_callback, email=email
            )

    async def _pil_denoise_fallback(
        self, input_path: str, output_path: str, progress_callback: ProgressCallback
    ) -> ProcessingResult:
        """Bilateral-filter-style PIL fallback: smoothing + sharpening."""
        import logging
        logger = logging.getLogger("artimagehub.nafnet")
        try:
            from PIL import Image, ImageFilter, ImageEnhance

            def _process():
                img = Image.open(input_path).convert("RGB")
                # Smooth then sharpen — approximates noise reduction
                img = img.filter(ImageFilter.SMOOTH_MORE)
                img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=2))
                img = ImageEnhance.Contrast(img).enhance(1.1)
                img.save(output_path, "JPEG", quality=90)

            await asyncio.to_thread(_process)

            if progress_callback:
                await progress_callback("Complete (fallback)", 100)

            logger.info("NAFNet PIL fallback succeeded")
            return ProcessingResult(success=True, output_path=output_path)
        except Exception as exc:
            logger.error("NAFNet PIL fallback also failed: %s", exc)
            return ProcessingResult(success=False, error=str(exc))


class NAFNetDeblurProvider:
    """Photo deblurring via NAFNet GoPro model (HF Space: chuxiaojie/NAFNet).

    Primary: local Mac inference server (192.168.68.221 via Cloudflare Tunnel).
    Fallback: HuggingFace Space chuxiaojie/NAFNet (GoPro task).
    """

    SPACE_ID = "chuxiaojie/NAFNet"
    TIMEOUT_S = 120

    async def deblur_photo(
        self,
        input_path: str,
        output_path: str,
        progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import logging
        logger = logging.getLogger("artimagehub.nafnet_deblur")

        if progress_callback:
            await progress_callback("Analyzing blur...", 10)

        # Primary: local Mac inference server
        ok = await _try_local_mac(
            input_path, output_path, "deblur",
            progress_callback, "Deblurring with local NAFNet...", 30,
        )
        if ok:
            if progress_callback:
                await progress_callback("Complete", 100)
            return ProcessingResult(success=True, output_path=output_path)

        try:
            from gradio_client import Client, handle_file

            client = Client(self.SPACE_ID, verbose=False)
            img = handle_file(input_path)

            if progress_callback:
                await progress_callback("Deblurring with NAFNet...", 30)

            result = await asyncio.wait_for(
                asyncio.to_thread(
                    client.predict,
                    img,
                    "GoPro",  # motion deblur benchmark dataset model
                    api_name="/predict",
                ),
                timeout=self.TIMEOUT_S,
            )

            if isinstance(result, tuple):
                result = result[0]
            if isinstance(result, dict):
                result = result.get("path") or result.get("url") or str(result)

            if not result:
                raise RuntimeError("NAFNet deblur returned empty result")

            if progress_callback:
                await progress_callback("Writing result...", 90)

            shutil.copy2(str(result), output_path)

            if progress_callback:
                await progress_callback("Complete", 100)

            logger.info("NAFNet deblurring succeeded")
            return ProcessingResult(success=True, output_path=output_path)

        except Exception as exc:
            logger.warning("NAFNet deblurring failed (%s); falling back to HuggingFace restore approach", exc)
            return await HuggingFaceProvider().process_photo(
                input_path, output_path, False, progress_callback, email=email
            )

    async def _pil_deblur_fallback(
        self, input_path: str, output_path: str, progress_callback: ProgressCallback
    ) -> ProcessingResult:
        import logging
        logger = logging.getLogger("artimagehub.nafnet_deblur")
        try:
            from PIL import Image, ImageFilter, ImageEnhance

            def _process():
                img = Image.open(input_path).convert("RGB")
                img = img.filter(ImageFilter.UnsharpMask(radius=2.0, percent=180, threshold=2))
                img = ImageEnhance.Sharpness(img).enhance(1.8)
                img = ImageEnhance.Contrast(img).enhance(1.1)
                img.save(output_path, "JPEG", quality=92)

            await asyncio.to_thread(_process)

            if progress_callback:
                await progress_callback("Complete (fallback)", 100)

            logger.info("NAFNet deblur PIL fallback succeeded")
            return ProcessingResult(success=True, output_path=output_path)
        except Exception as exc:
            logger.error("NAFNet deblur PIL fallback also failed: %s", exc)
            return ProcessingResult(success=False, error=str(exc))


class DeepSeekProvider(AIProvider):
    """Intelligent photo restoration router powered by DeepSeek V4 Pro + HF.

    DeepSeek V4 Pro is a text model (no vision), so it cannot process images
    directly. Instead it analyzes image metadata (format, dimensions, file size)
    and recommends the best restoration strategy. Actual restoration delegates
    to HuggingFace Spaces; if those also fail, PILEnhanceProvider is the last
    resort so the user never sees a hard error.
    """

    def __init__(self, api_key: str, api_base: str, model: str):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model

    async def _recommend_strategy(self, input_path: str, colorize: bool) -> str:
        """Ask DeepSeek V4 Pro to recommend a restoration strategy from file metadata."""
        import os
        import httpx
        from PIL import Image as PILImg

        stat = os.stat(input_path)
        img = PILImg.open(input_path)
        w, h = img.size
        fmt = img.format or "unknown"
        mode = img.mode

        metadata = (
            f"Image: {os.path.basename(input_path)}\n"
            f"Format: {fmt}, Mode: {mode}\n"
            f"Dimensions: {w}x{h} ({(w * h / 1_000_000):.1f} MP)\n"
            f"File size: {stat.st_size / 1024:.0f} KB\n"
            f"Colorize requested: {colorize}\n"
        )

        prompt = (
            "You are an image restoration expert. Based on the image metadata below, "
            "recommend the best restoration strategy. Consider: 1) If dimensions are "
            "small (<1MP), upscaling should be prioritized. 2) If the format is JPEG "
            "with small file size, compression artifact removal may help. 3) If "
            "colorize is requested, note that old B&W photos benefit most. "
            "4) If mode is not RGB, conversion is needed. "
            "Give a concise 2-3 sentence recommendation.\n\n"
            f"{metadata}"
        )

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.post(
                f"{self.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool,
        progress_callback: ProgressCallback, email: str = "",
    ) -> ProcessingResult:
        import logging
        logger = logging.getLogger("artimagehub.deepseek")

        try:
            if progress_callback:
                await progress_callback("Analyzing with DeepSeek V4...", 10)

            recommendation = await self._recommend_strategy(input_path, colorize)
            logger.info("DeepSeek recommendation: %s", recommendation[:300])

            if progress_callback:
                await progress_callback("DeepSeek analysis complete, restoring...", 20)

            return await HuggingFaceProvider().process_photo(
                input_path, output_path, colorize, progress_callback, email=email
            )
        except Exception as exc:
            logger.warning("DeepSeek failed (%s), falling back to HuggingFace", exc)
            return await HuggingFaceProvider().process_photo(
                input_path, output_path, colorize, progress_callback, email=email
            )


class SwinIRJpegProvider:
    """JPEG artifact removal via SwinIR (HF Space: JingyunLiang/SwinIR).

    Primary: local Mac inference server (192.168.68.221 via Cloudflare Tunnel).
    Fallback: HuggingFace Space JingyunLiang/SwinIR (color_jpeg_car model).
    """

    SPACE_ID = "JingyunLiang/SwinIR"
    TIMEOUT_S = 120

    async def fix_jpeg(
        self,
        input_path: str,
        output_path: str,
        progress_callback: ProgressCallback,
        email: str = "",
    ) -> ProcessingResult:
        import logging
        logger = logging.getLogger("artimagehub.swinir_jpeg")

        if progress_callback:
            await progress_callback("Analyzing compression artifacts...", 10)

        # Primary: local Mac inference server
        ok = await _try_local_mac(
            input_path, output_path, "jpeg-fix",
            progress_callback, "Removing JPEG artifacts with local SwinIR...", 30,
        )
        if ok:
            if progress_callback:
                await progress_callback("Complete", 100)
            return ProcessingResult(success=True, output_path=output_path)

        try:
            from gradio_client import Client, handle_file

            client = Client(self.SPACE_ID, verbose=False)
            img = handle_file(input_path)

            if progress_callback:
                await progress_callback("Removing JPEG artifacts with SwinIR...", 30)

            result = await asyncio.wait_for(
                asyncio.to_thread(
                    client.predict,
                    img,
                    "color_jpeg_car",  # JPEG artifact reduction for color images
                    40,                # JPEG quality factor (worst case; handles 40-100)
                    api_name="/predict",
                ),
                timeout=self.TIMEOUT_S,
            )

            if isinstance(result, tuple):
                result = result[0]
            if isinstance(result, dict):
                result = result.get("path") or result.get("url") or str(result)

            if not result:
                raise RuntimeError("SwinIR returned empty result")

            if progress_callback:
                await progress_callback("Writing result...", 90)

            shutil.copy2(str(result), output_path)

            if progress_callback:
                await progress_callback("Complete", 100)

            logger.info("SwinIR JPEG artifact removal succeeded")
            return ProcessingResult(success=True, output_path=output_path)

        except Exception as exc:
            logger.warning("SwinIR JPEG fix failed (%s); falling back to HuggingFace restore approach", exc)
            return await HuggingFaceProvider().process_photo(
                input_path, output_path, False, progress_callback, email=email
            )

    async def _pil_jpeg_fallback(
        self, input_path: str, output_path: str, progress_callback: ProgressCallback
    ) -> ProcessingResult:
        import logging
        logger = logging.getLogger("artimagehub.swinir_jpeg")
        try:
            from PIL import Image, ImageFilter, ImageEnhance

            def _process():
                img = Image.open(input_path).convert("RGB")
                # Mild smoothing to reduce JPEG blocking
                img = img.filter(ImageFilter.SMOOTH)
                # Unsharp mask to recover edge sharpness lost to compression
                img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=3))
                img.save(output_path, "JPEG", quality=92)

            await asyncio.to_thread(_process)

            if progress_callback:
                await progress_callback("Complete (fallback)", 100)

            logger.info("SwinIR PIL fallback succeeded")
            return ProcessingResult(success=True, output_path=output_path)
        except Exception as exc:
            logger.error("SwinIR PIL fallback also failed: %s", exc)
            return ProcessingResult(success=False, error=str(exc))


class AIService:
    """Delegates to the configured AI provider."""

    def __init__(self):
        settings = get_settings()
        provider = get_effective_ai_provider(settings)
        self._fallback_provider: AIProvider | None = None
        self._denoise_provider = NAFNetDenoiseProvider()
        self._deblur_provider = NAFNetDeblurProvider()
        self._jpeg_provider = SwinIRJpegProvider()

        if provider == "local":
            import logging
            import os

            python_path = settings.local_python
            models_dir = settings.local_models_dir
            inference_script = settings.local_inference_script

            # Auto-detect script location relative to this file: ../../scripts/codeformer_pipeline.py
            if not inference_script:
                here = Path(__file__).resolve()
                inference_script = str(here.parent.parent.parent.parent / "scripts" / "codeformer_pipeline.py")

            _local_ok = (
                python_path
                and os.path.exists(python_path)
                and models_dir
                and os.path.exists(models_dir)
                and os.path.exists(inference_script)
            )

            if not _local_ok:
                # Local GFPGAN not configured — fall back to free HuggingFace Spaces
                logging.getLogger("artimagehub.ai").warning(
                    "AI_PROVIDER=local but local environment is not ready "
                    "(LOCAL_PYTHON=%r, LOCAL_MODELS_DIR=%r) — falling back to huggingface",
                    python_path, models_dir,
                )
                self._provider: AIProvider = HuggingFaceProvider()
                return

            self._provider: AIProvider = LocalGFPGANProvider(
                python_path=python_path,
                models_dir=models_dir,
                inference_script=inference_script,
                scale=settings.local_scale,
                face_model=settings.local_face_model,
                fidelity=settings.local_fidelity,
            )
        elif provider == "replicate":
            self._provider: AIProvider = ReplicateProvider(settings.replicate_api_token)
        elif provider == "photofix":
            self._provider = PhotoFixProvider(settings.photofix_api_url, settings.internal_api_key)
            # Auto-fallback: if photofix backend is down or returning errors, use HF Spaces
            self._fallback_provider: AIProvider | None = HuggingFaceProvider()
        elif provider == "nero":
            self._provider = NeroAIProvider(settings.nero_api_key)
        elif provider == "hf_inference":
            model_candidates = [
                candidate.strip()
                for candidate in settings.hf_inference_models.split(",")
                if candidate.strip()
            ]
            self._provider = HFInferenceProvider(settings.hf_token, model_candidates)
        elif provider == "deepseek":
            self._provider = DeepSeekProvider(
                api_key=settings.deepseek_api_key,
                api_base=settings.deepseek_api_base,
                model=settings.deepseek_model,
            )
            self._fallback_provider = HuggingFaceProvider()
        elif provider == "huggingface":
            self._provider = HuggingFaceProvider()
        else:
            self._provider = MockProvider()

    async def process_photo(
        self,
        input_path: str,
        output_path: str,
        colorize: bool = False,
        progress_callback: ProgressCallback = None,
        email: str = "",
    ) -> ProcessingResult:
        import logging
        result = await self._provider.process_photo(
            input_path, output_path, colorize, progress_callback, email=email,
        )
        if not result.success and self._fallback_provider is not None:
            logging.getLogger("artimagehub.ai").warning(
                "Primary provider failed (%s), retrying with HuggingFace fallback", result.error
            )
            result = await self._fallback_provider.process_photo(
                input_path, output_path, colorize, progress_callback, email=email,
            )
        return result

    async def denoise_photo(
        self,
        input_path: str,
        output_path: str,
        progress_callback: ProgressCallback = None,
        email: str = "",
    ) -> ProcessingResult:
        return await self._denoise_provider.denoise_photo(
            input_path, output_path, progress_callback, email=email
        )

    async def deblur_photo(
        self,
        input_path: str,
        output_path: str,
        progress_callback: ProgressCallback = None,
        email: str = "",
    ) -> ProcessingResult:
        return await self._deblur_provider.deblur_photo(
            input_path, output_path, progress_callback, email=email
        )

    async def fix_jpeg_artifacts(
        self,
        input_path: str,
        output_path: str,
        progress_callback: ProgressCallback = None,
        email: str = "",
    ) -> ProcessingResult:
        return await self._jpeg_provider.fix_jpeg(
            input_path, output_path, progress_callback, email=email
        )


_service: AIService | None = None


def get_ai_service() -> AIService:
    global _service
    if _service is None:
        _service = AIService()
    return _service
