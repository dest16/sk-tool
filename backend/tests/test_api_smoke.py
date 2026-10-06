import os
import tempfile
from pathlib import Path

os.environ["SUKEBEI_CONFIG_DIR"] = tempfile.mkdtemp(prefix="sukebei-config-")
os.environ["SUKEBEI_DOWNLOAD_DIR"] = tempfile.mkdtemp(prefix="sukebei-downloads-")
os.environ["SUKEBEI_LIBRARY_DIR"] = tempfile.mkdtemp(prefix="sukebei-library-")

from fastapi.testclient import TestClient

from app.main import app, settings, _serialize_files
from app.models import DownloadTask


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


def test_setup_login_csrf_and_removed_proxy():
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/does-not-exist").status_code == 404
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
