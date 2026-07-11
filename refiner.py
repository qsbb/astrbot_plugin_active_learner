"""知识精炼器：把搜索结果或原始导入内容蒸馏成结构化记忆。

2 种精炼入口：
- refine_search_results: 搜索结果 → 1 步精炼（直接抽取事实并结构化为知识卡）
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


@dataclass
class MergeDecision:
    """融合判断结果。should_merge=True 表示新知识与已有条目应融合。"""

    should_merge: bool = False
    target_topic: str = ""
    target_id: str = ""
    merge_reason: str = ""
    refined: bool = True  # False 表示 LLM 调用失败，由调用方决定降级行为


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
        """1 步精炼搜索结果：从搜索结果中抽取事实并直接结构化为知识卡。

        合并原先的 2 步（抽取事实 + 结构化）为单次 LLM 调用，
        减少一半延迟且不降低质量。
        """
        if not provider_id:
            return RefineResult(
                summary=search_text[:500] if search_text else "",
                keywords=[topic] if topic else [],
                confidence=0.5,
                reasoning="未配置 LLM provider，跳过精炼",
                refined=False,
            )

        prompt = (
            f"你是知识工程师。从以下搜索结果中，为「{topic}」生成结构化知识卡。\n\n"
            f"要求：\n"
            f"1. 先从搜索结果中抽取关键事实（≤10 条，引用来源编号 [1][2] 等）\n"
            f"2. 基于事实生成摘要（≤200 字，包含核心定义/事实/边界条件）\n"
            f"3. 关键词 3-5 个，便于检索\n"
            f"4. 置信度 0-100（综合来源数量、一致性、信息完整度）\n"
            f"5. 简述置信度依据（≤50 字）\n\n"
            f"来源数量：{len(sources)}\n\n"
            f"搜索结果：\n{search_text[:3000]}\n\n"
            f"严格按以下格式输出（每行一个字段）：\n"
            f"SUMMARY: <摘要>\n"
            f"KEYWORDS: <关键词1>, <关键词2>, ...\n"
            f"CONFIDENCE: <0-100>\n"
            f"REASON: <依据>\n"
        )

        reply = await self._safe_generate(provider_id, prompt)
        result = self._parse_result(reply, fallback_summary=search_text[:500], topic=topic)
        if result is None:
            return RefineResult(
                summary=search_text[:500] if search_text else "",
                keywords=[topic] if topic else [],
                confidence=0.5,
                reasoning="LLM 精炼失败，降级返回原始内容",
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

    # ---------- 融合判断 ----------

    async def check_merge(
        self,
        new_topic: str,
        new_summary: str,
        new_keywords: list[str],
        existing_topic: str,
        existing_summary: str,
        existing_keywords: list[str],
        provider_id: str,
    ) -> MergeDecision:
        """判断新知识是否应融合到已有条目。

        场景：LLM 刚学到"糖猫"，但库里已有"米雪儿"。
        LLM 判断：
          - 如果糖猫 = 米雪儿的别名/属性 → 融合（should_merge=True）
          - 如果糖猫是一个独立新实体 → 不融合（should_merge=False）

        无 provider 时不判断（refined=False），由调用方决定降级行为。
        """
        if not provider_id:
            return MergeDecision(refined=False)

        existing_kws = "、".join(existing_keywords[:5]) if existing_keywords else "无"
        new_kws = "、".join(new_keywords[:5]) if new_keywords else "无"

        prompt = (
            "你是知识库管理员，判断两条知识是否描述同一实体，是否需要融合。\n\n"
            "--- 已有知识 ---\n"
            f"主题：{existing_topic}\n"
            f"内容：{existing_summary[:500]}\n"
            f"关键词：{existing_kws}\n\n"
            "--- 新知识 ---\n"
            f"主题：{new_topic}\n"
            f"内容：{new_summary[:500]}\n"
            f"关键词：{new_kws}\n\n"
            "判断标准：\n"
            "1. 如果新知识是已有知识的别名、属性补充、细节扩展 → 应融合\n"
            "   例如已有「米雪儿」学到「糖猫是米雪儿外号」→ 融合\n"
            "   例如已有「Python」学到「Python 3.13 新特性」→ 融合\n"
            "2. 如果新知识是完全独立的实体 → 不融合\n"
            "   例如已有「米雪儿」学到「量子纠缠」→ 不融合\n\n"
            "严格按以下格式输出（每行一个字段）：\n"
            "DECISION: yes / no\n"
            "TARGET: <融合目标主题（已有知识的主题）>\n"
            f"REASON: <判断理由，≤30字>\n"
        )

        reply = await self._safe_generate(provider_id, prompt)
        if not reply or not reply.strip():
            return MergeDecision(refined=False)

        decision = self._extract_field(reply, r"DECISION:\s*(\S+)", "").strip().lower()
        reason = self._extract_field(reply, r"REASON:\s*(.+?)(?=\n[A-Z]+:|\Z)", "")

        if decision != "yes":
            return MergeDecision(should_merge=False, merge_reason=reason, refined=True)

        return MergeDecision(
            should_merge=True,
            target_topic=existing_topic,
            merge_reason=reason,
            refined=True,
        )

    # ---------- 内部方法 ----------

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
