"""记忆检索与外部搜索：RetrievalMixin。

从 main.py 拆出的纯结构分层，方法实现逐行原样搬运，未做行为变更。
这些方法都不带 AstrBot 装饰器，因此可以安全离开 main.py——
带 @filter / @register 的方法必须留在 main.py，框架靠扫描装饰器注册。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from .constants import (
    _EMBEDDING_TIMEOUT_SECONDS,
    _EXTERNAL_SEARCH_DEADLINE_SECONDS,
    _FTS_MIN_CONFIDENCE,
    _FTS_MIN_TOP_SCORE_RATIO,
    _FTS_SUFFICIENT_HITS,
    _MEMORY_INJECT_ITEM_CHARS,
    _MEMORY_INJECT_MAX_COUNT,
    _MEMORY_INJECT_TOTAL_CHARS,
)
from .graph_memory import (
    is_complex_query,
    normalize_reconstruction_mode,
    parse_route_selection,
    should_reconstruct,
)
from .models import Scope
from .plugin_logger import logger
from .runtime import (
    build_semantic_search_instruction,
    comparison_coverage,
    entry_covers_topic,
    extract_comparison_objects,
    object_is_covered,
    topic_terms,
)


class RetrievalMixin:
    """检索侧行为。由 ActiveLearnerPlugin 混入，依赖宿主的 store/embedder 等属性。"""

    @staticmethod
    def _parse_hybrid_weights(s: str) -> tuple[float, float]:
        """解析 '0.4,0.6' 格式。返回 (fts_weight, vec_weight)。"""
        try:
            parts = [float(x.strip()) for x in str(s).split(",")]
            if len(parts) == 2 and all(0.0 <= p <= 1.0 for p in parts):
                return parts[0], parts[1]
        except Exception:
            pass
        return 0.4, 0.6

    @staticmethod
    def _parse_source_priority(s: str) -> list[str]:
        """解析外部知识搜索源优先级字符串，返回去重后的小写列表。

        本地记忆不属于外部搜索源，因此只接受 url / web / bilibili。
        """
        try:
            parts = [p.strip().lower() for p in str(s).split(",") if p.strip()]
            # 去重并保持顺序
            seen: set[str] = set()
            unique: list[str] = []
            for p in parts:
                if p in ("url", "web", "bilibili") and p not in seen:
                    seen.add(p)
                    unique.append(p)
            if unique:
                return unique
        except Exception:
            pass
        return ["url", "web", "bilibili"]

    def _source_available(self, source: str) -> bool:
        if source == "url":
            registry = getattr(self, "url_sources", None)
            return bool(registry and registry.enabled_count > 0)
        if source == "web":
            return bool(getattr(self.searcher, "is_available", False))
        if source == "bilibili":
            return bool(
                getattr(self, "_enable_bilibili", False)
                and self.bili_source
                and self.bili_source.is_available()
            )
        return False

    def _is_source_enabled(self, source: str) -> bool:
        """判断某个外部知识搜索源是否在当前配置下启用。"""
        source = source.lower()
        if source not in ("url", "web", "bilibili"):
            return False
        if source == "bilibili" and not getattr(self, "_enable_bilibili", False):
            return False
        if source not in self._knowledge_source_priority:
            return False
        if self._web_search_only_highest_priority:
            first_available = next(
                (
                    item
                    for item in self._knowledge_source_priority
                    if self._source_available(item)
                ),
                None,
            )
            return first_available == source
        return True

    async def _search_external_sources(
        self,
        query: str,
        *,
        url_limit: int = 3,
        web_limit: int = 5,
        bili_limit: int = 3,
        allowed_sources: Optional[set[str]] = None,
    ) -> list[dict]:
        """在统一总 deadline 内并发搜索启用的外部源。"""
        calls = {}
        allow_url = allowed_sources is None or "url" in allowed_sources
        allow_web = allowed_sources is None or "web" in allowed_sources
        allow_bili = allowed_sources is None or "bilibili" in allowed_sources
        if (
            allow_url
            and url_limit > 0
            and self._enable_web_search
            and self._is_source_enabled("url")
        ):
            calls["url"] = lambda: self.url_sources.search(
                query, max_results=url_limit
            )
        if (
            allow_web
            and web_limit > 0
            and self._enable_web_search
            and self._is_source_enabled("web")
        ):
            calls["web"] = lambda: self.searcher.search(query, max_results=web_limit)
        if (
            allow_bili
            and bili_limit > 0
            and self._is_source_enabled("bilibili")
            and self.bili_source
            and self.bili_source.is_available()
        ):
            calls["bilibili"] = lambda: self.bili_source.search(query, limit=bili_limit)
        calls = {
            source: calls[source]
            for source in self._knowledge_source_priority
            if source in calls
        }
        return await self._external_search.search(
            calls, deadline=_EXTERNAL_SEARCH_DEADLINE_SECONDS
        )

    def _query_in_domain_scope(self, query: str) -> bool:
        """判断查询是否命中用户设置的知识领域范围。

        未设置范围、或开启跨领域时视为命中。
        """
        if self._enable_cross_domain:
            return True
        if not self._knowledge_domain_scope:
            return True
        q = query.lower()
        return any(
            domain in q or q in domain for domain in self._knowledge_domain_scope
        )

    def _strip_search_tools(self, req) -> int:
        """从 LLM 请求中移除本插件的搜索/学习类工具定义。

        用于跨领域限制场景，确保 LLM 不再调用搜索工具。
        """
        if not hasattr(req, "tools") or not req.tools:
            return 0
        deny_names: set[str] = set()
        for t in self._tools or []:
            try:
                name = t.get("function", {}).get("name")
                if name:
                    deny_names.add(name)
            except Exception:
                continue
        # 同时屏蔽 AstrBot 内置联网搜索工具（如果存在）
        deny_names.update({"web_search", "astr_kb_search"})

        original = list(req.tools)
        cleaned: list = []
        for t in original:
            name = None
            try:
                name = t.get("function", {}).get("name")
            except Exception:
                pass
            if name in deny_names or (name and name.startswith("web_search_")):
                continue
            cleaned.append(t)
        req.tools = cleaned
        return len(original) - len(cleaned)

    def _get_learn_prompt(self, *, has_memory_hits: bool = False) -> str | None:
        """返回连续强度的语义检索策略；判断由主模型完成，不增加 LLM 调用。"""
        w = self._learn_weight
        if w <= 0.0 or not self._enable_active_learn_hint:
            return None
        return build_semantic_search_instruction(
            w,
            has_memory_hits=has_memory_hits,
        )

    def _hits_match_priority(self, hits) -> bool:
        """检查检索结果中是否有任一记忆命中关心领域。"""
        if not self._priority_topics or not hits:
            return False
        for h in hits:
            topic_lower = (h.entry.topic or "").lower()
            kws = h.entry.keywords or []
            text_to_check = topic_lower + " " + " ".join(k.lower() for k in kws)
            if any(pt in text_to_check for pt in self._priority_topics):
                return True
        return False

    def _fts_hits_sufficient(self, hits) -> bool:
        """FTS 充足需同时满足数量和保守的最低质量要求。"""
        required_hits = min(_FTS_SUFFICIENT_HITS, self._context_inject_count)
        if len(hits) < required_hits or not hits:
            return False
        top_score = max(hit.score for hit in hits)
        has_credible_hit = any(
            hit.entry.verified or hit.entry.confidence >= _FTS_MIN_CONFIDENCE
            for hit in hits
        )
        return (
            top_score >= self._hybrid_weights[0] * _FTS_MIN_TOP_SCORE_RATIO
            and has_credible_hit
        )

    async def _search_memory_once(
        self, scope: Scope, query: str, query_vec=None
    ) -> list:
        async with self._retrieval_semaphore:
            return await asyncio.to_thread(
                self.store.search_hybrid,
                scope,
                query,
                self._context_inject_count,
                embedder=self.embedder,
                fts_weight=self._hybrid_weights[0],
                vec_weight=self._hybrid_weights[1],
                enable_scope_fallback=self._enable_scope_fallback,
                decay_half_life_days=self._decay_half_life_days,
                query_vec=query_vec,
                priority_topics=self._priority_topics,
                priority_boost=self._priority_boost,
                track_access=False,
            )

    @staticmethod
    def _merge_search_hits(*groups) -> list:
        by_id = {}
        for group in groups:
            for hit in group or []:
                current = by_id.get(hit.entry.id)
                if current is None or hit.score > current.score:
                    by_id[hit.entry.id] = hit
        return sorted(by_id.values(), key=lambda hit: hit.score, reverse=True)

    async def _route_graph_hits(self, query: str, hits: list) -> list:
        """智能模式下最多一次 LLM 选路；失败返回原图候选。"""
        if not hits or not getattr(self, "llm_service", None):
            return hits
        allowed = [hit.entry.id for hit in hits]
        evidence = "\n".join(
            f"ID={hit.entry.id} CUE={hit.retrieval_path[0] if hit.retrieval_path else ''} "
            f"TAG={hit.retrieval_path[1] if len(hit.retrieval_path) > 1 else ''} "
            f"CONTENT={hit.entry.content[:240]}"
            for hit in hits
        )
        prompt = (
            "你是记忆检索路由器，只能从候选 ID 中选择最有助于回答问题。"
            "不要生成事实，不要改写内容，不要输出候选之外的 ID。\n"
            f"问题：{query[:500]}\n候选：\n{evidence}\n"
            "严格输出一行：SELECT: <ID1>, <ID2>\n"
        )
        try:
            reply = await asyncio.wait_for(
                self.llm_service.generate(
                    prompt=prompt,
                    provider_id=getattr(self, "_cfg_llm_provider_id", "") or None,
                ),
                timeout=2.5,
            )
        except Exception as exc:
            logger.debug("图记忆智能选路降级: %s", exc)
            return hits
        selected = parse_route_selection(reply, allowed, max_count=4)
        if not selected:
            return hits
        order = {memory_id: index for index, memory_id in enumerate(selected)}
        return sorted(
            hits,
            key=lambda hit: order.get(hit.entry.id, len(order) + 1),
        )
    async def _retrieve_memory(
        self, scope: Scope, query: str
    ) -> tuple[list, str, dict[str, bool]]:
        """并发启动整句 FTS/Embedding，必要时合并并逐个补查缺失对象。"""
        objects = extract_comparison_objects(query)

        async def embed_and_search(item: str) -> list:
            if self.embedder is None:
                return []
            async with self._retrieval_semaphore:
                vector = await asyncio.wait_for(
                    self.embedder.embed_query(item),
                    timeout=_EMBEDDING_TIMEOUT_SECONDS,
                )
            if vector is None:
                return []
            return await self._search_memory_once(scope, item, vector)

        fts_task = asyncio.create_task(self._search_memory_once(scope, query))
        vector_task = (
            asyncio.create_task(embed_and_search(query))
            if self.embedder is not None
            else None
        )
        hits = await fts_task
        coverage = comparison_coverage(objects, hits)
        fts_is_final = self._fts_hits_sufficient(hits) and all(coverage.values())
        mode = "fts"

        if vector_task is not None:
            if fts_is_final:
                vector_task.cancel()
                await asyncio.gather(vector_task, return_exceptions=True)
            else:
                vector_group = await asyncio.gather(vector_task, return_exceptions=True)
                if isinstance(vector_group[0], list) and vector_group[0]:
                    hits = self._merge_search_hits(hits, vector_group[0])
                    mode = "hybrid"
                    coverage = comparison_coverage(objects, hits)

        # 整句混合结果仍未覆盖的对象，才分别补查；不重复查询已有对象。
        missing = [obj for obj, covered in coverage.items() if not covered]
        if missing:

            async def search_missing_object(item: str) -> list:
                item_fts = asyncio.create_task(self._search_memory_once(scope, item))
                item_vector = (
                    asyncio.create_task(embed_and_search(item))
                    if self.embedder is not None
                    else None
                )
                item_hits = await item_fts
                if item_vector is not None:
                    vector_result = await asyncio.gather(
                        item_vector, return_exceptions=True
                    )
                    if isinstance(vector_result[0], list):
                        item_hits = self._merge_search_hits(item_hits, vector_result[0])
                return item_hits

            object_groups = await asyncio.gather(
                *(search_missing_object(obj) for obj in missing)
            )
            hits = self._merge_search_hits(hits, *object_groups)
            if any(object_groups) and self.embedder is not None:
                mode = "hybrid"
            coverage = comparison_coverage(objects, hits)

        graph_mode = normalize_reconstruction_mode(
            getattr(self, "_graph_reconstruction_mode", "off")
        )
        graph_enabled = bool(getattr(self, "_graph_memory_enabled", False))
        graph_gate = (
            graph_enabled
            and hasattr(self.store, "search_graph")
            and (
                should_reconstruct(
                    query,
                    mode=graph_mode,
                    passive_hit_count=len(hits),
                    required_hit_count=self._context_inject_count,
                    comparison_coverage=coverage,
                )
                or (
                    is_complex_query(query)
                    and (
                        not self._fts_hits_sufficient(hits)
                        or not all(coverage.values())
                    )
                )
            )
        )
        if graph_gate:
            try:
                graph_hits = await asyncio.to_thread(
                    self.store.search_graph,
                    scope,
                    query,
                    8,
                    include_global=self._enable_scope_fallback,
                    seed_memory_ids=[hit.entry.id for hit in hits[:4]],
                    max_hops=getattr(self, "_graph_max_hops", 2),
                    max_cues=4,
                    tags_per_cue=3,
                )
                if graph_mode == "smart" and is_complex_query(query):
                    graph_hits = await self._route_graph_hits(query, graph_hits)
                if graph_hits:
                    hits = self._merge_search_hits(hits, graph_hits)
                    mode = f"{mode}+graph"
                    coverage = comparison_coverage(objects, hits)
            except Exception as exc:
                logger.debug("图记忆检索降级: %s", exc)
        if objects:
            selected = []
            selected_ids = set()
            # 每个已覆盖对象至少保留一条对应命中，之后再按总分补齐。
            for obj in objects:
                candidates = [hit for hit in hits if object_is_covered(obj, [hit])]
                if candidates:
                    best = candidates[0]
                    if best.entry.id not in selected_ids:
                        selected.append(best)
                        selected_ids.add(best.entry.id)
            for hit in hits:
                if len(selected) >= self._context_inject_count:
                    break
                if hit.entry.id not in selected_ids:
                    selected.append(hit)
                    selected_ids.add(hit.entry.id)
            hits = selected
        elif hits and topic_terms(query):
            # 非对比问题要求单条候选完整覆盖当前主题实体。宽泛共同词命中不应
            # 进入上下文，否则高置信度的另一实体会先入为主地污染主模型判断。
            hits = [
                hit
                for hit in hits
                if getattr(hit, "retrieval_mode", "") == "graph"
                or entry_covers_topic(query, hit)
            ]

        return hits[: self._context_inject_count], mode, coverage

    @staticmethod
    def _build_memory_injection(hits) -> tuple[list[str], list]:
        """按条数、单条字符数和总字符数裁剪记忆注入。"""
        parts: list[str] = []
        injected_hits: list = []
        remaining = _MEMORY_INJECT_TOTAL_CHARS
        for hit in hits[:_MEMORY_INJECT_MAX_COUNT]:
            entry = hit.entry
            v_tag = "✅已验证" if entry.verified else f"⚠️置信度{entry.confidence:.0%}"
            prefix = f"【内部知识 #{entry.id} | {entry.topic} | {v_tag}】"
            item_limit = min(_MEMORY_INJECT_ITEM_CHARS, remaining)
            if item_limit <= len(prefix):
                break
            content = entry.content or ""
            if len(prefix) + len(content) > item_limit:
                keep = max(0, item_limit - len(prefix) - 1)
                content = content[:keep] + "…"
            item = prefix + content
            parts.append(item)
            injected_hits.append(hit)
            remaining -= len(item)
            if remaining <= 0:
                break
        return parts, injected_hits
