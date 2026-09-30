from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.api.deps import require_auth
from app.auth.google_session import DRIVE_SCOPE, credential_lock, save_google_credential, token_exchange
from app.config import get_settings, normalize_public_base_url
from app.db import db
from app.security import decrypt_json, encrypt_json

router = APIRouter(prefix="/api/auth/google", tags=["google"])
_pending: dict[str, dict] = {}


class GoogleConfigIn(BaseModel):
    client_id: str = Field(..., max_length=300)
    client_secret: str = Field(..., min_length=8, max_length=500)


@router.post("/config")
async def save_config(body: GoogleConfigIn, _: None = Depends(require_auth)):
    if not body.client_id.endswith(".apps.googleusercontent.com"):
        raise HTTPException(400, "請使用 Google 網頁應用程式 OAuth 用戶端")
    async with credential_lock(db):
        await db.set_credential("google_oauth", encrypt_json(body.model_dump()))
    return {"ok": True}


@router.get("/status")
async def status(_: None = Depends(require_auth)):
    return {"configured": bool(await db.get_credential("google_oauth")),
            "connected": bool(await db.get_credential("google"))}


@router.post("/start")
async def start(request: Request, _: None = Depends(require_auth)):
    origin = normalize_public_base_url(get_settings().public_base_url)
    if not origin:
        raise HTTPException(400, "請先配置正式 HTTPS 網址")
    enc = await db.get_credential("google_oauth")
    if not enc:
        raise HTTPException(400, "Google OAuth 用戶端尚未配置")
    for key, value in list(_pending.items()):
        if value["expires"] < time.time():
            _pending.pop(key, None)
    if len(_pending) >= 20:
        raise HTTPException(429, "授權次數過多，請稍後重試")
    config = decrypt_json(enc)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    callback = origin + "/api/auth/google/callback"
    _pending[state] = {"config": config, "verifier": verifier, "redirect": callback,
                       "expires": time.time() + 600,
                       "browser": hashlib.sha256(request.cookies.get("panbridge_session", "").encode()).hexdigest()}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": config["client_id"], "redirect_uri": callback,
        "response_type": "code", "scope": DRIVE_SCOPE, "state": state,
        "access_type": "offline", "prompt": "consent select_account",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })}


@router.get("/callback")
async def callback(request: Request, state: str = "", code: str = "", error: str = "",
                   _: None = Depends(require_auth)):
    pending = _pending.pop(state, None)
    browser = hashlib.sha256(request.cookies.get("panbridge_session", "").encode()).hexdigest()
    if not pending or pending["expires"] < time.time() or not secrets.compare_digest(pending["browser"], browser):
        raise HTTPException(400, "Google 授權已過期或不是同一登入，請重新開始")
    if error or not code:
        return RedirectResponse("/settings?google=cancelled", 303)
    try:
        result = await token_exchange({**pending["config"], "code": code,
            "grant_type": "authorization_code", "redirect_uri": pending["redirect"],
            "code_verifier": pending["verifier"]})
        await save_google_credential(db, result, pending["config"])
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/settings?google=connected", 303,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.delete("")
async def disconnect(_: None = Depends(require_auth)):
    async with credential_lock(db):
        await db.delete_credential("google")
    return {"ok": True}
