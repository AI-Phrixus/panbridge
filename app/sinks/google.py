from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

import httpx

from app.security import decrypt_json, encrypt_json
from app.auth.google_session import GoogleAuthenticationError
from app.util_paths import sanitize_rel_path

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
CHUNK_SIZE = 8 * 1024 * 1024
FIELDS = "id,name,size,parents,webViewLink"
_folder_locks = WeakValueDictionary()


def _folder_lock(account_id):
    lock = _folder_locks.get(account_id)
    if lock is None:
        lock = asyncio.Lock()
        _folder_locks[account_id] = lock
    return lock


def _complete_item(item, size):
    if not item.get("id") or int(item.get("size", -1)) != size:
        raise RuntimeError("Google 上傳完整性無法確認，未標記完成")
    return item


class GooglePermanentUploadError(RuntimeError):
    pass


def _quote(value):
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _session_url(value):
    parsed = urlsplit(str(value))
    if (parsed.scheme != "https" or parsed.netloc != "www.googleapis.com"
            or not parsed.path.startswith("/upload/drive/") or parsed.fragment):
        raise RuntimeError("Google 上傳位置不安全，已停止")
    return str(value)


def _offset(response, total):
    value = response.headers.get("range") or ""
    if not value:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", value)
    if not match or not 0 <= int(match[1]) < total:
        raise RuntimeError("Google 上傳進度回應不正確")
    return int(match[1]) + 1


