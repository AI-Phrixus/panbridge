from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
import uuid
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from urllib.parse import quote

import httpx

log = logging.getLogger("panbridge.quark_auth")

_sessions: dict[str, "QuarkLoginSession"] = {}
_SESSION_TTL = 600.0  # drop QR sessions after 10 min


def _purge_sessions() -> None:
    """Prevent unbounded in-memory QR session growth (memory / secret retention)."""
    now = time.time()
    for sid, s in list(_sessions.items()):
        age = now - s.created_at
        if age > _SESSION_TTL:
            _sessions.pop(sid, None)
            continue
        # wipe secrets soon after terminal states
        if s.status in ("confirmed", "expired", "error") and age > 120:
            s.cookie = ""
            if age > 300:
                _sessions.pop(sid, None)


# Browser-style UA for login CAS APIs (Electron client UA can trigger upgrade pages).
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
# Download / drive APIs still accept this cookie family.
_DRIVE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.56 Chrome/100.0.4896.160 "
    "Electron/18.3.5.12 Safari/537.36 Channel/pckk_other_ch"
)

_CLIENT_ID = "532"
# Official web-login QR payload (same as pan.quark.cn web client).
_QR_URL_TMPL = (
    "https://su.quark.cn/4_eMHBJ"
    "?token={token}"
    "&client_id=532"
    "&ssb=weblogin"
    "&uc_param_str="
    "&uc_biz_str={biz}"
)
_UC_BIZ = "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"


@dataclass
class QuarkLoginSession:
    id: str
    status: str = "pending"  # pending|scanned|confirmed|error|expired
    cookie: str = ""
    message: str = ""
    qr_data_url: str = ""
    debug: str = ""
    token: str = ""
    request_id: str = ""
    created_at: float = field(default_factory=time.time)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Referer": "https://pan.quark.cn/",
        "Origin": "https://pan.quark.cn",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


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


def _token_qr_payload(token: str) -> str:
    return _QR_URL_TMPL.format(token=quote(token, safe=""), biz=quote(_UC_BIZ, safe=""))


