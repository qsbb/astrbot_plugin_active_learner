"""LLM 工具：让大模型在对话中按需调用本插件的能力。

工具列表：
- search_and_learn:  搜索网络 → LLM 总结 → 存入记忆库
- recall_memory:     从记忆库检索已有知识
- verify_knowledge:  验证某条记忆的准确性（多源 + 自辩论 + 版本化）
- search_bilibili:   搜索 B 站视频（可选，需 bilibili-api-python）
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

try:
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except ImportError:  # 允许脱离 AstrBot 做语法检查
    FunctionTool = object  # type: ignore
    ToolExecResult = str  # type: ignore
    ContextWrapper = object  # type: ignore
    AstrAgentContext = object  # type: ignore

from .plugin_logger import logger

from .models import Scope


def _get_event(context):
    """从 ContextWrapper 取 event 的兜底 helper。"""
    try:
        return context.context.event
    except Exception:
        pass
    try:
        return context.event
    except Exception:
        return None


def _resolve_scope(context) -> Optional[Scope]:
    """从工具调用上下文推算 scope。"""
    event = _get_event(context)
    if event is None:
        return None
    return Scope.from_event(event)


# ============================================================
# Tool 1: search_and_learn
# ============================================================

@pydantic_dataclass
class SearchAndLearnTool(FunctionTool):  # type: ignore[misc]
    """搜索并学习新知识。当用户问了你不确定的问题时使用。"""

    name: str = "search_and_learn"
    description: str = (
        "【必用工具】搜索网络学习新知识并存入长期记忆。"
        "只要遇到以下情况，必须立即调用：\n"
        "1. 用户提到你不熟悉的人名、术语、概念、事件；\n"
        "2. 用户告诉你一个新信息（科普/教新知识）；\n"
        "3. 你不太确定某个回答是否正确；\n"
        "4. 记忆库未命中（你感觉「好像知道但不确定」）。\n"
        "调用后会：搜索多来源 → 总结 → 存入记忆库供日后使用。\n"
        "无需预告「让我查查看」等话术，直接调用即可。\n"
        "如果确认是自己已掌握的知识则无需调用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "要学习的主题名（如'量子纠缠'、'Python GIL'）",
                },
                "query": {
                    "type": "string",
                    "description": "搜索关键词，用于在网络上检索。可与主题不同，便于更精准搜索。",
                },
            },
            "required": ["topic", "query"],
        }
    )

    async def call(self, context, **kwargs) -> "ToolExecResult":  # type: ignore[override]
        topic = kwargs.get("topic", "").strip()
        query = kwargs.get("query", topic).strip()
        if not topic:
            return ("请提供要学习的主题")

        plugin = self._plugin
        scope = _resolve_scope(context)
        if scope is None:
            return ("无法识别会话作用域，学习失败")

        # 1. 多源搜索
        logger.info(f"搜索「{query}」(主题: {topic}, scope: {scope})")
        search_results = await plugin.searcher.search(query, max_results=6)

        # B 站补充（可选）
        if plugin.bili_source and plugin.bili_source.is_available():
            try:
                bili_results = await plugin.bili_source.search(topic, limit=3)
                search_results.extend(bili_results)
            except Exception as e:
                logger.debug(f"B 站搜索失败: {e}")

        if not search_results:
            logger.info(f"搜索「{query}」无结果")
            return (f"搜索「{query}」未找到结果，无法学习")

        # 2. 整理搜索结果
        snippets = []
        sources = []
        for r in search_results:
            snippets.append(f"标题: {r.get('title','')}\n摘要: {r.get('snippet','')}")
            sources.append(f"{r.get('title','')} ({r.get('url','')})")
        search_text = "\n---\n".join(snippets)

        # 3. LLM 精炼（2 步：抽取事实 + 结构化为知识卡）
        event = _get_event(context)
        provider_id = await plugin.llm_service.resolve_provider_id(event=event)

        refine_result = await plugin.refiner.refine_search_results(
            topic=topic, search_text=search_text, sources=sources, provider_id=provider_id,
        )
        summary = refine_result.summary

        # 4. 关键词：refined 时用精炼结果，否则降级用规则提取
        if refine_result.refined and refine_result.keywords:
            keywords = refine_result.keywords
        else:
            keywords = _extract_keywords(topic, search_results)

        # 5. 置信度：refined 时用 LLM 自评，否则用规则
        if refine_result.refined:
            confidence = refine_result.confidence
        else:
            confidence = min(0.7, 0.3 + len(search_results) * 0.07)
            if len(search_results) >= 3:
                confidence = min(0.85, confidence + 0.1)

        # 6. 存入记忆
        source_tag = f"网络搜索 ({len(sources)}个来源)"
        if refine_result.refined:
            source_tag += "+精炼"
        _evt = _get_event(context)
        _umo = getattr(_evt, "unified_msg_origin", "") if _evt else ""
        try:
            entry = await asyncio.to_thread(
                plugin.store.add_or_update,
                scope, topic, summary or search_text[:500],
                keywords=keywords,
                source=source_tag,
                sources_detail=sources,
                confidence=confidence,
                origin=f"conversation:{_umo}" if _umo else "conversation",
            )
        except Exception as e:
            logger.error(f"❌ 搜索学习存储失败「{topic}」: {e}", exc_info=True)
            return (f"搜索学习「{topic}」失败（存储错误），请稍后重试。")

        logger.info(f"✅ 已学习「{topic}」(id: {entry.id}, 置信度{confidence:.0%}, 来源{len(sources)}, refined={refine_result.refined}, scope: {scope})")
        plugin._active_learn_was_called = True

        return (
            f"已学习「{topic}」并存入记忆库。\n"
            f"总结: {summary[:200]}\n"
            f"置信度: {confidence:.0%}\n"
            f"来源数: {len(sources)}"
        )


# ============================================================
# Tool 2: recall_memory
# ============================================================

@pydantic_dataclass
class RecallMemoryTool(FunctionTool):  # type: ignore[misc]
    """从记忆库检索已学习的知识。"""

    name: str = "recall_memory"
    description: str = (
        "从记忆库检索已学习的知识。用户问到之前讨论过或学习过的话题时直接调用。"
        "检索到记忆就基于记忆回答，不要说\"让我回忆一下\"等话术；"
        "未检索到可考虑调用 search_and_learn。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要检索的关键词或主题",
                },
            },
            "required": ["query"],
        }
    )

    async def call(self, context, **kwargs) -> "ToolExecResult":  # type: ignore[override]
        query = kwargs.get("query", "").strip()
        if not query:
            return ("请提供要检索的关键词")

        plugin = self._plugin
        scope = _resolve_scope(context)
        if scope is None:
            return ("无法识别会话作用域")

        hits = plugin.store.search(scope, query, top_k=3)
        if not hits:
            logger.info(f"不知道「{query}」(scope: {scope})")
            return (f"记忆库中未找到关于「{query}」的知识。可调用 search_and_learn 学习。")

        logger.info(f"知道「{query}」(命中{len(hits)}条, scope: {scope})")
        parts = []
        for h in hits:
            entry = h.entry
            status = "✅已验证" if entry.verified else f"❓置信度{entry.confidence:.0%}"
            parts.append(
                f"【{entry.topic}】{status}\n"
                f"内容: {entry.content}\n"
                f"来源: {entry.source}"
            )
        return ("\n\n".join(parts))


# ============================================================
# Tool 3: verify_knowledge
# ============================================================

@pydantic_dataclass
class VerifyKnowledgeTool(FunctionTool):  # type: ignore[misc]
    """验证某条记忆的准确性。"""

    name: str = "verify_knowledge"
    description: str = (
        "验证某条知识的准确性。通过多源搜索 + LLM 自辩论 + 交叉验证来核实。"
        "用户质疑某条信息或需要确认准确性时直接调用。"
        "验证后自动更新记忆库置信度并保留历史版本。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "要验证的主题",
                },
                "claim": {
                    "type": "string",
                    "description": "需要验证的具体说法/内容。如果记忆库已有该主题，可传记忆库中的内容。",
                },
            },
            "required": ["topic", "claim"],
        }
    )

    async def call(self, context, **kwargs) -> "ToolExecResult":  # type: ignore[override]
        topic = kwargs.get("topic", "").strip()
        claim = kwargs.get("claim", "").strip()
        if not topic:
            return ("请提供要验证的主题")

        plugin = self._plugin
        scope = _resolve_scope(context)
        if scope is None:
            return ("无法识别会话作用域")

        # 优先从记忆库找已有条目
        entry = plugin.store.search_by_topic(scope, topic)
        if entry is None:
            # 模糊查找
            hits = plugin.store.search(scope, topic, top_k=1)
            if hits:
                entry = hits[0].entry

        if entry is None:
            return (f"记忆库中未找到关于「{topic}」的记忆，请先调用 search_and_learn 学习。")

        # 标记质疑
        plugin.store.inc_challenge(entry.id)

        # 取 LLM provider
        event = _get_event(context)
        provider_id = await plugin.llm_service.resolve_provider_id(event=event)

        # 执行验证
        logger.info(f"验证「{topic}」(scope: {scope})")
        result = await plugin.verifier.run(entry, provider_id, claim=claim or None)
        logger.info(f"已验证「{topic}」({result.verdict}, 置信度{result.confidence:.0%})")

        return (
            f"验证完成: {topic}\n"
            f"━━━━━━━━━━\n"
            f"{result.to_text()}"
        )


# ============================================================
# Tool 4: search_bilibili
# ============================================================

@pydantic_dataclass
class SearchBilibiliTool(FunctionTool):  # type: ignore[misc]
    """搜索 B 站视频。"""

    name: str = "search_bilibili"
    description: str = (
        "搜索 Bilibili 视频。返回标题、UP主、简介、链接。"
        "用户想看视频、找教程、了解某主题的视频内容时直接调用。"
        "不可用时自动回退到普通网页搜索 site:bilibili.com。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "limit": {
                    "type": "number",
                    "description": "返回结果数量，默认 5",
                },
            },
            "required": ["keyword"],
        }
    )

    async def call(self, context, **kwargs) -> "ToolExecResult":  # type: ignore[override]
        keyword = kwargs.get("keyword", "").strip()
        if not keyword:
            return ("请提供搜索关键词")

        try:
            limit = int(kwargs.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(10, limit))

        plugin = self._plugin

        logger.info(f"搜索 B站: {keyword} (limit={limit})")

        # 优先用 bilibili-api-python
        if plugin.bili_source and plugin.bili_source.is_available():
            results = await plugin.bili_source.search(keyword, limit=limit)
            if results:
                return (_format_bili_results(results))

            # 回退
            results = await plugin.bili_source.search_fallback(keyword, plugin.searcher, limit=limit)
            return (_format_bili_results(results, fallback=True))

        # 不可用 → web 搜索 site:bilibili.com
        query = f"{keyword} site:bilibili.com"
        results = await plugin.searcher.search(query, max_results=limit)
        return (_format_bili_results(results, fallback=True))


# ============================================================
# 辅助函数
# ============================================================

def _extract_keywords(topic: str, search_results: list[dict]) -> list[str]:
    """从搜索结果中提取高频关键词。"""
    keywords = [topic]
    all_text = " ".join(
        r.get("title", "") + " " + r.get("snippet", "")
        for r in search_results
    )
    # 中文词组 + 英文单词
    words = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", all_text)
    freq: dict[str, int] = {}
    for w in words:
        w_lower = w.lower()
        if w_lower in keywords:
            continue
        freq[w_lower] = freq.get(w_lower, 0) + 1
    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    keywords.extend([w for w, _ in sorted_words[:5]])
    return keywords[:8]


def _format_bili_results(results: list[dict], fallback: bool = False) -> str:
    """格式化 B 站搜索结果为文本。"""
    if not results:
        return "未找到相关视频"
    tag = "（网页搜索回退）" if fallback else ""
    lines = [f"B 站搜索结果{tag}:"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        snippet = r.get("snippet", "")
        url = r.get("url", "")
        author = r.get("author", "")
        author_str = f" | UP: {author}" if author else ""
        lines.append(f"{i}. {title}{author_str}")
        if snippet:
            lines.append(f"   {snippet[:80]}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)


# ============================================================
# Tool 5: save_memory
# ============================================================

@pydantic_dataclass
class SaveMemoryTool(FunctionTool):  # type: ignore[misc]
    """将对话中值得记忆的知识点标记给插件，插件异步精炼后存入记忆库。

    LLM 只需判断"这里有一个值得记的知识点"并传入对话片段，
    不需要自己组织内容——插件会调用 LLM 异步分析、精炼、存储。
    """

    name: str = "save_memory"
    description: str = (
        "标记对话中值得记忆的知识点。你只需传入主题和相关对话片段，"
        "插件会异步调用 LLM 精炼后存入记忆库。"
        "仅标记通用知识（概念、原理、事实、方法论），"
        "不要标记个人信息、偏好、关系、日程。"
        "当对话中出现了值得日后检索的知识时调用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "知识主题（如'Python GIL'、'量子纠缠原理'）",
                },
                "snippet": {
                    "type": "string",
                    "description": "对话中与该知识点相关的片段。不需要自己组织，直接传对话原文即可，插件会精炼。",
                },
            },
            "required": ["topic", "snippet"],
        }
    )

    async def call(self, context, **kwargs) -> "ToolExecResult":  # type: ignore[override]
        topic = kwargs.get("topic", "").strip()
        snippet = kwargs.get("snippet", "").strip()
        if not topic or not snippet:
            return ("topic 和 snippet 不能为空")

        plugin = self._plugin
        scope = _resolve_scope(context)
        if scope is None:
            return ("无法识别会话作用域")

        # 解析 provider_id + 会话标识
        event = _get_event(context)
        provider_id = await plugin.llm_service.resolve_provider_id(event=event)
        umo = getattr(event, "unified_msg_origin", "") if event else ""

        # 立即返回，异步精炼 + 存储
        asyncio.create_task(
            self._async_refine_and_save(plugin, scope, topic, snippet, provider_id, umo)
        )
        return (f"已标记「{topic}」，正在后台分析并存储。")

    async def _async_refine_and_save(self, plugin, scope, topic, snippet, provider_id, umo=""):
        """异步：LLM 精炼对话片段 → 存入记忆库 → 日志确认。"""
        try:
            logger.info(f"save_memory 开始精炼「{topic}」(scope: {scope}, provider: {'有' if provider_id else '无'})")
            # 1. LLM 精炼
            result = await plugin.refiner.refine_snippet(topic, snippet, provider_id)
            # 2. 存入记忆库
            entry = await asyncio.to_thread(
                plugin.store.add_or_update,
                scope, topic, result.summary,
                keywords=result.keywords,
                source="对话推理" + ("（精炼）" if result.refined else "（原始）"),
                confidence=result.confidence,
                origin=f"conversation:{umo}" if umo else "conversation",
            )
            # 3. 失效向量缓存
            if plugin.embedder is not None:
                plugin.embedder.invalidate_matrix_cache()
            logger.info(
                f"✅ save_memory 已存储「{topic}」"
                f"(id: {entry.id}, 精炼={'是' if result.refined else '否'}, "
                f"置信度={result.confidence:.0%})"
            )
        except Exception as e:
            logger.warning(f"❌ save_memory 异步存储失败「{topic}」: {e}")


def create_tools(plugin) -> list:
    """根据配置创建工具列表。"""
    tool_classes = [
        SearchAndLearnTool,
        RecallMemoryTool,
        VerifyKnowledgeTool,
        SaveMemoryTool,
    ]
    # B 站工具按配置启用
    enable_bili = bool(plugin.config.get("enable_bilibili", False))
    if enable_bili:
        tool_classes.append(SearchBilibiliTool)

    # pydantic v2 dataclass 会覆盖自定义 __init__，所以 plugin 引用
    # 通过 object.__setattr__ 在实例化后注入，绕过字段校验。
    tools = []
    for cls in tool_classes:
        tool = cls()
        object.__setattr__(tool, "_plugin", plugin)
        tools.append(tool)
    return tools
