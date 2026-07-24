from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx


class PCloudSink:
    def __init__(self, auth_token: str, api_host: str = "eapi.pcloud.com") -> None:
        self.auth = auth_token
        self.base = f"https://{api_host}"

    async def _get(self, method: str, **params: Any) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        params["auth"] = self.auth
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(f"{self.base}/{method}", params=params)
            data = r.json()
            if data.get("result") != 0:
                raise RuntimeError(f"pcloud {method} error {data.get('result')}: {data.get('error')}")
            return data

    async def userinfo(self) -> dict[str, Any]:
        return await self._get("userinfo")

    async def space_info(self) -> dict[str, int]:
        """Return quota/used/free in bytes (best-effort from userinfo)."""
        info = await self.userinfo()
        quota = int(info.get("quota") or 0)
        used = int(info.get("usedquota") or 0)
        freeq = int(info.get("freequota") or 0)
        # free accounts sometimes report freequota > quota; use min positive ceilings
        ceilings = [x for x in (quota, freeq) if x > 0]
        total = min(ceilings) if ceilings else quota or freeq
        free = max(0, total - used)
        return {"quota": total, "used": used, "free": free}

    async def create_folder_if_not_exists(self, path: str) -> int:
        path = path if path.startswith("/") else "/" + path
        data = await self._get("createfolderifnotexists", path=path)
        return int(data["metadata"]["folderid"])

    async def ensure_path(self, path: str) -> int:
        """Create nested folders; return final folderid."""
        path = path.strip("/")
        if not path:
            data = await self._get("listfolder", folderid=0)
            return 0
        cur = ""
        folder_id = 0
        for part in path.split("/"):
            if not part:
                continue
            cur += "/" + part
            folder_id = await self.create_folder_if_not_exists(cur)
        return folder_id

    async def stat_file(self, path: str) -> dict[str, Any] | None:
        try:
            data = await self._get("stat", path=path)
            return data.get("metadata")
        except RuntimeError:
            return None

    async def upload_file(
        self,
        local_path: Path,
        remote_folder_path: str,
        filename: str,
        progress_cb: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        remote_folder_path = remote_folder_path if remote_folder_path.startswith("/") else "/" + remote_folder_path
        await self.ensure_path(remote_folder_path)
        full_remote = remote_folder_path.rstrip("/") + "/" + filename

        existing = await self.stat_file(full_remote)
        size = local_path.stat().st_size
        if existing and not existing.get("isfolder") and int(existing.get("size") or 0) == size:
            if progress_cb:
                await progress_cb(size, size)
            return existing

        progresshash = hashlib.md5(f"{full_remote}:{size}".encode()).hexdigest()
        folderid = await self.ensure_path(remote_folder_path)

        # Stream upload via multipart
        # Avoid timeout=None hangs on stalled connections (same class of bug as download)
        _to = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0)
        async with httpx.AsyncClient(timeout=_to) as client:
            # Use PUT-style body upload is simpler for progress tracking
            # pCloud accepts POST multipart uploadfile
            with open(local_path, "rb") as f:
                files = {"file": (filename, f)}
                data = {
                    "auth": self.auth,
                    "folderid": str(folderid),
                    "filename": filename,
                    "nopartial": "1",
                    "progresshash": progresshash,
                    "renameifexists": "0",
                }
                # httpx reads file fully for multipart; for large files use manual streaming PUT
            # Prefer streaming PUT: https://api/uploadfile?auth=&folderid=&filename=
            url = f"{self.base}/uploadfile"
            params = {
                "auth": self.auth,
                "folderid": folderid,
                "filename": filename,
                "nopartial": 1,
                "progresshash": progresshash,
            }

            async def file_iter():
                sent = 0
                with open(local_path, "rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        sent += len(chunk)
                        if progress_cb:
                            await progress_cb(sent, size)
                        yield chunk

            headers = {"Content-Type": "application/octet-stream"}
            r = await client.put(url, params=params, content=file_iter(), headers=headers)
            try:
                result = r.json()
            except Exception as e:
                raise RuntimeError(f"pcloud upload bad response: {r.status_code} {r.text[:300]}") from e
            if result.get("result") != 0:
                # fallback multipart POST
                with open(local_path, "rb") as f:
                    r2 = await client.post(
                        url,
                        data={
                            "auth": self.auth,
                            "folderid": str(folderid),
                            "nopartial": "1",
                            "progresshash": progresshash,
                        },
                        files={"file": (filename, f)},
                    )
                    result = r2.json()
                if result.get("result") != 0:
                    raise RuntimeError(f"pcloud upload failed: {result}")
            meta_list = result.get("metadata") or []
            meta = meta_list[0] if meta_list else result.get("metadata") or {}
            if not isinstance(meta, dict):
                raise RuntimeError(f"pcloud upload missing metadata: {result}")
            remote_size = int(meta.get("size") or 0)
            if remote_size and remote_size != size:
                raise RuntimeError(
                    f"pcloud upload size mismatch: remote={remote_size} local={size}"
                )
            if not remote_size and size > 0:
                # some responses omit size — verify via stat when path known
                pass
            if progress_cb:
                await progress_cb(size, size)
            return meta

    async def web_url_for_path(self, remote_path: str) -> str:
        """Best-effort open location in pCloud web UI."""
        path = remote_path if remote_path.startswith("/") else "/" + remote_path
        # stat file or folder
        try:
            data = await self._get("stat", path=path)
            meta = data.get("metadata") or {}
        except Exception:
            # parent folder
            parent = "/".join(path.rstrip("/").split("/")[:-1]) or "/"
            data = await self._get("stat", path=parent)
            meta = data.get("metadata") or {}
        folderid = meta.get("folderid") or meta.get("parentfolderid") or 0
        # pCloud web file manager by folder id
        return f"https://my.pcloud.com/#page=filemanager&folder={folderid}"
