import asyncio
import base64
import httpx
import pytest

from app.services.ai_service import PhotoFixProvider


async def _no_sleep(_seconds):
    return None


@pytest.fixture
def anyio_backend():
    # production only ever runs under real asyncio (uvicorn); ai_service.py's
    # asyncio.get_running_loop() calls aren't trio-compatible, same as the
    # rest of this file's pre-existing async tests implicitly assume.
    return "asyncio"


TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAn/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AJdAA//Z"
)


@pytest.mark.anyio
async def test_poll_transport_error_repolls_same_job_not_resubmit(monkeypatch, tmp_path):
    """T246: a transport exception (no HTTP response) on one poll must retry
    the SAME job_id via another poll, not re-submit a fresh job."""
    input_path = tmp_path / "input.jpg"
    output_path = tmp_path / "output.jpg"
    input_path.write_bytes(TINY_JPEG)

    submit_calls = []
    poll_calls = []
    poll_attempt = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://remote.example/api/restore/async":
            submit_calls.append(url)
            return httpx.Response(200, json={"ok": True, "job_id": "abc123", "status": "queued"})
        if url.startswith("https://remote.example/api/restore/result/"):
            poll_calls.append(url)
            poll_attempt["n"] += 1
            if poll_attempt["n"] == 1:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={
                "ok": True, "status": "done",
                "result": base64.b64encode(TINY_JPEG).decode("ascii"),
                "route_used": "m2_ab_orchestrator",
            })
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    provider = PhotoFixProvider("https://remote.example/api/restore")
    provider.M2_POLL_INTERVAL_S = 0.01  # keep the test fast

    result = await provider.process_photo(str(input_path), str(output_path), False, None)

    assert result.success, f"expected success, got error={result.error!r}"
    assert len(submit_calls) == 1, f"expected exactly 1 submit, got {len(submit_calls)}: {submit_calls}"
    assert len(poll_calls) == 2, f"expected 2 poll attempts (1 failed + 1 succeeded), got {len(poll_calls)}"


@pytest.mark.anyio
async def test_poll_transport_error_exhausts_budget_then_fails_loud(monkeypatch, tmp_path):
    """T246 guardrail: once the poll-retry budget is exhausted, the call still
    fails loud (no silent success, no infinite retry)."""
    input_path = tmp_path / "input.jpg"
    output_path = tmp_path / "output.jpg"
    input_path.write_bytes(TINY_JPEG)

    submit_calls = []
    poll_calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://remote.example/api/restore/async":
            submit_calls.append(url)
            return httpx.Response(200, json={"ok": True, "job_id": "abc123", "status": "queued"})
        if url.startswith("https://remote.example/api/restore/result/"):
            poll_calls.append(url)
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    provider = PhotoFixProvider("https://remote.example/api/restore")
    provider.M2_POLL_INTERVAL_S = 0.01

    result = await provider.process_photo(str(input_path), str(output_path), False, None)

    assert not result.success
    assert result.error_code == "upstream_unavailable"
    # 2 outer attempts (task="restore" -> delays=[0, 9]) x (1 + POLL_TRANSPORT_RETRY_MAX) polls each
    expected_polls_per_attempt = 1 + PhotoFixProvider.POLL_TRANSPORT_RETRY_MAX
    assert len(poll_calls) == 2 * expected_polls_per_attempt, poll_calls
    assert len(submit_calls) == 2, "outer retry should still re-submit once its own poll-retry budget is exhausted"
