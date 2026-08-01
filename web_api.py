"""Dashboard 管理页面后端 API：WebApiMixin。

从 main.py 拆出的纯结构分层，方法实现逐行原样搬运，未做行为变更。
这些方法都不带 AstrBot 装饰器（web 路由是运行时 register_web_api 注册，
不依赖装饰器扫描），因此可以安全离开 main.py。
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astrbot.api.star import Context, StarTools

from .constants import (
    PLUGIN_NAME,
    _EXTERNAL_SEARCH_FIRST_RESULT_GRACE_SECONDS,
    _MEMORY_INJECT_MAX_COUNT,
)
from .embedder import Embedder
from .models import Scope, now_ts
from .plugin_logger import logger

try:
    from astrbot.api.web import error_response, file_response, json_response, request

    _WEB_AVAILABLE = True
except ImportError:  # AstrBot < v4.26 没有 Plugin Pages 支持
    _WEB_AVAILABLE = False
    error_response = file_response = json_response = request = None  # type: ignore


class WebApiMixin:
    """Dashboard 后端 API。由 ActiveLearnerPlugin 混入，依赖宿主的 store/config 等属性。"""

    def _register_web_apis(self, context: Context) -> None:
        """注册 Dashboard 页面所需的 web API 路由。"""
        context.register_web_api(
            f"/{PLUGIN_NAME}/stats", self._web_stats, ["GET"], "记忆库统计"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/scopes", self._web_scopes, ["GET"], "列出所有 scope"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memories",
            self._web_memories,
            ["GET"],
            "记忆列表（分页+搜索）",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/<entry_id>",
            self._web_memory_detail,
            ["GET"],
            "记忆详情",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/<entry_id>/versions",
            self._web_memory_versions,
            ["GET"],
            "版本历史",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/<entry_id>/forget",
            self._web_memory_forget,
            ["POST"],
            "软删除记忆",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/<entry_id>/verify",
            self._web_memory_verify,
            ["POST"],
            "触发验证",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/batch_verify",
            self._web_batch_verify,
            ["POST"],
            "批量验证",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/batch_enrich",
            self._web_batch_enrich,
            ["POST"],
            "批量补充信息",
        )
        # v1.1.11.0：关心领域主动学习（按钮触发，后台搜索+精炼+融合入库）
        context.register_web_api(
            f"/{PLUGIN_NAME}/priority_learn",
            self._web_priority_learn,
            ["POST"],
            "主动学习关心领域",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/priority_learn/status",
            self._web_priority_learn_status,
            ["GET"],
            "查询关心领域学习进度",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/export", self._web_export, ["GET"], "导出 JSON"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/import_text", self._web_import_text, ["POST"], "导入纯文本"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/import_md", self._web_import_md, ["POST"], "导入 Markdown"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/import_zip", self._web_import_zip, ["POST"], "批量导入 ZIP"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/import_pdf", self._web_import_pdf, ["POST"], "导入 PDF"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/import_docx", self._web_import_docx, ["POST"], "导入 DOCX"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/import_txt",
            self._web_import_txt,
            ["POST"],
            "导入 TXT（带分块）",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/providers",
            self._web_providers,
            ["GET"],
            "列出可用 LLM Provider",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings", self._web_get_settings, ["GET"], "获取插件设置"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings",
            self._web_save_settings,
            ["POST"],
            "保存插件设置",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/url_sources",
            self._web_url_sources,
            ["GET"],
            "列出 URL 知识来源",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/url_sources",
            self._web_add_url_source,
            ["POST"],
            "添加 URL 知识来源",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/url_sources/<source_id>/enabled",
            self._web_set_url_source_enabled,
            ["POST"],
            "启用或停用 URL 知识来源",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/url_sources/<source_id>/delete",
            self._web_delete_url_source,
            ["POST"],
            "删除 URL 知识来源",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/config_schema",
            self._web_config_schema,
            ["GET"],
            "获取配置 schema 与当前值",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/debug", self._web_debug, ["GET"], "诊断信息"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/builtin_kb/list",
            self._web_builtin_kb_list,
            ["GET"],
            "列出 AstrBot 内置知识库",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/builtin_kb/<kb_id>/documents",
            self._web_builtin_kb_documents,
            ["GET"],
            "列出 KB 内文档",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/builtin_kb/import",
            self._web_builtin_kb_import,
            ["POST"],
            "从内置 KB 批量导入",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/logs",
            self._web_logs,
            ["GET"],
            "获取插件日志",
        )

    async def _web_debug(self):
        """返回数据库、运行状态和当前实际配置。

        配置值统一从 ConfigManager 读取，不再读取启动时的旧 self.config。
        只返回安全的运行参数，provider_settings/API Key 等敏感字段不进入诊断响应。
        """
        embedder_available = False
        embedder_model = ""
        if self.embedder is not None:
            try:
                embedder_available = self.embedder.available
                embedder_model = self.embedder.model_name
            except Exception:
                pass

        cfg = self.config_manager.all()
        configured_provider = str(cfg.get("llm_provider_id", "") or "").strip()
        effective_provider = (
            configured_provider
            or self._cfg_llm_provider_id
            or self._resolve_default_provider_id()
        )
        config_snapshot = {
            "llm_provider_id": configured_provider,
            "effective_provider_id": effective_provider,
            "max_entries": int(cfg.get("max_entries", 500)),
            "min_confidence": float(cfg.get("min_confidence", 0.3)),
            "enable_active_learn_hint": bool(cfg.get("enable_active_learn_hint", True)),
            "learn_weight": float(cfg.get("learn_weight", 0.7)),
            "search_top_k": int(cfg.get("search_top_k", 5)),
            "default_confidence": float(cfg.get("default_confidence", 0.6)),
            "chunk_size": int(cfg.get("chunk_size", 500)),
            "chunk_overlap": int(cfg.get("chunk_overlap", 50)),
            "embedding_enabled": bool(cfg.get("embedding_enabled", True)),
            "hybrid_search_weight": str(cfg.get("hybrid_search_weight", "0.4,0.6")),
            "verifier_search_source": str(cfg.get("verifier_search_source", "auto")),
            "enable_web_search": bool(cfg.get("enable_web_search", True)),
            "enable_bilibili": bool(cfg.get("enable_bilibili", False)),
            "priority_topics": str(cfg.get("priority_topics", "")),
            "knowledge_domain_scope": str(cfg.get("knowledge_domain_scope", "")),
            "enable_cross_domain": bool(cfg.get("enable_cross_domain", True)),
            "llm_max_concurrency": int(cfg.get("llm_max_concurrency", 1)),
        }
        runtime_snapshot = {
            "learn_weight": self._learn_weight,
            "search_top_k": self._search_top_k,
            "enable_bilibili": self._enable_bilibili,
            "default_confidence": self._default_confidence,
            "chunk_size": self._chunk_size,
            "chunk_overlap": self._chunk_overlap,
            "enable_active_learn_hint": self._enable_active_learn_hint,
            "enable_web_search": self._enable_web_search,
            "priority_topics": list(self._priority_topics),
            "knowledge_domain_scope": list(self._knowledge_domain_scope),
            "enable_cross_domain": self._enable_cross_domain,
            "url_sources_enabled": self.url_sources.enabled_count,
        }
        return json_response(
            {
                "db_path": str(self._db_path),
                "schema_version": self.store._schema_version,
                "total_memories": self.store.count_all(),
                "scopes": self.store.list_scopes(),
                "embedder_available": embedder_available,
                "embedder_model": embedder_model,
                "priority_topics": self._priority_topics,
                "priority_boost": round(self._priority_boost, 2),
                "tools_registered": [t.name for t in self._tools],
                "token_stats": self.llm_service.get_token_stats(),
                "config": config_snapshot,
                "runtime": runtime_snapshot,
            }
        )

    @staticmethod
    def _scope_from_query():
        """从 query 参数构造 Scope，缺失时返回 None（表示全库视图）。"""
        st = request.query.get("scope_type")
        sid = request.query.get("scope_id")
        if not st or not sid:
            return None
        return Scope(type=st, id=sid)

    async def _web_stats(self):
        scope = self._scope_from_query()
        if scope is None:
            data = self.store.global_stats()
        else:
            data = self.store.stats(scope)
        return json_response(data)

    async def _web_scopes(self):
        return json_response({"scopes": self.store.list_scopes()})

    async def _web_memories(self):
        page = request.query.get("page", 1, type=int)
        per_page = request.query.get("per_page", 20, type=int)
        per_page = max(1, min(per_page, 100))
        keyword = request.query.get("keyword") or None
        scope = self._scope_from_query()
        if scope is None:
            entries, total, total_pages = self.store.list_all_memories(
                page=page, per_page=per_page, keyword=keyword
            )
        else:
            entries, total, total_pages = self.store.list_memories(
                scope, page=page, per_page=per_page
            )
        return json_response(
            {
                "items": [e.to_dict() for e in entries],
                "total": total,
                "total_pages": total_pages,
                "page": page,
                "per_page": per_page,
            }
        )

    async def _web_memory_detail(self, entry_id: str):
        entry = self.store.get_entry_by_id(entry_id)
        if entry is None:
            return error_response("memory not found", status_code=404)
        return json_response(entry.to_dict())

    async def _web_memory_versions(self, entry_id: str):
        versions = self.store.list_versions(entry_id)
        return json_response({"items": [v.to_dict() for v in versions]})

    async def _web_memory_forget(self, entry_id: str):
        entry = self.store.get_entry_by_id(entry_id)
        if entry is None:
            return error_response("memory not found", status_code=404)
        scope = Scope(type=entry.scope_type, id=entry.scope_id)
        ok, _ = self.store.forget(scope, entry.topic)
        if not ok:
            return error_response("forget failed", status_code=500)
        return json_response({"ok": True})

    async def _web_memory_verify(self, entry_id: str):
        entry = self.store.get_entry_by_id(entry_id)
        if entry is None:
            return error_response("memory not found", status_code=404)
        payload = await request.json(default={}) or {}
        provider_id = (payload.get("provider_id") or "").strip()
        provider_source = "frontend"
        if not provider_id:
            provider_id = await self._resolve_plugin_provider_id()
            provider_source = "fallback"
        logger.info(
            f"验证 memory={entry_id} topic={entry.topic!r} provider={provider_id!r} "
            f"source={provider_source}"
        )
        if not provider_id:
            # 诊断信息：列出当前可用的解析路径状态
            settings_pid = self.config_manager.get("llm_provider_id", "") or ""
            cfg_pid = self._cfg_llm_provider_id or ""
            pm = getattr(self.context, "provider_manager", None)
            pm_providers = []
            if pm is not None:
                for p in getattr(pm, "providers", None) or []:
                    pm_providers.append(
                        str(getattr(p, "id", "") or getattr(p, "name", ""))
                    )
            # cmd_config 诊断
            try:
                plugin_data_dir = str(StarTools.get_data_dir())
            except Exception:
                plugin_data_dir = "?"
            cmd_config_path = self._find_cmd_config()
            cfg_default_pid, cfg_providers = self._get_providers_from_config()
            logger.warning(
                f"provider 解析失败: settings_pid={settings_pid!r}, cfg_pid={cfg_pid!r}, "
                f"pm_providers={pm_providers}, plugin_data_dir={plugin_data_dir}, "
                f"cmd_config={str(cmd_config_path) if cmd_config_path else 'NOT FOUND'}, "
                f"cfg_default_pid={cfg_default_pid!r}, cfg_providers={len(cfg_providers)}个"
            )
            return error_response(
                "无法确定 LLM provider。请在插件配置中设置 llm_provider_id，"
                "或在 Dashboard 设置页选择一个模型。",
                status_code=400,
            )
        try:
            result = await self.verifier.run(entry, provider_id)
        except Exception as e:
            return error_response(f"验证失败: {e}", status_code=500)
        return json_response(
            {
                "verdict": result.verdict,
                "confidence": result.confidence,
                "content": result.content,
                "reasoning": result.reasoning,
                "sources_count": result.sources_count,
                "sources_consistent": result.sources_consistent,
                "text": result.to_text(),
                "debug_info": result.debug_info,
            }
        )

    async def _web_batch_verify(self):
        """批量验证：后端并发执行验证，减少前端请求次数。

        请求体：{"ids": [...], "provider_id": "..."}
        响应：{"ok": N, "fail": N, "total": N, "results": [{id, status, ...}, ...]}
        """
        try:
            payload = await request.json(default={}) or {}
            ids = payload.get("ids", [])
            if not ids or not isinstance(ids, list):
                return error_response("请提供要验证的记忆 ID 列表", status_code=400)

            provider_id = (payload.get("provider_id") or "").strip()
            if not provider_id:
                provider_id = await self._resolve_plugin_provider_id()
            if not provider_id:
                return error_response(
                    "无法确定 LLM provider。请在 Dashboard 设置页选择一个模型。",
                    status_code=400,
                )

            CONCURRENCY = 5
            total = len(ids)
            results: list[dict] = []
            ok = 0
            fail = 0
            next_index = 0
            lock = asyncio.Lock()

            async def worker():
                nonlocal ok, fail, next_index
                while True:
                    async with lock:
                        if next_index >= total:
                            return
                        idx = next_index
                        next_index += 1
                    entry_id = ids[idx]
                    try:
                        entry = self.store.get_entry_by_id(entry_id)
                        if entry is None:
                            async with lock:
                                fail += 1
                                results.append(
                                    {
                                        "id": entry_id,
                                        "status": "skipped",
                                        "reason": "记忆不存在",
                                    }
                                )
                            continue
                        result = await self.verifier.run(entry, provider_id)
                        async with lock:
                            ok += 1
                            results.append(
                                {
                                    "id": entry_id,
                                    "status": "verified",
                                    "verdict": result.verdict,
                                    "confidence": result.confidence,
                                }
                            )
                    except Exception as e:
                        logger.error(
                            f"批量验证条目失败 (id={entry_id}): {e}", exc_info=True
                        )
                        async with lock:
                            fail += 1
                            results.append(
                                {"id": entry_id, "status": "error", "reason": str(e)}
                            )

            workers = [worker() for _ in range(min(CONCURRENCY, total))]
            await asyncio.gather(*workers)

            return json_response(
                {
                    "ok": ok,
                    "fail": fail,
                    "total": total,
                    "results": results,
                }
            )
        except Exception as e:
            logger.error(f"批量验证失败: {e}", exc_info=True)
            return error_response(f"批量验证失败: {e}", status_code=500)

    async def _web_batch_enrich(self):
        """批量补充信息：为已选记忆搜索网络，提取新信息并更新条目。"""
        try:
            payload = await request.json(default={}) or {}
            ids = payload.get("ids", [])
            if not ids or not isinstance(ids, list):
                return error_response("请提供要补充的记忆 ID 列表", status_code=400)

            provider_id = (payload.get("provider_id") or "").strip()
            if not provider_id:
                provider_id = await self._resolve_plugin_provider_id()
            if not provider_id:
                return error_response(
                    "无法确定 LLM provider。请在 Dashboard 设置页选择一个模型。",
                    status_code=400,
                )

            # v1.2.0.0：补充信息依赖联网搜索
            if not self._enable_web_search:
                return error_response(
                    "联网搜索已关闭，无法执行补充信息。请在设置中开启「启用联网搜索」。",
                    status_code=400,
                )

            CONCURRENCY = 3
            total = len(ids)
            results: list[dict] = []
            ok = 0
            fail = 0
            next_index = 0
            lock = asyncio.Lock()

            async def worker():
                nonlocal ok, fail, next_index
                while True:
                    async with lock:
                        if next_index >= total:
                            return
                        idx = next_index
                        next_index += 1
                    entry_id = ids[idx]
                    try:
                        entry = self.store.get_entry_by_id(entry_id)
                        if entry is None:
                            async with lock:
                                fail += 1
                                results.append(
                                    {
                                        "id": entry_id,
                                        "status": "skipped",
                                        "reason": "记忆不存在",
                                    }
                                )
                            continue

                        # 搜索网络（统一分源超时、总 deadline 与限流）
                        search_results = await self._search_external_sources(
                            entry.topic, web_limit=5, bili_limit=3
                        )
                        if not search_results:
                            async with lock:
                                ok += 1
                                results.append(
                                    {
                                        "id": entry_id,
                                        "status": "no_new_info",
                                        "reason": "搜索无结果",
                                    }
                                )
                            continue

                        # 整理搜索结果
                        snippets = []
                        sources = []
                        for r in search_results:
                            snippets.append(
                                f"标题: {r.get('title', '')}\n摘要: {r.get('snippet', '')}"
                            )
                            sources.append(f"{r.get('title', '')} ({r.get('url', '')})")
                        search_text = "\n---\n".join(snippets)

                        # LLM 提取新信息
                        prompt = (
                            "你是知识库管理员，判断搜索结果中是否有「新增信息」。\n\n"
                            f"--- 已有知识 ---\n"
                            f"主题：{entry.topic}\n"
                            f"内容：{entry.content}\n"
                            f"关键词：{'、'.join(entry.keywords or [])}\n\n"
                            f"--- 搜索结果 ---\n{search_text[:3000]}\n\n"
                            "请判断：\n"
                            "1. 搜索结果中是否有已有知识未涵盖的**新信息**（新的属性、细节、别名、关联实体等）\n"
                            "2. 如果有，提取新信息并生成融合后的内容\n"
                            "3. 如果搜索结果只是重复已有知识的内容，则判定为无新信息\n\n"
                            "严格按以下格式输出（每行一个字段）：\n"
                            "HAS_NEW: yes / no\n"
                            "MERGED_CONTENT: <融合后的完整内容，≤500字，仅在 HAS_NEW=yes 时需要>\n"
                            "NEW_KEYWORDS: <新增的关键词，逗号分隔，仅在 HAS_NEW=yes 时需要>\n"
                            "REASON: <判断理由，≤30字>\n"
                        )

                        reply = await self.llm_service.generate(
                            prompt=prompt,
                            provider_id=provider_id,
                        )
                        if not reply or not reply.strip():
                            async with lock:
                                ok += 1
                                results.append(
                                    {
                                        "id": entry_id,
                                        "status": "no_new_info",
                                        "reason": "LLM 无返回",
                                    }
                                )
                            continue

                        import re as _re

                        hm = _re.search(r"HAS_NEW:\s*(\S+)", reply)
                        has_new = (
                            hm is not None and hm.group(1).strip().lower() == "yes"
                        )

                        if not has_new:
                            async with lock:
                                ok += 1
                                results.append(
                                    {
                                        "id": entry_id,
                                        "status": "no_new_info",
                                        "reason": "无新信息",
                                    }
                                )
                            continue

                        merged = _re.search(
                            r"MERGED_CONTENT:\s*(.+?)(?=\n[A-Z_]+:|\Z)",
                            reply,
                            _re.DOTALL,
                        )
                        merged_content = merged.group(1).strip() if merged else ""

                        nk = _re.search(
                            r"NEW_KEYWORDS:\s*(.+?)(?=\n[A-Z_]+:|\Z)", reply
                        )
                        new_keywords_str = nk.group(1).strip() if nk else ""

                        reason_m = _re.search(
                            r"REASON:\s*(.+?)(?=\n[A-Z_]+:|\Z)", reply
                        )
                        enrich_reason = reason_m.group(1).strip() if reason_m else ""

                        if not merged_content:
                            async with lock:
                                ok += 1
                                results.append(
                                    {
                                        "id": entry_id,
                                        "status": "no_new_info",
                                        "reason": "LLM 未返回有效内容",
                                    }
                                )
                            continue

                        # 融合关键词和来源
                        all_keywords = list(entry.keywords or [])
                        extra = []
                        if new_keywords_str:
                            extra = [
                                k.strip()
                                for k in _re.split(r"[,，、\s]+", new_keywords_str)
                                if k.strip() and len(k.strip()) >= 2
                            ]
                            all_keywords = list(dict.fromkeys(all_keywords + extra))

                        all_sources = list(entry.sources_detail or [])
                        all_sources.extend(s for s in sources if s not in all_sources)

                        # 更新记忆
                        await asyncio.to_thread(
                            self.store.add_or_update,
                            Scope(type=entry.scope_type, id=entry.scope_id),
                            entry.topic,
                            merged_content,
                            keywords=all_keywords,
                            source=f"{entry.source or ''} + 补充搜索",
                            sources_detail=all_sources,
                            confidence=min(1.0, entry.confidence + 0.05),
                            origin=entry.origin or "",
                        )
                        async with lock:
                            ok += 1
                            results.append(
                                {
                                    "id": entry_id,
                                    "status": "enriched",
                                    "reason": enrich_reason,
                                    "new_keywords": extra,
                                }
                            )
                    except Exception as e:
                        logger.error(
                            f"补充信息条目失败 (id={entry_id}): {e}", exc_info=True
                        )
                        async with lock:
                            fail += 1
                            results.append(
                                {"id": entry_id, "status": "error", "reason": str(e)}
                            )

            workers = [worker() for _ in range(min(CONCURRENCY, total))]
            await asyncio.gather(*workers)

            return json_response(
                {
                    "ok": ok,
                    "fail": fail,
                    "total": total,
                    "results": results,
                }
            )
        except Exception as e:
            logger.error(f"批量补充信息失败: {e}", exc_info=True)
            return error_response(f"批量补充信息失败: {e}", status_code=500)

    async def _web_priority_learn(self):
        """启动关心领域主动学习任务（后台运行）。

        流程：
        1. LLM 为每个 priority_topic 生成 N 个子查询（N = limit / 主题数）
        2. 对每个子查询：搜索网络 → 精炼 → 融合检查 → 存入记忆
        3. 达到 limit 上限或所有子查询处理完则结束
        """
        try:
            # v1.2.0.0：主动学习依赖联网搜索
            if not self._enable_web_search:
                return error_response(
                    "联网搜索已关闭，无法执行主动学习。请在设置中开启「启用联网搜索」。",
                    status_code=400,
                )

            payload = await request.json(default={}) or {}
            provider_id = (payload.get("provider_id") or "").strip()
            if not provider_id:
                provider_id = await self._resolve_plugin_provider_id()
            if not provider_id:
                return error_response(
                    "无法确定 LLM provider。请在 Dashboard 设置页选择一个模型。",
                    status_code=400,
                )

            topics_raw = self.config_manager.get("priority_topics") or ""
            topics = [t.strip() for t in topics_raw.split(",") if t.strip()]
            if not topics:
                return error_response(
                    "未设置关心领域。请在「📋 配置」中设置 priority_topics。",
                    status_code=400,
                )

            try:
                limit = int(self.config_manager.get("auto_learn_topic_limit") or 100)
            except (TypeError, ValueError):
                limit = 100
            limit = max(1, min(500, limit))

            if self._priority_learn_task and self._priority_learn_task.get("running"):
                return error_response(
                    "已有主动学习任务在运行，请等待完成或重启插件。",
                    status_code=400,
                )

            scope_type = (payload.get("scope_type") or "global").strip()
            scope_id = (payload.get("scope_id") or "global").strip()
            scope = Scope(type=scope_type, id=scope_id)

            self._priority_learn_task = {
                "running": True,
                "cancelled": False,
                "done": 0,
                "total": limit,
                "current_topic": "初始化中…",
                "topics": topics,
                "limit": limit,
                "errors": [],
                "started_at": now_ts(),
                "finished_at": None,
            }

            self._create_background_task(
                self._priority_learn_worker(topics, limit, scope, provider_id),
                name="active-learner-priority-learn",
            )
            logger.info(
                f"🎯 关心领域主动学习已启动：topics={topics}, limit={limit}, scope={scope}, provider={provider_id}"
            )
            return json_response(
                {
                    "status": "started",
                    "limit": limit,
                    "topics": topics,
                    "scope": {"type": scope.type, "id": scope.id},
                }
            )
        except Exception as e:
            logger.error(f"启动关心领域学习失败: {e}", exc_info=True)
            return error_response(f"启动失败: {e}", status_code=500)

    async def _web_priority_learn_status(self):
        """查询关心领域主动学习任务进度。"""
        task = self._priority_learn_task
        if not task:
            return json_response(
                {
                    "running": False,
                    "done": 0,
                    "total": 0,
                    "current_topic": "",
                    "topics": [],
                    "errors": [],
                }
            )
        return json_response(
            {
                "running": task["running"],
                "done": task["done"],
                "total": task["total"],
                "current_topic": task["current_topic"],
                "topics": task["topics"],
                "limit": task["limit"],
                "errors": task["errors"][-10:],
                "started_at": task.get("started_at"),
                "finished_at": task.get("finished_at"),
            }
        )

    async def _priority_learn_worker(
        self, topics: list[str], limit: int, scope: Scope, provider_id: str
    ) -> None:
        """关心领域主动学习后台任务。

        对每个 topic：
        1. LLM 生成子查询列表（均分 limit 配额）
        2. 并发处理子查询（信号量控制并发度），提升整体速度
        """
        task = self._priority_learn_task
        if task is None:
            return

        CONCURRENCY = 3  # 子查询并发数
        per_topic = max(1, limit // len(topics))
        sem = asyncio.Semaphore(CONCURRENCY)

        try:
            for topic in topics:
                if task["cancelled"] or task["done"] >= limit:
                    break

                task["current_topic"] = f"{topic}（生成子查询…）"
                # 1. LLM 生成子查询
                try:
                    subqueries = await self._generate_priority_subqueries(
                        topic, per_topic, provider_id
                    )
                except Exception as e:
                    logger.warning(f"生成子查询失败「{topic}」: {e}")
                    task["errors"].append(f"{topic}: 生成子查询失败 - {e}")
                    continue

                if not subqueries:
                    subqueries = [topic]

                # 2. 并发处理子查询（信号量控制并发度）
                async def _run_one(sq: str):
                    if task["cancelled"] or task["done"] >= limit:
                        return
                    async with sem:
                        if task["cancelled"] or task["done"] >= limit:
                            return
                        try:
                            await self._priority_learn_one(sq, scope, provider_id)
                            task["done"] += 1
                            logger.info(
                                f"🎯 主动学习 [{task['done']}/{limit}] {topic} → {sq}"
                            )
                        except Exception as e:
                            logger.warning(f"主动学习子查询失败「{sq}」: {e}")
                            task["errors"].append(f"{sq}: {e}")

                task["current_topic"] = (
                    f"{topic}（并发学习 {len(subqueries)} 个子主题…）"
                )
                await asyncio.gather(*[_run_one(sq) for sq in subqueries])

            task["current_topic"] = "已完成" if not task["cancelled"] else "已取消"
            logger.info(
                f"🎯 关心领域主动学习结束：done={task['done']}/{limit}, "
                f"errors={len(task['errors'])}"
            )
        except Exception as e:
            logger.error(f"关心领域主动学习任务异常: {e}", exc_info=True)
            task["current_topic"] = f"任务异常：{e}"
            task["errors"].append(f"task: {e}")
        finally:
            task["running"] = False
            task["finished_at"] = now_ts()

    async def _generate_priority_subqueries(
        self, topic: str, n: int, provider_id: str
    ) -> list[str]:
        """让 LLM 为某个关心领域生成 N 个不同的子查询关键词。"""
        if n <= 0:
            return [topic]
        # 上取整，至少 1 个，最多 50 个（避免一次生成太多 LLM 截断）
        n = max(1, min(50, n))
        prompt = (
            f"你是知识库管理员。针对主题「{topic}」，生成 {n} 个用于网络搜索的子主题关键词，"
            f"用于学习这个领域的各个方面（基础概念、原理、应用、历史、最新进展等）。\n\n"
            "要求：\n"
            "1. 每个关键词都是独立的搜索 query（不要重复）\n"
            "2. 涵盖该领域的不同侧面\n"
            "3. 关键词要具体、可搜索\n\n"
            "严格按以下格式输出，每行一个：\n"
            "1. xxx\n2. xxx\n3. xxx\n..."
        )
        reply = await self.llm_service.generate(prompt=prompt, provider_id=provider_id)
        if not reply or not reply.strip():
            return [topic]

        # 解析数字列表
        import re as _re

        lines = reply.strip().splitlines()
        queries: list[str] = []
        for line in lines:
            # 去除前缀数字 + 点 + 空格
            m = _re.match(r"^\s*\d+[\.\)、\s]+(.+)$", line)
            q = m.group(1).strip() if m else line.strip()
            if q and q not in queries and len(q) <= 100:
                queries.append(q)
            if len(queries) >= n:
                break
        if not queries:
            queries = [topic]
        return queries

    async def _priority_learn_one(
        self, query: str, scope: Scope, provider_id: str
    ) -> None:
        """对单个子查询执行：搜索 → 精炼 → 融合检查 → 存储。"""
        # 1. 搜索（分源超时 + 总 deadline + 全局限流）
        search_results = await self._search_external_sources(
            query, web_limit=5, bili_limit=3
        )
        if not search_results:
            logger.debug(f"主动学习「{query}」搜索无结果")
            return

        # 2. 整理搜索结果
        snippets = []
        sources = []
        for r in search_results:
            snippets.append(f"标题: {r.get('title', '')}\n摘要: {r.get('snippet', '')}")
            sources.append(f"{r.get('title', '')} ({r.get('url', '')})")
        search_text = "\n---\n".join(snippets)

        # 3. LLM 精炼
        topic = query
        refine_result = await self.refiner.refine_search_results(
            topic=topic,
            search_text=search_text,
            sources=sources,
            provider_id=provider_id,
        )
        summary = refine_result.summary or search_text[:500]

        # 4. 关键词
        if refine_result.refined and refine_result.keywords:
            keywords = refine_result.keywords
        else:
            keywords = [query]

        # 5. 置信度
        if refine_result.refined:
            confidence = refine_result.confidence
        else:
            confidence = min(0.7, 0.3 + len(search_results) * 0.07)
            if len(search_results) >= 3:
                confidence = min(0.85, confidence + 0.1)

        # 6. 融合检查
        if refine_result.refined:
            try:
                existing = self.store.search(scope, topic, top_k=3)
                if existing:
                    top_match = existing[0]
                    if top_match.entry.topic.lower() != topic.lower():
                        merge_decision = await self.refiner.check_merge(
                            new_topic=topic,
                            new_summary=summary,
                            new_keywords=keywords,
                            existing_topic=top_match.entry.topic,
                            existing_summary=top_match.entry.content,
                            existing_keywords=top_match.entry.keywords or [],
                            provider_id=provider_id,
                        )
                        if merge_decision.should_merge:
                            logger.info(
                                f"🧬 主动学习融合：「{topic}」→ 融合到「{merge_decision.target_topic}」"
                            )
                            topic = merge_decision.target_topic
                            existing_kws = top_match.entry.keywords or []
                            keywords = list(dict.fromkeys(existing_kws + keywords))
            except Exception as e:
                logger.debug(f"主动学习融合检查失败: {e}")

        # 7. 存入记忆
        source_tag = f"主动学习 ({len(sources)}个来源)"
        if refine_result.refined:
            source_tag += "+精炼"
        await asyncio.to_thread(
            self.store.add_or_update,
            scope,
            topic,
            summary,
            keywords=keywords,
            source=source_tag,
            sources_detail=sources,
            confidence=confidence,
            origin="priority_learn",
        )

    async def _web_export(self):
        scope = self._scope_from_query()
        if scope is None:
            entries, _, _ = self.store.list_all_memories(page=1, per_page=10**9)
            data = [e.to_dict() for e in entries]
            suffix = "all"
        else:
            data = self.store.export_scope(scope)
            suffix = f"{scope.type}_{scope.id}"
        export_path = StarTools.get_data_dir() / f"memory_export_{suffix}.json"
        try:
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return error_response(f"导出失败: {e}", status_code=500)
        return file_response(
            export_path,
            filename=f"memory_export_{suffix}.json",
            content_type="application/json",
        )

    def _find_cmd_config(self):
        """尝试找到 AstrBot 的 cmd_config.json。"""
        try:
            plugin_data = StarTools.get_data_dir()
            for parent in [plugin_data] + list(plugin_data.parents):
                for candidate in (
                    parent / "cmd_config.json",
                    parent / "data" / "cmd_config.json",
                    parent / "config" / "cmd_config.json",
                ):
                    if candidate.exists():
                        return candidate
                # 也尝试 abconf_ 前缀的多配置文件
                if parent.is_dir():
                    for ab in parent.glob("abconf_*.json"):
                        return ab
                    data_sub = parent / "data"
                    if data_sub.is_dir():
                        for ab in data_sub.glob("abconf_*.json"):
                            return ab
        except Exception as e:
            logger.debug(f"_find_cmd_config 异常: {e}")
        return None

    def _get_providers_from_config(self) -> tuple[str, list[dict]]:
        """读取 provider 列表和 default_provider_id。

        优先从 self.config（AstrBot 传入的 cfg，包含全局配置）读取，
        兜底从 cmd_config.json 文件读取。
        """
        # 1. 优先从 self.config（AstrBot 传入的 cfg）读取
        try:
            cfg = self.config or {}
            providers_raw = cfg.get("provider", []) or []
            if providers_raw:
                providers = [
                    {
                        "id": str(p.get("id", "") or ""),
                        "type": str(p.get("type", "") or ""),
                        "model": str(p.get("model", "") or ""),
                        "enable": bool(p.get("enable", True)),
                    }
                    for p in providers_raw
                    if p.get("id")
                ]
                default_pid = str(
                    (cfg.get("provider_settings") or {}).get("default_provider_id", "")
                    or ""
                )
                if providers or default_pid:
                    return default_pid, providers
        except Exception as e:
            logger.debug(f"从 self.config 读取 provider 失败: {e}")

        # 2. 兜底：从 cmd_config.json 文件读取
        config_path = self._find_cmd_config()
        if not config_path:
            return "", []
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            providers_raw = cfg.get("provider", []) or []
            providers = [
                {
                    "id": str(p.get("id", "") or ""),
                    "type": str(p.get("type", "") or ""),
                    "model": str(p.get("model", "") or ""),
                    "enable": bool(p.get("enable", True)),
                }
                for p in providers_raw
                if p.get("id")
            ]
            default_pid = str(
                (cfg.get("provider_settings") or {}).get("default_provider_id", "")
                or ""
            )
            return default_pid, providers
        except Exception as e:
            logger.debug(f"读取 cmd_config.json 失败: {e}")
            return "", []

    def _resolve_default_provider_id(self) -> str:
        """尝试从 context 拿默认 provider id，拿不到返回空串。"""
        for method_name in (
            "get_using_provider_id",
            "get_using_provider",
            "get_default_provider_id",
        ):
            method = getattr(self.context, method_name, None)
            if not callable(method) or asyncio.iscoroutinefunction(method):
                continue
            try:
                result = method()
            except Exception:
                continue
            if isinstance(result, str) and result:
                return result
            pid = getattr(result, "id", None) or getattr(result, "name", None)
            if pid:
                return str(pid)
        # 兜底 1：从 provider_manager.providers 取第一个
        pm = getattr(self.context, "provider_manager", None)
        if pm is not None:
            providers = getattr(pm, "providers", None) or []
            for p in providers:
                pid = getattr(p, "id", None) or getattr(p, "name", None)
                if pid:
                    return str(pid)
        # 兜底 2：从 cmd_config.json 读取 default_provider_id
        default_pid, providers = self._get_providers_from_config()
        if default_pid:
            return default_pid
        for p in providers:
            if p.get("enable", True):
                return p["id"]
        # 兜底 3：插件配置中的 llm_provider_id
        if self._cfg_llm_provider_id:
            return self._cfg_llm_provider_id
        return ""

    def _provider_exists(self, provider_id: str) -> bool:
        """校验 provider_id 是否在 provider_manager 中存在（防止选了已删除的 provider）。"""
        if not provider_id:
            return False
        pm = getattr(self.context, "provider_manager", None)
        if pm is not None:
            providers = getattr(pm, "providers", None) or []
            for p in providers:
                pid = getattr(p, "id", None) or getattr(p, "name", None)
                if pid and str(pid) == str(provider_id):
                    return True
            # provider_manager 不为空但没找到 → 再用 cmd_config 兜底
        # 从 cmd_config.json 校验
        _, cfg_providers = self._get_providers_from_config()
        if cfg_providers:
            for p in cfg_providers:
                if p["id"] == str(provider_id):
                    return True
            return False
        # 都拿不到 → 放行
        return True

    async def _resolve_plugin_provider_id(self, umo: str = "") -> str:
        """4 层 fallback 解析插件使用的 LLM Provider ID。

        1. ConfigManager 中的 llm_provider_id（Dashboard 设置，最高优先级）
        2. self._cfg_llm_provider_id（_conf_schema.json 中的字段）
        3. context.get_current_chat_provider_id(umo=...) （事件 scope 默认）
        4. self._resolve_default_provider_id() （同步兜底）

        每个候选都先经 _provider_exists 校验，避免选了已删除的 provider。
        """
        # 1. Dashboard 设置
        pid = self.config_manager.get("llm_provider_id", "") or ""
        if pid:
            if self._provider_exists(pid):
                logger.info(f"provider 解析 [1/4 Dashboard]: {pid!r}")
                return pid
            logger.warning(f"provider 解析 [1/4 Dashboard] 命中但校验失败: {pid!r}")

        # 2. schema 字段
        if self._cfg_llm_provider_id:
            if self._provider_exists(self._cfg_llm_provider_id):
                logger.info(
                    f"provider 解析 [2/4 Schema]: {self._cfg_llm_provider_id!r}"
                )
                return self._cfg_llm_provider_id
            logger.warning(
                f"provider 解析 [2/4 Schema] 命中但校验失败: {self._cfg_llm_provider_id!r}"
            )

        # 3. 事件 scope 默认（async），尝试调用 get_current_chat_provider_id
        method = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(method):
            try:
                pid = await method(umo=umo) if umo else await method()
                if pid:
                    if self._provider_exists(pid):
                        logger.info(
                            f"provider 解析 [3/4 当前对话默认]: {pid!r} (umo={umo!r})"
                        )
                        return pid
                    logger.warning(
                        f"provider 解析 [3/4 当前对话默认] 命中但校验失败: {pid!r}"
                    )
            except Exception as e:
                logger.debug(f"provider 解析 [3/4 当前对话默认] 调用异常: {e}")

        # 4. 同步兜底
        fallback = self._resolve_default_provider_id()
        configured = self.config_manager.get("llm_provider_id", "")
        logger.info(
            f"provider 解析 [4/4 兜底]: {fallback!r} "
            f"(settings_pid={configured!r}, cfg_pid={self._cfg_llm_provider_id!r})"
        )
        return fallback

    # ---------- 设置与 Provider API ----------

    @staticmethod
    def _normalize_provider(provider: Any) -> dict[str, str] | None:
        """兼容 AstrBot 不同版本的 Provider 实例与配置字典。"""
        if isinstance(provider, dict):
            provider_id = str(provider.get("id") or "").strip()
            if not provider_id:
                return None
            return {
                "id": provider_id,
                "name": str(provider.get("name") or provider_id).strip(),
                "type": str(
                    provider.get("provider_type") or provider.get("type") or ""
                ).strip(),
                "model": str(provider.get("model") or "").strip(),
            }

        meta = None
        meta_fn = getattr(provider, "meta", None)
        if callable(meta_fn):
            try:
                meta = meta_fn()
            except Exception:
                meta = None
        provider_config = getattr(provider, "provider_config", None)
        config = provider_config if isinstance(provider_config, dict) else {}
        provider_id = str(
            getattr(meta, "id", "")
            or config.get("id")
            or getattr(provider, "id", "")
            or getattr(provider, "name", "")
            or ""
        ).strip()
        if not provider_id:
            return None
        return {
            "id": provider_id,
            "name": str(
                getattr(meta, "name", "")
                or config.get("name")
                or getattr(provider, "name", "")
                or provider_id
            ).strip(),
            "type": str(
                getattr(meta, "provider_type", "")
                or getattr(meta, "type", "")
                or config.get("provider_type")
                or config.get("type")
                or getattr(provider, "type", "")
                or ""
            ).strip(),
            "model": str(
                getattr(meta, "model", "")
                or config.get("model")
                or getattr(provider, "model", "")
                or ""
            ).strip(),
        }

    def _list_provider_candidates(self) -> list[dict[str, str]]:
        candidates: list[Any] = []
        getter = getattr(self.context, "get_all_providers", None)
        if callable(getter):
            try:
                candidates.extend(list(getter() or []))
            except Exception as exc:
                logger.debug(f"读取 Provider 列表失败: {exc}")
        pm = getattr(self.context, "provider_manager", None)
        if pm is not None:
            for attr in ("providers", "provider_insts"):
                candidates.extend(list(getattr(pm, attr, None) or []))
            inst_map = getattr(pm, "inst_map", None)
            if isinstance(inst_map, dict):
                candidates.extend(inst_map.values())
            configs = getattr(pm, "providers_config", None)
            if isinstance(configs, list):
                candidates.extend(configs)

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            item = self._normalize_provider(candidate)
            if item is None or item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
        if not items:
            _, configured = self._get_providers_from_config()
            for candidate in configured:
                item = self._normalize_provider(candidate)
                if item is not None and item["id"] not in seen:
                    seen.add(item["id"])
                    items.append(item)
        return items

    async def _web_providers(self):
        """列出 Provider，并区分用户显式配置与实际生效值。"""
        configured = str(self.config_manager.get("llm_provider_id", "") or "").strip()
        effective = (
            configured
            or self._cfg_llm_provider_id
            or self._resolve_default_provider_id()
        )
        return json_response(
            {
                "providers": self._list_provider_candidates(),
                "configured": configured,
                "effective": effective,
                "current": effective,
            }
        )

    async def _web_get_settings(self):
        """返回当前插件设置（含默认值填充）。

        使用 all() 而非 overlay_all()，确保 AstrBot 插件配置页（_conf_schema.json）
        中设置的 llm_provider_id 等字段也能被前端读到。
        """
        data = self.config_manager.all()
        configured = str(data.get("llm_provider_id", "") or "").strip()
        effective = (
            configured
            or self._cfg_llm_provider_id
            or self._resolve_default_provider_id()
        )
        return json_response(
            {
                "llm_provider_id": configured,
                "effective_provider_id": effective,
                "refine_on_search": bool(data.get("refine_on_search", True)),
                "refine_on_import": bool(data.get("refine_on_import", True)),
                "refine_on_verify": bool(data.get("refine_on_verify", True)),
                "enable_active_learn_hint": bool(
                    data.get("enable_active_learn_hint", True)
                ),
                "learn_weight": float(data.get("learn_weight", 0.7)),
                "admin_ids": str(data.get("admin_ids", "")),
                "search_top_k": int(data.get("search_top_k", 5)),
                "default_confidence": float(data.get("default_confidence", 0.6)),
                "chunk_size": int(data.get("chunk_size", 500)),
                "chunk_overlap": int(data.get("chunk_overlap", 50)),
                "verifier_search_source": str(
                    data.get("verifier_search_source", "auto") or "auto"
                ),
                "priority_topics": str(data.get("priority_topics", "")),
                "auto_learn_topic_limit": int(data.get("auto_learn_topic_limit", 100)),
                # v1.2.0.0：联网搜索与知识领域控制
                "enable_web_search": bool(data.get("enable_web_search", True)),
                "enable_bilibili": bool(data.get("enable_bilibili", False)),
                "web_search_only_highest_priority": bool(
                    data.get("web_search_only_highest_priority", False)
                ),
                "knowledge_source_priority": str(
                    data.get("knowledge_source_priority", "url,web,bilibili")
                ),
                "knowledge_domain_scope": str(data.get("knowledge_domain_scope", "")),
                "enable_cross_domain": bool(data.get("enable_cross_domain", True)),
                "cross_domain_exclude_admin": bool(
                    data.get("cross_domain_exclude_admin", True)
                ),
            }
        )

    async def _web_url_sources(self):
        return json_response({"items": self.url_sources.list_sources()})

    async def _web_add_url_source(self):
        payload = await request.json(default={}) or {}
        if not isinstance(payload, dict):
            return error_response("payload must be a JSON object", status_code=400)
        try:
            source = self.url_sources.add_source(
                name=payload.get("name", ""),
                url=payload.get("url", ""),
                source_type=payload.get("type", "page"),
                enabled=payload.get("enabled", True),
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response({"ok": True, "source": source})

    async def _web_set_url_source_enabled(self, source_id: str):
        payload = await request.json(default={}) or {}
        try:
            source = self.url_sources.set_enabled(
                source_id,
                bool(payload.get("enabled", True)),
            )
        except KeyError:
            return error_response("URL source not found", status_code=404)
        return json_response({"ok": True, "source": source})

    async def _web_delete_url_source(self, source_id: str):
        if not self.url_sources.remove_source(source_id):
            return error_response("URL source not found", status_code=404)
        return json_response({"ok": True})

    def _load_schema(self) -> dict:
        """读取 _conf_schema.json。失败时返回空 dict。"""
        try:
            schema_path = Path(__file__).parent / "_conf_schema.json"
            if schema_path.exists():
                raw = schema_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"读取 _conf_schema.json 失败: {e}")
        return {}

    async def _web_config_schema(self):
        """返回 _conf_schema.json 全量字段 + 当前合并值。"""
        schema = self._load_schema()
        settings = self.config_manager.overlay_all()
        config = self.config_manager.all()
        fields = []
        for name, spec in schema.items():
            if not isinstance(spec, dict):
                continue
            default = spec.get("default")
            # 当前值：settings 优先，否则用 config（即 schema 默认已合并入 config）
            current_val = settings.get(name, config.get(name, default))
            fields.append(
                {
                    "name": name,
                    "description": spec.get("description", ""),
                    "hint": spec.get("hint", ""),
                    "type": spec.get("type", "string"),
                    "default": default,
                    "value": current_val,
                }
            )
        return json_response({"fields": fields})

    async def _web_save_settings(self):
        """保存插件设置。支持所有 schema 字段 + 4 个 refine_* 字段。

        校验：
        - llm_provider_id：必须存在（空字符串表示使用事件默认）
        - bool 类型字段：bool()
        - int 类型字段：int()
        - float 类型字段：float()
        - string 类型字段：str()
        保存后调用 _apply_config_to_runtime() 立即生效。
        """
        payload = await request.json(default={}) or {}
        if not isinstance(payload, dict):
            return error_response("payload must be a JSON object", status_code=400)

        schema = self._load_schema()
        new_settings: dict = {}

        # 1. llm_provider_id（特殊处理：空串表示使用默认）
        if "llm_provider_id" in payload:
            pid = payload.get("llm_provider_id")
            pid = str(pid).strip() if pid is not None else ""
            if pid and not self._provider_exists(pid):
                return error_response(f"provider_id '{pid}' 不存在", status_code=400)
            new_settings["llm_provider_id"] = pid

        # 2. refine_on_*（不在 schema 中，但 settings.json 支持）
        for key in ("refine_on_search", "refine_on_import", "refine_on_verify"):
            if key in payload:
                try:
                    new_settings[key] = bool(payload[key])
                except (TypeError, ValueError):
                    return error_response(f"{key} must be boolean", status_code=400)

        # 3. schema 字段（按 type 校验）
        for name, spec in schema.items():
            if name not in payload or not isinstance(spec, dict):
                continue
            ftype = spec.get("type", "string")
            raw = payload.get(name)
            if raw is None and ftype != "string":
                # None 表示用户清空了输入，跳过此字段
                continue
            try:
                if ftype == "bool":
                    new_settings[name] = bool(raw)
                elif ftype == "int":
                    new_settings[name] = int(raw)
                elif ftype == "float":
                    new_settings[name] = float(raw)
                else:
                    new_settings[name] = str(raw)
            except (TypeError, ValueError):
                return error_response(
                    f"字段 '{name}' 类型错误：期望 {ftype}，实际 {type(raw).__name__}",
                    status_code=400,
                )

        # 保存并应用
        updated = self.config_manager.update(**new_settings)
        try:
            self._apply_config_to_runtime(updated)
        except Exception as e:
            logger.warning(f"应用配置到运行时失败: {e}")
        logger.info(f"插件设置已更新: {new_settings}")

        # 返回更新后的全量设置（含 schema 字段）
        resp = {
            "llm_provider_id": updated.get("llm_provider_id", ""),
            "refine_on_search": bool(updated.get("refine_on_search", True)),
            "refine_on_import": bool(updated.get("refine_on_import", True)),
            "refine_on_verify": bool(updated.get("refine_on_verify", True)),
        }
        for name in schema.keys():
            if name in updated:
                resp[name] = updated[name]
        return json_response(resp)

    def _sync_config_snapshot(self, settings: dict) -> dict:
        """刷新所有模块共享的配置快照，并返回完整合并值。"""
        cfg = self.config_manager.all()
        cfg.update({k: v for k, v in settings.items() if v is not None})
        self.config = dict(cfg)
        return cfg

    def _apply_config_to_runtime(self, settings: dict) -> None:
        """把保存后的设置立即应用到运行时变量（无需重启 AstrBot）。

        合并优先级：settings（自管存储）覆盖 self.config（schema 默认）。
        """
        # tools.py、verifier.py、_get_admin_ids() 等运行路径会直接读取
        # plugin.config；必须先刷新公开快照，否则管理页保存后诊断和实际功能
        # 都会继续使用启动时旧参数。
        cfg = self._sync_config_snapshot(settings)

        # 容量与置信度阈值
        try:
            self.store._max_entries = int(cfg.get("max_entries", 500))
        except (TypeError, ValueError):
            pass
        try:
            self.store._min_confidence = float(cfg.get("min_confidence", 0.3))
        except (TypeError, ValueError):
            pass

        # 关键词提示
        self._enable_active_learn_hint = bool(cfg.get("enable_active_learn_hint", True))

        # LLM Provider
        self._cfg_llm_provider_id = (cfg.get("llm_provider_id") or "").strip()

        # 混合检索
        new_embedding_enabled = bool(cfg.get("embedding_enabled", True))
        if new_embedding_enabled and self.embedder is None:
            try:
                self.embedder = Embedder(self)
                logger.info("已启用向量检索（运行时切换）")
            except Exception as e:
                logger.warning(f"启用向量检索失败: {e}")
                self.embedder = None
        elif not new_embedding_enabled and self.embedder is not None:
            self.embedder = None
            logger.info("已禁用向量检索（运行时切换）")
        self._hybrid_weights = self._parse_hybrid_weights(
            cfg.get("hybrid_search_weight", "0.4,0.6")
        )
        try:
            self._decay_half_life_days = float(cfg.get("decay_half_life_days", 30))
        except (TypeError, ValueError):
            pass
        self._enable_scope_fallback = bool(cfg.get("enable_scope_fallback", True))
        try:
            self._external_search._first_result_grace = max(
                0.0,
                float(
                    cfg.get(
                        "external_search_first_result_grace_seconds",
                        _EXTERNAL_SEARCH_FIRST_RESULT_GRACE_SECONDS,
                    )
                ),
            )
        except (TypeError, ValueError):
            pass

        # 关心领域
        self._priority_topics = [
            t.strip().lower()
            for t in (cfg.get("priority_topics") or "").split(",")
            if t.strip()
        ]
        try:
            self._priority_boost_max = float(cfg.get("priority_boost_max", 1.3))
            self._priority_boost_min = float(cfg.get("priority_boost_min", 1.0))
            self._priority_boost_decay = float(cfg.get("priority_boost_decay", 0.85))
        except (TypeError, ValueError):
            pass
        # 重置当前 boost（命中关心领域重置为 max，否则保持 1.0）
        self._priority_boost = (
            self._priority_boost_max if self._priority_topics else 1.0
        )

        # 上下文注入条数
        try:
            self._context_inject_count = max(
                1,
                min(
                    _MEMORY_INJECT_MAX_COUNT,
                    int(cfg.get("context_inject_count", 3)),
                ),
            )
        except (TypeError, ValueError):
            pass

        # 学习权重
        try:
            self._learn_weight = max(0.0, min(1.0, float(cfg.get("learn_weight", 0.7))))
        except (TypeError, ValueError):
            pass
        try:
            self._search_top_k = max(1, min(20, int(cfg.get("search_top_k", 5))))
        except (TypeError, ValueError):
            pass
        try:
            self._default_confidence = max(
                0.1, min(1.0, float(cfg.get("default_confidence", 0.6)))
            )
        except (TypeError, ValueError):
            pass
        try:
            self._chunk_size = max(100, min(5000, int(cfg.get("chunk_size", 500))))
        except (TypeError, ValueError):
            pass
        try:
            self._chunk_overlap = max(0, min(1000, int(cfg.get("chunk_overlap", 50))))
        except (TypeError, ValueError):
            pass

        # v1.2.0.0：联网搜索与知识领域控制
        self._enable_web_search = bool(cfg.get("enable_web_search", True))
        old_enable_bilibili = getattr(self, "_enable_bilibili", False)
        self._enable_bilibili = bool(cfg.get("enable_bilibili", False))
        self._web_search_only_highest_priority = bool(
            cfg.get("web_search_only_highest_priority", False)
        )
        self._knowledge_source_priority = self._parse_source_priority(
            cfg.get("knowledge_source_priority", "url,web,bilibili")
        )
        self._knowledge_domain_scope = [
            d.strip().lower()
            for d in (cfg.get("knowledge_domain_scope") or "").split(",")
            if d.strip()
        ]
        self._enable_cross_domain = bool(cfg.get("enable_cross_domain", True))
        self._cross_domain_exclude_admin = bool(
            cfg.get("cross_domain_exclude_admin", True)
        )
        if self._enable_bilibili != old_enable_bilibili:
            try:
                self._register_llm_tools()
            except Exception as exc:
                logger.warning(f"按 B 站开关刷新 LLM 工具失败: {exc}")

        # 清空 embedder 矩阵缓存（参数变化后需重建）
        if self.embedder is not None:
            try:
                self.embedder.invalidate_matrix_cache()
            except Exception:
                pass

    # ---------- 导入功能（v1.1.5.0：委托 importer.py） ----------

    async def _web_import_text(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_text(payload)
        if not result.get("ok"):
            return error_response(
                result.get("error", "导入失败"),
                status_code=result.get("status_code", 500),
            )
        return json_response({"ok": True, "entry": result["entry"]})

    async def _web_import_md(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_md(payload)
        if not result.get("ok"):
            return error_response(
                result.get("error", "导入失败"),
                status_code=result.get("status_code", 500),
            )
        if "entry" in result:
            return json_response({"ok": True, "entry": result["entry"]})
        return json_response({"ok": True, "batch": result["batch"]})

    async def _web_import_pdf(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_pdf(payload)
        if not result.get("ok"):
            return error_response(
                result.get("error", "导入失败"),
                status_code=result.get("status_code", 500),
            )
        return json_response({"ok": True, "batch": result["batch"]})

    async def _web_import_docx(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_docx(payload)
        if not result.get("ok"):
            return error_response(
                result.get("error", "导入失败"),
                status_code=result.get("status_code", 500),
            )
        return json_response({"ok": True, "batch": result["batch"]})

    async def _web_import_txt(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_txt(payload)
        if not result.get("ok"):
            return error_response(
                result.get("error", "导入失败"),
                status_code=result.get("status_code", 500),
            )
        return json_response({"ok": True, "batch": result["batch"]})

    async def _web_import_zip(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_zip(payload)
        if not result.get("ok"):
            return error_response(
                result.get("error", "导入失败"),
                status_code=result.get("status_code", 500),
            )
        return json_response(result)

    async def _web_builtin_kb_list(self):
        try:
            items = await self.importer.get_builtin_kb_list()
            if items is None:
                return error_response(
                    "当前 AstrBot 版本未启用知识库模块（kb_manager 不可用）",
                    status_code=501,
                )
            return json_response({"items": items})
        except Exception as e:
            logger.error(f"读取知识库列表失败: {e}", exc_info=True)
            return error_response(f"读取知识库列表失败: {e}", status_code=500)

    async def _web_builtin_kb_documents(self, kb_id: str):
        try:
            result = await self.importer.get_builtin_kb_documents(kb_id)
            if result is None:
                return error_response("知识库不存在", status_code=404)
            return json_response(result)
        except Exception as e:
            logger.error(f"读取 KB 文档列表失败 (kb_id={kb_id}): {e}", exc_info=True)
            return error_response(f"读取文档列表失败: {e}", status_code=500)

    async def _web_builtin_kb_import(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_builtin_kb(payload)
        if not result.get("ok"):
            return error_response(
                result.get("error", "导入失败"),
                status_code=result.get("status_code", 500),
            )
        return json_response(result)

    async def _web_logs(self):
        """返回本插件最近的日志。"""
        logs = [item.get("text", "") for item in self._log_buffer]
        return json_response({"logs": logs, "count": len(logs)})


# v1.1.5.0：_parse_md 已移至 importer.py


class _BufferHandler(logging.Handler):
    """将日志写入内存缓冲区，供 Dashboard 查看。"""

    PLUGIN_LOGGER_PREFIX = "astrbot_plugin_active_learner"
    _SERIES_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
    _SENSITIVE_KEY = re.compile(
        r"(?i)(?:token|api[_-]?key|secret|password|authorization|cookie|umo|"
        r"user[_-]?id|group[_-]?id|platform[_-]?id|account|person|session|"
        r"requester|recipient|target|identity|filename|file[_-]?path|path|"
        r"location|latitude|longitude|prompt|response|reply|query|topic|"
        r"content|message|claim|snippet|url|scope$|scope[_-]?id|new_settings)"
    )
    _SECRET = re.compile(
        r"(?i)(token|api[_-]?key|secret|password|authorization|cookie|umo|"
        r"user[_-]?id|group[_-]?id|platform[_-]?id)(?:\s*[:=]\s*|\s+)"
        r"(?:bearer\s+)?([^,\s]+)"
    )
    _PRIVATE_VALUE = re.compile(
        r"(?i)(user_text|prompt|response|reply|query|topic|content|scope|message|new_settings)\s*=\s*(?:'[^']*'|\"[^\"]*\"|[^,\s]+)"
    )
    _LONG_NUMBER = re.compile(r"(?<![\w.])[0-9]{6,}(?![\w.])")
    _ACTOR_ID = re.compile(
        r"(?i)\b(?:user|group|account|person|session)[-_:][A-Za-z0-9_-]+\b"
    )
    _URL_QUERY = re.compile(r"(https?://[^\s?]+)\?[^\s]+", re.IGNORECASE)
    _URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    _PATH = re.compile(r"(?:[A-Za-z]:\\|/)[^\s]+")
    _EMAIL = re.compile(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE
    )
    _OPAQUE_VALUE = re.compile(
        r"(?<![\w])(?=[A-Za-z0-9_-]{20,}(?![\w]))"
        r"(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])"
        r"[A-Za-z0-9_-]+"
    )

    def __init__(self, buffer: collections.deque):
        super().__init__()
        self._buffer = buffer
        self._stream_id = uuid.uuid4().hex
        self._sequence = 0
        self._lock = threading.Lock()

    @classmethod
    def _safe_text(cls, value: Any, limit: int = 320) -> str:
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        text = cls._SECRET.sub(r"\1=<已隐藏>", text)
        text = cls._PRIVATE_VALUE.sub(r"\1=<已隐藏>", text)
        text = cls._EMAIL.sub("<已隐藏邮箱>", text)
        text = cls._OPAQUE_VALUE.sub("<已隐藏随机标识>", text)
        text = cls._ACTOR_ID.sub("<已隐藏标识>", text)
        text = cls._LONG_NUMBER.sub("<已隐藏标识>", text)
        text = cls._URL_QUERY.sub(r"\1?[已隐藏参数]", text)
        text = cls._URL.sub("<已隐藏网址>", text)
        text = cls._PATH.sub("<已隐藏路径>", text)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @classmethod
    def _safe_details(cls, details: Any) -> dict[str, Any]:
        """Keep diagnostic details structured and privacy-safe."""
        if not isinstance(details, dict):
            return {}
        result: dict[str, Any] = {}
        for key, value in details.items():
            name = str(key)[:64]
            if cls._SENSITIVE_KEY.search(name):
                continue
            if isinstance(value, bool | int | float) or value is None:
                result[name] = value
            elif isinstance(value, (str, bytes)):
                raw = value.decode(errors="replace") if isinstance(value, bytes) else value
                result[name] = cls._safe_text(raw, 2000 if name.lower() == "log_detail" else 160)
            elif isinstance(value, (list, tuple)):
                result[name] = [cls._safe_text(item, 80) for item in value[:8]]
        return result

    def record_event(
        self,
        level: str,
        code: str,
        summary: Any,
        details: dict[str, Any] | None = None,
        *,
        text: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            safe_details = self._safe_details(details)
            if text:
                safe_details["log_detail"] = self._safe_text(text, 2000)
            self._sequence += 1
            event = {
                "seq": self._sequence,
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "plugin_id": "astrbot_plugin_active_learner",
                "plugin_name": "知",
                "level": str(level).upper(),
                "code": self._safe_text(code, 80),
                "summary": self._safe_text(summary),
                "details": safe_details,
                "text": self._safe_text(text or summary, 500),
            }
            self._buffer.append(event)
            return event

    def snapshot(self, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        after = max(0, int(after_seq or 0))
        size = min(1000, max(1, int(limit or 200)))
        with self._lock:
            events = [
                {key: value for key, value in item.items() if key != "text"}
                for item in self._buffer
                if item.get("seq", 0) > after
                and (
                    not str(item.get("code", "")).startswith("logger.")
                    or item.get("level") in self._SERIES_LEVELS
                )
            ][-size:]
            first = self._buffer[0]["seq"] if self._buffer else self._sequence + 1
            return {
                "contract": "series.diagnostics@1.0",
                "plugin_id": "astrbot_plugin_active_learner",
                "plugin_name": "知",
                "stream_id": self._stream_id,
                "events": events,
                "next_seq": self._sequence,
                "dropped_before": max(0, first - 1),
            }

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._stream_id = uuid.uuid4().hex

    def emit(self, record: logging.LogRecord) -> None:
        # 严格过滤：只接受本插件 logger 的日志，避免被根 logger / AstrBot 日志污染
        if not record.name or not record.name.startswith(self.PLUGIN_LOGGER_PREFIX):
            return
        try:
            formatted = self.format(record)
            module = self._safe_text(record.module or "plugin", 40)
            level = record.levelname.lower()
            self.record_event(
                record.levelname,
                f"logger.{level}.{module}.{self._safe_text(record.funcName or 'event', 60)}",
                f"{module} 记录了一条 {record.levelname} 事件，详细信息仅在知的独立日志页查看",
                details={"module": module, "function": record.funcName or ""},
                text=formatted,
            )
        except Exception:
            pass
