"""Size-weighted job progress."""
import asyncio
from pathlib import Path

import pytest

from app.db import Database


@pytest.mark.asyncio
async def test_progress_size_weighted(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    jid = await db.create_job("baidu", "https://pan.baidu.com/s/x", destination="onedrive")
    # 2 tiny done + 1 huge partial
    f1 = await db.create_file(jid, "a.jpg", size=1000)
    f2 = await db.create_file(jid, "b.jpg", size=1000)
    f3 = await db.create_file(jid, "big.mkv", size=1_000_000)
    await db.update_file(f1, status="done", downloaded_bytes=1000, uploaded_bytes=1000)
    await db.update_file(f2, status="done", downloaded_bytes=1000, uploaded_bytes=1000)
    await db.update_file(f3, status="downloading", downloaded_bytes=100_000, uploaded_bytes=0)
    prog = await db.recompute_job_progress(jid)
    # Equal-weight would be ~ (1+1+0.07)/3 * 100 ≈ 69%
    # Size-weight: almost all weight on big file → ~7% download weight * 0.7 ≈ 7%
    assert prog < 15.0, prog
    assert prog > 5.0, prog
    await db.close()


@pytest.mark.asyncio
async def test_progress_all_done(tmp_path: Path):
    db = Database(tmp_path / "t2.db")
    await db.connect()
    jid = await db.create_job("quark", "https://pan.quark.cn/s/x")
    fid = await db.create_file(jid, "a.bin", size=100)
    await db.update_file(fid, status="done", downloaded_bytes=100, uploaded_bytes=100)
    assert await db.recompute_job_progress(jid) == 100.0
    await db.close()
