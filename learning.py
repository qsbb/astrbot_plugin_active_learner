"""后置学习与群黑话批量学习：LearningMixin。

从 main.py 拆出的纯结构分层，方法实现逐行原样搬运，未做行为变更。
注意：触发后置学习的 @filter.on_llm_response 钩子仍留在 main.py，
因为 AstrBot 靠扫描类上的装饰器注册事件；这里只承载无装饰器的实现体。
"""

from __future__ import annotations

import asyncio
import re

from astrbot.api.event import AstrMessageEvent

from .models import Scope, now_ts
from .plugin_logger import logger
from .slang_capture import build_batch_prompt, parse_batch_response


class LearningMixin:
    """学习侧行为。由 ActiveLearnerPlugin 混入，依赖宿主的 store/refiner 等属性。"""

    async def _post_learn_analysis_bg(self, event: AstrMessageEvent, response) -> None:
        """后置学习分析的后台包装，不阻塞回复发送。"""
        try:
            await self._post_learn_analysis(event, response)
        except Exception as e:
            logger.debug(f"后置学习分析异常: {e}")

    async def _post_learn_analysis(self, event: AstrMessageEvent, response) -> None:
        """回复完成后，异步分析对话是否包含可学习知识点，自动存入记忆库。"""
        # 1. 开关检查
        if not self._enable_active_learn_hint or self._learn_weight <= 0.0:
            logger.debug(
                f"后置学习跳过: enable={self._enable_active_learn_hint}, weight={self._learn_weight}"
            )
            return

        # 2. 提取用户消息
        user_msg = ""
        try:
            user_msg = (event.get_message_str() or "").strip()
        except Exception as e:
            logger.debug(f"后置学习跳过: 提取用户消息失败 {e}")
            return
        if not user_msg or len(user_msg) < 5:
            logger.debug(
                f"后置学习跳过: 用户消息过短 ({len(user_msg) if user_msg else 0}字)"
            )
            return

        # 3. 管理员：只有包含明确学习意图（记住/学习/保存等）时才分析
        if self._is_admin_user(event):
            learn_intent = re.search(
                r"(记住|记下来|学习|学一下|记一下|保存|存起来|存下|收录|记到知识|记到记忆|记入|收录到)",
                user_msg,
            )
            if not learn_intent:
                logger.debug("后置学习跳过: 管理员消息无明确学习意图")
                return
            # 去掉学习指令，保留真正要学习的内容
            user_msg = re.sub(
                r"(?:请|帮我)?(?:记住|记下来|学习|学一下|记一下|保存|存起来|存下|收录|记到知识|记到记忆|记入|收录到)[：:的]?\s*",
                "",
                user_msg,
            ).strip()
            if not user_msg or len(user_msg) < 2:
                logger.debug("后置学习跳过: 去掉指令后用户消息为空")
                return
        else:
            logger.debug("后置学习分析: 非管理员用户，自动分析")

        # 4. 提取 LLM 回复
        llm_text = ""
        if hasattr(response, "completion_text"):
            llm_text = (getattr(response, "completion_text") or "").strip()
        elif hasattr(response, "text"):
            llm_text = (getattr(response, "text") or "").strip()
        elif isinstance(response, str):
            llm_text = response.strip()
        if not llm_text:
            logger.debug("后置学习跳过: LLM 回复为空")
            return

        # 5. 节流：每 scope 30 秒最多分析一次
        scope = Scope.from_event(event)
        scope_key = f"{scope.type}:{scope.id}"
        now = now_ts()
        last = getattr(self, "_last_post_learn", {})
        if now - last.get(scope_key, 0) < 30:
            logger.debug(f"后置学习跳过: 节流中 (scope={scope_key})")
            return
        last[scope_key] = now
        self._last_post_learn = last

        # 6. 调用 LLM 分析该对话是否包含新知识点
        provider_id = ""
        try:
            provider_id = await self._resolve_plugin_provider_id(
                umo=getattr(event, "unified_msg_origin", "")
            )
        except Exception:
            pass

        if not provider_id:
            logger.debug("后置学习跳过: 未解析到 LLM provider")
            return

        prompt = (
            "你是一个知识提取助手。分析以下对话，判断用户是否向机器人传授了新知识。\n\n"
            f"用户消息：{user_msg}\n"
            f"你的回复：{llm_text}\n\n"
            "【要求】\n"
            "1. TYPE=learn（有新知识点）或 skip（无新知识点，如闲聊、问候、已有知识确认等）\n"
            "2. 如果是 learn，给出 TOPIC（主题，10字内）、CONTENT（要记忆的内容，50字内）、KEYWORDS（逗号分隔）\n\n"
            "【输出格式（严格按此格式，不要额外内容）】\n"
            "TYPE: <learn 或 skip>\n"
            "TOPIC: <主题，仅 TYPE=learn 时需要>\n"
            "CONTENT: <记忆内容，仅 TYPE=learn 时需要>\n"
            "KEYWORDS: <关键词，仅 TYPE=learn 时需要>"
        )

        logger.debug(f"后置学习分析: 调用 LLM 判断 (msg={user_msg[:40]}...)")
        text = await self.refiner._safe_generate(provider_id, prompt)
        if not text:
            logger.debug("后置学习跳过: LLM 分析无返回")
            return

        # 7. 解析响应
        type_match = re.search(r"TYPE:\s*(\w+)", text)
        if not type_match or type_match.group(1).lower() != "learn":
            logger.debug(f"后置学习跳过: LLM 判定为 skip (raw={text[:80]})")
            return

        topic = ""
        content = ""
        keywords: list[str] = []

        topic_m = re.search(r"TOPIC:\s*(.+)", text)
        if topic_m:
            topic = topic_m.group(1).strip()
        content_m = re.search(r"CONTENT:\s*(.+)", text)
        if content_m:
            content = content_m.group(1).strip()
        keywords_m = re.search(r"KEYWORDS:\s*(.+)", text)
        if keywords_m:
            keywords = [k.strip() for k in keywords_m.group(1).split(",") if k.strip()]

        if not topic or not content:
            logger.debug(
                f"后置学习跳过: LLM 返回 learn 但缺 topic/content (topic={topic!r}, content={content!r})"
            )
            return

        # 8. 融合检查：搜索本地已有记忆，判断是否与现有条目融合
        if provider_id:
            try:
                existing = self.store.search(scope, topic, top_k=3)
                if existing:
                    top_match = existing[0]
                    if top_match.entry.topic.lower() != topic.lower():
                        merge_decision = await self.refiner.check_merge(
                            new_topic=topic,
                            new_summary=content,
                            new_keywords=keywords or [topic],
                            existing_topic=top_match.entry.topic,
                            existing_summary=top_match.entry.content,
                            existing_keywords=top_match.entry.keywords or [],
                            provider_id=provider_id,
                        )
                        if merge_decision.should_merge:
                            logger.info(
                                f"🧬 后置融合：新知识「{topic}」→ 融合到已有「{merge_decision.target_topic}」"
                                f"（理由：{merge_decision.merge_reason}）"
                            )
                            topic = merge_decision.target_topic
                            existing_kws = top_match.entry.keywords or []
                            merged_kws = list(
                                dict.fromkeys(existing_kws + (keywords or [topic]))
                            )
                            keywords = merged_kws
            except Exception as e:
                logger.debug(f"后置学习融合检查失败: {e}")

        # 9. 存入记忆
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            entry = await asyncio.to_thread(
                self.store.add_or_update,
                scope=scope,
                topic=topic,
                content=content,
                keywords=keywords or [topic],
                source="后置学习分析",
                sources_detail=None,
                confidence=self._default_confidence,
                origin=f"conversation:{umo}" if umo else "conversation",
            )
            logger.info(
                f"✅ 后置学习已存入记忆: {topic} (id: {entry.id}, scope: {scope})"
            )
        except Exception as e:
            logger.error(f"❌ 后置学习存储失败「{topic}」: {e}", exc_info=True)

    # ---------- v1.1.4.0：群黑话定时批量学习（捕获已移至 on_llm_request）----------

    def _maybe_trigger_batch_learn(self, scope: Scope) -> None:
        """节流检查：每 scope 5 分钟最多查一次 DB；满足条件则托管后台批量学习。"""
        scope_key = f"{scope.type}:{scope.id}"
        now = now_ts()
        last = self._slang_last_check.get(scope_key, 0.0)
        if now - last < 300:  # 5 分钟节流
            return
        self._slang_last_check[scope_key] = now
        try:
            last_batch = self.store.get_last_batch_time(scope)
            if now - last_batch < self._slang_interval_hours * 3600:
                return
            pending = self.store.list_pending_slang(scope, limit=self._slang_batch_size)
            if len(pending) < self._slang_batch_size:
                return
            # 过滤 occurrences < min_occurrences
            qualified = [
                c for c in pending if c["occurrences"] >= self._slang_min_occurrences
            ]
            if len(qualified) < self._slang_batch_size:
                return
            self._create_background_task(
                self._async_batch_learn_slang(scope, qualified),
                name=f"active-learner-slang-{scope_key}",
            )
        except Exception as e:
            logger.debug(f"slang 触发检查失败: {e}")

    async def _async_batch_learn_slang(
        self, scope: Scope, candidates: list[dict]
    ) -> None:
        """1 次 LLM 调用批量学习 K 个候选词。"""
        try:
            provider_id = ""
            try:
                provider_id = await self._resolve_plugin_provider_id(umo="")
            except Exception:
                provider_id = ""
            prompt = build_batch_prompt(candidates)
            # 复用 refiner._safe_generate 的 LLM 调用模式
            response_text = await self.refiner._safe_generate(provider_id, prompt)
            if not response_text or not response_text.strip():
                logger.warning(
                    f"slang 批量学习失败：LLM 无响应 (scope: {scope}, candidates: {len(candidates)})"
                )
                return
            parsed = parse_batch_response(response_text, candidates)
            parsed_phrases = {p["phrase"] for p in parsed}
            success = 0
            for item in parsed:
                try:
                    await asyncio.to_thread(
                        self.store.add_or_update,
                        scope,
                        item["phrase"],
                        item["summary"],
                        keywords=item["keywords"],
                        source="群黑话自动学习",
                        confidence=item["confidence"],
                        origin="slang",
                    )
                    success += 1
                except Exception as e:
                    logger.warning(f"slang 入库失败「{item['phrase']}」: {e}")
                await asyncio.to_thread(
                    self.store.mark_slang_learned, scope, item["phrase"]
                )
            # 标记未解析的候选词为 learned（避免无限重试）
            for c in candidates:
                if c["phrase"] not in parsed_phrases:
                    await asyncio.to_thread(
                        self.store.mark_slang_learned, scope, c["phrase"]
                    )
            if self.embedder is not None:
                self.embedder.invalidate_matrix_cache()
            logger.info(
                f"✅ slang 批量学习: {success}/{len(candidates)} 成功 (scope: {scope})"
            )
        except Exception as e:
            logger.warning(f"❌ slang 批量学习异常: {e}")
