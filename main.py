"""AstrBot 凝心溯溪-知插件主入口。

功能：
1. 自动检索记忆并注入 LLM 上下文
2. 主动学习新知识（关键词触发 + LLM 工具调用）
3. 按用户/群聊双层隔离的 SQLite 记忆库
4. 质疑时多源交叉验证 + LLM 自辩论 + 版本化
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import time
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .plugin_logger import logger

from .bili_source import BiliSource
from .embedder import Embedder
from .models import SCOPE_GLOBAL, SCOPE_GROUP, SCOPE_PRIVATE, Scope
from .refiner import KnowledgeRefiner
from .runtime import (
    BackgroundTaskHost,
    ExternalSearchController,
    build_missing_comparison_instruction,
    get_request_learning_state,
    mark_request_learning_hinted,
    should_apply_domain_restriction,
)
from .searcher import WebSearcher
from .slang_capture import extract_candidates
from .storage import MemoryStore
from .triggers import CHALLENGE_PATTERNS
from .tools import create_tools
from .verifier import Verifier

# v1.1.5.0：架构重构 —— 统一服务层
from .config_manager import ConfigManager
from .llm_service import LLMService
from .importer import Importer  # noqa: F811

# v1.2.9：main.py 分层重构 —— 无装饰器的方法按职责拆到三个 mixin。
# 带 @register / @filter 的方法必须留在本文件的类里，AstrBot 靠扫描装饰器注册。
# 共享常量统一放 constants.py，避免 mixin 反向 import main 形成循环导入。
from .constants import (
    KNOWLEDGE_CONTRACT_NAME,
    KNOWLEDGE_CONTRACT_VERSION,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    _BRIDGE_RECALL_MAX_TOP_K,
    _EXTERNAL_SEARCH_DEADLINE_SECONDS,
    _EXTERNAL_SEARCH_FIRST_RESULT_GRACE_SECONDS,
    _MEMORY_INJECT_MAX_COUNT,
    _RETRIEVAL_CONCURRENCY,
    _TOOL_NAMES,
)
from .learning import LearningMixin
from .retrieval import RetrievalMixin
from .web_api import _WEB_AVAILABLE, WebApiMixin, _BufferHandler

# 运行时检测 on_llm_response hook 是否可用（不可用时降级为 on_llm_request 内嵌 References）
_ON_LLM_RESPONSE_AVAILABLE = callable(getattr(filter, "on_llm_response", None))


@register(
    "astrbot_plugin_active_learner",
    "凌溪",
    "凝心溯溪-知，知识学习、检索与验证，支持自动上下文注入、多源学习、统一记忆池与版本管理",
    PLUGIN_VERSION,
    "https://github.com/qsbb/astrbot_plugin_active_learner",
)
class ActiveLearnerPlugin(WebApiMixin, RetrievalMixin, LearningMixin, Star):
    """凝心溯溪-知：面向知识学习、检索与验证的插件。"""

    # ---------- 生命周期 ----------

    def __init__(self, context: Context):
        super().__init__(context)
        # 兼容多种 config 注入方式：
        # 1. AstrBot 新版：self.config 自动注入
        # 2. 旧版：context.get_config() 返回全局配置，需取插件子键
        # 3. 兜底：空字典
        cfg = getattr(self, "config", None) or {}
        # AstrBot 注入的 config 是 AstrBotConfig 实例，带 save_config() 可写回
        # 插件配置页对应的持久化文件。下面 self.config 会被替换成合并后的普通
        # dict，因此必须先把原生对象单独存起来，否则管理页的修改无法回写 AstrBot，
        # 会导致 overlay 永久压制插件配置页（页面改了却完全不生效）。
        self._native_config = cfg if hasattr(cfg, "save_config") else None
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
        self.config = cfg  # 统一保存，供 tools.py 等模块读取

        # 存储层
        db_path = StarTools.get_data_dir() / "memory.db"
        self._db_path = db_path
        self.store = MemoryStore(
            db_path=db_path,
            max_entries=max_entries,
            min_confidence=min_confidence,
        )

        # 搜索器与验证器
        self.searcher = WebSearcher()
        # 从 AstrBot 配置读取搜索 API（Tavily / BoCha / Brave）
        provider_settings = cfg.get("provider_settings") or {}
        if isinstance(provider_settings, dict):
            self.searcher.configure_from_settings(provider_settings)
        if self.searcher.is_available:
            logger.info(f"搜索器已就绪: provider={self.searcher._provider}")
        else:
            logger.info("搜索器未配置 API key，验证将使用 LLM-only 模式")
        self.bili_source = BiliSource(context)
        self.verifier = Verifier(self)
        self._retrieval_semaphore = asyncio.Semaphore(_RETRIEVAL_CONCURRENCY)
        self._external_search = ExternalSearchController(
            concurrency=3,
            source_timeouts={"web": 8.0, "bilibili": 6.0},
            total_deadline=_EXTERNAL_SEARCH_DEADLINE_SECONDS,
            first_result_grace=float(
                cfg.get(
                    "external_search_first_result_grace_seconds",
                    _EXTERNAL_SEARCH_FIRST_RESULT_GRACE_SECONDS,
                )
            ),
        )
        self._background_tasks = BackgroundTaskHost()

        # Phase 1：精炼器 + 自管设置
        self._cfg_llm_provider_id = (cfg.get("llm_provider_id") or "").strip()
        if not self._cfg_llm_provider_id:
            # 诊断：列出 cfg 中所有和 provider/llm 相关的 key
            provider_keys = {
                k: (str(v)[:80] if v else repr(v))
                for k, v in cfg.items()
                if any(x in k.lower() for x in ("provider", "llm", "model"))
            }
            logger.warning(
                f"llm_provider_id 为空! cfg 中 provider 相关字段: {provider_keys}"
            )
        self.refiner = KnowledgeRefiner(self)

        # v1.1.5.0：统一服务层。ConfigManager 独占 Dashboard 配置文件，
        # 避免两个缓存同时读取同一文件后产生运行时旧值。
        self.config_manager = ConfigManager(
            StarTools.get_data_dir(), cfg, native_config=self._native_config
        )
        cfg = self.config_manager.all()
        self.config = cfg
        self.llm_service = LLMService(self)
        self.importer = Importer(self)

        # 日志缓冲区：捕获本插件最近 200 条日志
        # 严格隔离：清除可能被 AstrBot 框架挂到本 logger 上的 handler，
        # 防止插件日志泄漏到 AstrBot 主日志界面，也防止 AstrBot 日志反向污染本插件缓冲区。
        self._log_buffer: collections.deque = collections.deque(maxlen=200)
        self._log_handler = _BufferHandler(self._log_buffer)
        self._log_handler.setLevel(logging.INFO)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        )
        # 诊断：记录清除前的 handler 状态，便于排查 AstrBot 是否往本 logger 挂了 handler
        _before_handlers = list(logger.handlers)
        # 仅保留 NullHandler（兜底），移除所有其他 handler（含 AstrBot 可能挂的 StreamHandler 等）
        logger.handlers = [
            h for h in logger.handlers if isinstance(h, logging.NullHandler)
        ]
        # 强制不传播：防止 AstrBot 框架重置 propagate 导致插件日志泄漏到根 logger
        logger.propagate = False
        logger.addHandler(self._log_handler)
        if len(_before_handlers) > 1:
            logger.info(
                f"日志隔离：已移除 {len(_before_handlers) - 1} 个非本插件 handler，"
                f"当前仅保留 _BufferHandler + NullHandler，propagate={logger.propagate}"
            )

        # v1.1.2.0：向量混合检索配置
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
        self._context_inject_count = max(
            1, min(_MEMORY_INJECT_MAX_COUNT, int(cfg.get("context_inject_count", 3)))
        )
        # priority boost 动态衰减：命中关心领域重置为 max，未命中则逐步衰减到 min
        self._priority_boost_max = float(cfg.get("priority_boost_max", 1.3))
        self._priority_boost_min = float(cfg.get("priority_boost_min", 1.0))
        self._priority_boost_decay = float(cfg.get("priority_boost_decay", 0.85))
        self._priority_boost = (
            self._priority_boost_max if self._priority_topics else 1.0
        )

        # 关键词提示开关
        self._enable_active_learn_hint = bool(cfg.get("enable_active_learn_hint", True))
        # v1.1.4.7：学习权重
        self._learn_weight = max(0.0, min(1.0, float(cfg.get("learn_weight", 0.7))))
        # v1.1.4.7：搜索返回条数
        self._search_top_k = max(1, min(20, int(cfg.get("search_top_k", 5))))
        # v1.1.4.7：默认置信度
        self._default_confidence = max(
            0.1, min(1.0, float(cfg.get("default_confidence", 0.6)))
        )
        # v1.1.4.7：分块参数
        self._chunk_size = max(100, min(5000, int(cfg.get("chunk_size", 500))))
        self._chunk_overlap = max(0, min(1000, int(cfg.get("chunk_overlap", 50))))
        # 主动学习追踪状态绑定到每个请求 event，避免并发会话互相覆盖。
        # 保留同名属性供旧版扩展读取，但核心流程不再依赖它们。
        self._active_learn_hinted = False
        self._active_learn_was_called = False
        # v1.1.4.9：后置学习节流
        self._last_post_learn: dict[str, float] = {}

        # v1.1.11.0：关心领域主动学习任务状态（单例，避免并发）
        self._priority_learn_task: Optional[dict] = None

        # v1.1.4.0：群黑话被动捕获 + 定时批量学习（通过 on_llm_request 捕获）
        self._enable_slang_capture = bool(cfg.get("enable_slang_capture", False))
        self._slang_interval_hours = float(cfg.get("slang_capture_interval_hours", 24))
        self._slang_batch_size = int(cfg.get("slang_capture_batch_size", 5))
        self._slang_min_occurrences = int(cfg.get("slang_capture_min_occurrences", 2))
        self._slang_scope_only_group = bool(
            cfg.get("slang_capture_scope_only_group", True)
        )
        self._slang_last_check: dict[
            str, float
        ] = {}  # 进程内节流：scope_key → 上次检查时间

        # v1.2.0.0：联网搜索与知识领域控制
        self._enable_web_search = bool(cfg.get("enable_web_search", True))
        self._web_search_only_highest_priority = bool(
            cfg.get("web_search_only_highest_priority", False)
        )
        self._knowledge_source_priority = self._parse_source_priority(
            cfg.get("knowledge_source_priority", "web,bilibili")
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

        # 注册 LLM 工具。先清理热重载可能残留的旧实例。
        self._tools = []
        try:
            self._cleanup_llm_tools()
            tools = create_tools(self)
            if tools:
                self._tools = tools
                self.context.add_llm_tools(*tools)
                logger.info(
                    f"已注册 {len(tools)} 个 LLM 工具: {[t.name for t in tools]}"
                )
        except Exception as e:
            logger.error(f"注册 LLM 工具失败: {e}")

        # 诊断：启动时打印数据库状态
        try:
            total = self.store.count_all()
            logger.info(
                f"凝心溯溪-知 v{PLUGIN_VERSION} 已加载 | max_entries={max_entries} | "
                f"bili={'on' if self.bili_source.is_available() else 'off'} | "
                f"db={db_path} | 记忆={total}条 | "
                f"schema=v{self.store._schema_version} | "
                f"learn_weight={self._learn_weight} | "
                f"search_top_k={self._search_top_k} | "
                f"default_conf={self._default_confidence}"
            )
        except Exception as e:
            logger.warning(f"数据库状态检查失败: {e}")

        # 清理超过 30 天的 token 用量记录，防止无限增长
        try:
            deleted = self.store.cleanup_old_token_usage(days=30)
            if deleted > 0:
                logger.info(f"已清理 {deleted} 条过期 token 用量记录（>30天）")
        except Exception as e:
            logger.debug(f"token 用量清理失败（不影响运行）: {e}")

        # v1.1.4.0：群黑话捕获特性状态
        if self._enable_slang_capture:
            logger.info(
                f"群黑话捕获已启用 | interval={self._slang_interval_hours}h | "
                f"batch_size={self._slang_batch_size} | min_occ={self._slang_min_occurrences} | "
                f"scope_only_group={self._slang_scope_only_group}"
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

    # ---------- 跨插件知识桥接（公开契约） ----------

    def knowledge_contract(self) -> dict:
        """声明知识桥接契约，供消费方（序）启动时校验。

        消费方按 major 版本判断兼容性：major 不一致必须停用桥接并告警，
        不要退化成 duck-typing 探测——那会把失配变成静默失效。
        """
        return {
            "name": KNOWLEDGE_CONTRACT_NAME,
            "version": KNOWLEDGE_CONTRACT_VERSION,
            "plugin": PLUGIN_NAME,
            "capabilities": ("recall",),
        }

    @staticmethod
    def _parse_bridge_scope(scope: str) -> Scope:
        """把桥接传入的字符串作用域解析为内部 Scope。

        接受 `group:123` / `private:u1` / `global`；空串或无法识别时回落到 global，
        因为序的入群审核发生在成员进群前，此时没有可信的群/私聊上下文。
        """
        raw = (scope or "").strip()
        if not raw:
            return Scope(SCOPE_GLOBAL, "global")
        if ":" in raw:
            kind, _, ident = raw.partition(":")
            kind = kind.strip().lower()
            ident = ident.strip()
            if kind == SCOPE_GROUP and ident:
                return Scope(SCOPE_GROUP, ident)
            if kind == SCOPE_PRIVATE and ident:
                return Scope(SCOPE_PRIVATE, ident)
        return Scope(SCOPE_GLOBAL, "global")

    async def recall(self, query: str, scope: str = "", top_k: int = 5) -> list[dict]:
        """只读检索记忆库，返回契约约定的证据字典列表。

        契约 1.0 的每条结果含 content / source / score / topic / verified /
        confidence 字段。只读：不写库、不计访问次数、不触发学习。

        失败或无命中一律返回空列表——消费方（序的入群审核）把空结果视为
        unavailable 并保持待审，不能因为桥接异常而放行或拒绝。
        """
        text = (query or "").strip()
        if not text:
            return []
        limit = max(1, min(int(top_k or 5), _BRIDGE_RECALL_MAX_TOP_K))
        try:
            hits = await self._search_memory_once(self._parse_bridge_scope(scope), text)
        except Exception as exc:
            logger.warning(f"[知] 知识桥接检索失败: {exc}")
            return []

        results: list[dict] = []
        for hit in (hits or [])[:limit]:
            entry = getattr(hit, "entry", None)
            if entry is None:
                continue
            results.append(
                {
                    "content": entry.content,
                    "source": entry.source,
                    "score": float(getattr(hit, "score", 0.0)),
                    "topic": entry.topic,
                    "verified": bool(entry.verified),
                    "confidence": float(entry.confidence),
                }
            )
        return results

    async def terminate(self):
        try:
            await self._background_tasks.close()
        except Exception:
            pass
        self._cleanup_llm_tools()
        try:
            self.store.close()
        except Exception:
            pass
        logger.info("ActiveLearner 已卸载，后台任务与 LLM 工具已回收，记忆已持久化")

    def _cleanup_llm_tools(self) -> None:
        """清理本插件工具，避免热重载后残留同名旧实例。"""
        for method_name in (
            "remove_llm_tool",
            "remove_llm_tools",
            "unregister_llm_tool",
        ):
            method = getattr(self.context, method_name, None)
            if not callable(method):
                continue
            try:
                for name in _TOOL_NAMES:
                    method(name)
                return
            except Exception as exc:
                logger.debug(f"通过 {method_name} 清理 LLM 工具失败: {exc}")

        try:
            manager = getattr(self.context, "_func_tool_manager", None) or getattr(
                self.context, "func_tool_manager", None
            )
            tools = getattr(manager, "tools", None)
            if not isinstance(tools, list):
                return
            manager.tools = [
                tool for tool in tools if getattr(tool, "name", "") not in _TOOL_NAMES
            ]
        except Exception as exc:
            logger.debug(f"清理残留 LLM 工具失败: {exc}")

    def _create_background_task(self, awaitable, *, name: str):
        """统一托管插件后台任务，卸载时取消并等待回收。"""
        return self._background_tasks.create(awaitable, name=name)

    def _get_admin_ids(self) -> set[str]:
        """从 AstrBot 全局配置 + 插件配置中读取管理员名单。"""
        admins: set[str] = set()
        # 1. 从插件配置读取 admin_ids（逗号分隔字符串，可在 Dashboard 设置页修改）
        cfg = getattr(self, "config", None) or {}
        raw = (cfg.get("admin_ids") or "").strip()
        if raw:
            for part in raw.split(","):
                pid = part.strip()
                if pid:
                    admins.add(pid)
        # 2. 从 AstrBot 全局配置读取 wl_admin
        if hasattr(self.context, "get_config"):
            try:
                raw_conf = self.context.get_config()
                if isinstance(raw_conf, dict):
                    val = raw_conf.get("wl_admin", [])
                    if isinstance(val, list):
                        admins.update(str(a) for a in val)
            except Exception:
                pass
        # 3. 兜底：从当前 config 中读 wl_admin
        extra = cfg.get("wl_admin", [])
        if isinstance(extra, list):
            admins.update(str(a) for a in extra)
        return admins

    def _is_admin_user(self, event) -> bool:
        """判断当前用户是否为 AstrBot 管理员。无管理员配置时默认所有人都可学习。"""
        admins = self._get_admin_ids()
        if not admins:
            return True  # 无管理员名单 → 向后兼容，所有人可学
        try:
            uid = event.get_sender_id()
            return bool(uid) and str(uid) in admins
        except Exception:
            return True

    def _is_admin_user_strict(self, event) -> bool:
        """判断当前用户是否为管理员（用于权限绕过）。

        与 _is_admin_user 的区别：无管理员配置时不视为管理员，
        确保跨领域限制等安全开关对普通用户生效。
        """
        admins = self._get_admin_ids()
        if not admins:
            return False
        try:
            uid = event.get_sender_id()
            return bool(uid) and str(uid) in admins
        except Exception:
            return False

    # ---------- 上下文注入 + 质疑检测 + 主动学习提示（合并钩子） ----------

    # priority=700：凝心溯溪系列 on_llm_request 区间为 200-800，数值越大越先执行。
    # 顺序为 序 800（身份安全边界）> 知 700（知识事实）> 情 600（表达约束）>
    # 言 500（沉默判断）。知识注入必须晚于身份边界，否则安全层可能被事实注入抢先。
    @filter.on_llm_request(priority=700)
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

        # 1. 普通请求先做全库 FTS；命中不足时才限时 Embedding + 混合检索。
        retrieval_started = time.perf_counter()
        retrieval_mode = "fts"
        comparison_status: dict[str, bool] = {}
        try:
            hits, retrieval_mode, comparison_status = await self._retrieve_memory(
                scope, msg
            )
            if comparison_status and logger.isEnabledFor(logging.DEBUG):
                logger.debug("对比对象覆盖: %s", comparison_status)

            await asyncio.to_thread(self.store.track_search_hits, hits)
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
            logger.warning(f"记忆检索失败: {e}")
            hits = []

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "记忆检索阶段总计: mode=%s, hits=%d, elapsed_ms=%.1f",
                retrieval_mode,
                len(hits),
                (time.perf_counter() - retrieval_started) * 1000,
            )
        logger.info(
            f"记忆检索: {len(hits)} hits (mode: {retrieval_mode}, "
            f"scope: {scope}, query: {msg[:50]})"
        )

        injection_started = time.perf_counter()
        memory_parts, injected_hits = self._build_memory_injection(hits)
        parts.extend(memory_parts)
        # 把实际注入的记忆 ID 挂到 event 上，供 on_llm_response footer 使用
        injected_ids = [h.entry.id for h in injected_hits]
        try:
            object.__setattr__(event, "_injected_memory_ids", injected_ids)
        except Exception:
            pass
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "记忆注入裁剪: selected=%d/%d, chars=%d, elapsed_ms=%.1f",
                len(injected_hits),
                len(hits),
                sum(len(part) for part in memory_parts),
                (time.perf_counter() - injection_started) * 1000,
            )

        if memory_parts:
            parts.append(
                "【以上为内部知识参考，请基于上述内容作答，不要在回复中输出【内部知识】标记】"
            )
            parts.append("（如发现错误请指出，可调用 verify_knowledge 验证）")
            if not _ON_LLM_RESPONSE_AVAILABLE and hits:
                logger.info(
                    "注入记忆: "
                    + " | ".join(
                        f"[{h.entry.id}] {h.entry.topic} ({h.entry.confidence:.0%})"
                        for h in hits
                    )
                )

        missing_comparison_objects = [
            obj for obj, covered in comparison_status.items() if not covered
        ]
        missing_instruction = build_missing_comparison_instruction(
            missing_comparison_objects
        )
        if missing_instruction:
            parts.append(missing_instruction)
            mark_request_learning_hinted(event)
            self._active_learn_hinted = True  # 兼容旧版扩展读取

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

        # v1.2.0.0：知识领域范围控制
        # 本地记忆已有相关知识时不受影响；开启跨领域时不限制。
        # 管理员是否排除取决于 cross_domain_exclude_admin 配置。
        is_admin = self._is_admin_user_strict(event)
        admin_bypass = is_admin and self._cross_domain_exclude_admin
        domain_restricted = should_apply_domain_restriction(
            admin_bypass=admin_bypass,
            enable_cross_domain=self._enable_cross_domain,
            domains_configured=bool(self._knowledge_domain_scope),
            has_hits=bool(hits),
            query_in_scope=self._query_in_domain_scope(msg),
            requires_missing_search=bool(missing_instruction),
        )
        if domain_restricted:
            removed = self._strip_search_tools(req)
            parts.append(
                "【领域限制】用户的问题不在你的知识领域范围内，且本地记忆中没有相关记录。"
                "请不要尝试搜索网络、调用工具或编造答案，"
                "直接明确回复你不知道/没玩过/没见过。"
            )
            logger.info(
                f"领域限制：无本地记忆且非兴趣领域，已移除 {removed} 个搜索工具 (query: {msg[:50]})"
            )

        # 3. 主动学习提示（v1.1.5.0：所有用户按 learn_weight 触发，不限于管理员）
        # 缺失对象提示本身就是强制学习请求，不受通用主动学习开关影响。
        if missing_instruction:
            pass
        elif not domain_restricted and self._enable_active_learn_hint:
            if not hits:
                hint = self._get_learn_prompt()
                if hint:
                    state = get_request_learning_state(event)
                    if state is not None:
                        state.hinted = True
                    self._active_learn_hinted = True  # 兼容旧版扩展读取
                    parts.append(hint)
                    logger.info(
                        f"ℹ️ 已注入学习提示 (weight={self._learn_weight}, scope: {scope})"
                    )
                else:
                    logger.info(f"ℹ️ learn_weight=0，跳过主动学习 (scope: {scope})")
            elif self._learn_weight >= 0.5:
                # 即使有记忆命中，也注入简短工具提醒
                parts.append(
                    "（如果用户提供了你原本不掌握的新知识点，可调用 search_and_learn 工具学习）"
                )
        else:
            state = get_request_learning_state(event, create=False)
            if state is not None:
                state.hinted = False
            self._active_learn_hinted = False

        # 4. 注入
        if not parts:
            return

        # 行为规范
        if domain_restricted:
            parts.append(
                "[行为规范] 请仅按上面的领域限制直接回复，不要调用任何工具或搜索网络。"
            )
        else:
            parts.append(
                "[行为规范] 有记忆就直接答；需调用工具时直接调用，"
                '不要在回复里预告"让我查查看"、"我搜一下"、"让我想想"等话术。'
            )

        injection = "\n".join(parts)
        # 标签汇总，让日志一眼看出注入了什么
        tags = []
        if hits:
            tags.append(f"{len(hits)}条记忆")
        if is_challenge and hits:
            tags.append("质疑提示")
        if domain_restricted:
            tags.append("领域限制")
        request_state = get_request_learning_state(event, create=False)
        if request_state is not None and request_state.hinted:
            tags.append("学习提示")
        try:
            if hasattr(req, "extra_user_content_parts"):
                from astrbot.core.agent.message import TextPart

                req.extra_user_content_parts.append(TextPart(text=injection))
                logger.info(f"注入上下文 [{'/'.join(tags)}] (scope: {scope})")
            else:
                # 兜底：修改 system_prompt（会破坏 prompt 缓存，仅降级用）
                req.system_prompt = (req.system_prompt or "") + "\n" + injection
                logger.warning(
                    "extra_user_content_parts 不可用，降级用 system_prompt 注入"
                )
        except Exception as e:
            logger.error(f"上下文注入失败: {e}")

        # 5. 群黑话被动捕获（v1.1.5.0：通过 on_llm_request 降级，不依赖 on_message 钩子）
        if self._enable_slang_capture:
            try:
                if self._slang_scope_only_group and scope.type != "group":
                    pass  # 仅捕获群消息
                else:
                    candidates = extract_candidates(msg)
                    if candidates:
                        for phrase, ctx in candidates:
                            await asyncio.to_thread(
                                self.store.add_slang_candidate, scope, phrase, ctx
                            )
                        self._maybe_trigger_batch_learn(scope)
            except Exception as e:
                logger.debug(f"slang 捕获失败: {e}")

    # v1.1.4.9：on_llm_response hook（+ 后置异步学习分析，不依赖 LLM 主动调工具）
    if _ON_LLM_RESPONSE_AVAILABLE:
        # priority=700：与本插件 on_llm_request 保持同档，早于情 600、言 500。
        @filter.on_llm_response(priority=700)  # type: ignore[misc]
        async def on_llm_response(self, event: AstrMessageEvent, response):
            """追踪主动学习提示 + 回复完成后置学习分析。"""
            # 请求级追踪，避免并发会话共享实例布尔值造成串扰。
            state = get_request_learning_state(event, create=False)
            if state is not None and state.hinted:
                state.hinted = False
                if state.called:
                    logger.info("主动学习已执行并存入记忆库")
                else:
                    logger.info(
                        "主动学习提示已注入，LLM 未调用 search_and_learn（无需学习）"
                    )

            # 后置学习由插件托管，不阻塞回复，卸载时统一回收。
            self._create_background_task(
                self._post_learn_analysis_bg(event, response),
                name="active-learner-post-learn",
            )

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
        entries, total, total_pages = self.store.list_memories(
            scope, page=page, per_page=10
        )
        if not entries:
            yield event.plain_result("📝 当前作用域记忆库为空")
            return
        lines = [f"📝 记忆列表 ({page}/{total_pages}页，共{total}条)\n"]
        for i, e in enumerate(entries, (page - 1) * 10 + 1):
            v = "✅" if e.verified else "❓"
            lines.append(
                f"{i}. {v} {e.topic} (置信度{e.confidence:.0%}, 访问{e.access_count}次)"
            )
        lines.append("\n使用 /memory list <页码> 翻页")
        yield event.plain_result("\n".join(lines))

    @memory_cmd.command("search")
    async def memory_search(self, event: AstrMessageEvent, keyword: str):
        """搜索记忆"""
        scope = Scope.from_event(event)
        hits = self.store.search(scope, keyword, top_k=self._search_top_k)
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
            yield event.plain_result(
                f"🗑️ 已删除关于「{deleted.topic}」的记忆（版本已留痕）"
            )
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
        export_path = (
            StarTools.get_data_dir() / f"memory_export_{scope.type}_{scope.id}.json"
        )
        try:
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            yield event.plain_result(f"📦 已导出 {len(data)} 条记忆到:\n{export_path}")
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
