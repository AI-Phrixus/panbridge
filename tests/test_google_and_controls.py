"""Offline Google upload and user-control acceptance regressions."""
import hashlib
import asyncio
import logging
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.api import routes_google as oauth, routes_tasks as tasks
from app.auth import google_session
from app.db import Database
from app.security import encrypt_json, make_session_token
from app.sinks import google
from app.workers.runner import Worker
from app.workers import runner
from app.config import Settings


@pytest_asyncio.fixture
async def store(tmp_path, monkeypatch):
    database = Database(tmp_path / "controls.db")
    await database.connect()
    monkeypatch.setattr(tasks, "db", database)
    monkeypatch.setattr(oauth, "db", database)
    oauth._pending.clear()
    yield database
    oauth._pending.clear()
    await database.close()


def install_transport(monkeypatch, handler):
    real = httpx.AsyncClient
    def factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_copy_google_repeated_requests_reuse_unfinished_job(store, monkeypatch):
    original = await store.create_job('quark', 'https://pan.quark.cn/s/copy', destination='onedrive')
    await store.set_credential('google', 'configured')
    await store.set_credential('quark', 'configured')
    async def credential(database):
        return {'account_id':'same-account'}
    monkeypatch.setattr(tasks, 'google_credential', credential)
    first, second = await asyncio.gather(
        tasks.copy_task_to_google(original, tasks.CopyTaskIn()),
        tasks.copy_task_to_google(original, tasks.CopyTaskIn()),
    )
    assert first['job_id'] == second['job_id']
    assert second['reused'] is True
    assert len(await store.list_jobs()) == 2
    assert (await store.get_job(original))['destination'] == 'onedrive'


async def token(force=False):
    return "fresh" if force else "old"


@pytest.mark.asyncio
async def test_google_resume_probe_and_401_refresh(tmp_path, monkeypatch):
    path = tmp_path / "video.bin"
    path.write_bytes(b"abcdefgh")
    seen, progress = [], []
    def handle(request):
        seen.append(request)
        if request.method == "GET":
            if request.headers["authorization"] == "Bearer old":
                return httpx.Response(401, json={"secret": "not surfaced"})
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            return httpx.Response(200, headers={"location": google.UPLOAD + "?upload_id=private"})
        if request.headers["content-range"] == "bytes */8":
            return httpx.Response(308, headers={"range": "bytes=0-3"})
        assert request.headers["content-range"] == "bytes 4-7/8"
        assert request.content == b"efgh"
        return httpx.Response(200, json={"id": "done", "size": "8"})
    install_transport(monkeypatch, handle)
    async def report(done, total):
        progress.append((done, total))
    result = await google.GoogleDriveSink(token, "account").upload_file(path, "", path.name, report)
    assert result["id"] == "done"
    assert progress == [(4, 8), (8, 8)]
    assert [r.headers["authorization"] for r in seen[:2]] == ["Bearer old", "Bearer fresh"]
    assert not path.with_name(path.name + ".google-upload.enc").exists()


@pytest.mark.asyncio
async def test_google_lost_response_probes_before_resending(tmp_path, monkeypatch):
    path = tmp_path / "file.bin"
    path.write_bytes(b"abcdefgh")
    monkeypatch.setattr(google, "CHUNK_SIZE", 4)
    probes, chunks = 0, []
    async def no_sleep(_):
        pass
    monkeypatch.setattr(google.asyncio, "sleep", no_sleep)
    def handle(request):
        nonlocal probes
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            return httpx.Response(200, headers={"location": google.UPLOAD + "?upload_id=x"})
        if request.headers["content-range"] == "bytes */8":
            probes += 1
            return httpx.Response(308, headers={"range": "bytes=0-3"} if probes > 1 else {})
        chunks.append(request.content)
        if len(chunks) == 1:
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(200, json={"id": "done", "size": "8"})
    install_transport(monkeypatch, handle)
    await google.GoogleDriveSink(token, "account").upload_file(path, "", path.name)
    assert probes == 2 and chunks == [b"abcd", b"efgh"]


