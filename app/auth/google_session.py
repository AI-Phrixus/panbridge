from __future__ import annotations

import asyncio
import time
from uuid import uuid4
from weakref import WeakKeyDictionary

import httpx

from app.security import decrypt_json, encrypt_json

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_locks = WeakKeyDictionary()


class GoogleAuthenticationError(RuntimeError):
    pass


def credential_lock(db):
    if db not in _locks:
        _locks[db] = asyncio.Lock()
    return _locks[db]


async def token_exchange(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.post("https://oauth2.googleapis.com/token", data=data)
    if response.status_code != 200:
        # Never expose raw OAuth responses, codes, client secrets or refresh tokens.
        if response.status_code in (400, 401, 403):
            raise GoogleAuthenticationError("Google 登入已失效，請重新連接；進度已保留")
        raise RuntimeError(f"Google 授權服務暫時不可用（HTTP {response.status_code}），請稍後重試")
    result = response.json()
    if not result.get("access_token"):
        raise RuntimeError("Google 授權回應不完整")
    return result


async def save_google_credential(db, token: dict, config: dict):
    if not token.get("refresh_token"):
        raise RuntimeError("Google 未授予離線續期權限，請重新授權")
    scopes = set(str(token.get("scope") or "").split())
    if DRIVE_SCOPE not in scopes:
        raise RuntimeError("Google 未授予所需的文件權限")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        about = await client.get("https://www.googleapis.com/drive/v3/about",
            params={"fields": "user(permissionId,emailAddress)"},
            headers={"Authorization": "Bearer " + token["access_token"]})
    if about.status_code != 200:
        raise RuntimeError("無法確認 Google Drive 帳號身分，尚未保存連線")
    user = about.json().get("user") or {}
    if not user.get("permissionId"):
        raise RuntimeError("Google Drive 未返回可確認的帳號身分")
    cred = {**config, "access_token": token["access_token"],
            "refresh_token": token["refresh_token"], "session_id": uuid4().hex,
            "expires_at": time.time() + int(token.get("expires_in") or 3600),
            "account_id": user["permissionId"], "email": user.get("emailAddress")}
    async with credential_lock(db):
        await db.set_credential("google", encrypt_json(cred))


async def google_credential(db, *, force=False, session_id=None):
    async with credential_lock(db):
        enc = await db.get_credential("google")
        if not enc:
            raise GoogleAuthenticationError("Google Drive 未連接，請到帳號設定授權")
        cred = decrypt_json(enc)
        if session_id and cred.get("session_id") != session_id:
            raise GoogleAuthenticationError("Google 帳號已更換，舊任務請重新確認後重試")
        if force or float(cred.get("expires_at") or 0) <= time.time() + 300:
            result = await token_exchange({
                "grant_type": "refresh_token", "refresh_token": cred["refresh_token"],
                "client_id": cred["client_id"], "client_secret": cred["client_secret"],
            })
            cred["access_token"] = result["access_token"]
            cred["refresh_token"] = result.get("refresh_token") or cred["refresh_token"]
            cred["expires_at"] = time.time() + int(result.get("expires_in") or 3600)
            await db.set_credential("google", encrypt_json(cred))
        return cred


async def make_google_sink(db):
    from app.sinks.google import GoogleDriveSink
    credential = await google_credential(db)
    generation = credential["session_id"]

    async def access(force=False):
        cred = await google_credential(db, force=force, session_id=generation)
        return cred["access_token"]

    return GoogleDriveSink(access, generation, credential["account_id"])
