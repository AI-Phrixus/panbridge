from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import Database
from app.security import decrypt_json
from app.sources.baidu import BaiduSource
from app.sources.quark import QuarkSource
from app.sources.base import SourceFile


VIDEO_EXT = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".m2ts",
    ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".rmvb", ".rm",
}


@dataclass
class StreamSource:
    kind: str  # local | baidu | quark | onedrive | http
    url: str = ""
    headers: dict[str, str] | None = None
    local_path: Path | None = None
    filename: str = ""
    size: int = 0
    content_type: str = "application/octet-stream"


def is_video_name(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXT


async def resolve_stream(db: Database, job_id: int, file_id: int) -> StreamSource:
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

    # 1) Local delivered / partial complete file
    candidates: list[Path] = []
    if f.get("pcloud_fileid") and str(f["pcloud_fileid"]).startswith("/"):
        candidates.append(Path(str(f["pcloud_fileid"])))
    if f.get("local_path"):
        candidates.append(Path(f["local_path"]))
    rel = (f.get("pcloud_path") or f.get("relative_path") or "").lstrip("/")
    if rel:
        candidates.append(settings.data_path / "delivered" / rel)
    for c in candidates:
        if c.exists() and c.is_file() and c.stat().st_size > 0:
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
        enc = await db.get_credential("quark")
        if not enc:
            raise RuntimeError("夸克未登录")
        src = QuarkSource(decrypt_json(enc)["cookie"])
        sf = SourceFile(
            fid=str(meta.get("owned_fid") or f.get("source_fid") or ""),
            name=name,
            size=size,
            meta=meta,
        )
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
