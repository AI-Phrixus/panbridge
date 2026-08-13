from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_SECRET = "dev-secret-change-me-please-32chars"
_DEFAULT_PASSWORD = "admin"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    panbridge_secret: str = _DEFAULT_SECRET
    admin_password: str = _DEFAULT_PASSWORD
    host: str = "0.0.0.0"
    port: int = 8080
    data_dir: str = "./data"
    # Keep 1 job on small VPS; raise on bigger machines
    max_concurrent_jobs: int = 1
    download_chunk_size: int = 1_048_576
    # Multi-connection download (auto-fallback if server rejects Range)
    download_connections: int = 6
    # Baidu: try multi Range with LogStatistic; falls back to 1 on 403
    baidu_download_connections: int = 4
    # Keep free space buffer when downloading (bytes)
    disk_reserve_bytes: int = 2 * 1024 * 1024 * 1024
    pcloud_api_host: str = "eapi.pcloud.com"
    pcloud_default_path: str = "/PanBridge"
    session_max_age: int = 60 * 60 * 24 * 30
    # Signed player URLs work outside the browser session (VLC/IINA/etc.).
    stream_token_max_age: int = 60 * 60 * 24 * 7
    # Optional canonical external URL, e.g. https://panbridge.example.com.
    public_base_url: str = ""
    app_version: str = "0.4.3"

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "app.db"

    @property
    def secrets_path(self) -> Path:
        p = self.data_path / "secrets"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tmp_path(self) -> Path:
        p = self.data_path / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p


def validate_runtime_security(settings: Settings) -> None:
    """Fail closed instead of exposing public, forgeable default credentials."""
    secret = settings.panbridge_secret.strip()
    password = settings.admin_password.strip()
    bad_secret = (
        secret == _DEFAULT_SECRET
        or "change-me" in secret.lower()
        or len(secret) < 32
    )
    bad_password = (
        password == _DEFAULT_PASSWORD
        or "change-me" in password.lower()
        or len(password) < 10
    )
    if bad_secret or bad_password:
        missing = []
        if bad_secret:
            missing.append("PANBRIDGE_SECRET（至少 32 字元隨機值）")
        if bad_password:
            missing.append("ADMIN_PASSWORD（至少 10 字元強密碼）")
        raise RuntimeError(
            "安全設定未完成，服務拒絕啟動：請在 .env 設定 " + "、".join(missing)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
