from __future__ import annotations

import shutil
from pathlib import Path


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    return int(usage.free)


def ensure_space(path: Path, need: int, reserve: int = 2 * 1024 * 1024 * 1024) -> None:
    """Raise if free space < need + reserve (default reserve 2GB).

    For huge single files on small VPS disks, shrink reserve so a 25GB file can
    still start when free ≈ need + a few hundred MB (keep at least 512MB buffer).
    """
    free = free_bytes(path)
    need_i = max(0, int(need))
    reserve_i = int(reserve)
    # Adaptive reserve: never demand more than 25% of free, floor 512MB
    if need_i > 0 and free > 0:
        adaptive = max(512 * 1024 * 1024, min(reserve_i, int(free * 0.25)))
        # If need itself is huge relative to disk, use the smaller adaptive reserve
        if need_i + reserve_i > free:
            reserve_i = adaptive
    required = need_i + reserve_i
    if free < required:
        raise RuntimeError(
            f"磁碟空間不足：需要約 {required/1024/1024/1024:.1f} GB"
            f"（檔案約 {need_i/1024/1024/1024:.1f} GB + 預留），"
            f"目前可用 {free/1024/1024/1024:.1f} GB。請清理 data 或擴容後再試。"
        )
