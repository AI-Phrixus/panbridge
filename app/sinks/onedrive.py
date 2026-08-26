from __future__ import annotations

import asyncio
import hashlib
import logging
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from app.auth.onedrive_auth import refresh_access_token

# type alias used in __init__

log = logging.getLogger("panbridge.onedrive")

GRAPH = "https://graph.microsoft.com/v1.0"
# 5 MiB chunks (Graph requires multiple of 320 KiB; 5MiB = 16*320KiB)
CHUNK = 5 * 1024 * 1024
_TIMEOUT = httpx.Timeout(connect=30.0, read=180.0, write=180.0, pool=30.0)

# OneDrive/SharePoint rejects these characters even when they are percent
# encoded in a Graph path. Use visually similar full-width characters so the
# delivered name remains understandable. A stable hash marker is appended
# whenever a name changes, preventing distinct source names from colliding.
_ONEDRIVE_REPLACEMENTS = str.maketrans(
    {
        '"': "＂",
        "*": "＊",
        ":": "：",
        "<": "＜",
        ">": "＞",
        "?": "？",
        "/": "／",
        "\\": "＼",
        "|": "｜",
        # Some OneDrive for Business tenants still reject these two.
        "#": "＃",
        "%": "％",
    }
)
_ONEDRIVE_RESERVED = {
    ".lock",
    "con",
    "prn",
    "aux",
    "nul",
    "desktop.ini",
    "forms",
    *(f"com{i}" for i in range(10)),
    *(f"lpt{i}" for i in range(10)),
}
_ONEDRIVE_MAX_PATH = 380  # Microsoft limit is 400 decoded characters; keep margin.


class OneDrivePermanentUploadError(RuntimeError):
    """A retry cannot fix the current Graph upload request."""


def _name_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:10]


def _fit_changed_name(
    value: str,
    original: str,
    max_length: int,
    *,
    preserve_extension: bool,
) -> str:
    """Fit a changed name while retaining its extension and collision marker."""
    marker = f" [pb-{_name_hash(original)}]"
    suffix = ""
    stem = value
    if preserve_extension:
        candidate = Path(value).suffix
        # Treat only ordinary short suffixes as extensions. A long final dotted
        # phrase is part of the title, not a useful extension.
        if candidate and len(candidate) <= 24 and len(candidate) < len(value):
            suffix = candidate
            stem = value[: -len(candidate)]
    suffix_budget = max(0, max_length - len(marker) - 1)
    if len(suffix) > suffix_budget:
        suffix = suffix[:suffix_budget]
    stem_budget = max(1, max_length - len(marker) - len(suffix))
    fitted = stem[:stem_budget].rstrip(" .") or "檔案"
    return (fitted + marker + suffix)[:max_length]


def sanitize_onedrive_segment(
    name: str,
    *,
    max_length: int = 180,
    preserve_extension: bool = False,
) -> str:
    """Return a deterministic OneDrive-safe file or folder name.

    The original source name stays in PanBridge's database/UI. Only the
    delivered OneDrive name is normalized.
    """
    original = str(name or "")
    value = unicodedata.normalize("NFC", original).translate(_ONEDRIVE_REPLACEMENTS)
    value = "".join(
        char if ord(char) >= 0x20 and ord(char) != 0x7F else "＿"
        for char in value
    )
    value = value.strip()
    while value.endswith("."):
        value = value[:-1] + "．"
    if value.startswith("~"):
        value = "～" + value[1:]
    if value.startswith(("゛", "ဧ")):
        value = "＿" + value
    if "_vti_" in value.lower():
        # Preserve surrounding text and neutralize every case-insensitive hit.
        lower = value.lower()
        rebuilt: list[str] = []
        cursor = 0
        while True:
            index = lower.find("_vti_", cursor)
            if index < 0:
                rebuilt.append(value[cursor:])
                break
            rebuilt.append(value[cursor:index])
            rebuilt.append("＿vti＿")
            cursor = index + 5
        value = "".join(rebuilt)
    check_name = value.lower()
    check_stem = Path(value).stem.lower()
    check_first_component = value.split(".", 1)[0].lower()
    if (
        check_name in _ONEDRIVE_RESERVED
        or check_stem in _ONEDRIVE_RESERVED
        or check_first_component in _ONEDRIVE_RESERVED
        or value.startswith("~$")
    ):
        value = "＿" + value
    if not value or value in {".", ".."}:
        value = "未命名"

    changed = value != original or len(value) > max_length
    if changed:
        value = _fit_changed_name(
            value,
            original,
            max_length,
            preserve_extension=preserve_extension,
        )
    return value[:max_length]


