from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import msal

_sessions: dict[str, "OneDriveAuthSession"] = {}

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["User.Read", "Files.ReadWrite", "offline_access"]
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
DEVICE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"


@dataclass
class OneDriveAuthSession:
    id: str
    client_id: str
    user_code: str = ""
    device_code: str = ""
    verification_uri: str = "https://microsoft.com/devicelogin"
    message: str = ""
    status: str = "pending"  # pending|confirmed|expired|error
    interval: int = 5
    expires_at: float = 0
    result: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


async def start_device_login(client_id: str) -> OneDriveAuthSession:
    client_id = (client_id or "").strip()
    if not client_id:
        raise RuntimeError("缺少 OneDrive Client ID")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            DEVICE_URL,
            data={
                "client_id": client_id,
                "scope": " ".join(SCOPES),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(data.get("error_description") or data.get("error") or str(data))
        sid = uuid.uuid4().hex
        sess = OneDriveAuthSession(
            id=sid,
            client_id=client_id,
            user_code=data.get("user_code", ""),
            device_code=data.get("device_code", ""),
            verification_uri=data.get("verification_uri")
            or data.get("verification_uri_complete")
            or "https://microsoft.com/devicelogin",
            message=data.get("message") or "",
            interval=int(data.get("interval") or 5),
            expires_at=time.time() + int(data.get("expires_in") or 900),
            status="pending",
        )
        _sessions[sid] = sess
        return sess


async def poll_device_login(sid: str) -> OneDriveAuthSession:
    sess = _sessions.get(sid)
    if not sess:
        raise RuntimeError("session not found")
    if sess.status in ("confirmed", "expired", "error"):
        return sess
    if time.time() > sess.expires_at:
        sess.status = "expired"
        sess.message = "裝置碼已過期，請重新開始"
        return sess

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": sess.client_id,
                "device_code": sess.device_code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = r.json()
        if r.status_code < 400 and data.get("access_token"):
            sess.status = "confirmed"
            sess.result = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "expires_in": data.get("expires_in"),
                "client_id": sess.client_id,
            }
            # fetch display name
            try:
                me = await get_me(data["access_token"])
                sess.result["email"] = me.get("mail") or me.get("userPrincipalName") or me.get("displayName")
            except Exception:
                pass
            sess.message = f"OneDrive 登入成功" + (f"：{sess.result.get('email')}" if sess.result.get("email") else "")
            return sess

        err = data.get("error")
        if err in ("authorization_pending", "slow_down"):
            sess.status = "pending"
            sess.message = "請用瀏覽器打開驗證網址，輸入代碼並登入…"
            return sess
        if err == "expired_token":
            sess.status = "expired"
            sess.message = "裝置碼已過期，請重新開始"
            return sess
        sess.status = "error"
        sess.message = data.get("error_description") or err or str(data)
        return sess


async def get_me(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{GRAPH}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Graph /me failed: {r.status_code} {r.text[:200]}")
        return r.json()


async def refresh_access_token(client_id: str, refresh_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": " ".join(SCOPES),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = r.json()
        if r.status_code >= 400 or not data.get("access_token"):
            raise RuntimeError(data.get("error_description") or data.get("error") or "refresh failed")
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or refresh_token,
            "expires_in": data.get("expires_in"),
            "client_id": client_id,
        }


def get_session(sid: str) -> OneDriveAuthSession | None:
    return _sessions.get(sid)
