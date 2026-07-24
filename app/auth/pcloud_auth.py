from __future__ import annotations

import hashlib
from typing import Any

import httpx


def _password_digest(username: str, password: str, digest: str) -> str:
    # passworddigest = sha1( password + sha1(lowercase(username)) + digest )
    u = hashlib.sha1(username.lower().encode("utf-8")).hexdigest()
    return hashlib.sha1((password + u + digest).encode("utf-8")).hexdigest()


async def login_with_password(
    email: str,
    password: str,
    api_host: str = "eapi.pcloud.com",
    code: str | None = None,
) -> dict[str, Any]:
    """Try multiple pCloud login styles. Prefer token paste if 2FA keeps failing."""
    hosts = []
    for h in (api_host, "api.pcloud.com", "eapi.pcloud.com"):
        if h and h not in hosts:
            hosts.append(h)

    code_s = (code or "").strip() or None
    last_err = "pcloud login failed"
    last_raw: dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for host in hosts:
            base = f"https://{host}"

            # 1) Plain password (+ optional 2FA code) via POST (avoids URL logging / encoding issues)
            data: dict[str, Any] = {
                "getauth": 1,
                "logout": 1,
                "username": email,
                "password": password,
            }
            if code_s:
                data["code"] = code_s
            r = await client.post(f"{base}/userinfo", data=data)
            try:
                j = r.json()
            except Exception:
                j = {"result": -1, "error": f"HTTP {r.status_code}"}
            last_raw = j
            if j.get("result") == 0 and j.get("auth"):
                return _ok(j, email, host)

            last_err = str(j.get("error") or f"result={j.get('result')}")

            # 2) Digest login (+ code)
            try:
                dg = await client.get(f"{base}/getdigest")
                dj = dg.json()
                digest = dj.get("digest")
                if digest:
                    data2: dict[str, Any] = {
                        "getauth": 1,
                        "logout": 1,
                        "username": email,
                        "digest": digest,
                        "passworddigest": _password_digest(email, password, digest),
                    }
                    if code_s:
                        data2["code"] = code_s
                    r2 = await client.post(f"{base}/userinfo", data=data2)
                    j2 = r2.json()
                    last_raw = j2
                    if j2.get("result") == 0 and j2.get("auth"):
                        return _ok(j2, email, host)
                    last_err = str(j2.get("error") or f"result={j2.get('result')}")
            except Exception as e:
                last_err = f"digest login error: {e}"

            # If explicitly needs code and we already sent one, break with clear msg
            if "code" in last_err.lower():
                if code_s:
                    raise RuntimeError(
                        "pCloud 拒绝了验证码（可能已过期/不是 Authenticator 的码，或账号 2FA 方式不兼容密码 API）。"
                        "请改用下方「粘贴 auth token」方式连接。"
                    )
                raise RuntimeError(
                    "pCloud 需要两步验证。请填写手机 Authenticator 的 6 位码；"
                    "若仍失败请用「粘贴 auth token」。"
                )

    raise RuntimeError(f"{last_err} | raw={last_raw.get('result')}")


async def login_with_token(auth: str, api_host: str = "api.pcloud.com") -> dict[str, Any]:
    auth = (auth or "").strip()
    if not auth:
        raise RuntimeError("auth token empty")
    # strip quotes / auth= prefix if user pasted raw
    if auth.lower().startswith("auth="):
        auth = auth.split("=", 1)[1].strip()
    auth = auth.strip().strip('"').strip("'")

    hosts = []
    for h in (api_host, "api.pcloud.com", "eapi.pcloud.com"):
        if h and h not in hosts:
            hosts.append(h)

    last_err = "invalid token"
    async with httpx.AsyncClient(timeout=30) as client:
        for host in hosts:
            r = await client.post(f"https://{host}/userinfo", data={"auth": auth})
            try:
                j = r.json()
            except Exception:
                continue
            if j.get("result") == 0:
                return {
                    "auth": auth,
                    "email": j.get("email") or "",
                    "api_host": host,
                    "userid": j.get("userid"),
                }
            last_err = str(j.get("error") or f"result={j.get('result')}")
    raise RuntimeError(last_err)


def _ok(j: dict[str, Any], email: str, host: str) -> dict[str, Any]:
    return {
        "auth": j["auth"],
        "email": j.get("email") or email,
        "api_host": host,
        "userid": j.get("userid"),
    }


async def validate_token(auth: str, api_host: str = "eapi.pcloud.com") -> dict[str, Any]:
    return await login_with_token(auth, api_host)
