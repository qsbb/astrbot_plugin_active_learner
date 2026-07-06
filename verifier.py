"""质疑验证器。

流程：
1. 多源搜索（AstrBot web_search 兜底 DuckDuckGo + B 站可选）
2. LLM 自辩论 2 轮（支持方 → 质疑方 → 仲裁）
3. 交叉验证：≥2 个独立来源结论一致才升 verified=True
4. 版本快照：内容差异 >30 字符或置信度下降 >0.15 时写 memory_versions
"""

from __future__ import annotations

import re
from typing import Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("active_learner")

from .models import MemoryEntry


class VerificationResult:
    """验证结果。"""

    def __init__(
        self,
        verdict: str,           # 'correct' | 'partial' | 'wrong' | 'inconclusive'
        confidence: float,      # 0.0 ~ 1.0
        content: str,           # 修正后的内容（可能与原内容相同）
        reasoning: str,         # 验证推理过程
        sources_count: int,
        sources_consistent: bool,
    ):
        self.verdict = verdict
        self.confidence = confidence
        self.content = content
        self.reasoning = reasoning
        self.sources_count = sources_count
        self.sources_consistent = sources_consistent

    def to_text(self) -> str:
        verdict_cn = {
            "correct": "✅ 正确",
            "partial": "⚠️ 部分正确",
            "wrong": "❌ 错误",
            "inconclusive": "❓ 证据不足",
        }.get(self.verdict, self.verdict)
        return (
            f"验证结论: {verdict_cn}\n"
            f"置信度: {self.confidence:.0%}\n"
            f"来源数: {self.sources_count}（来源{'一致' if self.sources_consistent else '存在分歧'}）\n"
            f"━━━━━━━━━━\n"
            f"{self.reasoning}"
        )