class GoogleDriveSink:
    """Private Drive uploads; never creates public sharing permissions."""

    def __init__(self, access_token_cb, session_id, account_id=None):
        self.access = access_token_cb
        self.session_id = session_id
        self.account_id = account_id or session_id
        self.folders = {}

    async def _request(self, client, method, url, **kwargs):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.googleapis.com":
            raise RuntimeError("Google API 位置不安全")
        headers = dict(kwargs.pop("headers", {}))
        for force in (False, True):
            headers["Authorization"] = "Bearer " + await self.access(force)
            response = await client.request(method, url, headers=headers, **kwargs)
            if response.status_code != 401:
                return response
        raise GoogleAuthenticationError("Google 登入已失效，請重新授權；進度已保留")

    @staticmethod
    def _check(response, allowed=(200, 201)):
        if response.status_code not in allowed:
            if response.status_code in (400, 403):
                raise GooglePermanentUploadError(f"Google Drive 拒絕請求（HTTP {response.status_code}），請檢查空間或權限；進度已保留")
            raise RuntimeError(f"Google Drive 請求失敗（HTTP {response.status_code}）；請檢查空間、授權或稍後重試")

    async def space_info(self):
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await self._request(client, "GET", API + "/about",
                                          params={"fields": "storageQuota,user(displayName,emailAddress)"})
        self._check(response)
        data = response.json()
        quota = data.get("storageQuota") or {}
        limit = int(quota["limit"]) if quota.get("limit") is not None else None
        used = int(quota.get("usage") or 0)
        return {"quota": limit, "used": used, "free": max(0, limit - used) if limit is not None else None,
                "email": (data.get("user") or {}).get("emailAddress")}

    async def ensure_folder(self, client, path):
        # Shared per-account lock covers common prefixes across distinct sinks.
        # Recheck inside the lock; appProperties do not enforce uniqueness.
        async with _folder_lock(self.account_id):
            return await self._ensure_folder(client, path)

    async def _ensure_folder(self, client, path):
        clean = sanitize_rel_path(path)
        parent, accumulated = "root", ""
        for name in clean.split("/") if clean else []:
            accumulated += "/" + name
            if accumulated in self.folders:
                parent = self.folders[accumulated]
                continue
            key = hashlib.sha256(accumulated.encode()).hexdigest()
            response = await self._request(client, "GET", API + "/files", params={
                "q": f"trashed=false and mimeType='application/vnd.google-apps.folder' and '{_quote(parent)}' in parents and appProperties has {{ key='panbridge_folder' and value='{key}' }}",
                "fields": "files(id)", "pageSize": 100,
            })
            self._check(response)
            found = response.json().get("files") or []
            if found:
                parent = found[0]["id"]
            else:
                response = await self._request(client, "POST", API + "/files", params={"fields": "id"}, json={
                    "name": name, "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent], "appProperties": {"panbridge_folder": key},
                })
                self._check(response)
                parent = response.json()["id"]
            self.folders[accumulated] = parent
        return parent

    async def folder_url(self, path):
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            folder_id = await self.ensure_folder(client, path)
        return "https://drive.google.com/drive/folders/" + folder_id

    async def upload_file(self, local_path, remote_dir, filename, progress_cb=None):
        local = Path(local_path)
        size = local.stat().st_size
        key = hashlib.sha256((self.account_id + "\0" + remote_dir + "\0" + local.name).encode()).hexdigest()
        checkpoint = Path(str(local) + ".google-upload.enc")
        session = ""
        try:
            saved = decrypt_json(checkpoint.read_text())
            if saved.get("key") == key and saved.get("size") == size:
                session = _session_url(saved["session"])
        except (OSError, ValueError, KeyError):
            pass
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=30), follow_redirects=False) as client:
            parent = await self.ensure_folder(client, remote_dir)
            # A lost final response must not create duplicate copies on retry.
            found = await self._request(client, "GET", API + "/files", params={
                "q": f"trashed=false and '{_quote(parent)}' in parents and appProperties has {{ key='panbridge_upload' and value='{key}' }}",
                "fields": f"files({FIELDS})", "pageSize": 100,
            })
            self._check(found)
            for item in found.json().get("files") or []:
                if item.get("id") and item.get("size") is not None and int(item["size"]) == size:
                    checkpoint.unlink(missing_ok=True)
                    if progress_cb:
                        await progress_cb(size, size)
                    return item

            if not session:
                mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                response = await self._request(client, "POST", UPLOAD,
                    params={"uploadType": "resumable", "fields": FIELDS},
                    headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                    json={"name": filename, "parents": [parent], "appProperties": {"panbridge_upload": key}})
                self._check(response)
                session = _session_url(response.headers.get("location") or "")
                encoded = encrypt_json({"key": key, "size": size, "session": session})
                staging = Path(str(checkpoint) + ".tmp")
                fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(staging, checkpoint)

            async def probe():
                return await self._request(client, "PUT", session, content=b"",
                    headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"})

            response = await probe()
            if response.status_code in (200, 201):
                item = _complete_item(response.json(), size)
                checkpoint.unlink(missing_ok=True)
                return item
            if response.status_code in (404, 410):
                checkpoint.unlink(missing_ok=True)
                raise RuntimeError("Google 上傳工作階段已過期；重試時重新建立，下載文件已保留")
            self._check(response, (308,))
            position, failures = _offset(response, size), 0
            if progress_cb:
                await progress_cb(position, size)
            with local.open("rb") as handle:
                while position < size or size == 0:
                    handle.seek(position)
                    content = await asyncio.to_thread(handle.read, CHUNK_SIZE)
                    end = position + len(content) - 1
                    content_range = f"bytes {position}-{end}/{size}" if size else "bytes */0"
                    try:
                        response = await self._request(client, "PUT", session, content=content,
                            headers={"Content-Length": str(len(content)), "Content-Range": content_range})
                    except httpx.TransportError:
                        response = None
                    if response is None or response.status_code in (408, 429, 500, 502, 503, 504):
                        failures += 1
                        if failures > 6:
                            raise RuntimeError("Google 上傳暫時不可用；續傳紀錄已保留")
                        await asyncio.sleep(min(30, 2 ** failures))
                        response = await probe()
                    if response.status_code in (200, 201):
                        item = _complete_item(response.json(), size)
                        checkpoint.unlink(missing_ok=True)
                        if progress_cb:
                            await progress_cb(size, size)
                        return item
                    self._check(response, (308,))
                    acknowledged = _offset(response, size)
                    if acknowledged <= position:
                        failures += 1
                        if failures > 6:
                            raise RuntimeError("Google 上傳沒有進展；續傳紀錄已保留")
                        await asyncio.sleep(min(30, 2 ** failures))
                    else:
                        failures = 0
                    position = acknowledged
                    if progress_cb:
                        await progress_cb(position, size)
        raise RuntimeError("Google 上傳尚未確認完成；續傳紀錄已保留")
