from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.db import Database
from app.security import decrypt_json
from app.auth.onedrive_session import make_onedrive_sink
from app.auth.google_session import make_google_sink, GoogleAuthenticationError
from app.sinks.google import GooglePermanentUploadError
from app.auth.quark_session import load_quark_source
from app.sources.baidu import BaiduSource
from app.sources.base import SourceFile
from app.sources.quark import QuarkAuthenticationError
from app.sinks.pcloud import PCloudSink
from app.sinks.local import LocalSink
from app.sinks.onedrive import (
    OneDrivePermanentUploadError,
    OneDriveSink,
    normalize_onedrive_target,
)
from app.transfer.disk import ensure_space, free_bytes
from app.transfer.downloader import downloaded_bytes_on_disk, resumable_download
from app.transfer.pipeline import PipelineProgress, PipelineResources, StagingCapacityError

log = logging.getLogger("panbridge.worker")


def _error_message(error: Exception) -> str:
    # HTTPX timeouts often stringify to an empty string. Preserve an actionable
    # explanation, but never display transport request URLs or auth headers.
    if isinstance(error, httpx.TimeoutException):
        return f"網路連線逾時（{type(error).__name__}）；進度已保留，可重試"
    if isinstance(error, httpx.TransportError):
        return f"網路連線暫時中斷（{type(error).__name__}）；進度已保留，可重試"
    return str(error).strip() or f"處理失敗（{type(error).__name__}）；進度已保留，可重試"


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps/1024:.1f} KB/s"
    return f"{bps/1024/1024:.2f} MB/s"


def _is_auth_error(error: Exception) -> bool:
    message = str(error).lower()
    return isinstance(error, (QuarkAuthenticationError, GoogleAuthenticationError)) or any(
        marker in message
        for marker in (
            "登入已失效",
            "登录已失效",
            "require login",
            "auth expired",
            "login invalid",
        )
    )


# If a running job has no DB progress update for this long, force-cancel & requeue.
_STALE_JOB_SECONDS = 600.0


