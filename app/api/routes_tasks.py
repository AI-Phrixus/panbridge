from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import require_auth
from app.config import get_settings
from app.db import db
from app.security import decrypt_json
from app.sources.link_parse import parse_many, parse_share_link
from app.sinks.pcloud import PCloudSink
from app.transfer.disk import free_bytes

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class CreateTaskIn(BaseModel):
    text: str = Field(..., description="one or more share links")
    passcode: str = ""
    pcloud_path: str = ""
    destination: str = "auto"  # auto | pcloud | local


@router.get("/system/status")
async def system_status(_: None = Depends(require_auth)):
    s = get_settings()
    free = free_bytes(s.tmp_path)
    total = shutil.disk_usage(s.tmp_path).total
    providers = {p["provider"] for p in await db.list_credential_providers()}
    pcloud_space = None
    onedrive_space = None
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
            from app.security import decrypt_json
            from app.sinks.onedrive import OneDriveSink
            from app.auth.onedrive_auth import refresh_access_token
            cred = decrypt_json(enc)
            access = cred.get("access_token") or ""
            if cred.get("refresh_token") and cred.get("client_id"):
                try:
                    tok = await refresh_access_token(cred["client_id"], cred["refresh_token"])
                    access = tok["access_token"]
                    if tok.get("refresh_token"):
                        cred["refresh_token"] = tok["refresh_token"]
                    cred["access_token"] = access
                    from app.security import encrypt_json

                    await db.set_credential("onedrive", encrypt_json(cred))
                except Exception:
                    pass
            if access:
                od = OneDriveSink(access, cred.get("refresh_token") or "", cred.get("client_id") or "")
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
        },
        "pcloud_free_gb": round((pcloud_space or {}).get("free", 0) / 1024 / 1024 / 1024, 2) if pcloud_space else None,
        "pcloud_used_gb": round((pcloud_space or {}).get("used", 0) / 1024 / 1024 / 1024, 2) if pcloud_space else None,
        "pcloud_quota_gb": round((pcloud_space or {}).get("quota", 0) / 1024 / 1024 / 1024, 2) if pcloud_space else None,
        "onedrive_free_gb": round((onedrive_space or {}).get("free", 0) / 1024 / 1024 / 1024, 2) if onedrive_space else None,
        "onedrive_quota_gb": round((onedrive_space or {}).get("quota", 0) / 1024 / 1024 / 1024, 2) if onedrive_space else None,
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
    if not job:
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
            job["files_total"] = sum(counts.values())
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
    if dest not in ("auto", "pcloud", "local", "onedrive"):
        raise HTTPException(400, "destination must be auto|pcloud|local|onedrive")
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
        )
        created.append(jid)
    return {"ok": True, "job_ids": created, "destination": dest}


@router.post("/{job_id}/retry")
async def retry_task(job_id: int, request: Request, _: None = Depends(require_auth)):
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
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
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    await db.update_job(job_id, status="cancelled", status_detail="已取消", speed_bps=0)
    # ADV-R5: mid-flight file rows left as downloading block clean retry semantics
    files = await db.list_files(job_id)
    for f in files:
        if f.get("status") in ("downloading", "uploading"):
            await db.update_file(f["id"], status="queued", error_message="")
    # BUG-11: also cancel in-flight asyncio task (upload/download)
    worker = getattr(request.app.state, "worker", None)
    if worker is not None and hasattr(worker, "request_cancel"):
        worker.request_cancel(job_id)
    return {"ok": True}
