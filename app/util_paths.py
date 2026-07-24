"""Path sanitization helpers (path traversal defense)."""
from __future__ import annotations

from pathlib import Path


def sanitize_rel_path(path: str) -> str:
    """Normalize a relative path; strip ``..`` and absolute roots."""
    raw = (path or "").replace("\\", "/").strip()
    parts: list[str] = []
    for p in raw.split("/"):
        if not p or p == ".":
            continue
        if p == "..":
            continue
        # drop nulls / weird control
        p = p.replace("\x00", "")
        if p:
            parts.append(p)
    return "/".join(parts)


def safe_under_root(root: Path, *parts: str) -> Path:
    """Resolve path under root; raise if escape attempted."""
    root = root.resolve()
    rel = sanitize_rel_path("/".join(str(p) for p in parts))
    dest = (root / rel).resolve() if rel else root
    if dest != root and root not in dest.parents:
        raise RuntimeError(f"path escapes root: {dest}")
    return dest