@pytest.mark.asyncio
async def test_google_existing_upload_not_duplicated(tmp_path, monkeypatch):
    path = tmp_path / "file.bin"
    path.write_bytes(b"data")
    seen = []
    def handle(request):
        seen.append(request.method)
        return httpx.Response(200, json={"files": [{"id": "already", "size": "4"}]})
    install_transport(monkeypatch, handle)
    result = await google.GoogleDriveSink(token, "account").upload_file(path, "", path.name)
    assert result["id"] == "already" and seen == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [{"id": "wrong", "size": "3"}, {"size": "4"}])
async def test_google_probe_complete_requires_valid_id_and_exact_size(tmp_path, monkeypatch, item):
    path = tmp_path / "file.bin"
    path.write_bytes(b"data")
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            return httpx.Response(200, headers={"location": google.UPLOAD + "?upload_id=x"})
        return httpx.Response(200, json=item)
    install_transport(monkeypatch, handle)
    with pytest.raises(RuntimeError, match="完整性"):
        await google.GoogleDriveSink(token, "account").upload_file(path, "", path.name)
    assert path.with_name(path.name + ".google-upload.enc").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_google_expired_session_clears_checkpoint_keeps_file(tmp_path, monkeypatch, status):
    path = tmp_path / "file.bin"
    path.write_bytes(b"data")
    key = hashlib.sha256(("account\0\0" + path.name).encode()).hexdigest()
    checkpoint = path.with_name(path.name + ".google-upload.enc")
    checkpoint.write_text(encrypt_json({"key": key, "size": 4, "session": google.UPLOAD + "?upload_id=expired"}))
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        assert request.method == "PUT"
        return httpx.Response(status)
    install_transport(monkeypatch, handle)
    with pytest.raises(RuntimeError, match="過期"):
        await google.GoogleDriveSink(token, "account").upload_file(path, "", path.name)
    assert path.read_bytes() == b"data" and not checkpoint.exists()


@pytest.mark.parametrize("url", ["http://www.googleapis.com/upload/drive/v3/files", "https://evil.test/upload/drive/v3/files", "https://www.googleapis.com@evil.test/upload/drive/v3/files", "https://www.googleapis.com/upload/drive/v3/files#x", "https://www.googleapis.com:444/upload/drive/v3/files"])
def test_google_session_url_rejects_credential_exfiltration(url):
    with pytest.raises(RuntimeError, match="不安全"):
        google._session_url(url)


@pytest.mark.asyncio
async def test_controls_pause_resume_preserve_done_and_partial(store):
    jid = await store.create_job("quark", "https://example.test")
    done = await store.create_file(jid, "done", size=10)
    partial = await store.create_file(jid, "partial", size=100)
    await store.update_file(done, status="done", downloaded_bytes=10, uploaded_bytes=10)
    await store.update_file(partial, status="downloading", downloaded_bytes=40)
    await store.update_job(jid, status="downloading")
    calls = []
    async def interrupt(job_id):
        calls.append(job_id)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(worker=SimpleNamespace(interrupt_job=interrupt))))
    await tasks.pause_task(jid, request)
    assert calls == [jid] and (await store.get_job(jid))["status"] == "paused"
    assert (await store.get_file(partial))["downloaded_bytes"] == 40
    assert (await store.get_file(done))["status"] == "done"
    assert await store.claim_next_job() is None
    await Worker(store)._run_job(jid)
    assert (await store.get_job(jid))["status"] == "paused"
    await tasks.resume_task(jid)
    assert (await store.get_job(jid))["status"] == "queued"