def normalize_onedrive_target(
    folder_path: str,
    filename: str,
) -> tuple[str, str]:
    """Normalize a complete upload target and keep it below OneDrive limits."""
    # PanBridge's remote paths always use '/'. A backslash is a legal source
    # character but an invalid OneDrive name character, so keep it inside the
    # segment and sanitize it instead of accidentally turning it into another
    # directory level.
    raw_parts = [part for part in str(folder_path or "").split("/") if part]
    safe_parts = [sanitize_onedrive_segment(part, max_length=110) for part in raw_parts]
    safe_filename = sanitize_onedrive_segment(
        filename,
        max_length=180,
        preserve_extension=True,
    )
    leading_slash = str(folder_path or "").startswith("/")

    def joined_length(parts: list[str]) -> int:
        return len("/".join([*parts, safe_filename]))

    if joined_length(safe_parts) > _ONEDRIVE_MAX_PATH:
        # Preserve the recognizable top-level folder and collapse the overly
        # deep remainder to a stable unique segment.
        path_hash = _name_hash("/".join(raw_parts))
        collapsed = f"縮短路徑 [pb-{path_hash}]"
        safe_parts = [safe_parts[0], collapsed] if safe_parts else [collapsed]
    if joined_length(safe_parts) > _ONEDRIVE_MAX_PATH:
        available = max(24, _ONEDRIVE_MAX_PATH - len(safe_filename) - 1)
        safe_parts = [
            sanitize_onedrive_segment("/".join(raw_parts), max_length=available)
        ]

    safe_folder = "/".join(safe_parts)
    if leading_slash and safe_folder:
        safe_folder = "/" + safe_folder
    return safe_folder, safe_filename


def _onedrive_name_key(value: str) -> str:
    """Approximate OneDrive's case-insensitive, Unicode-normalized comparison."""
    return unicodedata.normalize("NFKC", str(value or "")).casefold().rstrip(" .")


