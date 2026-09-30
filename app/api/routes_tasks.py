from __future__ import annotations

import mimetypes
import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import require_auth
from app.auth.onedrive_session import make_onedrive_sink
from app.auth.google_session import make_google_sink, google_credential
from app.config import get_settings
from app.db import db
from app.security import decrypt_json
from app.sources.link_parse import parse_many, parse_share_link
from app.sinks.pcloud import PCloudSink
from app.transfer.disk import free_bytes

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
_control_locks: dict[int, asyncio.Lock] = {}


def _control_lock(job_id: int) -> asyncio.Lock:
    return _control_locks.setdefault(job_id, asyncio.Lock())


class CreateTaskIn(BaseModel):
    text: str = Field(..., description="one or more share links")
    passcode: str = ""
    pcloud_path: str = ""
    destination: str = "auto"  # auto | pcloud | local
    select_files: bool = False


class SelectionIn(BaseModel):
    file_ids: list[int] = Field(..., min_length=1, max_length=100000)


class CopyTaskIn(BaseModel):
    select_files: bool = True


@router.get("/system/status")
async def system_status(_: None = Depends(require_auth)):
    s = get_settings()
    free = free_bytes(s.tmp_path)
    total = shutil.disk_usage(s.tmp_path).total
    providers = {p["provider"] for p in await db.list_credential_providers()}
    pcloud_space = None
    onedrive_space = None
    google_space = None
    try:
        if "google" in providers:
            google_space = await (await make_google_sink(db)).space_info()
    except Exception:
        pass
    try:
        enc = await db.get_credential("pcloud")
        if enc:
            cred = decrypt_json(enc)
            sink = PCloudSink(cred["auth"], cred.get("api_host") or s.pcloud_api_host)
            pcloud_space = await sink.space_info()
    except Exception:
        pcloud_space = None
    try:
        enc = await db.get_credential("onedrive")
        if enc:
            od = await make_onedrive_sink(db)
            onedrive_space = await od.space_info()
    except Exception:
        onedrive_space = None
    return {
        "version": s.app_version,
        "disk_free": free,
        "disk_total": total,
        "disk_free_gb": round(free / 1024 / 1024 / 1024, 2),
        "download_connections": s.download_connections,
        "max_concurrent_jobs": s.max_concurrent_jobs,
        "providers": {
            "pcloud": "pcloud" in providers,
            "quark": "quark" in providers,
            "baidu": "baidu" in providers,
            "onedrive": "onedrive" in providers,
            "google": "google" in providers,
        },
        "pcloud_free_gb": round((pcloud_space or {}).get("free", 0) / 1024 / 1024 / 1024, 2) if pcloud_space else None,
        "pcloud_used_gb": round((pcloud_space or {}).get("used", 0) / 1024 / 1024 / 1024, 2) if pcloud_space else None,
        "pcloud_quota_gb": round((pcloud_space or {}).get("quota", 0) / 1024 / 1024 / 1024, 2) if pcloud_space else None,
        "onedrive_free_gb": round((onedrive_space or {}).get("free", 0) / 1024 / 1024 / 1024, 2) if onedrive_space else None,
        "onedrive_quota_gb": round((onedrive_space or {}).get("quota", 0) / 1024 / 1024 / 1024, 2) if onedrive_space else None,
        "google_free_gb": round(google_space["free"] / 1024 ** 3, 2) if google_space and google_space["free"] is not None else None,
        "google_email": (google_space or {}).get("email"),
    }


@router.get("")
async def list_tasks(_: None = Depends(require_auth)):
    jobs = await db.list_jobs(200)
    # Refresh progress for active jobs so UI is not stuck at 0 after restart/queue
    out = []
    for j in jobs:
        st = j.get("status") or ""
        if st in ("queued", "downloading", "uploading", "resolving", "saving") and j.get("id"):
            try:
                prog = await db.recompute_job_progress(int(j["id"]))
                j = dict(j)
                j["progress"] = prog
            except Exception:
                pass
        out.append(j)
    return {"jobs": out}


@router.get("/{job_id}")
async def get_task(job_id: int, _: None = Depends(require_auth)):
    job = await db.get_job(job_id)
    if not job or job["status"] == "deleted":
        raise HTTPException(404, "not found")
    files = await db.list_files(job_id)
    st = job.get("status") or ""
    if st in ("queued", "downloading", "uploading", "resolving", "saving"):
        try:
            prog = await db.recompute_job_progress(job_id)
            job = dict(job)
            job["progress"] = prog
            counts = await db.file_status_counts(job_id)
            job["files_done"] = int(counts.get("done") or 0)
            job["files_total"] = sum(value for key, value in counts.items() if key != "skipped")
        except Exception:
            pass
    return {"job": job, "files": files}


