from functools import lru_cache
import os
from pathlib import Path

from pydantic import Field
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
    aria2_rpc_host: str = "127.0.0.1"
    aria2_rpc_port: int = 6800
    # BitTorrent/DHT listen port, exposed by Docker over TCP and UDP.
    aria2_p2p_port: int = Field(default=51413, ge=1024, le=65535)
    transmission_binary: str = "transmission-daemon"
    transmission_rpc_host: str = "127.0.0.1"
    transmission_rpc_port: int = 9091

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{(self.config_dir / 'app.db').as_posix()}"

    @property
    def setup_token_file(self) -> Path:
        return self.config_dir / "setup-token"

    @property
    def aria2_session_file(self) -> Path:
        return self.config_dir / "aria2.session"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


