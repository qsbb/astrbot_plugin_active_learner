"""用户维护的网页与 MediaWiki 知识来源。"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .plugin_logger import logger
from .runtime import normalize_match_text, topic_terms
from .searcher import USER_AGENT


SOURCE_TYPES = {"page", "mediawiki"}
MAX_URL_SOURCES = 20
MAX_ACTIVE_SOURCES_PER_SEARCH = 5
SOURCE_TIMEOUT_SECONDS = 5.0


def normalize_source_url(value: str) -> str:
    """校验并规范化管理员输入的 HTTP(S) URL。"""
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL 必须是完整的 http:// 或 https:// 地址")
    if parsed.username or parsed.password:
        raise ValueError("URL 不能包含用户名或密码")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def mediawiki_api_url(value: str) -> str:
    """从 Wiki 首页、页面或 api.php 地址推导 MediaWiki API 地址。"""
    normalized = normalize_source_url(value)
    parsed = urlsplit(normalized)
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if lowered.endswith("/api.php"):
        api_path = path
    elif "/wiki/" in lowered:
        prefix = path[: lowered.index("/wiki/")]
        api_path = f"{prefix}/w/api.php"
    elif lowered.endswith("/wiki"):
        api_path = f"{path[:-5]}/w/api.php"
    elif lowered.endswith("/index.php"):
        api_path = f"{path[:-10]}/api.php"
    elif parsed.netloc.endswith(".wikipedia.org"):
        api_path = "/w/api.php"
    elif parsed.path.endswith("/"):
        api_path = f"{path}/api.php"
    else:
        parent = path.rsplit("/", 1)[0]
        api_path = f"{parent}/api.php"
    api_path = re.sub(r"/{2,}", "/", api_path) or "/api.php"
    return urlunsplit((parsed.scheme, parsed.netloc, api_path, "", ""))


class UrlSourceRegistry:
    """持久化 URL 来源，并在问答搜索时按需抓取。"""

    def __init__(self, data_dir: Path, searcher) -> None:
        self._path = Path(data_dir) / "url_knowledge_sources.json"
        self._searcher = searcher
        self._lock = threading.RLock()
        self._sources: list[dict] = []
        self._load()

    @property
    def enabled_count(self) -> int:
        with self._lock:
            return sum(1 for source in self._sources if source.get("enabled", True))

    def list_sources(self) -> list[dict]:
        with self._lock:
            return [dict(source) for source in self._sources]

    def add_source(
        self,
        *,
        name: str,
        url: str,
        source_type: str = "page",
        enabled: bool = True,
    ) -> dict:
        source_name = str(name or "").strip()[:80]
        kind = str(source_type or "page").strip().lower()
        normalized_url = normalize_source_url(url)
        if not source_name:
            raise ValueError("来源名称不能为空")
        if kind not in SOURCE_TYPES:
            raise ValueError("来源类型必须是 page 或 mediawiki")
        with self._lock:
            if len(self._sources) >= MAX_URL_SOURCES:
                raise ValueError(f"最多只能添加 {MAX_URL_SOURCES} 个 URL 来源")
            if any(source["url"] == normalized_url for source in self._sources):
                raise ValueError("该 URL 已存在")
            source = {
                "id": uuid.uuid4().hex[:12],
                "name": source_name,
                "url": normalized_url,
                "type": kind,
                "enabled": bool(enabled),
                "created_at": time.time(),
            }
            self._sources.append(source)
            self._persist_unlocked()
            return dict(source)

    def set_enabled(self, source_id: str, enabled: bool) -> dict:
        with self._lock:
            for source in self._sources:
                if source["id"] == source_id:
                    source["enabled"] = bool(enabled)
                    self._persist_unlocked()
                    return dict(source)
        raise KeyError(source_id)

    def remove_source(self, source_id: str) -> bool:
        with self._lock:
            before = len(self._sources)
            self._sources = [s for s in self._sources if s["id"] != source_id]
            if len(self._sources) == before:
                return False
            self._persist_unlocked()
            return True

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        with self._lock:
            sources = [
                dict(source) for source in self._sources if source.get("enabled", True)
            ][:MAX_ACTIVE_SOURCES_PER_SEARCH]
        if not sources or not str(query or "").strip():
            return []

        async def run(source: dict) -> list[dict]:
            try:
                return await asyncio.wait_for(
                    self._search_one(source, query, max_results),
                    timeout=SOURCE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.info(f"URL 来源超时: {source['name']}")
            except Exception as exc:
                logger.info(f"URL 来源不可用: {source['name']} ({exc})")
            return []

        groups = await asyncio.gather(*(run(source) for source in sources))
        results: list[dict] = []
        seen: set[str] = set()
        for group in groups:
            for item in group:
                key = item.get("url") or f"{item.get('source_id')}:{item.get('title')}"
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
                if len(results) >= max(1, int(max_results)):
                    return results
        return results

    async def _search_one(
        self, source: dict, query: str, max_results: int
    ) -> list[dict]:
        if source["type"] == "mediawiki":
            return await self._search_mediawiki(source, query, max_results)
        return await self._search_page(source, query)

    async def _search_page(self, source: dict, query: str) -> list[dict]:
        text = await self._searcher.fetch_url(source["url"], max_chars=12000)
        if not text:
            return []
        normalized = normalize_match_text(text)
        terms = topic_terms(query)
        matched = [term for term in terms if term in normalized]
        if terms and len(matched) < len(terms):
            return []
        snippet = self._page_snippet(text, matched or terms)
        return [
            {
                "title": source["name"],
                "snippet": snippet,
                "url": source["url"],
                "source_id": source["id"],
                "source_name": source["name"],
            }
        ]

    async def _search_mediawiki(
        self, source: dict, query: str, max_results: int
    ) -> list[dict]:
        api_url = mediawiki_api_url(source["url"])
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": max(1, min(5, int(max_results))),
            "prop": "extracts|info",
            "explaintext": "1",
            "exintro": "1",
            "inprop": "url",
            "redirects": "1",
        }
        timeout = aiohttp.ClientTimeout(total=SOURCE_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                api_url,
                params=params,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                if response.status != 200:
                    return []
                data = await response.json(content_type=None)
        pages = data.get("query", {}).get("pages", []) if isinstance(data, dict) else []
        results = []
        for page in pages if isinstance(pages, list) else []:
            title = str(page.get("title") or "").strip()
            extract = re.sub(r"\s+", " ", str(page.get("extract") or "")).strip()
            if not title or not extract:
                continue
            page_url = page.get("fullurl") or (
                f"{source['url'].rstrip('/')}/{quote(title.replace(' ', '_'))}"
            )
            results.append(
                {
                    "title": title,
                    "snippet": extract[:1000],
                    "url": page_url,
                    "source_id": source["id"],
                    "source_name": source["name"],
                }
            )
        return results

    @staticmethod
    def _page_snippet(text: str, terms: list[str], max_chars: int = 1000) -> str:
        if not text:
            return ""
        lowered = text.lower()
        positions = [lowered.find(term.lower()) for term in terms if term]
        positions = [position for position in positions if position >= 0]
        if not positions:
            return text[:max_chars]
        start = max(0, min(positions) - 200)
        return html.unescape(text[start : start + max_chars]).strip()

    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = data.get("sources", []) if isinstance(data, dict) else []
            loaded = []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    url = normalize_source_url(item.get("url", ""))
                except ValueError:
                    continue
                kind = str(item.get("type", "page")).lower()
                if kind not in SOURCE_TYPES:
                    continue
                loaded.append(
                    {
                        "id": str(item.get("id") or uuid.uuid4().hex[:12]),
                        "name": str(item.get("name") or url)[:80],
                        "url": url,
                        "type": kind,
                        "enabled": bool(item.get("enabled", True)),
                        "created_at": float(item.get("created_at") or time.time()),
                    }
                )
            self._sources = loaded[:MAX_URL_SOURCES]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._sources = []

    def _persist_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"version": 1, "sources": self._sources}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
