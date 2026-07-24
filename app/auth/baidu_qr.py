from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

import httpx

_sessions: dict[str, "BaiduQRSession"] = {}
_SESSION_TTL = 600.0


def _purge_sessions() -> None:
    now = time.time()
    for sid, s in list(_sessions.items()):
        age = now - s.created_at
        if age > _SESSION_TTL:
            _sessions.pop(sid, None)
            continue
        if s.status in ("confirmed", "expired", "error") and age > 120:
            s.cookie = ""
            if age > 300:
                _sessions.pop(sid, None)


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class BaiduQRSession:
    id: str
    sign: str
    imgurl: str
    status: str = "pending"  # pending|scanned|confirmed|expired|error
    cookie: str = ""
    message: str = ""
    debug: str = ""
    created_at: float = field(default_factory=time.time)


def _merge_set_cookie(headers: httpx.Headers, jar: dict[str, str]) -> None:
    values: list[str] = []
    try:
        values = headers.get_list("set-cookie")  # type: ignore[attr-defined]
    except Exception:
        pass
    if not values:
        sc = headers.get("set-cookie")
        if sc:
            values = [sc]
    for raw in values:
        c = SimpleCookie()
        try:
            c.load(raw)
            for name, morsel in c.items():
                jar[name] = morsel.value
        except Exception:
            part = raw.split(";", 1)[0]
            if "=" in part:
                k, v = part.split("=", 1)
                jar[k.strip()] = v.strip()


def _jar_to_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def _parse_channel_v(channel_v) -> tuple[str | None, str | None]:
    """Return (status, bduss_candidate)."""
    if channel_v is None:
        return None, None
    if isinstance(channel_v, (int, float)):
        return str(int(channel_v)), None
    if isinstance(channel_v, str):
        s = channel_v.strip()
        if not s:
            return None, None
        # try JSON
        try:
            obj = json.loads(s)
            return _parse_channel_v(obj)
        except Exception:
            # plain bduss string
            if len(s) > 20 and "{" not in s:
                return "1", s
            return None, None
    if isinstance(channel_v, dict):
        status = channel_v.get("status")
        if status is not None:
            status = str(status)
        v = channel_v.get("v") or channel_v.get("BDUSS") or channel_v.get("bduss")
        if isinstance(v, dict):
            v = v.get("v") or v.get("BDUSS")
        if v is not None:
            v = str(v).strip() or None
        return status, v
    return None, None


async def start_qr() -> BaiduQRSession:
    _purge_sessions()
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}) as client:
        r = await client.get(
            "https://passport.baidu.com/v2/api/getqrcode",
            params={"lp": "pc", "apiver": "v3", "tpl": "netdisk"},
        )
        data = r.json()
        sign = data.get("sign") or (data.get("data") or {}).get("sign")
        imgurl = data.get("imgurl") or (data.get("data") or {}).get("imgurl")
        if not sign:
            raise RuntimeError(f"baidu getqrcode failed: {data}")
        if imgurl and str(imgurl).startswith("//"):
            imgurl = "https:" + imgurl
        elif imgurl and not str(imgurl).startswith("http"):
            imgurl = "https://" + str(imgurl).lstrip("/")
        # prefer full https qr image host
        if imgurl and "qrcode" not in str(imgurl) and data.get("imgurl"):
            pass
        sid = uuid.uuid4().hex
        sess = BaiduQRSession(id=sid, sign=str(sign), imgurl=str(imgurl or ""))
        _sessions[sid] = sess
        asyncio.create_task(_poll(sess))
        return sess