@pytest.mark.asyncio
async def test_selection_skips_unselected_progress_and_delete_keeps_records(store):
    jid = await store.create_job("quark", "https://example.test", select_files=True)
    wanted = await store.create_file(jid, "wanted", size=10)
    unwanted = await store.create_file(jid, "unwanted", size=1000)
    await store.update_job(jid, status="awaiting_selection")
    with pytest.raises(HTTPException) as error:
        await tasks.select_task_files(jid, tasks.SelectionIn(file_ids=[wanted, 999999]))
    assert error.value.status_code == 400
    await tasks.select_task_files(jid, tasks.SelectionIn(file_ids=[wanted]))
    assert (await store.get_file(unwanted))["status"] == "skipped"
    await store.update_file(wanted, status="done", downloaded_bytes=10, uploaded_bytes=10)
    assert await store.recompute_job_progress(jid) == 100
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(worker=None)))
    result = await tasks.delete_task(jid, request)
    assert not result["cloud_files_deleted"]
    assert await store.list_jobs() == [] and len(await store.list_files(jid)) == 2
    with pytest.raises(HTTPException) as error:
        await tasks.get_task(jid)
    assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["expired", "other_browser", "unauthenticated"])
async def test_google_oauth_callback_bound_and_expiring(store, monkeypatch, mode):
    app = FastAPI()
    app.include_router(oauth.router)
    session = make_session_token()
    oauth._pending["state"] = {"expires": time.time() - 1 if mode == "expired" else time.time() + 600,
        "browser": hashlib.sha256(session.encode()).hexdigest()}
    async def forbidden(_):
        pytest.fail("invalid authorization must never exchange tokens")
    monkeypatch.setattr(oauth, "token_exchange", forbidden)
    cookie = make_session_token() if mode == "other_browser" else session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get("/api/auth/google/callback?state=state&code=secretcode",
            headers={"Cookie": "panbridge_session=" + cookie} if mode != "unauthenticated" else {})
    assert response.status_code == (401 if mode == "unauthenticated" else 400)


@pytest.mark.asyncio
async def test_google_token_error_omits_secret_body(monkeypatch, caplog):
    secret = "SHOULD_NOT_LEAK_REFRESH_TOKEN"
    install_transport(monkeypatch, lambda request: httpx.Response(400, json={"error": secret}))
    with pytest.raises(RuntimeError) as error:
        await google_session.token_exchange({"refresh_token": secret})
    assert secret not in str(error.value) and secret not in caplog.text


@pytest.mark.asyncio
async def test_google_sink_binding_blocks_account_switch(store):
    await store.set_credential("google", encrypt_json({"access_token": "old-access",
        "refresh_token": "old-refresh", "session_id": "old", "account_id": "old-account", "expires_at": time.time() + 3600}))
    sink = await google_session.make_google_sink(store)
    await store.set_credential("google", encrypt_json({"access_token": "new-access",
        "refresh_token": "new-refresh", "session_id": "new", "account_id": "new-account", "expires_at": time.time() + 3600}))
    with pytest.raises(RuntimeError, match="帳號已更換"):
        await sink.access()


@pytest.mark.asyncio
async def test_google_oauth_pkce_minimal_scope_and_one_use(store, monkeypatch):
    await store.set_credential("google_oauth", encrypt_json({"client_id": "example.apps.googleusercontent.com", "client_secret": "PRIVATE_SECRET"}))
    monkeypatch.setattr(oauth, "get_settings", lambda: SimpleNamespace(public_base_url="https://panbridge.example"))
    app = FastAPI()
    app.include_router(oauth.router)
    session = make_session_token()
    exchanged, saved = [], []
    async def exchange(data):
        exchanged.append(data)
        return {"access_token": "PRIVATE_ACCESS"}
    async def save(db, token, config):
        saved.append(token)
    monkeypatch.setattr(oauth, "token_exchange", exchange)
    monkeypatch.setattr(oauth, "save_google_credential", save)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test", headers={"Cookie": "panbridge_session=" + session}) as client:
        start = await client.post("/api/auth/google/start")
        assert start.status_code == 200 and "PRIVATE_SECRET" not in start.text
        params = parse_qs(urlsplit(start.json()["url"]).query)
        assert params["scope"] == [google_session.DRIVE_SCOPE]
        assert params["code_challenge_method"] == ["S256"]
        assert params["access_type"] == ["offline"]
        callback = "/api/auth/google/callback?state=" + params["state"][0] + "&code=PRIVATE_CODE"
        result = await client.get(callback)
        replay = await client.get(callback)
    assert result.status_code == 303 and replay.status_code == 400
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["referrer-policy"] == "no-referrer"
    assert len(exchanged) == len(saved) == 1
    verifier = exchanged[0]["code_verifier"]
    import base64
    assert params["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")]


