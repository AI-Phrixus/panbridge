import asyncio
import hashlib
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.config import Settings
from app.db import Database
from app.sources.quark import QuarkAuthenticationError
from app.sinks.google import GooglePermanentUploadError
from app.transfer import pipeline as limiter
from app.transfer.pipeline import PipelineProgress, PipelineResources, StagingCapacityError
from app.workers import runner
from app.workers.runner import Worker


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    await db.connect()
    settings = Settings(data_dir=str(tmp_path / "data"), transfer_staging_bytes=1024**3)
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    jid = await db.create_job("quark", "https://example.test", destination="google", target_account_id="same")

    class Source:
        def __init__(self):
            self.calls = []
        async def prepare_download(self, sf, meta):
            self.calls.append(sf.fid)
            return sf.fid
        def get_download_headers(self):
            return {}

    class FakeGoogleDriveSink:
        account_id = "same"
        def __init__(self):
            self.uploads = []
        async def upload_file(self, path, remote_dir, filename, progress_cb=None):
            data = path.read_bytes()
            self.uploads.append((filename, hashlib.sha256(data).hexdigest()))
            await progress_cb(len(data), len(data))
            return {"id": filename, "size": len(data), "parents": ["private"]}

    source, sink, worker = Source(), FakeGoogleDriveSink(), Worker(db)
    async def load(_):
        return source
    async def pick(*_):
        return "google", sink, "test"
    monkeypatch.setattr(runner, "load_quark_source", load)
    monkeypatch.setattr(worker, "_pick_destination", pick)

    async def add(name, size=16):
        return await db.create_file(jid, name, size=size, source_fid=name)
    async def download(url, path, **kwargs):
        assert kwargs["connections"] == 6
        path.write_bytes(b"x" * kwargs["expected_size"])
        await kwargs["progress_cb"](kwargs["expected_size"], kwargs["expected_size"])
    monkeypatch.setattr(runner, "resumable_download", download)
    yield SimpleNamespace(db=db, jid=jid, source=source, sink=sink, worker=worker,
                          settings=settings, add=add, download=download)
    await worker.stop()
    await db.close()


@pytest.mark.asyncio
async def test_overlap_bounded_to_three_and_exact_delivery(setup, monkeypatch):
    s = setup
    ids = [await s.add(f"f{i}") for i in range(5)]
    upload_started, third_download, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    uploaded, downloads, active_dl, active_ul, maxima = [], [], 0, 0, [0, 0]
    async def download(url, path, **kw):
        nonlocal active_dl
        active_dl += 1; maxima[0] = max(maxima[0], active_dl)
        await s.download(url, path, **kw)
        downloads.append(url)
        if len(downloads) == 3:
            third_download.set()
        active_dl -= 1
    async def upload(path, remote_dir, filename, progress_cb=None):
        nonlocal active_ul
        active_ul += 1; maxima[1] = max(maxima[1], active_ul)
        upload_started.set()
        await release.wait()
        data = path.read_bytes()
        assert data == b"x" * 16
        uploaded.append(filename)
        await progress_cb(16, 16)
        active_ul -= 1
        return {"id": filename, "size": 16, "parents": ["private"]}
    monkeypatch.setattr(runner, "resumable_download", download)
    monkeypatch.setattr(s.sink, "upload_file", upload)
    task = asyncio.create_task(s.worker._run_job(s.jid))
    await asyncio.wait_for(upload_started.wait(), 2)
    await asyncio.wait_for(third_download.wait(), 2)
    await asyncio.sleep(.02)
    assert len(downloads) == 3 and uploaded == []
    job = await s.db.get_job(s.jid)
    assert "流水線" in job["status_detail"] and "上傳 1" in job["status_detail"]
    release.set()
    await asyncio.wait_for(task, 3)
    assert maxima == [1, 1] and len(uploaded) == len(set(uploaded)) == 5
    assert {f["status"] for f in await s.db.list_files(s.jid)} == {"done"}
    assert (await s.db.get_job(s.jid))["status"] == "done"
    assert not list((s.settings.tmp_path / str(s.jid)).iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "deleted", "cancelled"])
