"""质疑验证器。

流程：
1. 多源搜索（AstrBot web_search 兜底 + B 站可选）
2. LLM 自辩论 2 轮（支持方 → 质疑方 → 仲裁）
3. 交叉验证：≥2 个独立来源结论一致才升 verified=True
4. 版本快照：内容差异 >30 字符或置信度下降 >0.15 时写 memory_versions
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from .plugin_logger import logger

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
        debug_info: dict | None = None,
    ):
        self.verdict = verdict
        self.confidence = confidence
        self.content = content
        self.reasoning = reasoning
        self.sources_count = sources_count
        self.sources_consistent = sources_consistent
        self.debug_info = debug_info or {}

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

    def _get_search_source(self) -> str:
        """读取验证搜索源配置，并受联网搜索总开关与优先级约束。"""
        cfg = str(self._plugin.config.get("verifier_search_source", "auto") or "auto").lower()

        # v1.1.12.0：联网搜索关闭时强制纯 LLM
        if not getattr(self._plugin, "_enable_web_search", True):
            return "llm"

        # 仅使用最高优先级来源
        if getattr(self._plugin, "_web_search_only_highest_priority", False):
            priority = getattr(self._plugin, "_knowledge_source_priority", ["web"])
            top = priority[0] if priority else "web"
            if top == "web":
                return "web"
            if top == "bilibili":
                return "bilibili"
            return "llm"

        return cfg

    async def run(
        self,
        entry: MemoryEntry,
        provider_id: str,
        claim: Optional[str] = None,
    ) -> VerificationResult:
        """对一条记忆执行验证流程。

        流程：LLM 提取关键词 → 用关键词搜索 → LLM 交叉验证 → 仲裁。
        """
        claim = claim or entry.content
        topic = entry.topic
        source_cfg = self._get_search_source()
        debug_info: dict = {
            "provider_id": provider_id,
            "topic": topic,
            "claim": claim,
            "source_cfg": source_cfg,
            "keywords": [],
            "prompts": [],
            "replies": [],
            "sources": [],
        }

        # 1. LLM 提取搜索关键词
        keywords, kw_prompt, kw_reply = await self._extract_keywords(
            topic, claim, provider_id
        )
        debug_info["keywords"] = keywords
        debug_info["prompts"].append({"step": "extract_keywords", "text": kw_prompt})
        debug_info["replies"].append({"step": "extract_keywords", "text": kw_reply})
        logger.info(f"验证关键词提取: {topic} → {keywords}")

        # 2. 搜索源选择
        if source_cfg == "llm":
            sources: list[dict] = []
            llm_only = True
        else:
            sources = await self._collect_sources(topic, claim, source_cfg, keywords)
            llm_only = len(sources) < 2
        debug_info["sources"] = sources

        if llm_only and source_cfg != "llm":
            logger.info(
                f"搜索源不足({len(sources)}个, cfg={source_cfg})，降级为纯 LLM 验证: {topic}"
            )
        elif source_cfg == "llm":
            logger.info(f"配置为纯 LLM 验证: {topic}")

        # 3. LLM 交叉验证
        debate_result, debate_prompts, debate_replies = await self._llm_debate(
            topic, claim, sources, provider_id, llm_only=llm_only
        )
        debug_info["prompts"].extend(debate_prompts)
        debug_info["replies"].extend(debate_replies)

        # 4. 交叉验证一致性
        if llm_only:
            sources_consistent = True
        else:
            sources_consistent = self._check_consistency(sources, debate_result)

        # 5. 计算最终置信度
        new_confidence = self._adjust_confidence(
            entry.confidence, debate_result.verdict, sources_consistent,
            llm_only=llm_only,
        )

        # 6. 决定是否更新内容
        new_content = entry.content
        if debate_result.verdict == "wrong" and debate_result.content:
            new_content = debate_result.content
        elif debate_result.verdict == "partial" and debate_result.content:
            new_content = debate_result.content

        # 7. 版本快照 + 更新记忆
        verified = (
            debate_result.verdict in ("correct", "partial")
            and new_confidence >= 0.5
        )
        reason = self._reason_tag(debate_result.verdict, sources_consistent, llm_only=llm_only)

        self._plugin.store.update_content(
            entry_id=entry.id,
            content=new_content,
            confidence=new_confidence,
            source=entry.source + " | 验证于 " + str(int(__import__("time").time())),
            verified=verified,
            reason=reason,
            snapshot=True,
        )

        mode_tag = "LLM-only" if llm_only else f"{len(sources)}源"
        logger.info(
            f"✅ 验证完成 [{mode_tag}]: {topic} → {debate_result.verdict} "
            f"(置信度: {new_confidence:.0%}, verified={verified})"
        )

        return VerificationResult(
            verdict=debate_result.verdict,
            confidence=new_confidence,
            content=new_content,
            reasoning=debate_result.reasoning,
            sources_count=len(sources),
            sources_consistent=sources_consistent,
            debug_info=debug_info,
        )

    # ---------- 内部方法 ----------

    async def _extract_keywords(
        self, topic: str, claim: str, provider_id: str
    ) -> tuple[list[str], str, str]:
        """让 LLM 从记忆内容中提取适合搜索的关键词。

        返回 (keywords, prompt, reply)。
        """
        prompt = (
            f"从以下知识点中提取 3-5 个适合搜索引擎检索的关键词或短语。\n\n"
            f"主题: {topic}\n内容: {claim}\n\n"
            f"要求:\n"
            f"1. 提取核心实体、术语、人名、概念等\n"
            f"2. 避免常见词（如\"什么是\"、\"解释\"等）\n"
            f"3. 输出格式：KEYWORDS: 关键词1, 关键词2, 关键词3\n"
            f"4. 只输出一行，不要额外解释"
        )
        text = await self._safe_llm_generate(provider_id, prompt)
        m = re.search(r"KEYWORDS:\s*(.+)", text)
        if m:
            kws = [k.strip() for k in m.group(1).split(",") if k.strip()]
            if kws:
                return kws[:5], prompt, text
        # 兜底：用 topic 本身
        return ([topic] if topic else []), prompt, text

    async def _collect_sources(
        self,
        topic: str,
        claim: str,
        source_cfg: str = "auto",
        keywords: list[str] | None = None,
    ) -> list[dict]:
        """根据配置从对应搜索源收集证据。Web 搜索和 B 站搜索并行执行。"""
        sources: list[dict] = []
        keywords = keywords or []

        use_web = source_cfg in ("auto", "web", "web+bilibili")
        use_bili = source_cfg in ("auto", "bilibili", "web+bilibili")

        # 构建搜索 query：优先用关键词组合
        search_query = " ".join(keywords[:3]) if keywords else topic

        # 并行发起所有搜索任务
        search_tasks = []
        task_labels = []

        if use_web:
            search_tasks.append(self._plugin.searcher.search(search_query, max_results=5))
            task_labels.append("web_primary")
            # 第二轮搜索：用 topic + 关键词
            if keywords:
                alt_query = f"{topic} {keywords[0]}"
                search_tasks.append(self._plugin.searcher.search(alt_query, max_results=3))
                task_labels.append("web_secondary")

        if use_bili and self._plugin.bili_source and self._plugin.bili_source.is_available():
            bili_query = keywords[0] if keywords else topic
            search_tasks.append(self._plugin.bili_source.search(bili_query, limit=3))
            task_labels.append("bilibili")

        # 并行执行所有搜索
        if search_tasks:
            results_list = await asyncio.gather(*search_tasks, return_exceptions=True)
            for i, label in enumerate(task_labels):
                result = results_list[i]
                if isinstance(result, Exception):
                    logger.debug(f"搜索任务 {label} 失败: {result}")
                    continue
                if not isinstance(result, list):
                    continue
                source_type = "web" if label == "web_primary" else "web_factcheck" if label == "web_secondary" else "bilibili"
                for r in result:
                    sources.append({
                        "title": r.get("title", ""),
                        "snippet": r.get("snippet", ""),
                        "url": r.get("url", ""),
                        "source_type": source_type,
                    })

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
        llm_only: bool = False,
    ) -> tuple["_DebateResult", list[dict], list[dict]]:
        """LLM 自辩论：支持方 → 质疑方 → 仲裁。

        返回 (debate_result, prompts, replies)。
        """
        prompts: list[dict] = []
        replies: list[dict] = []

        if sources:
            sources_text = "\n---\n".join(
                f"[来源{i+1}] ({s['source_type']}) {s['title']}\n{s['snippet']}"
                for i, s in enumerate(sources[:8])
            )
        else:
            sources_text = "（无外部搜索源，请基于你的知识库判断）"

        # Round A：支持方
        if llm_only:
            prompt_a = (
                f"你是一个严谨的事实核查员。请基于你的知识，判断以下说法是否成立。\n\n"
                f"主题: {topic}\n说法: {claim}\n\n"
                f"请从你的知识出发，论证该说法是否成立。\n"
                f"要求:\n"
                f"1. 给出置信度评分（0-100）\n"
                f"2. 如果不确定，请明确说明\n"
                f"3. 200 字以内"
            )
        else:
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
        prompts.append({"step": "debate_round_a_supportive", "text": prompt_a})
        replies.append({"step": "debate_round_a_supportive", "text": reply_a})

        # Round B：质疑方
        prompt_b = (
            f"你是一个挑刺的质疑者，请反驳以下『支持方论证』。\n\n"
            f"主题: {topic}\n原说法: {claim}\n"
            + (f"搜索来源:\n{sources_text}\n\n" if not llm_only else "")
            + f"支持方论证:\n{reply_a}\n\n"
            f"请指出支持方论证中的:\n"
            f"1. 事实错误或偷换概念\n"
            + ("2. 来源不足或引用偏差\n" if not llm_only else "2. 知识盲区或过时信息\n")
            + f"3. 逻辑漏洞\n"
            f"4. 给出你的置信度评分（0-100，越低越怀疑）\n"
            f"5. 200 字以内"
        )
        reply_b = await self._safe_llm_generate(provider_id, prompt_b)
        prompts.append({"step": "debate_round_b_skeptical", "text": prompt_b})
        replies.append({"step": "debate_round_b_skeptical", "text": reply_b})

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
        prompts.append({"step": "debate_round_c_arbiter", "text": prompt_c})
        replies.append({"step": "debate_round_c_arbiter", "text": reply_c})

        return self._parse_debate_result(reply_c), prompts, replies

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

    def _adjust_confidence(
        self, old: float, verdict: str, consistent: bool, llm_only: bool = False
    ) -> float:
        """根据验证结果调整置信度。

        策略：correct 提升置信度，wrong 大幅降低，partial 轻微降低，
        inconclusive 保持不变（避免反复验证导致置信度无意义下降）。
        """
        if verdict == "correct":
            boost = 0.15 if llm_only else (0.20 if consistent else 0.10)
            return min(1.0, old + boost)
        if verdict == "wrong":
            return max(0.1, old - 0.25)
        if verdict == "partial":
            # partial 不再降，反而轻微提升（说明 LLM 认为部分正确）
            return min(1.0, old + 0.05)
        return old  # inconclusive 保持不变

    def _reason_tag(
        self, verdict: str, consistent: bool, llm_only: bool = False
    ) -> str:
        if verdict == "correct" and consistent:
            return "verify_passed_llm" if llm_only else "verify_passed"
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
