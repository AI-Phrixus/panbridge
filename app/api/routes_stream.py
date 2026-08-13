from __future__ import annotations

import html
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from app.api.deps import require_auth
from app.auth.quark_session import load_quark_source
from app.config import get_settings
from app.db import db
from app.security import (
    make_hls_asset_token,
    make_stream_token,
    verify_hls_asset_token,
    verify_session_token,
    verify_stream_token,
)
from app.stream.resolve import completed_onedrive_info, resolve_stream

router = APIRouter(tags=["stream"])


def _public_base(request: Request) -> str:
    configured = get_settings().public_base_url.strip().rstrip("/")
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return configured
    scheme = request.url.scheme if request.url.scheme in ("http", "https") else "http"
    # Host is request-controlled. Only normal DNS/IP/IPv6 authority characters
    # may be embedded into the HTML page and external-player links.
    authority = request.url.netloc
    if not re.fullmatch(r"[A-Za-z0-9.:[\]_-]+", authority or ""):
        authority = f"127.0.0.1:{get_settings().port}"
    return f"{scheme}://{authority}"


def _content_disposition(filename: str) -> str:
    safe = quote(filename.replace("\r", "").replace("\n", ""), safe="")
    return f"inline; filename*=UTF-8''{safe}"


def _player_links(
    request: Request,
    job_id: int,
    file_id: int,
    filename: str,
    *,
    direct_url: str = "",
) -> dict[str, str | bool]:
    """Build platform links, preferring a fresh OneDrive HTTPS URL."""
    token = make_stream_token(job_id, file_id)
    stream_path = (
        f"/api/tasks/{job_id}/files/{file_id}/stream"
        f"?token={quote(token, safe='')}"
    )
    stream_url = direct_url or (_public_base(request) + stream_path)
    encoded_url = quote(stream_url, safe="")
    encoded_name = quote(
        str(filename).replace("\r", " ").replace("\n", " "), safe=""
    )
    return {
        "stream_path": direct_url or stream_path,
        # Keep the compatibility key for existing clients, but serve the
        # original file directly. Quark's browser transcode endpoint currently
        # returns plf_invalid and can otherwise delay every native MP4 play.
        "browser_stream_path": direct_url or stream_path,
        "stream_url": stream_url,
        "direct_onedrive": bool(direct_url),
        "infuse_url": (
            "infuse://x-callback-url/play"
            f"?url={encoded_url}&filename={encoded_name}"
        ),
        "vlc_url": "vlc://" + stream_url,
        "vlc_ios_url": f"vlc-x-callback://x-callback-url/stream?url={encoded_url}",
        "vlc_android_url": (
            "intent:" + stream_url
            + "#Intent;action=android.intent.action.VIEW;type=video/*;"
            "package=org.videolan.vlc;end"
        ),
        "iina_url": f"iina://weblink?url={encoded_url}",
        "potplayer_url": "potplayer://" + stream_url,
        "playlist_path": f"/api/tasks/{job_id}/files/{file_id}/playlist.m3u",
    }


async def _links_for_file(
    request: Request,
    job: dict,
    file_row: dict,
    filename: str,
) -> dict[str, str | bool]:
    """Resolve completed OneDrive files just in time for the chosen player."""
    direct_url = ""
    if (
        file_row.get("status") == "done"
        and str(job.get("destination") or "").lower() == "onedrive"
    ):
        try:
            info = await completed_onedrive_info(db, job, file_row)
            if not info:
                raise RuntimeError("OneDrive 完成檔資料不完整")
            direct_url = str(info.get("url") or "")
        except Exception as error:
            raise HTTPException(
                502,
                f"無法取得 OneDrive 播放直鏈，請在設定頁重新連接 OneDrive：{error}",
            ) from error
    return _player_links(
        request,
        int(job["id"]),
        int(file_row["id"]),
        filename,
        direct_url=direct_url,
    )


