import base64
from pathlib import Path

import httpx
import pytest

from app.services.ai_service import PhotoFixProvider


TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAn/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AJdAA//Z"
)


@pytest.mark.anyio
async def test_m2_online_does_not_fall_through_to_remote(monkeypatch, tmp_path):
    input_path = tmp_path / "input.jpg"
    output_path = tmp_path / "output.jpg"
    input_path.write_bytes(TINY_JPEG)
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://m2.example/health":
            return httpx.Response(200, json={"ok": True})
        if str(request.url) == "https://m2.example/api/restore":
            return httpx.Response(502, text="m2 failed")
        if str(request.url) == "https://remote.example/api/restore":
            return httpx.Response(200, json={"ok": True, "result": base64.b64encode(TINY_JPEG).decode("ascii")})
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    provider = PhotoFixProvider(
        "https://remote.example/api/restore",
        m2_api_url="https://m2.example/api/restore",
        m2_health_url="https://m2.example/health",
    )

    result = await provider.process_photo(str(input_path), str(output_path), False, None)

    assert not result.success
    assert "https://remote.example/api/restore" not in calls


@pytest.mark.anyio
async def test_m2_offline_allows_remote_fallback(monkeypatch, tmp_path):
    input_path = tmp_path / "input.jpg"
    output_path = tmp_path / "output.jpg"
    input_path.write_bytes(TINY_JPEG)
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://m2.example/health":
            return httpx.Response(503, json={"ok": False})
        if str(request.url) == "https://remote.example/api/restore":
            return httpx.Response(200, json={"ok": True, "result": base64.b64encode(TINY_JPEG).decode("ascii")})
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    provider = PhotoFixProvider(
        "https://remote.example/api/restore",
        m2_api_url="https://m2.example/api/restore",
        m2_health_url="https://m2.example/health",
    )

    result = await provider.process_photo(str(input_path), str(output_path), False, None)

    assert result.success
    assert output_path.exists()
    assert "https://remote.example/api/restore" in calls
