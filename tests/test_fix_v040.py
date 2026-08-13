from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from packaging.version import Version
from starlette.requests import Request

from app.api import routes_stream
from app.api.routes_stream import (
    _parse_range,
    _rewrite_hls_manifest,
    _send_quark_stream_request,
)
from app.auth.quark_session import (
    delete_quark_credential,
    load_quark_source,
    save_quark_credential,
)
from app.auth.onedrive_session import make_onedrive_sink, save_onedrive_credential
from app.db import Database
from app.security import (
    decrypt_json,
    encrypt_json,
    make_hls_asset_token,
    make_stream_token,
    verify_hls_asset_token,
    verify_stream_token,
)
from app.sources.base import SourceFile
from app.sources.quark import QuarkSource
from app.sources.quark import QuarkAuthenticationError
from app.sinks.pcloud import PCloudSink
from app.stream.resolve import StreamSource, resolve_stream
from app.transfer.downloader import (
    _prepare_single_stream_resume,
    _probe_size,
    _recover_legacy_ranges,
    _verified_contiguous_prefix,
    downloaded_bytes_on_disk,
    resumable_download,
)
from app.workers.runner import Worker
from app.config import Settings
from app.config import validate_runtime_security


class RangeTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload: bytes, reject: int | None = None) -> None:
        self.payload = payload
        self.reject = reject
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={"content-length": str(len(self.payload)), "accept-ranges": "bytes"},
            )
        if self.reject:
            return httpx.Response(self.reject, content=b"require login")
        value = request.headers.get("range")
        if not value:
            return httpx.Response(
                200,
                content=self.payload,
                headers={"content-length": str(len(self.payload))},
            )
        assert value.startswith("bytes=")
        start_s, end_s = value.removeprefix("bytes=").split("-", 1)
        start = int(start_s)
        end = int(end_s) if end_s else len(self.payload) - 1
        body = self.payload[start : end + 1]
        return httpx.Response(
            206,
            content=body,
            headers={
                "content-length": str(len(body)),
                "content-range": f"bytes {start}-{end}/{len(self.payload)}",
            },
        )


def test_fix_release_version():
    assert Version(Settings().app_version) >= Version("0.4.0")


def test_public_default_credentials_fail_closed_at_startup():
    with pytest.raises(RuntimeError, match="PANBRIDGE_SECRET"):
        validate_runtime_security(Settings())
    validate_runtime_security(
        Settings(
            panbridge_secret="s" * 48,
            admin_password="a-strong-password",
        )
    )


class BombStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("range capability probe must not read a full response body")
        yield b""  # pragma: no cover


class IgnoresRangeTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(50 * 1024**3)})
        return httpx.Response(
            200,
            headers={"content-length": str(50 * 1024**3)},
            stream=BombStream(),
        )


def _patch_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    real = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return real(transport=transport, follow_redirects=True)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)


@pytest.mark.asyncio
async def test_quark_rotating_cookie_is_merged_and_persisted(tmp_path: Path):
    db = Database(tmp_path / "cookie.db")
    await db.connect()
    await db.set_credential(
        "quark",
        encrypt_json({"cookie": "base=1; __pus=long; __puus=old", "nickname": "tester"}),
    )
    source = await load_quark_source(db)
    response = httpx.Response(
        200,
        headers=[
            ("set-cookie", "__puus=fresh; Path=/; Secure; HttpOnly"),
            ("set-cookie", "unrelated=ignored; Path=/"),
        ],
        request=httpx.Request("GET", "https://drive.quark.cn/config"),
    )
    await source._capture_cookie_update(response)

    assert "__puus=fresh" in source.cookie
    assert "unrelated=" not in source.cookie
    saved = decrypt_json(await db.get_credential("quark"))
    assert "__puus=fresh" in saved["cookie"]
    assert saved["nickname"] == "tester"
    await db.close()


@pytest.mark.asyncio
async def test_concurrent_quark_rotations_do_not_overwrite_each_other(tmp_path: Path):
    db = Database(tmp_path / "cookie-race.db")
    await db.connect()
    await save_quark_credential(
        db, "base=1; __pus=oldS; __puus=oldU", "tester"
    )
    source_u, source_s = await asyncio.gather(
        load_quark_source(db), load_quark_source(db)
    )

    def response(cookie: str) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"set-cookie": f"{cookie}; Path=/; Secure"},
            request=httpx.Request("GET", "https://drive.quark.cn/config"),
        )

    await asyncio.gather(
        source_u._capture_cookie_update(response("__puus=newU")),
        source_s._capture_cookie_update(response("__pus=newS")),
    )
    saved = decrypt_json(await db.get_credential("quark"))
    assert "__puus=newU" in saved["cookie"]
    assert "__pus=newS" in saved["cookie"]
    await db.close()


