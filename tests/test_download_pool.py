"""File-scoped connection pool regressions (offline, not a CDN benchmark)."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.transfer import downloader as dl


@pytest.mark.asyncio
async def test_slices_share_one_pool_and_preserve_exact_bytes(tmp_path, monkeypatch):
    data = bytes(range(256)) * 16
    clients = []
    requests = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(dl, "_SLICE_MIN", 256)
    monkeypatch.setattr(dl, "_SLICE_MAX", 256)

    def handle(request):
        requests.append(request)
        start, end = map(int, request.headers["range"][6:].split("-"))
        return httpx.Response(206, content=data[start:end + 1], headers={
            "content-range": f"bytes {start}-{end}/{len(data)}",
        })

    def factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handle))
        client = real_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    dest = tmp_path / "pooled.part"
    await dl._parallel_range_download(
        "https://example.test/file", dest, {"X-Account": "a"},
        len(data), None, 2, 2,
    )
    assert dest.read_bytes() == data
    assert len(requests) == 16
    assert len(clients) == 17  # one pool owner + isolated jars for 16 slices
    assert all(client.is_closed for client in clients)
    assert all(r.headers["X-Account"] == "a" for r in requests)
    assert not dest.with_suffix(".part.ranges.json").exists()


@pytest.mark.asyncio
async def test_cancellation_drains_slices_before_closing_pool(tmp_path, monkeypatch):
    started = asyncio.Event()
    clients = []
    real_client = httpx.AsyncClient
    active = 0
    monkeypatch.setattr(dl, "_SLICE_MIN", 256)
    monkeypatch.setattr(dl, "_SLICE_MAX", 256)

    async def handle(request):
        nonlocal active
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    def factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handle))
        client = real_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    task = asyncio.create_task(dl._parallel_range_download(
        "https://example.test/file", tmp_path / "cancel.part", {},
        4096, None, 2, 2,
    ))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0
    assert len(clients) >= 2 and all(client.is_closed for client in clients)
    assert dl.downloaded_bytes_on_disk(tmp_path / "cancel.part", 4096) == 0


@pytest.mark.asyncio
async def test_redirect_seed_works_without_stale_auth_between_attempts():
    observed = []

    def handle(request):
        observed.append((request.url.host, request.headers.get("cookie", "")))
        if request.url.host == "origin.example.test":
            return httpx.Response(302, headers={
                "location": "https://cdn.example.test/file",
                "set-cookie": "seed=fresh; Domain=.example.test; Path=/; Secure",
            })
        return httpx.Response(200, content=b"ok", headers={
            "set-cookie": "session=stale; Domain=cdn.example.test; Path=/; Secure",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as owner:
        for token in ("old", "new"):
            async with dl._borrow_download_client(owner) as client:
                async with client.stream("GET", "https://origin.example.test/file",
                                         headers={"Cookie": f"session={token}"}) as resp:
                    assert await resp.aread() == b"ok"
            assert not owner.is_closed
    assert observed[0][1] == "session=old"
    assert observed[2][1] == "session=new"
    assert observed[1][1] == observed[3][1] == "seed=fresh"


@pytest.mark.asyncio
async def test_real_http_keepalive_reuses_socket_with_isolated_clients():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        accepted = 0

        def get_request(self):
            request = super().get_request()
            self.accepted += 1
            return request

    try:
        server = Server(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("sandbox disallows localhost socket.bind")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as owner:
            for _ in range(10):
                async with dl._borrow_download_client(owner) as client:
                    async with client.stream("GET", f"http://127.0.0.1:{server.server_port}/") as resp:
                        assert await resp.aread() == b"ok"
        assert server.accepted == 1  # 10 attempts, 1 real TCP connection
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
