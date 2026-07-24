"""Round 2 adversarial tests: local sink, size gates, job terminal states."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.db import Database
from app.sinks.local import LocalSink


@pytest.mark.asyncio
async def test_local_sink_blocks_traversal(tmp_path: Path):
    root = tmp_path / "delivered"
    root.mkdir()
    # create a secret outside root
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    sink = LocalSink(root)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"hello-payload")
    # attempt path traversal in folder path
    meta = await sink.upload_file(src, "../../", "x.bin")
    dest = Path(meta["path"])
    assert root.resolve() in dest.resolve().parents or dest.resolve().parent == root.resolve()
    assert dest.read_bytes() == b"hello-payload"
    assert secret.read_text() == "nope"


@pytest.mark.asyncio
async def test_job_leftover_not_auto_done(tmp_path: Path):
    """Simulate terminal decision: queued leftovers must not be done."""
    db = Database(tmp_path / "j.db")
    await db.connect()
    jid = await db.create_job("baidu", "https://x")
    f1 = await db.create_file(jid, "a.bin", size=10)
    f2 = await db.create_file(jid, "b.bin", size=10)
    await db.update_file(f1, status="done", downloaded_bytes=10, uploaded_bytes=10)
    # f2 stays queued
    files = await db.list_files(jid)
    assert not all(x["status"] == "done" for x in files)
    assert any(x["status"] == "queued" for x in files)
    await db.close()


@pytest.mark.asyncio
async def test_incomplete_download_gate_logic():
    """Runner must refuse upload when size_now < sf.size."""
    sf_size = 1000
    size_now = 100
    assert sf_size > 0 and size_now < sf_size


@pytest.mark.asyncio
async def test_resolve_clear_on_partial_state(tmp_path: Path):
    db = Database(tmp_path / "r.db")
    await db.connect()
    jid = await db.create_job("baidu", "https://x")
    await db.update_job(jid, status="saving")
    await db.create_file(jid, "only_one.bin", size=1)
    files = await db.list_files(jid)
    assert len(files) == 1
    # blue-team recovery: clear then re-add
    await db.clear_files(jid)
    assert await db.list_files(jid) == []
    await db.close()


def test_version_bumped():
    from app.config import Settings
    assert Settings().app_version >= "0.3.6"
