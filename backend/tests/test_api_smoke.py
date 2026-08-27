import os
import tempfile
from pathlib import Path

os.environ["SUKEBEI_CONFIG_DIR"] = tempfile.mkdtemp(prefix="sukebei-config-")
os.environ["SUKEBEI_DOWNLOAD_DIR"] = tempfile.mkdtemp(prefix="sukebei-downloads-")
os.environ["SUKEBEI_LIBRARY_DIR"] = tempfile.mkdtemp(prefix="sukebei-library-")

from fastapi.testclient import TestClient

from app.main import app, settings


def test_setup_login_csrf_and_redacted_proxy():
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/does-not-exist").status_code == 404
        token = Path(settings.setup_token_file).read_text(encoding="utf-8")
        setup = client.post("/api/setup", json={"setup_token": token, "username": "admin", "password": "correct horse battery"})
        assert setup.status_code == 200
        csrf = setup.json()["csrf_token"]
        assert client.get("/api/auth/me").json()["username"] == "admin"
        assert client.post("/api/downloads", json={"magnet_uri": "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "title": "x"}).status_code == 403
        saved = client.put("/api/settings/proxy", headers={"X-CSRF-Token": csrf}, json={"indexer_proxy": "http://user:secret@example.test:8080", "aria2_proxy": None})
        assert saved.status_code == 200
        proxy = client.get("/api/settings/proxy")
        assert proxy.status_code == 200
        assert proxy.json()["indexer_proxy"] is None
        assert proxy.json()["indexer_proxy_configured"] is True

