import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Aria2Error(RuntimeError):
    pass


class Aria2Client:
    def __init__(self, settings, proxy: str | None = None):
        self.settings = settings
        self.proxy = proxy
        self.secret = secrets.token_urlsafe(32)
        self.process: asyncio.subprocess.Process | None = None
        self._start_lock = asyncio.Lock()

    @property
    def endpoint(self) -> str:
        return f"http://{self.settings.aria2_rpc_host}:{self.settings.aria2_rpc_port}/jsonrpc"

    async def start(self) -> None:
        async with self._start_lock:
            if self.process and self.process.returncode is None:
                return
            self.settings.config_dir.mkdir(parents=True, exist_ok=True)
            self.settings.download_dir.mkdir(parents=True, exist_ok=True)
            conf_path = self.settings.config_dir / "aria2.conf"
            config_lines = [
                "enable-rpc=true",
                "rpc-listen-all=false",
                f"rpc-listen-port={self.settings.aria2_rpc_port}",
                f"rpc-secret={self.secret}",
                f"dir={self.settings.download_dir}",
                f"save-session={self.settings.aria2_session_file}",
                f"input-file={self.settings.aria2_session_file}",
                "save-session-interval=10",
                "continue=true",
                "seed-time=0",
                "seed-ratio=0",
                "file-allocation=none",
                "summary-interval=0",
            ]
            if self.proxy:
                config_lines.append(f"all-proxy={self.proxy}")
            conf_path.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
            try:
                conf_path.chmod(0o600)
            except OSError:
                pass
            args = [self.settings.aria2_binary, f"--conf-path={conf_path}"]
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        for _ in range(50):
            try:
                await self.call("aria2.getVersion", [])
                return
            except Exception:
                if self.process and self.process.returncode is not None:
                    stderr = (await self.process.stderr.read()).decode(errors="replace") if self.process.stderr else ""
                    raise Aria2Error(f"aria2 启动失败：{stderr[-500:]}")
                await asyncio.sleep(0.1)
        await self.stop()
        raise Aria2Error("aria2 RPC 在规定时间内未就绪")

    async def stop(self) -> None:
        process = self.process
        if not process:
            return
        if process.returncode is None:
            try:
                await self.call("aria2.saveSession", [])
            except Exception:
                logger.debug("aria2 会话保存失败", exc_info=True)
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self.process = None

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(8),
            "method": method,
            "params": [f"token:{self.secret}", *(params or [])],
        }
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                response = await client.post(self.endpoint, json=payload)
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                raise Aria2Error(f"aria2 RPC 请求失败：{exc}") from exc
        if "error" in result:
            error = result["error"]
            raise Aria2Error(f"aria2：{error.get('message', '未知错误')}")
        return result.get("result")

    async def add_magnet(self, magnet: str, directory: Path) -> str:
        options = {
            "dir": str(directory),
            "seed-time": "0",
            "seed-ratio": "0",
            "bt-stop-timeout": "0",
            "continue": "true",
        }
        return await self.call("aria2.addUri", [[magnet], options])

    async def status(self, gid: str) -> dict:
        fields = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "errorCode",
            "errorMessage",
            "files",
            "dir",
        ]
        return await self.call("aria2.tellStatus", [gid, fields])

    async def pause(self, gid: str) -> Any:
        return await self.call("aria2.pause", [gid])

    async def resume(self, gid: str) -> Any:
        return await self.call("aria2.unpause", [gid])

    async def remove(self, gid: str) -> Any:
        try:
            await self.call("aria2.forceRemove", [gid])
        except Aria2Error as exc:
            if "not found" not in str(exc).lower():
                raise
        try:
            await self.call("aria2.removeDownloadResult", [gid])
        except Aria2Error:
            pass

