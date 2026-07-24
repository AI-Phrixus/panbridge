from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from app.sources.base import ResolvedShare, SourceFile


class BaiduSource:
    """Baidu share → transfer to own pan → dlink download."""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie.strip()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://pan.baidu.com/disk/main",
            "Cookie": self.cookie,
        }
        self._bdstoken: str | None = None
        self._extra_cookies: dict[str, str] = {}

    def _cookie_header(self) -> str:
        base = self.cookie
        if not self._extra_cookies:
            return base
        extra = "; ".join(f"{k}={v}" for k, v in self._extra_cookies.items())
        return f"{base}; {extra}" if base else extra

    def get_download_headers(self) -> dict[str, str]:
        # Baidu d.pcs.baidu.com rejects normal browser UA (error 31326 / hitcode 119).
        # UA "LogStatistic" is the well-known workaround used by open-source clients.
        return {
            "User-Agent": "LogStatistic",
            "Referer": "https://pan.baidu.com/disk/main",
            "Cookie": self._cookie_header(),
            "Accept": "*/*",
        }

    def _headers(self) -> dict[str, str]:
        h = dict(self.headers)
        h["Cookie"] = self._cookie_header()
        return h

    @staticmethod
    def extract_surl(share_url: str) -> str:
        m = re.search(r"/s/([A-Za-z0-9_-]+)", share_url)
        if m:
            return m.group(1)
        q = parse_qs(urlparse(share_url).query)
        if "surl" in q and q["surl"]:
            return q["surl"][0]
        raise ValueError("cannot parse baidu surl")

    @staticmethod
    def extract_pwd(share_url: str, passcode: str = "") -> str:
        if passcode:
            return passcode.strip()
        q = parse_qs(urlparse(share_url).query)
        if "pwd" in q and q["pwd"]:
            return q["pwd"][0].strip()
        m = re.search(r"(?:提取码|pwd|密码)[：:\s]*([A-Za-z0-9]{4})", share_url, re.I)
        return m.group(1) if m else ""

    @staticmethod
    def _short_forms(surl: str) -> list[str]:
        """Baidu APIs inconsistently want with/without leading '1'."""
        s = surl.strip()
        forms = []
        for x in (s, s[1:] if s.startswith("1") else "1" + s):
            if x and x not in forms:
                forms.append(x)
        return forms

    async def ensure_login(self) -> None:
        async with httpx.AsyncClient(timeout=60, headers=self._headers(), follow_redirects=True) as client:
            r3 = await client.get(
                "https://pan.baidu.com/api/gettemplatevariable",
                params={
                    "clienttype": 0,
                    "app_id": 250528,
                    "web": 1,
                    "fields": '["bdstoken","token","uk","isdocuser","servertime"]',
                },
            )
            try:
                d3 = r3.json()
                result = d3.get("result") or {}
                self._bdstoken = result.get("bdstoken") or d3.get("bdstoken")
            except Exception:
                pass
            if not self._bdstoken:
                home = await client.get("https://pan.baidu.com/disk/main")
                m = re.search(r'"bdstoken"\s*:\s*"([^"]+)"', home.text)
                if m:
                    self._bdstoken = m.group(1)
            if not self._bdstoken:
                has_bduss = "BDUSS=" in self.cookie or "BDUSS_BFESS=" in self.cookie
                if not has_bduss:
                    raise RuntimeError(
                        "百度 Cookie 缺少 BDUSS：请从 pan.baidu.com 已登录页复制完整 Cookie"
                    )
                raise RuntimeError("无法获取 bdstoken：Cookie 可能过期，请重新登录后复制")

    def _absorb_cookies(self, resp: httpx.Response) -> None:
        for k, v in resp.cookies.items():
            self._extra_cookies[k] = v
        # also parse set-cookie for BDCLND
        try:
            for sc in resp.headers.get_list("set-cookie"):  # type: ignore[attr-defined]
                part = sc.split(";", 1)[0]
                if "=" in part:
                    name, val = part.split("=", 1)
                    self._extra_cookies[name.strip()] = val.strip()
        except Exception:
            sc = resp.headers.get("set-cookie") or ""
            if "BDCLND=" in sc:
                m = re.search(r"BDCLND=([^;]+)", sc)
                if m:
                    self._extra_cookies["BDCLND"] = m.group(1)

    async def verify_share(self, surl: str, pwd: str) -> dict[str, Any]:
        await self.ensure_login()
        # normalize: bare surl without leading 1 for verify/init; path form prefers 1+surl
        bare = surl[1:] if surl.startswith("1") else surl
        path_surl = bare if bare.startswith("1") else "1" + bare

        async with httpx.AsyncClient(timeout=60, headers=self._headers(), follow_redirects=True) as client:
            # init page seeds cookies
            pr = await client.get(f"https://pan.baidu.com/share/init?surl={bare}")
            self._absorb_cookies(pr)

            verify_ok = not bool(pwd)  # no pwd => skip verify
            last_verify: dict[str, Any] = {}
            if pwd:
                r = await client.post(
                    "https://pan.baidu.com/share/verify",
                    params={
                        "surl": bare,
                        "t": str(int(time.time() * 1000)),
                        "channel": "chunlei",
                        "web": 1,
                        "app_id": 250528,
                        "clienttype": 0,
                    },
                    data={"pwd": pwd, "vcode": "", "vcode_str": ""},
                    headers={
                        **self._headers(),
                        "Referer": f"https://pan.baidu.com/share/init?surl={bare}",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    },
                )
                self._absorb_cookies(r)
                last_verify = r.json()
                if last_verify.get("errno") == 0:
                    verify_ok = True
                    # Keep BDCLND exactly as cookie jar / randsk from server
                    if "BDCLND" not in self._extra_cookies:
                        randsk = last_verify.get("randsk")
                        if randsk:
                            self._extra_cookies["BDCLND"] = str(randsk)
                if not verify_ok:
                    raise RuntimeError(
                        f"百度提取码验证失败: {last_verify.get('show_msg') or last_verify} "
                        f"(pwd={pwd!r})"
                    )

            # After verify, open /s/1xxxx (leading 1 is required for many links)
            page_text = ""
            for form in (path_surl, bare, "1" + bare if not bare.startswith("1") else bare):
                page = await client.get(
                    f"https://pan.baidu.com/s/{form}",
                    headers={**self._headers(), "Referer": f"https://pan.baidu.com/share/init?surl={bare}"},
                )
                self._absorb_cookies(page)
                page_text = page.text or ""
                if page.status_code == 200 and ("shareid" in page_text or "share_uk" in page_text):
                    break

            shareid = None
            uk = None
            m = re.search(r'"shareid"\s*:\s*"?(\d+)"?', page_text)
            if m:
                shareid = m.group(1)
            m = re.search(r'"share_uk"\s*:\s*"?(\d+)"?', page_text) or re.search(
                r'"uk"\s*:\s*"?(\d+)"?', page_text
            )
            if m:
                uk = m.group(1)

            # try list APIs (BUG-8: paginate beyond first 100)
            last_err: Any = None
            list_data: dict[str, Any] | None = None

            async def _list_all(base_params: dict[str, Any]) -> dict[str, Any] | None:
                page = 1
                all_items: list[Any] = []
                first: dict[str, Any] | None = None
                while page <= 50:  # hard cap 5000 entries
                    params = {**base_params, "page": page, "num": 100}
                    r = await client.get(
                        "https://pan.baidu.com/share/list",
                        params=params,
                        headers=self._headers(),
                    )
                    d = r.json()
                    if d.get("errno") != 0:
                        return first if first else None
                    if first is None:
                        first = dict(d)
                    batch = d.get("list") or []
                    all_items.extend(batch)
                    if len(batch) < 100:
                        break
                    page += 1
                if first is not None:
                    first["list"] = all_items
                return first

            # 1) shareid + uk
            if shareid and uk:
                d = await _list_all(
                    {
                        "shareid": shareid,
                        "uk": uk,
                        "root": 1,
                        "channel": "chunlei",
                        "web": 1,
                        "clienttype": 0,
                        "app_id": 250528,
                        "order": "other",
                        "desc": 1,
                        "showempty": 0,
                    }
                )
                if d is not None:
                    list_data = d
                else:
                    last_err = {"errno": -1, "show_msg": "share list failed"}

            # 2) shorturl forms
            if list_data is None:
                for form in self._short_forms(surl):
                    for short in (form if form.startswith("1") else "1" + form, form):
                        d = await _list_all(
                            {
                                "shorturl": short,
                                "root": 1,
                                "channel": "chunlei",
                                "web": 1,
                                "clienttype": 0,
                                "app_id": 250528,
                            }
                        )
                        if d is not None:
                            list_data = d
                            shareid = str(d.get("share_id") or d.get("shareid") or shareid or "")
                            uk = str(d.get("uk") or d.get("share_uk") or uk or "")
                            break
                        last_err = d
                    if list_data is not None:
                        break

            # 3) wxlist
            if list_data is None:
                for form in self._short_forms(surl):
                    short = form[1:] if form.startswith("1") else form
                    r = await client.get(
                        "https://pan.baidu.com/share/wxlist",
                        params={
                            "channel": "weixin",
                            "version": "2.2.2",
                            "clienttype": 25,
                            "web": 1,
                            "shorturl": short,
                        },
                        headers=self._headers(),
                    )
                    try:
                        d = r.json()
                    except Exception:
                        continue
                    if d.get("errno") == 0 and (d.get("data") or d.get("list")):
                        data = d.get("data") or d
                        list_data = {
                            "list": data.get("list") or d.get("list") or [],
                            "share_id": data.get("shareid") or data.get("share_id"),
                            "uk": data.get("uk") or data.get("share_uk"),
                            "title": data.get("title") or "baidu-share",
                        }
                        shareid = str(list_data.get("share_id") or "")
                        uk = str(list_data.get("uk") or "")
                        break
                    last_err = d

            if list_data is None:
                msg = ""
                if isinstance(last_err, dict):
                    msg = last_err.get("show_msg") or str(last_err)
                raise RuntimeError(
                    f"百度分享列表失败: {msg or last_err}。"
                    "请确认链接未过期、提取码正确，并在浏览器能打开该分享。"
                )

            files = list_data.get("list") or []
            if not shareid:
                shareid = str(list_data.get("share_id") or list_data.get("shareid") or "")
            if not uk:
                uk = str(list_data.get("uk") or list_data.get("share_uk") or "")

            return {
                "shareid": shareid,
                "uk": uk,
                "list": files,
                "title": list_data.get("title") or "baidu-share",
                "sekey": self._extra_cookies.get("BDCLND", ""),
            }

    async def transfer(self, shareid: str, uk: str, fsids: list[int], path: str = "/PanBridge-Temp") -> None:
        await self.ensure_login()
        async with httpx.AsyncClient(timeout=60, headers=self._headers(), follow_redirects=True) as client:
            await client.post(
                "https://pan.baidu.com/api/create",
                params={
                    "a": "commit",
                    "channel": "chunlei",
                    "web": 1,
                    "app_id": 250528,
                    "clienttype": 0,
                    "bdstoken": self._bdstoken,
                },
                data={"path": path, "isdir": 1, "block_list": "[]"},
            )
            r = await client.post(
                "https://pan.baidu.com/share/transfer",
                params={
                    "shareid": shareid,
                    "from": uk,
                    "sekey": unquote(self._extra_cookies.get("BDCLND", "")),
                    "channel": "chunlei",
                    "web": 1,
                    "app_id": 250528,
                    "clienttype": 0,
                    "bdstoken": self._bdstoken,
                },
                data={
                    "fsidlist": "[" + ",".join(str(i) for i in fsids) + "]",
                    "path": path,
                },
                headers={
                    **self._headers(),
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": "https://pan.baidu.com/s/",
                },
            )
            data = r.json()
            if data.get("errno") not in (0, 4, -30):  # -30 sometimes partial
                if data.get("errno") != 0:
                    raise RuntimeError(f"baidu transfer failed: {data}")

    async def list_owned(self, dir_path: str = "/PanBridge-Temp") -> list[dict[str, Any]]:
        await self.ensure_login()
        out: list[dict[str, Any]] = []

        async def walk(path: str, prefix: str = "") -> None:
            async with httpx.AsyncClient(timeout=60, headers=self._headers()) as client:
                page = 1
                while True:
                    r = await client.get(
                        "https://pan.baidu.com/api/list",
                        params={
                            "dir": path,
                            "order": "name",
                            "desc": 0,
                            "showempty": 0,
                            "page": page,
                            "num": 100,
                            "channel": "chunlei",
                            "web": 1,
                            "app_id": 250528,
                            "clienttype": 0,
                        },
                    )
                    data = r.json()
                    if data.get("errno") != 0:
                        break
                    items = data.get("list") or []
                    for it in items:
                        name = it.get("server_filename") or ""
                        rel = f"{prefix}{name}"
                        if it.get("isdir"):
                            await walk(it["path"], rel + "/")
                        else:
                            out.append(
                                {
                                    "fs_id": it["fs_id"],
                                    "name": name,
                                    "path": it["path"],
                                    "relative_path": rel,
                                    "size": int(it.get("size") or 0),
                                }
                            )
                    if len(items) < 100:
                        break
                    page += 1

        await walk(dir_path)
        return out

    async def get_dlink(self, fs_id: int) -> str:
        await self.ensure_login()
        async with httpx.AsyncClient(timeout=60, headers=self._headers(), follow_redirects=False) as client:
            r = await client.get(
                "https://pan.baidu.com/api/filemetas",
                params={
                    "dlink": 1,
                    "fsids": f"[{fs_id}]",
                    "channel": "chunlei",
                    "web": 1,
                    "app_id": 250528,
                    "clienttype": 0,
                },
            )
            data = r.json()
            if data.get("errno") != 0:
                raise RuntimeError(f"baidu filemetas failed: {data}")
            infos = data.get("info") or data.get("list") or []
            if not infos:
                raise RuntimeError("no filemetas")
            dlink = infos[0].get("dlink")
            if not dlink:
                raise RuntimeError("no dlink")
            if dlink.startswith("//"):
                dlink = "https:" + dlink
            return dlink

    async def resolve(self, share_url: str, passcode: str = "") -> ResolvedShare:
        await self.ensure_login()
        surl = self.extract_surl(share_url)
        pwd = self.extract_pwd(share_url, passcode)
        info = await self.verify_share(surl, pwd)
        items = info.get("list") or []
        if not items:
            raise RuntimeError("百度分享为空或链接无效/已过期")

        # top-level fsids for transfer (files + folders)
        fsids = [int(i["fs_id"]) for i in items if i.get("fs_id") is not None]
        if not fsids:
            raise RuntimeError("分享中没有可转存的文件")

        import time as _t

        batch = f"/PanBridge-Temp/job-{int(_t.time() * 1000)}"
        await self.transfer(str(info["shareid"]), str(info["uk"]), fsids, path=batch)

        owned = await self.list_owned(batch)
        files: list[SourceFile] = []
        for o in owned:
            files.append(
                SourceFile(
                    fid=str(o["fs_id"]),
                    name=o["name"],
                    size=int(o["size"]),
                    relative_path=o["relative_path"],
                    meta={"fs_id": o["fs_id"], "path": o["path"]},
                )
            )
        if not files:
            # transfer may place at root of batch as same names
            for i in items:
                if i.get("isdir"):
                    continue
                name = i.get("server_filename") or "file"
                files.append(
                    SourceFile(
                        fid=str(i.get("fs_id")),
                        name=name,
                        size=int(i.get("size") or 0),
                        relative_path=name,
                        meta={"fs_id": int(i.get("fs_id"))},
                    )
                )
        if not files:
            raise RuntimeError("转存后未找到文件，请到百度网盘查看 /PanBridge-Temp")

        title = info.get("title") or files[0].name
        return ResolvedShare(
            title=title,
            files=files,
            meta={"shareid": info.get("shareid"), "uk": info.get("uk")},
        )

    async def prepare_download(self, file: SourceFile, share_meta: dict[str, Any]) -> str:
        fs_id = int(file.meta.get("fs_id") or file.fid)
        path = file.meta.get("path") or ""
        # Prefer filemetas dlink (works with UA LogStatistic)
        try:
            return await self.get_dlink(fs_id)
        except Exception:
            if not path:
                raise
        # Fallback: PCS locatedownload
        async with httpx.AsyncClient(timeout=60, headers=self._headers()) as client:
            r = await client.post(
                "https://pcs.baidu.com/rest/2.0/pcs/file",
                params={"method": "locatedownload", "app_id": 250528, "path": path},
                headers={"User-Agent": "netdisk;P2SP;2.2.60.26", "Cookie": self._cookie_header()},
            )
            data = r.json()
            urls = data.get("urls") or []
            if not urls:
                raise RuntimeError(f"baidu locatedownload failed: {data}")
            return urls[0]["url"]
