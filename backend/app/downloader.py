from pathlib import Path
from typing import Any, Protocol


class DownloaderError(RuntimeError):
    """Raised when the configured downloader cannot complete an operation."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class Downloader(Protocol):
    proxy: str | None

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def version(self) -> dict[str, Any]: ...

    async def add_magnet(self, magnet: str, directory: Path) -> str: ...

    async def status(self, gid: str) -> dict[str, Any]: ...

    async def pause(self, gid: str) -> Any: ...

    async def resume(self, gid: str) -> Any: ...

    async def remove(self, gid: str) -> Any: ...

    async def remove_download_result(self, gid: str) -> Any: ...


def is_metadata_file(file_info: dict[str, Any]) -> bool:
    """Recognise the temporary metadata file exposed by legacy adapters."""
    path = str(file_info.get("path") or "").replace("\\", "/")
    return path.rsplit("/", 1)[-1].lower().startswith("[metadata]")