@pytest.mark.asyncio
async def test_quark_control_requests_serialize_same_key_rotation(tmp_path: Path):
    db = Database(tmp_path / "cookie-order.db")
    await db.connect()
    await save_quark_credential(db, "base=1; __puus=baseU", "tester")
    first, second = await asyncio.gather(
        load_quark_source(db), load_quark_source(db)
    )
    request_cookies: list[str] = []

    class RotatingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            cookie = request.headers.get("cookie", "")
            request_cookies.append(cookie)
            next_value = "firstU" if "__puus=baseU" in cookie else "secondU"
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"set-cookie": f"__puus={next_value}; Path=/; Secure"},
            )

    async with httpx.AsyncClient(transport=RotatingTransport()) as client:
        await asyncio.gather(
            first._request(client, "GET", "https://drive.quark.cn/test", headers=first.headers),
            second._request(client, "GET", "https://drive.quark.cn/test", headers=second.headers),
        )
    assert "__puus=baseU" in request_cookies[0]
    assert "__puus=firstU" in request_cookies[1]
    saved = decrypt_json(await db.get_credential("quark"))
    assert "__puus=secondU" in saved["cookie"]
    await db.close()


@pytest.mark.asyncio
async def test_new_quark_login_waits_for_inflight_old_control_response(tmp_path: Path):
    db = Database(tmp_path / "cookie-linearization.db")
    await db.connect()
    await save_quark_credential(db, "base=old; __puus=oldU", "old")
    source = await load_quark_source(db)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=SlowTransport()) as client:
        old_request = asyncio.create_task(
            source._request(
                client,
                "GET",
                "https://drive.quark.cn/test",
                headers=source.headers,
            )
        )
        await started.wait()
        new_login = asyncio.create_task(
            save_quark_credential(db, "base=new; __puus=newU", "new")
        )
        await asyncio.sleep(0)
        assert not new_login.done(), "new generation must linearize after old response"
        release.set()
        await old_request
        await new_login

    saved = decrypt_json(await db.get_credential("quark"))
    assert saved["nickname"] == "new"
    assert "__puus=newU" in saved["cookie"]
    await db.close()


@pytest.mark.asyncio
async def test_old_quark_source_cannot_overwrite_new_login(tmp_path: Path):
    db = Database(tmp_path / "cookie-generation.db")
    await db.connect()
    await save_quark_credential(db, "base=old; __puus=oldU", "old")
    old_source = await load_quark_source(db)
    await save_quark_credential(db, "base=new; __puus=newU", "new")

    stale = httpx.Response(
        200,
        headers={"set-cookie": "__puus=staleU; Path=/; Secure"},
        request=httpx.Request("GET", "https://drive.quark.cn/config"),
    )
    with pytest.raises(QuarkAuthenticationError, match="登入已更新"):
        await old_source._capture_cookie_update(stale)

    saved = decrypt_json(await db.get_credential("quark"))
    assert saved["nickname"] == "new"
    assert "base=new" in saved["cookie"]
    assert "__puus=newU" in saved["cookie"]
    assert "staleU" not in saved["cookie"]
    await db.close()


@pytest.mark.asyncio
async def test_deleted_quark_login_cannot_be_recreated_by_old_response(tmp_path: Path):
    db = Database(tmp_path / "cookie-delete.db")
    await db.connect()
    await save_quark_credential(db, "base=old; __puus=oldU", "old")
    source = await load_quark_source(db)
    await delete_quark_credential(db)
    stale = httpx.Response(
        200,
        headers={"set-cookie": "__puus=staleU; Path=/; Secure"},
        request=httpx.Request("GET", "https://drive.quark.cn/config"),
    )
    with pytest.raises(QuarkAuthenticationError, match="斷開"):
        await source._capture_cookie_update(stale)
    assert await db.get_credential("quark") is None
    await db.close()


