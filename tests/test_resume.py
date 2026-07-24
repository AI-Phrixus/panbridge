import asyncio
from pathlib import Path

import httpx
import pytest
from httpx import Response

from app.transfer.downloader import resumable_download


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> Response:
        self.calls += 1
        rng = request.headers.get("range") or request.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])
            body = self.payload[start:]
            headers = {
                "content-length": str(len(body)),
                "content-range": f"bytes {start}-{len(self.payload)-1}/{len(self.payload)}",
            }
            return Response(206, content=body, headers=headers)
        return Response(
            200,
            content=self.payload,
            headers={"content-length": str(len(self.payload))},
        )


@pytest.mark.asyncio
async def test_resumable_download(tmp_path: Path, monkeypatch):
    data = b"abcdefghijklmnopqrstuvwxyz" * 100
    transport = MockTransport(data)

    async def fake_client(*args, **kwargs):
        return httpx.AsyncClient(transport=transport)

    # Patch AsyncClient used inside resumable_download via monkeypatch of httpx.AsyncClient
    real = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real(transport=transport, follow_redirects=True)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    dest = tmp_path / "f.bin.part"
    # first partial write manually
    dest.write_bytes(data[:50])

    progresses = []

    async def cb(done, total):
        progresses.append((done, total))

    out = await resumable_download(
        "https://example.test/file",
        dest,
        expected_size=len(data),
        progress_cb=cb,
    )
    assert out.read_bytes() == data
    assert transport.calls >= 1
