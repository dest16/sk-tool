from functools import lru_cache
import os
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUKEBEI_", case_sensitive=False)

    app_name: str = "Sukebei 下载管理器"
    host: str = "0.0.0.0"
    port: int = 8080
    config_dir: Path = Path("/config") if os.name != "nt" else Path("data/config")
    download_dir: Path = Path("/downloads") if os.name != "nt" else Path("data/downloads")
    library_dir: Path = Path("/library") if os.name != "nt" else Path("data/library")
    indexer_base_url: str = "https://sukebei.nyaa.si/"
    request_timeout_seconds: float = Field(default=20.0, ge=2, le=120)
    search_cache_seconds: int = Field(default=30, ge=0, le=3600)
    session_days: int = Field(default=7, ge=1, le=90)
    cookie_secure: bool = False
    qbittorrent_url: str = "http://10.10.0.213:8080"
    qbittorrent_username: str = ""
    qbittorrent_password: SecretStr = SecretStr("")
    # Path as seen inside the external qBittorrent container. The Unraid
    # deployment maps /downloads/18x there to this application's /downloads.
    qbittorrent_save_path: str = "/downloads/18x"
    qbittorrent_timeout_seconds: float = Field(default=8.0, ge=2, le=120)

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{(self.config_dir / 'app.db').as_posix()}"

    @property
    def setup_token_file(self) -> Path:
        return self.config_dir / "setup-token"



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

