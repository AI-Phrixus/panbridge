"""Round 3: security / auth / rate limit."""
from __future__ import annotations

from app.api.routes_auth import _rate_limit_login, _login_hits, _LOGIN_MAX
from fastapi import HTTPException
import pytest


def test_login_rate_limit():
    ip = "203.0.113.99-r3"
    _login_hits.pop(ip, None)
    for _ in range(_LOGIN_MAX):
        _rate_limit_login(ip)
    with pytest.raises(HTTPException) as ei:
        _rate_limit_login(ip)
    assert ei.value.status_code == 429
    _login_hits.pop(ip, None)


def test_baidu_qr_no_infinite_timeout():
    import pathlib
    src = pathlib.Path("app/auth/baidu_qr.py").read_text()
    assert "timeout=None" not in src
