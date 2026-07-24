from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.util_paths import safe_under_root, sanitize_rel_path


class LocalSink:
    """Keep finished files on VPS for user download (best free option for huge files)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    async def upload_file(
        self,
        local_path: Path,
        remote_folder_path: str,
        filename: str,
        progress_cb: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        rel = sanitize_rel_path(remote_folder_path)
        safe_name = Path(sanitize_rel_path(filename)).name or "file"
        if rel:
            dest = safe_under_root(self.root, rel, safe_name)
            dest_dir = dest.parent
        else:
            dest = safe_under_root(self.root, safe_name)
            dest_dir = dest.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        size = local_path.stat().st_size
        if dest.exists() and dest.stat().st_size == size:
            if progress_cb:
                await progress_cb(size, size)
            return {"path": str(dest), "size": size, "skipped": True}

        if progress_cb:
            await progress_cb(0, size)
        try:
            local_path.replace(dest)
        except OSError:
            shutil.copy2(local_path, dest)
            local_path.unlink(missing_ok=True)
        if progress_cb:
            await progress_cb(size, size)
        return {"path": str(dest), "size": size, "fileid": str(dest)}
