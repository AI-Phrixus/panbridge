from __future__ import annotations

import asyncio
from uuid import uuid4
from weakref import WeakKeyDictionary

from app.auth.quark_cookie import merge_cookie_header
from app.db import Database
from app.security import decrypt_json, encrypt_json
from app.sources.quark import QuarkAuthenticationError, QuarkSource


_credential_locks: WeakKeyDictionary[Database, asyncio.Lock] = WeakKeyDictionary()
_api_locks: WeakKeyDictionary[Database, asyncio.Lock] = WeakKeyDictionary()


def _credential_lock(db: Database) -> asyncio.Lock:
    lock = _credential_locks.get(db)
    if lock is None:
        lock = asyncio.Lock()
        _credential_locks[db] = lock
    return lock


def _api_lock(db: Database) -> asyncio.Lock:
    lock = _api_locks.get(db)
    if lock is None:
        lock = asyncio.Lock()
        _api_locks[db] = lock
    return lock


async def save_quark_credential(db: Database, cookie: str, nickname: str = "") -> None:
    """Persist an explicit login as a new generation.

    A generation marker prevents a delayed response from an older worker from
    writing rotating cookies into a freshly scanned account session.
    """
    # Same lock order as QuarkSource._request (API -> credential) makes an
    # explicit login a linearization point: no old control response can return
    # after the new generation becomes visible.
    async with _api_lock(db):
        async with _credential_lock(db):
            await db.set_credential(
                "quark",
                encrypt_json(
                    {
                        "cookie": cookie.strip(),
                        "nickname": nickname,
                        "session_id": uuid4().hex,
                    }
                ),
            )


async def delete_quark_credential(db: Database) -> None:
    """Delete the login without racing a rotating-cookie writeback."""
    async with _api_lock(db):
        async with _credential_lock(db):
            await db.delete_credential("quark")


async def load_quark_source(db: Database) -> QuarkSource:
    """Build a source whose rotating cookies are merged back into encrypted DB state."""
    lock = _credential_lock(db)
    async with lock:
        encrypted = await db.get_credential("quark")
        if not encrypted:
            raise RuntimeError("夸克未登入，請到設定頁掃碼")
        credential = decrypt_json(encrypted)
        cookie = str(credential.get("cookie") or "").strip()
        if not cookie:
            raise RuntimeError("夸克 Cookie 為空，請到設定頁重新掃碼")
        session_id = str(credential.get("session_id") or "")
        if not session_id:
            # Lazy migration for credentials saved by v0.3.x.  This also makes
            # all sources loaded afterwards share the same login generation.
            session_id = uuid4().hex
            credential["session_id"] = session_id
            await db.set_credential("quark", encrypt_json(credential))

    async def persist_cookie(_merged: str, updates: dict[str, str]) -> str:
        # Serialize read/merge/write so two downloads cannot lose each other's
        # __pus / __puus rotation. Never recreate a deleted login, and never let
        # a delayed response from an older login overwrite a new QR session.
        async with lock:
            latest_encrypted = await db.get_credential("quark")
            if not latest_encrypted:
                raise QuarkAuthenticationError("夸克已斷開連接，請重新登入後重試")
            latest = decrypt_json(latest_encrypted)
            before = str(latest.get("cookie") or "")
            if str(latest.get("session_id") or "") != session_id:
                raise QuarkAuthenticationError("夸克登入已更新，舊下載已安全停止；請重試任務")
            after = merge_cookie_header(before, updates)
            if after == before:
                return before
            latest["cookie"] = after
            await db.set_credential("quark", encrypt_json(latest))
            return after

    async def current_cookie() -> str:
        async with lock:
            latest_encrypted = await db.get_credential("quark")
            if not latest_encrypted:
                raise QuarkAuthenticationError("夸克已斷開連接，請重新登入後重試")
            latest = decrypt_json(latest_encrypted)
            if str(latest.get("session_id") or "") != session_id:
                raise QuarkAuthenticationError("夸克登入已更新，舊下載已安全停止；請重試任務")
            latest_cookie = str(latest.get("cookie") or "")
            if not latest_cookie:
                raise QuarkAuthenticationError("夸克 Cookie 為空，請重新登入")
            return latest_cookie

    return QuarkSource(
        cookie,
        on_cookie_update=persist_cookie,
        request_lock=_api_lock(db),
        on_request_start=current_cookie,
    )
