"""网页搜索器。

本插件不内置搜索引擎，搜索依赖：
- LLM 在工具调用时使用 AstrBot 内置 web_search（Tavily / BoCha / Brave / 百度）
- B 站搜索由 bili_source.py 独立提供

本类仅保留 URL 抓取能力，用于获取网页正文。
"""

from __future__ import annotations

import re

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
    """仅提供 URL 抓取的轻量级网页搜索器。"""

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        """搜索入口。无内置搜索引擎，始终返回空列表。"""
        return []

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
