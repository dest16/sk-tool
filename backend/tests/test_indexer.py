from pathlib import Path

import httpx
import pytest

from app.indexer import IndexerError, SukebeiAdapter, btih_from_magnet, parse_size
from app.schemas import ProxySettings


def test_parse_fixture(fixture_dir: Path):
    html = (fixture_dir / "results.html").read_text(encoding="utf-8")
    result = SukebeiAdapter("https://sukebei.nyaa.si/").parse(html, "https://sukebei.nyaa.si/?q=x")
    assert len(result.items) == 2
    assert result.items[0].result_id == "0123456789abcdef0123456789abcdef01234567"
    assert result.items[0].size_bytes == int(1.5 * 1024**3)
    assert result.items[0].seeders == 123
    assert result.items[1].published_at is not None


def test_magnet_validation():
    assert btih_from_magnet("magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789")
    assert btih_from_magnet("https://example.com/file") is None
    assert btih_from_magnet("magnet:?xt=urn:sha1:abcdef") is None


def test_size_parser():
    text, value = parse_size(" 2.5 GiB ")
    assert text == "2.5 GiB"
    assert value == int(2.5 * 1024**3)


def test_changed_markup_with_magnet_is_rejected():
    with pytest.raises(IndexerError):
        SukebeiAdapter("https://example.com").parse('<table class="torrent-list"><tr><td><a href="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">x</a></td></tr></table>')


def test_category_can_be_derived_from_icon_link():
    html = '''<table class="torrent-list"><tbody><tr>
      <td><a href="/?c=5_0"><img alt="Pictures"></a></td>
      <td><a href="/view/1">x</a><a href="magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb">m</a></td>
      <td>1 MiB</td><td>2025-01-01</td><td>1</td><td>2</td><td>3</td>
    </tr></tbody></table>'''
    result = SukebeiAdapter("https://example.com").parse(html)
    assert result.items[0].category == "图片"


@pytest.mark.asyncio
async def test_fetch_encodes_allowlisted_query(fixture_dir: Path):
    html = (fixture_dir / "results.html").read_text(encoding="utf-8")

    async def handler(request: httpx.Request):
        assert request.url.path == "/"
        assert request.url.params["q"] == "a b"
        assert request.url.params["p"] == "2"
        assert request.url.params["s"] == "seeders"
        assert request.url.params["o"] == "asc"
        return httpx.Response(200, text=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await SukebeiAdapter("https://example.com/", client=client).fetch("a b", page=2, sort="seeders", order="asc")
    finally:
        await client.aclose()
    assert len(result.items) == 2


def test_proxy_validation_rejects_config_injection():
    with pytest.raises(ValueError):
        ProxySettings(indexer_proxy="http://proxy.test:8080\nall-proxy=bad")

