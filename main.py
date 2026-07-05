"""AstrBot 主动学习记忆插件主入口。

功能：
1. 自动检索记忆并注入 LLM 上下文
2. 主动学习新知识（关键词触发 + LLM 工具调用）
3. 按用户/群聊双层隔离的 SQLite 记忆库
4. 质疑时多源交叉验证 + LLM 自辩论 + 版本化
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.api.web import error_response, file_response, json_response, request
    _WEB_AVAILABLE = True
except ImportError:  # AstrBot < v4.26 没有 Plugin Pages 支持
    _WEB_AVAILABLE = False
    error_response = file_response = json_response = request = None  # type: ignore

from .bili_source import BiliSource
from .chunker import chunk_docx, chunk_markdown, chunk_pdf, chunk_text
from .embedder import Embedder
from .models import Scope, make_chunk_id
from .refiner import KnowledgeRefiner
from .searcher import WebSearcher
from .settings_store import SettingsStore
from .storage import MemoryStore
from .triggers import ACTIVE_LEARN_PATTERNS, CHALLENGE_PATTERNS
from .tools import create_tools
from .verifier import Verifier

PLUGIN_NAME = "astrbot_plugin_active_learner"

# 运行时检测 on_llm_response hook 是否可用（不可用时降级为 on_llm_request 内嵌 References）
_ON_LLM_RESPONSE_AVAILABLE = callable(getattr(filter, "on_llm_response", None))


@register(
    "astrbot_plugin_active_learner",
    "AstrBotUser",
    "主动学习记忆：自动检索注入、主动多源学习、双层隔离 SQLite 记忆库、质疑多源验证",
    "2.4.0",
    "https://github.com/qsbb/astrbot_plugin_active_learner",
)
class ActiveLearnerPlugin(Star):
    """主动学习记忆插件。"""

    # ---------- 生命周期 ----------

    def __init__(self, context: Context):
        super().__init__(context)
        # 兼容多种 config 注入方式：
        # 1. AstrBot 新版：self.config 自动注入
        # 2. 旧版：context.get_config() 返回全局配置，需取插件子键
        # 3. 兜底：空字典
        cfg = getattr(self, "config", None) or {}
        if not cfg and hasattr(context, "get_config"):
            try:
                raw = context.get_config()
                if isinstance(raw, dict):
                    cfg = raw.get("active_learner", raw)
                else:
                    cfg = {}
            except Exception:
                cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}

        max_entries = int(cfg.get("max_entries", 500))
        min_confidence = float(cfg.get("min_confidence", 0.3))
        ddg_fallback = bool(cfg.get("ddg_fallback", True))
        self.config = cfg  # 统一保存，供 tools.py 等模块读取

        # 存储层
        db_path = StarTools.get_data_dir() / "memory.db"
        self.store = MemoryStore(
            db_path=db_path,
            max_entries=max_entries,
            min_confidence=min_confidence,
        )

        # 搜索器与验证器
        self.searcher = WebSearcher(ddg_fallback=ddg_fallback)
        self.bili_source = BiliSource(context)
        self.verifier = Verifier(self)

        # Phase 1：精炼器 + 自管设置
        self._cfg_llm_provider_id = (cfg.get("llm_provider_id") or "").strip()
        self.refiner = KnowledgeRefiner(self)
        self._settings = SettingsStore(
            StarTools.get_data_dir() / "active_learner_settings.json"
        )

        # v2.4.0：向量混合检索配置
        self._embedding_enabled = bool(cfg.get("embedding_enabled", True))
        self._hybrid_weights = self._parse_hybrid_weights(
            cfg.get("hybrid_search_weight", "0.4,0.6")
        )
        self._decay_half_life_days = float(cfg.get("decay_half_life_days", 30))
        self._enable_scope_fallback = bool(cfg.get("enable_scope_fallback", True))
        self.embedder: Optional[Embedder] = (
            Embedder(self) if self._embedding_enabled else None
        )
        # 关心领域优先 + 注入条数
        self._priority_topics = [
            t.strip().lower()
            for t in (cfg.get("priority_topics") or "").split(",")
            if t.strip()
        ]
        self._context_inject_count = max(1, min(10, int(cfg.get("context_inject_count", 3))))
        # priority boost 动态衰减：命中关心领域重置为 max，未命中则逐步衰减到 min
        self._priority_boost_max = float(cfg.get("priority_boost_max", 1.3))
        self._priority_boost_min = float(cfg.get("priority_boost_min", 1.0))
        self._priority_boost_decay = float(cfg.get("priority_boost_decay", 0.85))
        self._priority_boost = self._priority_boost_max if self._priority_topics else 1.0

        # 关键词提示开关
        self._enable_active_learn_hint = bool(cfg.get("enable_active_learn_hint", True))

        # 注册 LLM 工具
        try:
            tools = create_tools(self)
            if tools:
                self.context.add_llm_tools(*tools)
                logger.info(f"已注册 {len(tools)} 个 LLM 工具: {[t.name for t in tools]}")
        except Exception as e:
            logger.error(f"注册 LLM 工具失败: {e}")

        logger.info(
            f"ActiveLearner 已加载 | max_entries={max_entries} | "
            f"bili={'on' if self.bili_source.is_available() else 'off'} | "
            f"db={db_path}"
        )

        # 注册 Dashboard 管理页面后端 API（AstrBot v4.26+）
        if _WEB_AVAILABLE:
            try:
                self._register_web_apis(context)
                logger.info("已注册 Dashboard 管理页面 API")
            except Exception as e:
                logger.warning(f"Web API 注册失败，Dashboard 页面将不可用: {e}")
        else:
            logger.info("当前 AstrBot 版本不支持 Plugin Pages，跳过 Dashboard 页面注册")

    async def terminate(self):
        try:
            self.store.close()
        except Exception:
            pass
        logger.info("ActiveLearner 已卸载，记忆已持久化")

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

    # ---------- 上下文注入 + 质疑检测 + 主动学习提示（合并钩子） ----------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求前的统一钩子：检索记忆 + 质疑检测 + 主动学习提示。"""
        try:
            msg = event.get_message_str()
        except Exception:
            return
        if not msg or len(msg) < 3:
            return

        scope = Scope.from_event(event)
        parts: list[str] = []

        # 1. 检索记忆（v2.4.0：混合检索 FTS5 + 向量）
        try:
            query_vec = None
            if self.embedder is not None:
                query_vec = await self.embedder.embed_query(msg)
            hits = await asyncio.to_thread(
                self.store.search_hybrid,
                scope, msg, self._context_inject_count,
                embedder=self.embedder,
                fts_weight=self._hybrid_weights[0],
                vec_weight=self._hybrid_weights[1],
                enable_scope_fallback=self._enable_scope_fallback,
                decay_half_life_days=self._decay_half_life_days,
                query_vec=query_vec,
                priority_topics=self._priority_topics,
                priority_boost=self._priority_boost,
            )
            # 动态调整 priority boost：命中关心领域 → 重置；未命中 → 衰减
            if self._priority_topics:
                if self._hits_match_priority(hits):
                    if self._priority_boost < self._priority_boost_max:
                        logger.debug(
                            f"priority boost 命中重置: {self._priority_boost:.2f} -> {self._priority_boost_max:.2f}"
                        )
                    self._priority_boost = self._priority_boost_max
                else:
                    new_boost = max(
                        self._priority_boost_min,
                        self._priority_boost * self._priority_boost_decay,
                    )
                    if new_boost != self._priority_boost:
                        logger.debug(
                            f"priority boost 衰减: {self._priority_boost:.2f} -> {new_boost:.2f}"
                        )
                    self._priority_boost = new_boost
        except Exception as e:
            logger.debug(f"记忆检索失败: {e}")
            hits = []

        # 把注入的记忆 ID 挂到 event 上，供 on_llm_response footer 使用
        injected_ids = [h.entry.id for h in hits]
        try:
            object.__setattr__(event, "_injected_memory_ids", injected_ids)
        except Exception:
            pass

        for h in hits:
            entry = h.entry
            tag = "✅已验证" if entry.verified else f"⚠️置信度{entry.confidence:.0%}"
            parts.append(f"[记忆#{entry.id}] {entry.topic}（{tag}）: {entry.content}")

        if parts:
            parts.append("（参考即可，不要照搬；如发现错误请指出，可调用 verify_knowledge 验证）")
            # v2.4.0：References footer 内嵌（fallback 路径，on_llm_response hook 不可用时也能看到引用）
            if not _ON_LLM_RESPONSE_AVAILABLE and hits:
                refs_lines = ["📚 参考资料:"]
                for h in hits:
                    e = h.entry
                    v_tag = "已验证" if e.verified else "待验证"
                    refs_lines.append(f"- [{e.topic}] 置信度 {e.confidence:.0%} ({v_tag})")
                parts.append("\n".join(refs_lines))

        # 2. 质疑检测
        is_challenge = any(re.search(p, msg) for p in CHALLENGE_PATTERNS)
        if is_challenge and hits:
            target = hits[0].entry
            try:
                await asyncio.to_thread(self.store.inc_challenge, target.id)
            except Exception:
                pass
            parts.append(
                f"[质疑提示] 用户似乎在质疑关于「{target.topic}」的记忆。"
                f"当前记忆置信度 {target.confidence:.0%}。"
                f"若不确定，请调用 verify_knowledge 工具进行多源验证。"
            )

        # 3. 主动学习提示
        is_learn_trigger = False
        if self._enable_active_learn_hint and not hits:
            is_learn_trigger = any(re.search(p, msg) for p in ACTIVE_LEARN_PATTERNS)
            if is_learn_trigger:
                parts.append(
                    "[学习提示] 此问题记忆库暂无答案。"
                    "如需学习该知识，可调用 search_and_learn 工具搜索并存储。"
                )

        # 4. 注入
        if not parts:
            return

        # 行为规范（有内容注入时附带，约束 LLM 不要预告工具调用）
        parts.append(
            "[行为规范] 有记忆就直接答；需调用工具时直接调用，"
            "不要在回复里预告\"让我查查看\"、\"我搜一下\"、\"让我想想\"等话术。"
        )

        injection = "\n".join(parts)
        # 标签汇总，让日志一眼看出注入了什么
        tags = []
        if hits:
            tags.append(f"{len(hits)}条记忆")
        if is_challenge and hits:
            tags.append("质疑提示")
        if is_learn_trigger:
            tags.append("学习提示")
        try:
            if hasattr(req, "extra_user_content_parts"):
                from astrbot.core.agent.message import TextPart
                req.extra_user_content_parts.append(TextPart(text=injection))
                logger.info(f"注入上下文 [{'/'.join(tags)}] (scope: {scope})")
            else:
                # 兜底：修改 system_prompt（会破坏 prompt 缓存，仅降级用）
                req.system_prompt = (req.system_prompt or "") + "\n" + injection
                logger.warning("extra_user_content_parts 不可用，降级用 system_prompt 注入")
        except Exception as e:
            logger.error(f"上下文注入失败: {e}")

    # v2.4.0：on_llm_response hook（仅在 AstrBot 支持时启用）
    if _ON_LLM_RESPONSE_AVAILABLE:

        @filter.on_llm_response()  # type: ignore[misc]
        async def on_llm_response(self, event: AstrMessageEvent, response):
            """LLM 响应后追加 References footer（如果 on_llm_request 注入了记忆）。"""
            try:
                injected_ids = getattr(event, "_injected_memory_ids", []) or []
                if not injected_ids:
                    return
                refs_entries = self.store.get_entries_by_ids(injected_ids[:5])
                if not refs_entries:
                    return
                footer_lines = ["", "📚 参考资料:"]
                for e in refs_entries:
                    v_tag = "已验证" if e.verified else "待验证"
                    footer_lines.append(f"- [{e.topic}] 置信度 {e.confidence:.0%} ({v_tag})")
                footer = "\n".join(footer_lines)
                completion = getattr(response, "completion_text", None)
                if completion is not None:
                    try:
                        response.completion_text = completion + "\n" + footer
                    except Exception:
                        pass
                else:
                    try:
                        response.text = (response.text or "") + "\n" + footer
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"on_llm_response footer 失败: {e}")

    # ---------- /memory 指令组 ----------

    @filter.command_group("memory")
    def memory_cmd(self):
        """记忆库管理指令组。子指令: list/search/info/forget/verify/export/stats"""
        pass

    @memory_cmd.command("stats")
    async def memory_stats(self, event: AstrMessageEvent):
        """查看记忆库统计"""
        scope = Scope.from_event(event)
        stats = self.store.stats(scope)
        if stats["total"] == 0:
            yield event.plain_result(
                f"📝 当前作用域记忆库为空\n"
                f"作用域: {stats['scope_type']}:{stats['scope_id']}\n"
                f"我会在聊天中自动学习新知识~"
            )
            return
        text = (
            f"📝 记忆库统计\n"
            f"━━━━━━━━━━\n"
            f"作用域: {stats['scope_type']}:{stats['scope_id']}\n"
            f"总条数: {stats['total']}\n"
            f"已验证: {stats['verified']}\n"
            f"被质疑: {stats['challenged']}\n"
            f"平均置信度: {stats['avg_confidence']:.0%}\n"
            f"最常访问: {stats.get('most_accessed') or '无'}\n"
            f"━━━━━━━━━━\n"
            f"指令: /memory list | search <关键词> | info <主题> | "
            f"forget <主题> | verify <主题> | export"
        )
        yield event.plain_result(text)

    @memory_cmd.command("list")
    async def memory_list(self, event: AstrMessageEvent, page: int = 1):
        """列出记忆条目"""
        scope = Scope.from_event(event)
        entries, total, total_pages = self.store.list_memories(scope, page=page, per_page=10)
        if not entries:
            yield event.plain_result("📝 当前作用域记忆库为空")
            return
        lines = [f"📝 记忆列表 ({page}/{total_pages}页，共{total}条)\n"]
        for i, e in enumerate(entries, (page - 1) * 10 + 1):
            v = "✅" if e.verified else "❓"
            lines.append(
                f"{i}. {v} {e.topic} "
                f"(置信度{e.confidence:.0%}, 访问{e.access_count}次)"
            )
        lines.append(f"\n使用 /memory list <页码> 翻页")
        yield event.plain_result("\n".join(lines))

    @memory_cmd.command("search")
    async def memory_search(self, event: AstrMessageEvent, keyword: str):
        """搜索记忆"""
        scope = Scope.from_event(event)
        hits = self.store.search(scope, keyword, top_k=5)
        if not hits:
            yield event.plain_result(f"🔍 未找到与「{keyword}」相关的记忆")
            return
        lines = [f"🔍 搜索「{keyword}」的结果:\n"]
        for h in hits:
            e = h.entry
            v = "✅" if e.verified else "❓"
            lines.append(f"{v} {e.topic}")
            lines.append(f"   {e.content[:80]}...")
            lines.append(f"   置信度: {e.confidence:.0%} | 来源: {e.source}\n")
        yield event.plain_result("\n".join(lines))

    @memory_cmd.command("info")
    async def memory_info(self, event: AstrMessageEvent, topic: str):
        """查看某条记忆详情"""
        scope = Scope.from_event(event)
        entry = self.store.search_by_topic(scope, topic)
        if entry is None:
            hits = self.store.search(scope, topic, top_k=1)
            entry = hits[0].entry if hits else None
        if entry is None:
            yield event.plain_result(f"❌ 未找到关于「{topic}」的记忆")
            return
        import time as _time
        text = (
            f"📖 记忆详情: {entry.topic}\n"
            f"━━━━━━━━━━\n"
            f"内容: {entry.content}\n"
            f"关键词: {', '.join(entry.keywords) if entry.keywords else '无'}\n"
            f"来源: {entry.source}\n"
            f"置信度: {entry.confidence:.0%}\n"
            f"已验证: {'是✅' if entry.verified else '否❌'}\n"
            f"被质疑: {entry.challenge_count}次\n"
            f"访问次数: {entry.access_count}\n"
            f"创建: {_time.strftime('%Y-%m-%d %H:%M', _time.localtime(entry.created_at))}\n"
            f"更新: {_time.strftime('%Y-%m-%d %H:%M', _time.localtime(entry.updated_at))}"
        )
        yield event.plain_result(text)

    @memory_cmd.command("forget")
    async def memory_forget(self, event: AstrMessageEvent, topic: str):
        """删除某条记忆（软删除，留版本痕）"""
        scope = Scope.from_event(event)
        ok, deleted = self.store.forget(scope, topic)
        if ok and deleted:
            yield event.plain_result(f"🗑️ 已删除关于「{deleted.topic}」的记忆（版本已留痕）")
        else:
            yield event.plain_result(f"❌ 未找到关于「{topic}」的记忆")

    @memory_cmd.command("verify")
    async def memory_verify(self, event: AstrMessageEvent, topic: str):
        """手动触发验证"""
        scope = Scope.from_event(event)
        entry = self.store.search_by_topic(scope, topic)
        if entry is None:
            hits = self.store.search(scope, topic, top_k=1)
            entry = hits[0].entry if hits else None
        if entry is None:
            yield event.plain_result(f"❌ 未找到关于「{topic}」的记忆，请先学习该主题")
            return

        yield event.plain_result(f"🔍 正在多源验证「{entry.topic}」，请稍候...")

        # 取 provider（4 层 fallback：Dashboard 设置 → schema → 事件 scope → 同步默认）
        try:
            provider_id = await self._resolve_plugin_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception:
            provider_id = ""

        if not provider_id:
            yield event.plain_result("❌ 未找到可用的 LLM 提供商，无法执行验证")
            return

        # 执行验证
        try:
            result = await self.verifier.run(entry, provider_id)
        except Exception as e:
            logger.error(f"验证失败: {e}")
            yield event.plain_result(f"❌ 验证过程出错: {e}")
            return

        # 读取更新后的 entry
        updated = self.store.get_entry_by_id(entry.id)
        if updated:
            extra = f"\n\n更新后置信度: {updated.confidence:.0%}"
            if updated.verified:
                extra += " ✅已验证"
        else:
            extra = ""

        yield event.plain_result(result.to_text() + extra)

    @memory_cmd.command("export")
    async def memory_export(self, event: AstrMessageEvent):
        """导出当前作用域的记忆库为 JSON"""
        scope = Scope.from_event(event)
        data = self.store.export_scope(scope)
        if not data:
            yield event.plain_result("📝 当前作用域记忆库为空，无需导出")
            return
        export_path = StarTools.get_data_dir() / f"memory_export_{scope.type}_{scope.id}.json"
        try:
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            yield event.plain_result(
                f"📦 已导出 {len(data)} 条记忆到:\n{export_path}"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 导出失败: {e}")

    @memory_cmd.command("versions")
    async def memory_versions(self, event: AstrMessageEvent, topic: str):
        """查看某条记忆的历史版本"""
        scope = Scope.from_event(event)
        entry = self.store.search_by_topic(scope, topic)
        if entry is None:
            hits = self.store.search(scope, topic, top_k=1)
            entry = hits[0].entry if hits else None
        if entry is None:
            yield event.plain_result(f"❌ 未找到关于「{topic}」的记忆")
            return
        versions = self.store.list_versions(entry.id)
        if not versions:
            yield event.plain_result(f"📝 「{entry.topic}」暂无历史版本")
            return
        import time as _time
        lines = [f"📜 「{entry.topic}」的历史版本:\n"]
        for v in versions:
            lines.append(
                f"v{v.version_no} [{v.reason}] "
                f"置信度{v.confidence:.0%} "
                f"{_time.strftime('%Y-%m-%d %H:%M', _time.localtime(v.created_at))}"
            )
            lines.append(f"   {v.content[:100]}...")
            lines.append("")
        yield event.plain_result("\n".join(lines))

    @memory_cmd.command("refresh")
    async def memory_refresh(self, event: AstrMessageEvent, topic: str):
        """刷新某条记忆的 last_accessed_at，恢复衰减分数。"""
        scope = Scope.from_event(event)
        entry = self.store.search_by_topic(scope, topic)
        if entry is None:
            hits = self.store.search(scope, topic, top_k=1)
            entry = hits[0].entry if hits else None
        if entry is None:
            yield event.plain_result(f"❌ 未找到关于「{topic}」的记忆")
            return
        self.store.update_last_accessed(entry.id)
        yield event.plain_result(
            f"🔄 已刷新「{entry.topic}」的访问时间，衰减分数已恢复。\n"
            f"当前置信度: {entry.confidence:.0%}"
        )

    # ---------- Dashboard 管理页面后端 API ----------

    def _register_web_apis(self, context: Context) -> None:
        """注册 17 个 web API 路由供 Dashboard 页面调用。"""
        context.register_web_api(
            f"/{PLUGIN_NAME}/stats", self._web_stats, ["GET"], "记忆库统计"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/scopes", self._web_scopes, ["GET"], "列出所有 scope"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memories", self._web_memories, ["GET"], "记忆列表（分页+搜索）"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/<entry_id>",
            self._web_memory_detail, ["GET"], "记忆详情",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/<entry_id>/versions",
            self._web_memory_versions, ["GET"], "版本历史",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/<entry_id>/forget",
            self._web_memory_forget, ["POST"], "软删除记忆",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/memory/<entry_id>/verify",
            self._web_memory_verify, ["POST"], "触发验证",
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
            f"/{PLUGIN_NAME}/import_txt", self._web_import_txt, ["POST"], "导入 TXT（带分块）"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/providers", self._web_providers, ["GET"], "列出可用 LLM Provider"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings", self._web_get_settings, ["GET"], "获取插件设置"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings", self._web_save_settings, ["POST"], "保存插件设置"
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
        return json_response({
            "items": [e.to_dict() for e in entries],
            "total": total,
            "total_pages": total_pages,
            "page": page,
            "per_page": per_page,
        })

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
        if not provider_id:
            provider_id = self._resolve_default_provider_id()
        if not provider_id:
            return error_response(
                "无法确定 LLM provider，请在请求体中指定 provider_id",
                status_code=400,
            )
        try:
            result = await self.verifier.run(entry, provider_id)
        except Exception as e:
            return error_response(f"验证失败: {e}", status_code=500)
        return json_response({
            "verdict": result.verdict,
            "confidence": result.confidence,
            "content": result.content,
            "reasoning": result.reasoning,
            "sources_count": result.sources_count,
            "sources_consistent": result.sources_consistent,
            "text": result.to_text(),
        })

    async def _web_export(self):
        scope = self._scope_from_query()
        if scope is None:
            entries, _, _ = self.store.list_all_memories(page=1, per_page=10 ** 9)
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
        # 兜底：从 provider_manager.providers 取第一个
        pm = getattr(self.context, "provider_manager", None)
        if pm is not None:
            providers = getattr(pm, "providers", None) or []
            for p in providers:
                pid = getattr(p, "id", None) or getattr(p, "name", None)
                if pid:
                    return str(pid)
        return ""

    def _provider_exists(self, provider_id: str) -> bool:
        """校验 provider_id 是否在 provider_manager 中存在（防止选了已删除的 provider）。"""
        if not provider_id:
            return False
        pm = getattr(self.context, "provider_manager", None)
        if pm is None:
            return True  # 无法校验时不过滤
        providers = getattr(pm, "providers", None) or []
        for p in providers:
            pid = getattr(p, "id", None) or getattr(p, "name", None)
            if pid and str(pid) == str(provider_id):
                return True
        return False

    async def _resolve_plugin_provider_id(self, umo: str = "") -> str:
        """4 层 fallback 解析插件使用的 LLM Provider ID。

        1. self._settings 中的 llm_provider_id（Dashboard 设置，最高优先级）
        2. self._cfg_llm_provider_id（_conf_schema.json 中的字段）
        3. context.get_current_chat_provider_id(umo=...) （事件 scope 默认）
        4. self._resolve_default_provider_id() （同步兜底）

        每个候选都先经 _provider_exists 校验，避免选了已删除的 provider。
        """
        # 1. Dashboard 设置
        pid = self._settings.get("llm_provider_id") or ""
        if pid and self._provider_exists(pid):
            return pid

        # 2. schema 字段
        if self._cfg_llm_provider_id and self._provider_exists(self._cfg_llm_provider_id):
            return self._cfg_llm_provider_id

        # 3. 事件 scope 默认（async）
        if umo:
            method = getattr(self.context, "get_current_chat_provider_id", None)
            if callable(method):
                try:
                    pid = await method(umo=umo)
                    if pid and self._provider_exists(pid):
                        return pid
                except Exception:
                    pass

        # 4. 同步兜底
        return self._resolve_default_provider_id()

    # ---------- 设置与 Provider API ----------

    async def _web_providers(self):
        """列出所有可用 LLM Provider + 当前选中的。"""
        pm = getattr(self.context, "provider_manager", None)
        providers_list = []
        if pm is not None:
            for p in getattr(pm, "providers", None) or []:
                providers_list.append({
                    "id": str(getattr(p, "id", "") or ""),
                    "name": str(getattr(p, "name", "") or ""),
                    "type": str(getattr(p, "type", "") or ""),
                })
        current = (
            self._settings.get("llm_provider_id")
            or self._cfg_llm_provider_id
            or self._resolve_default_provider_id()
        )
        return json_response({"providers": providers_list, "current": current})

    async def _web_get_settings(self):
        """返回当前插件设置（含默认值填充）。"""
        data = self._settings.all()
        return json_response({
            "llm_provider_id": data.get("llm_provider_id", ""),
            "refine_on_search": bool(data.get("refine_on_search", True)),
            "refine_on_import": bool(data.get("refine_on_import", True)),
            "refine_on_verify": bool(data.get("refine_on_verify", True)),
        })

    async def _web_save_settings(self):
        """保存插件设置。校验 provider_id 存在性 + bool 字段。"""
        payload = await request.json(default={}) or {}
        if not isinstance(payload, dict):
            return error_response("payload must be a JSON object", status_code=400)

        new_settings: dict = {}

        pid = payload.get("llm_provider_id")
        if pid is not None:
            pid = str(pid).strip()
            if pid and not self._provider_exists(pid):
                return error_response(
                    f"provider_id '{pid}' 不存在", status_code=400
                )
            new_settings["llm_provider_id"] = pid

        for key in ("refine_on_search", "refine_on_import", "refine_on_verify"):
            if key in payload:
                try:
                    new_settings[key] = bool(payload[key])
                except (TypeError, ValueError):
                    return error_response(f"{key} must be boolean", status_code=400)

        updated = self._settings.update(**new_settings)
        logger.info(f"插件设置已更新: {new_settings}")
        return json_response({
            "llm_provider_id": updated.get("llm_provider_id", ""),
            "refine_on_search": bool(updated.get("refine_on_search", True)),
            "refine_on_import": bool(updated.get("refine_on_import", True)),
            "refine_on_verify": bool(updated.get("refine_on_verify", True)),
        })

    # ---------- 导入功能 ----------

    async def _web_import_text(self):
        """导入纯文本：JSON body {topic, content, scope_type, scope_id, keywords?, refine?}"""
        payload = await request.json(default={}) or {}
        topic = (payload.get("topic") or "").strip()
        content = payload.get("content") or ""
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        keywords = payload.get("keywords") or None
        refine = bool(payload.get("refine", True)) and bool(self._settings.get("refine_on_import", True))
        if not topic or not content or not scope_type:
            return error_response("topic, content, scope_type required", status_code=400)
        scope = Scope(type=scope_type, id=scope_id)
        try:
            final_content = content
            final_keywords = keywords
            final_confidence = 0.6
            source_tag = "手动导入"
            if refine:
                provider_id = await self._resolve_plugin_provider_id()
                result = await self.refiner.refine_import(topic, content, provider_id)
                final_content = result.summary
                final_keywords = result.keywords or keywords
                final_confidence = result.confidence
                source_tag = "手动导入+精炼" if result.refined else "手动导入+未精炼"
                if not result.refined:
                    logger.warning(f"导入「{topic}」精炼降级为原内容")
            entry = self.store.add_or_update(
                scope=scope, topic=topic, content=final_content,
                keywords=final_keywords, source=source_tag,
                sources_detail=None, confidence=final_confidence,
            )
        except Exception as e:
            return error_response(f"导入失败: {e}", status_code=500)
        logger.info(f"导入文本: {topic} (scope: {scope}, source: {source_tag})")
        return json_response({"ok": True, "entry": entry.to_dict()})

    async def _web_import_md(self):
        """导入单个 Markdown：JSON body {filename?, topic?, content, scope_type, scope_id, refine?, chunk_size?, chunk_overlap?}

        v2.4.0 起支持长文档分块：超过 chunk_size 的 MD 会被拆成多个 chunk 入库。
        """
        payload = await request.json(default={}) or {}
        content = payload.get("content") or ""
        filename = (payload.get("filename") or "").strip()
        topic = (payload.get("topic") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(self._settings.get("refine_on_import", True))
        chunk_size = int(payload.get("chunk_size", 500))
        chunk_overlap = int(payload.get("chunk_overlap", 50))
        if not content or not scope_type:
            return error_response("content, scope_type required", status_code=400)

        content_clean, extracted_topic = _parse_md(content)
        if not topic:
            topic = extracted_topic or (filename.rsplit(".", 1)[0] if filename else "未命名")

        scope = Scope(type=scope_type, id=scope_id)

        # v2.4.0：长文档分块
        chunks = chunk_markdown(content_clean, max_size=chunk_size, overlap=chunk_overlap)
        if not chunks:
            return error_response("MD 内容为空", status_code=400)

        # 单 chunk：走简单路径，保持响应格式向后兼容
        if len(chunks) == 1:
            try:
                final_content = chunks[0]
                final_keywords = None
                final_confidence = 0.6
                base_source = f"MD导入 ({filename})" if filename else "MD导入"
                source_tag = base_source
                if refine:
                    provider_id = await self._resolve_plugin_provider_id()
                    result = await self.refiner.refine_import(topic, chunks[0], provider_id)
                    final_content = result.summary
                    final_keywords = result.keywords
                    final_confidence = result.confidence
                    source_tag = f"{base_source}+精炼" if result.refined else f"{base_source}+未精炼"
                    if not result.refined:
                        logger.warning(f"导入「{topic}」精炼降级为原内容")
                entry = self.store.add_or_update(
                    scope=scope, topic=topic, content=final_content,
                    keywords=final_keywords, source=source_tag,
                    sources_detail=None, confidence=final_confidence,
                )
            except Exception as e:
                return error_response(f"导入失败: {e}", status_code=500)
            logger.info(f"导入 MD: {topic} (scope: {scope}, source: {source_tag})")
            return json_response({"ok": True, "entry": entry.to_dict()})

        # 多 chunk：走批量路径
        parent_doc_id = uuid.uuid4().hex[:16]
        return await self._import_chunks_batch(
            chunks=chunks,
            scope=scope,
            parent_doc_id=parent_doc_id,
            base_topic=topic,
            source_label=f"MD导入 ({filename})" if filename else "MD导入",
            refine=refine,
        )

    async def _import_chunks_batch(
        self,
        chunks: list[str],
        scope: Scope,
        parent_doc_id: str,
        base_topic: str,
        source_label: str,
        refine: bool,
    ) -> "MessageEventResult":
        """共享的批量 chunk 入库逻辑（PDF/DOCX/TXT/长 MD 共用）。

        - 批量精炼（如有 provider）
        - 批量嵌入（如有 embedder）
        - 每个 chunk 用 make_chunk_id 生成独立 ID
        - 写入后失效向量矩阵缓存
        """
        # 批量精炼
        refine_results = None
        if refine:
            provider_id = await self._resolve_plugin_provider_id()
            if provider_id:
                try:
                    refine_results = await self.refiner.refine_import_batch(
                        topics=[f"{base_topic} #{i+1}" for i in range(len(chunks))],
                        raw_contents=chunks,
                        provider_id=provider_id,
                    )
                except Exception as e:
                    logger.warning(f"批量精炼失败，降级为原内容: {e}")
                    refine_results = None

        # 批量嵌入
        embed_vecs = None
        if self.embedder is not None and self.embedder.available:
            try:
                embed_vecs = await self.embedder.embed_batch(chunks)
            except Exception as e:
                logger.warning(f"批量嵌入失败: {e}")
                embed_vecs = None

        # 入库
        results = []
        success_count = 0
        for i, chunk in enumerate(chunks):
            try:
                chunk_id = make_chunk_id(scope, parent_doc_id, i)
                topic = f"{base_topic} #{i+1}"

                if refine_results and i < len(refine_results) and refine_results[i].refined:
                    final_content = refine_results[i].summary
                    final_keywords = refine_results[i].keywords
                    final_confidence = refine_results[i].confidence
                    source_tag = f"{source_label}+精炼"
                else:
                    final_content = chunk
                    final_keywords = None
                    final_confidence = 0.5
                    source_tag = source_label

                entry = self.store.add_chunk(
                    chunk_id=chunk_id, scope=scope, topic=topic,
                    content=final_content, keywords=final_keywords,
                    source=source_tag, confidence=final_confidence,
                    parent_doc_id=parent_doc_id,
                )

                # 保存向量
                if embed_vecs and i < len(embed_vecs) and embed_vecs[i]:
                    try:
                        self.store.save_embedding(
                            chunk_id, embed_vecs[i], self.embedder.dim, self.embedder.model_name  # type: ignore[union-attr]
                        )
                    except Exception as e:
                        logger.debug(f"保存向量失败 chunk {i}: {e}")

                success_count += 1
                results.append({"chunk": i + 1, "topic": topic, "entry_id": entry.id, "ok": True})
            except Exception as e:
                results.append({"chunk": i + 1, "ok": False, "error": str(e)})

        # 失效向量矩阵缓存
        if self.embedder is not None:
            self.embedder.invalidate_matrix_cache(f"{scope.type}:{scope.id}")

        logger.info(
            f"导入 {source_label}: {success_count}/{len(chunks)} chunks 成功 "
            f"(scope: {scope}, refine={'yes' if refine_results else 'no'})"
        )
        return json_response({
            "ok": True,
            "total": len(chunks),
            "success": success_count,
            "failed": len(chunks) - success_count,
            "parent_doc_id": parent_doc_id,
            "results": results,
        })

    async def _web_import_pdf(self):
        """导入 PDF：JSON body {filename, base64, scope_type, scope_id, refine?, chunk_size?, chunk_overlap?}"""
        import base64 as _b64

        payload = await request.json(default={}) or {}
        b64 = payload.get("base64") or ""
        filename = (payload.get("filename") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(self._settings.get("refine_on_import", True))
        chunk_size = int(payload.get("chunk_size", 500))
        chunk_overlap = int(payload.get("chunk_overlap", 50))

        if not b64 or not scope_type:
            return error_response("base64, scope_type required", status_code=400)

        try:
            file_bytes = _b64.b64decode(b64)
        except Exception as e:
            return error_response(f"base64 解码失败: {e}", status_code=400)

        try:
            chunks = chunk_pdf(file_bytes, max_size=chunk_size, overlap=chunk_overlap)
        except ImportError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(f"PDF 解析失败: {e}", status_code=500)

        if not chunks:
            return error_response("PDF 未提取到任何文本", status_code=400)

        scope = Scope(type=scope_type, id=scope_id)
        parent_doc_id = uuid.uuid4().hex[:16]
        base_topic = filename.rsplit(".", 1)[0] if filename else "PDF文档"
        return await self._import_chunks_batch(
            chunks=chunks, scope=scope, parent_doc_id=parent_doc_id,
            base_topic=base_topic,
            source_label=f"PDF导入 ({filename})" if filename else "PDF导入",
            refine=refine,
        )

    async def _web_import_docx(self):
        """导入 DOCX：JSON body {filename, base64, scope_type, scope_id, refine?, chunk_size?, chunk_overlap?}"""
        import base64 as _b64

        payload = await request.json(default={}) or {}
        b64 = payload.get("base64") or ""
        filename = (payload.get("filename") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(self._settings.get("refine_on_import", True))
        chunk_size = int(payload.get("chunk_size", 500))
        chunk_overlap = int(payload.get("chunk_overlap", 50))

        if not b64 or not scope_type:
            return error_response("base64, scope_type required", status_code=400)

        try:
            file_bytes = _b64.b64decode(b64)
        except Exception as e:
            return error_response(f"base64 解码失败: {e}", status_code=400)

        try:
            chunks = chunk_docx(file_bytes, max_size=chunk_size, overlap=chunk_overlap)
        except ImportError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(f"DOCX 解析失败: {e}", status_code=500)

        if not chunks:
            return error_response("DOCX 未提取到任何文本", status_code=400)

        scope = Scope(type=scope_type, id=scope_id)
        parent_doc_id = uuid.uuid4().hex[:16]
        base_topic = filename.rsplit(".", 1)[0] if filename else "DOCX文档"
        return await self._import_chunks_batch(
            chunks=chunks, scope=scope, parent_doc_id=parent_doc_id,
            base_topic=base_topic,
            source_label=f"DOCX导入 ({filename})" if filename else "DOCX导入",
            refine=refine,
        )

    async def _web_import_txt(self):
        """导入 TXT：JSON body {filename, base64, scope_type, scope_id, refine?, chunk_size?, chunk_overlap?}"""
        import base64 as _b64

        payload = await request.json(default={}) or {}
        b64 = payload.get("base64") or ""
        filename = (payload.get("filename") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(self._settings.get("refine_on_import", True))
        chunk_size = int(payload.get("chunk_size", 500))
        chunk_overlap = int(payload.get("chunk_overlap", 50))

        if not b64 or not scope_type:
            return error_response("base64, scope_type required", status_code=400)

        try:
            file_bytes = _b64.b64decode(b64)
        except Exception as e:
            return error_response(f"base64 解码失败: {e}", status_code=400)

        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("gbk", errors="replace")

        chunks = chunk_text(text, max_size=chunk_size, overlap=chunk_overlap)
        if not chunks:
            return error_response("TXT 内容为空", status_code=400)

        scope = Scope(type=scope_type, id=scope_id)
        parent_doc_id = uuid.uuid4().hex[:16]
        base_topic = filename.rsplit(".", 1)[0] if filename else "TXT文档"
        return await self._import_chunks_batch(
            chunks=chunks, scope=scope, parent_doc_id=parent_doc_id,
            base_topic=base_topic,
            source_label=f"TXT导入 ({filename})" if filename else "TXT导入",
            refine=refine,
        )

    async def _web_import_zip(self):
        """批量导入 ZIP 中的 .md 文件：JSON body {filename?, base64, scope_type, scope_id, refine?}"""
        import base64 as _b64

        payload = await request.json(default={}) or {}
        b64 = payload.get("base64") or ""
        filename = (payload.get("filename") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(self._settings.get("refine_on_import", True))
        if not b64 or not scope_type:
            return error_response("base64, scope_type required", status_code=400)

        try:
            raw = _b64.b64decode(b64)
        except Exception as e:
            return error_response(f"base64 解码失败: {e}", status_code=400)
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except Exception as e:
            return error_response(f"无法读取 zip: {e}", status_code=400)

        scope = Scope(type=scope_type, id=scope_id)
        results = []
        success_count = 0
        for name in zf.namelist():
            if name.endswith("/") or not name.lower().endswith(".md"):
                continue
            try:
                md_bytes = zf.read(name)
                try:
                    md_content = md_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    md_content = md_bytes.decode("gbk", errors="replace")
                md_clean, extracted_topic = _parse_md(md_content)
                topic = extracted_topic or name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                final_content = md_clean
                final_keywords = None
                final_confidence = 0.6
                base_source = f"ZIP导入 ({name})"
                source_tag = base_source
                if refine:
                    provider_id = await self._resolve_plugin_provider_id()
                    result = await self.refiner.refine_import(topic, md_clean, provider_id)
                    final_content = result.summary
                    final_keywords = result.keywords
                    final_confidence = result.confidence
                    source_tag = f"{base_source}+精炼" if result.refined else f"{base_source}+未精炼"
                entry = self.store.add_or_update(
                    scope=scope, topic=topic, content=final_content,
                    keywords=final_keywords, source=source_tag,
                    sources_detail=None, confidence=final_confidence,
                )
                success_count += 1
                results.append({"file": name, "topic": topic, "entry_id": entry.id, "ok": True})
            except Exception as e:
                results.append({"file": name, "ok": False, "error": str(e)})

        logger.info(
            f"批量导入 ZIP: {success_count}/{len(results)} 成功 (scope: {scope}, refine={refine})"
        )
        return json_response({
            "ok": True,
            "total": len(results),
            "success": success_count,
            "failed": len(results) - success_count,
            "results": results,
        })


def _parse_md(content: str) -> tuple[str, str]:
    """解析 Markdown：去除 YAML frontmatter，提取首个 # 标题。

    返回 (clean_content, title)。无标题时 title 为空字符串。
    """
    title = ""
    # 去 YAML frontmatter
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            content = content[end + 4:].lstrip("\n")
    # 提取首个 # 标题
    for line in content.split("\n", 30):
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
        if stripped and not stripped.startswith("#"):
            break
    return content, title
