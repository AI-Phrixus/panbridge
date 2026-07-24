from __future__ import annotations

from fastapi import Cookie, HTTPException, Request, status

from app.security import verify_session_token


def require_auth(session: str | None = Cookie(default=None, alias="panbridge_session")) -> None:
    if not verify_session_token(session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