def test_google_callback_access_log_redacts_code_and_state():
    from app.main import _OAuthLogRedaction
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("local", "GET", "/api/auth/google/callback?code=PRIVATE_CODE&state=PRIVATE_STATE", "1.1", 303), None)
    assert _OAuthLogRedaction().filter(record)
    assert "PRIVATE_CODE" not in record.getMessage() and "PRIVATE_STATE" not in record.getMessage()


@pytest.mark.asyncio
async def test_waiting_selection_cancel_retry_cannot_download_without_selection(store, monkeypatch):
    jid = await store.create_job("quark", "https://example.test", select_files=True)
    await store.create_file(jid, "listed", size=10)
    await store.update_job(jid, status="awaiting_selection")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(worker=None)))
    await tasks.cancel_task(jid, request)
    await tasks.retry_task(jid, request)
    async def source(db):
        return SimpleNamespace()
    monkeypatch.setattr(runner, "load_quark_source", source)
    worker = Worker(store)
    async def never_pick(*args):
        pytest.fail("selection gate must block picking a sink and downloading")
    monkeypatch.setattr(worker, "_pick_destination", never_pick)
    await worker._run_job(jid)
    assert (await store.get_job(jid))["status"] == "awaiting_selection"
    assert len(await store.list_files(jid)) == 1


@pytest.mark.asyncio
async def test_google_job_account_mismatch_blocks_file_processing(store, monkeypatch, tmp_path):
    jid = await store.create_job("quark", "https://example.test", destination="google", target_account_id="original")
    await store.create_file(jid, "listed", size=10)
    async def source(db):
        return SimpleNamespace()
    monkeypatch.setattr(runner, "load_quark_source", source)
    monkeypatch.setattr(runner, "get_settings", lambda: Settings(data_dir=str(tmp_path)))
    worker = Worker(store)
    async def pick(*args):
        return "google", SimpleNamespace(account_id="replacement"), "test"
    async def forbidden(*args):
        pytest.fail("must not download/upload to a substituted account")
    monkeypatch.setattr(worker, "_pick_destination", pick)
    monkeypatch.setattr(worker, "_process_file", forbidden)
    await worker._run_job_safe(jid)
    job = await store.get_job(jid)
    assert job["status"] == "failed" and "另一個 Google 帳號" in job["error_message"]
    assert job["target_account_id"] == "original"


@pytest.mark.asyncio
async def test_google_parallel_sinks_create_shared_folder_only_once():
    creates = 0
    exists = False
    async def handle(request):
        nonlocal creates, exists
        if request.method == "GET":
            # Force a scheduling opportunity while holding the shared lock.
            await asyncio.sleep(0)
            return httpx.Response(200, json={"files": [{"id": "folder"}] if exists else []})
        creates += 1
        await asyncio.sleep(0)
        exists = True
        return httpx.Response(200, json={"id": "folder"})
    first = google.GoogleDriveSink(token, "session-one", "same-account")
    second = google.GoogleDriveSink(token, "session-two", "same-account")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await asyncio.gather(first.ensure_folder(client, "/PanBridge"), second.ensure_folder(client, "/PanBridge"))
    assert result == ["folder", "folder"] and creates == 1


def test_google_copy_confirmation_does_not_block_browser_dialogs():
    from pathlib import Path
    template = (Path(__file__).resolve().parents[1] / "web/templates/task.html").read_text()
    copy_handler = template.split("async function copyToGoogle(){", 1)[1].split("window.retryJob", 1)[0]
    assert "confirm(" not in copy_handler and "alert(" not in copy_handler
    assert 'id="google-copy-confirm" hidden' in template
    assert 'id="google-copy-submit" onclick="copyToGoogle()"' in template
    assert "select_files:true" in copy_handler
    assert "if(copyingToGoogle)return" in copy_handler
    assert "disabled=true" in copy_handler and "disabled=false" in copy_handler