@pytest.mark.asyncio
async def test_concurrent_onedrive_refreshes_chain_latest_rotating_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = Database(tmp_path / "onedrive-race.db")
    await db.connect()
    await save_onedrive_credential(
        db,
        {
            "access_token": "a0",
            "refresh_token": "r0",
            "client_id": "client",
            "email": "old@example.test",
            "expires_in": 3600,
        },
    )
    first, second = await asyncio.gather(
        make_onedrive_sink(db), make_onedrive_sink(db)
    )
    refresh_inputs: list[str] = []

    async def fake_refresh(client_id: str, refresh_token: str):
        refresh_inputs.append(refresh_token)
        await asyncio.sleep(0)
        index = len(refresh_inputs)
        return {
            "access_token": f"a{index}",
            "refresh_token": f"r{index}",
            "client_id": client_id,
            "expires_in": 3600,
        }

    monkeypatch.setattr(
        "app.auth.onedrive_session.refresh_access_token", fake_refresh
    )
    assert await asyncio.gather(first._refresh(), second._refresh()) == [True, True]
    assert refresh_inputs == ["r0", "r1"]
    saved = decrypt_json(await db.get_credential("onedrive"))
    assert saved["access_token"] == "a2"
    assert saved["refresh_token"] == "r2"
    await db.close()


@pytest.mark.asyncio
async def test_old_onedrive_sink_cannot_overwrite_new_device_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = Database(tmp_path / "onedrive-generation.db")
    await db.connect()
    await save_onedrive_credential(
        db,
        {
            "access_token": "old-a",
            "refresh_token": "old-r",
            "client_id": "old-client",
            "email": "old@example.test",
        },
    )
    old_sink = await make_onedrive_sink(db)
    await save_onedrive_credential(
        db,
        {
            "access_token": "new-a",
            "refresh_token": "new-r",
            "client_id": "new-client",
            "email": "new@example.test",
        },
    )

    async def should_not_refresh(*args):
        raise AssertionError("old generation must fail before Microsoft refresh")

    monkeypatch.setattr(
        "app.auth.onedrive_session.refresh_access_token", should_not_refresh
    )
    assert await old_sink._refresh() is False
    saved = decrypt_json(await db.get_credential("onedrive"))
    assert saved["email"] == "new@example.test"
    assert saved["refresh_token"] == "new-r"
    await db.close()


def test_player_token_is_scoped_to_one_file():
    token = make_stream_token(7, 11)
    assert verify_stream_token(token, 7, 11)
    assert not verify_stream_token(token, 7, 12)
    assert not verify_stream_token(token, 8, 11)
    assert not verify_stream_token("garbage", 7, 11)


def test_range_parser_supports_players_and_rejects_multi_range():
    assert _parse_range("bytes=10-19", 100) == (10, 19)
    assert _parse_range("bytes=90-", 100) == (90, 99)
    assert _parse_range("bytes=-20", 100) == (80, 99)
    with pytest.raises(ValueError):
        _parse_range("bytes=0-1,5-6", 100)
    with pytest.raises(ValueError):
        _parse_range("bytes=100-", 100)


@pytest.mark.asyncio
async def test_signed_player_head_works_without_browser_cookie(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve(*args, **kwargs):
        return StreamSource(
            kind="http",
            url="https://cdn.example/video.mp4",
            filename="電影.mp4",
            size=12345,
            content_type="video/mp4",
        )

    monkeypatch.setattr(routes_stream, "resolve_stream", fake_resolve)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "HEAD",
        "scheme": "https",
        "path": "/api/tasks/1/files/2/stream",
        "raw_path": b"/api/tasks/1/files/2/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("example.test", 443),
    }
    request = Request(scope)
    token = make_stream_token(1, 2)
    response = await routes_stream.stream_file(1, 2, request, token=token, transcode=False)
    assert response.status_code == 200
    assert response.headers["content-length"] == "12345"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]

    with pytest.raises(HTTPException) as caught:
        await routes_stream.stream_file(1, 2, request, token="wrong", transcode=False)
    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_incomplete_part_is_not_used_as_local_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = Database(tmp_path / "stream.db")
    await db.connect()
    job_id = await db.create_job("baidu", "https://pan.baidu.com/s/x")
    file_id = await db.create_file(job_id, "movie.mp4", size=100, source_fid="9")
    part = tmp_path / "movie.mp4.part"
    part.write_bytes(b"partial")
    await db.update_file(file_id, status="downloading", local_path=str(part), downloaded_bytes=7)
    await db.set_credential("baidu", encrypt_json({"cookie": "BDUSS=test"}))

    async def fake_prepare(self, file, share_meta):
        return "https://cdn.example/original.mp4"

    monkeypatch.setattr("app.stream.resolve.BaiduSource.prepare_download", fake_prepare)
    source = await resolve_stream(db, job_id, file_id)
    assert source.kind == "baidu"
    assert source.local_path is None
    await db.close()


