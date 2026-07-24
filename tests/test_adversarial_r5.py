"""Extreme adversarial round (0.3.13): worker queue, SQL progress, cancel integrity."""
from __future__ import annotations

from pathlib import Path

import pytest
from packaging.version import Version

from app.config import Settings
from app.db import Database


@pytest.mark.asyncio
async def test_sql_progress_matches_many_small_done(tmp_path: Path):
    db = Database(tmp_path / "p.db")
    await db.connect()
    jid = await db.create_job("quark", "https://x")
    await db.create_file(jid, "huge.bin", size=80_000_000_000)
    for i in range(50):
        fid = await db.create_file(jid, f"s{i}.doc", size=4000)
        await db.update_file(fid, status="done", downloaded_bytes=4000, uploaded_bytes=4000)
    prog = await db.recompute_job_progress(jid)
    assert prog > 0.0
    assert prog < 20.0
    counts = await db.file_status_counts(jid)
    assert counts.get("done") == 50
    await db.close()


@pytest.mark.asyncio
async def test_update_job_touch_false_keeps_updated_at(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    jid = await db.create_job("baidu", "https://x")
    await db.update_job(jid, status="downloading", status_detail="work")
    row = await db.get_job(jid)
    ts1 = row["updated_at"]
    await db.update_job(jid, touch=False, status_detail="排隊續傳中", progress=1.5)
    row2 = await db.get_job(jid)
    assert row2["updated_at"] == ts1
    assert row2["status_detail"].startswith("排隊")
    assert row2["progress"] == 1.5
    await db.close()


@pytest.mark.asyncio
async def test_claim_oldest_interrupted_first(tmp_path: Path):
    db = Database(tmp_path / "c.db")
    await db.connect()
    a = await db.create_job("quark", "https://a")
    b = await db.create_job("quark", "https://b")
    await db.update_job(a, status="downloading", status_detail="old")
    # b is "newer" interrupt
    await db.update_job(b, status="downloading", status_detail="new")
    # touch=False heartbeat on b must NOT make b look older... a is still older
    await db.update_job(b, touch=False, status_detail="排隊", progress=1.0)
    got = await db.claim_next_job()
    assert got and got["id"] == a
    await db.close()


@pytest.mark.asyncio
async def test_cancel_resets_inflight_files(tmp_path: Path):
    """Simulate cancel API: mid-flight files → queued, keep done."""
    db = Database(tmp_path / "x.db")
    await db.connect()
    jid = await db.create_job("quark", "https://x")
    f1 = await db.create_file(jid, "ok.bin", size=10)
    f2 = await db.create_file(jid, "mid.bin", size=100)
    await db.update_file(f1, status="done", downloaded_bytes=10, uploaded_bytes=10)
    await db.update_file(f2, status="downloading", downloaded_bytes=40)
    await db.update_job(jid, status="cancelled", status_detail="已取消")
    for f in await db.list_files(jid):
        if f["status"] in ("downloading", "uploading"):
            await db.update_file(f["id"], status="queued", error_message="")
    files = await db.list_files(jid)
    assert files[0]["status"] == "done"
    assert files[1]["status"] == "queued"
    assert files[1]["downloaded_bytes"] == 40  # resume bytes kept
    await db.close()


def test_version_0_3_13():
    assert Version(Settings().app_version) >= Version("0.3.13")


def test_mark_waiting_not_always_full():
    src = Path("app/workers/runner.py").read_text()
    assert "已滿" in src
    assert "即將開始" in src or "等待空位" in src
    assert "touch=False" in src
    assert "while len(self._running_jobs)" in src


def test_cancel_does_not_overwrite_user_cancel_msg():
    src = Path("app/workers/runner.py").read_text()
    assert 'j.get("status") != "cancelled"' in src
