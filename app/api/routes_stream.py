from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from app.api.deps import require_auth
from app.config import get_settings
from app.db import db
from app.stream.resolve import resolve_stream

router = APIRouter(tags=["stream"])


def _public_base(request: Request) -> str:
    host = request.headers.get("host") or f"127.0.0.1:{get_settings().port}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    return f"{scheme}://{host}"


@router.get("/api/tasks/{job_id}/files/{file_id}/stream")
async def stream_file(
    job_id: int,
    file_id: int,
    request: Request,
    _: None = Depends(require_auth),
):
    """Proxy/stream file for players. Supports HTTP Range."""
    try:
        src = await resolve_stream(db, job_id, file_id)
    except FileNotFoundError:
        raise HTTPException(404, "not found")
    except Exception as e:
        raise HTTPException(400, str(e)) from e

    range_header = request.headers.get("range") or request.headers.get("Range")

    # ---- Local file ----
    if src.kind == "local" and src.local_path:
        path = src.local_path
        file_size = path.stat().st_size
        media = src.content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not range_header:
            return FileResponse(
                path,
                media_type=media,
                filename=src.filename,
                headers={"Accept-Ranges": "bytes", "Content-Disposition": f'inline; filename="{src.filename}"'},
            )

        units, _, rng = range_header.partition("=")
        if units.strip() != "bytes":
            raise HTTPException(416, "unsupported range")
        start_s, _, end_s = rng.partition("-")
        try:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
        except ValueError:
            raise HTTPException(416, "invalid range")
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            raise HTTPException(416, "invalid range")
        length = end - start + 1

        def iterfile():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iterfile(),
            status_code=206,
            media_type=media,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Disposition": f'inline; filename="{src.filename}"',
            },
        )

    # ---- Remote proxy ----
    if not src.url:
        raise HTTPException(400, "no remote url")

    up_headers = dict(src.headers or {})
    if range_header:
        up_headers["Range"] = range_header

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=30.0),
        follow_redirects=True,
    )
    req = client.build_request("GET", src.url, headers=up_headers)
    resp = await client.send(req, stream=True)

    if resp.status_code >= 400 and resp.status_code != 206:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        raise HTTPException(502, f"源站拒绝播放流: HTTP {resp.status_code} {body[:160]!r}")

    media = src.content_type or resp.headers.get("content-type") or "application/octet-stream"
    out_headers = {
        "Accept-Ranges": resp.headers.get("accept-ranges") or "bytes",
        "Content-Disposition": f'inline; filename="{src.filename}"',
        "Cache-Control": "no-store",
    }
    if resp.headers.get("content-length"):
        out_headers["Content-Length"] = resp.headers["content-length"]
    if resp.headers.get("content-range"):
        out_headers["Content-Range"] = resp.headers["content-range"]

    async def gen():
        try:
            async for chunk in resp.aiter_bytes(1024 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=resp.status_code, media_type=media, headers=out_headers)


