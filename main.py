"""AstrBot 主动学习记忆插件主入口。

功能：
1. 自动检索记忆并注入 LLM 上下文
2. 主动学习新知识（关键词触发 + LLM 工具调用）
3. 按用户/群聊双层隔离的 SQLite 记忆库
4. 质疑时多源交叉验证 + LLM 自辩论 + 版本化
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

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
from .models import Scope
from .searcher import WebSearcher
from .storage import MemoryStore
from .triggers import ACTIVE_LEARN_PATTERNS, CHALLENGE_PATTERNS
from .tools import create_tools
from .verifier import Verifier

PLUGIN_NAME = "astrbot_plugin_active_learner"


@register(
    "astrbot_plugin_active_learner",
    "AstrBotUser",
    "主动学习记忆：自动检索注入、主动多源学习、双层隔离 SQLite 记忆库、质疑多源验证",
    "2.1.0",
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
        self.bili_source = BiliSource()
        self.verifier = Verifier(self)

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

        # 1. 检索记忆
        try:
            hits = await asyncio.to_thread(self.store.search, scope, msg, 3)
        except Exception as e:
            logger.debug(f"记忆检索失败: {e}")
            hits = []

        for h in hits:
            entry = h.entry
            tag = "✅已验证" if entry.verified else f"⚠️置信度{entry.confidence:.0%}"
            parts.append(f"[记忆] {entry.topic}（{tag}）: {entry.content}")

        if parts:
            parts.append("（参考即可，不要照搬；如发现错误请指出，可调用 verify_knowledge 验证）")

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

        injection = "\n".join(parts)
        try:
            if hasattr(req, "extra_user_content_parts"):
                from astrbot.core.agent.message import TextPart
                req.extra_user_content_parts.append(TextPart(text=injection))
                logger.debug(f"注入 {len(hits)} 条记忆上下文 (extra_user_content_parts)")
            else:
                # 兜底：修改 system_prompt（会破坏 prompt 缓存，仅降级用）
                req.system_prompt = (req.system_prompt or "") + "\n" + injection
                logger.warning("extra_user_content_parts 不可用，降级用 system_prompt 注入")
        except Exception as e:
            logger.error(f"上下文注入失败: {e}")

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

        # 取 provider
        try:
            provider_id = await self.context.get_current_chat_provider_id(
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

    # ---------- Dashboard 管理页面后端 API ----------

    def _register_web_apis(self, context: Context) -> None:
        """注册 8 个 web API 路由供 Dashboard 页面调用。"""
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
