import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


def is_metadata_file(file_info: dict[str, Any]) -> bool:
    """Keep the manager's file filtering compatible with its old adapter."""
    path = str(file_info.get("path") or "").replace("\\", "/")
    return path.rsplit("/", 1)[-1].lower().startswith("[metadata]")


class TransmissionError(RuntimeError):
    """Raised when the Transmission RPC or daemon is unavailable."""

    def __init__(self, message: str, *, code: int | str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TransmissionClient:
    """Small async adapter around transmission-daemon's JSON RPC API."""

    def __init__(self, settings, proxy: str | None = None):
        self.settings = settings
        self.proxy = proxy
        self.process: asyncio.subprocess.Process | None = None
        self._start_lock = asyncio.Lock()
        self._session_id: str | None = None

    @property
    def endpoint(self) -> str:
        return f"http://{self.settings.transmission_rpc_host}:{self.settings.transmission_rpc_port}/transmission/rpc"

    @property
    def config_dir(self) -> Path:
        return self.settings.config_dir / "transmission"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    def _proxy_settings(self) -> dict[str, Any]:
        if not self.proxy:
            return {"proxy-enabled": False}
        parsed = urlparse(self.proxy)
        if not parsed.hostname or not parsed.port:
            logger.warning("下载代理地址无法转换为 Transmission 设置，将忽略代理")
            return {"proxy-enabled": False}
        scheme = parsed.scheme.lower()
        proxy_type = {"http": 0, "https": 0, "socks5": 2, "socks5h": 2}.get(scheme)
        if proxy_type is None:
            logger.warning("Transmission 不支持此下载代理协议：%s", scheme)
            return {"proxy-enabled": False}
        values: dict[str, Any] = {
            "proxy-enabled": True,
            "proxy-server": parsed.hostname,
            "proxy-port": parsed.port,
            "proxy-type": proxy_type,
        }
        if parsed.username is not None:
            values.update(
                {
                    "proxy-auth-enabled": True,
                    "proxy-auth-username": parsed.username,
                    "proxy-auth-password": parsed.password or "",
                }
            )
        return values

    def _write_settings(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        values: dict[str, Any] = {}
        if self.settings_file.exists():
            try:
                values = json.loads(self.settings_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TransmissionError(f"Transmission 配置文件无效：{self.settings_file}") from exc
        values.update(
            {
                "download-dir": str(self.settings.download_dir),
                "incomplete-dir-enabled": False,
                "peer-port": self.settings.aria2_p2p_port,
                "peer-port-random-on-start": False,
                "port-forwarding-enabled": True,
                "dht-enabled": True,
                "pex-enabled": True,
                "lpd-enabled": True,
                "rpc-enabled": True,
                "rpc-bind-address": self.settings.transmission_rpc_host,
                "rpc-port": self.settings.transmission_rpc_port,
                "rpc-whitelist-enabled": True,
                "rpc-whitelist": "127.0.0.1",
                "start-added-torrents": True,
            }
        )
        values.update(self._proxy_settings())
        self.settings_file.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            self.settings_file.chmod(0o600)
        except OSError:
            pass

    async def start(self) -> None:
        async with self._start_lock:
            if self.process and self.process.returncode is None:
                return
            self.settings.config_dir.mkdir(parents=True, exist_ok=True)
            self.settings.download_dir.mkdir(parents=True, exist_ok=True)
            self._write_settings()
            args = [
                self.settings.transmission_binary,
                "--foreground",
                "--config-dir",
                str(self.config_dir),
            ]
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._session_id = None
        for _ in range(50):
            try:
                await self.call("session-get")
                return
            except Exception:
                if self.process and self.process.returncode is not None:
                    stderr = (await self.process.stderr.read()).decode(errors="replace") if self.process.stderr else ""
                    raise TransmissionError(f"Transmission 启动失败：{stderr[-500:]}")
                await asyncio.sleep(0.1)
        await self.stop()
        raise TransmissionError("Transmission RPC 在规定时间内未就绪")

    async def stop(self) -> None:
        process = self.process
        if not process:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self.process = None
        self._session_id = None

    async def call(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"method": method, "arguments": arguments or {}, "tag": secrets.token_hex(8)}
        headers = {"X-Transmission-Session-Id": self._session_id} if self._session_id else {}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                response = await client.post(self.endpoint, json=payload, headers=headers)
                if response.status_code == 409:
                    session_id = response.headers.get("X-Transmission-Session-Id")
                    if not session_id:
                        raise TransmissionError("Transmission RPC 未返回会话标识")
                    self._session_id = session_id
                    response = await client.post(
                        self.endpoint,
                        json=payload,
                        headers={"X-Transmission-Session-Id": session_id},
                    )
            except httpx.HTTPError as exc:
                raise TransmissionError(f"Transmission RPC 请求失败：{exc}") from exc
        try:
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPStatusError, json.JSONDecodeError) as exc:
            raise TransmissionError(f"Transmission RPC HTTP {response.status_code}：{response.text[:500]}") from exc
        if result.get("result") != "success":
            raise TransmissionError(f"Transmission：{result.get('result', '未知错误')}")
        return result.get("arguments") or {}

    async def version(self) -> dict[str, Any]:
        return await self.call("session-get")

    async def add_magnet(self, magnet: str, directory: Path) -> str:
        result = await self.call(
            "torrent-add",
            {
                "filename": magnet,
                "download-dir": str(directory),
                "paused": False,
            },
        )
        added = result.get("torrent-added") or result.get("torrent-duplicate")
        if not added:
            raise TransmissionError("Transmission 未返回新增任务")
        identifier = added.get("hashString") or added.get("id")
        if identifier is None:
            raise TransmissionError("Transmission 未返回任务标识")
        return str(identifier)

    async def status(self, gid: str) -> dict[str, Any]:
        fields = [
            "id",
            "hashString",
            "name",
            "status",
            "totalSize",
            "percentDone",
            "rateDownload",
            "error",
            "errorString",
            "downloadDir",
            "files",
            "metadataPercentComplete",
        ]
        result = await self.call("torrent-get", {"ids": [gid], "fields": fields})
        torrents = result.get("torrents") or []
        if not torrents:
            raise TransmissionError(f"Transmission 任务不存在：{gid}")
        torrent = torrents[0]
        download_dir = Path(torrent.get("downloadDir") or self.settings.download_dir)
        torrent_name = str(torrent.get("name") or "")
        files = [
            {
                "path": str(download_dir / str(item.get("name") or "")),
                "length": str(item.get("length") or 0),
                "completedLength": str(item.get("bytesCompleted") or 0),
                "selected": "true",
            }
            for item in torrent.get("files") or []
        ]
        content_path = str(download_dir / torrent_name) if files and torrent_name else None
        total = sum(int(item["length"]) for item in files)
        completed = sum(int(item["completedLength"]) for item in files)
        if not total:
            total = int(torrent.get("totalSize") or 0)
        error = int(torrent.get("error") or 0)
        transmission_state = int(torrent.get("status") or 0)
        if error:
            state = "error"
        elif float(torrent.get("percentDone") or 0) >= 1:
            state = "complete"
        elif transmission_state == 0:
            state = "paused"
        elif transmission_state in {1, 2, 3}:
            state = "waiting"
        else:
            state = "active"
        return {
            "gid": str(torrent.get("hashString") or torrent.get("id") or gid),
            "status": state,
            "totalLength": str(total),
            "completedLength": str(completed),
            "downloadSpeed": str(torrent.get("rateDownload") or 0),
            "errorCode": str(error),
            "errorMessage": torrent.get("errorString") or None,
            "files": files,
            "dir": str(download_dir),
            "contentPath": content_path,
            "followedBy": [],
        }

    async def pause(self, gid: str) -> Any:
        return await self.call("torrent-stop", {"ids": [gid]})

    async def resume(self, gid: str) -> Any:
        return await self.call("torrent-start", {"ids": [gid]})

    async def remove(self, gid: str) -> Any:
        try:
            return await self.call("torrent-remove", {"ids": [gid], "delete-local-data": False})
        except TransmissionError as exc:
            if self._is_gone_error(exc):
                return None
            raise

    async def remove_download_result(self, gid: str) -> Any:
        return await self.remove(gid)

    @staticmethod
    def _is_gone_error(exc: TransmissionError) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in ("no such torrent", "not found", "unknown torrent"))


# Kept as a compatibility alias for extensions importing the old adapter name.
Aria2Error = TransmissionError

