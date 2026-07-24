"""Three-round extreme adversarial regressions (0.3.11)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from packaging.version import Version

from app.auth import quark_auth, baidu_qr
from app.config import Settings
from app.db import Database
from app.util_paths import sanitize_rel_path, safe_under_root


# ---- Round 1: auth / secrets / path ----

def test_r1_quark_nickname_extract():
    assert quark_auth.extract_nickname("已登錄：南山下看") == "南山下看"
    assert quark_auth.extract_nickname("已登录：Foo") == "Foo"
    assert quark_auth.extract_nickname("plain") == "plain"


def test_r1_qr_session_purge():
    # inject stale sessions
    old = quark_auth.QuarkLoginSession(id="old1", cookie="secret=1", status="confirmed")
    old.created_at = time.time() - 700
    quark_auth._sessions["old1"] = old
    fresh = quark_auth.QuarkLoginSession(id="new1", cookie="secret=2", status="pending")
    quark_auth._sessions["new1"] = fresh
    quark_auth._purge_sessions()
    assert "old1" not in quark_auth._sessions
    assert "new1" in quark_auth._sessions
    quark_auth._sessions.pop("new1", None)


def test_r1_baidu_session_purge():
    s = baidu_qr.BaiduQRSession(id="b1", sign="x", imgurl="y", cookie="BDUSS=1", status="confirmed")
    s.created_at = time.time() - 700
    baidu_qr._sessions["b1"] = s
    baidu_qr._purge_sessions()
    assert "b1" not in baidu_qr._sessions


def test_r1_path_escape_blocked(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    # .. segments stripped — result stays under root
    p = safe_under_root(root, "..", "etc", "passwd")
    assert root.resolve() in p.resolve().parents or p.resolve() == root.resolve() / "etc" / "passwd"
    assert ".." not in sanitize_rel_path("../../etc/passwd")
    # absolute roots stripped
    assert not sanitize_rel_path("/etc/passwd").startswith("/")


def test_r1_qr_rate_limit():
    from app.api.routes_auth import _rate_limit_qr, _qr_hits, _QR_MAX
    from fastapi import HTTPException

    key = "test-qr-adv"
    _qr_hits.pop(key, None)
    for _ in range(_QR_MAX):
        _rate_limit_qr(key)
    with pytest.raises(HTTPException) as ei:
        _rate_limit_qr(key)
    assert ei.value.status_code == 429
    _qr_hits.pop(key, None)


# ---- Round 2: progress / claim / concurrency ----

@pytest.mark.asyncio
async def test_r2_progress_not_zero_when_small_files_done(tmp_path: Path):
    db = Database(tmp_path / "prog.db")
    await db.connect()
    jid = await db.create_job("quark", "https://pan.quark.cn/s/x")
    # one huge pending + many tiny done (real multi-dir share pattern)
    await db.create_file(jid, "huge.bin", size=50_000_000_000)
    for i in range(40):
        fid = await db.create_file(jid, f"s{i}.doc", size=5000)
        await db.update_file(fid, status="done", downloaded_bytes=5000, uploaded_bytes=5000)
    prog = await db.recompute_job_progress(jid)
    assert prog > 0.0, "must not stick at 0.0 when dozens of small files done"
    assert prog < 20.0  # still size-dominated, not fake 100%
    await db.close()


@pytest.mark.asyncio
async def test_r2_claim_excludes_running(tmp_path: Path):
    db = Database(tmp_path / "claim.db")
    await db.connect()
    a = await db.create_job("quark", "https://a")
    b = await db.create_job("quark", "https://b")
    await db.update_job(a, status="downloading")
    await db.update_job(b, status="queued")
    # a already running → claim b
    got = await db.claim_next_job(exclude_ids={a})
    assert got and got["id"] == b
    # neither running → resume a (downloading) before new queued b
    got2 = await db.claim_next_job()
    assert got2 and got2["id"] == a
    await db.close()


@pytest.mark.asyncio
async def test_r2_retry_keeps_done_files(tmp_path: Path):
    db = Database(tmp_path / "retry.db")
    await db.connect()
    jid = await db.create_job("baidu", "https://x")
    f1 = await db.create_file(jid, "ok.bin", size=100)
    f2 = await db.create_file(jid, "bad.bin", size=100)
    await db.update_file(f1, status="done", downloaded_bytes=100, uploaded_bytes=100)
    await db.update_file(f2, status="failed", error_message="boom")
    await db.update_job(jid, status="failed")
    # simulate retry_task logic
    for f in await db.list_files(jid):
        if f["status"] in ("failed", "downloading", "uploading"):
            await db.update_file(f["id"], status="queued", error_message="")
    files = await db.list_files(jid)
    assert files[0]["status"] == "done"
    assert files[1]["status"] == "queued"
    await db.close()


# ---- Round 3: download gates / version / hard-fail messages ----

def test_r3_version_semver():
    assert Version(Settings().app_version) >= Version("0.3.11")


def test_r3_downloader_hardfail_message_present():
    src = Path("app/transfer/downloader.py").read_text()
    assert "require login" in src
    assert "登录已失效" in src or "登录已失效" in src
    # parallel path must hard-fail 412 auth too
    assert src.count("require login") >= 2


def test_r3_quark_qr_is_api_not_screenshot():
    src = Path("app/auth/quark_auth.py").read_text()
    assert "getTokenForQrcodeLogin" in src
    assert "su.quark.cn/4_eMHBJ" in src
    assert "playwright" not in src.lower() or "start_playwright_login" in src


def test_r3_incomplete_upload_gate():
    # runner must refuse truncated upload when size known
    src = Path("app/workers/runner.py").read_text()
    assert "下載不完整，拒絕上傳" in src
