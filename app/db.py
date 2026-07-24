from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    provider TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    share_url TEXT NOT NULL,
    passcode TEXT DEFAULT '',
    title TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    error_message TEXT DEFAULT '',
    pcloud_path TEXT DEFAULT '',
    destination TEXT DEFAULT 'auto',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    source_fid TEXT DEFAULT '',
    remote_name TEXT NOT NULL,
    relative_path TEXT DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    local_path TEXT DEFAULT '',
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    uploaded_bytes INTEGER NOT NULL DEFAULT 0,
    download_url TEXT DEFAULT '',
    pcloud_fileid TEXT DEFAULT '',
    pcloud_path TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    error_message TEXT DEFAULT '',
    meta_json TEXT DEFAULT '{}',
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_files_job ON files(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = str(path or get_settings().db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL: progress updates during long downloads won't block API reads as hard
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._migrate()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("database not connected")
        return self._conn


    async def _migrate(self) -> None:
        """Additive schema upgrades."""
        cur = await self.conn.execute("PRAGMA table_info(jobs)")
        cols = {row[1] for row in await cur.fetchall()}
        alters = []
        if "speed_bps" not in cols:
            alters.append("ALTER TABLE jobs ADD COLUMN speed_bps REAL NOT NULL DEFAULT 0")
        if "status_detail" not in cols:
            alters.append("ALTER TABLE jobs ADD COLUMN status_detail TEXT DEFAULT ''")
        if "destination" not in cols:
            alters.append("ALTER TABLE jobs ADD COLUMN destination TEXT DEFAULT 'auto'")
        for sql in alters:
            await self.conn.execute(sql)
        if alters:
            await self.conn.commit()

    # ---- credentials ----
    async def set_credential(self, provider: str, payload_enc: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO credentials(provider, payload, updated_at) VALUES(?,?,?)
            ON CONFLICT(provider) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (provider, payload_enc, _now()),
        )
        await self.conn.commit()

    async def get_credential(self, provider: str) -> str | None:
        cur = await self.conn.execute("SELECT payload FROM credentials WHERE provider=?", (provider,))
        row = await cur.fetchone()
        return row["payload"] if row else None

    async def delete_credential(self, provider: str) -> None:
        await self.conn.execute("DELETE FROM credentials WHERE provider=?", (provider,))
        await self.conn.commit()

    async def list_credential_providers(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute("SELECT provider, updated_at FROM credentials")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---- jobs ----
    async def create_job(
        self,
        source_type: str,
        share_url: str,
        passcode: str = "",
        title: str = "",
        pcloud_path: str = "",
        destination: str = "auto",
    ) -> int:
        now = _now()
        cur = await self.conn.execute(
            """
            INSERT INTO jobs(source_type, share_url, passcode, title, status, progress, pcloud_path, destination, created_at, updated_at)
            VALUES(?,?,?,?, 'queued', 0, ?, ?, ?, ?)
            """,
            (source_type, share_url, passcode, title, pcloud_path, destination or "auto", now, now),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [job_id]
        await self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", vals)
        await self.conn.commit()

    async def get_job(self, job_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def claim_next_job(self, allowed: set[str] | None = None) -> dict[str, Any] | None:
        allowed = allowed or {"queued", "downloading", "uploading", "resolving", "saving"}
        placeholders = ",".join("?" for _ in allowed)
        cur = await self.conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE status IN ({placeholders})
            ORDER BY
              CASE status
                WHEN 'downloading' THEN 0
                WHEN 'uploading' THEN 0
                WHEN 'resolving' THEN 0
                WHEN 'saving' THEN 0
                ELSE 1
              END,
              id ASC
            LIMIT 1
            """,
            tuple(allowed),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def count_active_jobs(self) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE status IN ('resolving','saving','downloading','uploading')"
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    # ---- files ----
    async def create_file(
        self,
        job_id: int,
        remote_name: str,
        relative_path: str = "",
        size: int = 0,
        source_fid: str = "",
        meta: dict | None = None,
    ) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO files(job_id, source_fid, remote_name, relative_path, size, status, meta_json)
            VALUES(?,?,?,?,?, 'queued', ?)
            """,
            (job_id, source_fid, remote_name, relative_path, size, json.dumps(meta or {})),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def update_file(self, file_id: int, **fields: Any) -> None:
        if "meta" in fields:
            fields["meta_json"] = json.dumps(fields.pop("meta"))
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [file_id]
        await self.conn.execute(f"UPDATE files SET {cols} WHERE id=?", vals)
        await self.conn.commit()

    async def get_file(self, file_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_files(self, job_id: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM files WHERE job_id=? ORDER BY id ASC", (job_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def recompute_job_progress(self, job_id: int) -> float:
        """Size-weighted progress so a 26GB file is not buried by small images."""
        files = await self.list_files(job_id)
        if not files:
            return 0.0
        weighted = 0.0
        total_w = 0.0
        for f in files:
            size = max(int(f["size"] or 0), 1)
            w = float(size)
            total_w += w
            # download 70% weight, upload 30%
            dl = min(1.0, int(f["downloaded_bytes"] or 0) / size)
            ul = min(1.0, int(f["uploaded_bytes"] or 0) / size)
            if f["status"] == "done":
                frac = 1.0
            else:
                frac = max(0.0, dl * 0.7 + ul * 0.3)
            weighted += frac * w
        if total_w <= 0:
            return 0.0
        return round(100.0 * weighted / total_w, 2)


db = Database()