def _parse_range(value: str, size: int) -> tuple[int, int]:
    units, separator, raw = value.partition("=")
    if separator != "=" or units.strip().lower() != "bytes" or "," in raw:
        raise ValueError("unsupported range")
    start_s, dash, end_s = raw.strip().partition("-")
    if dash != "-" or (not start_s and not end_s):
        raise ValueError("invalid range")
    if not start_s:
        suffix = int(end_s)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    end = min(end, size - 1)
    if start < 0 or start > end or start >= size:
        raise ValueError("range outside file")
    return start, end


def _stream_authorized(request: Request, token: str | None, job_id: int, file_id: int) -> bool:
    session = request.cookies.get("panbridge_session")
    return verify_session_token(session) or verify_stream_token(token, job_id, file_id)


def _hls_asset_path(job_id: int, file_id: int, url: str) -> str:
    if not _safe_quark_url(url):
        return f"/api/tasks/{job_id}/files/{file_id}/hls-asset?asset=blocked"
    token = make_hls_asset_token(job_id, file_id, url)
    return (
        f"/api/tasks/{job_id}/files/{file_id}/hls-asset"
        f"?asset={quote(token, safe='')}"
    )


def _rewrite_hls_manifest(
    manifest: str, manifest_url: str, job_id: int, file_id: int
) -> str:
    """Route playlists, segments, keys and init maps through signed proxy URLs."""

    def proxied(value: str) -> str:
        absolute = urljoin(manifest_url, value.strip())
        if not absolute.startswith(("https://", "http://")):
            return value
        return _hls_asset_path(job_id, file_id, absolute)

    quoted_uri = re.compile(r'URI="([^"]+)"')
    bare_uri = re.compile(r'URI=(?!")([^,\s]+)')
    output: list[str] = []
    for line in manifest.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            output.append(proxied(stripped))
            continue
        line = quoted_uri.sub(lambda match: f'URI="{proxied(match.group(1))}"', line)
        line = bare_uri.sub(lambda match: f"URI={proxied(match.group(1))}", line)
        output.append(line)
    return "\n".join(output) + ("\n" if manifest.endswith(("\n", "\r")) else "")


def _is_hls(content_type: str, url: str = "") -> bool:
    media = content_type.lower()
    return "mpegurl" in media or ".m3u8" in urlsplit(url).path.lower()


