import asyncio
import re
from datetime import datetime
from time import monotonic
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .schemas import SearchResponse, SearchResult


CATEGORY_OPTIONS = {
    "0_0": "全部分类",
    "1_0": "动漫",
    "2_0": "音频",
    "3_0": "文学",
    "4_0": "真人",
    "5_0": "图片",
    "6_0": "软件",
    "7_0": "其他",
}
SORT_OPTIONS = {"": "默认", "seeders": "做种数", "leechers": "下载数", "downloads": "完成数", "size": "大小", "date": "日期"}


class IndexerError(RuntimeError):
    pass


def parse_size(text: str) -> tuple[str, int | None]:
    cleaned = " ".join(text.split())
    match = re.search(r"(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB|PiB)", cleaned, re.I)
    if not match:
        return cleaned, None
    value = float(match.group(1))
    unit = match.group(2).lower()
    multipliers = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "pib": 1024**5}
    return cleaned, int(value * multipliers[unit])


def parse_datetime(text: str) -> datetime | None:
    value = " ".join(text.split()).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def int_text(text: str) -> int:
    match = re.search(r"\d[\d,]*", text or "")
    return int(match.group(0).replace(",", "")) if match else 0


def btih_from_magnet(magnet: str) -> str | None:
    parsed = urlparse(magnet)
    if parsed.scheme.lower() != "magnet":
        return None
    values = parse_qs(parsed.query).get("xt", [])
    for value in values:
        match = re.fullmatch(r"urn:btih:([A-Za-z0-9]{32}|[A-Fa-f0-9]{40})", value, re.I)
        if match:
            return match.group(1).lower()
    return None


class SukebeiAdapter:
    def __init__(self, base_url: str, timeout: float = 20, proxy: str | None = None, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.proxy = proxy
        self._client = client

    async def fetch(self, query: str, category: str = "0_0", page: int = 1, sort: str = "", order: str = "desc") -> SearchResponse:
        if not query.strip():
            return SearchResponse(items=[], page=1, has_next=False)
        if category not in CATEGORY_OPTIONS:
            raise IndexerError("不支持的分类")
        if sort not in SORT_OPTIONS:
            raise IndexerError("不支持的排序字段")
        if order not in {"asc", "desc"}:
            raise IndexerError("不支持的排序方向")
        page = max(1, min(page, 10000))
        params = {"f": "0", "c": category, "q": query.strip(), "p": str(page)}
        if sort:
            params.update({"s": sort, "o": order})
        url = self.base_url + "?" + urlencode(params)
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, proxy=self.proxy, headers={"User-Agent": "SukebeiManager/1.0"})
        response = None
        succeeded = False
        last_error: Exception | None = None
        try:
            for attempt in range(2):
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    succeeded = True
                    break
                except (httpx.HTTPError, TimeoutError) as exc:
                    last_error = exc
                    if attempt == 0:
                        await asyncio.sleep(0.25)
            if not succeeded or response is None:
                raise IndexerError(f"搜索站点请求失败：{last_error}") from last_error
        finally:
            if close_client:
                await client.aclose()
        return self.parse(response.text, response.url, page)

    def parse(self, html: str, response_url: str | httpx.URL | None = None, page: int = 1) -> SearchResponse:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.torrent-list tbody tr")
        if not rows:
            rows = soup.select("tr[data-category], tr")
        items: list[SearchResult] = []
        for row in rows:
            magnet_anchor = row.select_one('a[href^="magnet:"]')
            title_anchor = row.select_one('a[href*="/view/"]') or row.select_one("a.torrent-name")
            if not magnet_anchor or not title_anchor:
                continue
            magnet = magnet_anchor.get("href", "")
            result_id = btih_from_magnet(magnet)
            if not result_id:
                continue
            cells = row.select("td")
            texts = [" ".join(cell.get_text(" ", strip=True).split()) for cell in cells]
            # Nyaa's table is category, name, size, date, seeders, leechers, completed.
            # Keep a fallback scan because minor themes may add/remove a column.
            size_candidate = texts[2] if len(texts) > 2 else ""
            size_text, size_bytes = parse_size(size_candidate)
            if size_bytes is None:
                size_text, size_bytes = next(
                    ((text, parsed) for text in texts for parsed in [parse_size(text)[1]] if parsed is not None),
                    ("未知", None),
                )
            date_value = parse_datetime(texts[3]) if len(texts) > 3 else None
            if date_value is None:
                date_value = next((parsed for text in texts if (parsed := parse_datetime(text))), None)
            if len(texts) >= 7:
                tail = [int_text(texts[-3]), int_text(texts[-2]), int_text(texts[-1])]
            else:
                numbers = [int_text(text) for text in texts]
                tail = numbers[-3:] if len(numbers) >= 3 else [0, 0, 0]
            details = title_anchor.get("href")
            details_url = urljoin(str(response_url or self.base_url), details) if details else None
            category = row.get("data-category")
            if not category and cells:
                category_anchor = cells[0].select_one('a[href*="c="]')
                if category_anchor:
                    category_query = parse_qs(urlparse(category_anchor.get("href", "")).query).get("c", [""])[0]
                    category = CATEGORY_OPTIONS.get(category_query) or category_query or None
                    category = category or category_anchor.get("title")
                if not category:
                    icon = cells[0].select_one("img[alt], [title]")
                    category = (icon.get("alt") or icon.get("title")) if icon else None
            category = category or (texts[0] if texts else "未分类")
            items.append(
                SearchResult(
                    result_id=result_id,
                    title=" ".join(title_anchor.get_text(" ", strip=True).split()),
                    category=category,
                    size_text=size_text,
                    size_bytes=size_bytes,
                    published_at=date_value,
                    seeders=tail[0],
                    leechers=tail[1],
                    completed=tail[2],
                    magnet_uri=magnet,
                    details_url=details_url,
                )
            )
        if rows and not items and any(row.select_one('a[href^="magnet:"]') for row in rows):
            raise IndexerError("搜索结果页面结构发生变化，无法安全解析磁力链接")
        has_next = bool(soup.select_one('a[rel="next"]')) or any("下一页" in link.get_text(" ", strip=True) for link in soup.select("a")) or len(items) >= 75
        return SearchResponse(items=items, page=page, has_next=has_next)


class SearchService:
    def __init__(self, adapter_factory, ttl: int = 30):
        self.adapter_factory = adapter_factory
        self.ttl = ttl
        self.cache: dict[tuple, tuple[float, SearchResponse]] = {}
        self.lock = asyncio.Lock()

    async def search(self, query: str, category: str, page: int, sort: str, order: str, proxy: str | None = None) -> SearchResponse:
        key = (query.strip(), category, page, sort, order, proxy)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and monotonic() - cached[0] < self.ttl:
                return cached[1]
        result = await self.adapter_factory(proxy).fetch(query, category, page, sort, order)
        if self.ttl:
            async with self.lock:
                self.cache[key] = (monotonic(), result)
        return result