class Worker:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._running_jobs: set[int] = set()
        self._job_tasks: dict[int, asyncio.Task] = {}
        self._last_wait_mark: float = 0.0
        self._pipeline_resources: PipelineResources | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="panbridge-worker")

    async def stop(self) -> None:
        self._stop.set()
        # Cancel in-flight job tasks so .part flushes and process can exit cleanly
        for jid, t in list(self._job_tasks.items()):
            if not t.done():
                t.cancel()
        if self._job_tasks:
            await asyncio.gather(*self._job_tasks.values(), return_exceptions=True)
        self._job_tasks.clear()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                self._task.cancel()

    async def _loop(self) -> None:
        settings = get_settings()
        log.info("worker started v%s", settings.app_version)
        while not self._stop.is_set():
            try:
                await self._reap_and_watch_stale()
                await self._mark_waiting_jobs(settings.max_concurrent_jobs)
                # Fill all free slots in one tick (ADV-R5: was only +1 per 1.5s)
                while len(self._running_jobs) < settings.max_concurrent_jobs:
                    job = await self.db.claim_next_job(exclude_ids=set(self._running_jobs))
                    if not job or job["id"] in self._running_jobs:
                        break
                    if job["status"] not in (
                        "queued",
                        "resolving",
                        "saving",
                        "downloading",
                        "uploading",
                    ):
                        break
                    self._running_jobs.add(job["id"])
                    t = asyncio.create_task(
                        self._run_job_safe(job["id"]), name=f"job-{job['id']}"
                    )
                    self._job_tasks[job["id"]] = t
            except Exception:
                log.exception("worker loop error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                pass
        log.info("worker stopped")

    async def _mark_waiting_jobs(self, max_conc: int) -> None:
        """Jobs left as downloading/queued but not running → show queue + live progress.

        After restart/cancel, status_detail stuck on「任務中斷」and progress froze at 0
        while other jobs held concurrent slots (user saw #4 with no progress).
        """
        now = time.time()
        # Throttle: 1452-file recompute every 1.5s was wasteful (ADV-R5)
        if now - self._last_wait_mark < 8.0:
            return
        self._last_wait_mark = now
        try:
            jobs = await self.db.list_jobs(50)
        except Exception:
            return
        running = set(self._running_jobs)
        n_run = len(running)
        full = n_run >= max_conc
        for j in jobs:
            jid = int(j["id"])
            st = j.get("status") or ""
            if st not in ("queued", "downloading", "uploading", "resolving", "saving"):
                continue
            if jid in running:
                continue
            # not actively executing
            try:
                prog = await self.db.recompute_job_progress(jid)
                counts = await self.db.file_status_counts(jid)
                n_done = int(counts.get("done") or 0)
                n_all = sum(counts.values())
                slot = f"併發 {n_run}/{max_conc}"
                if full:
                    slot += " 已滿"
                elif n_run > 0:
                    slot += " · 等待空位"
                else:
                    slot += " · 即將開始"
                if st == "queued" and n_all == 0:
                    detail = f"排隊等待執行（{slot}）"
                elif n_all:
                    detail = f"排隊續傳中（{slot}）· 已完成 {n_done}/{n_all} 檔 · {prog:.2f}%"
                else:
                    detail = f"排隊等待執行（{slot}）"
                old_detail = j.get("status_detail") or ""
                old_prog = float(j.get("progress") or 0)
                if old_detail != detail or abs(old_prog - prog) >= 0.01:
                    # touch=False: do not reset updated_at (claim prefers oldest interrupt)
                    await self.db.update_job(
                        jid,
                        touch=False,
                        progress=prog,
                        speed_bps=0,
                        status_detail=detail,
                    )
            except Exception:
                log.exception("mark waiting job %s failed", jid)

    async def _reap_and_watch_stale(self) -> None:
        """Drop finished task refs; force-cancel jobs with no progress for too long."""
        for jid, t in list(self._job_tasks.items()):
            if t.done():
                self._job_tasks.pop(jid, None)
                self._running_jobs.discard(jid)
        now = time.time()
        for jid in list(self._running_jobs):
            job = await self.db.get_job(jid)
            if not job:
                t = self._job_tasks.pop(jid, None)
                if t and not t.done():
                    t.cancel()
                self._running_jobs.discard(jid)
                continue
            if job["status"] not in ("downloading", "uploading", "resolving", "saving"):
                continue
            updated = job.get("updated_at") or ""
            try:
                # ISO timestamps from db
                from datetime import datetime, timezone

                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = now - ts.timestamp()
            except Exception:
                continue
            if age > _STALE_JOB_SECONDS:
                log.warning(
                    "job %s stale for %.0fs (status=%s), cancelling task for resume",
                    jid,
                    age,
                    job["status"],
                )
                t = self._job_tasks.get(jid)
                if t and not t.done():
                    t.cancel()
                # leave status as-is so claim_next_job will pick it up again after reap

    async def _run_job_safe(self, job_id: int) -> None:
        try:
            await self._run_job(job_id)
        except asyncio.CancelledError:
            log.info("job %s cancelled (shutdown or stale watchdog)", job_id)
            # Keep status downloading/uploading so restart/reclaim can resume from .part
            # ADV-R5: do NOT overwrite user cancel message with「等待自動續傳」
            try:
                j = await self.db.get_job(job_id)
                if j and j.get("status") not in ("cancelled", "paused", "deleted", "awaiting_selection"):
                    await self.db.update_transfer_job(
                        job_id,
                        status_detail="任務中斷，等待自動續傳…",
                        speed_bps=0,
                    )
            except Exception:
                pass
            raise
        except Exception as e:
            log.exception("job %s failed", job_id)
            current = await self.db.get_job(job_id)
            failed_phase = (current or {}).get("status") or ""
            if failed_phase in ("cancelled", "paused", "deleted", "awaiting_selection"):
                return
            if failed_phase in ("resolving", "saving"):
                # A crash/error while inserting a large resolved file list can
                # leave a plausible-looking but incomplete subset. Force the
                # next retry to resolve atomically instead of silently skipping files.
                await self.db.clear_files(job_id)
            update = self.db.update_job if failed_phase in ("resolving", "saving") else self.db.update_transfer_job
            await update(
                job_id,
                status="failed",
                error_message=_error_message(e)[:2000],
                status_detail=(
                    "帳號登入失效 · 請到設定頁重新連接後重試（下載進度已保留）"
                    if _is_auth_error(e)
                    else (
                        "解析未完成 · 已清除不完整檔案清單，重試時會重新解析"
                        if failed_phase in ("resolving", "saving")
                        else ""
                    )
                ),
                speed_bps=0,
            )
        finally:
            self._running_jobs.discard(job_id)
            self._job_tasks.pop(job_id, None)

    async def _load_cred(self, provider: str) -> dict[str, Any]:
        enc = await self.db.get_credential(provider)
        if not enc:
            raise RuntimeError(f"{provider} 未配置，請到設定頁連接帳號")
        return decrypt_json(enc)

    async def _run_job(self, job_id: int) -> None:
        job = await self.db.get_job(job_id)
        if not job or job["status"] in ("cancelled", "paused", "deleted", "awaiting_selection"):
            return

        settings = get_settings()
        destination = (job.get("destination") or "auto").lower()

        source_type = job["source_type"]
        if source_type == "quark":
            source = await load_quark_source(self.db)
        elif source_type == "baidu":
            source = BaiduSource((await self._load_cred("baidu"))["cookie"])
        else:
            raise RuntimeError(f"未知来源: {source_type}")

        files = await self.db.list_files(job_id)
        share_meta: dict[str, Any] = {}

        # BUG-3: crash mid-resolve left partial files + status saving/resolving → re-resolve
        need_resolve = (not files) or (job["status"] in ("resolving", "saving"))
        if need_resolve:
            if files and job["status"] in ("resolving", "saving"):
                log.warning("job %s incomplete resolve state; clearing %s file rows", job_id, len(files))
                await self.db.clear_files(job_id)
            await self.db.update_job(
                job_id, status="resolving", error_message="", status_detail="解析分享連結…"
            )
            # heartbeat so stale watchdog does not kill long resolve (BUG-12)
            async def _hb_resolve() -> None:
                while True:
                    await asyncio.sleep(60)
                    try:
                        await self.db.update_job(job_id, status_detail="解析分享連結中…")
                    except Exception:
                        return

            hb = asyncio.create_task(_hb_resolve())
            try:
                resolved = await source.resolve(job["share_url"], job.get("passcode") or "")
            finally:
                hb.cancel()
                try:
                    await hb
                except asyncio.CancelledError:
                    pass
            title = resolved.title or job.get("title") or ""
            base_path = job.get("pcloud_path") or f"{settings.pcloud_default_path}/{title}"
            await self.db.update_job(
                job_id,
                title=title,
                status="saving",
                pcloud_path=base_path,
                status_detail=f"已解析 {len(resolved.files)} 個檔案",
            )
            for sf in resolved.files:
                await self.db.create_file(
                    job_id,
                    remote_name=sf.name,
                    relative_path=sf.relative_path or sf.name,
                    size=sf.size,
                    source_fid=sf.fid,
                    meta=sf.meta,
                )
            share_meta = resolved.meta
            if job.get("select_files"):
                await self.db.update_job(
                    job_id, status="awaiting_selection", speed_bps=0,
                    status_detail="請選擇要下載的文件，確認後才開始搬運",
                )
                return
            # mark past resolve so we won't wipe files on next resume
            await self.db.update_job(
                job_id, status="downloading", status_detail="準備下載…"
            )
            files = await self.db.list_files(job_id)

        current_job = await self.db.get_job(job_id) or {}
        if current_job.get("select_files"):
            await self.db.update_job(job_id, status="awaiting_selection", speed_bps=0,
                                     status_detail="請選擇要下載的文件，確認後才開始搬運")
            return
        base_path = current_job.get("pcloud_path") or settings.pcloud_default_path
        job_tmp = settings.tmp_path / str(job_id)
        job_tmp.mkdir(parents=True, exist_ok=True)

        total_size = sum(int(f.get("size") or 0) for f in files if f["status"] not in ("done", "skipped"))

        dest, sink, dest_note = await self._pick_destination(destination, total_size, settings)
        if dest == "google":
            bound = current_job.get("target_account_id") or ""
            if bound and bound != sink.account_id:
                raise RuntimeError("此任務綁定另一個 Google 帳號，已停止搬運；請連回原帳號或另建任務")
            await self.db.update_job(job_id, target_account_id=sink.account_id)
        await self.db.update_job(job_id, destination=dest)
        # Don't require free space for ALL remaining files up-front (sequential download).
        # Per-file ensure_space runs in _process_file.
        log.info("job %s destination=%s note=%s", job_id, dest, dest_note)

        file_errors: list[str] = []
        # ADV-R5: stale mid-flight markers from last interrupt → re-queue (keep bytes)
        for f in files:
            if f.get("status") in ("downloading", "uploading"):
                await self.db.update_file(int(f["id"]), status="queued", error_message="")
                f["status"] = "queued"
        serial_files = files
        if dest != "local" and settings.transfer_prefetch_files > 1:
            result = await self._run_file_pipeline(
                source, sink, job_id, files, base_path, job_tmp, share_meta, settings,
            )
            if result is None:
                return
            file_errors.extend(result)
            serial_files = []
        for f in serial_files:
            job = await self.db.get_job(job_id)
            if not job or job["status"] in ("cancelled", "paused", "deleted", "awaiting_selection"):
                return
            if f["status"] in ("done", "skipped"):
                continue
            # Skip files still marked failed unless re-queued via retry API
            if f["status"] == "failed":
                file_errors.append(f"{f.get('remote_name')}: {f.get('error_message') or 'failed'}")
                continue
            try:
                await self._process_file(source, sink, job_id, f, base_path, job_tmp, share_meta)
            except Exception as e:
                jcheck = await self.db.get_job(job_id)
                if (jcheck and jcheck["status"] == "cancelled") or "取消" in str(e):
                    await self.db.update_job(
                        job_id, status="cancelled", status_detail="已取消", speed_bps=0
                    )
                    return
                # Cookie/login hard-fail: abort job so user re-auths once (not 1452 times)
                es = _error_message(e)
                if _is_auth_error(e):
                    log.error("job %s auth hard-fail: %s", job_id, e)
                    await self.db.update_job(
                        job_id,
                        status="failed",
                        error_message=es[:2000],
                        status_detail="帳號登入失效 · 請到設定頁重新連接後重試",
                        speed_bps=0,
                    )
                    return
                if isinstance(e, GooglePermanentUploadError):
                    await self.db.update_job(job_id, status="failed", error_message=str(e),
                        status_detail="Google Drive 拒絕搬運；已停止，請檢查空間或權限後重試", speed_bps=0)
                    return
                # Per-file failure must not abort the whole job (remaining files still process)
                log.exception("job %s file %s failed", job_id, f.get("id"))
                file_errors.append(f"{f.get('remote_name')}: {_error_message(e)}")
            prog = await self.db.recompute_job_progress(job_id)
            snap = await self.db.list_files(job_id)
            snap = [x for x in snap if x["status"] != "skipped"]
            n_done = sum(1 for x in snap if x["status"] == "done")
            await self.db.update_job(
                job_id,
                progress=prog,
                status_detail=f"進度 {n_done}/{len(snap)} 檔案 · {prog:.2f}%",
            )

        files = await self.db.list_files(job_id)
        j2 = await self.db.get_job(job_id)
        if not j2 or j2["status"] in ("cancelled", "paused", "deleted", "awaiting_selection"):
            return
        files = [x for x in files if x["status"] != "skipped"]
        if all(x["status"] == "done" for x in files) and files:
            dest = (j2 or {}).get("destination") or "pcloud"
            if dest == "local":
                detail = "全部完成 · 請到本站「任務詳情」下載（伺服器暫存）"
            elif dest == "onedrive":
                detail = "全部完成 · 請到 OneDrive 自取"
            elif dest == "google":
                detail = "全部完成 · 已交付 Google Drive"
            else:
                detail = "全部完成 · 請到 pCloud 自取"
            await self.db.update_transfer_job(
                job_id,
                status="done",
                progress=100,
                error_message="",
                status_detail=detail,
                speed_bps=0,
            )
        elif any(x["status"] == "failed" for x in files) or file_errors:
            errs = "; ".join(
                (x.get("error_message") or "") for x in files if x["status"] == "failed"
            )
            if not errs and file_errors:
                errs = "; ".join(file_errors)
            n_ok = sum(1 for x in files if x["status"] == "done")
            await self.db.update_transfer_job(
                job_id,
                status="failed",
                error_message=errs[:2000],
                status_detail=f"部分失敗（完成 {n_ok}/{len(files)}）· 可點重試",
                speed_bps=0,
            )
        elif files:
            # BUG-13: never mark done with non-terminal leftovers
            pending = [x for x in files if x["status"] not in ("done", "failed")]
            names = ", ".join((x.get("remote_name") or "?") for x in pending[:5])
            await self.db.update_transfer_job(
                job_id,
                status="failed",
                error_message=f"未完成檔案: {names}"[:2000],
                status_detail=f"異常中止（{len(pending)} 個檔案未完成）· 可點重試",
                speed_bps=0,
            )

    async def _run_file_pipeline(self, source, sink, job_id, files, base_path,
                                 job_tmp, share_meta, settings) -> list[str] | None:
        if self._pipeline_resources is None:
            self._pipeline_resources = PipelineResources(
                settings.tmp_path, settings.transfer_prefetch_files,
                settings.transfer_staging_bytes, settings.disk_reserve_bytes,
            )
        resources = self._pipeline_resources
        progress = PipelineProgress(self.db, job_id, resources)
        errors = [f.get("error_message") or "failed" for f in files if f["status"] == "failed"]
        remaining = iter(f for f in files if f["status"] not in ("done", "skipped", "failed"))
        pending: dict[asyncio.Task, dict] = {}
        exhausted = False

        async def process(f):
            fid = int(f["id"])
            safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in f["remote_name"])
            final = job_tmp / f"{fid}_{safe}"
            try:
                await progress.report(fid, "waiting")
                async with resources.reserve_file(
                    fid, int(f.get("size") or 0), (final, Path(str(final) + ".part")),
                ):
                    await self._process_file(source, sink, job_id, f, base_path,
                                             job_tmp, share_meta, pipeline=progress)
            except Exception as error:
                if _is_auth_error(error) or isinstance(error, (GooglePermanentUploadError, StagingCapacityError)):
                    progress.abort(error)
                raise
            finally:
                await progress.remove(fid)

        try:
            while pending or not exhausted:
                job = await self.db.get_job(job_id)
                if not job or job["status"] in ("cancelled", "paused", "deleted", "awaiting_selection"):
                    return None
                while not exhausted and len(pending) < resources.max_files:
                    f = next(remaining, None)
                    if f is None:
                        exhausted = True
                        break
                    task = asyncio.create_task(process(f), name=f"transfer-{job_id}-{f['id']}")
                    pending[task] = f
                if not pending:
                    break
                completed, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if progress.failure is not None:
                    error = progress.failure
                    detail = ("帳號登入失效 · 請到設定頁重新連接後重試"
                              if _is_auth_error(error) else "搬運已停止；暫存和續傳進度保留，請檢查空間或權限")
                    await self.db.update_transfer_job(job_id, status="failed",
                        error_message=_error_message(error)[:2000], status_detail=detail, speed_bps=0)
                    return None
                for task in completed:
                    f = pending.pop(task)
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        if _is_auth_error(error) or isinstance(error, (GooglePermanentUploadError, StagingCapacityError)):
                            detail = ("帳號登入失效 · 請到設定頁重新連接後重試"
                                      if _is_auth_error(error) else "搬運已停止；暫存和續傳進度保留，請檢查空間或權限")
                            await self.db.update_transfer_job(job_id, status="failed",
                                error_message=_error_message(error)[:2000], status_detail=detail, speed_bps=0)
                            return None
                        log.warning("pipeline file %s failed (%s)", f["id"], type(error).__name__)
                        errors.append(_error_message(error))
            return errors
        finally:
            # Includes children waiting for disk/download/upload admission. A
            # pause/delete/restart cannot finish while any writer is still live.
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def request_cancel(self, job_id: int) -> None:
        """Cancel in-flight asyncio task for job (BUG-11)."""
        t = self._job_tasks.get(job_id)
        if t and not t.done():
            t.cancel()

    async def interrupt_job(self, job_id: int) -> None:
        """Wait for download checkpoint flush before a control action completes."""
        t = self._job_tasks.get(job_id)
        if t and not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        self._running_jobs.discard(job_id)

    async def _make_onedrive_sink(self) -> "OneDriveSink":
        return await make_onedrive_sink(self.db)

    async def _pick_destination(self, destination: str, total_size: int, settings) -> tuple[str, Any, str]:
        """Choose onedrive / pcloud / local."""
        destination = (destination or "auto").lower()
        delivered = settings.data_path / "delivered"
        delivered.mkdir(parents=True, exist_ok=True)

        def local_sink():
            return LocalSink(delivered)

        if destination == "local":
            return "local", local_sink(), "目標: 伺服器暫存（網頁下載）"
        if destination == "google" or (destination == "auto" and await self.db.get_credential("google")):
            sink = await make_google_sink(self.db)
            space = await sink.space_info()
            available = space["free"]
            if available is not None and total_size > available:
                raise RuntimeError("Google Drive 空間不足，請清理空間後重試")
            return "google", sink, "目標: Google Drive（私人文件、不公開分享）"

        pcloud_free = None
        pcloud_sink = None
        try:
            cred = await self._load_cred("pcloud")
            pcloud_sink = PCloudSink(cred["auth"], cred.get("api_host") or settings.pcloud_api_host)
            space = await pcloud_sink.space_info()
            pcloud_free = int(space["free"])
        except Exception as e:
            log.warning("pcloud space check failed: %s", e)
            if destination == "pcloud":
                raise RuntimeError(f"pCloud 不可用: {e}")

        od_sink = None
        od_free = None
        try:
            if await self.db.get_credential("onedrive"):
                od_sink = await self._make_onedrive_sink()
                sp = await od_sink.space_info()
                od_free = int(sp["free"])
        except Exception as e:
            log.warning("onedrive space check failed: %s", e)
            if destination == "onedrive":
                raise RuntimeError(f"OneDrive 不可用: {e}")

        if destination == "local":
            return "local", local_sink(), "目標: 伺服器暫存（網頁下載）"
        if destination == "onedrive":
            if not od_sink:
                raise RuntimeError("OneDrive 未配置，請到設定頁登入")
            # BUG-6: do not chain comparisons (od_free==0 previously bypassed the check)
            if od_free is not None and total_size > od_free * 0.95:
                raise RuntimeError(
                    f"OneDrive 空間不足：任務約 {total_size/1024/1024/1024:.1f} GB，剩餘約 {od_free/1024/1024/1024:.1f} GB"
                )
            return "onedrive", od_sink, f"目標: OneDrive（剩餘約 {(od_free or 0)/1024/1024/1024:.1f} GB）"
        if destination == "pcloud":
            if not pcloud_sink:
                raise RuntimeError("pCloud 未配置")
            if pcloud_free is not None and total_size > pcloud_free * 0.95:
                raise RuntimeError(
                    f"pCloud 空間不足：任務約 {total_size/1024/1024/1024:.1f} GB，剩餘約 {pcloud_free/1024/1024/1024:.1f} GB。"
                    "請改選 OneDrive 或伺服器暫存。"
                )
            return "pcloud", pcloud_sink, f"目標: pCloud（剩餘約 {(pcloud_free or 0)/1024/1024/1024:.1f} GB）"

        # auto: prefer OneDrive when large or pcloud tight
        gb = total_size / 1024 / 1024 / 1024
        if od_sink and total_size > 0 and (pcloud_free is None or total_size > pcloud_free * 0.9):
            if od_free is None or total_size <= od_free * 0.95:
                return "onedrive", od_sink, f"自動: OneDrive（任務 {gb:.1f} GB）"
        if pcloud_sink and pcloud_free is not None and total_size > 0 and total_size <= pcloud_free * 0.9:
            return "pcloud", pcloud_sink, f"自動: pCloud（任務 {gb:.1f} GB）"
        if od_sink:
            return "onedrive", od_sink, "自動: OneDrive"
        if pcloud_sink and total_size == 0:
            return "pcloud", pcloud_sink, "自動: pCloud"
        if pcloud_sink and pcloud_free is not None and total_size <= pcloud_free * 0.9:
            return "pcloud", pcloud_sink, "自動: pCloud"
        return "local", local_sink(), "自動: 伺服器暫存"

    async def _process_file(
        self,
        source: Any,
        sink: Any,
        job_id: int,
        f: dict[str, Any],
        base_path: str,
        job_tmp: Path,
        share_meta: dict[str, Any],
        pipeline: PipelineProgress | None = None,
    ) -> None:
        settings = get_settings()
        file_id = f["id"]
        try:
            meta = json.loads(f.get("meta_json") or "{}")
        except Exception:
            meta = {}

        sf = SourceFile(
            fid=f.get("source_fid") or "",
            name=f["remote_name"],
            size=int(f.get("size") or 0),
            relative_path=f.get("relative_path") or f["remote_name"],
            meta=meta,
        )

        # sanitize filename for local path
        safe_name = "".join(c if c not in '\\/:*?"<>|' else "_" for c in sf.name)
        local_name = f"{file_id}_{safe_name}"
        part_path = job_tmp / (local_name + ".part")
        final_path = job_tmp / local_name

        async def record_resume_state(status: str, error_message: str) -> None:
            checkpoint = final_path if final_path.is_file() and final_path.stat().st_size == sf.size > 0 else part_path
            verified = downloaded_bytes_on_disk(checkpoint, sf.size)
            fields: dict[str, Any] = {
                "status": status,
                "error_message": error_message,
                "downloaded_bytes": verified,
            }
            if checkpoint.exists():
                fields["local_path"] = str(checkpoint)
            await self.db.update_file(file_id, **fields)

        download_owned = upload_owned = False
        download_started = upload_started = 0.0
        try:
            if pipeline:
                await pipeline.resources.download_slot.acquire()
                download_owned = True
                await pipeline.report(file_id, "downloading", force=True)
                download_started = time.monotonic()
            await self.db.update_file(file_id, status="downloading", error_message="")
            if not pipeline:
                await self.db.update_job(job_id, status="downloading", speed_bps=0,
                    status_detail=f"下載中: {sf.relative_path or sf.name}")

            if final_path.exists() and sf.size > 0 and final_path.stat().st_size > sf.size:
                # This directory contains only regenerable transfer staging.
                # An oversized v0.3.x result is not trustworthy and must never
                # be uploaded as if complete.
                final_path.unlink()

            if part_path.exists():
                await self.db.update_file(
                    file_id,
                    downloaded_bytes=downloaded_bytes_on_disk(part_path, sf.size),
                    local_path=str(part_path),
                )
            elif final_path.exists() and final_path.stat().st_size == sf.size > 0:
                part_path = final_path

            need_download = True
            # size==0 alone must NOT skip download (empty placeholder file)
            if final_path.exists() and sf.size > 0 and final_path.stat().st_size == sf.size:
                need_download = False
                part_path = final_path

            if need_download:
                have_on_disk = downloaded_bytes_on_disk(part_path, sf.size)
                need = max(0, (sf.size or 0) - have_on_disk)
                if need:
                    ensure_space(job_tmp, need, settings.disk_reserve_bytes)

                # Always refresh download URL (Baidu dlinks expire quickly)
                url = await source.prepare_download(sf, share_meta)
                await self.db.update_file(file_id, download_url=url)

                # Speed: multi Range in-place (disk-safe). Baidu may 403 → auto fallback single.
                src_name = type(source).__name__.lower()
                if "baidu" in src_name:
                    conns = max(1, min(8, int(getattr(settings, "baidu_download_connections", 4) or 4)))
                else:
                    conns = max(1, min(8, settings.download_connections))

                speed_state = {"t0": time.monotonic(), "b0": have_on_disk, "last": 0.0}
                cancel_flag = {"cancelled": False}

                def _fmt_bytes(n: int) -> str:
                    if n >= 1024**3:
                        return f"{n / 1024**3:.2f} GB"
                    if n >= 1024**2:
                        return f"{n / 1024**2:.1f} MB"
                    if n >= 1024:
                        return f"{n / 1024:.0f} KB"
                    return f"{n} B"

                async def dl_cb(done: int, total: int) -> None:
                    now = time.monotonic()
                    # cancel check every few seconds
                    if now - getattr(dl_cb, "_last_cancel", 0) >= 3.0:
                        dl_cb._last_cancel = now  # type: ignore[attr-defined]
                        j = await self.db.get_job(job_id)
                        if not j or j["status"] in ("cancelled", "paused", "deleted", "awaiting_selection"):
                            cancel_flag["cancelled"] = True
                            raise RuntimeError("任務已取消")
                    dt = max(0.001, now - speed_state["t0"])
                    bps = max(0.0, (done - speed_state["b0"]) / dt)
                    if now - speed_state["t0"] > 3:
                        speed_state["t0"] = now
                        speed_state["b0"] = done
                    speed_state["last"] = bps
                    # BUG-7: never overwrite authoritative size with "bytes so far"
                    tot = total or sf.size or 0
                    if tot > 0 and sf.size > 0 and abs(tot - sf.size) > max(1024, sf.size * 0.01):
                        tot = sf.size  # prefer share metadata size
                    upd: dict[str, Any] = {"downloaded_bytes": done}
                    if tot > 0 and (not sf.size or tot == sf.size):
                        upd["size"] = tot
                    await self.db.update_file(file_id, **upd)
                    if pipeline and not sf.size:
                        # Unknown-length responses are exclusive, with a live
                        # reserve check rather than unlimited disk consumption.
                        if (free_bytes(job_tmp) < settings.download_chunk_size + pipeline.resources.reserve
                                or pipeline.resources.staged_bytes() > pipeline.resources.max_bytes):
                            raise StagingCapacityError("未知大小文件達到暫存或磁碟安全上限；進度已保留")
                    tot_ui = tot or sf.size or done
                    if pipeline:
                        await pipeline.report(file_id, "downloading", bps)
                    elif now - getattr(dl_cb, "_last_job", 0) >= 0.8:
                        dl_cb._last_job = now  # type: ignore[attr-defined]
                        prog = await self.db.recompute_job_progress(job_id)
                        pct = (100.0 * done / tot_ui) if tot_ui else 0
                        await self.db.update_job(
                            job_id,
                            progress=prog,
                            speed_bps=bps,
                            status_detail=(
                                f"下載 {sf.name} · {_fmt_bytes(done)}/{_fmt_bytes(tot_ui)}"
                                f" ({pct:.1f}%) · {_fmt_speed(bps)}"
                            ),
                        )

                async def refresh_url() -> str:
                    u = await source.prepare_download(sf, share_meta)
                    await self.db.update_file(file_id, download_url=u)
                    have = downloaded_bytes_on_disk(part_path, sf.size)
                    if pipeline:
                        await pipeline.report(file_id, "downloading", force=True)
                    else:
                        await self.db.update_job(job_id,
                            status_detail=f"直鏈刷新後續傳 · 已 {_fmt_bytes(have)}: {sf.name}")
                    return u

                async def refresh_request() -> tuple[str, dict[str, str]]:
                    refreshed_url = await refresh_url()
                    # Quark rotates __puus/__pus while issuing the new URL. The
                    # resumed CDN request must use those new cookies, not the
                    # header snapshot from the beginning of a multi-day job.
                    return refreshed_url, source.get_download_headers()

                async def do_dl(u: str) -> None:
                    await resumable_download(
                        u,
                        part_path,
                        headers=source.get_download_headers(),
                        expected_size=sf.size,
                        progress_cb=dl_cb,
                        connections=conns,
                        max_retries=60,
                        url_refresh_cb=refresh_url,
                        request_refresh_cb=refresh_request,
                    )

                last_err: Exception | None = None
                for attempt in range(6):
                    if cancel_flag["cancelled"]:
                        raise RuntimeError("任務已取消")
                    try:
                        if attempt > 0:
                            url = await refresh_url()
                            if pipeline:
                                await pipeline.report(file_id, "downloading", force=True)
                            else:
                                await self.db.update_job(job_id,
                                    status_detail=f"網路中斷，重新取鏈續傳 ({attempt + 1}/6): {sf.name}")
                        await do_dl(url)
                        last_err = None
                        break
                    except RuntimeError as e:
                        last_err = e
                        msg = str(e).lower()
                        if "取消" in str(e) or "cancel" in msg:
                            raise
                        # cookie/login hard fail — do not burn retries
                        if _is_auth_error(e):
                            raise
                        if any(
                            x in msg
                            for x in (
                                "expired",
                                "forbidden",
                                "403",
                                "401",
                                "404",
                                "412",
                                "stalled",
                                "timeout",
                                "retries",
                            )
                        ):
                            await asyncio.sleep(1.5 * attempt + 0.5)
                            continue
                        raise
                    except Exception as e:
                        last_err = e
                        log.warning("download outer retry %s for file %s: %s", attempt + 1, file_id, e)
                        await asyncio.sleep(1.5 * attempt + 0.5)
                        continue
                if last_err:
                    raise last_err

                if part_path != final_path and part_path.exists():
                    part_path.replace(final_path)
                    part_path = final_path

            local_upload = final_path if final_path.exists() else part_path
            size_now = local_upload.stat().st_size
            # refuse to upload truncated payloads when we know the real size
            if sf.size > 0 and size_now != sf.size:
                raise RuntimeError(
                    f"下載不完整，拒絕上傳（大小不符）: {size_now}/{sf.size} bytes ({sf.name})"
                )
            if pipeline:
                log.info("pipeline-stage file=%s phase=download seconds=%.3f bytes=%s",
                         file_id, time.monotonic() - download_started, size_now)
                # Keep the staging lease while waiting for verified delivery.
                # Releasing ONLY the download slot lets the next file prefetch.
                pipeline.resources.download_slot.release()
                download_owned = False
                await self.db.update_file(file_id, status="queued", local_path=str(local_upload),
                    downloaded_bytes=size_now, size=sf.size or size_now)
                await pipeline.report(file_id, "buffered", force=True)
                await pipeline.resources.upload_slot.acquire()
                upload_owned = True
                await pipeline.report(file_id, "uploading", force=True)
                upload_started = time.monotonic()
            await self.db.update_file(
                file_id,
                status="uploading",
                local_path=str(local_upload),
                downloaded_bytes=size_now,
                size=sf.size or size_now,
            )
            if not pipeline:
                await self.db.update_job(job_id, status="uploading", speed_bps=0,
                    status_detail=f"上傳中: {sf.name}")

            from app.util_paths import sanitize_rel_path

            rel = sanitize_rel_path(sf.relative_path or sf.name)
            parent = str(Path(rel).parent).replace("\\", "/")
            remote_dir = base_path if parent in (".", "") else base_path.rstrip("/") + "/" + parent
            filename = Path(rel).name or sf.name
            is_onedrive = isinstance(sink, OneDriveSink)
            if is_onedrive:
                remote_dir, filename = normalize_onedrive_target(
                    remote_dir, filename
                )

            async def ul_cb(done: int, total: int) -> None:
                bps = 0.0
                # BUG-11: honor cancel during upload
                j = await self.db.get_job(job_id)
                if not j or j["status"] in ("cancelled", "paused", "deleted", "awaiting_selection"):
                    raise RuntimeError("任務已取消")
                await self.db.update_file(file_id, uploaded_bytes=done)
                prog = await self.db.recompute_job_progress(job_id) if not pipeline else 0.0
                now = time.monotonic()
                st = getattr(ul_cb, "_st", None)
                if st is None:
                    ul_cb._st = {"t": now, "b": done}  # type: ignore[attr-defined]
                    bps = 0
                else:
                    dt = now - st["t"]
                    if dt >= 1:
                        bps = max(0.0, (done - st["b"]) / dt)
                        ul_cb._st = {"t": now, "b": done}  # type: ignore[attr-defined]
                tot = total or size_now or 1
                pct = 100.0 * done / tot
                if pipeline:
                    await pipeline.report(file_id, "uploading", bps)
                else:
                    await self.db.update_job(job_id, progress=prog, speed_bps=bps,
                        status_detail=f"上傳 {filename} · {pct:.1f}% · {_fmt_speed(bps)}")

            # OneDrive large uploads may need a few full-session retries
            meta_up: dict[str, Any] = {}
            last_up_err: Exception | None = None
            for up_try in range(4):
                try:
                    if up_try > 0:
                        if pipeline:
                            await pipeline.report(file_id, "uploading", force=True)
                        else:
                            await self.db.update_job(job_id,
                                status_detail=f"上傳重試 ({up_try + 1}/4): {filename}")
                        # refresh OneDrive token if applicable
                        if "OneDrive" in type(sink).__name__:
                            try:
                                sink = await self._make_onedrive_sink()
                            except Exception:
                                pass
                    meta_up = await sink.upload_file(
                        local_upload, remote_dir, filename, progress_cb=ul_cb
                    )
                    last_up_err = None
                    break
                except Exception as e:
                    last_up_err = e
                    log.warning("upload attempt %s failed for %s: %s", up_try + 1, filename, e)
                    if isinstance(e, (OneDrivePermanentUploadError, GooglePermanentUploadError)) or _is_auth_error(e):
                        break
                    await asyncio.sleep(2 * up_try + 1)
            if last_up_err:
                raise last_up_err

            remote_dir = str(
                meta_up.get("_panbridge_remote_folder") or remote_dir
            )
            filename = str(meta_up.get("_panbridge_remote_name") or filename)
            final_remote = remote_dir.rstrip("/") + "/" + filename
            is_local = "LocalSink" in type(sink).__name__
            stored = str(meta_up.get("path") or meta_up.get("fileid") or meta_up.get("id") or "")
            # Fix operator-precedence bug: meta size must win even after LocalSink move
            if "size" not in meta_up:
                raise RuntimeError("遠端上傳未返回檔案大小，拒絕標記完成")
            size_final = int(meta_up.get("size") or 0)
            if not stored:
                raise RuntimeError("遠端上傳未返回檔案 ID／路徑，拒絕標記完成")
            if size_final != size_now:
                raise RuntimeError(
                    "遠端檔案大小不符，拒絕標記完成: "
                    f"local={size_now} remote={size_final}"
                )

            delivery_meta = dict(meta)
            if "GoogleDriveSink" in type(sink).__name__:
                delivery_meta["google_delivery"] = {
                    "item_id": stored, "parent_id": (meta_up.get("parents") or [""])[0],
                    "name": filename, "path": final_remote,
                }
            if is_onedrive:
                drive_id = str(
                    (meta_up.get("parentReference") or {}).get("driveId") or ""
                )
                if not drive_id:
                    info_up = await sink.download_info_for_item(stored)
                    drive_id = str(info_up.get("drive_id") or "")
                if not drive_id:
                    raise RuntimeError("OneDrive 上傳完成但無法確認 Drive 身分")
                delivery_meta["onedrive_delivery"] = {
                    "drive_id": drive_id,
                    "item_id": stored,
                    "name": filename,
                    "path": final_remote,
                }

            await self.db.update_file(
                file_id,
                status="done",
                uploaded_bytes=size_final,
                pcloud_fileid=stored,
                pcloud_path=final_remote,
                meta=delivery_meta,
            )
            if pipeline:
                log.info("pipeline-stage file=%s phase=upload seconds=%.3f bytes=%s",
                         file_id, time.monotonic() - upload_started, size_final)
            # cleanup tmp leftovers (LocalSink already moved the main file — do not delete dest)
            try:
                if not is_local and local_upload.exists():
                    local_upload.unlink()
                if part_path.exists() and part_path != local_upload:
                    # after LocalSink move, part may still exist separately
                    if not is_local or part_path.exists():
                        part_path.unlink(missing_ok=True)
                # multi-range meta + legacy segment dirs
                for extra in (
                    Path(str(job_tmp / (local_name + ".part")) + ".ranges.json"),
                    job_tmp / (local_name + ".part.ranges.json"),
                    Path(str(part_path) + ".ranges.json"),
                    Path(str(part_path) + ".segs"),
                    Path(str(part_path) + ".segments.json"),
                ):
                    try:
                        if extra.is_dir():
                            for c in extra.glob("*"):
                                c.unlink(missing_ok=True)
                            extra.rmdir()
                        elif extra.exists():
                            extra.unlink(missing_ok=True)
                    except OSError:
                        pass
            except OSError:
                pass
        except asyncio.CancelledError:
            # Task.cancel() bypasses ``except Exception`` on Python 3.12. Make
            # the verified resume checkpoint durable before propagating cancel.
            reconcile = asyncio.create_task(record_resume_state("queued", ""))
            try:
                await asyncio.shield(reconcile)
            except asyncio.CancelledError:
                await reconcile
            raise
        except Exception as e:
            msg = _error_message(e)
            if pipeline and (_is_auth_error(e) or isinstance(e, (GooglePermanentUploadError, StagingCapacityError))):
                pipeline.abort(e)
            # Parallel slices report in-flight bytes for a responsive UI, but
            # only metadata-marked ranges survive a retry. Reconcile the DB on
            # every abort so a 412/cancel never leaves a misleading percentage.
            if "取消" in msg or "cancel" in msg.lower():
                await record_resume_state("queued", "")
                raise
            await record_resume_state("failed", msg[:1500])
            raise
        finally:
            if pipeline:
                if download_owned:
                    pipeline.resources.download_slot.release()
                if upload_owned:
                    pipeline.resources.upload_slot.release()
