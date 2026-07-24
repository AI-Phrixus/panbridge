from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import Database
from app.security import decrypt_json, encrypt_json
from app.sources.baidu import BaiduSource
from app.sources.base import SourceFile
from app.sources.quark import QuarkSource
from app.sinks.pcloud import PCloudSink
from app.sinks.local import LocalSink
from app.sinks.onedrive import OneDriveSink
from app.auth.onedrive_auth import refresh_access_token
from app.transfer.disk import ensure_space
from app.transfer.downloader import resumable_download

log = logging.getLogger("panbridge.worker")


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps/1024:.1f} KB/s"
    return f"{bps/1024/1024:.2f} MB/s"


# If a running job has no DB progress update for this long, force-cancel & requeue.
_STALE_JOB_SECONDS = 600.0


class Worker:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._running_jobs: set[int] = set()
        self._job_tasks: dict[int, asyncio.Task] = {}

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
                active = len(self._running_jobs)
                if active < settings.max_concurrent_jobs:
                    job = await self.db.claim_next_job(exclude_ids=set(self._running_jobs))
                    if job and job["id"] not in self._running_jobs:
                        if job["status"] in (
                            "queued",
                            "resolving",
                            "saving",
                            "downloading",
                            "uploading",
                        ):
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
            try:
                await self.db.update_job(
                    job_id,
                    status_detail="任務中斷，等待自動續傳…",
                    speed_bps=0,
                )
            except Exception:
                pass
            raise
        except Exception as e:
            log.exception("job %s failed", job_id)
            await self.db.update_job(
                job_id,
                status="failed",
                error_message=str(e)[:2000],
                status_detail="",
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
        if not job or job["status"] == "cancelled":
            return

        settings = get_settings()
        destination = (job.get("destination") or "auto").lower()

        source_type = job["source_type"]
        if source_type == "quark":
            source = QuarkSource((await self._load_cred("quark"))["cookie"])
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
            # mark past resolve so we won't wipe files on next resume
            await self.db.update_job(
                job_id, status="downloading", status_detail="準備下載…"
            )
            files = await self.db.list_files(job_id)

        base_path = (await self.db.get_job(job_id) or {}).get("pcloud_path") or settings.pcloud_default_path
        job_tmp = settings.tmp_path / str(job_id)
        job_tmp.mkdir(parents=True, exist_ok=True)

        total_size = sum(int(f.get("size") or 0) for f in files)

        dest, sink, dest_note = await self._pick_destination(destination, total_size, settings)
        await self.db.update_job(job_id, destination=dest)
        # Don't require free space for ALL remaining files up-front (sequential download).
        # Per-file ensure_space runs in _process_file.
        log.info("job %s destination=%s note=%s", job_id, dest, dest_note)

        file_errors: list[str] = []
        for f in files:
            job = await self.db.get_job(job_id)
            if job and job["status"] == "cancelled":
                await self.db.update_job(job_id, status="cancelled", status_detail="已取消", speed_bps=0)
                return
            if f["status"] == "done":
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
                # Per-file failure must not abort the whole job (remaining files still process)
                log.exception("job %s file %s failed", job_id, f.get("id"))
                file_errors.append(f"{f.get('remote_name')}: {e}")
            prog = await self.db.recompute_job_progress(job_id)
            await self.db.update_job(job_id, progress=prog)

        files = await self.db.list_files(job_id)
        j2 = await self.db.get_job(job_id)
        if j2 and j2["status"] == "cancelled":
            return
        if all(x["status"] == "done" for x in files) and files:
            dest = (j2 or {}).get("destination") or "pcloud"
            if dest == "local":
                detail = "全部完成 · 請到本站「任務詳情」下載（伺服器暫存）"
            elif dest == "onedrive":
                detail = "全部完成 · 請到 OneDrive 自取"
            else:
                detail = "全部完成 · 請到 pCloud 自取"
            await self.db.update_job(
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
            await self.db.update_job(
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
            await self.db.update_job(
                job_id,
                status="failed",
                error_message=f"未完成檔案: {names}"[:2000],
                status_detail=f"異常中止（{len(pending)} 個檔案未完成）· 可點重試",
                speed_bps=0,
            )

    def request_cancel(self, job_id: int) -> None:
        """Cancel in-flight asyncio task for job (BUG-11)."""
        t = self._job_tasks.get(job_id)
        if t and not t.done():
            t.cancel()

    async def _make_onedrive_sink(self) -> "OneDriveSink":
        cred = await self._load_cred("onedrive")
        access = cred.get("access_token") or ""
        refresh = cred.get("refresh_token") or ""
        client_id = cred.get("client_id") or ""

        async def _persist(access_t: str, refresh_t: str) -> None:
            cred["access_token"] = access_t
            cred["refresh_token"] = refresh_t
            await self.db.set_credential("onedrive", encrypt_json(cred))

        if refresh and client_id:
            try:
                tok = await refresh_access_token(client_id, refresh)
                access = tok["access_token"]
                refresh = tok.get("refresh_token") or refresh
                await _persist(access, refresh)
            except Exception as e:
                log.warning("onedrive refresh failed: %s", e)
        if not access:
            raise RuntimeError("OneDrive 未登入或 token 失效，請到設定頁重新裝置碼登入")
        return OneDriveSink(access, refresh, client_id, on_tokens=_persist)

    async def _pick_destination(self, destination: str, total_size: int, settings) -> tuple[str, Any, str]:
        """Choose onedrive / pcloud / local."""
        destination = (destination or "auto").lower()
        delivered = settings.data_path / "delivered"
        delivered.mkdir(parents=True, exist_ok=True)

        def local_sink():
            return LocalSink(delivered)

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

        try:
            await self.db.update_file(file_id, status="downloading", error_message="")
            await self.db.update_job(
                job_id,
                status="downloading",
                status_detail=f"下載中: {sf.relative_path or sf.name}",
            )

            if part_path.exists():
                await self.db.update_file(
                    file_id, downloaded_bytes=part_path.stat().st_size, local_path=str(part_path)
                )
            elif final_path.exists() and final_path.stat().st_size >= (sf.size or 0) > 0:
                part_path = final_path

            need_download = True
            # size==0 alone must NOT skip download (empty placeholder file)
            if final_path.exists() and sf.size > 0 and final_path.stat().st_size >= sf.size:
                need_download = False
                part_path = final_path

            if need_download:
                need = max(0, (sf.size or 0) - (part_path.stat().st_size if part_path.exists() else 0))
                if need:
                    ensure_space(job_tmp, need, settings.disk_reserve_bytes)

                # Always refresh download URL (Baidu dlinks expire quickly)
                url = await source.prepare_download(sf, share_meta)
                await self.db.update_file(file_id, download_url=url)

                # Baidu often rejects multi-Range (403); use single stream for baidu
                src_name = type(source).__name__.lower()
                conns = 1 if "baidu" in src_name else settings.download_connections

                speed_state = {"t0": time.monotonic(), "b0": 0, "last": 0.0}
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
                        if j and j["status"] == "cancelled":
                            cancel_flag["cancelled"] = True
                            raise RuntimeError("任務已取消")
                    dt = max(0.001, now - speed_state["t0"])
                    bps = (done - speed_state["b0"]) / dt
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
                    tot_ui = tot or sf.size or done
                    if now - getattr(dl_cb, "_last_job", 0) >= 0.8:
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
                    have = part_path.stat().st_size if part_path.exists() else 0
                    await self.db.update_job(
                        job_id,
                        status_detail=f"直鏈刷新後續傳 · 已 {_fmt_bytes(have)}: {sf.name}",
                    )
                    return u

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
                    )

                last_err: Exception | None = None
                for attempt in range(6):
                    if cancel_flag["cancelled"]:
                        raise RuntimeError("任務已取消")
                    try:
                        if attempt > 0:
                            url = await refresh_url()
                            await self.db.update_job(
                                job_id,
                                status_detail=f"網路中斷，重新取鏈續傳 ({attempt + 1}/6): {sf.name}",
                            )
                        await do_dl(url)
                        last_err = None
                        break
                    except RuntimeError as e:
                        last_err = e
                        msg = str(e).lower()
                        if "取消" in str(e) or "cancel" in msg:
                            raise
                        if any(
                            x in msg
                            for x in (
                                "expired",
                                "forbidden",
                                "403",
                                "401",
                                "404",
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
            if sf.size > 0 and size_now < sf.size:
                raise RuntimeError(
                    f"下載不完整，拒絕上傳: {size_now}/{sf.size} bytes ({sf.name})"
                )
            await self.db.update_file(
                file_id,
                status="uploading",
                local_path=str(local_upload),
                downloaded_bytes=size_now,
                size=sf.size or size_now,
            )
            await self.db.update_job(
                job_id,
                status="uploading",
                status_detail=f"上傳中: {sf.name}",
            )

            from app.util_paths import sanitize_rel_path

            rel = sanitize_rel_path(sf.relative_path or sf.name)
            parent = str(Path(rel).parent).replace("\\", "/")
            remote_dir = base_path if parent in (".", "") else base_path.rstrip("/") + "/" + parent
            filename = Path(rel).name or sf.name

            async def ul_cb(done: int, total: int) -> None:
                bps = 0.0
                # BUG-11: honor cancel during upload
                j = await self.db.get_job(job_id)
                if j and j["status"] == "cancelled":
                    raise RuntimeError("任務已取消")
                await self.db.update_file(file_id, uploaded_bytes=done)
                prog = await self.db.recompute_job_progress(job_id)
                now = time.monotonic()
                st = getattr(ul_cb, "_st", None)
                if st is None:
                    ul_cb._st = {"t": now, "b": done}  # type: ignore[attr-defined]
                    bps = 0
                else:
                    dt = now - st["t"]
                    if dt >= 1:
                        bps = (done - st["b"]) / dt
                        ul_cb._st = {"t": now, "b": done}  # type: ignore[attr-defined]
                tot = total or size_now or 1
                pct = 100.0 * done / tot
                await self.db.update_job(
                    job_id,
                    progress=prog,
                    speed_bps=bps,
                    status_detail=f"上傳 {filename} · {pct:.1f}% · {_fmt_speed(bps)}",
                )

            # OneDrive large uploads may need a few full-session retries
            meta_up: dict[str, Any] = {}
            last_up_err: Exception | None = None
            for up_try in range(4):
                try:
                    if up_try > 0:
                        await self.db.update_job(
                            job_id,
                            status_detail=f"上傳重試 ({up_try + 1}/4): {filename}",
                        )
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
                    await asyncio.sleep(2 * up_try + 1)
            if last_up_err:
                raise last_up_err

            final_remote = remote_dir.rstrip("/") + "/" + filename
            is_local = "LocalSink" in type(sink).__name__
            stored = str(meta_up.get("path") or meta_up.get("fileid") or meta_up.get("id") or "")
            # Fix operator-precedence bug: meta size must win even after LocalSink move
            size_final = int(meta_up.get("size") or 0)
            if size_final <= 0 and local_upload.exists():
                size_final = local_upload.stat().st_size
            if size_final <= 0:
                size_final = size_now

            await self.db.update_file(
                file_id,
                status="done",
                uploaded_bytes=size_final,
                pcloud_fileid=stored,
                pcloud_path=final_remote,
            )
            # cleanup tmp leftovers (LocalSink already moved the main file — do not delete dest)
            try:
                if not is_local and local_upload.exists():
                    local_upload.unlink()
                if part_path.exists() and part_path != local_upload:
                    # after LocalSink move, part may still exist separately
                    if not is_local or part_path.exists():
                        part_path.unlink(missing_ok=True)
                for pattern in (f"{part_path}.segs", f"{part_path}.segments.json"):
                    pth = Path(pattern)
                    if pth.is_dir():
                        for c in pth.glob("*"):
                            c.unlink(missing_ok=True)
                        pth.rmdir()
                    elif pth.exists():
                        pth.unlink(missing_ok=True)
            except OSError:
                pass
        except Exception as e:
            msg = str(e)
            if "取消" in msg or "cancel" in msg.lower():
                await self.db.update_file(file_id, status="queued", error_message="")
                raise
            await self.db.update_file(file_id, status="failed", error_message=msg[:1500])
            raise
