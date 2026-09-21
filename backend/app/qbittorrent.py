import asyncio
import base64
import binascii
import logging
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from .downloader import DownloaderError
from .indexer import btih_from_magnet

logger = logging.getLogger(__name__)


class QBittorrentError(DownloaderError):
    """Raised when qBittorrent's Web API is unavailable or rejects a request."""


class QBittorrentClient:
    """Async qBittorrent Web API adapter with safe remote/local path mapping."""

    def __init__(self, settings, proxy: str | None = None):
        self.settings = settings
        self.proxy = proxy
        self._client: httpx.AsyncClient | None = None
        self._started = False
        self._authenticated = False
        self._start_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._base_url = self._normalise_base_url(settings.qbittorrent_url)
        parsed_base = urlparse(self._base_url)
        self._origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        self._remote_download_root = self._normalise_remote_path(settings.qbittorrent_save_path)
        self._local_download_root = Path(settings.download_dir).resolve()

    @staticmethod
    def _normalise_base_url(value: str) -> str:
        parsed = urlparse(str(value).strip().rstrip("/"))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise QBittorrentError("qBittorrent 地址必须是带主机名的 http 或 https URL")
        if parsed.username or parsed.password:
            raise QBittorrentError("qBittorrent 地址不能内嵌账号密码")
        return f"{parsed.scheme.lower()}://{parsed.netloc}{parsed.path.rstrip('/') or ''}"

    @staticmethod
    def _normalise_remote_path(value: str) -> PurePosixPath:
        raw = str(value or "").replace("\\", "/").strip()
        path = PurePosixPath(raw)
        if not raw or not path.is_absolute() or ".." in path.parts:
            raise QBittorrentError("qBittorrent 下载路径必须是绝对路径且不能包含 ..")
        return path

    @property
    def api_url(self) -> str:
        return f"{self._base_url}/api/v2"

    @property
    def process(self):
        """Compatibility attribute for older integrations; qBittorrent is external."""
        return self if self._started else None

    def _headers(self) -> dict[str, str]:
        return {
            "Referer": f"{self._base_url}/",
            "Origin": self._origin,
            "User-Agent": "SukebeiManager/1.0",
        }

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "timeout": getattr(self.settings, "qbittorrent_timeout_seconds", 8.0),
                "follow_redirects": True,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def _login(self, *, force: bool = False) -> None:
        async with self._auth_lock:
            if self._authenticated and not force:
                return
            client = await self._ensure_client()
            username = str(self.settings.qbittorrent_username or "")
            password = self.settings.qbittorrent_password.get_secret_value()
            if not username or not password:
                raise QBittorrentError("未配置 qBittorrent 账号或密码")
            try:
                response = await client.post(
                    f"{self.api_url}/auth/login",
                    data={"username": username, "password": password},
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                raise QBittorrentError(f"qBittorrent 登录请求失败：{exc}") from exc
            body = response.text.strip().lower()
            if response.status_code not in {200, 204} or (body and body not in {"ok", "ok."}):
                detail = response.text[:200].strip() or response.reason_phrase
                raise QBittorrentError(f"qBittorrent 登录失败：{detail}", status_code=response.status_code)
            self._authenticated = True

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json: Any = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        client = await self._ensure_client()
        if authenticated:
            await self._login()
        for attempt in range(2):
            try:
                response = await client.request(
                    method,
                    f"{self.api_url}{endpoint}",
                    params=params,
                    data=data,
                    json=json,
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                raise QBittorrentError(f"qBittorrent API 请求失败：{exc}") from exc
            if authenticated and response.status_code in {401, 403} and attempt == 0:
                self._authenticated = False
                await self._login(force=True)
                continue
            if response.status_code < 200 or response.status_code >= 300:
                detail = response.text[:500].strip() or response.reason_phrase
                raise QBittorrentError(
                    f"qBittorrent API HTTP {response.status_code}：{detail}",
                    status_code=response.status_code,
                )
            return response
        raise QBittorrentError("qBittorrent API 认证重试失败")

    async def _json(self, method: str, endpoint: str, **kwargs) -> Any:
        response = await self._request(method, endpoint, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise QBittorrentError(f"qBittorrent API 返回了无效 JSON：{response.text[:300]}") from exc

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            try:
                await self.version()
            except Exception:
                await self.stop()
                raise
            self._started = True

    async def stop(self) -> None:
        client = self._client
        self._client = None
        self._started = False
        self._authenticated = False
        if client is not None:
            await client.aclose()

    async def version(self) -> dict[str, Any]:
        response = await self._request("GET", "/app/version")
        value = response.text
        return {"version": value.strip()}

    def _local_to_remote_save_path(self, directory: Path) -> str:
        local = Path(directory).resolve()
        if local != self._local_download_root:
            raise QBittorrentError(
                f"下载目录 {local} 未映射到 qBittorrent 保存目录 {self._remote_download_root}"
            )
        return str(self._remote_download_root)

    def _remote_to_local(self, value: object) -> Path:
        raw = str(value or "").replace("\\", "/")
        remote = PurePosixPath(raw)
        if not remote.is_absolute() or ".." in remote.parts:
            raise QBittorrentError("qBittorrent 返回了不安全的下载路径")
        try:
            relative = remote.relative_to(self._remote_download_root)
        except ValueError as exc:
            raise QBittorrentError(
                f"qBittorrent 任务路径 {remote} 不在已映射目录 {self._remote_download_root} 内"
            ) from exc
        local = self._local_download_root.joinpath(*relative.parts).resolve()
        if local != self._local_download_root and not local.is_relative_to(self._local_download_root):
            raise QBittorrentError("qBittorrent 路径映射越出本地下载目录")
        return local

    @staticmethod
    def _join_remote(parent: object, child: object) -> PurePosixPath:
        child_path = PurePosixPath(str(child or "").replace("\\", "/"))
        if child_path.is_absolute() or ".." in child_path.parts:
            raise QBittorrentError("qBittorrent 返回了不安全的文件路径")
        return PurePosixPath(str(parent).replace("\\", "/")) / child_path

    @staticmethod
    def _normalise_hash(magnet: str) -> str:
        btih = btih_from_magnet(magnet)
        if not btih:
            raise QBittorrentError("磁力链接缺少可识别的 BTIH")
        if len(btih) == 40:
            return btih.lower()
        try:
            encoded = btih.upper().encode("ascii")
            padded = encoded + b"=" * ((8 - len(encoded) % 8) % 8)
            return base64.b32decode(padded).hex()
        except (binascii.Error, ValueError) as exc:
            raise QBittorrentError("磁力链接中的 BTIH 编码无效") from exc

    async def add_magnet(self, magnet: str, directory: Path) -> str:
        gid = self._normalise_hash(magnet)
        response = await self._request(
            "POST",
            "/torrents/add",
            data={
                "urls": magnet,
                "savepath": self._local_to_remote_save_path(directory),
                "paused": "false",
                "autoTMM": "false",
            },
        )
        if response.text.strip().lower() != "ok.":
            raise QBittorrentError(f"qBittorrent 添加任务失败：{response.text[:300]}")
        return gid

    @staticmethod
    def _state(state: str, progress: float, total: int) -> str:
        if state in {"error", "missingFiles"}:
            return "error"
        if state == "checkingDL":
            return "active"
        if progress >= 1 or state in {"uploading", "stalledUP", "queuedUP", "forcedUP", "checkingUP"}:
            return "complete"
        if state in {"pausedDL", "pausedUP"}:
            return "paused"
        if state == "metaDL" or (not total and progress < 1):
            return "metadata"
        if state in {"queuedDL", "checkingDL"}:
            return "waiting"
        return "active"

    async def status(self, gid: str) -> dict[str, Any]:
        torrents = await self._json("GET", "/torrents/info", params={"hashes": gid})
        if not isinstance(torrents, list) or not torrents:
            raise QBittorrentError(f"qBittorrent 任务不存在：{gid}", status_code=404)
        torrent = torrents[0]
        progress = float(torrent.get("progress") or 0)
        total = int(torrent.get("total_size") or torrent.get("size") or 0)
        completed = int(torrent.get("completed") or 0)
        remote_content_path = torrent.get("content_path")
        files_payload: list[dict[str, Any]] = []
        if remote_content_path:
            files_payload = await self._json("GET", "/torrents/files", params={"hash": gid})
            if not isinstance(files_payload, list):
                files_payload = []
        files: list[dict[str, str]] = []
        for item in files_payload:
            item_name = str(item.get("name") or "")
            content_path = PurePosixPath(str(remote_content_path).replace("\\", "/"))
            item_path = PurePosixPath(item_name.replace("\\", "/"))
            # qBittorrent reports a single-file torrent's content_path as the
            # file itself, while multi-file torrents use the content directory.
            if len(files_payload) == 1 and content_path.name == PurePosixPath(item_name).name:
                remote_file = content_path
            elif item_path.parts and item_path.parts[0] == content_path.name:
                # Some qBittorrent versions include the torrent root directory
                # in the returned relative file name; avoid joining it twice.
                remote_file = content_path.joinpath(*item_path.parts[1:])
            else:
                remote_file = self._join_remote(remote_content_path, item_name)
            local_file = self._remote_to_local(remote_file)
            length = int(item.get("size") or 0)
            item_completed = int(round(length * float(item.get("progress") or 0)))
            files.append(
                {
                    "path": str(local_file),
                    "length": str(length),
                    "completedLength": str(item_completed),
                    "selected": "true" if int(item.get("priority") or 0) > 0 else "false",
                }
            )
        if not total:
            total = sum(int(item["length"]) for item in files)
        if not completed and files:
            completed = sum(int(item["completedLength"]) for item in files)
        content_path = str(self._remote_to_local(remote_content_path)) if remote_content_path else None
        state = str(torrent.get("state") or "unknown")
        normalised_state = self._state(state, progress, total)
        error_message = None
        if normalised_state == "error":
            error_message = f"qBittorrent 状态：{state}"
        eta = int(torrent.get("eta") or 0)
        if eta >= 8640000:
            eta = 0
        return {
            "gid": str(torrent.get("hash") or gid),
            "status": normalised_state,
            "totalLength": str(total),
            "completedLength": str(completed),
            "downloadSpeed": str(int(torrent.get("dlspeed") or 0)),
            "errorCode": "1" if error_message else "0",
            "errorMessage": error_message,
            "files": files,
            "dir": str(self._remote_to_local(torrent.get("save_path") or self._remote_download_root)),
            "contentPath": content_path,
            "followedBy": [],
            "eta": eta,
        }

    async def pause(self, gid: str) -> Any:
        return await self._request("POST", "/torrents/pause", data={"hashes": gid})

    async def resume(self, gid: str) -> Any:
        return await self._request("POST", "/torrents/resume", data={"hashes": gid})

    async def remove(self, gid: str) -> Any:
        try:
            return await self._request(
                "POST",
                "/torrents/delete",
                data={"hashes": gid, "deleteFiles": "false"},
            )
        except QBittorrentError as exc:
            if exc.status_code == 404 or "not found" in str(exc).lower():
                return None
            raise

    async def remove_download_result(self, gid: str) -> Any:
        return await self.remove(gid)
