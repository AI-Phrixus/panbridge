from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Mapping

import httpx


# Quark rotates these session cookies during ordinary drive API requests.
# Persisting the returned values is what keeps long-running transfers alive.
REFRESH_COOKIE_NAMES = frozenset({"__puus", "__pus"})


def parse_cookie_header(value: str) -> dict[str, str]:
    """Parse a Cookie header defensively while preserving non-standard values."""
    out: dict[str, str] = {}
    parsed = SimpleCookie()
    try:
        parsed.load(value or "")
        out.update({name: morsel.value for name, morsel in parsed.items()})
    except Exception:
        pass

    # SimpleCookie rejects some real-world values. Fill any missing pairs using
    # the browser Cookie-header grammar, where semicolons delimit entries.
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        name, raw = part.split("=", 1)
        name = name.strip()
        if name and name not in out:
            out[name] = raw.strip()
    return out


def merge_cookie_header(value: str, updates: Mapping[str, str]) -> str:
    jar = parse_cookie_header(value)
    for name, cookie_value in updates.items():
        if name and cookie_value:
            jar[name] = cookie_value
    return "; ".join(f"{name}={cookie_value}" for name, cookie_value in jar.items())


def refresh_cookies_from_response(response: httpx.Response) -> dict[str, str]:
    """Extract only Quark's rotating auth cookies from an HTTP response."""
    found: dict[str, str] = {}
    try:
        for cookie in response.cookies.jar:
            if cookie.name in REFRESH_COOKIE_NAMES and cookie.value:
                found[cookie.name] = cookie.value
    except Exception:
        pass

    values: list[str] = []
    try:
        values = response.headers.get_list("set-cookie")
    except Exception:
        raw = response.headers.get("set-cookie")
        if raw:
            values = [raw]
    for raw in values:
        parsed = SimpleCookie()
        try:
            parsed.load(raw)
        except Exception:
            continue
        for name, morsel in parsed.items():
            if name in REFRESH_COOKIE_NAMES and morsel.value:
                found[name] = morsel.value
    return found