@router.get("/play/{job_id}/{file_id}", response_class=HTMLResponse)
async def play_page(job_id: int, file_id: int, request: Request, _: None = Depends(require_auth)):
    job = await db.get_job(job_id)
    f = await db.get_file(file_id)
    if not job or not f or f["job_id"] != job_id:
        raise HTTPException(404, "not found")

    name = f.get("remote_name") or "video"
    size = int(f.get("size") or 0)
    stream_path = f"/api/tasks/{job_id}/files/{file_id}/stream"
    base = _public_base(request)
    stream_url = base + stream_path
    ext = Path(name).suffix.lower()
    web_playable = ext in {".mp4", ".webm", ".mov", ".m4v"}

    encoded = quote(stream_url, safe="")
    vlc_web = "vlc://" + stream_url
    android_vlc = (
        "intent:" + stream_url +
        "#Intent;action=android.intent.action.VIEW;type=video/*;package=org.videolan.vlc;end"
    )
    ios_vlc = f"vlc-x-callback://x-callback-url/stream?url={encoded}"
    iina = f"iina://weblink?url={encoded}"
    pot = "potplayer://" + stream_url

    size_gb = size / 1024 / 1024 / 1024 if size else 0
    safe_name = (
        str(name)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    if web_playable:
        video_tag = f'''
        <video id="v" controls playsinline preload="metadata"
          style="width:100%;max-height:70vh;background:#000;border-radius:12px">
          <source src="{stream_path}">
        </video>'''
    else:
        video_tag = f'''
        <div class="card" style="background:#111;color:#ccc;text-align:center;padding:28px">
          <p style="font-size:1.1rem;margin:0 0 8px">格式 {ext or "未知"} 网页通常无法完整体验</p>
          <p class="muted" style="margin:0">请用 VLC / IINA / PotPlayer / nPlayer 打开串流地址（MKV、杜比更完整）</p>
        </div>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>播放 · {safe_name}</title>
  <link rel="stylesheet" href="/static/style.css" />
  <style>
    .play-actions {{ display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; }}
    .play-actions a, .play-actions button {{
      display:inline-flex; align-items:center; justify-content:center;
      padding:10px 14px; border-radius:8px; border:1px solid var(--border);
      background:var(--panel); color:var(--text); text-decoration:none; font-weight:600; cursor:pointer;
      font-size:0.9rem;
    }}
    .play-actions a.primary {{ background:var(--accent); border-color:transparent; color:#fff; }}
    .urlbox {{ word-break:break-all; font-family:ui-monospace,monospace; font-size:.8rem;
      background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:10px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="logo">Pan<span>Bridge</span> 播放</div>
      <nav>
        <a href="/tasks/{job_id}">← 返回任务</a>
        <a href="/">任务列表</a>
      </nav>
    </header>

    <div class="card">
      <h2 style="margin-top:0">{safe_name}</h2>
      <p class="muted">约 {size_gb:.2f} GB · 无需完整搬到本机 · 经服务器鉴权串流（源站→服务器→你的播放器）</p>
      {video_tag}
    </div>

    <div class="card">
      <h3>用专业播放器打开（推荐）</h3>
      <p class="muted">若按钮无反应：复制串流地址 → 打开播放器 →「媒体/打开网络串流」粘贴播放。</p>
      <div class="play-actions">
        <a class="primary" href="{vlc_web}">VLC 打开</a>
        <a href="{iina}">IINA (Mac)</a>
        <a href="{pot}">PotPlayer (Win)</a>
        <a href="{ios_vlc}">VLC (iPhone/iPad)</a>
        <a href="{android_vlc}">VLC (Android)</a>
        <button type="button" id="copyBtn">复制串流地址</button>
        <a href="{stream_path}">新标签打开串流</a>
      </div>
      <div class="urlbox" id="streamUrl">{stream_url}</div>
      <p id="copyMsg" class="muted"></p>
    </div>

    <div class="card">
      <h3>跨平台说明</h3>
      <ul class="muted">
        <li><b>Windows</b>：装 <a href="https://www.videolan.org/vlc/" target="_blank">VLC</a> 或 PotPlayer，点对应按钮或粘贴串流地址。</li>
        <li><b>Mac</b>：VLC 或 <a href="https://iina.io/" target="_blank">IINA</a> 体验最好。</li>
        <li><b>iPhone / iPad</b>：App Store 装 VLC，点「VLC (iPhone/iPad)」；或复制地址到 Infuse / nPlayer。</li>
        <li><b>Android</b>：装 VLC 后点「VLC (Android)」。</li>
        <li><b>MKV / 杜比</b>：务必用专业播放器，系统自带网页播放器往往不行。</li>
        <li>播放会消耗服务器与源站流量；暂停/拖动取决于源站是否支持 Range。</li>
      </ul>
    </div>
  </div>
  <script>
    document.getElementById('copyBtn').onclick = async () => {{
      const t = document.getElementById('streamUrl').textContent.trim();
      try {{
        await navigator.clipboard.writeText(t);
        document.getElementById('copyMsg').textContent = '已复制串流地址';
      }} catch (e) {{
        prompt('请手动复制：', t);
      }}
    }};
  </script>
</body>
</html>'''
    return HTMLResponse(html)
