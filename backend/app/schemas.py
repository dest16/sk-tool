from datetime import datetime
import re
from urllib.parse import urlparse
from pydantic import BaseModel, Field, SecretStr, field_validator


class SetupRequest(BaseModel):
    setup_token: str = Field(min_length=8, max_length=256)
    username: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    username: str
    password: str


class QBittorrentSettingsRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    username: str = Field(min_length=1, max_length=128)
    # Omitted or blank means retain the existing password.
    password: SecretStr | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlparse(value)
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("地址不能包含空白或控制字符")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("请输入完整的 http 或 https 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("地址不能内嵌账号密码、查询参数或片段")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("端口无效") from exc
        return value

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("请输入有效的 qBittorrent 用户名")
        return value


class QBittorrentSettingsResponse(BaseModel):
    url: str
    username: str
    password_configured: bool
    save_path: str


class SearchResult(BaseModel):
    result_id: str
    title: str
    category: str
    size_text: str
    size_bytes: int | None = None
    published_at: datetime | None = None
    seeders: int = 0
    leechers: int = 0
    completed: int = 0
    magnet_uri: str
    details_url: str | None = None


class SearchResponse(BaseModel):
    items: list[SearchResult]
    page: int
    has_next: bool


class DownloadCreateRequest(BaseModel):
    magnet_uri: str = Field(min_length=20, max_length=4096)
    title: str = Field(default="未命名任务", min_length=1, max_length=500)
    source_url: str | None = None
    auto_move: bool = False


class DownloadActionRequest(BaseModel):
    # Destructive cancellation removes the task's staging directory by
    # default; clients can explicitly opt to retain it.
    delete_files: bool = True


class SyncFilterSettings(BaseModel):
    filename_regex: str | None = Field(default=None, max_length=200)
    min_size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    max_size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)

    @field_validator("filename_regex")
    @classmethod
    def validate_filename_regex(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if any(ord(char) < 32 for char in value):
            raise ValueError("文件名正则不能包含控制字符")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"文件名正则无效：{exc}") from exc
        return value


class DownloadResponse(BaseModel):
    id: str
    gid: str | None
    title: str
    status: str
    auto_move: bool
    total_bytes: int
    completed_bytes: int
    download_speed: float
    eta_seconds: int | None
    error: str | None
    files: list[dict]
    created_at: datetime
    completed_at: datetime | None
    moved_at: datetime | None


class DownloadListResponse(BaseModel):
    items: list[DownloadResponse]


class ActionResponse(BaseModel):
    ok: bool = True
    task: DownloadResponse | None = None
