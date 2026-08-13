from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from app.api.deps import require_auth
from app.auth.onedrive_session import make_onedrive_sink
from app.config import get_settings
from app.db import db
from app.security import decrypt_json
from app.sinks.onedrive import OneDriveSink
from app.sinks.pcloud import PCloudSink

router = APIRouter(tags=["location"])


async def _onedrive_sink() -> OneDriveSink:
    try:
        return await make_onedrive_sink(db)
    except RuntimeError as error:
        raise HTTPException(400, str(error)) from error


async def _pcloud_sink() -> PCloudSink:
    enc = await db.get_credential("pcloud")
    if not enc:
        raise HTTPException(400, "pCloud 未连接")
    cred = decrypt_json(enc)
    return PCloudSink(cred["auth"], cred.get("api_host") or "api.pcloud.com")


@router.get("/api/tasks/{job_id}/files/{file_id}/location")
async def file_location(job_id: int, file_id: int, _: None = Depends(require_auth)):
    """Return cloud/web URL for file location (login required)."""
    job = await db.get_job(job_id)
    f = await db.get_file(file_id)
    if not job or not f or f["job_id"] != job_id:
        raise HTTPException(404, "not found")

    dest = (job.get("destination") or "auto").lower()
    path = (f.get("pcloud_path") or "").strip()
    # folder path for "open containing folder"
    folder = str(Path(path).parent).replace("\\", "/") if path else (job.get("pcloud_path") or "/PanBridge")
    if folder in (".", ""):
        folder = job.get("pcloud_path") or "/PanBridge"

    result = {
        "job_id": job_id,
        "file_id": file_id,
        "destination": dest,
        "path": path,
        "folder": folder,
        "url": None,
        "folder_url": None,
        "kind": dest,
        "note": "",
    }

    try:
        if dest == "onedrive":
            od = await _onedrive_sink()
            if path:
                try:
                    result["url"] = await od.web_url_for_path(path)
                except Exception:
                    result["url"] = await od.web_url_for_folder_path(folder)
            result["folder_url"] = await od.web_url_for_folder_path(folder)
            result["note"] = "在 OneDrive 网页中打开；登录微软账号后可删除文件"
        elif dest == "pcloud":
            pc = await _pcloud_sink()
            result["folder_url"] = await pc.web_url_for_path(folder if not path else path)
            result["url"] = result["folder_url"]
            result["note"] = "在 pCloud 网页中打开对应文件夹"
        elif dest == "local":
            settings = get_settings()
            # authenticated local browse page
            result["url"] = f"/browse/local/{job_id}"
            result["folder_url"] = result["url"]
            result["kind"] = "local"
            result["note"] = "服务器暂存文件列表（需登录），可在此删除"
        else:
            # auto/unknown: try onedrive then pcloud
            if await db.get_credential("onedrive") and path:
                try:
                    od = await _onedrive_sink()
                    result["url"] = await od.web_url_for_path(path)
                    result["folder_url"] = await od.web_url_for_folder_path(folder)
                    result["kind"] = "onedrive"
                    result["note"] = "在 OneDrive 中打开"
                except Exception as e:
                    result["note"] = str(e)
            if not result["url"] and await db.get_credential("pcloud"):
                pc = await _pcloud_sink()
                result["folder_url"] = await pc.web_url_for_path(folder if not path else path)
                result["url"] = result["folder_url"]
                result["kind"] = "pcloud"
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"无法定位云端位置: {e}") from e

    if not result.get("url") and not result.get("folder_url"):
        raise HTTPException(404, "文件尚未上传到云端，或路径不可用")

    return result


