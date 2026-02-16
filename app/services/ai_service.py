"""
AI Service - Strategy pattern with HuggingFace Spaces (dev) and Replicate (prod).
"""
import asyncio
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Callable, Awaitable

from app.config import get_settings


class ProcessingResult:
    def __init__(self, success: bool, output_path: Optional[str] = None, error: Optional[str] = None):
        self.success = success
        self.output_path = output_path
        self.error = error


ProgressCallback = Optional[Callable[[str, int], Awaitable[None]]]


class AIProvider(ABC):
    @abstractmethod
    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback
    ) -> ProcessingResult:
        ...


class MockProvider(AIProvider):
    """Returns original image after simulated delay. For testing UI flow."""

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback
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

    # Each entry: (space_id, call_fn, includes_upscale)
    # call_fn takes (handle_file(path),) and returns predict args
    RESTORE_SPACES: list[tuple[str, str]] = [
        # CodeFormer: image, bg_enhance, face_upsample, upscale, fidelity_weight
        ("sczhou/CodeFormer", "codeformer"),
        # Multi-model spaces (avans06 forks) — use GFPGAN model within them
        ("avans06/Image_Face_Upscale_Restoration-GFPGAN-RestoreFormer-CodeFormer-GPEN", "multimodel"),
        ("titanito/Image_Face_Upscale_Restoration-GFPGAN-RestoreFormer-CodeFormer-GPEN", "multimodel"),
        # GFPGAN-only spaces
        ("Xintao/GFPGAN", "gfpgan"),
        ("nightfury/Image_Face_Upscale_Restoration-GFPGAN", "gfpgan"),
        ("leonelhs/GFPGAN", "gfpgan"),
        ("akhaliq/GFPGAN", "gfpgan"),
    ]

    DEOLDIFY_SPACES = [
        "jantic/DeOldify",
    ]

    async def _try_space(self, space_id: str, space_type: str, input_path: str) -> tuple[str, bool]:
        """Try a single Space. Returns (output_path, includes_upscale)."""
        from gradio_client import Client, handle_file

        client = Client(space_id, verbose=False)
        img = handle_file(input_path)

        if space_type == "codeformer":
            # CodeFormer: image, bg_enhance, face_upsample, upscale_factor, codeformer_fidelity
            result = await asyncio.to_thread(
                client.predict,
                img,
                True,    # background_enhance
                True,    # face_upsample
                2,       # rescaling_factor
                0.7,     # codeformer_fidelity (0=quality, 1=fidelity)
                api_name="/predict",
            )
            return str(result), True  # CodeFormer includes upscale

        elif space_type == "multimodel":
            # avans06 multi-model: image, model_name, rescale
            result = await asyncio.to_thread(
                client.predict,
                img,
                "CodeFormer",  # model selection
                2,             # rescale factor
                api_name="/predict",
            )
            return str(result), True  # includes upscale

        else:  # gfpgan
            result = await asyncio.to_thread(
                client.predict,
                img,
                "v1.4",  # version
                2,        # rescaling_factor
                api_name="/predict",
            )
            return str(result), False  # GFPGAN alone doesn't super-resolve well

    async def _restore_face(self, input_path: str, progress_callback: ProgressCallback) -> tuple[str, bool]:
        """Try face restoration Spaces with fallback. Returns (path, did_upscale)."""
        import logging
        logger = logging.getLogger("colorbyte.hf")
        errors = []

        for space_id, space_type in self.RESTORE_SPACES:
            try:
                logger.info("Trying %s (%s)...", space_id, space_type)
                if progress_callback:
                    short_name = space_id.split("/")[-1][:20]
                    await progress_callback(f"Restoring faces ({short_name})...", 20)

                result_path, includes_upscale = await self._try_space(space_id, space_type, input_path)
                logger.info("Succeeded with: %s", space_id)
                return result_path, includes_upscale
            except Exception as e:
                err_msg = str(e)
                logger.warning("%s failed: %s", space_id, err_msg[:200])
                errors.append(f"{space_id.split('/')[-1]}: {err_msg[:80]}")
                continue

        raise RuntimeError(f"All face restoration Spaces failed: {'; '.join(errors[-3:])}")

    async def _call_esrgan(self, input_path: str) -> str:
        """Try Real-ESRGAN for super resolution."""
        import logging
        from gradio_client import Client, handle_file

        logger = logging.getLogger("colorbyte.hf")
        spaces = ["doevent/Face-Real-ESRGAN"]

        for space in spaces:
            try:
                logger.info("Trying ESRGAN: %s", space)
                client = Client(space, verbose=False)
                result = await asyncio.to_thread(
                    client.predict,
                    handle_file(input_path),
                    api_name="/predict",
                )
                logger.info("ESRGAN succeeded with: %s", space)
                return str(result)
            except Exception as e:
                logger.warning("ESRGAN %s failed: %s", space, e)
                continue

        raise RuntimeError("Real-ESRGAN unavailable")

    async def _call_deoldify(self, input_path: str) -> str:
        """Try DeOldify for colorization."""
        import logging
        from gradio_client import Client, handle_file

        logger = logging.getLogger("colorbyte.hf")

        for space in self.DEOLDIFY_SPACES:
            try:
                logger.info("Trying DeOldify: %s", space)
                client = Client(space, verbose=False)
                result = await asyncio.to_thread(
                    client.predict,
                    handle_file(input_path),
                    10,
                    api_name="/predict",
                )
                logger.info("DeOldify succeeded with: %s", space)
                return str(result)
            except Exception as e:
                logger.warning("DeOldify %s failed: %s", space, e)
                continue

        raise RuntimeError("DeOldify unavailable")

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback
    ) -> ProcessingResult:
        try:
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
                    await progress_callback("Colorizing (DeOldify)...", 80)
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
            return ProcessingResult(success=False, error=str(e))


