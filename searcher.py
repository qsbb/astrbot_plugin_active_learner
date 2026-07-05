"""网页搜索器。

策略：
1. 优先使用 AstrBot 配置的 web_search（Tavily / BoCha / Brave / 百度）
   —— 但本插件无法直接调用，这部分由 LLM 的 web_search 工具完成
2. 兜底：DuckDuckGo HTML 解析（无需 API Key）

本搜索器只负责"直接抓取"的搜索能力，用于主动学习时获取原始搜索结果片段。
AstrBot 内置的 web_search 仍由 LLM 在工具调用时使用，与本搜索器并行可用。
"""

from __future__ import annotations

import re
from typing import Optional

import aiohttp

try:
    from astrbot.api import logger
except ImportError:  # 允许脱离 AstrBot 独立导入（语法测试用）
    import logging
    logger = logging.getLogger("active_learner")


DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class WebSearcher:
    """轻量级网页搜索，仅作 DuckDuckGo 兜底。

    AstrBot 自带的 web_search 通过 LLM 函数调用机制工作，
    本类不直接调用它，避免与 AstrBot 内部状态冲突。
    """

    def __init__(self, ddg_fallback: bool = True):
        self._ddg_fallback = ddg_fallback

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        """搜索入口。返回 [{"title","snippet","url"}, ...]"""
        if not query or not query.strip():
            return []
        results: list[dict] = []

        if self._ddg_fallback:
            results = await self._search_duckduckgo(query, max_results)
            # 中文查询补充
            has_cn = any("\u4e00" <= c <= "\u9fff" for c in query)
            if has_cn and len(results) < max_results:
                cn_query = f"{query} 详细解释"
                cn_results = await self._search_duckduckgo(cn_query, max_results - len(results))
                results.extend(cn_results)

        # 去重
        seen: set[str] = set()
        unique: list[dict] = []
        for r in results:
            key = (r.get("title") or "")[:30]
            if key and key not in seen:
                seen.add(key)
                unique.append(r)
        return unique[:max_results]

    @staticmethod
    async def _search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
        """DuckDuckGo HTML 解析（无需 API Key）。"""
        results: list[dict] = []
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                data = {"q": query, "b": ""}
                headers = {"User-Agent": USER_AGENT}
                async with session.post(
                    DUCKDUCKGO_URL, data=data, headers=headers,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"DuckDuckGo 返回状态码 {resp.status}")
                        return []
                    html = await resp.text(encoding="utf-8", errors="ignore")

                # 解析结果
                pattern = (
                    r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
                    r'.*?class="result__snippet"[^>]*>(.*?)</(?:td|span)'
                )
                matches = re.findall(pattern, html, re.DOTALL)
                for url, title, snippet in matches[:max_results]:
                    title = re.sub(r"<.*?>", "", title).strip()
                    snippet = re.sub(r"<.*?>", "", snippet).strip()
                    # DuckDuckGo 的链接是重定向
                    if url.startswith("//"):
                        url = "https:" + url
                    if title and snippet:
                        results.append({"title": title, "url": url, "snippet": snippet})
        except Exception as e:
            logger.warning(f"DuckDuckGo 搜索失败: {e}")
        return results

    async def fetch_url(self, url: str, max_chars: int = 2000) -> str:
        """抓取指定 URL 的纯文本（用于深度学习时获取网页正文）。"""
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = {"User-Agent": USER_AGENT}
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return ""
                    html = await resp.text(encoding="utf-8", errors="ignore")
                    # 简单 HTML → 纯文本
                    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()
                    return text[:max_chars]
        except Exception as e:
            logger.debug(f"抓取 URL 失败 {url}: {e}")
            return ""
