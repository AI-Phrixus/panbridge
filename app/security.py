from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    digest = hashlib.sha256(settings.panbridge_secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_text(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_text(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("failed to decrypt secret") from e


def encrypt_json(data: dict[str, Any]) -> str:
    return encrypt_text(json.dumps(data, ensure_ascii=False))


def decrypt_json(token: str) -> dict[str, Any]:
    return json.loads(decrypt_text(token))


def session_serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.panbridge_secret, salt="panbridge-session")


def make_session_token() -> str:
    return session_serializer().dumps({"auth": True, "nonce": secrets.token_hex(8)})


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    settings = get_settings()
    try:
        data = session_serializer().loads(token, max_age=settings.session_max_age)
        return bool(data.get("auth"))
    except (BadSignature, SignatureExpired):
        return False


def stream_serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.panbridge_secret, salt="panbridge-stream")


def make_stream_token(job_id: int, file_id: int) -> str:
    return stream_serializer().dumps({"job_id": int(job_id), "file_id": int(file_id)})


def verify_stream_token(token: str | None, job_id: int, file_id: int) -> bool:
    if not token:
        return False
    try:
        data = stream_serializer().loads(token, max_age=get_settings().stream_token_max_age)
        return int(data.get("job_id")) == int(job_id) and int(data.get("file_id")) == int(file_id)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False


def hls_asset_serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.panbridge_secret, salt="panbridge-hls-asset")


def make_hls_asset_token(job_id: int, file_id: int, url: str) -> str:
    return hls_asset_serializer().dumps(
        {"job_id": int(job_id), "file_id": int(file_id), "url": str(url)}
    )


def verify_hls_asset_token(token: str | None, job_id: int, file_id: int) -> str | None:
    if not token:
        return None
    try:
        data = hls_asset_serializer().loads(
            token, max_age=get_settings().stream_token_max_age
        )
        if int(data.get("job_id")) != int(job_id) or int(data.get("file_id")) != int(file_id):
            return None
        url = str(data.get("url") or "")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if not (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and (host == "quark.cn" or host.endswith(".quark.cn"))
        ):
            return None
        return url
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def check_password(password: str) -> bool:
    settings = get_settings()
    return secrets.compare_digest(password, settings.admin_password)