@router.get("/{job_id}/files/{file_id}/download")
async def download_local_file(job_id: int, file_id: int, _: None = Depends(require_auth)):
    """Download a finished file stored on VPS (destination=local)."""
    job = await db.get_job(job_id)
    f = await db.get_file(file_id)
    if not job or not f or f["job_id"] != job_id:
        raise HTTPException(404, "not found")
    if f["status"] != "done":
        raise HTTPException(400, "file not ready")

    settings = get_settings()
    # ADV-R1: only serve files under data_path (never arbitrary absolute paths)
    data_root = settings.data_path.resolve()
    delivered = (data_root / "delivered").resolve()

    def _safe_candidate(p: Path) -> Path | None:
        try:
            rp = p.resolve()
        except OSError:
            return None
        if not rp.is_file():
            return None
        if data_root not in rp.parents and rp != data_root:
            return None
        # prefer delivered; still allow tmp leftovers under data_path
        return rp

    candidates: list[Path] = []
    if f.get("pcloud_fileid") and str(f["pcloud_fileid"]).startswith("/"):
        candidates.append(Path(f["pcloud_fileid"]))
    from app.util_paths import sanitize_rel_path

    rel = sanitize_rel_path(
        f.get("pcloud_path") or f.get("relative_path") or f.get("remote_name") or ""
    )
    if rel:
        candidates.append(delivered / rel)
    if f.get("relative_path"):
        base = sanitize_rel_path(job.get("pcloud_path") or settings.pcloud_default_path)
        rel2 = sanitize_rel_path(f["relative_path"])
        if base and rel2:
            candidates.append(delivered / base / rel2)
        elif rel2:
            candidates.append(delivered / rel2)

    path = next((c for c in (_safe_candidate(x) for x in candidates) if c is not None), None)
    if not path:
        raise HTTPException(404, "local file missing — 可能尚未下完或已清理")

    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, filename=path.name, media_type=media)


@router.post("")
async def create_tasks(body: CreateTaskIn, _: None = Depends(require_auth)):
    settings = get_settings()
    dest = (body.destination or "auto").lower()
    if dest not in ("auto", "pcloud", "local", "onedrive", "google"):
        raise HTTPException(400, "destination must be auto|pcloud|local|onedrive|google")
    if dest == "google" and not await db.get_credential("google"):
        raise HTTPException(400, "Google Drive 未連接，請到帳號設定授權")
    if dest == "pcloud" and not await db.get_credential("pcloud"):
        raise HTTPException(400, "pCloud 未配置，請到設定頁連接")
    if dest == "onedrive" and not await db.get_credential("onedrive"):
        raise HTTPException(400, "OneDrive 未配置，請到設定頁登入")
    # auto: need at least one sink or local is always available
    if dest == "auto":
        has_sink = bool(
            await db.get_credential("onedrive")
            or await db.get_credential("pcloud")
        )
        if not has_sink:
            # fall through to local on the worker; still allow create
            pass

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "請貼上至少一個分享連結")
    try:
        parsed_list = parse_many(text)
    except ValueError:
        p = parse_share_link(text)
        if body.passcode:
            p.passcode = body.passcode
        parsed_list = [p]
    if not parsed_list:
        raise HTTPException(400, "未解析到有效連結（僅支援夸克 / 百度）")

    target_account_id = ""
    if dest == "google" or (dest == "auto" and await db.get_credential("google")):
        credential = await google_credential(db)
        target_account_id = credential["account_id"]
        dest = "google"
    created = []
    for p in parsed_list:
        if body.passcode and not p.passcode:
            p.passcode = body.passcode
        if not await db.get_credential(p.source_type):
            raise HTTPException(400, f"{p.source_type} 帳號未配置")
        path = body.pcloud_path or settings.pcloud_default_path
        jid = await db.create_job(
            source_type=p.source_type,
            share_url=p.share_url,
            passcode=p.passcode,
            pcloud_path=path,
            destination=dest,
            select_files=body.select_files,
            target_account_id=target_account_id,
        )
        created.append(jid)
    return {"ok": True, "job_ids": created, "destination": dest}


@router.post("/{job_id}/retry")
async def retry_task(job_id: int, request: Request, _: None = Depends(require_auth)):
    async with _control_lock(job_id):
        return await _retry_task(job_id, request)


async def _retry_task(job_id: int, request: Request):
    job = await db.get_job(job_id)
    if not job or job["status"] == "deleted":
        raise HTTPException(404, "not found")
    if job["status"] in ("paused", "awaiting_selection"):
        raise HTTPException(409, "請使用繼續或確認選檔，不要重試")
    # Don't steal a job that is actively owned by the worker
    worker = getattr(request.app.state, "worker", None)
    running = set(getattr(worker, "_running_jobs", set()) or set()) if worker else set()
    if job_id in running:
        raise HTTPException(400, "任務正在執行中，請先取消再重試")
    # Status may still say downloading while only queued for a free slot — allow retry
    if job["status"] in ("resolving", "saving") and job_id in running:
        raise HTTPException(400, "任務進行中，請先取消再重試，或等待完成")
    files = await db.list_files(job_id)
    for f in files:
        if f["status"] in ("failed", "downloading", "uploading"):
            # re-queue incomplete work; keep done files
            await db.update_file(f["id"], status="queued", error_message="")
    prog = await db.recompute_job_progress(job_id)
    await db.update_job(
        job_id,
        status="queued",
        error_message="",
        progress=prog,
        status_detail="等待重試…",
        speed_bps=0,
    )
    return {"ok": True}


