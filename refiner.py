"""知识精炼器：把搜索结果或原始导入内容蒸馏成结构化记忆。

2 种精炼入口：
- refine_search_results: 搜索结果 → 2 步精炼（抽取关键事实 + 结构化为知识卡）
- refine_import: 原始文本 → 1 步精炼（蒸馏为摘要 + 关键词 + 置信度）

无 provider 时降级返回原始内容，refined=False，由调用方决定是否接受。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .plugin_logger import logger


@dataclass
class RefineResult:
    """精炼结果。refined=False 表示降级（无 provider 或 LLM 失败）。"""

    summary: str
    keywords: list[str] = field(default_factory=list)
    confidence: float = 0.5
    reasoning: str = ""
    refined: bool = True


class KnowledgeRefiner:
    """依赖 plugin 持有的 context（用于 llm_generate）。"""

    def __init__(self, plugin):
        self._plugin = plugin

    # ---------- 公开入口 ----------

    async def refine_search_results(
        self,
        topic: str,
        search_text: str,
        sources: list[str],
        provider_id: str,
    ) -> RefineResult:
        """2 步精炼搜索结果。

        Step 1: 抽取关键事实（带来源编号引用）
        Step 2: 结构化为知识卡（摘要 + 关键词 + 自评置信度）
        """
        if not provider_id:
            return RefineResult(
                summary=search_text[:500] if search_text else "",
                keywords=[topic] if topic else [],
                confidence=0.5,
                reasoning="未配置 LLM provider，跳过精炼",
                refined=False,
            )

        # Step 1: 抽取关键事实
        facts = await self._extract_facts(topic, search_text, provider_id)
        if not facts:
            # 抽取失败，降级用 search_text 直接结构化
            facts = search_text[:1000]

        # Step 2: 结构化
        result = await self._structure_knowledge(
            topic, facts, len(sources), provider_id
        )
        if result is None:
            return RefineResult(
                summary=search_text[:500] if search_text else "",
                keywords=[topic] if topic else [],
                confidence=0.5,
                reasoning="LLM 结构化失败，降级返回原始内容",
                refined=False,
            )
        return result

    async def refine_import(
        self,
        topic: str,
        raw_content: str,
        provider_id: str,
    ) -> RefineResult:
        """单步精炼原始导入内容。"""
        if not provider_id:
            return RefineResult(
                summary=raw_content,
                keywords=[topic] if topic else [],
                confidence=0.5,
                reasoning="未配置 LLM provider，跳过精炼",
                refined=False,
            )

        prompt = (
            f"请把以下内容蒸馏为简洁、准确的知识卡。\n"
            f"主题：{topic}\n\n"
            f"原始内容：\n{raw_content[:3000]}\n\n"
            f"要求：\n"
            f"1. 提取核心事实，剔除冗余和主观判断\n"
            f"2. 摘要 ≤200 字，中文表达\n"
            f"3. 关键词 3-5 个\n"
            f"4. 自评置信度 0-100（信息完整度+可信度）\n"
            f"5. 严格按以下格式输出（每行一个字段）：\n"
            f"SUMMARY: <摘要>\n"
            f"KEYWORDS: <关键词1>, <关键词2>, ...\n"
            f"CONFIDENCE: <0-100>\n"
            f"REASON: <简述依据，≤50字>\n"
        )

        reply = await self._safe_generate(provider_id, prompt)
        return self._parse_result(reply, fallback_summary=raw_content, topic=topic)

    async def refine_snippet(
        self,
        topic: str,
        snippet: str,
        provider_id: str,
    ) -> RefineResult:
        """从对话片段中提取并精炼知识。

        LLM 在对话中标记了值得记忆的知识点，本方法把对话片段蒸馏为结构化知识卡。
        无 provider 时降级存储原始片段。
        """
        if not provider_id:
            return RefineResult(
                summary=snippet,
                keywords=[topic] if topic else [],
                confidence=0.4,
                reasoning="未配置 LLM provider，直接存储原始片段",
                refined=False,
            )

        prompt = (
            f"你是知识工程师。用户在对话中提到了「{topic}」，"
            f"请从以下对话片段中提取并精炼知识。\n\n"
            f"对话片段：\n{snippet[:3000]}\n\n"
            f"要求：\n"
            f"1. SUMMARY ≤200 字，提取核心知识点（定义/原理/事实），剔除闲聊和无关内容\n"
            f"2. KEYWORDS 3-5 个，便于检索\n"
            f"3. CONFIDENCE 0-100：信息完整度和可信度\n"
            f"4. REASON ≤50 字\n"
            f"5. 严格按以下格式输出（每行一个字段）：\n"
            f"SUMMARY: <摘要>\n"
            f"KEYWORDS: <关键词1>, <关键词2>, ...\n"
            f"CONFIDENCE: <0-100>\n"
            f"REASON: <依据>\n"
        )

        reply = await self._safe_generate(provider_id, prompt)
        result = self._parse_result(reply, fallback_summary=snippet, topic=topic)
        if result is None:
            return RefineResult(
                summary=snippet,
                keywords=[topic] if topic else [],
                confidence=0.4,
                reasoning="精炼解析失败，降级存储原始片段",
                refined=False,
            )
        return result

    async def refine_import_batch(
        self,
        topics: list[str],
        raw_contents: list[str],
        provider_id: str,
    ) -> list[RefineResult]:
        """批量精炼。每个 chunk 一次 LLM 调用（顺序执行，避免 provider 限流）。

        单 chunk 失败不影响其他 chunk。无 provider 时整体降级。
        """
        if not provider_id:
            return [
                RefineResult(
                    summary=raw,
                    keywords=[topic] if topic else [],
                    confidence=0.5,
                    reasoning="未配置 provider",
                    refined=False,
                )
                for topic, raw in zip(topics, raw_contents)
            ]

        results: list[RefineResult] = []
        for topic, raw in zip(topics, raw_contents):
            try:
                r = await self.refine_import(topic, raw, provider_id)
                results.append(r)
            except Exception as e:
                logger.warning(f"批量精炼 chunk '{topic}' 失败: {e}")
                results.append(RefineResult(
                    summary=raw,
                    keywords=[topic] if topic else [],
                    confidence=0.5,
                    reasoning=f"精炼失败: {e}",
                    refined=False,
                ))
        return results

    # ---------- 内部方法 ----------

    async def _extract_facts(self, topic: str, search_text: str, provider_id: str) -> str:
        """Step 1：抽取关键事实，带来源编号引用。"""
        prompt = (
            f"你是严谨的事实抽取员。从以下搜索结果中，抽取关于「{topic}」的关键事实。\n\n"
            f"要求：\n"
            f"1. 每条事实独立成行，≤50 字\n"
            f"2. 引用具体来源编号（如 [1]、[2]）\n"
            f"3. 不要总结、不要主观判断、不要编造\n"
            f"4. 标注相互矛盾的事实（如有）\n"
            f"5. 最多 10 条事实\n\n"
            f"搜索结果：\n{search_text[:3000]}\n"
        )
        return await self._safe_generate(provider_id, prompt)

    async def _structure_knowledge(
        self,
        topic: str,
        facts: str,
        sources_count: int,
        provider_id: str,
    ) -> Optional[RefineResult]:
        """Step 2：把事实结构化为知识卡。"""
        prompt = (
            f"你是知识工程师。基于以下抽取的事实，结构化关于「{topic}」的知识卡。\n\n"
            f"事实列表：\n{facts}\n\n"
            f"来源数量：{sources_count}\n\n"
            f"要求：\n"
            f"1. SUMMARY ≤200 字，包含核心定义/事实/边界条件\n"
            f"2. KEYWORDS 3-5 个，便于检索\n"
            f"3. CONFIDENCE 0-100：综合来源数量、一致性、信息完整度\n"
            f"4. REASON ≤50 字，简述置信度依据\n"
            f"5. 严格按以下格式输出（每行一个字段）：\n"
            f"SUMMARY: <摘要>\n"
            f"KEYWORDS: <关键词1>, <关键词2>, ...\n"
            f"CONFIDENCE: <0-100>\n"
            f"REASON: <依据>\n"
        )
        reply = await self._safe_generate(provider_id, prompt)
        return self._parse_result(reply, fallback_summary=facts, topic=topic)

    async def _safe_generate(self, provider_id: str, prompt: str) -> str:
        """安全调用 LLM，失败返回空串。复用 LLMService 统一入口。"""
        return await self._plugin.llm_service.generate(
            prompt=prompt, provider_id=provider_id
        )

    def _parse_result(
        self,
        reply: str,
        fallback_summary: str,
        topic: str,
    ) -> Optional[RefineResult]:
        """解析结构化 LLM 响应。解析失败返回 None（让上层降级）。"""
        if not reply or not reply.strip():
            return None

        summary = self._extract_field(reply, r"SUMMARY:\s*(.+?)(?=\n[A-Z]+:|\Z)", fallback_summary)
        keywords_str = self._extract_field(reply, r"KEYWORDS:\s*(.+?)(?=\n[A-Z]+:|\Z)", "")
        confidence_str = self._extract_field(reply, r"CONFIDENCE:\s*(\d+)", "")
        reason = self._extract_field(reply, r"REASON:\s*(.+?)(?=\n[A-Z]+:|\Z)", "")

        # 解析关键词
        keywords = []
        if keywords_str:
            # 按逗号/顿号/空格分隔
            parts = re.split(r"[,，、\s]+", keywords_str.strip())
            keywords = [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2][:8]
        if not keywords and topic:
            keywords = [topic]

        # 解析置信度
        confidence = 0.5
        if confidence_str:
            try:
                score = int(confidence_str)
                confidence = max(0.0, min(1.0, score / 100.0))
            except (ValueError, TypeError):
                pass

        return RefineResult(
            summary=summary.strip() or fallback_summary,
            keywords=keywords,
            confidence=confidence,
            reasoning=reason.strip(),
            refined=True,
        )

    @staticmethod
    def _extract_field(text: str, pattern: str, fallback: str) -> str:
        """从 LLM 响应中正则提取字段。"""
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return fallback
