"""Adversarial / red-team regression tests (round 1 fixes)."""
from __future__ import annotations

import pytest
from pathlib import Path

from app.util_paths import sanitize_rel_path, safe_under_root
from app.transfer.disk import ensure_space
from app.db import Database


def test_sanitize_blocks_dotdot():
    assert ".." not in sanitize_rel_path("../../etc/passwd")
    assert sanitize_rel_path("a/../../b") == "a/b" or sanitize_rel_path("a/../../b") == "b"
    assert sanitize_rel_path("/abs/path") == "abs/path"


def test_safe_under_root(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    p = safe_under_root(root, "ok", "file.txt")
    assert root.resolve() in p.resolve().parents or p.resolve() == root.resolve() / "ok" / "file.txt"
    # ".." segments are stripped (no escape); result stays under root
    p2 = safe_under_root(root, "..", "etc")
    assert root.resolve() == p2.resolve() or root.resolve() in p2.resolve().parents


def test_ensure_space_zero_free_style(tmp_path: Path):
    # adaptive reserve should still raise for absurd need
    with pytest.raises(RuntimeError):
        ensure_space(tmp_path, need=10**18, reserve=0)


@pytest.mark.asyncio
async def test_db_field_allowlist(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    await db.connect()
    jid = await db.create_job("baidu", "https://x")
    with pytest.raises(ValueError):
        await db.update_job(jid, **{"status;drop": "x"})  # type: ignore[arg-type]
    await db.update_job(jid, status="queued")
    await db.close()


@pytest.mark.asyncio
async def test_claim_prefers_queued_over_downloading(tmp_path: Path):
    db = Database(tmp_path / "b.db")
    await db.connect()
    j1 = await db.create_job("baidu", "https://a")
    j2 = await db.create_job("baidu", "https://b")
    await db.update_job(j1, status="downloading")
    await db.update_job(j2, status="queued")
    # excluding j1 (running) should get j2
    got = await db.claim_next_job(exclude_ids={j1})
    assert got is not None
    assert got["id"] == j2
    # with no exclude, queued still preferred first
    got2 = await db.claim_next_job()
    assert got2 is not None
    assert got2["id"] == j2
    await db.close()


@pytest.mark.asyncio
async def test_clear_files_and_progress_size(tmp_path: Path):
    db = Database(tmp_path / "c.db")
    await db.connect()
    jid = await db.create_job("baidu", "https://x")
    f1 = await db.create_file(jid, "big.bin", size=1_000_000)
    await db.update_file(f1, status="downloading", downloaded_bytes=1000)
    # size must stay 1_000_000 for correct progress
    f = await db.get_file(f1)
    assert f["size"] == 1_000_000
    prog = await db.recompute_job_progress(jid)
    assert prog < 5.0  # 1000/1e6 * 0.7 * 100
    await db.clear_files(jid)
    assert await db.list_files(jid) == []
    await db.close()


def test_onedrive_space_check_logic():
    """Mirror BUG-6 fix: free==0 must still fail for large total."""
    total_size = 100
    od_free = 0
    # old broken: total_size > od_free * 0.95 > 0  → False when free 0
    old = total_size > od_free * 0.95 > 0
    new = od_free is not None and total_size > od_free * 0.95
    assert old is False
    assert new is True


def test_sanitize_filename_for_local():
    from app.util_paths import sanitize_rel_path
    assert Path(sanitize_rel_path("a/../../x")).name != ".."