class ReplicateProvider(AIProvider):
    """Paid API via Replicate. Higher quality, faster, for production."""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self._client = None

    def _get_client(self):
        if self._client is None:
            import replicate
            self._client = replicate.Client(api_token=self.api_token)
        return self._client

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback
    ) -> ProcessingResult:
        import httpx

        client = self._get_client()
        try:
            if progress_callback:
                await progress_callback("Enhancing faces...", 20)

            with open(input_path, "rb") as f:
                gfpgan_output = await asyncio.to_thread(
                    client.run,
                    "tencentarc/gfpgan:0fbacf7afc6c144e5be9767cff80f25aff23e52b0708f17e20f9879b2f21516c",
                    input={"img": f, "version": "v1.4", "scale": 2},
                )
            current_url = str(gfpgan_output)

            if progress_callback:
                await progress_callback("Upscaling resolution...", 50)

            esrgan_output = await asyncio.to_thread(
                client.run,
                "nightmareai/real-esrgan:f121d640bd286e1fdc67f9799164c1d5be36ff74576ee11c803ae5b665dd46aa",
                input={"image": current_url, "scale": 4, "face_enhance": True},
            )
            current_url = str(esrgan_output)

            if colorize:
                if progress_callback:
                    await progress_callback("Colorizing...", 80)

                color_output = await asyncio.to_thread(
                    client.run,
                    "piddnad/ddcolor:ca494ba129e44e45f661d6ece83c4c98a9a7c774309beca01f4d095d7f4e4c97",
                    input={"image": current_url, "model_size": "large"},
                )
                current_url = str(color_output)

            if progress_callback:
                await progress_callback("Generating result...", 95)

            async with httpx.AsyncClient() as http:
                resp = await http.get(current_url)
                resp.raise_for_status()
                Path(output_path).write_bytes(resp.content)

            if progress_callback:
                await progress_callback("Complete", 100)

            return ProcessingResult(success=True, output_path=output_path)

        except Exception as e:
            return ProcessingResult(success=False, error=str(e))


class AIService:
    """Delegates to the configured AI provider."""

    def __init__(self):
        settings = get_settings()
        if settings.ai_provider == "replicate":
            self._provider: AIProvider = ReplicateProvider(settings.replicate_api_token)
        elif settings.ai_provider == "huggingface":
            self._provider = HuggingFaceProvider()
        else:
            self._provider = MockProvider()

    async def process_photo(
        self,
        input_path: str,
        output_path: str,
        colorize: bool = False,
        progress_callback: ProgressCallback = None,
    ) -> ProcessingResult:
        return await self._provider.process_photo(
            input_path, output_path, colorize, progress_callback
        )


_service: AIService | None = None


def get_ai_service() -> AIService:
    global _service
    if _service is None:
        _service = AIService()
    return _service
