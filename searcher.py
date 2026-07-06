"""网页搜索器。

复用 AstrBot 配置的搜索 API（Tavily / BoCha / Brave）进行搜索。
API key 从 AstrBot 的 provider_settings 读取，无需额外配置。
"""

from __future__ import annotations

import re
from typing import Any, Optional

import aiohttp

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("active_learner")


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class WebSearcher:
    """网页搜索器，复用 AstrBot 配置的搜索 API。"""

    def __init__(self, provider: str = "", api_key: str = ""):
        self._provider = provider.lower() if provider else ""
        self._api_key = api_key

    def configure_from_settings(self, provider_settings: dict[str, Any]) -> None:
        """从 AstrBot 的 provider_settings 读取搜索配置。"""
        if not isinstance(provider_settings, dict):
            return
        provider = provider_settings.get("websearch_provider", "") or ""
        self._provider = provider.lower()

        key = ""
        if self._provider == "tavily":
            keys = provider_settings.get("websearch_tavily_key", []) or []
            key = keys[0] if isinstance(keys, list) and keys else str(keys or "")
        elif self._provider == "bocha":
            keys = provider_settings.get("websearch_bocha_key", []) or []
            key = keys[0] if isinstance(keys, list) and keys else str(keys or "")
        elif self._provider == "brave":
            keys = provider_settings.get("websearch_brave_key", []) or []
            key = keys[0] if isinstance(keys, list) and keys else str(keys or "")
        self._api_key = key

    @property
    def is_available(self) -> bool:
        return bool(self._provider and self._api_key)

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        """搜索入口。根据配置的 provider 调用对应 API。"""
        if not self.is_available:
            return []
        try:
            if self._provider == "tavily":
                return await self._search_tavily(query, max_results)
            if self._provider == "bocha":
                return await self._search_bocha(query, max_results)
            if self._provider == "brave":
                return await self._search_brave(query, max_results)
        except Exception as e:
            logger.warning(f"搜索失败 (provider={self._provider}): {e}")
        return []

    async def _search_tavily(self, query: str, max_results: int) -> list[dict]:
        """Tavily 搜索 API。"""
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning(f"Tavily 返回 {resp.status}")
                    return []
                data = await resp.json()
        results = []
        for r in (data.get("results") or [])[:max_results]:
            results.append({
                "title": r.get("title", ""),
                "snippet": r.get("content", "")[:500],
                "url": r.get("url", ""),
            })
        return results

    async def _search_bocha(self, query: str, max_results: int) -> list[dict]:
        """BoCha 搜索 API。"""
        url = "https://api.bochaai.com/v1/web-search"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {"query": query, "count": max_results}
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"BoCha 返回 {resp.status}")
                    return []
                data = await resp.json()
        results = []
        web_pages = (data.get("data") or {}).get("webPages") or []
        for r in web_pages[:max_results]:
            results.append({
                "title": r.get("name", "") or r.get("title", ""),
                "snippet": (r.get("summary", "") or r.get("snippet", "") or "")[:500],
                "url": r.get("url", "") or r.get("link", ""),
            })
        return results

    async def _search_brave(self, query: str, max_results: int) -> list[dict]:
        """Brave 搜索 API。"""
        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {"q": query, "count": max_results}
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Brave 返回 {resp.status}")
                    return []
                data = await resp.json()
        results = []
        web_results = (data.get("web") or {}).get("results") or []
        for r in web_results[:max_results]:
            results.append({
                "title": r.get("title", ""),
                "snippet": (r.get("description", "") or r.get("snippet", ""))[:500],
                "url": r.get("url", ""),
            })
        return results

    async def fetch_url(self, url: str, max_chars: int = 2000) -> str:
        """抓取指定 URL 的纯文本。"""
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = {"User-Agent": USER_AGENT}
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return ""
                    html = await resp.text(encoding="utf-8", errors="ignore")
                    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()
                    return text[:max_chars]
        except Exception as e:
            logger.debug(f"抓取 URL 失败 {url}: {e}")
            return ""