def test_sparse_parallel_progress_does_not_trust_file_size(tmp_path: Path):
    part = tmp_path / "large.part"
    with open(part, "wb") as file_obj:
        file_obj.truncate(100)
    Path(str(part) + ".ranges.json").write_text(
        json.dumps(
            {
                "version": 2,
                "size": 100,
                "prefix": 10,
                "ranges": [[10, 39], [40, 69], [70, 99]],
                "done": ["40-69"],
            }
        )
    )
    assert part.stat().st_size == 100
    assert downloaded_bytes_on_disk(part, 100) == 40


def test_large_v1_resume_metadata_keeps_unambiguous_completed_slices(tmp_path: Path):
    size = 700 * 1024 * 1024
    part = tmp_path / "legacy.part"
    with open(part, "wb") as file_obj:
        file_obj.truncate(size)
    metadata = {"size": size, "prefix": 0, "done": ["0", "2"]}
    Path(str(part) + ".ranges.json").write_text(json.dumps(metadata))

    recovered = _recover_legacy_ranges(metadata, size)
    assert recovered is not None
    ranges, done = recovered
    assert ranges[0] == (0, 64 * 1024 * 1024 - 1)
    assert done == {
        f"{ranges[0][0]}-{ranges[0][1]}",
        f"{ranges[2][0]}-{ranges[2][1]}",
    }
    assert downloaded_bytes_on_disk(part, size) == 128 * 1024 * 1024


def test_parallel_fallback_keeps_completed_contiguous_head():
    metadata = {
        "version": 2,
        "size": 30,
        "prefix": 0,
        "ranges": [[0, 9], [10, 19], [20, 29]],
        "done": ["0-9", "10-19"],
    }
    assert _verified_contiguous_prefix(metadata, 30) == 20
    metadata["done"] = ["0-9", "20-29"]
    assert _verified_contiguous_prefix(metadata, 30) == 10


def test_single_fallback_flushes_data_before_removing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    part = tmp_path / "ordered.part"
    part.write_bytes(b"abcdefgh" + b"\0" * 8)
    meta = Path(str(part) + ".ranges.json")
    meta.write_text(
        json.dumps(
            {
                "version": 2,
                "size": 16,
                "prefix": 0,
                "ranges": [[0, 7], [8, 15]],
                "done": ["0-7"],
            }
        )
    )
    events: list[str] = []
    original_unlink = Path.unlink

    def record_file_sync(path: Path):
        events.append("data-fsync")

    def record_unlink(path: Path, *args, **kwargs):
        events.append("sidecar-unlink")
        return original_unlink(path, *args, **kwargs)

    def record_directory_sync(path: Path):
        events.append("directory-fsync")

    monkeypatch.setattr("app.transfer.downloader._fsync_file", record_file_sync)
    monkeypatch.setattr(Path, "unlink", record_unlink)
    monkeypatch.setattr("app.transfer.downloader._fsync_directory", record_directory_sync)
    assert _prepare_single_stream_resume(part, 16) == 8
    assert part.read_bytes() == b"abcdefgh"
    assert events == ["data-fsync", "sidecar-unlink", "directory-fsync"]


def test_hls_manifest_rewrites_relative_segments_maps_keys_and_child_playlists():
    manifest = """#EXTM3U
#EXT-X-MAP:URI="init.mp4"
#EXT-X-KEY:METHOD=AES-128,URI="../key.bin"
child/720p.m3u8
segment-1.m4s
"""
    rewritten = _rewrite_hls_manifest(
        manifest, "https://video.pds.quark.cn/path/master.m3u8?sig=x", 3, 7
    )
    assert "segment-1.m4s" not in rewritten.replace("https%", "")
    assets = []
    for line in rewritten.splitlines():
        for raw in __import__("re").findall(r"asset=([^\"\s,]+)", line):
            token = unquote(raw)
            url = verify_hls_asset_token(token, 3, 7)
            assert url is not None
            assets.append(url)
    assert set(assets) == {
        "https://video.pds.quark.cn/path/init.mp4",
        "https://video.pds.quark.cn/key.bin",
        "https://video.pds.quark.cn/path/child/720p.m3u8",
        "https://video.pds.quark.cn/path/segment-1.m4s",
    }


