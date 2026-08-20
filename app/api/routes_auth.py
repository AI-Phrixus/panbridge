from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.deps import require_auth
from app.auth import baidu_qr, quark_auth, pcloud_auth, onedrive_auth
from app.auth.quark_session import delete_quark_credential, save_quark_credential
from app.auth.onedrive_session import (
    delete_onedrive_credential,
    save_onedrive_credential,
)
from app.config import get_settings
from app.db import db
from app.security import check_password, encrypt_json, make_session_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

# simple in-memory login rate limit (BUG-16)
_login_hits: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_WINDOW = 300.0  # 5 min
_LOGIN_MAX = 20
# QR start rate limit — external CAS / passport hammering (ADV-R1)
_qr_hits: dict[str, deque[float]] = defaultdict(deque)
_QR_WINDOW = 300.0
_QR_MAX = 12


def _rate_limit_login(ip: str) -> None:
    now = time.time()
    q = _login_hits[ip]
    while q and now - q[0] > _LOGIN_WINDOW:
        q.popleft()
    if len(q) >= _LOGIN_MAX:
        raise HTTPException(status_code=429, detail="too many login attempts, try later")
    q.append(now)


def _rate_limit_qr(key: str) -> None:
    now = time.time()
    q = _qr_hits[key]
    while q and now - q[0] > _QR_WINDOW:
        q.popleft()
    if len(q) >= _QR_MAX:
        raise HTTPException(status_code=429, detail="掃碼請求過於頻繁，請稍後再試")
    q.append(now)


class PasswordIn(BaseModel):
    password: str


class CookieIn(BaseModel):
    cookie: str


class PCloudIn(BaseModel):
    email: str
    password: str
    api_host: str | None = None
    code: str | None = None  # 2FA authenticator code if enabled


class PCloudTokenIn(BaseModel):
    auth: str
    api_host: str | None = None


def _is_secure_request(request: Request) -> bool:
    """Use only the scheme Uvicorn accepted from its trusted proxy boundary."""
    return request.url.scheme.lower() == "https"


