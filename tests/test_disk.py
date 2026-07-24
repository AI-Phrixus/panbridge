from pathlib import Path
import pytest
from app.transfer.disk import free_bytes, ensure_space


def test_free_bytes(tmp_path: Path):
    assert free_bytes(tmp_path) > 0


def test_ensure_space_ok(tmp_path: Path):
    ensure_space(tmp_path, need=1024, reserve=0)


def test_ensure_space_fail(tmp_path: Path):
    with pytest.raises(RuntimeError):
        ensure_space(tmp_path, need=10**18, reserve=0)