def test_hls_manifest_blocks_foreign_and_private_assets_without_signing_them():
    manifest = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://attacker.example/leak"
http://169.254.169.254/latest/meta-data
https://video.pds.quark.cn/safe.m4s
"""
    rewritten = _rewrite_hls_manifest(
        manifest, "https://video.pds.quark.cn/master.m3u8", 8, 9
    )
    assert "attacker.example" not in rewritten
    assert "169.254.169.254" not in rewritten
    assert rewritten.count("asset=blocked") == 2
    assert "asset=blocked" not in rewritten.splitlines()[-1]

    foreign = make_hls_asset_token(8, 9, "https://attacker.example/leak")
    private = make_hls_asset_token(8, 9, "http://127.0.0.1/secret")
    assert verify_hls_asset_token(foreign, 8, 9) is None
    assert verify_hls_asset_token(private, 8, 9) is None


@pytest.mark.asyncio
async def test_quark_redirect_never_sends_cookie_to_foreign_host():
    calls: list[str] = []

    class RedirectTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            assert request.headers.get("cookie") == "secret=must-not-leak"
            return httpx.Response(
                302,
                headers={"location": "https://attacker.example/steal"},
            )

    async with httpx.AsyncClient(
        transport=RedirectTransport(), follow_redirects=False
    ) as client:
        with pytest.raises(RuntimeError, match="非官方"):
            await _send_quark_stream_request(
                client,
                "GET",
                "https://video.pds.quark.cn/master.m3u8",
                {"cookie": "secret=must-not-leak"},
            )
    assert calls == ["https://video.pds.quark.cn/master.m3u8"]


@pytest.mark.asyncio
async def test_remote_proxy_keeps_compressed_length_and_raw_body(
    monkeypatch: pytest.MonkeyPatch,
):
    plain = (b"#EXTM3U\n" + b"segment.ts\n" * 100)
    wire = gzip.compress(plain)
    seen_accept_encoding = ""

    class CompressedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal seen_accept_encoding
            seen_accept_encoding = request.headers.get("accept-encoding", "")

            class CompressedStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield wire

            return httpx.Response(
                200,
                stream=CompressedStream(),
                headers={
                    "content-type": "video/mp4",
                    "content-encoding": "gzip",
                    "content-length": str(len(wire)),
                },
            )

    async def fake_resolve(*args, **kwargs):
        return StreamSource(
            kind="http",
            url="https://cdn.example/video.mp4",
            filename="video.mp4",
            size=len(plain),
            content_type="video/mp4",
        )

    monkeypatch.setattr(routes_stream, "resolve_stream", fake_resolve)
    _patch_transport(monkeypatch, CompressedTransport())
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/tasks/1/files/2/stream",
        "raw_path": b"/api/tasks/1/files/2/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("example.test", 443),
    }
    response = await routes_stream.stream_file(
        1, 2, Request(scope), token=make_stream_token(1, 2), transcode=False
    )
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert seen_accept_encoding == "identity"
    assert response.headers["content-encoding"] == "gzip"
    assert int(response.headers["content-length"]) == len(wire)
    assert body == wire


@pytest.mark.asyncio
async def test_player_playlist_supports_windows_vlc_potplayer_and_infuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = Database(tmp_path / "playlist.db")
    await db.connect()
    job_id = await db.create_job("quark", "https://pan.quark.cn/s/x")
    file_id = await db.create_file(job_id, "電影.mkv", size=123, source_fid="f")
    monkeypatch.setattr(routes_stream, "db", db)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": f"/api/tasks/{job_id}/files/{file_id}/playlist.m3u",
        "raw_path": b"/playlist.m3u",
        "query_string": b"",
        "headers": [(b"host", b"panbridge.example.test")],
        "client": ("127.0.0.1", 1),
        "server": ("panbridge.example.test", 443),
    }
    response = await routes_stream.player_playlist(
        job_id, file_id, Request(scope), None
    )
    playlist = response.body.decode("utf-8")
    assert playlist.startswith("#EXTM3U\n#EXTINF:-1,電影.mkv\n")
    stream_url = playlist.splitlines()[-1]
    assert stream_url.startswith(
        f"https://panbridge.example.test/api/tasks/{job_id}/files/{file_id}/stream?"
    )
    token = parse_qs(urlsplit(stream_url).query)["token"][0]
    assert verify_stream_token(token, job_id, file_id)
    assert "attachment" in response.headers["content-disposition"]

    page = await routes_stream.play_page(job_id, file_id, Request(scope), None)
    page_html = page.body.decode("utf-8")
    assert "PotPlayer (Win)" in page_html
    assert "Windows / VLC" in page_html
    assert "Infuse 付費版" in page_html
    assert 'src="/static/vendor/hls.light.min.js"' in page_html
    assert "cdn.jsdelivr.net" not in page_html
    hls_path = Path(routes_stream.__file__).parents[2] / "web/static/vendor/hls.light.min.js"
    digest = base64.b64encode(hashlib.sha384(hls_path.read_bytes()).digest()).decode()
    assert f'integrity="sha384-{digest}"' in page_html
    assert "DOMContentLoaded" in page_html
    assert hls_path.exists()
    await db.close()


@pytest.mark.asyncio
async def test_pcloud_large_upload_uses_streaming_documented_multipart_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "large.bin"
    payload = bytes(range(251)) * 5000
    source.write_bytes(payload)
    methods: list[str] = []
    request_body = b""

    class UploadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal request_body
            methods.append(request.method)
            request_body = await request.aread()
            assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
            assert int(request.headers["content-length"]) == len(request_body)
            return httpx.Response(
                200,
                json={
                    "result": 0,
                    "metadata": [{"fileid": 9, "name": "large.bin", "size": len(payload)}],
                },
            )

    sink = PCloudSink("secret")

    async def fake_ensure(path: str) -> int:
        return 7

    async def fake_stat(path: str):
        return None

    monkeypatch.setattr(sink, "ensure_path", fake_ensure)
    monkeypatch.setattr(sink, "stat_file", fake_stat)
    _patch_transport(monkeypatch, UploadTransport())
    progress: list[int] = []

    async def report(done: int, total: int):
        assert total == len(payload)
        progress.append(done)

    result = await sink.upload_file(source, "/PanBridge", "large.bin", report)
    assert methods == ["POST"]
    assert payload in request_body
    assert b'name="auth"' in request_body
    assert b'name="file"' in request_body
    assert progress[-1] == len(payload)
    assert result["size"] == len(payload)


@pytest.mark.asyncio
async def test_range_probe_never_buffers_entire_large_file():
    async with httpx.AsyncClient(transport=IgnoresRangeTransport()) as client:
        size, supported = await _probe_size(client, "https://cdn.example/huge", {})
    assert size == 50 * 1024**3
    assert supported is False


@pytest.mark.asyncio
async def test_range_probe_fallback_collapses_sparse_parallel_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = b"verified-complete-payload"
    first_end = 7
    part = tmp_path / "probe-fallback.part"
    with open(part, "wb") as file_obj:
        file_obj.write(data[: first_end + 1])
        file_obj.truncate(len(data))
    meta = Path(str(part) + ".ranges.json")
    meta.write_text(
        json.dumps(
            {
                "version": 2,
                "size": len(data),
                "prefix": 0,
                "ranges": [[0, first_end], [first_end + 1, len(data) - 1]],
                "done": [f"0-{first_end}"],
            }
        )
    )

    async def no_range_probe(client, url, headers):
        return len(data), False

    monkeypatch.setattr("app.transfer.downloader._probe_size", no_range_probe)
    _patch_transport(monkeypatch, RangeTransport(data))
    result = await resumable_download(
        "https://cdn.example/file",
        part,
        expected_size=len(data),
        connections=4,
        max_retries=2,
    )
    assert result.read_bytes() == data
    assert not meta.exists()


@pytest.mark.asyncio
async def test_range_probe_fallback_rejects_truncated_claimed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = b"verified-complete-payload"
    first_end = 7
    part = tmp_path / "truncated-probe-fallback.part"
    part.write_bytes(data[:2])
    meta = Path(str(part) + ".ranges.json")
    meta.write_text(
        json.dumps(
            {
                "version": 2,
                "size": len(data),
                "prefix": 0,
                "ranges": [[0, first_end], [first_end + 1, len(data) - 1]],
                "done": [f"0-{first_end}"],
            }
        )
    )

    async def no_range_probe(client, url, headers):
        return len(data), False

    monkeypatch.setattr("app.transfer.downloader._probe_size", no_range_probe)
    _patch_transport(monkeypatch, RangeTransport(data))
    result = await resumable_download(
        "https://cdn.example/file",
        part,
        expected_size=len(data),
        connections=4,
        max_retries=2,
    )
    assert result.read_bytes() == data
    assert not meta.exists()


@pytest.mark.asyncio
async def test_single_download_flushes_complete_file_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = b"single-stream-complete"
    part = tmp_path / "single.part"

    async def no_range_probe(client, url, headers):
        return len(data), False

    synced: list[bytes] = []
    monkeypatch.setattr("app.transfer.downloader._probe_size", no_range_probe)
    monkeypatch.setattr(
        "app.transfer.downloader._fsync_file",
        lambda path: synced.append(path.read_bytes()),
    )
    _patch_transport(monkeypatch, RangeTransport(data))
    result = await resumable_download(
        "https://cdn.example/file",
        part,
        expected_size=len(data),
        connections=1,
        max_retries=2,
    )
    assert result.read_bytes() == data
    assert synced == [data]


@pytest.mark.asyncio
async def test_parallel_resume_uses_persisted_ranges_when_connection_count_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = bytes(range(251)) * 34000  # > 8 MiB, so the parallel path is selected
    part = tmp_path / "resume.part"
    first_end = 8 * 1024 * 1024 - 1
    with open(part, "wb") as file_obj:
        file_obj.write(data[: first_end + 1])
        file_obj.truncate(len(data))
    Path(str(part) + ".ranges.json").write_text(
        json.dumps(
            {
                "version": 2,
                "size": len(data),
                "prefix": 0,
                "ranges": [[0, first_end], [first_end + 1, len(data) - 1]],
                "done": [f"0-{first_end}"],
            }
        )
    )
    transport = RangeTransport(data)
    _patch_transport(monkeypatch, transport)
    result = await resumable_download(
        "https://cdn.example/file",
        part,
        expected_size=len(data),
        connections=7,
        max_retries=2,
    )
    assert result.read_bytes() == data
    assert not Path(str(part) + ".ranges.json").exists()


@pytest.mark.asyncio
async def test_stale_parallel_sidecar_without_part_restarts_missing_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = bytes(range(251)) * 34000  # > 8 MiB selects parallel range mode
    part = tmp_path / "missing-part.part"
    first_end = 8 * 1024 * 1024 - 1
    meta = Path(str(part) + ".ranges.json")
    meta.write_text(
        json.dumps(
            {
                "version": 2,
                "size": len(data),
                "prefix": 0,
                "ranges": [[0, first_end], [first_end + 1, len(data) - 1]],
                "done": [f"0-{first_end}"],
            }
        )
    )
    _patch_transport(monkeypatch, RangeTransport(data))
    result = await resumable_download(
        "https://cdn.example/file",
        part,
        expected_size=len(data),
        connections=2,
        max_retries=2,
    )
    assert result.read_bytes() == data
    assert not meta.exists()


@pytest.mark.asyncio
async def test_parallel_auth_failure_preserves_resume_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = b"x" * (8 * 1024 * 1024 + 4096)
    part = tmp_path / "auth.part"
    transport = RangeTransport(data, reject=412)
    _patch_transport(monkeypatch, transport)
    refresh_count = 0

    async def refresh_request():
        nonlocal refresh_count
        refresh_count += 1
        return "https://cdn.example/refreshed", {"cookie": f"__puus={refresh_count}"}

    with pytest.raises(RuntimeError):
        await resumable_download(
            "https://cdn.example/file",
            part,
            expected_size=len(data),
            connections=2,
            max_retries=1,
            request_refresh_cb=refresh_request,
        )
    meta = Path(str(part) + ".ranges.json")
    assert meta.exists(), "auth failure must not discard multi-GB resume state"
    assert json.loads(meta.read_text())["version"] == 2
    assert refresh_count >= 1


@pytest.mark.asyncio
async def test_quark_transcode_prefers_api_default_resolution(monkeypatch: pytest.MonkeyPatch):
    source = QuarkSource("base=1")

    async def fake_request(client, method, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "status": 200,
                "code": 0,
                "data": {
                    "default_resolution": "high",
                    "video_list": [
                        {"resolution": "normal", "video_info": {"url": "https://v/normal", "size": 10}},
                        {"resolution": "high", "video_info": {"url": "https://v/high", "size": 20}},
                    ],
                },
            },
        )

    monkeypatch.setattr(source, "_request", fake_request)
    result = await source.prepare_stream(SourceFile(fid="f1", name="movie.mkv", size=100))
    assert result["url"] == "https://v/high"
    assert result["content_type"] == "video/mp4"


@pytest.mark.asyncio
async def test_failed_partial_resolve_file_list_is_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "resolve.db")
    await db.connect()
    job_id = await db.create_job("quark", "https://pan.quark.cn/s/x")
    await db.update_job(job_id, status="saving")
    await db.create_file(job_id, "only-the-first-of-many.bin", size=10)
    worker = Worker(db)

    async def fail_job(_job_id: int):
        raise RuntimeError("database interrupted during file-list insert")

    monkeypatch.setattr(worker, "_run_job", fail_job)
    await worker._run_job_safe(job_id)
    assert await db.list_files(job_id) == []
    job = await db.get_job(job_id)
    assert job["status"] == "failed"
    assert "重新解析" in job["status_detail"]
    await db.close()


@pytest.mark.asyncio
async def test_real_task_cancel_reconciles_inflight_parallel_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = Database(tmp_path / "cancel-progress.db")
    await db.connect()
    job_id = await db.create_job("quark", "https://pan.quark.cn/s/x")
    file_id = await db.create_file(
        job_id, "movie.bin", size=100, source_fid="f1"
    )
    file_row = await db.get_file(file_id)
    worker = Worker(db)
    started = asyncio.Event()

    class FakeSource:
        async def prepare_download(self, source_file, share_meta):
            return "https://video.pds.quark.cn/file"

        def get_download_headers(self):
            return {"cookie": "base=1"}

    class FakeSink:
        async def upload_file(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("cancelled download must never upload")

    async def fake_download(url, dest, **kwargs):
        with open(dest, "wb") as file_obj:
            file_obj.truncate(100)
        Path(str(dest) + ".ranges.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "size": 100,
                    "prefix": 0,
                    "ranges": [[0, 99]],
                    "done": [],
                }
            )
        )
        await kwargs["progress_cb"](75, 100)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.workers.runner.resumable_download", fake_download)
    task = asyncio.create_task(
        worker._process_file(
            FakeSource(),
            FakeSink(),
            job_id,
            file_row,
            "/PanBridge",
            tmp_path / "job",
            {},
        )
    )
    await started.wait()
    before = await db.get_file(file_id)
    assert before["downloaded_bytes"] == 75
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = await db.get_file(file_id)
    assert after["status"] == "queued"
    assert after["downloaded_bytes"] == 0
    await db.close()


@pytest.mark.asyncio
async def test_oversized_staged_final_is_redownloaded_not_uploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = Database(tmp_path / "oversized.db")
    await db.connect()
    job_id = await db.create_job("quark", "https://pan.quark.cn/s/x")
    file_id = await db.create_file(job_id, "movie.bin", size=100, source_fid="f1")
    file_row = await db.get_file(file_id)
    worker = Worker(db)
    job_tmp = tmp_path / "job"
    job_tmp.mkdir()
    final_path = job_tmp / f"{file_id}_movie.bin"
    final_path.write_bytes(b"x" * 101)
    downloaded = False

    class FakeSource:
        async def prepare_download(self, source_file, share_meta):
            return "https://video.pds.quark.cn/file"

        def get_download_headers(self):
            return {"cookie": "base=1"}

    class FakeSink:
        async def upload_file(self, local_path, remote_dir, filename, progress_cb=None):
            assert local_path.read_bytes() == b"y" * 100
            if progress_cb:
                await progress_cb(100, 100)
            return {"fileid": "ok", "size": 100}

    async def fake_download(url, dest, **kwargs):
        nonlocal downloaded
        downloaded = True
        assert not final_path.exists()
        dest.write_bytes(b"y" * 100)
        await kwargs["progress_cb"](100, 100)
        return dest

    monkeypatch.setattr("app.workers.runner.resumable_download", fake_download)
    await worker._process_file(
        FakeSource(), FakeSink(), job_id, file_row, "/PanBridge", job_tmp, {}
    )
    assert downloaded is True
    saved = await db.get_file(file_id)
    assert saved["status"] == "done"
    assert saved["uploaded_bytes"] == 100
    await db.close()
