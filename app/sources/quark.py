from __future__ import annotations

import random
import time
from typing import Any

import httpx

from app.sources.base import ResolvedShare, SourceFile


def _ts13() -> int:
    return int(time.time() * 1000)


def _dt() -> int:
    return random.randint(100, 9999)


class QuarkSource:
    def __init__(self, cookie: str) -> None:
        self.cookie = cookie.strip()
        self.headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "origin": "https://pan.quark.cn",
            "referer": "https://pan.quark.cn/",
            "accept-language": "zh-CN,zh;q=0.9",
            "cookie": self.cookie,
        }
        self._save_dir_fid = "0"

    def get_download_headers(self) -> dict[str, str]:
        return {
            "user-agent": self.headers["user-agent"],
            "origin": "https://pan.quark.cn",
            "referer": "https://pan.quark.cn/",
            "cookie": self.cookie,
        }

    @staticmethod
    def pwd_id(share_url: str) -> str:
        return share_url.split("?")[0].rstrip("/").split("/s/")[-1].split("#")[0]

    async def get_user_nickname(self) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(
                "https://pan.quark.cn/account/info",
                params={"fr": "pc", "platform": "pc"},
                headers=self.headers,
            )
            data = r.json()
            if data.get("data"):
                return data["data"].get("nickname") or "quark-user"
            raise RuntimeError(f"quark login invalid: {data.get('message') or data}")

    async def get_stoken(self, pwd_id: str, password: str = "") -> str:
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "__dt": _dt(), "__t": _ts13()}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token",
                params=params,
                json={"pwd_id": pwd_id, "passcode": password},
                headers=self.headers,
            )
            data = r.json()
            if data.get("status") == 200 and data.get("data"):
                return data["data"]["stoken"]
            raise RuntimeError(data.get("message") or "failed to get quark stoken")

    async def get_detail(
        self, pwd_id: str, stoken: str, pdir_fid: str = "0"
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return (is_owner, file_list). is_owner=1 means share is from own drive."""
        api = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail"
        page = 1
        file_list: list[dict[str, Any]] = []
        is_owner = 0
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                params = {
                    "pr": "ucpro",
                    "fr": "pc",
                    "uc_param_str": "",
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "pdir_fid": pdir_fid,
                    "force": "0",
                    "_page": str(page),
                    "_size": "50",
                    "_sort": "file_type:asc,updated_at:desc",
                    "__dt": _dt(),
                    "__t": _ts13(),
                }
                r = await client.get(api, params=params, headers=self.headers)
                data = r.json()
                if data.get("status") != 200:
                    raise RuntimeError(data.get("message") or "quark detail failed")
                is_owner = int((data.get("data") or {}).get("is_owner") or 0)
                items = data.get("data", {}).get("list") or []
                file_list.extend(items)
                meta = data.get("metadata") or {}
                total = int(meta.get("_total") or 0)
                size = int(meta.get("_size") or 50)
                count = int(meta.get("_count") or len(items))
                if total <= size or count < size or not items:
                    break
                page += 1
        return is_owner, file_list


    async def _create_subfolder(self, parent_fid: str, name: str) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://drive-pc.quark.cn/1/clouddrive/file",
                params={"pr": "ucpro", "fr": "pc", "uc_param_str": "", "__dt": _dt(), "__t": _ts13()},
                json={"pdir_fid": parent_fid, "file_name": name, "dir_path": "", "dir_init_lock": False},
                headers=self.headers,
            )
            data = r.json()
            if data.get("code") == 0:
                return data["data"]["fid"]
            if data.get("code") == 23008:
                # name conflict: list parent and find
                r2 = await client.get(
                    "https://drive-pc.quark.cn/1/clouddrive/file/sort",
                    params={
                        "pr": "ucpro",
                        "fr": "pc",
                        "uc_param_str": "",
                        "pdir_fid": parent_fid,
                        "_page": "1",
                        "_size": "100",
                        "_fetch_total": "1",
                        "_fetch_sub_dirs": "1",
                        "_sort": "file_type:asc,file_name:asc",
                        "__dt": _dt(),
                        "__t": _ts13(),
                    },
                    headers=self.headers,
                )
                for item in (r2.json().get("data") or {}).get("list") or []:
                    if item.get("dir") and item.get("file_name") == name:
                        return item["fid"]
            raise RuntimeError(data.get("message") or "create quark subfolder failed")

    async def ensure_bridge_folder(self) -> str:
        """Create /PanBridge-Temp under root if needed; return fid."""
        name = "PanBridge-Temp"
        async with httpx.AsyncClient(timeout=60) as client:
            # list root
            r = await client.get(
                "https://drive-pc.quark.cn/1/clouddrive/file/sort",
                params={
                    "pr": "ucpro",
                    "fr": "pc",
                    "uc_param_str": "",
                    "pdir_fid": "0",
                    "_page": "1",
                    "_size": "100",
                    "_fetch_total": "1",
                    "_fetch_sub_dirs": "1",
                    "_sort": "file_type:asc,file_name:asc",
                    "__dt": _dt(),
                    "__t": _ts13(),
                },
                headers=self.headers,
            )
            data = r.json()
            for item in data.get("data", {}).get("list") or []:
                if item.get("dir") and item.get("file_name") == name:
                    self._save_dir_fid = item["fid"]
                    return item["fid"]
            # create
            r2 = await client.post(
                "https://drive-pc.quark.cn/1/clouddrive/file",
                params={"pr": "ucpro", "fr": "pc", "uc_param_str": "", "__dt": _dt(), "__t": _ts13()},
                json={"pdir_fid": "0", "file_name": name, "dir_path": "", "dir_init_lock": False},
                headers=self.headers,
            )
            d2 = r2.json()
            if d2.get("code") == 0:
                self._save_dir_fid = d2["data"]["fid"]
                return self._save_dir_fid
            if d2.get("code") == 23008:
                # race: list again
                return await self.ensure_bridge_folder()
            raise RuntimeError(d2.get("message") or "create quark folder failed")

    async def save_share(
        self,
        pwd_id: str,
        stoken: str,
        fid_list: list[str],
        share_fid_token_list: list[str],
        to_pdir_fid: str,
    ) -> str:
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "__dt": _dt(), "__t": _ts13()}
        body = {
            "fid_list": fid_list,
            "fid_token_list": share_fid_token_list,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://drive.quark.cn/1/clouddrive/share/sharepage/save",
                params=params,
                json=body,
                headers=self.headers,
            )
            data = r.json()
            task_id = (data.get("data") or {}).get("task_id")
            if not task_id:
                raise RuntimeError(data.get("message") or "quark save task failed")
            # poll task
            for i in range(60):
                await _async_sleep(0.5 + random.random() * 0.5)
                tr = await client.get(
                    "https://drive-pc.quark.cn/1/clouddrive/task",
                    params={
                        "pr": "ucpro",
                        "fr": "pc",
                        "uc_param_str": "",
                        "task_id": task_id,
                        "retry_index": str(i),
                        "__dt": _dt(),
                        "__t": _ts13(),
                    },
                    headers=self.headers,
                )
                td = tr.json()
                if td.get("message") == "ok" and (td.get("data") or {}).get("status") == 2:
                    return task_id
                if td.get("code") and td.get("code") != 0:
                    msg = str(td.get("message") or td)
                    if "capacity" in msg.lower():
                        raise RuntimeError("quark capacity limit")
                    raise RuntimeError(msg)
                # status not complete but message may carry forbid
                msg = str(td.get("message") or "")
                if "禁止" in msg or "自己" in msg:
                    raise RuntimeError(msg)
            raise RuntimeError("quark save task timeout")

    async def get_download_urls(self, fids: list[str]) -> list[dict[str, Any]]:
        headers = dict(self.headers)
        headers["content-type"] = "application/json"
        params = {"pr": "ucpro", "fr": "pc", "sys": "win32", "ve": "2.5.56", "ut": "", "guid": ""}
        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in range(2):
                r = await client.post(
                    "https://drive-pc.quark.cn/1/clouddrive/file/download",
                    params=params,
                    json={"fids": fids},
                    headers=headers,
                )
                data = r.json()
                if data.get("code") == 23018:
                    headers["user-agent"] = (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) quark-cloud-drive/2.5.56 Chrome/100.0.4896.160 "
                        "Electron/18.3.5.12 Safari/537.36 Channel/pckk_other_ch"
                    )
                    continue
                if data.get("status") != 200:
                    raise RuntimeError(data.get("message") or "quark download list failed")
                return data.get("data") or []
        return []

    async def resolve(self, share_url: str, passcode: str = "") -> ResolvedShare:
        await self.get_user_nickname()
        pwd_id = self.pwd_id(share_url)
        stoken = await self.get_stoken(pwd_id, passcode)
        is_owner, top = await self.get_detail(pwd_id, stoken, "0")
        if not top:
            raise RuntimeError("share is empty or invalid")

        # flatten files recursively for folders in share
        flat: list[SourceFile] = []
        title_parts: list[str] = []

        async def walk(items: list[dict[str, Any]], prefix: str) -> None:
            for it in items:
                name = it.get("file_name") or "unknown"
                if it.get("dir"):
                    _own, children = await self.get_detail(pwd_id, stoken, it["fid"])
                    await walk(children, f"{prefix}{name}/")
                else:
                    flat.append(
                        SourceFile(
                            fid=it["fid"],
                            name=name,
                            size=int(it.get("size") or 0),
                            relative_path=prefix + name,
                            is_dir=False,
                            meta={
                                "share_fid_token": it.get("share_fid_token"),
                                "pdir_fid": it.get("pdir_fid"),
                            },
                        )
                    )
                    if not title_parts:
                        title_parts.append(name)

        # if single folder share, use folder name as title
        if len(top) == 1 and top[0].get("dir"):
            title = top[0].get("file_name") or "quark-share"
        else:
            title = title_parts[0] if title_parts else "quark-share"

        await walk(top, "")
        if not flat:
            raise RuntimeError("no downloadable files in share")

        # Own share: Quark forbids 转存自己的分享 — download with share fids directly
        if is_owner == 1:
            for f in flat:
                f.meta["owned_fid"] = f.fid
            return ResolvedShare(
                title=title,
                files=flat,
                meta={"pwd_id": pwd_id, "stoken": stoken, "is_owner": 1},
            )

        # Others' share: transfer into unique subfolder then map owned fids
        root = await self.ensure_bridge_folder()
        batch_name = f"job-{_ts13()}"
        to_dir = await self._create_subfolder(root, batch_name)
        fid_list = [i["fid"] for i in top]
        token_list = [i.get("share_fid_token") or "" for i in top]
        try:
            await self.save_share(pwd_id, stoken, fid_list, token_list, to_dir)
        except RuntimeError as e:
            msg = str(e)
            # fallback if API still says own-share forbidden
            if "自己" in msg or "禁止转存" in msg:
                for f in flat:
                    f.meta["owned_fid"] = f.fid
                return ResolvedShare(
                    title=title,
                    files=flat,
                    meta={"pwd_id": pwd_id, "stoken": stoken, "is_owner": 1},
                )
            raise

        owned = await self._list_tree(to_dir)
        by_rel: dict[str, dict] = {}
        for o in owned:
            by_rel[o["relative_path"]] = o
            by_rel[o["name"]] = o

        for f in flat:
            hit = by_rel.get(f.relative_path) or by_rel.get(f.name)
            if hit:
                f.meta["owned_fid"] = hit["fid"]
                f.size = hit.get("size") or f.size
            else:
                # last resort: try original fid
                f.meta["owned_fid"] = f.fid

        return ResolvedShare(
            title=title,
            files=flat,
            meta={"pwd_id": pwd_id, "stoken": stoken, "save_dir": to_dir, "is_owner": 0},
        )

    async def _list_tree(self, pdir_fid: str, prefix: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                r = await client.get(
                    "https://drive-pc.quark.cn/1/clouddrive/file/sort",
                    params={
                        "pr": "ucpro",
                        "fr": "pc",
                        "uc_param_str": "",
                        "pdir_fid": pdir_fid,
                        "_page": str(page),
                        "_size": "100",
                        "_fetch_total": "1",
                        "_fetch_sub_dirs": "1",
                        "_sort": "file_type:asc,file_name:asc",
                        "__dt": _dt(),
                        "__t": _ts13(),
                    },
                    headers=self.headers,
                )
                data = r.json()
                items = data.get("data", {}).get("list") or []
                for it in items:
                    name = it.get("file_name") or ""
                    rel = f"{prefix}{name}"
                    if it.get("dir"):
                        out.extend(await self._list_tree(it["fid"], rel + "/"))
                    else:
                        out.append({"fid": it["fid"], "name": name, "relative_path": rel, "size": int(it.get("size") or 0)})
                meta = data.get("metadata") or {}
                total = int(meta.get("_total") or 0)
                size = int(meta.get("_size") or 100)
                if page * size >= total or not items:
                    break
                page += 1
        return out

    async def prepare_download(self, file: SourceFile, share_meta: dict[str, Any]) -> str:
        fid = file.meta.get("owned_fid") or file.fid
        items = await self.get_download_urls([fid])
        if not items:
            raise RuntimeError(f"no download url for {file.name}")
        return items[0]["download_url"]


async def _async_sleep(sec: float) -> None:
    import asyncio

    await asyncio.sleep(sec)
