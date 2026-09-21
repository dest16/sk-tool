from pathlib import Path
from urllib.parse import parse_qs

import httpx

from app.config import Settings
from app.qbittorrent import QBittorrentClient, QBittorrentError


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        config_dir=tmp_path / "config",
        download_dir=tmp_path / "downloads",
        library_dir=tmp_path / "library",
        qbittorrent_url="http://qb.test:8080",
        qbittorrent_username="admin",
        qbittorrent_password="secret",
        qbittorrent_save_path="/downloads/18x",
    )


def _client(tmp_path: Path, handler):
    client = QBittorrentClient(_settings(tmp_path))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_qbittorrent_add_and_status_map_remote_paths(tmp_path: Path):
    calls: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, parse_qs(request.content.decode())))
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=test; Path=/"})
        if request.url.path.endswith("/app/version"):
            return httpx.Response(200, text="v5.2.3")
        if request.url.path.endswith("/torrents/add"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/info"):
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "a" * 40,
                        "state": "downloading",
                        "progress": 0.2,
                        "size": 100,
                        "completed": 20,
                        "dlspeed": 40,
                        "eta": 2,
                        "save_path": "/downloads/18x",
                        "content_path": "/downloads/18x/Example",
                    }
                ],
            )
        if request.url.path.endswith("/torrents/files"):
            return httpx.Response(200, json=[{"name": "video.mkv", "size": 100, "progress": 0.2, "priority": 1}])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    downloader = _client(tmp_path, handler)
    magnet = "magnet:?xt=urn:btih:" + "A" * 40

    await downloader.start()
    assert await downloader.add_magnet(magnet, tmp_path / "downloads") == "a" * 40
    result = await downloader.status("a" * 40)
    await downloader.stop()

    assert result["status"] == "active"
    assert result["totalLength"] == "100"
    assert result["completedLength"] == "20"
    assert result["files"][0]["path"] == str(tmp_path / "downloads" / "Example" / "video.mkv")
    add_request = next(item for item in calls if item[1].endswith("/torrents/add"))
    assert add_request[2]["savepath"] == ["/downloads/18x"]
    assert add_request[2]["autoTMM"] == ["false"]


async def test_qbittorrent_completed_and_delete_keep_data(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/app/version"):
            return httpx.Response(200, text="v5.2.3")
        if request.url.path.endswith("/torrents/info"):
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "b" * 40,
                        "state": "stalledUP",
                        "progress": 1,
                        "size": 100,
                        "completed": 100,
                        "save_path": "/downloads/18x",
                        "content_path": "/downloads/18x/file.mkv",
                    }
                ],
            )
        if request.url.path.endswith("/torrents/files"):
            return httpx.Response(200, json=[{"name": "file.mkv", "size": 100, "progress": 1, "priority": 1}])
        if request.url.path.endswith("/torrents/delete"):
            assert parse_qs(request.content.decode()) == {"hashes": ["b" * 40], "deleteFiles": ["false"]}
            return httpx.Response(200, text="Ok.")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    downloader = _client(tmp_path, handler)
    result = await downloader.status("b" * 40)
    assert result["status"] == "complete"
    assert result["files"][0]["path"] == str(tmp_path / "downloads" / "file.mkv")
    await downloader.remove_download_result("b" * 40)
    await downloader.stop()


async def test_qbittorrent_rejects_paths_outside_configured_mount(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/info"):
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "c" * 40,
                        "state": "downloading",
                        "progress": 0.5,
                        "size": 100,
                        "save_path": "/downloads",
                        "content_path": "/downloads/outside",
                    }
                ],
            )
        if request.url.path.endswith("/torrents/files"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    downloader = _client(tmp_path, handler)
    try:
        await downloader.status("c" * 40)
    except QBittorrentError as exc:
        assert "不在已映射目录" in str(exc)
    else:
        raise AssertionError("an unmapped qBittorrent path must be rejected")
    finally:
        await downloader.stop()
