"""B 站搜索源（可选）。

依赖 bilibili-api-python 库（不写入 requirements.txt，用户按需安装）。
不可用时降级为普通网页搜索 site:bilibili.com。
"""

from __future__ import annotations

from typing import Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("active_learner")


# 尝试导入 bilibili-api-python
_BILI_AVAILABLE = False
_bili_search = None
try:
    from bilibili_api import search as _bili_search_mod  # type: ignore
    _bili_search = _bili_search_mod
    _BILI_AVAILABLE = True
except ImportError:
    _BILI_AVAILABLE = False
except Exception as e:
    logger.debug(f"bilibili-api-python 导入异常: {e}")
    _BILI_AVAILABLE = False


def is_available() -> bool:
    return _BILI_AVAILABLE


class BiliSource:
    """B 站搜索封装。

    可用时：调用 bilibili-api-python 的 search_by_type 搜视频
    不可用时：调用方应回退到 WebSearcher.search(keyword + " site:bilibili.com")
    """

    def __init__(self):
        if not _BILI_AVAILABLE:
            logger.warning(
                "bilibili-api-python 未安装，B 站搜索将回退到普通网页搜索。"
                "如需启用：pip install bilibili-api-python"
            )

    async def search(self, keyword: str, limit: int = 5) -> list[dict]:
        """搜索 B 站视频。返回 [{"title","snippet","url","author"}, ...]"""
        if not _BILI_AVAILABLE or not keyword.strip():
            return []
        try:
            return await self._search_via_bili_api(keyword, limit)
        except Exception as e:
            logger.warning(f"B 站 API 搜索失败: {e}")
            return []

    async def _search_via_bili_api(self, keyword: str, limit: int) -> list[dict]:
        """通过 bilibili-api-python 搜索。"""
        import asyncio
        from bilibili_api.search import search_by_type, SearchObjectType  # type: ignore

        result = await search_by_type(
            keyword=keyword,
            search_type=SearchObjectType.VIDEO,
            page=1,
        )
        items = result.get("result", []) if isinstance(result, dict) else []
        out: list[dict] = []
        for item in items[:limit]:
            title = item.get("title", "")
            # B 站搜索结果的 title 包含 <em class="keyword">...</em> 高亮标签
            import re
            title = re.sub(r"<[^>]+>", "", title)
            description = item.get("description", "")
            author = item.get("author", "")
            bvid = item.get("bvid", "")
            url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            snippet = f"{description}（UP主: {author}）" if description else f"UP主: {author}"
            out.append({
                "title": title,
                "snippet": snippet,
                "url": url,
                "author": author,
            })
        return out

    async def search_fallback(self, keyword: str, web_searcher, limit: int = 5) -> list[dict]:
        """降级方法：通过 WebSearcher 搜 site:bilibili.com。"""
        query = f"{keyword} site:bilibili.com"
        results = await web_searcher.search(query, max_results=limit)
        for r in results:
            r.setdefault("author", "")
        return results