async def _complete_login(client: httpx.AsyncClient, sess: BaiduQRSession, bduss_v: str) -> bool:
    jar: dict[str, str] = {"BDUSS": bduss_v}
    login = await client.get(
        "https://passport.baidu.com/v3/login/main/qrbdusslogin",
        params={
            "v": int(time.time() * 1000),
            "bduss": bduss_v,
            "u": "https://pan.baidu.com/disk/home",
            "loginVersion": "v4",
            "qrcode": 1,
            "tpl": "netdisk",
            "apiver": "v3",
            "tt": int(time.time() * 1000),
        },
        headers={"User-Agent": UA, "Referer": "https://passport.baidu.com/"},
    )
    _merge_set_cookie(login.headers, jar)
    for name, value in login.cookies.items():
        jar[name] = value
    if "BDUSS" not in jar and "BDUSS_BFESS" in jar:
        jar["BDUSS"] = jar["BDUSS_BFESS"]
    if "BDUSS" not in jar:
        jar["BDUSS"] = bduss_v

    # enrich with pan cookies / STOKEN
    for url in (
        "https://pan.baidu.com/disk/main",
        "https://pan.baidu.com/api/loginStatus?clienttype=0",
    ):
        try:
            pr = await client.get(
                url,
                headers={
                    "Cookie": _jar_to_header(jar),
                    "User-Agent": UA,
                    "Referer": "https://pan.baidu.com/",
                },
            )
            _merge_set_cookie(pr.headers, jar)
            for name, value in pr.cookies.items():
                jar[name] = value
            if "STOKEN" not in jar and pr.text:
                m = re.search(r'"STOKEN"\s*:\s*"([^"]+)"', pr.text)
                if m:
                    jar["STOKEN"] = m.group(1)
        except Exception:
            pass

    raw = _jar_to_header(jar)
    if "BDUSS=" not in raw and "BDUSS_BFESS=" not in raw:
        sess.status = "error"
        sess.message = "手机已确认，但未能拿到 BDUSS。请改用下方手动粘贴 Cookie。"
        sess.cookie = raw
        return False

    sess.cookie = raw
    sess.status = "confirmed"
    sess.message = "登录成功" + ("（含 STOKEN）" if "STOKEN=" in raw else "（建议再手动补 STOKEN 更稳）")
    return True


async def _poll(sess: BaiduQRSession) -> None:
    deadline = time.time() + 180
    scanned_at: float | None = None
    # long poll for QR confirm — use long read timeout, not infinite (R3)
    _qr_to = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=_qr_to, headers={"User-Agent": UA}, follow_redirects=True) as client:
        while time.time() < deadline and sess.status in ("pending", "scanned"):
            try:
                # long-poll style: wait up to 25s for next event
                try:
                    r = await client.get(
                        "https://passport.baidu.com/channel/unicast",
                        params={
                            "channel_id": sess.sign,
                            "tpl": "netdisk",
                            "apiver": "v3",
                            "callback": "",
                            "_": int(time.time() * 1000),
                        },
                        timeout=25.0,
                    )
                except httpx.TimeoutException:
                    sess.debug = "unicast timeout (waiting confirm)"
                    continue

                text = (r.text or "").strip()
                # strip JSONP if any
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1]
                try:
                    data = r.json() if not text.startswith("(") else json.loads(text)
                except Exception:
                    try:
                        data = json.loads(text)
                    except Exception:
                        sess.debug = f"bad unicast body: {text[:120]}"
                        await asyncio.sleep(1)
                        continue

                errno = data.get("errno")
                sess.debug = f"errno={errno} raw={str(data)[:180]}"

                # errno 1 / -1 = still waiting
                if errno not in (0, "0"):
                    await asyncio.sleep(0.3)
                    continue

                status, bduss_v = _parse_channel_v(data.get("channel_v"))
                sess.debug = f"errno=0 status={status} has_v={bool(bduss_v)}"

                # scanned waiting for phone confirm
                if status in ("0", "0") and not bduss_v:
                    sess.status = "scanned"
                    if scanned_at is None:
                        scanned_at = time.time()
                    waited = int(time.time() - scanned_at)
                    if waited < 8:
                        sess.message = "已扫码，请在手机上点「确认登录」"
                    else:
                        sess.message = (
                            f"仍显示已扫码（{waited}s）。若手机已点确认仍不变，"
                            "请点「开始扫码」重来，或改用下方手动粘贴 Cookie（更稳）。"
                        )
                    await asyncio.sleep(0.5)
                    continue

                # confirmed: status 1/2 or any non-empty bduss
                if bduss_v or status in ("1", "2"):
                    if not bduss_v:
                        sess.message = "收到确认但无 BDUSS 字段，请重试扫码或手动 Cookie"
                        sess.status = "error"
                        return
                    sess.message = "手机已确认，正在完成登录…"
                    sess.status = "scanned"  # keep until cookie ready
                    ok = await _complete_login(client, sess, bduss_v)
                    if ok:
                        return
                    # failed complete — stop
                    return

                await asyncio.sleep(0.5)
            except Exception as e:
                sess.message = f"轮询异常: {e}"[:200]
                sess.debug = str(e)[:200]
                await asyncio.sleep(1.5)

        if sess.status not in ("confirmed",):
            sess.status = "expired"
            if not sess.message or "扫码" in sess.message:
                sess.message = "二维码已过期或未完成确认。请重新扫码，或手动粘贴 Cookie。"


def get_session(sid: str) -> BaiduQRSession | None:
    _purge_sessions()
    return _sessions.get(sid)
