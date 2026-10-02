"""Bounded disk-backed overlap; never stream an incomplete file to a sink."""
from __future__ import annotations

import asyncio
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from app.transfer.disk import free_bytes


class StagingCapacityError(RuntimeError):
    pass


@dataclass
class Reservation:
    size: int
    paths: tuple[Path, ...]

    def present(self, *, allocated: bool = False) -> int:
        total = 0
        for path in self.paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("暫存文件不是普通文件，已停止搬運")
            total += info.st_blocks * 512 if allocated else info.st_size
        return total


class PipelineResources:
    """One worker-wide budget, so concurrent jobs cannot overbook the disk."""

    def __init__(self, root: Path, max_files: int, max_bytes: int, reserve: int):
        self.root = root
        self.max_files = max(1, min(3, max_files))
        self.max_bytes = max(1, max_bytes)
        self.reserve = max(512 * 1024**2, reserve)
        self.download_slot = asyncio.Semaphore(1)
        self.upload_slot = asyncio.Semaphore(1)
        self._condition = asyncio.Condition()
        self._held: dict[int, Reservation] = {}

    def staged_bytes(self) -> int:
        # Includes retained failed files and prior-process checkpoints. A failed
        # upload releasing its lease is NOT the same thing as freeing its disk.
        total = 0
        for path in self.root.rglob("*"):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            elif stat.S_ISLNK(info.st_mode):
                raise RuntimeError("暫存目錄含符號連結，已停止搬運")
        return total

    @asynccontextmanager
    async def reserve_file(self, file_id: int, size: int, paths: tuple[Path, ...]):
        candidate = Reservation(max(0, int(size)), paths)
        async with self._condition:
            if file_id in self._held:
                raise RuntimeError("同一文件已在搬運，拒絕重複寫入")
            while True:
                held = list(self._held.values())
                staged = self.staged_bytes()
                logical_extra = sum(max(0, r.size - r.present()) for r in held)
                logical_extra += max(0, candidate.size - candidate.present())
                planned = staged + logical_extra
                # A single file larger than the cache budget may run alone,
                # provided it physically fits and no other retained payloads
                # consume that budget. Unknown lengths also run exclusively.
                exclusive = candidate.size == 0 or candidate.size > self.max_bytes
                own = candidate.present()
                budget_ok = planned <= self.max_bytes or (
                    exclusive and not held and staged <= own + 1024**2
                )
                count_ok = len(held) < self.max_files and not (
                    exclusive and held
                ) and not any(r.size == 0 or r.size > self.max_bytes for r in held)
                needed = sum(max(0, r.size - r.present(allocated=True)) for r in held)
                needed += max(0, candidate.size - candidate.present(allocated=True))
                disk_ok = free_bytes(self.root) >= needed + self.reserve
                if count_ok and budget_ok and disk_ok:
                    self._held[file_id] = candidate
                    break
                if not held:
                    raise StagingCapacityError("暫存預算或磁碟安全空間不足；進度保留，請釋放空間後繼續")
                await self._condition.wait()
        try:
            yield
        finally:
            async with self._condition:
                self._held.pop(file_id, None)
                self._condition.notify_all()


class PipelineProgress:
    """One publisher per job; stage callbacks must not overwrite each other."""

    def __init__(self, db, job_id: int, resources: PipelineResources):
        self.db, self.job_id, self.resources = db, job_id, resources
        self._states: dict[int, tuple[str, float, float]] = {}
        self._lock = asyncio.Lock()
        self._last_publish = 0.0
        self.failure: Exception | None = None

    def abort(self, error: Exception):
        # Latch before releasing a stage slot: a queued downloader must not
        # issue another request between a hard failure and supervisor wakeup.
        if self.failure is None:
            self.failure = error

    async def report(self, file_id: int, phase: str, speed: float = 0.0, *, force=False):
        async with self._lock:
            if self.failure is not None:
                raise asyncio.CancelledError()
            now = time.monotonic()
            old = self._states.get(file_id)
            self._states[file_id] = (phase, max(0.0, speed), now)
            changed = not old or old[0] != phase
            if not force and not changed and now - self._last_publish < 0.8:
                return
            await self._publish(now)

    async def remove(self, file_id: int):
        async with self._lock:
            self._states.pop(file_id, None)
            await self._publish(time.monotonic())

    async def _publish(self, now: float):
        counts = {stage: sum(value[0] == stage for value in self._states.values())
                  for stage in ("downloading", "uploading", "buffered", "waiting")}
        rates = {stage: sum(value[1] for value in self._states.values()
                           if value[0] == stage and now - value[2] <= 5)
                 for stage in ("downloading", "uploading")}
        progress = await self.db.recompute_job_progress(self.job_id)
        phase = "uploading" if counts["uploading"] else "downloading"
        detail = (f"流水線 · 下載 {counts['downloading']} · 上傳 {counts['uploading']}"
                  f" · 暫存等待 {counts['buffered'] + counts['waiting']}"
                  f" · ↓ {rates['downloading'] / 1024**2:.2f} MB/s"
                  f" · ↑ {rates['uploading'] / 1024**2:.2f} MB/s")
        if not await self.db.update_transfer_job(
            self.job_id, status=phase, progress=progress,
            speed_bps=rates["downloading"] + rates["uploading"], status_detail=detail,
        ):
            raise asyncio.CancelledError()
        self._last_publish = now
