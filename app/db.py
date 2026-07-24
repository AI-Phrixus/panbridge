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

    _JOB_COLS = frozenset({
        "source_type", "share_url", "passcode", "title", "status", "progress",
        "error_message", "pcloud_path", "destination", "speed_bps", "status_detail",
        "updated_at", "created_at",
    })
    _FILE_COLS = frozenset({
        "source_fid", "remote_name", "relative_path", "size", "local_path",
        "downloaded_bytes", "uploaded_bytes", "download_url", "pcloud_fileid",
        "pcloud_path", "status", "error_message", "meta_json",
    })

    async def update_job(self, job_id: int, touch: bool = True, **fields: Any) -> None:
        """Update job fields. touch=False keeps updated_at (queue heartbeat must not
        reset claim priority / look 'active' while only waiting)."""
        if not fields:
            return
        bad = set(fields) - self._JOB_COLS
        if bad:
            raise ValueError(f"invalid job fields: {bad}")
        if touch:
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

    async def claim_next_job(
        self,
        allowed: set[str] | None = None,
        exclude_ids: set[int] | None = None,
    ) -> dict[str, Any] | None:
        """Pick next runnable job, skipping IDs already owned by this worker.

        Prefer resume of interrupted in-progress work over brand-new queued
        (prevents partial jobs starving behind new links). exclude_ids avoids
        double-claim within one worker process.
        """
        allowed = allowed or {"queued", "downloading", "uploading", "resolving", "saving"}
        exclude_ids = exclude_ids or set()
        placeholders = ",".join("?" for _ in allowed)
        params: list[Any] = list(allowed)
        exclude_sql = ""
        if exclude_ids:
            exclude_sql = " AND id NOT IN (" + ",".join("?" for _ in exclude_ids) + ")"
            params.extend(exclude_ids)
        # Prefer resume of in-progress work over brand-new queued (otherwise a
        # restarted partial job sits at 0% forever while new tasks take slots).
        # Within same priority: oldest updated_at first so interrupted jobs recover.
        cur = await self.conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE status IN ({placeholders}){exclude_sql}
            ORDER BY
              CASE status
                WHEN 'resolving' THEN 0
                WHEN 'saving' THEN 0
                WHEN 'downloading' THEN 1
                WHEN 'uploading' THEN 1
                WHEN 'queued' THEN 2
                ELSE 3
              END,
              updated_at ASC,
              id ASC
            LIMIT 1
            """,
            tuple(params),
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
        bad = set(fields) - self._FILE_COLS
        if bad:
            raise ValueError(f"invalid file fields: {bad}")
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [file_id]
        await self.conn.execute(f"UPDATE files SET {cols} WHERE id=?", vals)
        await self.conn.commit()

    async def clear_files(self, job_id: int) -> None:
        await self.conn.execute("DELETE FROM files WHERE job_id=?", (job_id,))
        await self.conn.commit()

    async def get_file(self, file_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_files(self, job_id: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM files WHERE job_id=? ORDER BY id ASC", (job_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def file_status_counts(self, job_id: int) -> dict[str, int]:
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM files WHERE job_id=? GROUP BY status",
            (job_id,),
        )
        rows = await cur.fetchall()
        return {str(r["status"]): int(r["c"]) for r in rows}

    async def recompute_job_progress(self, job_id: int) -> float:
        """Size-weighted progress (SQL) so a 26GB file is not buried by small images.

        BUG-ADV-R2: pure size-weight rounds to 0.00 while dozens of small files
        finish first — file-count floor keeps UI alive.
        ADV-R5: avoid loading 1000+ file rows into Python on every poll.
        """
        cur = await self.conn.execute(
            """
            SELECT
              COUNT(*) AS n_all,
              COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS n_done,
              COALESCE(SUM(CASE WHEN size > 0 THEN size ELSE 1 END), 0) AS total_w,
              COALESCE(SUM(
                (CASE WHEN size > 0 THEN size ELSE 1 END) * (
                  CASE
                    WHEN status = 'done' THEN 1.0
                    ELSE MAX(
                      0.0,
                      MIN(1.0, CAST(downloaded_bytes AS REAL)
                          / (CASE WHEN size > 0 THEN size ELSE 1 END)) * 0.7
                      + MIN(1.0, CAST(uploaded_bytes AS REAL)
                          / (CASE WHEN size > 0 THEN size ELSE 1 END)) * 0.3
                    )
                  END
                )
              ), 0) AS weighted
            FROM files
            WHERE job_id = ?
            """,
            (job_id,),
        )
        row = await cur.fetchone()
        if not row or int(row["n_all"] or 0) == 0:
            return 0.0
        total_w = float(row["total_w"] or 0)
        if total_w <= 0:
            return 0.0
        pct = 100.0 * float(row["weighted"] or 0) / total_w
        n_done = int(row["n_done"] or 0)
        n_all = int(row["n_all"] or 0)
        if n_done and n_all:
            file_pct = 100.0 * n_done / n_all
            pct = max(pct, min(file_pct * 0.15, 15.0))
        if 0 < pct < 0.01:
            return 0.01
        return round(min(100.0, pct), 2)


db = Database()
