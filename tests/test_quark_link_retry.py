import asyncio

import httpx
import pytest

from app.sources.quark import QuarkAuthenticationError, QuarkSource
from app.workers.runner import _error_message


def setup_client(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.sources.quark.asyncio.sleep", sleep)
    return delays


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "disconnect", 429, 503])
async def test_link_request_recovers_before_content_download(monkeypatch, failure):
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.path.endswith("/file/download")
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ConnectTimeout("", request=req)
            if failure == "disconnect":
                raise httpx.RemoteProtocolError("", request=req)
            return httpx.Response(failure, content=b"not JSON")
        return httpx.Response(200, json={"status": 200, "data": [{"download_url": "test"}]})

    delays = setup_client(monkeypatch, handler)
    assert await QuarkSource("test-cookie").get_download_urls(["fid"]) == [{"download_url": "test"}]
    assert len(calls) == 2 and delays == [1]


@pytest.mark.asyncio
async def test_link_timeout_retries_are_bounded(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ConnectTimeout("", request=req)

    delays = setup_client(monkeypatch, handler)
    with pytest.raises(httpx.ConnectTimeout):
        await QuarkSource("test-cookie").get_download_urls(["fid"])
    assert len(calls) == 3 and delays == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_failures_are_not_retried(monkeypatch, status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, content=b"not JSON")

    delays = setup_client(monkeypatch, handler)
    with pytest.raises(QuarkAuthenticationError):
        await QuarkSource("test-cookie").get_download_urls(["fid"])
    assert len(calls) == 1 and delays == []


@pytest.mark.asyncio
async def test_cancel_does_not_trigger_retry(monkeypatch):
    def handler(req):
        raise asyncio.CancelledError()

    delays = setup_client(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        await QuarkSource("test-cookie").get_download_urls(["fid"])
    assert delays == []


@pytest.mark.asyncio
async def test_service_errors_have_a_bounded_readable_failure(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503, content=b"not JSON")

    delays = setup_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await QuarkSource("test-cookie").get_download_urls(["fid"])
    assert len(calls) == 3 and delays == [1, 2]


@pytest.mark.asyncio
async def test_mutating_requests_are_not_automatically_replayed(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ConnectTimeout("", request=req)

    delays = setup_client(monkeypatch, handler)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectTimeout):
            await QuarkSource("test-cookie")._request(
                client, "POST", "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/save")
    assert len(calls) == 1 and delays == []


@pytest.mark.parametrize("error", [httpx.ConnectTimeout(""), httpx.ReadTimeout(""),
    httpx.ConnectError("https://example.test/?secret=do-not-show"), Exception("")])
def test_empty_and_transport_errors_are_actionable_without_secrets(error):
    message = _error_message(error)
    assert "進度已保留" in message and "可重試" in message
    assert "do-not-show" not in message and "https://" not in message
