"""LLM 工具：让大模型在对话中按需调用本插件的能力。

工具列表：
- search_and_learn:  搜索网络 → LLM 总结 → 存入记忆库
- recall_memory:     从记忆库检索已有知识
- verify_knowledge:  验证某条记忆的准确性（多源 + 自辩论 + 版本化）
- search_bilibili:   搜索 B 站视频（可选，需 bilibili-api-python）
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

try:
    from astrbot.api import logger
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except ImportError:  # 允许脱离 AstrBot 做语法检查
    logger = None
    FunctionTool = object  # type: ignore
    ToolExecResult = str  # type: ignore
    ContextWrapper = object  # type: ignore
    AstrAgentContext = object  # type: ignore

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
        "搜索网络学习新知识并记忆。当遇到你不确定或不知道的问题时调用："
        "先搜索多个来源，再总结成简明知识，最后存入记忆库供日后检索。"
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
        search_results = await plugin.searcher.search(query, max_results=6)

        # B 站补充（可选）
        if plugin.bili_source and plugin.bili_source.is_available():
            try:
                bili_results = await plugin.bili_source.search(topic, limit=3)
                search_results.extend(bili_results)
            except Exception as e:
                logger.debug(f"B 站搜索失败: {e}")

        if not search_results:
            return (f"搜索「{query}」未找到结果，无法学习")

        # 2. 整理搜索结果
        snippets = []
        sources = []
        for r in search_results:
            snippets.append(f"标题: {r.get('title','')}\n摘要: {r.get('snippet','')}")
            sources.append(f"{r.get('title','')} ({r.get('url','')})")
        search_text = "\n---\n".join(snippets)

        # 3. LLM 总结
        event = _get_event(context)
        provider_id = ""
        if event is not None:
            try:
                provider_id = await plugin.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            except Exception:
                provider_id = ""

        summary = await _llm_summarize(plugin, provider_id, topic, search_text)

        # 4. 提取关键词
        keywords = _extract_keywords(topic, search_results)

        # 5. 置信度
        confidence = min(0.7, 0.3 + len(search_results) * 0.07)
        if len(search_results) >= 3:
            confidence = min(0.85, confidence + 0.1)

        # 6. 存入记忆
        entry = plugin.store.add_or_update(
            scope=scope,
            topic=topic,
            content=summary,
            keywords=keywords,
            source=f"网络搜索 ({len(sources)}个来源)",
            sources_detail=sources,
            confidence=confidence,
        )

        logger.info(f"学习新知识: {topic} (置信度{confidence:.0%})")

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
        "从记忆库检索已学习的知识。当用户问到之前讨论过或学习过的话题时使用。"
        "如果检索到相关记忆，请基于记忆内容回答；如果未检索到，可以考虑调用 search_and_learn。"
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
            return (f"记忆库中未找到关于「{query}」的知识。可调用 search_and_learn 学习。")

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
        "当用户质疑某条信息、或你需要确认准确性时使用。"
        "验证后会自动更新记忆库的置信度并保留历史版本。"
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
        provider_id = ""
        if event is not None:
            try:
                provider_id = await plugin.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            except Exception:
                provider_id = ""

        # 执行验证
        result = await plugin.verifier.run(entry, provider_id, claim=claim or None)

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
        "适用于用户想看视频、找教程、了解某主题的视频内容时。"
        "如果不可用会自动回退到普通网页搜索 site:bilibili.com。"
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

async def _llm_summarize(plugin, provider_id: str, topic: str, search_text: str) -> str:
    """用 LLM 总结搜索结果。"""
    if not provider_id:
        return f"（搜索结果摘要）{search_text[:200]}"

    prompt = (
        f"请根据以下搜索结果，对「{topic}」进行准确、简洁的总结。\n"
        f"要求:\n"
        f"1. 提取关键事实，避免主观判断\n"
        f"2. 标注信息的可信度（高/中/低）\n"
        f"3. 如果搜索结果相互矛盾，请指出分歧\n"
        f"4. 总结控制在 200 字以内\n"
        f"5. 提取 3-5 个关键词放在末尾，格式: [关键词: x, y, z]\n\n"
        f"搜索结果:\n{search_text}"
    )
    try:
        resp = await plugin.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        return getattr(resp, "completion_text", "") or f"（搜索结果摘要）{search_text[:200]}"
    except Exception as e:
        logger.error(f"LLM 总结失败: {e}")
        return f"（搜索结果摘要）{search_text[:200]}"


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


def create_tools(plugin) -> list:
    """根据配置创建工具列表。"""
    tool_classes = [
        SearchAndLearnTool,
        RecallMemoryTool,
        VerifyKnowledgeTool,
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
