from pathlib import Path

from app.aria2 import Aria2Client
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

