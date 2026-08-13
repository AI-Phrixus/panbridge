from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import Database
from app.auth.quark_session import load_quark_source
from app.auth.onedrive_session import make_onedrive_sink
from app.security import decrypt_json
from app.sources.baidu import BaiduSource
from app.sources.quark import QuarkAuthenticationError
from app.sources.base import SourceFile


VIDEO_EXT = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".m2ts",
    ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".rmvb", ".rm",
}

BROWSER_NATIVE_EXT = {".mp4", ".webm", ".mov", ".m4v"}


@dataclass
class StreamSource:
    kind: str  # local | baidu | quark | onedrive | http
    url: str = ""
    headers: dict[str, str] | None = None
    local_path: Path | None = None
    filename: str = ""
    size: int = 0
    content_type: str = "application/octet-stream"


async def completed_onedrive_info(
    db: Database, job: dict, file_row: dict
) -> dict[str, Any] | None:
    """Return a verified current-account OneDrive item or fail closed."""
    completed_onedrive = (
        file_row.get("status") == "done"
        and str(job.get("destination") or "").lower() == "onedrive"
    )
    if not completed_onedrive:
        return None
    item_id = str(file_row.get("pcloud_fileid") or "")
    if not item_id:
        raise RuntimeError("OneDrive 完成檔缺少檔案 ID，已停止不安全的來源回退")
    sink = await make_onedrive_sink(db)
    info = await sink.download_info_for_item(item_id)
    try:
        file_meta = json.loads(file_row.get("meta_json") or "{}")
    except Exception:
        file_meta = {}
    delivery = file_meta.get("onedrive_delivery") or {}
    expected_drive_id = str(delivery.get("drive_id") or "")
    actual_drive_id = str(info.get("drive_id") or "")
    if not actual_drive_id:
        raise RuntimeError("OneDrive 未返回檔案所屬 Drive ID")
    if expected_drive_id:
        if expected_drive_id != actual_drive_id:
            raise RuntimeError("此任務屬於另一個 OneDrive，請切回原帳號")
    else:
        # A DriveItem ID is only meaningful inside its drive. Name and size
        # are not a safe ownership proof when the user reconnects a different
        # Microsoft account, so legacy rows require an explicit admin migration.
        raise RuntimeError(
            "舊任務尚未綁定 OneDrive 帳號，請由管理員完成一次安全升級"
        )
    return info


def is_video_name(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXT


async def resolve_stream(
    db: Database,
    job_id: int,
    file_id: int,
    *,
    prefer_transcode: bool = False,
) -> StreamSource:
    job = await db.get_job(job_id)
    f = await db.get_file(file_id)
    if not job or not f or f["job_id"] != job_id:
        raise FileNotFoundError("task/file not found")

    name = f.get("remote_name") or f.get("relative_path") or "video"
    size = int(f.get("size") or 0)
    meta: dict[str, Any] = {}
    try:
        meta = json.loads(f.get("meta_json") or "{}")
    except Exception:
        meta = {}

    settings = get_settings()
    suffix = Path(name).suffix.lower()
    ctype = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".mkv": "video/x-matroska",
        ".ts": "video/mp2t",
    }.get(suffix, "application/octet-stream")

    async def completed_onedrive() -> StreamSource | None:
        info = await completed_onedrive_info(db, job, f)
        if not info:
            return None
        return StreamSource(
            kind="onedrive",
            url=str(info["url"]),
            headers={},
            filename=str(info.get("name") or name),
            size=int(info.get("size") or size),
            content_type=str(info.get("content_type") or ctype),
        )

    # A completed OneDrive copy is always authoritative, including old links
    # carrying transcode=1 for MKV/HEVC. Check it before any leftover local
    # staging file so Oracle can never serve completed media bytes.
    onedrive = await completed_onedrive()
    if onedrive:
        return onedrive

    # 1) Local delivered / partial complete file for non-OneDrive-complete jobs
    candidates: list[Path] = []
    if f.get("pcloud_fileid") and str(f["pcloud_fileid"]).startswith("/"):
        candidates.append(Path(str(f["pcloud_fileid"])))
    if f.get("local_path"):
        candidates.append(Path(f["local_path"]))
    rel = (f.get("pcloud_path") or f.get("relative_path") or "").lstrip("/")
    if rel:
        candidates.append(settings.data_path / "delivered" / rel)
    for c in candidates:
        if not c.exists() or not c.is_file() or c.stat().st_size <= 0:
            continue
        range_meta = Path(str(c) + ".ranges.json")
        complete = f.get("status") == "done" or (
            size > 0 and c.stat().st_size == size and not range_meta.exists()
        )
        # local_path is updated while a .part is downloading. Serving it here
        # exposes a truncated or sparse file and prevents source-stream fallback.
        if complete:
            return StreamSource(
                kind="local",
                local_path=c,
                filename=name,
                size=c.stat().st_size,
                content_type=ctype,
            )

    # 2) Source netdisk direct stream (no need to fully download first)
    source_type = job["source_type"]
    if source_type == "baidu":
        if prefer_transcode and suffix not in BROWSER_NATIVE_EXT:
            raise RuntimeError(
                "此影片格式無法直接在網頁解碼，請按 Infuse、VLC 或 PotPlayer 播放"
            )
        enc = await db.get_credential("baidu")
        if not enc:
            raise RuntimeError("百度未登录")
        src = BaiduSource(decrypt_json(enc)["cookie"])
        sf = SourceFile(
            fid=str(meta.get("fs_id") or f.get("source_fid") or ""),
            name=name,
            size=size,
            meta=meta,
        )
        url = await src.prepare_download(sf, {})
        return StreamSource(
            kind="baidu",
            url=url,
            headers=src.get_download_headers(),
            filename=name,
            size=size,
            content_type=ctype,
        )

    if source_type == "quark":
        src = await load_quark_source(db)
        sf = SourceFile(
            fid=str(meta.get("owned_fid") or f.get("source_fid") or ""),
            name=name,
            size=size,
            meta=meta,
        )
        if prefer_transcode:
            try:
                stream = await src.prepare_stream(sf)
                return StreamSource(
                    kind="quark_transcode",
                    url=stream["url"],
                    headers=src.get_download_headers(),
                    filename=name,
                    size=int(stream.get("size") or size),
                    content_type=str(stream.get("content_type") or "video/mp4"),
                )
            except QuarkAuthenticationError:
                raise
            except Exception as error:
                # Transcoding is account/file dependent. Original-file proxy is
                # still a valid fallback for native browser formats and players.
                if suffix not in BROWSER_NATIVE_EXT:
                    raise RuntimeError(
                        f"夸克網頁轉碼暫不可用（{error}）；請按 Infuse、VLC 或 PotPlayer 播放原畫"
                    ) from error
        url = await src.prepare_download(sf, {})
        return StreamSource(
            kind="quark",
            url=url,
            headers=src.get_download_headers(),
            filename=name,
            size=size,
            content_type=ctype,
        )

    raise RuntimeError("无法解析播放源：文件未落地且无法从网盘取直链")
