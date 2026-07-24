from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    panbridge_secret: str = "dev-secret-change-me-please-32chars"
    admin_password: str = "admin"
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
    app_version: str = "0.3.7"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
