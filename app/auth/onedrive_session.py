from __future__ import annotations

import asyncio
import time
from uuid import uuid4
from weakref import WeakKeyDictionary

from app.auth.onedrive_auth import refresh_access_token
from app.db import Database
from app.security import decrypt_json, encrypt_json


_credential_locks: WeakKeyDictionary[Database, asyncio.Lock] = WeakKeyDictionary()


class OneDriveSessionReplaced(RuntimeError):
    pass


def _credential_lock(db: Database) -> asyncio.Lock:
    lock = _credential_locks.get(db)
    if lock is None:
        lock = asyncio.Lock()
        _credential_locks[db] = lock
    return lock


async def save_onedrive_credential(db: Database, result: dict) -> None:
    """Persist an explicit device login as a new, isolated generation."""
    expires_in = max(60, int(result.get("expires_in") or 3600))
    credential = {
        "access_token": str(result.get("access_token") or ""),
        "refresh_token": str(result.get("refresh_token") or ""),
        "client_id": str(result.get("client_id") or ""),
        "email": result.get("email"),
        "expires_at": time.time() + expires_in,
        "session_id": uuid4().hex,
    }
    if not credential["access_token"]:
        raise RuntimeError("OneDrive 登入結果缺少 access token")
    async with _credential_lock(db):
        await db.set_credential("onedrive", encrypt_json(credential))


async def delete_onedrive_credential(db: Database) -> None:
    async with _credential_lock(db):
        await db.delete_credential("onedrive")


async def load_onedrive_credential(db: Database, *, refresh: bool = True) -> dict:
    """Load and, when near expiry, refresh tokens exactly once per process."""
    async with _credential_lock(db):
        encrypted = await db.get_credential("onedrive")
        if not encrypted:
            raise RuntimeError("OneDrive 未連接")
        credential = decrypt_json(encrypted)
        changed = False
        if not credential.get("session_id"):
            credential["session_id"] = uuid4().hex
            changed = True

        access = str(credential.get("access_token") or "")
        refresh_token = str(credential.get("refresh_token") or "")
        client_id = str(credential.get("client_id") or "")
        expires_at = float(credential.get("expires_at") or 0)
        if refresh and refresh_token and client_id and expires_at <= time.time() + 300:
            token = await refresh_access_token(client_id, refresh_token)
            credential["access_token"] = str(token["access_token"])
            credential["refresh_token"] = str(token.get("refresh_token") or refresh_token)
            credential["expires_at"] = time.time() + max(
                60, int(token.get("expires_in") or 3600)
            )
            access = credential["access_token"]
            changed = True
        if changed:
            await db.set_credential("onedrive", encrypt_json(credential))
        if not access:
            raise RuntimeError("OneDrive token 失效，請重新登入")
        return credential


async def persist_onedrive_tokens(
    db: Database,
    session_id: str,
    access_token: str,
    refresh_token: str,
) -> None:
    """Persist sink-side rotation only into the generation that requested it."""
    async with _credential_lock(db):
        encrypted = await db.get_credential("onedrive")
        if not encrypted:
            raise OneDriveSessionReplaced("OneDrive 已斷開連接")
        credential = decrypt_json(encrypted)
        if str(credential.get("session_id") or "") != str(session_id):
            raise OneDriveSessionReplaced("OneDrive 登入已更新，舊工作已停止")
        credential["access_token"] = access_token
        credential["refresh_token"] = refresh_token
        credential["expires_at"] = time.time() + 3600
        await db.set_credential("onedrive", encrypt_json(credential))


async def refresh_onedrive_session(
    db: Database, session_id: str
) -> tuple[str, str]:
    """Refresh using the latest rotating token under the generation lock."""
    async with _credential_lock(db):
        encrypted = await db.get_credential("onedrive")
        if not encrypted:
            raise OneDriveSessionReplaced("OneDrive 已斷開連接")
        credential = decrypt_json(encrypted)
        if str(credential.get("session_id") or "") != str(session_id):
            raise OneDriveSessionReplaced("OneDrive 登入已更新，舊工作已停止")
        refresh_token = str(credential.get("refresh_token") or "")
        client_id = str(credential.get("client_id") or "")
        if not refresh_token or not client_id:
            raise RuntimeError("OneDrive 缺少續期憑證，請重新登入")
        token = await refresh_access_token(client_id, refresh_token)
        access = str(token["access_token"])
        rotated_refresh = str(token.get("refresh_token") or refresh_token)
        credential["access_token"] = access
        credential["refresh_token"] = rotated_refresh
        credential["expires_at"] = time.time() + max(
            60, int(token.get("expires_in") or 3600)
        )
        await db.set_credential("onedrive", encrypt_json(credential))
        return access, rotated_refresh


async def make_onedrive_sink(db: Database):
    from app.sinks.onedrive import OneDriveSink

    credential = await load_onedrive_credential(db)
    access = str(credential.get("access_token") or "")
    refresh_token = str(credential.get("refresh_token") or "")
    client_id = str(credential.get("client_id") or "")
    session_id = str(credential.get("session_id") or "")

    async def refresh() -> tuple[str, str]:
        return await refresh_onedrive_session(db, session_id)

    return OneDriveSink(
        access,
        refresh_token,
        client_id,
        refresh_cb=refresh,
    )
