from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class SourceFile:
    fid: str
    name: str
    size: int
    relative_path: str = ""
    is_dir: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedShare:
    title: str
    files: list[SourceFile]
    meta: dict[str, Any] = field(default_factory=dict)


class ShareSource(Protocol):
    async def resolve(self, share_url: str, passcode: str = "") -> ResolvedShare: ...

    async def prepare_download(self, file: SourceFile, share_meta: dict[str, Any]) -> str:
        """Return a direct download URL (may require auth headers via get_download_headers)."""
        ...

    def get_download_headers(self) -> dict[str, str]: ...