class OneDriveSink:
    def __init__(
        self,
        access_token: str,
        refresh_token: str = "",
        client_id: str = "",
        on_tokens: Callable[[str, str], Awaitable[None]] | None = None,
        refresh_cb: Callable[[], Awaitable[tuple[str, str]]] | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self._on_tokens = on_tokens  # persist rotated tokens (BUG-10)
        self._refresh_cb = refresh_cb
        self._last_ensured_folder_path = ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _refresh(self) -> bool:
        if self._refresh_cb:
            try:
                access, refresh = await self._refresh_cb()
                self.access_token = access
                self.refresh_token = refresh
                return True
            except Exception as e:
                log.warning("onedrive coordinated token refresh failed: %s", e)
                return False
        if not (self.refresh_token and self.client_id):
            return False
        try:
            tok = await refresh_access_token(self.client_id, self.refresh_token)
            self.access_token = tok["access_token"]
            if tok.get("refresh_token"):
                self.refresh_token = tok["refresh_token"]
            if self._on_tokens:
                # A generation-aware callback deliberately rejects tokens from
                # an old login. Treat that as refresh failure instead of using
                # the wrong account silently.
                await self._on_tokens(self.access_token, self.refresh_token)
            return True
        except Exception as e:
            log.warning("onedrive token refresh failed: %s", e)
            return False

    async def _request(
        self,
        method: str,
        url: str,
        *,
        follow_redirects: bool = True,
        **kwargs,
    ) -> httpx.Response:
        extra_headers = dict(kwargs.pop("headers", None) or {})
        headers = {**self._headers(), **extra_headers}
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=follow_redirects
        ) as client:
            r = await client.request(method, url, headers=headers, **kwargs)
            if r.status_code == 401 and await self._refresh():
                headers = {**self._headers(), **extra_headers}
                r = await client.request(method, url, headers=headers, **kwargs)
            return r

    async def space_info(self) -> dict[str, int]:
        r = await self._request("GET", f"{GRAPH}/me/drive")
        if r.status_code >= 400:
            raise RuntimeError(f"onedrive drive info: {r.status_code} {r.text[:200]}")
        data = r.json()
        q = data.get("quota") or {}
        total = int(q.get("total") or 0)
        used = int(q.get("used") or 0)
        remaining = int(q.get("remaining") or max(0, total - used))
        return {"quota": total, "used": used, "free": remaining}

    async def download_info_for_item(self, item_id: str) -> dict[str, Any]:
        """Return a fresh, pre-authenticated Graph download URL for an item."""
        safe_id = quote(str(item_id), safe="")
        r = await self._request(
            "GET",
            f"{GRAPH}/me/drive/items/{safe_id}",
            params={"$select": "id,name,size,file,parentReference"},
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"onedrive download link: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        drive_id = str((data.get("parentReference") or {}).get("driveId") or "")
        if not drive_id:
            raise RuntimeError("OneDrive 未返回檔案所屬 Drive ID")
        # Personal OneDrive no longer consistently returns the instance
        # annotation even when it is selected. Graph's documented /content
        # endpoint always returns the same short-lived URL in a 302 Location.
        download_url = await self._download_redirect_for_item(safe_id)
        parsed = httpx.URL(download_url)
        host = str(parsed.host or "").lower().rstrip(".")
        microsoft_download_host = any(
            host == suffix or host.endswith("." + suffix)
            for suffix in ("1drv.com", "sharepoint.com", "sharepoint.cn")
        )
        if (
            parsed.scheme != "https"
            or not microsoft_download_host
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
        ):
            raise RuntimeError("OneDrive 未返回安全的下載網址")
        file_meta = data.get("file") or {}
        return {
            "url": download_url,
            "name": str(data.get("name") or ""),
            "size": int(data.get("size") or 0),
            "content_type": str(
                file_meta.get("mimeType") or "application/octet-stream"
            ),
            "drive_id": drive_id,
        }

    async def _download_redirect_for_item(self, safe_id: str) -> str:
        """Read only Graph's 302 headers; never buffer the file response body."""
        url = f"{GRAPH}/me/drive/items/{safe_id}/content"
        for attempt in range(2):
            headers = self._headers()
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=False
            ) as client:
                request = client.build_request("GET", url, headers=headers)
                response = await client.send(request, stream=True)
                try:
                    if response.status_code == 401 and attempt == 0:
                        if await self._refresh():
                            continue
                    if response.status_code != 302:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            remaining = 200 - len(body)
                            if remaining <= 0:
                                break
                            body.extend(chunk[:remaining])
                            if len(body) >= 200:
                                break
                        raise RuntimeError(
                            "onedrive content redirect: "
                            f"{response.status_code} {bytes(body)!r}"
                        )
                    return str(response.headers.get("location") or "")
                finally:
                    await response.aclose()
        raise RuntimeError("OneDrive token 失效，請重新登入")

    async def ensure_folder_path(self, path: str) -> str:
        """Create nested folders under root; return item id of final folder."""
        path, _ = normalize_onedrive_target(path, "檔案")
        leading_slash = path.startswith("/")
        path = path.strip("/")
        if not path:
            self._last_ensured_folder_path = ""
            r = await self._request("GET", f"{GRAPH}/me/drive/root")
            return r.json()["id"]
        parent = "root"
        resolved_parts: list[str] = []
        for part in path.split("/"):
            if not part:
                continue
            parent, actual_name = await self._ensure_child_folder_target(parent, part)
            resolved_parts.append(actual_name)
        self._last_ensured_folder_path = (
            ("/" if leading_slash else "") + "/".join(resolved_parts)
        )
        return parent

    async def _ensure_child_folder(self, parent_id: str, name: str) -> str:
        item_id, _actual_name = await self._ensure_child_folder_target(parent_id, name)
        return item_id

    async def _ensure_child_folder_target(
        self, parent_id: str, name: str
    ) -> tuple[str, str]:
        name = sanitize_onedrive_segment(name, max_length=110)
        if parent_id == "root":
            list_url = f"{GRAPH}/me/drive/root/children"
            create_url = f"{GRAPH}/me/drive/root/children"
        else:
            list_url = f"{GRAPH}/me/drive/items/{parent_id}/children"
            create_url = f"{GRAPH}/me/drive/items/{parent_id}/children"
        # Paginate children (folders with many files). OneDrive compares names
        # case-insensitively and with Unicode compatibility normalization. Reuse
        # only an exact name; otherwise derive a stable alternate instead of
        # merging distinct source folders such as "Movie" and "movie".
        existing_folders: list[dict[str, Any]] = []
        next_url: str | None = list_url
        params: dict[str, str] | None = {"$select": "id,name,folder", "$top": "200"}
        while next_url:
            if next_url == list_url:
                r = await self._request("GET", next_url, params=params)
            else:
                r = await self._request("GET", next_url)
            if r.status_code >= 400:
                break
            data = r.json()
            for it in data.get("value") or []:
                if "folder" in it:
                    existing_folders.append(it)
            next_url = data.get("@odata.nextLink")
            params = None

        for it in existing_folders:
            if str(it.get("name") or "") == name:
                return it["id"], name
        if any(
            _onedrive_name_key(it.get("name")) == _onedrive_name_key(name)
            for it in existing_folders
        ):
            name = _fit_changed_name(
                name,
                f"onedrive-folder-collision:{name}",
                110,
                preserve_extension=False,
            )
            for it in existing_folders:
                if str(it.get("name") or "") == name:
                    return it["id"], name

        r2 = await self._request(
            "POST",
            create_url,
            headers={"Content-Type": "application/json"},
            json={
                "name": name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            },
        )
        if r2.status_code in (200, 201):
            created = r2.json()
            return created["id"], str(created.get("name") or name)
        if r2.status_code in (409, 400):
            # paginate conflict re-list (BUG-14)
            next_url2: str | None = list_url
            params2: dict[str, str] | None = {"$select": "id,name,folder", "$top": "200"}
            while next_url2:
                if next_url2 == list_url:
                    r3 = await self._request("GET", next_url2, params=params2)
                else:
                    r3 = await self._request("GET", next_url2)
                if r3.status_code >= 400:
                    break
                data3 = r3.json()
                for it in data3.get("value") or []:
                    if str(it.get("name") or "") == name and "folder" in it:
                        return it["id"], name
                next_url2 = data3.get("@odata.nextLink")
                params2 = None
        raise RuntimeError(f"create folder {name}: {r2.status_code} {r2.text[:200]}")

    async def upload_file(
        self,
        local_path: Path,
        remote_folder_path: str,
        filename: str,
        progress_cb: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        size = local_path.stat().st_size
        safe_folder, safe_filename = normalize_onedrive_target(
            remote_folder_path, filename
        )
        # Fallback remains the requested target when a test double or future
        # backend overrides ensure_folder_path without resolving names.
        self._last_ensured_folder_path = safe_folder
        folder_id = await self.ensure_folder_path(safe_folder)
        actual_folder = self._last_ensured_folder_path or safe_folder

        # Graph upload sessions support rename-on-conflict. Use them for every
        # non-empty file, including small files, so a case/Unicode-equivalent
        # name can never silently replace an unrelated OneDrive item. Personal
        # OneDrive cannot portably commit a zero-byte upload session; fail
        # closed instead of using simple PUT, which could overwrite an existing
        # non-empty file with the same name.
        if size == 0:
            raise OneDrivePermanentUploadError(
                "OneDrive 安全上傳拒絕空檔：無法在不覆蓋同名項目的情況下提交 0 bytes"
            )

        # Upload sessions are also used for small files to guarantee the same
        # conflict and integrity semantics at every size.
        sess_url = (
            f"{GRAPH}/me/drive/items/{folder_id}:/{quote(safe_filename, safe='')}"
            ":/createUploadSession"
        )
        r = await self._request(
            "POST",
            sess_url,
            headers={"Content-Type": "application/json"},
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": "rename",
                    "name": safe_filename,
                }
            },
        )
        if r.status_code >= 400:
            error = f"createUploadSession: {r.status_code} {r.text[:300]}"
            if r.status_code == 400:
                raise OneDrivePermanentUploadError(error)
            raise RuntimeError(error)
        upload_url = r.json()["uploadUrl"]

        sent = 0
        final_item: dict[str, Any] | None = None
        with open(local_path, "rb") as f:
            while sent < size:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                start = sent
                end = sent + len(chunk) - 1
                headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                }
                last_err: Exception | None = None
                for attempt in range(8):
                    try:
                        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                            ur = await client.put(upload_url, content=chunk, headers=headers)
                        if ur.status_code in (200, 201, 202):
                            sent = end + 1
                            if progress_cb:
                                await progress_cb(sent, size)
                            if ur.status_code in (200, 201):
                                try:
                                    final_item = ur.json()
                                except Exception:
                                    raise RuntimeError(
                                        "OneDrive 完成上傳但未返回可核對的檔案資料"
                                    )
                                if (
                                    not isinstance(final_item, dict)
                                    or "size" not in final_item
                                ):
                                    raise RuntimeError(
                                        "OneDrive 完成上傳但未返回可核對的檔案資料"
                                    )
                                remote_size = int(final_item.get("size") or 0)
                                remote_id = str(final_item.get("id") or "")
                                if not remote_id or remote_size != size:
                                    raise RuntimeError(
                                        "OneDrive 上傳後資料不符: "
                                        f"id={bool(remote_id)} local={size} "
                                        f"remote={remote_size}"
                                    )
                                final_item = dict(final_item)
                                actual_name = str(
                                    final_item.get("name") or safe_filename
                                )
                                final_item["_panbridge_remote_folder"] = actual_folder
                                final_item["_panbridge_remote_name"] = actual_name
                                return final_item
                            last_err = None
                            break
                        if ur.status_code in (404, 410):
                            raise RuntimeError(
                                f"upload session expired HTTP {ur.status_code}; will retry file upload"
                            )
                        last_err = RuntimeError(f"chunk upload {ur.status_code}: {ur.text[:300]}")
                        if ur.status_code in (429, 500, 502, 503, 504) or ur.status_code >= 500:
                            await asyncio.sleep(min(30, 1.5 * (attempt + 1)))
                            continue
                        raise last_err
                    except (httpx.TimeoutException, httpx.TransportError, OSError) as e:
                        last_err = e
                        log.warning(
                            "onedrive chunk %s-%s attempt %s: %s",
                            start,
                            end,
                            attempt + 1,
                            e,
                        )
                        await asyncio.sleep(min(30, 1.5 * (attempt + 1)))
                if last_err is not None and sent <= start:
                    raise RuntimeError(f"onedrive chunk failed after retries: {last_err}") from last_err

        # BUG-1: never treat "all 202s" as success without Drive item
        if sent < size:
            raise RuntimeError(f"onedrive upload incomplete: {sent}/{size} bytes")
        raise RuntimeError(
            "onedrive upload finished bytes but never received 200/201 item metadata; retry session"
        )

    async def root_web_url(self) -> str:
        r = await self._request("GET", f"{GRAPH}/me/drive/root")
        if r.status_code >= 400:
            raise RuntimeError(f"onedrive root: {r.status_code}")
        return (r.json().get("webUrl") or "https://onedrive.live.com/").strip()

    async def web_url_for_path(self, remote_path: str) -> str:
        """Return OneDrive webUrl for file path; fall back to parent folder or root."""
        path = remote_path.strip().lstrip("/")
        if not path:
            return await self.root_web_url()
        r = await self._request("GET", f"{GRAPH}/me/drive/root:/{quote(path, safe='/')}:")
        if r.status_code < 400:
            return (r.json().get("webUrl") or "").strip() or await self.root_web_url()
        parent = str(Path(path).parent).replace("\\", "/").strip(".")
        if parent and parent not in (".", path):
            r2 = await self._request("GET", f"{GRAPH}/me/drive/root:/{quote(parent, safe='/')}:")
            if r2.status_code < 400:
                return (r2.json().get("webUrl") or "").strip() or await self.root_web_url()
        return await self.root_web_url()

    async def web_url_for_folder_path(self, remote_folder_path: str) -> str:
        safe_folder, _ = normalize_onedrive_target(remote_folder_path, "檔案")
        path = safe_folder.strip().lstrip("/")
        if not path:
            return await self.root_web_url()
        r = await self._request("GET", f"{GRAPH}/me/drive/root:/{quote(path, safe='/')}:")
        if r.status_code < 400:
            return (r.json().get("webUrl") or "").strip() or await self.root_web_url()
        try:
            await self.ensure_folder_path(path)
            resolved_path = self._last_ensured_folder_path.strip().lstrip("/") or path
            r3 = await self._request(
                "GET",
                f"{GRAPH}/me/drive/root:/{quote(resolved_path, safe='/')}:",
            )
            if r3.status_code < 400:
                return (r3.json().get("webUrl") or "").strip() or await self.root_web_url()
        except Exception:
            pass
        return await self.root_web_url()