@router.post("/{job_id}/cancel")
async def cancel_task(job_id: int, request: Request, _: None = Depends(require_auth)):
    async with _control_lock(job_id):
        await _stop_task(job_id, request, "cancelled", "已取消；進度已保留")
    return {"ok": True}


async def _stop_task(job_id: int, request: Request, status: str, detail: str):
    job = await db.get_job(job_id)
    if not job or job["status"] == "deleted":
        raise HTTPException(404, "not found")
    if job["status"] == "done" and status != "deleted":
        raise HTTPException(409, "任務已完成")
    await db.update_job(job_id, status=status, speed_bps=0)
    worker = getattr(request.app.state, "worker", None)
    if worker:
        await worker.interrupt_job(job_id)
    # A cancelled resolve can leave only part of the listing; never resume it.
    if job["status"] in ("resolving", "saving"):
        await db.clear_files(job_id)
    else:
        for f in await db.list_files(job_id):
            if f["status"] in ("downloading", "uploading"):
                await db.update_file(f["id"], status="queued", error_message="")
    await db.recompute_job_progress(job_id)
    await db.update_job(job_id, status=status, status_detail=detail, speed_bps=0)


@router.post("/{job_id}/pause")
async def pause_task(job_id: int, request: Request, _: None = Depends(require_auth)):
    async with _control_lock(job_id):
        job = await db.get_job(job_id)
        if job and job["status"] == "awaiting_selection":
            raise HTTPException(409, "任務已停止，正在等待選檔")
        await _stop_task(job_id, request, "paused", "已暫停；繼續時從已驗證進度恢復")
    return {"ok": True}


@router.post("/{job_id}/resume")
async def resume_task(job_id: int, _: None = Depends(require_auth)):
    async with _control_lock(job_id):
        job = await db.get_job(job_id)
        if not job or job["status"] == "deleted":
            raise HTTPException(404, "not found")
        if job["status"] != "paused":
            raise HTTPException(409, "只有暫停的任務可以繼續")
        for f in await db.list_files(job_id):
            if f["status"] == "failed":
                await db.update_file(f["id"], status="queued", error_message="")
        await db.update_job(job_id, status="queued", error_message="", status_detail="等待繼續…")
    return {"ok": True}


@router.post("/{job_id}/selection")
async def select_task_files(job_id: int, body: SelectionIn, _: None = Depends(require_auth)):
    async with _control_lock(job_id):
        job = await db.get_job(job_id)
        if not job or job["status"] == "deleted":
            raise HTTPException(404, "not found")
        if job["status"] != "awaiting_selection":
            raise HTTPException(409, "任務不是等待選檔狀態")
        files = await db.list_files(job_id)
        chosen = set(body.file_ids)
        if not chosen.issubset({f["id"] for f in files}):
            raise HTTPException(400, "選擇包含不屬於此任務的文件")
        for f in files:
            await db.update_file(f["id"], status="queued" if f["id"] in chosen else "skipped")
        await db.recompute_job_progress(job_id)
        await db.update_job(job_id, select_files=0, status="queued", status_detail=f"已選 {len(chosen)} 個文件；等待搬運")
    return {"ok": True, "selected": len(chosen)}


@router.delete("/{job_id}")
async def delete_task(job_id: int, request: Request, _: None = Depends(require_auth)):
    """Soft-delete the task only; delivered cloud files and checkpoints survive."""
    async with _control_lock(job_id):
        await _stop_task(job_id, request, "deleted", "已從任務列表移除；雲端文件未刪除")
    return {"ok": True, "cloud_files_deleted": False}


@router.post("/{job_id}/copy-to-google")
async def copy_task_to_google(job_id: int, body: CopyTaskIn, _: None = Depends(require_auth)):
    async with _control_lock(job_id):
        original = await db.get_job(job_id)
        if not original or original["status"] == "deleted":
            raise HTTPException(404, "not found")
        if not await db.get_credential("google"):
            raise HTTPException(400, "請先連接 Google Drive")
        if not await db.get_credential(original["source_type"]):
            raise HTTPException(400, "來源帳號未連接")
        credential = await google_credential(db)
        copied = await db.create_job(
            source_type=original["source_type"], share_url=original["share_url"],
            passcode=original.get("passcode") or "", title=original.get("title") or "",
            pcloud_path=original.get("pcloud_path") or "/PanBridge",
            destination="google", select_files=body.select_files,
            target_account_id=credential["account_id"],
        )
    return {"ok": True, "job_id": copied, "original_job_id": job_id}
