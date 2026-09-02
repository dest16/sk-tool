import json
from pathlib import Path

from app.config import Settings
from app.transmission import TransmissionClient, is_metadata_file


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        config_dir=tmp_path / "config",
        download_dir=tmp_path / "downloads",
        library_dir=tmp_path / "library",
        aria2_p2p_port=51413,
    )


def test_transmission_settings_enable_fixed_port_and_upnp(tmp_path: Path):
    settings = _settings(tmp_path)
    client = TransmissionClient(settings)

    client._write_settings()
    saved = json.loads(client.settings_file.read_text(encoding="utf-8"))

    assert saved["peer-port"] == 51413
    assert saved["peer-port-random-on-start"] is False
    assert saved["port-forwarding-enabled"] is True
    assert saved["dht-enabled"] is True
    assert saved["pex-enabled"] is True
    assert saved["rpc-bind-address"] == "127.0.0.1"


def test_metadata_file_detection_uses_basename():
    assert is_metadata_file({"path": "/downloads/job/[METADATA]resource.torrent"})
    assert is_metadata_file({"path": r"C:\downloads\job\[metadata]resource.torrent"})
    assert not is_metadata_file({"path": "/downloads/job/resource.mkv"})


async def test_transmission_status_is_normalised_for_manager(tmp_path: Path):
    settings = _settings(tmp_path)
    client = TransmissionClient(settings)
    seen: dict[str, object] = {}

    async def fake_call(method: str, arguments: dict[str, object] | None = None):
        seen["method"] = method
        seen["arguments"] = arguments
        return {
            "torrents": [
                {
                    "id": 7,
                    "hashString": "a" * 40,
                    "status": 4,
                    "totalSize": 100,
                    "percentDone": 0.5,
                    "rateDownload": 20,
                    "error": 0,
                    "errorString": "",
                    "downloadDir": str(tmp_path / "downloads" / "task"),
                    "name": "video.mkv",
                    "files": [{"name": "video.mkv", "length": 100, "bytesCompleted": 50}],
                }
            ]
        }

    client.call = fake_call  # type: ignore[method-assign]
    result = await client.status("a" * 40)

    assert seen["method"] == "torrent-get"
    assert seen["arguments"] == {"ids": ["a" * 40], "fields": [
        "id", "hashString", "status", "totalSize", "percentDone", "rateDownload",
        "error", "errorString", "downloadDir", "name", "files", "metadataPercentComplete",
    ]}
    assert result["status"] == "active"
    assert result["totalLength"] == "100"
    assert result["completedLength"] == "50"
    assert result["downloadSpeed"] == "20"
    assert result["files"][0]["path"].endswith("task/video.mkv")
    assert result["contentPath"].endswith("task/video.mkv")


async def test_transmission_add_magnet_returns_stable_hash(tmp_path: Path):
    client = TransmissionClient(_settings(tmp_path))
    expected = "b" * 40

    async def fake_call(method: str, arguments: dict[str, object] | None = None):
        assert method == "torrent-add"
        assert arguments and arguments["filename"].startswith("magnet:")
        return {"torrent-added": {"id": 3, "hashString": expected}}

    client.call = fake_call  # type: ignore[method-assign]
    assert await client.add_magnet("magnet:?xt=urn:btih:" + expected, tmp_path / "downloads") == expected

