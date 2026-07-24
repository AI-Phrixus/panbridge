from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

import httpx

_sessions: dict[str, "QuarkLoginSession"] = {}


@dataclass
class QuarkLoginSession:
    id: str
    status: str = "pending"  # pending|confirmed|error|expired
    cookie: str = ""
    message: str = ""
    qr_data_url: str = ""
    created_at: float = field(default_factory=time.time)


async def validate_cookie(cookie: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://pan.quark.cn/account/info",
            params={"fr": "pc", "platform": "pc"},
            headers={
                "cookie": cookie,
                "user-agent": "Mozilla/5.0",
                "referer": "https://pan.quark.cn/",
            },
        )
        data = r.json()
        if data.get("data"):
            return data["data"].get("nickname") or "ok"
        raise RuntimeError(data.get("message") or "invalid quark cookie")


async def start_playwright_login() -> QuarkLoginSession:
    """Open quark login page, expose QR screenshot as data URL, wait for cookie."""
    sid = uuid.uuid4().hex
    sess = QuarkLoginSession(id=sid)
    _sessions[sid] = sess
    asyncio.create_task(_run_playwright(sess))
    return sess


async def _run_playwright(sess: QuarkLoginSession) -> None:
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        sess.status = "error"
        sess.message = f"playwright not available: {e}. Use paste-cookie instead."
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 480, "height": 640})
            page = await context.new_page()
            await page.goto("https://pan.quark.cn/", wait_until="domcontentloaded", timeout=60000)
            # wait for possible QR
            await asyncio.sleep(2)
            # screenshot whole page as QR carrier
            import base64

            for _ in range(90):  # ~3 min
                png = await page.screenshot(full_page=False)
                sess.qr_data_url = "data:image/png;base64," + base64.b64encode(png).decode()
                cookies = await context.cookies()
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                if "__puus" in cookie_str or "puus" in cookie_str or len(cookies) > 5:
                    try:
                        nick = await validate_cookie(cookie_str)
                        sess.cookie = cookie_str
                        sess.status = "confirmed"
                        sess.message = nick
                        await browser.close()
                        return
                    except Exception:
                        pass
                await asyncio.sleep(2)
            sess.status = "expired"
            await browser.close()
    except Exception as e:
        sess.status = "error"
        sess.message = str(e)


def get_session(sid: str) -> QuarkLoginSession | None:
    return _sessions.get(sid)
