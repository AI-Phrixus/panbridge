#!/usr/bin/env python3
import asyncio
import json
import sqlite3

import httpx

from app.security import decrypt_json
from app.sources.quark import QuarkSource, _QUARK_PC_UA


async def try_file(src: QuarkSource, fid: str, name: str) -> None:
    items = await src.get_download_urls([fid])
    it = items[0]
    print("---", name[:60])
    print(
        "ban",
        it.get("ban"),
        "risk",
        it.get("risk_type"),
        "status",
        it.get("status"),
        "size",
        it.get("size"),
    )
    url = it["download_url"]
    headers = {
        "User-Agent": _QUARK_PC_UA,
        "Cookie": src.cookie,
        "Referer": "https://pan.quark.cn/",
    }
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, http2=False) as client:
        r = await client.get(url, headers=headers)
        ok = r.status_code == 200 and len(r.content) > 500
        print("status", r.status_code, "len", len(r.content), "ok" if ok else "bad")


async def main() -> None:
    from app.config import get_settings
    from app.security import decrypt_text
    import aiosqlite
    from app.db import Database

    db = Database()
    await db.connect()
    enc = await db.get_credential("quark")
    cookie = decrypt_json(enc)["cookie"]
    await db.close()

    src = QuarkSource(cookie)
    con = sqlite3.connect(str(get_settings().db_path))
    rows = con.execute(
        "SELECT remote_name, meta_json, size FROM files WHERE job_id=4 ORDER BY size ASC LIMIT 8"
    ).fetchall()
    for name, mj, size in rows:
        meta = json.loads(mj or "{}")
        fid = meta.get("owned_fid")
        if not fid:
            print("no fid", name)
            continue
        try:
            await try_file(src, fid, name)
        except Exception as e:
            print(name, "ERR", type(e).__name__, e)
        await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