async def test_control_drains_upload_download_and_waiters(setup, monkeypatch, status):
    s = setup
    ids = [await s.add(f"f{i}") for i in range(4)]
    uploading, downloading = asyncio.Event(), asyncio.Event()
    async def download(url, path, **kw):
        if url == "f0":
            return await s.download(url, path, **kw)
        path.write_bytes(b"x" * 5)
        await kw["progress_cb"](5, 16)
        downloading.set()
        await asyncio.Event().wait()
    async def upload(*args, **kwargs):
        uploading.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(runner, "resumable_download", download)
    monkeypatch.setattr(s.sink, "upload_file", upload)
    task = asyncio.create_task(s.worker._run_job_safe(s.jid))
    s.worker._job_tasks[s.jid] = task
    await asyncio.wait_for(uploading.wait(), 2)
    await asyncio.wait_for(downloading.wait(), 2)
    await s.db.update_job(s.jid, status=status)
    await asyncio.wait_for(s.worker.interrupt_job(s.jid), 2)
    assert task.done() and (await s.db.get_job(s.jid))["status"] == status
    assert not s.worker._pipeline_resources._held
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("transfer-") and not t.done()]
    first, second = await s.db.get_file(ids[0]), await s.db.get_file(ids[1])
    assert first["status"] == second["status"] == "queued"
    assert first["downloaded_bytes"] == 16 and second["downloaded_bytes"] == 5
    assert first["local_path"] and second["local_path"]
    assert "f3" not in s.source.calls


@pytest.mark.asyncio
async def test_complete_prefetch_resumes_without_redownload(setup):
    s = setup
    fid = await s.add("ready")
    tmp = s.settings.tmp_path / str(s.jid)
    tmp.mkdir()
    (tmp / f"{fid}_ready").write_bytes(b"x" * 16)
    await s.db.update_file(fid, status="uploading", downloaded_bytes=16)
    await s.worker._run_job(s.jid)
    assert s.source.calls == [] and (await s.db.get_file(fid))["status"] == "done"


@pytest.mark.asyncio
async def test_terminal_files_not_replayed_and_failure_preserved(setup):
    s = setup
    for status in ("done", "skipped", "failed"):
        fid = await s.add(status)
        await s.db.update_file(fid, status=status)
    await s.add("queued")
    await s.worker._run_job(s.jid)
    assert s.source.calls == ["queued"]
    assert (await s.db.get_job(s.jid))["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [QuarkAuthenticationError("登入已失效"), GooglePermanentUploadError("space")])
async def test_hard_failure_stops_and_drains_other_stage(setup, monkeypatch, error):
    s = setup
    for i in range(4):
        await s.add(f"f{i}")
    upload_started, was_cancelled = asyncio.Event(), asyncio.Event()
    async def download(url, path, **kw):
        if url == "f0":
            return await s.download(url, path, **kw)
        await upload_started.wait()
        raise error
    async def upload(*args, **kwargs):
        upload_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            was_cancelled.set()
    monkeypatch.setattr(runner, "resumable_download", download)
    monkeypatch.setattr(s.sink, "upload_file", upload)
    await asyncio.wait_for(s.worker._run_job(s.jid), 3)
    assert was_cancelled.is_set() and not s.worker._pipeline_resources._held
    assert (await s.db.get_job(s.jid))["status"] == "failed"
    assert "f2" not in s.source.calls and "f3" not in s.source.calls


@pytest.mark.asyncio
async def test_atomic_progress_cannot_resurrect_controls(setup):
    s = setup
    r = PipelineResources(s.settings.tmp_path, 3, 1000, 512*1024**2)
    p = PipelineProgress(s.db, s.jid, r)
    await s.db.update_job(s.jid, status="paused", status_detail="keep")
    with pytest.raises(asyncio.CancelledError):
        await p.report(1, "downloading", 100, force=True)
    j = await s.db.get_job(s.jid)
    assert j["status"] == "paused" and j["status_detail"] == "keep"


@pytest.mark.asyncio
async def test_aggregate_speeds_and_clear_completed_phase(setup):
    s = setup
    p = PipelineProgress(s.db, s.jid, PipelineResources(s.settings.tmp_path, 3, 1000, 512*1024**2))
    await p.report(1, "downloading", 100, force=True)
    await p.report(2, "uploading", 200, force=True)
    assert (await s.db.get_job(s.jid))["speed_bps"] == 300
    await p.remove(2)
    assert (await s.db.get_job(s.jid))["speed_bps"] == 100
    await p.report(1, "buffered", force=True)
    assert (await s.db.get_job(s.jid))["speed_bps"] == 0


@pytest.mark.asyncio
async def test_disk_reservation_does_not_treat_sparse_size_as_allocated(tmp_path, monkeypatch):
    reserve = 512*1024**2
    monkeypatch.setattr(limiter, "free_bytes", lambda p: reserve + 15000)
    r = PipelineResources(tmp_path, 3, 50000, reserve)
    first = tmp_path / "1.part"
    with first.open("wb") as fp:
        fp.truncate(10000)
    async with r.reserve_file(1, 10000, (first,)):
        entered = asyncio.Event()
        async def next_file():
            async with r.reserve_file(2, 10000, (tmp_path / "2.part",)):
                entered.set()
        task = asyncio.create_task(next_file())
        await asyncio.sleep(.02)
        assert not entered.is_set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not r._held


@pytest.mark.asyncio
async def test_retained_failed_payload_consumes_staging_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(limiter, "free_bytes", lambda p: 10**12)
    r = PipelineResources(tmp_path, 3, 100, 512*1024**2)
    retained = tmp_path / "1_failed"
    async with r.reserve_file(1, 80, (retained,)):
        retained.write_bytes(b"x" * 80)
    with pytest.raises(StagingCapacityError):
        async with r.reserve_file(2, 30, (tmp_path / "2_new",)):
            pytest.fail("failed data must count even after lease release")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 101])