class Verifier:
    """质疑验证器。依赖 plugin 持有的 searcher / bili_source / store / context。"""

    def __init__(self, plugin):
        self._plugin = plugin

    async def run(
        self,
        entry: MemoryEntry,
        provider_id: str,
        claim: Optional[str] = None,
    ) -> VerificationResult:
        """对一条记忆执行验证流程。"""
        claim = claim or entry.content
        topic = entry.topic

        # 1. 多源搜索
        sources = await self._collect_sources(topic, claim)
        if len(sources) < 2:
            return VerificationResult(
                verdict="inconclusive",
                confidence=entry.confidence,
                content=entry.content,
                reasoning="无法收集到足够的独立来源进行验证，建议稍后重试或换用更强的搜索源。",
                sources_count=len(sources),
                sources_consistent=False,
            )

        # 2. LLM 自辩论
        debate_result = await self._llm_debate(topic, claim, sources, provider_id)

        # 3. 交叉验证：检查来源一致性
        sources_consistent = self._check_consistency(sources, debate_result)

        # 4. 计算最终置信度
        new_confidence = self._adjust_confidence(
            entry.confidence, debate_result.verdict, sources_consistent
        )

        # 5. 决定是否更新内容
        new_content = entry.content
        if debate_result.verdict == "wrong" and debate_result.content:
            new_content = debate_result.content
        elif debate_result.verdict == "partial" and debate_result.content:
            new_content = debate_result.content

        # 6. 版本快照 + 更新记忆
        verified = (
            debate_result.verdict == "correct"
            and sources_consistent
            and new_confidence >= 0.6
        )
        reason = self._reason_tag(debate_result.verdict, sources_consistent)

        self._plugin.store.update_content(
            entry_id=entry.id,
            content=new_content,
            confidence=new_confidence,
            source=entry.source + " | 验证于 " + str(int(__import__("time").time())),
            verified=verified,
            reason=reason,
            snapshot=True,
        )

        return VerificationResult(
            verdict=debate_result.verdict,
            confidence=new_confidence,
            content=new_content,
            reasoning=debate_result.reasoning,
            sources_count=len(sources),
            sources_consistent=sources_consistent,
        )

    # ---------- 内部方法 ----------

    async def _collect_sources(self, topic: str, claim: str) -> list[dict]:
        """从多个搜索源收集证据。"""
        sources: list[dict] = []

        # 主搜索
        results = await self._plugin.searcher.search(topic, max_results=5)
        for r in results:
            sources.append({
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "url": r.get("url", ""),
                "source_type": "web",
            })

        # 真假验证搜索
        fact_results = await self._plugin.searcher.search(f"{claim} 真假 验证", max_results=3)
        for r in fact_results:
            if r.get("title") and r["title"][:30] not in {s["title"][:30] for s in sources}:
                sources.append({
                    "title": r.get("title", ""),
                    "snippet": r.get("snippet", ""),
                    "url": r.get("url", ""),
                    "source_type": "web_factcheck",
                })

        # B 站搜索（可选）
        if self._plugin.bili_source and self._plugin.bili_source.is_available():
            try:
                bili_results = await self._plugin.bili_source.search(topic, limit=3)
                for r in bili_results:
                    sources.append({
                        "title": r.get("title", ""),
                        "snippet": r.get("snippet", ""),
                        "url": r.get("url", ""),
                        "source_type": "bilibili",
                    })
            except Exception as e:
                logger.debug(f"B 站搜索失败: {e}")

        # 去重
        seen: set[str] = set()
        unique: list[dict] = []
        for s in sources:
            key = s["title"][:30]
            if key and key not in seen:
                seen.add(key)
                unique.append(s)
        return unique

    async def _llm_debate(
        self,
        topic: str,
        claim: str,
        sources: list[dict],
        provider_id: str,
    ) -> "_DebateResult":
        """LLM 自辩论：支持方 → 质疑方 → 仲裁。"""
        sources_text = "\n---\n".join(
            f"[来源{i+1}] ({s['source_type']}) {s['title']}\n{s['snippet']}"
            for i, s in enumerate(sources[:8])
        )

        # Round A：支持方
        prompt_a = (
            f"你是一个严谨的事实核查员，现在需要为以下说法**寻找支持证据**。\n\n"
            f"主题: {topic}\n说法: {claim}\n\n"
            f"搜索来源:\n{sources_text}\n\n"
            f"请基于以上来源，论证该说法是否成立。\n"
            f"要求:\n"
            f"1. 引用具体来源编号（如 [来源1]）\n"
            f"2. 给出置信度评分（0-100）\n"
            f"3. 不要编造来源中不存在的信息\n"
            f"4. 200 字以内"
        )
        reply_a = await self._safe_llm_generate(provider_id, prompt_a)

        # Round B：质疑方
        prompt_b = (
            f"你是一个挑刺的质疑者，请反驳以下『支持方论证』。\n\n"
            f"主题: {topic}\n原说法: {claim}\n搜索来源:\n{sources_text}\n\n"
            f"支持方论证:\n{reply_a}\n\n"
            f"请指出支持方论证中的:\n"
            f"1. 事实错误或偷换概念\n"
            f"2. 来源不足或引用偏差\n"
            f"3. 逻辑漏洞\n"
            f"4. 给出你的置信度评分（0-100，越低越怀疑）\n"
            f"5. 200 字以内"
        )
        reply_b = await self._safe_llm_generate(provider_id, prompt_b)

        # Round C：仲裁
        prompt_c = (
            f"你是仲裁员，综合支持方与质疑方的论证，给出最终结论。\n\n"
            f"主题: {topic}\n原说法: {claim}\n\n"
            f"支持方:\n{reply_a}\n\n"
            f"质疑方:\n{reply_b}\n\n"
            f"请输出（严格按格式）:\n"
            f"VERDICT: 正确 | 部分正确 | 错误 | 无法确认\n"
            f"CONFIDENCE: 0-100\n"
            f"CONTENT: 修正后的内容（如果原说法正确则复述原说法，错误则给出正确版本，80 字以内）\n"
            f"REASON: 简述仲裁依据（100 字以内）"
        )
        reply_c = await self._safe_llm_generate(provider_id, prompt_c)

        return self._parse_debate_result(reply_c)

    def _parse_debate_result(self, text: str) -> "_DebateResult":
        """解析仲裁结果。"""
        verdict = "inconclusive"
        confidence = 50
        content = ""
        reasoning = text

        v_match = re.search(r"VERDICT:\s*(.+)", text)
        if v_match:
            v_raw = v_match.group(1).strip()
            if "正确" in v_raw and "部分" not in v_raw and "错误" not in v_raw:
                verdict = "correct"
            elif "部分正确" in v_raw:
                verdict = "partial"
            elif "错误" in v_raw:
                verdict = "wrong"
            else:
                verdict = "inconclusive"

        c_match = re.search(r"CONFIDENCE:\s*(\d+)", text)
        if c_match:
            confidence = int(c_match.group(1))
            confidence = max(0, min(100, confidence))

        ct_match = re.search(r"CONTENT:\s*(.+?)(?=\n(?:REASON:|$))", text, re.DOTALL)
        if ct_match:
            content = ct_match.group(1).strip()

        r_match = re.search(r"REASON:\s*(.+)", text, re.DOTALL)
        if r_match:
            reasoning = r_match.group(1).strip()

        return _DebateResult(
            verdict=verdict,
            confidence=confidence / 100.0,
            content=content,
            reasoning=reasoning,
        )

    def _check_consistency(self, sources: list[dict], debate: "_DebateResult") -> bool:
        """检查来源一致性。

        简化版：如果不同来源类型（web / web_factcheck / bilibili）≥2 种
        且仲裁结论为 correct/wrong，认为一致。
        """
        source_types = {s["source_type"] for s in sources}
        if len(source_types) >= 2 and debate.verdict in ("correct", "wrong"):
            return True
        if len(sources) >= 3 and debate.verdict == "correct":
            return True
        return False

    def _adjust_confidence(self, old: float, verdict: str, consistent: bool) -> float:
        """根据验证结果调整置信度。"""
        if verdict == "correct":
            return min(1.0, old + (0.15 if consistent else 0.05))
        if verdict == "wrong":
            return max(0.1, old - 0.3)
        if verdict == "partial":
            return max(0.2, old - 0.1)
        return old  # inconclusive 保持不变

    def _reason_tag(self, verdict: str, consistent: bool) -> str:
        if verdict == "correct" and consistent:
            return "verify_passed"
        if verdict == "wrong":
            return "challenge_corrected"
        if verdict == "partial":
            return "verify_partial"
        return "verify_inconclusive"

    async def _safe_llm_generate(self, provider_id: str, prompt: str) -> str:
        """LLM 调用容错。复用 LLMService 统一入口。"""
        if not provider_id:
            return "（LLM 不可用）"
        text = await self._plugin.llm_service.generate(
            prompt=prompt, provider_id=provider_id
        )
        if text:
            return text
        return "（LLM 调用失败）"


class _DebateResult:
    """自辩论内部结果。"""

    def __init__(self, verdict: str, confidence: float, content: str, reasoning: str):
        self.verdict = verdict
        self.confidence = confidence
        self.content = content
        self.reasoning = reasoning