def _safe_quark_url(url: str) -> bool:
    """Allow only HTTPS URLs owned by Quark; reject IP/private/foreign hosts."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and (host == "quark.cn" or host.endswith(".quark.cn"))
        )
    except ValueError:
        return False


async def _send_quark_stream_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    """Follow only redirects that remain on HTTPS Quark-owned hosts."""
    current = url
    for _redirect in range(4):
        if not _safe_quark_url(current):
            raise RuntimeError("夸克播放網址被安全策略拒絕")
        request = client.build_request(method, current, headers=headers)
        response = await client.send(request, stream=True)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location") or ""
        next_url = urljoin(str(response.url), location)
        await response.aclose()
        if not _safe_quark_url(next_url):
            raise RuntimeError("夸克播放重新導向到非官方網址，已阻擋")
        current = next_url
    raise RuntimeError("夸克播放重新導向次數過多")


@router.api_route("/api/tasks/{job_id}/files/{file_id}/stream", methods=["GET", "HEAD"])
async def stream_file(
    job_id: int,
    file_id: int,
    request: Request,
    token: str | None = Query(default=None),
    transcode: bool = Query(default=False),
):
    """Proxy/stream file for players. Supports HTTP Range."""
    if not _stream_authorized(request, token, job_id, file_id):
        raise HTTPException(401, "login or valid player link required")
    try:
        src = await resolve_stream(db, job_id, file_id, prefer_transcode=transcode)
    except FileNotFoundError:
        raise HTTPException(404, "not found")
    except Exception as e:
        raise HTTPException(400, str(e)) from e

    # Completed OneDrive files are never media-proxied. This also upgrades old
    # seven-day PanBridge tokens/playlists to the latest Microsoft HTTPS URL.
    if src.kind == "onedrive" and src.url:
        return RedirectResponse(
            src.url,
            status_code=307,
            headers={"Cache-Control": "private, no-store"},
        )

    range_header = request.headers.get("range") or request.headers.get("Range")

    # ---- Local file ----
    if src.kind == "local" and src.local_path:
        path = src.local_path
        file_size = path.stat().st_size
        media = src.content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not range_header:
            if request.method == "HEAD":
                return Response(
                    status_code=200,
                    media_type=media,
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(file_size),
                        "Content-Disposition": _content_disposition(src.filename),
                    },
                )
            return FileResponse(
                path,
                media_type=media,
                filename=src.filename,
                headers={"Accept-Ranges": "bytes", "Content-Disposition": _content_disposition(src.filename)},
            )

        try:
            start, end = _parse_range(range_header, file_size)
        except (TypeError, ValueError):
            raise HTTPException(416, "invalid range", headers={"Content-Range": f"bytes */{file_size}"})
        length = end - start + 1

        if request.method == "HEAD":
            return Response(
                status_code=206,
                media_type=media,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Content-Disposition": _content_disposition(src.filename),
                },
            )

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
                "Content-Disposition": _content_disposition(src.filename),
            },
        )

    # ---- Remote proxy ----
    if not src.url:
        raise HTTPException(400, "no remote url")

    if request.method == "HEAD":
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": _content_disposition(src.filename),
            "Cache-Control": "private, no-store",
        }
        if src.size > 0:
            headers["Content-Length"] = str(src.size)
        return Response(status_code=200, media_type=src.content_type, headers=headers)

    if range_header:
        try:
            if src.size > 0:
                _parse_range(range_header, src.size)
            elif not re.match(r"^bytes=\d*-\d*$", range_header.strip(), re.IGNORECASE):
                raise ValueError("invalid range")
        except ValueError:
            extra = {"Content-Range": f"bytes */{src.size}"} if src.size > 0 else None
            raise HTTPException(416, "invalid range", headers=extra)

    client: httpx.AsyncClient | None = None
    resp: httpx.Response | None = None
    error_body = b""
    for upstream_attempt in range(2):
        up_headers = dict(src.headers or {})
        # Keep upstream length and downstream bytes in the same representation.
        # httpx otherwise transparently decompresses gzip/br while we forward
        # the encoded Content-Length, which truncates small HLS manifests.
        up_headers["Accept-Encoding"] = "identity"
        if range_header:
            up_headers["Range"] = range_header
        quark_upstream = src.kind in ("quark", "quark_transcode")
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=30.0),
            follow_redirects=not quark_upstream,
        )
        try:
            if quark_upstream:
                resp = await _send_quark_stream_request(
                    client, "GET", src.url, up_headers
                )
            else:
                req = client.build_request("GET", src.url, headers=up_headers)
                resp = await client.send(req, stream=True)
        except Exception:
            await client.aclose()
            raise
        if resp.status_code < 400 or resp.status_code == 206:
            break
        error_body = (await resp.aread())[:400]
        status = resp.status_code
        await resp.aclose()
        await client.aclose()
        resp = None
        client = None
        if upstream_attempt == 0 and status in (401, 403, 404, 412):
            # The player may hold this proxy request for hours. Resolve one new
            # source URL (and rotating Quark cookies) before returning an error.
            try:
                src = await resolve_stream(db, job_id, file_id, prefer_transcode=transcode)
                continue
            except Exception as error:
                raise HTTPException(502, f"播放直鏈刷新失敗: {error}") from error
        raise HTTPException(502, f"源站拒絕播放串流: HTTP {status} {error_body[:160]!r}")

    if resp is None or client is None:
        raise HTTPException(502, "無法建立播放串流")

    media = src.content_type or resp.headers.get("content-type") or "application/octet-stream"
    if _is_hls(media, str(resp.url)):
        try:
            payload = await resp.aread()
            manifest = payload.decode("utf-8-sig")
            rewritten = _rewrite_hls_manifest(
                manifest, str(resp.url), job_id, file_id
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise HTTPException(502, f"HLS 播放清單無法解析: {error}") from error
        finally:
            await resp.aclose()
            await client.aclose()
        return Response(
            content=rewritten,
            status_code=resp.status_code,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "private, no-store"},
        )

    out_headers = {
        "Accept-Ranges": resp.headers.get("accept-ranges") or "bytes",
        "Content-Disposition": _content_disposition(src.filename),
        "Cache-Control": "no-store",
    }
    if resp.headers.get("content-length"):
        out_headers["Content-Length"] = resp.headers["content-length"]
    if resp.headers.get("content-range"):
        out_headers["Content-Range"] = resp.headers["content-range"]
    if resp.headers.get("content-encoding"):
        out_headers["Content-Encoding"] = resp.headers["content-encoding"]

    async def gen():
        try:
            async for chunk in resp.aiter_raw(1024 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=resp.status_code, media_type=media, headers=out_headers)


@router.api_route(
    "/api/tasks/{job_id}/files/{file_id}/hls-asset", methods=["GET", "HEAD"]
)
async def hls_asset(
    job_id: int,
    file_id: int,
    request: Request,
    asset: str = Query(...),
):
    """Proxy only server-signed HLS child playlists, segments, maps and keys."""
    upstream_url = verify_hls_asset_token(asset, job_id, file_id)
    if not upstream_url:
        raise HTTPException(401, "invalid or expired HLS asset link")
    job = await db.get_job(job_id)
    file_row = await db.get_file(file_id)
    if not job or not file_row or file_row["job_id"] != job_id:
        raise HTTPException(404, "not found")
    if (
        file_row.get("status") == "done"
        and str(job.get("destination") or "").lower() == "onedrive"
    ):
        raise HTTPException(
            410,
            "此舊播放分片已停用，請重新按 OneDrive 直連播放器",
            headers={"Cache-Control": "private, no-store"},
        )
    if job["source_type"] != "quark":
        raise HTTPException(400, "HLS proxy is unavailable for this source")

    source = await load_quark_source(db)
    upstream_headers = source.get_download_headers()
    upstream_headers["Accept-Encoding"] = "identity"
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=30.0),
        follow_redirects=False,
    )
    try:
        upstream = await _send_quark_stream_request(
            client, request.method, upstream_url, upstream_headers
        )
    except Exception:
        await client.aclose()
        raise
    if upstream.status_code >= 400:
        body = (await upstream.aread())[:200]
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        proxy_status = 410 if status in (401, 403, 404, 412) else 502
        raise HTTPException(
            proxy_status,
            f"HLS 資源已過期或被源站拒絕: HTTP {status} {body!r}",
        )

    media = upstream.headers.get("content-type") or "application/octet-stream"
    common_headers = {"Cache-Control": "private, no-store"}
    if upstream.headers.get("accept-ranges"):
        common_headers["Accept-Ranges"] = upstream.headers["accept-ranges"]
    if upstream.headers.get("content-range"):
        common_headers["Content-Range"] = upstream.headers["content-range"]

    if request.method == "HEAD":
        if upstream.headers.get("content-length"):
            common_headers["Content-Length"] = upstream.headers["content-length"]
        await upstream.aclose()
        await client.aclose()
        return Response(status_code=upstream.status_code, media_type=media, headers=common_headers)

    if _is_hls(media, str(upstream.url)):
        try:
            manifest = (await upstream.aread()).decode("utf-8-sig")
            rewritten = _rewrite_hls_manifest(
                manifest, str(upstream.url), job_id, file_id
            )
        except UnicodeDecodeError as error:
            raise HTTPException(502, f"HLS 子播放清單無法解析: {error}") from error
        finally:
            await upstream.aclose()
            await client.aclose()
        return Response(
            content=rewritten,
            status_code=upstream.status_code,
            media_type="application/vnd.apple.mpegurl",
            headers=common_headers,
        )

    if upstream.headers.get("content-length"):
        common_headers["Content-Length"] = upstream.headers["content-length"]
    if upstream.headers.get("content-encoding"):
        common_headers["Content-Encoding"] = upstream.headers["content-encoding"]

    async def stream_asset():
        try:
            async for chunk in upstream.aiter_raw(1024 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_asset(),
        status_code=upstream.status_code,
        media_type=media,
        headers=common_headers,
    )


@router.get("/api/tasks/{job_id}/files/{file_id}/playlist.m3u")
async def player_playlist(
    job_id: int,
    file_id: int,
    request: Request,
    _: None = Depends(require_auth),
):
    """Download a standard playlist for VLC/PotPlayer/Infuse on other devices."""
    job = await db.get_job(job_id)
    file_row = await db.get_file(file_id)
    if not job or not file_row or file_row["job_id"] != job_id:
        raise HTTPException(404, "not found")
    name = str(file_row.get("remote_name") or "video").replace("\r", " ").replace("\n", " ")
    links = await _links_for_file(request, job, file_row, name)
    stream_url = str(links["stream_url"])
    playlist = f"#EXTM3U\n#EXTINF:-1,{name}\n{stream_url}\n"
    download_name = (Path(name).stem or "panbridge") + ".m3u"
    disposition = _content_disposition(download_name).replace("inline;", "attachment;", 1)
    return Response(
        content=playlist,
        media_type="audio/x-mpegurl",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/api/tasks/{job_id}/files/{file_id}/player-links")
async def player_links(
    job_id: int,
    file_id: int,
    request: Request,
    _: None = Depends(require_auth),
):
    """Return signed direct-open links for the current device's player."""
    job = await db.get_job(job_id)
    file_row = await db.get_file(file_id)
    if not job or not file_row or file_row["job_id"] != job_id:
        raise HTTPException(404, "not found")
    name = str(
        file_row.get("remote_name")
        or file_row.get("relative_path")
        or "video"
    )
    if not Path(name).suffix.lower() in {
        ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".m2ts",
        ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".rmvb", ".rm",
        ".hevc", ".h265",
    }:
        raise HTTPException(400, "not a supported video file")
    return JSONResponse(
        {**(await _links_for_file(request, job, file_row, name)), "filename": name},
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/tasks/{job_id}/files/{file_id}/open/{player}")
async def open_player(
    job_id: int,
    file_id: int,
    player: str,
    request: Request,
    _: None = Depends(require_auth),
):
    """Keep browser user activation while redirecting into a native player."""
    job = await db.get_job(job_id)
    file_row = await db.get_file(file_id)
    if not job or not file_row or file_row["job_id"] != job_id:
        raise HTTPException(404, "not found")
    name = str(file_row.get("remote_name") or "video")
    links = await _links_for_file(request, job, file_row, name)
    target_key = {
        "infuse": "infuse_url",
        "vlc": "vlc_url",
        "iina": "iina_url",
        "potplayer": "potplayer_url",
    }.get(player.lower())
    if not target_key:
        raise HTTPException(400, "unsupported player")
    return RedirectResponse(
        links[target_key],
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/play/{job_id}/{file_id}", response_class=HTMLResponse)
async def play_page(job_id: int, file_id: int, request: Request, _: None = Depends(require_auth)):
    job = await db.get_job(job_id)
    f = await db.get_file(file_id)
    if not job or not f or f["job_id"] != job_id:
        raise HTTPException(404, "not found")

    name = f.get("remote_name") or "video"
    size = int(f.get("size") or 0)
    links = await _links_for_file(request, job, f, str(name))
    stream_path = str(links["stream_path"])
    browser_stream_path = str(links["browser_stream_path"])
    stream_url = str(links["stream_url"])
    ext = Path(name).suffix.lower()
    web_playable = ext in {".mp4", ".webm", ".mov", ".m4v"}

    safe_stream_path = html.escape(stream_path, quote=True)
    safe_browser_stream_path = html.escape(browser_stream_path, quote=True)
    safe_stream_url = html.escape(stream_url, quote=True)
    open_base = f"/api/tasks/{job_id}/files/{file_id}/open"
    safe_infuse = html.escape(f"{open_base}/infuse", quote=True)
    safe_vlc_web = html.escape(f"{open_base}/vlc", quote=True)
    safe_android_vlc = html.escape(links["vlc_android_url"], quote=True)
    safe_ios_vlc = html.escape(links["vlc_ios_url"], quote=True)
    safe_iina = html.escape(f"{open_base}/iina", quote=True)
    safe_pot = html.escape(f"{open_base}/potplayer", quote=True)
    safe_playlist_path = html.escape(links["playlist_path"], quote=True)

    size_gb = size / 1024 / 1024 / 1024 if size else 0
    safe_name = html.escape(str(name), quote=True)
    safe_ext = html.escape(ext, quote=True)

    if web_playable:
        video_tag = f'''
        <video id="v" controls playsinline preload="metadata"
          data-stream="{safe_browser_stream_path}"
          data-direct="{1 if links['direct_onedrive'] else 0}"
          style="width:100%;max-height:70vh;background:#000;border-radius:12px"></video>
        <p id="playerMsg" class="muted"></p>'''
    else:
        video_tag = f'''
        <div class="card" style="background:#111;color:#ccc;text-align:center;padding:28px">
          <p style="font-size:1.1rem;margin:0 0 8px">{safe_ext or "此"} 格式不交給瀏覽器硬解碼</p>
          <p class="muted" style="margin:0">請直接按下方 Infuse / VLC / PotPlayer；仍是即時在線串流，不需要先下載影片。</p>
        </div>'''

    playback_note = (
        "瀏覽器會直接嘗試播放原檔；若是 HEVC／杜比或出現黑畫面、無聲，請改用下方專業播放器。"
        if web_playable
        else "MKV／HEVC／杜比等格式由專業播放器在線串流，避免瀏覽器黑畫面或無聲。"
    )

    page_html = f'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>播放 · {safe_name}</title>
  <link rel="stylesheet" href="/static/style.css?v=0.4.2" />
  <script async id="hlsLibrary" src="/static/vendor/hls.light.min.js"
    integrity="sha384-R/A0SfcLw9wTUjx6JTLqfFBfDpC0DQOKgiff7C516hTFU9AWjNDazyoPSfFhD3sx"
    crossorigin="anonymous"></script>
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
      <div class="logo">Pan<span>Bridge</span> 播放 <small style="font-size:.65rem">v0.4.2 · OneDrive 直連</small></div>
      <nav>
        <a href="/tasks/{job_id}">← 返回任务</a>
        <a href="/">任务列表</a>
      </nav>
    </header>

    <div class="card">
      <h2 style="margin-top:0">{safe_name}</h2>
      <p class="muted">約 {size_gb:.2f} GB · 無需等搬運完成 · {playback_note}</p>
      {video_tag}
    </div>

    <div class="card">
      <h3>用專業播放器開啟（推薦）</h3>
      <p class="muted">搬運完成後會取得最新 OneDrive HTTPS 臨時直鏈；影片資料由播放器直接讀取 OneDrive，不再經過 Oracle。若按鈕無反應：下載 .m3u，或複製直鏈到播放器的「打開網路串流」。</p>
      <div class="play-actions">
        <a class="primary" href="{safe_infuse}">Infuse（Apple）</a>
        <a href="{safe_vlc_web}">VLC（Mac / Windows）</a>
        <a href="{safe_iina}">IINA (Mac)</a>
        <a href="{safe_pot}">PotPlayer (Win)</a>
        <a href="{safe_ios_vlc}">VLC (iPhone/iPad)</a>
        <a href="{safe_android_vlc}">VLC (Android)</a>
        <button type="button" id="copyBtn">复制串流地址</button>
        <a href="{safe_playlist_path}">下載 .m3u（Windows / VLC）</a>
        <a href="{safe_stream_path}">新標籤打開串流</a>
      </div>
      <div class="urlbox" id="streamUrl">{safe_stream_url}</div>
      <p id="copyMsg" class="muted"></p>
    </div>

    <div class="card">
      <h3>跨平台说明</h3>
      <ul class="muted">
        <li><b>Windows</b>：VLC 可按上方按鈕；PotPlayer 可按專用按鈕；也可下載 .m3u 後雙擊，用 Windows 已設定的播放器開啟。</li>
        <li><b>Mac</b>：可直接按 Infuse、VLC 或 <a href="https://iina.io/" target="_blank">IINA</a>。</li>
        <li><b>iPhone / iPad</b>：Infuse 付費版可按上方 Infuse 按鈕直開；也可使用 VLC 專用按鈕。</li>
        <li><b>Apple TV</b>：在 Infuse 新增 PanBridge 的串流地址，或由同一 Apple 帳號裝置接續播放。</li>
        <li><b>Android</b>：装 VLC 后点「VLC (Android)」。</li>
        <li><b>MKV / 杜比</b>：务必用专业播放器，系统自带网页播放器往往不行。</li>
        <li>播放会消耗服务器与源站流量；暂停/拖动取决于源站是否支持 Range。</li>
      </ul>
    </div>
  </div>
  <script>
    let playerStarted = false;
    let playerStarting = false;
    let hlsReloads = 0;
    async function setupPlayer() {{
      const video = document.getElementById('v');
      if (!video || playerStarted || playerStarting) return;
      playerStarting = true;
      const source = video.dataset.stream;
      const message = document.getElementById('playerMsg');
      try {{
        if (video.dataset.direct === '1') {{
          playerStarted = true;
          video.src = source;
          video.load();
          return;
        }}
        const probe = await fetch(source, {{method: 'HEAD', cache: 'no-store'}});
        const type = (probe.headers.get('content-type') || '').toLowerCase();
        if (!probe.ok) {{
          let detail = '網頁播放暫不可用';
          try {{
            const payload = await probe.json();
            detail = payload.detail || detail;
          }} catch (_ignored) {{}}
          if (message) message.textContent = detail;
          return;
        }}
        if (type.includes('mpegurl')) {{
          if (video.canPlayType('application/vnd.apple.mpegurl')) {{
            playerStarted = true;
            video.src = source;
          }} else if (window.Hls && Hls.isSupported()) {{
            playerStarted = true;
            const attach = (position = 0) => {{
              const hls = new Hls({{enableWorker: true, startPosition: position}});
              hls.loadSource(source + '&_reload=' + Date.now());
              hls.attachMedia(video);
              hls.on(Hls.Events.ERROR, (_event, data) => {{
                if (!data.fatal) return;
                const resumeAt = video.currentTime || position;
                hls.destroy();
                if (hlsReloads < 2) {{
                  hlsReloads += 1;
                  if (message) message.textContent = '播放連結已刷新，正在從中斷位置恢復…';
                  setTimeout(() => attach(resumeAt), 800);
                }} else if (message) {{
                  message.textContent = '網頁播放暫時失敗，請使用下方 VLC / IINA 原畫連結。';
                }}
              }});
            }};
            attach();
          }} else {{
            if (message) message.textContent = '正在載入網頁播放器；若未開始，請使用下方 VLC / IINA 原畫連結。';
            return;
          }}
        }} else {{
          playerStarted = true;
          video.src = source;
        }}
      }} catch (_error) {{
        playerStarted = true;
        video.src = source;
      }} finally {{
        playerStarting = false;
      }}
    }}
    window.addEventListener('DOMContentLoaded', setupPlayer);
    const hlsLibrary = document.getElementById('hlsLibrary');
    if (hlsLibrary) hlsLibrary.addEventListener('load', setupPlayer);
    if (hlsLibrary) hlsLibrary.addEventListener('error', () => {{
      const message = document.getElementById('playerMsg');
      if (message) message.textContent = '網頁播放器載入失敗，請使用下方 VLC / Infuse / PotPlayer 連結。';
    }});

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
    return HTMLResponse(page_html, headers={"Cache-Control": "no-store"})