async def test_unknown_and_oversize_are_exclusive(tmp_path, monkeypatch, size):
    monkeypatch.setattr(limiter, "free_bytes", lambda p: 10**12)
    r = PipelineResources(tmp_path, 3, 100, 512*1024**2)
    async with r.reserve_file(1, size, (tmp_path / "1",)):
        entered = asyncio.Event()
        async def next_file():
            async with r.reserve_file(2, 1, (tmp_path / "2",)):
                entered.set()
        task = asyncio.create_task(next_file())
        await asyncio.sleep(.02)
        assert not entered.is_set()
    await asyncio.wait_for(task, 1)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_insufficient_space_fails_without_waiting_on_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(limiter, "free_bytes", lambda p: 512*1024**2 + 5)
    r = PipelineResources(tmp_path, 3, 100, 512*1024**2)
    with pytest.raises(StagingCapacityError):
        async with r.reserve_file(1, 101, (tmp_path / "1",)):
            pytest.fail("oversize must still physically fit")


@pytest.mark.asyncio
async def test_duplicate_file_lease_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(limiter, "free_bytes", lambda p: 10**12)
    r = PipelineResources(tmp_path, 3, 100, 512*1024**2)
    async with r.reserve_file(1, 10, (tmp_path / "1",)):
        with pytest.raises(RuntimeError, match="重複"):
            async with r.reserve_file(1, 10, (tmp_path / "1",)):
                pass


@pytest.mark.asyncio
async def test_one_global_budget_across_job_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(limiter, "free_bytes", lambda p: 10**12)
    r = PipelineResources(tmp_path, 3, 100, 512*1024**2)
    async with r.reserve_file(1, 60, (tmp_path / "job1" / "1",)):
        entered = asyncio.Event()
        async def second_job():
            async with r.reserve_file(2, 60, (tmp_path / "job2" / "2",)):
                entered.set()
        task = asyncio.create_task(second_job())
        await asyncio.sleep(.02)
        assert not entered.is_set()
    await asyncio.wait_for(task, 1)
    assert entered.is_set() and not r._held


@pytest.mark.asyncio
async def test_unknown_download_hits_hard_budget_without_upload(setup, monkeypatch):
    s = setup
    s.settings.transfer_staging_bytes = 10
    fid = await s.add("unknown", size=0)
    async def download(url, path, **kw):
        path.write_bytes(b"x" * 11)
        await kw["progress_cb"](11, 0)
    monkeypatch.setattr(runner, "resumable_download", download)
    await s.worker._run_job(s.jid)
    assert not s.sink.uploads
    assert (await s.db.get_job(s.jid))["status"] == "failed"
    assert (await s.db.get_file(fid))["downloaded_bytes"] == 11


@pytest.mark.asyncio
async def test_partial_resume_bytes_are_not_reported_as_new_speed(setup, monkeypatch):
    s = setup
    fid = await s.add("partial")
    tmp = s.settings.tmp_path / str(s.jid)
    tmp.mkdir()
    (tmp / f"{fid}_partial.part").write_bytes(b"x" * 5)
    seen = []
    async def download(url, path, **kw):
        await kw["progress_cb"](5, 16)
        seen.append((await s.db.get_job(s.jid))["speed_bps"])
        path.write_bytes(b"x" * 16)
        await kw["progress_cb"](16, 16)
    monkeypatch.setattr(runner, "resumable_download", download)
    await s.worker._run_job(s.jid)
    assert seen == [0.0] and (await s.db.get_file(fid))["status"] == "done"


@pytest.mark.asyncio
async def test_unknown_budget_includes_retained_failed_payload(setup, monkeypatch):
    s = setup
    s.settings.transfer_staging_bytes = 100
    failed = await s.add("old_failed", 80)
    await s.db.update_file(failed, status="failed")
    tmp = s.settings.tmp_path / str(s.jid)
    tmp.mkdir()
    (tmp / f"{failed}_old_failed").write_bytes(b"x" * 80)
    await s.add("unknown", 0)
    async def download(url, path, **kw):
        path.write_bytes(b"x" * 21)
        await kw["progress_cb"](21, 0)
    monkeypatch.setattr(runner, "resumable_download", download)
    await s.worker._run_job(s.jid)
    assert not s.sink.uploads and (await s.db.get_job(s.jid))["status"] == "failed"
    assert (tmp / f"{failed}_old_failed").stat().st_size == 80