@router.post("/login")
async def login(body: PasswordIn, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    _rate_limit_login(ip)
    if not check_password(body.password):
        raise HTTPException(status_code=401, detail="wrong password")
    token = make_session_token()
    # Uvicorn accepts forwarded scheme only from its configured trusted proxy.
    response.set_cookie(
        "panbridge_session",
        token,
        httponly=True,
        samesite="lax",
        secure=_is_secure_request(request),
        max_age=get_settings().session_max_age,
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("panbridge_session")
    return {"ok": True}


@router.get("/me")
async def me(session_ok: None = Depends(require_auth)):
    providers = await db.list_credential_providers()
    return {"ok": True, "providers": providers}


# ---- Baidu QR ----
@router.post("/baidu/qr/start")
async def baidu_qr_start(request: Request, _: None = Depends(require_auth)):
    ip = request.client.host if request.client else "unknown"
    _rate_limit_qr(f"baidu:{ip}")
    sess = await baidu_qr.start_qr()
    return {"id": sess.id, "imgurl": sess.imgurl, "status": sess.status}


@router.get("/baidu/qr/{sid}")
async def baidu_qr_status(sid: str, _: None = Depends(require_auth)):
    sess = baidu_qr.get_session(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    if sess.status == "confirmed" and sess.cookie:
        ck = sess.cookie
        if "BDUSS=" not in ck and "BDUSS_BFESS=" not in ck:
            # do not persist incomplete login
            return {
                "id": sess.id,
                "status": "error",
                "message": "扫码未拿到 BDUSS，请重新扫码或手动粘贴完整 Cookie",
                "imgurl": sess.imgurl,
            }
        await db.set_credential("baidu", encrypt_json({"cookie": ck}))
        sess.cookie = ""  # wipe secret after persist (ADV-R1)
    return {"id": sess.id, "status": sess.status, "message": sess.message, "imgurl": sess.imgurl, "debug": getattr(sess, "debug", "")}


@router.post("/baidu/cookie")
async def baidu_cookie(body: CookieIn, _: None = Depends(require_auth)):
    ck = body.cookie.strip()
    # Accept BDUSS or BDUSS_BFESS
    if "BDUSS=" not in ck and "BDUSS_BFESS=" not in ck:
        raise HTTPException(
            400,
            "Cookie 不完整：必须包含 BDUSS=（请从 pan.baidu.com 已登录页面的 Request Headers → Cookie 整段复制，不要只复制 BAIDUID）",
        )
    # STOKEN strongly recommended for share ops
    warn = ""
    if "STOKEN=" not in ck:
        warn = "（警告：未检测到 STOKEN，分享转存可能失败，建议一并复制）"
    await db.set_credential("baidu", encrypt_json({"cookie": ck}))
    return {"ok": True, "message": "baidu cookie saved" + warn, "has_stoken": "STOKEN=" in ck}


# ---- Quark ----
@router.post("/quark/qr/start")
async def quark_qr_start(request: Request, _: None = Depends(require_auth)):
    # Pure CAS API QR (not page screenshot — avoids client upgrade / download QRs)
    ip = request.client.host if request.client else "unknown"
    _rate_limit_qr(f"quark:{ip}")
    sess = await quark_auth.start_qr()
    if sess.status == "error" and not sess.qr_data_url:
        raise HTTPException(502, sess.message or "quark qr start failed")
    return {
        "id": sess.id,
        "status": sess.status,
        "message": sess.message,
        "qr_data_url": sess.qr_data_url,
        "debug": getattr(sess, "debug", ""),
    }


@router.get("/quark/qr/{sid}")
async def quark_qr_status(sid: str, _: None = Depends(require_auth)):
    sess = quark_auth.get_session(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    if sess.status == "confirmed" and sess.cookie:
        nick = quark_auth.extract_nickname(sess.message)
        await save_quark_credential(db, sess.cookie, nick)
        sess.cookie = ""  # wipe secret after persist (ADV-R1)
        if nick and not sess.message.startswith("已登錄"):
            sess.message = f"已登錄：{nick}"
    return {
        "id": sess.id,
        "status": sess.status,
        "message": sess.message,
        "qr_data_url": sess.qr_data_url,
        "debug": getattr(sess, "debug", ""),
    }


@router.post("/quark/cookie")
async def quark_cookie(body: CookieIn, _: None = Depends(require_auth)):
    ck = body.cookie.strip()
    if len(ck) < 20:
        raise HTTPException(400, "Cookie 過短，請貼上完整 Cookie")
    nick = await quark_auth.validate_cookie(ck)
    await save_quark_credential(db, ck, nick)
    return {"ok": True, "nickname": nick}


# ---- pCloud ----
@router.post("/pcloud/login")
async def pcloud_login(body: PCloudIn, _: None = Depends(require_auth)):
    host = body.api_host or get_settings().pcloud_api_host
    try:
        info = await pcloud_auth.login_with_password(
            body.email, body.password, host, code=body.code
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"pCloud login error: {e}") from e
    await db.set_credential(
        "pcloud",
        encrypt_json({"auth": info["auth"], "email": info.get("email"), "api_host": info.get("api_host") or host}),
    )
    return {"ok": True, "email": info.get("email"), "api_host": info.get("api_host")}




@router.post("/pcloud/token")
async def pcloud_token(body: PCloudTokenIn, _: None = Depends(require_auth)):
    host = body.api_host or get_settings().pcloud_api_host
    try:
        info = await pcloud_auth.login_with_token(body.auth, host)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"pCloud token error: {e}") from e
    await db.set_credential(
        "pcloud",
        encrypt_json({"auth": info["auth"], "email": info.get("email"), "api_host": info.get("api_host") or host}),
    )
    return {"ok": True, "email": info.get("email"), "api_host": info.get("api_host")}




class OneDriveClientIn(BaseModel):
    client_id: str


@router.post("/onedrive/device/start")
async def onedrive_device_start(body: OneDriveClientIn, _: None = Depends(require_auth)):
    try:
        sess = await onedrive_auth.start_device_login(body.client_id)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    return {
        "id": sess.id,
        "user_code": sess.user_code,
        "verification_uri": sess.verification_uri,
        "message": sess.message,
        "status": sess.status,
    }


@router.get("/onedrive/device/{sid}")
async def onedrive_device_status(sid: str, _: None = Depends(require_auth)):
    try:
        sess = await onedrive_auth.poll_device_login(sid)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    if sess.status == "confirmed" and sess.result.get("access_token"):
        await save_onedrive_credential(db, sess.result)
        # A confirmed polling endpoint may be called repeatedly by the UI. Wipe
        # tokens after the first durable save so it cannot create a new session
        # generation every few seconds.
        sess.result = {}
    return {
        "id": sess.id,
        "status": sess.status,
        "message": sess.message,
        "user_code": sess.user_code,
        "verification_uri": sess.verification_uri,
    }


@router.post("/onedrive/client_id")
async def onedrive_save_client_id(body: OneDriveClientIn, _: None = Depends(require_auth)):
    """Save client id alone for later device login."""
    await db.set_credential("onedrive_app", encrypt_json({"client_id": body.client_id.strip()}))
    return {"ok": True}


@router.delete("/{provider}")
async def delete_provider(provider: str, _: None = Depends(require_auth)):
    if provider not in ("quark", "baidu", "pcloud", "onedrive", "onedrive_app"):
        raise HTTPException(400, "bad provider")
    if provider == "quark":
        await delete_quark_credential(db)
    elif provider == "onedrive":
        await delete_onedrive_credential(db)
    else:
        await db.delete_credential(provider)
    return {"ok": True}
