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
    """Free API via HuggingFace Spaces Gradio endpoints."""

    async def process_photo(
        self, input_path: str, output_path: str, colorize: bool, progress_callback: ProgressCallback
    ) -> ProcessingResult:
        try:
            from gradio_client import Client, handle_file

            # Step 1: Face restoration via GFPGAN
            if progress_callback:
                await progress_callback("Enhancing faces (GFPGAN)...", 15)

            gfpgan = Client("Xintao/GFPGAN", verbose=False)
            result = await asyncio.to_thread(
                gfpgan.predict,
                handle_file(input_path),
                "v1.4",  # version
                2,  # rescaling_factor
                api_name="/predict",
            )
            # GFPGAN returns a filepath string
            current_path = str(result)

            # Step 2: Super resolution via Real-ESRGAN
            if progress_callback:
                await progress_callback("Upscaling resolution (Real-ESRGAN)...", 50)

            esrgan = Client("doevent/Face-Real-ESRGAN", verbose=False)
            result = await asyncio.to_thread(
                esrgan.predict,
                handle_file(current_path),
                api_name="/predict",
            )
            current_path = str(result)

            # Step 3: Colorization (optional)
            if colorize:
                if progress_callback:
                    await progress_callback("Colorizing (DeOldify)...", 80)

                try:
                    deoldify = Client("jantic/DeOldify", verbose=False)
                    result = await asyncio.to_thread(
                        deoldify.predict,
                        handle_file(current_path),
                        10,  # render_factor
                        api_name="/predict",
                    )
                    current_path = str(result)
                except Exception:
                    # Colorization is optional - continue without it
                    pass

            if progress_callback:
                await progress_callback("Generating result...", 95)

            # Copy final result to output path
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
