from pathlib import Path

import pytest

from app.aria2 import Aria2Client, Aria2Error, is_metadata_file
from app.config import Settings


def test_aria2_config_exposes_p2p_port_and_upnp(tmp_path: Path):
    settings = Settings(
        config_dir=tmp_path / "config",
        download_dir=tmp_path / "downloads",
        library_dir=tmp_path / "library",
        aria2_p2p_port=51413,
    )
    lines = Aria2Client(settings)._config_lines()

    assert "listen-port=51413" in lines
    assert "dht-listen-port=51413" in lines
    assert "enable-dht=true" in lines
    assert "enable-peer-exchange=true" in lines
    assert "enable-upnp=true" in lines
    assert "rpc-listen-all=false" in lines


def test_metadata_file_detection_uses_basename():
    assert is_metadata_file({"path": "/downloads/job/[METADATA]resource.torrent"})
    assert is_metadata_file({"path": r"C:\downloads\job\[metadata]resource.torrent"})
    assert not is_metadata_file({"path": "/downloads/job/resource.mkv"})


async def test_aria2_status_requests_followed_by_field(tmp_path: Path):
    settings = Settings(
        config_dir=tmp_path / "config",
        download_dir=tmp_path / "downloads",
        library_dir=tmp_path / "library",
    )
    client = Aria2Client(settings)
    seen: dict[str, object] = {}

    async def fake_call(method: str, params: list[object]):
        seen["method"] = method
        seen["params"] = params
        return {"status": "complete", "followedBy": ["child-gid"]}

    client.call = fake_call  # type: ignore[method-assign]
    result = await client.status("parent-gid")

    assert result["followedBy"] == ["child-gid"]
    assert seen["method"] == "aria2.tellStatus"
    assert "followedBy" in seen["params"][1]  # type: ignore[index]


def test_aria2_session_file_is_created_before_start(tmp_path: Path):
    settings = Settings(
        config_dir=tmp_path / "config",
        download_dir=tmp_path / "downloads",
        library_dir=tmp_path / "library",
    )
    settings.config_dir.mkdir()
    client = Aria2Client(settings)

    client._prepare_session_file()

    assert settings.aria2_session_file.is_file()
    assert settings.aria2_session_file.read_text(encoding="utf-8") == ""


def test_aria2_session_directory_is_rejected(tmp_path: Path):
    settings = Settings(
        config_dir=tmp_path / "config",
        download_dir=tmp_path / "downloads",
        library_dir=tmp_path / "library",
    )
    settings.config_dir.mkdir()
    settings.aria2_session_file.mkdir()

    with pytest.raises(Aria2Error, match="目录或特殊文件"):
        Aria2Client(settings)._prepare_session_file()