@router.get("/api/tasks/{job_id}/location")
async def job_folder_location(job_id: int, _: None = Depends(require_auth)):
    """Open job destination folder in cloud."""
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    dest = (job.get("destination") or "auto").lower()
    folder = job.get("pcloud_path") or "/PanBridge"
    if dest == "local":
        return {
            "url": f"/browse/local/{job_id}",
            "kind": "local",
            "folder": folder,
            "note": "服务器暂存（需登录）",
        }
    if dest == "onedrive" or (dest == "auto" and await db.get_credential("onedrive")):
        try:
            od = await _onedrive_sink()
            url = await od.web_url_for_folder_path(folder)
            return {
                "url": url,
                "kind": "onedrive",
                "folder": folder,
                "note": "在 OneDrive 网页打开（需登录微软账号后可删除文件）",
            }
        except Exception as e:
            if dest == "onedrive":
                raise HTTPException(400, f"无法打开 OneDrive 位置: {e}") from e
    if await db.get_credential("pcloud"):
        pc = await _pcloud_sink()
        url = await pc.web_url_for_path(folder)
        return {"url": url, "kind": "pcloud", "folder": folder, "note": "pCloud 文件夹"}
    raise HTTPException(404, "无可用的云端位置")




@router.get("/browse/local/{job_id}")
async def browse_local(job_id: int, _: None = Depends(require_auth)):
    """Simple local delivered file browser with delete (login required)."""
    from fastapi.responses import HTMLResponse

    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    files = await db.list_files(job_id)
    settings = get_settings()
    rows = []
    for f in files:
        if f["status"] != "done":
            continue
        path = None
        if f.get("pcloud_fileid") and str(f["pcloud_fileid"]).startswith("/"):
            path = Path(str(f["pcloud_fileid"]))
        rel = (f.get("pcloud_path") or f.get("relative_path") or "").lstrip("/")
        if not path and rel:
            path = settings.data_path / "delivered" / rel
        exists = path.exists() if path else False
        size = path.stat().st_size if exists else int(f.get("size") or 0)
        from html import escape as _esc

        name = _esc(str(f.get("relative_path") or f.get("remote_name") or ""))
        rows.append(
            f"<tr><td>{int(f['id'])}</td><td>{name}</td>"
            f"<td>{'存在' if exists else '已清理'}</td><td>{int(size)}</td>"
            f"<td>"
            + (
                f"<button onclick=\"delFile({int(job_id)},{int(f['id'])})\">删除本地副本</button>"
                if exists
                else "-"
            )
            + "</td></tr>"
        )
    html = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>本地暂存 · 任务 #{job_id}</title>
<link rel="stylesheet" href="/static/style.css"/></head>
<body><div class="wrap">
<header><div class="logo">本地暂存</div><nav><a href="/tasks/{job_id}">返回任务</a></nav></header>
<div class="card"><h2>任务 #{job_id} 服务器文件</h2>
<p class="muted">仅登录用户可访问。删除只清服务器副本，不影响 OneDrive/pCloud。</p>
<table><thead><tr><th>ID</th><th>路径</th><th>状态</th><th>大小</th><th>操作</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=5 class=muted>无已完成文件</td></tr>'}</tbody></table>
</div></div>
<script>
async function delFile(jobId, fileId){{
  if(!confirm('确定删除服务器上的本地副本？')) return;
  const r = await fetch('/api/tasks/'+jobId+'/files/'+fileId+'/local', {{method:'DELETE'}});
  if(r.ok) location.reload();
  else alert('删除失败');
}}
</script></body></html>"""
    return HTMLResponse(html)


@router.delete("/api/tasks/{job_id}/files/{file_id}/local")
async def delete_local_copy(job_id: int, file_id: int, _: None = Depends(require_auth)):
    job = await db.get_job(job_id)
    f = await db.get_file(file_id)
    if not job or not f or f["job_id"] != job_id:
        raise HTTPException(404, "not found")
    settings = get_settings()
    removed = []
    candidates = []
    if f.get("pcloud_fileid") and str(f["pcloud_fileid"]).startswith("/"):
        candidates.append(Path(str(f["pcloud_fileid"])))
    rel = (f.get("pcloud_path") or f.get("relative_path") or "").lstrip("/")
    if rel:
        candidates.append(settings.data_path / "delivered" / rel)
    for c in candidates:
        if c.exists() and c.is_file():
            c.unlink()
            removed.append(str(c))
    return {"ok": True, "removed": removed}
