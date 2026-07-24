#!/usr/bin/env python3
import asyncio
import json
import subprocess
from pathlib import Path

import httpx

from app.db import Database
from app.security import decrypt_json
from app.sources.quark import QuarkSource, _QUARK_PC_UA


async def main() -> None:
    db = Database()
    await db.connect()
    cookie = decrypt_json(await db.get_credential("quark"))["cookie"]
    src = QuarkSource(cookie)
    f = await db.get_file(14)
    meta = json.loads(f["meta_json"] or "{}")
    items = await src.get_download_urls([meta["owned_fid"]])
    print("item keys", list(items[0].keys()))
    url = items[0]["download_url"]
    Path("/tmp/qurl.txt").write_text(url)
    Path("/tmp/qck.txt").write_text(cookie)
    headers = {
        "User-Agent": _QUARK_PC_UA,
        "Cookie": cookie,
        "Referer": "https://pan.quark.cn/",
        "Accept-Encoding": "identity",
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, http2=False) as client:
        r = await client.get(url, headers=headers)
        print("status", r.status_code)
        print("resp headers", dict(r.headers))
        print("body", r.text[:500])
    out = subprocess.getoutput(
        f'curl -sI -m 25 -A "{_QUARK_PC_UA}" -b /tmp/qck.txt '
        f'-e "https://pan.quark.cn/" "$(cat /tmp/qurl.txt)"'
    )
    print("CURL_I\n", out[:800])
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
