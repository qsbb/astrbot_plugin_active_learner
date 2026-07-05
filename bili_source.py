"""B 站搜索源（可选）。

优先调用 astrbot_plugin_bilibili_ai_bot 插件的 search_bilibili_videos 方法，
未安装时降级到 bilibili-api-python 库，再降级到普通网页搜索 site:bilibili.com。

接口：
    BiliSource(context).is_available() -> bool
    await BiliSource(context).search(keyword, limit) -> list[dict]
    await BiliSource(context).search_fallback(keyword, web_searcher, limit) -> list[dict]
"""

from __future__ import annotations

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("active_learner")


# 尝试导入 bilibili-api-python（第二降级路径）
_BILI_AVAILABLE = False
try:
    from bilibili_api import search as _bili_search_mod  # type: ignore
    _BILI_AVAILABLE = True
except ImportError:
    _BILI_AVAILABLE = False
except Exception as e:
    logger.debug(f"bilibili-api-python 导入异常: {e}")
    _BILI_AVAILABLE = False


class BiliSource:
    """B 站搜索封装。

    优先级：
    1. astrbot_plugin_bilibili_ai_bot 插件实例（通过 Context 查找）
    2. bilibili-api-python 库
    3. 降级到 WebSearcher.search(keyword + " site:bilibili.com")
    """

    def __init__(self, context=None):
        self._context = context
        self._bili_plugin = None
        self._checked = False

    def _find_bili_plugin(self):
        """懒查找 astrbot_plugin_bilibili_ai_bot 插件实例。

        在首次调用时遍历 Context 持有的已加载 Star 列表，
        找到类名为 BiliBiliBot 且暴露 search_bilibili_videos 方法的实例。
        找到后缓存，后续直接返回。
        """
        if self._checked:
            return self._bili_plugin
        self._checked = True
        if self._context is None:
            return None
        try:
            stars = self._collect_stars()
            if not stars:
                return None
            for star in stars:
                cls = star.__class__
                module_name = getattr(cls, "__module__", "") or ""
                cls_name = cls.__name__
                if cls_name == "BiliBiliBot" or "bilibili_ai_bot" in module_name:
                    if callable(getattr(star, "search_bilibili_videos", None)):
                        self._bili_plugin = star
                        logger.info("已连接 astrbot_plugin_bilibili_ai_bot，B 站搜索将走该插件")
                        return star
        except Exception as e:
            logger.debug(f"查找 BiliBot 插件失败: {e}")
        return None

    def _collect_stars(self):
        """从 Context 多种可能属性中收集已加载的 Star 实例列表。"""
        ctx = self._context
        # 1. 直接属性
        for attr in ("stars", "_stars", "star_map", "_star_map"):
            val = getattr(ctx, attr, None)
            if val:
                return list(val.values()) if isinstance(val, dict) else list(val)
        # 2. 方法调用
        for method_name in ("get_all_stars", "get_star_insts", "get_all_star_insts"):
            method = getattr(ctx, method_name, None)
            if callable(method):
                try:
                    return list(method())
                except Exception:
                    continue
        # 3. 通过 star_manager
        sm = getattr(ctx, "star_manager", None) or getattr(ctx, "_star_manager", None)
        if sm is not None:
            for attr in ("stars", "_stars", "star_map", "_star_map"):
                val = getattr(sm, attr, None)
                if val:
                    return list(val.values()) if isinstance(val, dict) else list(val)
        return None

    def is_available(self) -> bool:
        """B 站搜索是否可用（BiliBot 插件或 bilibili-api-python 任一可用即可）。"""
        if self._find_bili_plugin() is not None:
            return True
        return _BILI_AVAILABLE

    async def search(self, keyword: str, limit: int = 5) -> list[dict]:
        """搜索 B 站视频。返回 [{"title","snippet","url","author"}, ...]"""
        if not keyword.strip():
            return []
        # 1. 优先用 BiliBot 插件
        plugin = self._find_bili_plugin()
        if plugin is not None:
            try:
                return await self._search_via_bili_bot(plugin, keyword, limit)
            except Exception as e:
                logger.warning(f"BiliBot 插件搜索失败，降级: {e}")
        # 2. 降级到 bilibili-api-python
        if _BILI_AVAILABLE:
            try:
                return await self._search_via_bili_api(keyword, limit)
            except Exception as e:
                logger.warning(f"bilibili-api-python 搜索失败: {e}")
        return []

    async def _search_via_bili_bot(self, plugin, keyword: str, limit: int) -> list[dict]:
        """通过 astrbot_plugin_bilibili_ai_bot 的 search_bilibili_videos 方法搜索。

        该方法返回 [{title, author, bvid, play, duration}, ...]，
        这里转成统一格式 {title, snippet, url, author}。
        """
        results = await plugin.search_bilibili_videos(keyword, ps=limit)
        out: list[dict] = []
        for v in results:
            bvid = v.get("bvid", "")
            url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            author = v.get("author", "")
            play = v.get("play", 0)
            duration = v.get("duration", "")
            parts = []
            if play:
                parts.append(f"播放:{play}")
            if duration:
                parts.append(f"时长:{duration}")
            snippet = f"（UP主: {author}）"
            if parts:
                snippet = "、".join(parts) + snippet
            out.append({
                "title": v.get("title", ""),
                "snippet": snippet,
                "url": url,
                "author": author,
            })
        return out

    async def _search_via_bili_api(self, keyword: str, limit: int) -> list[dict]:
        """通过 bilibili-api-python 搜索（降级路径）。"""
        import re
        from bilibili_api.search import search_by_type, SearchObjectType  # type: ignore

        result = await search_by_type(
            keyword=keyword,
            search_type=SearchObjectType.VIDEO,
            page=1,
        )
        items = result.get("result", []) if isinstance(result, dict) else []
        out: list[dict] = []
        for item in items[:limit]:
            title = re.sub(r"<[^>]+>", "", item.get("title", ""))
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
