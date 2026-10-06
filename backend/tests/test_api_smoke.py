import os
import tempfile
from pathlib import Path

os.environ["SUKEBEI_CONFIG_DIR"] = tempfile.mkdtemp(prefix="sukebei-config-")
os.environ["SUKEBEI_DOWNLOAD_DIR"] = tempfile.mkdtemp(prefix="sukebei-downloads-")
os.environ["SUKEBEI_LIBRARY_DIR"] = tempfile.mkdtemp(prefix="sukebei-library-")

from fastapi.testclient import TestClient

from app.main import app, settings, _serialize_files
from app.models import DownloadTask
from app.qbittorrent import QBittorrentClient, QBittorrentError
from app import main


def check_qb_configuration(client, csrf, monkeypatch):
    calls = []

    async def version(candidate):
        calls.append((candidate.settings.qbittorrent_username, candidate.settings.qbittorrent_password.get_secret_value()))
        return {"version": "v5.2.3"}

    monkeypatch.setattr(QBittorrentClient, "version", version)
    endpoint = "/api/settings/qbittorrent"
    headers = {"X-CSRF-Token": csrf}
    payload = {"url": "http://qb.test:8080", "username": "qb-admin", "password": "qb-private-password"}
    assert client.put(endpoint, json=payload).status_code == 403
    assert client.post(endpoint + "/test", json=payload).status_code == 403
    assert client.put(endpoint, headers=headers, json={"url": payload["url"], "username": "qb-admin"}).status_code == 422
    assert client.put(endpoint, headers=headers, json={**payload, "url": "file:///config/app.db"}).status_code == 422
    assert client.get(endpoint).json()["password_configured"] is False
    tested = client.post(endpoint + "/test", headers=headers, json=payload)
    assert tested.json() == {"ok": True, "version": "v5.2.3"}
    assert client.get(endpoint).json()["password_configured"] is False  # Test must not save.
    saved = client.put(endpoint, headers=headers, json=payload)
    assert saved.status_code == 200
    assert saved.json()["password_configured"] is True
    assert "qb-private-password" not in saved.text
    assert "password" not in saved.json()
    assert main.manager.downloader is main.downloader
    assert main.downloader.settings.qbittorrent_username == "qb-admin"
    assert client.put(endpoint, headers=headers, json={**payload, "password": ""}).status_code == 200
    assert calls[-1] == ("qb-admin", "qb-private-password")
    assert client.put(endpoint, headers=headers, json={"url": payload["url"], "username": "qb-admin"}).status_code == 200
    assert calls[-1] == ("qb-admin", "qb-private-password")

    async def unavailable(candidate):
        raise QBittorrentError("upstream echoes qb-private-password")

    monkeypatch.setattr(QBittorrentClient, "version", unavailable)
    failed = client.put(endpoint, headers=headers, json={**payload, "username": "wrong"})
    assert failed.status_code == 502
    assert "qb-private-password" not in failed.text
    assert client.get(endpoint).json()["username"] == "qb-admin"  # Failed save keeps working config.
    assert main.downloader.settings.qbittorrent_username == "qb-admin"
    monkeypatch.setattr(QBittorrentClient, "version", version)


def test_single_file_list_after_downloader_record_removed():
    path = settings.download_dir / "single-video.mp4"
    path.write_bytes(b"payload")
    try:
        files = _serialize_files(DownloadTask(staging_dir=str(path)))
        assert files == [{"path": path.name, "length": 7, "completed_length": 7, "selected": True}]
    finally:
        path.unlink()


def test_file_list_rejects_paths_outside_downloads(tmp_path):
    path = tmp_path / "private.txt"
    path.write_text("private")
    assert _serialize_files(DownloadTask(staging_dir=str(path))) == []


def test_file_list_is_bounded():
    with tempfile.TemporaryDirectory(dir=settings.download_dir) as directory:
        root = Path(directory)
        for index in range(505):
            (root / f"{index}.mp4").touch()
        assert len(_serialize_files(DownloadTask(staging_dir=str(root)))) == 500


def test_setup_login_csrf_and_removed_proxy(monkeypatch):
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/does-not-exist").status_code == 404
        assert client.get("/api/settings/qbittorrent").status_code == 401
        token = Path(settings.setup_token_file).read_text(encoding="utf-8")
        setup = client.post("/api/setup", json={"setup_token": token, "username": "admin", "password": "correct horse battery"})
        assert setup.status_code == 200
        csrf = setup.json()["csrf_token"]
        assert client.get("/api/auth/me").json()["username"] == "admin"
        assert client.post("/api/downloads", json={"magnet_uri": "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "title": "x"}).status_code == 403
        assert client.get("/api/settings/proxy").status_code == 404
        assert client.put("/api/settings/proxy", headers={"X-CSRF-Token": csrf}, json={}).status_code in {404, 405}
        filters = client.get("/api/settings/filters")
        assert filters.status_code == 200
        assert filters.json() == {"filename_regex": None, "min_size_bytes": None, "max_size_bytes": None}
        saved_filters = client.put(
            "/api/settings/filters",
            headers={"X-CSRF-Token": csrf},
            json={"filename_regex": r"\.(mkv|mp4)$", "min_size_bytes": 1024, "max_size_bytes": 1048576},
        )
        assert saved_filters.status_code == 200
        assert saved_filters.json()["min_size_bytes"] == 1024
        assert client.put(
            "/api/settings/filters",
            headers={"X-CSRF-Token": csrf},
            json={"filename_regex": "["},
        ).status_code == 422
        check_qb_configuration(client, csrf, monkeypatch)

    # Exercise real lifespan loading rather than just checking the DB row.
    with TestClient(app) as restarted:
        login = restarted.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})
        assert login.status_code == 200
        loaded = restarted.get("/api/settings/qbittorrent")
        assert loaded.json()["url"] == "http://qb.test:8080"
        assert loaded.json()["username"] == "qb-admin"
        assert loaded.json()["password_configured"] is True
        assert "qb-private-password" not in loaded.text
        assert main.downloader.settings.qbittorrent_password.get_secret_value() == "qb-private-password"