def _make_qr_data_url(payload: str) -> str:
    """Render login QR as PNG data-URL (real login token, not client-download QR)."""
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def validate_cookie(cookie: str) -> str:
    """Return nickname if cookie can access account API."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://pan.quark.cn/account/info",
            params={"fr": "pc", "platform": "pc"},
            headers={
                "cookie": cookie,
                "user-agent": _DRIVE_UA,
                "referer": "https://pan.quark.cn/",
                "origin": "https://pan.quark.cn",
            },
        )
        data = r.json()
        if data.get("data"):
            return data["data"].get("nickname") or "ok"
        raise RuntimeError(data.get("message") or "invalid quark cookie")


async def start_qr() -> QuarkLoginSession:
    """Baidu-like pure API QR login: get token → show QR → poll ticket → cookies.

    Does NOT screenshot pan.quark.cn (that page mixes client-download / upgrade QRs).
    """
    _purge_sessions()
    sid = uuid.uuid4().hex
    request_id = str(uuid.uuid4())
    sess = QuarkLoginSession(id=sid, request_id=request_id, message="正在取得登錄二維碼…")
    _sessions[sid] = sess

    try:
        async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
            r = await client.get(
                "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin",
                params={"client_id": _CLIENT_ID, "v": "1.2", "request_id": request_id},
            )
            data = r.json()
            if data.get("status") != 2000000:
                raise RuntimeError(data.get("message") or f"getToken failed: {data}")
            token = ((data.get("data") or {}).get("members") or {}).get("token")
            if not token:
                raise RuntimeError(f"no token in response: {data}")
            sess.token = str(token)
            payload = _token_qr_payload(sess.token)
            sess.qr_data_url = _make_qr_data_url(payload)
            sess.status = "pending"
            sess.message = "請用手機夸克 App 掃碼（登錄碼，非下載/升級提示）"
            log.info("quark qr start sid=%s token=%s…", sid[:8], sess.token[:12])
    except Exception as e:
        log.exception("quark qr start failed")
        sess.status = "error"
        sess.message = f"取得二維碼失敗: {e}"
        return sess

    asyncio.create_task(_poll(sess))
    return sess


# Back-compat alias used by older routes
async def start_playwright_login() -> QuarkLoginSession:
    return await start_qr()


async def _poll(sess: QuarkLoginSession) -> None:
    """Poll CAS until user confirms scan, then exchange service_ticket for cookies."""
    deadline = time.time() + 180  # 3 min
    try:
        async with httpx.AsyncClient(timeout=30, headers=_headers(), follow_redirects=False) as client:
            while time.time() < deadline:
                if sess.status in ("confirmed", "error", "expired"):
                    return
                poll_id = str(uuid.uuid4())
                try:
                    r = await client.get(
                        "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken",
                        params={
                            "client_id": _CLIENT_ID,
                            "v": "1.2",
                            "request_id": poll_id,
                            "token": sess.token,
                        },
                    )
                    data = r.json()
                except Exception as e:
                    sess.debug = f"poll:{e}"[:160]
                    await asyncio.sleep(2)
                    continue

                status = data.get("status")
                members = ((data.get("data") or {}).get("members") or {})
                # Common codes: 2000000 success; 50004001/02 waiting / scanned-not-confirm
                if status == 2000000 and members.get("service_ticket"):
                    st = str(members["service_ticket"])
                    sess.status = "scanned"
                    sess.message = "掃碼成功，正在換取 Cookie…"
                    try:
                        cookie = await _exchange_ticket(client, st)
                        nick = await validate_cookie(cookie)
                        sess.cookie = cookie
                        sess.status = "confirmed"
                        sess.message = f"已登錄：{nick}"
                        log.info(
                            "quark qr login ok nick=%s cookie_len=%s has_puus=%s",
                            nick,
                            len(cookie),
                            "__puus=" in cookie,
                        )
                    except Exception as e:
                        log.exception("quark ticket exchange failed")
                        sess.status = "error"
                        sess.message = f"掃碼成功但換取 Cookie 失敗: {e}"
                    return

                msg = (data.get("message") or "").lower()
                # scanned waiting confirm
                if status in (50004002, 50004001) or "scan" in msg or "confirm" in msg:
                    sess.status = "scanned"
                    sess.message = "已掃碼，請在手機上點確認登錄"
                else:
                    sess.debug = f"wait status={status} {data.get('message') or ''}"[:160]
                await asyncio.sleep(2)

            if sess.status not in ("confirmed", "error"):
                sess.status = "expired"
                sess.message = "掃碼超時，請重新點「開始掃碼登錄」"
    except Exception as e:
        log.exception("quark qr poll failed")
        sess.status = "error"
        sess.message = f"掃碼輪詢失敗: {e}"


async def _exchange_ticket(client: httpx.AsyncClient, service_ticket: str) -> str:
    """service_ticket → session cookies (__puus etc.)."""
    jar: dict[str, str] = {}

    # Step 1: account/info with st establishes login cookies
    r = await client.get(
        "https://pan.quark.cn/account/info",
        params={"st": service_ticket, "lw": "scan"},
        headers=_headers(),
        follow_redirects=True,
    )
    _merge_set_cookie(r.headers, jar)
    # Also pull from cookie jar if httpx stored them
    for c in r.cookies.jar:
        jar[c.name] = c.value

    cookie_header = _jar_to_header(jar)
    log.info("quark exchange step1 keys=%s", ",".join(sorted(jar.keys())))

    # Steps 2-4: hit drive endpoints to fill __puus used by CDN download
    enrich_urls = [
        "https://pan.quark.cn/list",
        (
            "https://drive-pc.quark.cn/1/clouddrive/file/sort"
            "?pr=ucpro&fr=pc&uc_param_str=&pdir_fid=0&_page=1&_size=50"
            "&_fetch_total=1&_sort=file_type:asc,updated_at:desc"
        ),
        (
            "https://drive.quark.cn/1/clouddrive/member"
            "?pr=ucpro&fr=pc&uc_param_str=&fetch_subscribe=true"
        ),
    ]
    for url in enrich_urls:
        if "__puus=" in cookie_header and "__pus=" in cookie_header:
            break
        try:
            rr = await client.get(
                url,
                headers={
                    **_headers(),
                    "User-Agent": _DRIVE_UA,
                    "Cookie": cookie_header,
                },
                follow_redirects=True,
            )
            _merge_set_cookie(rr.headers, jar)
            for c in rr.cookies.jar:
                jar[c.name] = c.value
            cookie_header = _jar_to_header(jar)
        except Exception as e:
            log.warning("quark enrich cookie %s failed: %s", url[:60], e)

    if not jar:
        raise RuntimeError("no cookies after service_ticket exchange")
    if "__puus" not in jar and "__pus" not in jar:
        log.warning("quark cookie missing __puus/__pus keys=%s", list(jar.keys()))
    return _jar_to_header(jar)


def get_session(sid: str) -> QuarkLoginSession | None:
    _purge_sessions()
    return _sessions.get(sid)


def extract_nickname(message: str) -> str:
    """Strip UI prefix from login message → bare nickname for storage."""
    m = (message or "").strip()
    for sep in ("已登錄：", "已登录：", "已登錄:", "已登录:"):
        if m.startswith(sep):
            return m[len(sep):].strip() or m
    return m
