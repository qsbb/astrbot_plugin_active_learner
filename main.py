"""AstrBot 主动学习记忆插件主入口。

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
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .plugin_logger import logger

try:
    from astrbot.api.web import error_response, file_response, json_response, request
    _WEB_AVAILABLE = True
except ImportError:  # AstrBot < v4.26 没有 Plugin Pages 支持
    _WEB_AVAILABLE = False
    error_response = file_response = json_response = request = None  # type: ignore

from .bili_source import BiliSource
from .embedder import Embedder
from .models import Scope, make_chunk_id, now_ts
from .refiner import KnowledgeRefiner
from .searcher import WebSearcher
from .settings_store import SettingsStore
from .slang_capture import (
    build_batch_prompt,
    extract_candidates,
    parse_batch_response,
)
from .storage import MemoryStore
from .triggers import ACTIVE_LEARN_PATTERNS, CHALLENGE_PATTERNS
from .tools import create_tools
from .verifier import Verifier

# v1.1.5.0：架构重构 —— 统一服务层
from .config_manager import ConfigManager
from .llm_service import LLMService
from .importer import Importer  # noqa: F811

PLUGIN_NAME = "astrbot_plugin_active_learner"

# 运行时检测 on_llm_response hook 是否可用（不可用时降级为 on_llm_request 内嵌 References）
_ON_LLM_RESPONSE_AVAILABLE = callable(getattr(filter, "on_llm_response", None))


@register(
    "astrbot_plugin_active_learner",
    "lingxi",
    "主动学习记忆：自动检索注入、主动多源学习、双层隔离 SQLite 记忆库、质疑多源验证",
    "1.1.6.5",
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
        self._settings = SettingsStore(
            StarTools.get_data_dir() / "active_learner_settings.json"
        )
        # v1.1.4.8：Dashboard 设置覆盖 AstrBot 配置，确保两边修改都生效
        dash_cfg = self._settings.all()
        if isinstance(dash_cfg, dict):
            cfg.update({k: v for k, v in dash_cfg.items() if v is not None})

        # v1.1.5.0：统一服务层
        self.config_manager = ConfigManager(
            StarTools.get_data_dir(), cfg
        )
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
        self._context_inject_count = max(1, min(10, int(cfg.get("context_inject_count", 3))))
        # priority boost 动态衰减：命中关心领域重置为 max，未命中则逐步衰减到 min
        self._priority_boost_max = float(cfg.get("priority_boost_max", 1.3))
        self._priority_boost_min = float(cfg.get("priority_boost_min", 1.0))
        self._priority_boost_decay = float(cfg.get("priority_boost_decay", 0.85))
        self._priority_boost = self._priority_boost_max if self._priority_topics else 1.0

        # 关键词提示开关
        self._enable_active_learn_hint = bool(cfg.get("enable_active_learn_hint", True))
        # v1.1.4.7：学习权重
        self._learn_weight = max(0.0, min(1.0, float(cfg.get("learn_weight", 0.7))))
        # v1.1.4.7：搜索返回条数
        self._search_top_k = max(1, min(20, int(cfg.get("search_top_k", 5))))
        # v1.1.4.7：默认置信度
        self._default_confidence = max(0.1, min(1.0, float(cfg.get("default_confidence", 0.6))))
        # v1.1.4.7：分块参数
        self._chunk_size = max(100, min(5000, int(cfg.get("chunk_size", 500))))
        self._chunk_overlap = max(0, min(1000, int(cfg.get("chunk_overlap", 50))))
        # 主动学习追踪标记（on_llm_request ↔ on_llm_response）
        self._active_learn_hinted = False
        self._active_learn_was_called = False
        # v1.1.4.9：后置学习节流
        self._last_post_learn: dict[str, float] = {}

        # v1.1.4.0：群黑话被动捕获 + 定时批量学习（通过 on_llm_request 捕获）
        self._enable_slang_capture = bool(cfg.get("enable_slang_capture", False))
        self._slang_interval_hours = float(cfg.get("slang_capture_interval_hours", 24))
        self._slang_batch_size = int(cfg.get("slang_capture_batch_size", 5))
        self._slang_min_occurrences = int(cfg.get("slang_capture_min_occurrences", 2))
        self._slang_scope_only_group = bool(
            cfg.get("slang_capture_scope_only_group", True)
        )
        self._slang_last_check: dict[str, float] = {}  # 进程内节流：scope_key → 上次检查时间

        # 注册 LLM 工具
        self._tools = []
        try:
            tools = create_tools(self)
            if tools:
                self._tools = tools
                self.context.add_llm_tools(*tools)
                logger.info(f"已注册 {len(tools)} 个 LLM 工具: {[t.name for t in tools]}")
        except Exception as e:
            logger.error(f"注册 LLM 工具失败: {e}")

        # 诊断：启动时打印数据库状态
        try:
            total = self.store.count_all()
            logger.info(
                f"ActiveLearner v1.1.6.5 已加载 | max_entries={max_entries} | "
                f"bili={'on' if self.bili_source.is_available() else 'off'} | "
                f"db={db_path} | 记忆={total}条 | "
                f"schema=v{self.store._schema_version} | "
                f"learn_weight={self._learn_weight} | "
                f"search_top_k={self._search_top_k} | "
                f"default_conf={self._default_confidence}"
            )
        except Exception as e:
            logger.warning(f"数据库状态检查失败: {e}")

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

    def _get_learn_prompt(self) -> str | None:
        """根据 learn_weight 返回对应强度的学习提示。None=不注入。"""
        w = self._learn_weight
        if w <= 0.0 or not self._enable_active_learn_hint:
            return None
        if w < 0.4:
            return ("[学习提示] 如果这是你原本不掌握的新知识点（对方在科普），"
                    "可考虑调用 search_and_learn 工具学习。")
        if w < 0.7:
            return ("[学习提示] 如果这是对方在科普新知识，"
                    "建议调用 search_and_learn 工具存入记忆库，以免以后遗忘。")
        # 0.7 ~ 0.9（默认 0.7）
        if w < 1.0:
            return ("[学习提示] 你刚刚收到了一条新信息。如果这是你原本不掌握的知识点"
                    "（即对方在向你科普/教你新东西），请立即调用 search_and_learn"
                    " 工具将其存入记忆库，以免以后遗忘。"
                    "如果信息与已有认知冲突，同样调用 search_and_learn 核实后学习。")
        # 1.0：激进模式
        return (
            "[学习提示] **重要指令**：请判断以下对话是否包含新知识点"
            "（用户可能在科普、纠正或教你新东西）。如果是，请**立即调用** "
            "search_and_learn 工具搜索并存入记忆库。\n"
            "判断标准：\n"
            "- 出现你不熟悉的人名/术语/概念 → 调用\n"
            "- 用户说「其实是」「是XX不是XX」等纠正性表述 → 调用\n"
            "- 用户主动介绍某个知识 → 调用\n"
            "- 你自己完全确定且明确知道的内容 → 不调用\n"
            "调用后无需告知用户，直接继续回复即可。"
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

        # 1. 检索记忆（v1.1.2.0：混合检索 FTS5 + 向量）
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
            logger.warning(f"记忆检索失败: {e}")
            hits = []

        logger.info(f"记忆检索: {len(hits)} hits (scope: {scope}, query: {msg[:50]})")

        # 把注入的记忆 ID 挂到 event 上，供 on_llm_response footer 使用
        injected_ids = [h.entry.id for h in hits]
        try:
            object.__setattr__(event, "_injected_memory_ids", injected_ids)
        except Exception:
            pass

        for h in hits:
            entry = h.entry
            v_tag = "✅已验证" if entry.verified else f"⚠️置信度{entry.confidence:.0%}"
            parts.append(
                f"【内部知识 #{entry.id} | {entry.topic} | {v_tag}】{entry.content}"
            )

        if parts:
            parts.append(
                "【以上为内部知识参考，请基于上述内容作答，不要在回复中输出【内部知识】标记】"
            )
            parts.append("（如发现错误请指出，可调用 verify_knowledge 验证）")
            if not _ON_LLM_RESPONSE_AVAILABLE and hits:
                logger.info(
                    "注入记忆: " + " | ".join(
                        f"[{h.entry.id}] {h.entry.topic} ({h.entry.confidence:.0%})"
                        for h in hits
                    )
                )

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

        # 3. 主动学习提示（v1.1.5.0：所有用户按 learn_weight 触发，不限于管理员）
        if self._enable_active_learn_hint:
            if not hits:
                hint = self._get_learn_prompt()
                if hint:
                    self._active_learn_hinted = True
                    parts.append(hint)
                    logger.info(
                        f"ℹ️ 已注入学习提示 (weight={self._learn_weight}, scope: {scope})"
                    )
                else:
                    logger.info(
                        f"ℹ️ learn_weight=0，跳过主动学习 (scope: {scope})"
                    )
            elif self._learn_weight >= 0.5:
                # 即使有记忆命中，也注入简短工具提醒
                parts.append(
                    "（如果用户提供了你原本不掌握的新知识点，可调用 search_and_learn 工具学习）"
                )
        else:
            self._active_learn_hinted = False

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
        if self._active_learn_hinted:
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

        @filter.on_llm_response()  # type: ignore[misc]
        async def on_llm_response(self, event: AstrMessageEvent, response):
            """追踪主动学习提示 + 回复完成后置学习分析。"""
            # v1.1.2.0：追踪主动学习提示是否被 LLM 调用
            if getattr(self, "_active_learn_hinted", False):
                self._active_learn_hinted = False
                if getattr(self, "_active_learn_was_called", False):
                    self._active_learn_was_called = False
                    logger.info("✅ 主动学习已执行并存入记忆库")
                else:
                    logger.info("ℹ️ 主动学习提示已注入，LLM 未调用 search_and_learn（无需学习）")

            # v1.1.4.9：后置异步学习分析（回复完后自动分析是否需要记忆）
            try:
                await self._post_learn_analysis(event, response)
            except Exception as e:
                logger.debug(f"后置学习分析异常: {e}")

    async def _post_learn_analysis(self, event: AstrMessageEvent, response) -> None:
        """回复完成后，异步分析对话是否包含可学习知识点，自动存入记忆库。"""
        # 1. 开关检查
        if not self._enable_active_learn_hint or self._learn_weight <= 0.0:
            logger.debug(f"后置学习跳过: enable={self._enable_active_learn_hint}, weight={self._learn_weight}")
            return

        # 2. 提取用户消息
        user_msg = ""
        try:
            user_msg = (event.get_message_str() or "").strip()
        except Exception as e:
            logger.debug(f"后置学习跳过: 提取用户消息失败 {e}")
            return
        if not user_msg or len(user_msg) < 5:
            logger.debug(f"后置学习跳过: 用户消息过短 ({len(user_msg) if user_msg else 0}字)")
            return

        # 3. 管理员：只有包含明确学习意图（记住/学习/保存等）时才分析
        if self._is_admin_user(event):
            learn_intent = re.search(
                r"(记住|记下来|学习|学一下|记一下|保存|存起来|存下|收录|记到知识|记到记忆|记入|收录到)",
                user_msg,
            )
            if not learn_intent:
                logger.debug(f"后置学习跳过: 管理员消息无明确学习意图")
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
            logger.debug(f"后置学习分析: 非管理员用户，自动分析")

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
            logger.debug(f"后置学习跳过: LLM 返回 learn 但缺 topic/content (topic={topic!r}, content={content!r})")
            return

        # 8. 存入记忆
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
            logger.info(f"✅ 后置学习已存入记忆: {topic} (id: {entry.id}, scope: {scope})")
        except Exception as e:
            logger.error(f"❌ 后置学习存储失败「{topic}」: {e}", exc_info=True)

    # ---------- v1.1.4.0：群黑话定时批量学习（捕获已移至 on_llm_request）----------

    def _maybe_trigger_batch_learn(self, scope: Scope) -> None:
        """节流检查：每 scope 5 分钟最多查一次 DB；满足条件则 asyncio.create_task 触发批量学习。"""
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
            pending = self.store.list_pending_slang(
                scope, limit=self._slang_batch_size
            )
            if len(pending) < self._slang_batch_size:
                return
            # 过滤 occurrences < min_occurrences
            qualified = [
                c for c in pending
                if c["occurrences"] >= self._slang_min_occurrences
            ]
            if len(qualified) < self._slang_batch_size:
                return
            asyncio.create_task(self._async_batch_learn_slang(scope, qualified))
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
                        scope, item["phrase"], item["summary"],
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
        context.register_web_api(
            f"/{PLUGIN_NAME}/config_schema", self._web_config_schema, ["GET"], "获取配置 schema 与当前值"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/debug", self._web_debug, ["GET"], "诊断信息"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/builtin_kb/list", self._web_builtin_kb_list, ["GET"], "列出 AstrBot 内置知识库"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/builtin_kb/<kb_id>/documents",
            self._web_builtin_kb_documents, ["GET"], "列出 KB 内文档",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/builtin_kb/import",
            self._web_builtin_kb_import, ["POST"], "从内置 KB 批量导入",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/logs", self._web_logs, ["GET"], "获取插件日志",
        )

    async def _web_debug(self):
        """返回数据库和插件诊断信息。"""
        embedder_available = False
        embedder_model = ""
        if self.embedder is not None:
            try:
                embedder_available = self.embedder.available
                embedder_model = self.embedder.model_name
            except Exception:
                pass
        return json_response({
            "db_path": str(self._db_path),
            "schema_version": self.store._schema_version,
            "total_memories": self.store.count_all(),
            "scopes": self.store.list_scopes(),
            "embedder_available": embedder_available,
            "embedder_model": embedder_model,
            "priority_topics": self._priority_topics,
            "priority_boost": round(self._priority_boost, 2),
            "tools_registered": [t.name for t in self._tools],
        })

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
            settings_pid = self._settings.get("llm_provider_id") or ""
            cfg_pid = self._cfg_llm_provider_id or ""
            pm = getattr(self.context, "provider_manager", None)
            pm_providers = []
            if pm is not None:
                for p in getattr(pm, "providers", None) or []:
                    pm_providers.append(str(getattr(p, "id", "") or getattr(p, "name", "")))
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
        return json_response({
            "verdict": result.verdict,
            "confidence": result.confidence,
            "content": result.content,
            "reasoning": result.reasoning,
            "sources_count": result.sources_count,
            "sources_consistent": result.sources_consistent,
            "text": result.to_text(),
            "debug_info": result.debug_info,
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
                    (cfg.get("provider_settings") or {}).get(
                        "default_provider_id", ""
                    )
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

        1. self._settings 中的 llm_provider_id（Dashboard 设置，最高优先级）
        2. self._cfg_llm_provider_id（_conf_schema.json 中的字段）
        3. context.get_current_chat_provider_id(umo=...) （事件 scope 默认）
        4. self._resolve_default_provider_id() （同步兜底）

        每个候选都先经 _provider_exists 校验，避免选了已删除的 provider。
        """
        # 1. Dashboard 设置
        pid = self._settings.get("llm_provider_id") or ""
        if pid:
            if self._provider_exists(pid):
                logger.info(f"provider 解析 [1/4 Dashboard]: {pid!r}")
                return pid
            logger.warning(f"provider 解析 [1/4 Dashboard] 命中但校验失败: {pid!r}")

        # 2. schema 字段
        if self._cfg_llm_provider_id:
            if self._provider_exists(self._cfg_llm_provider_id):
                logger.info(f"provider 解析 [2/4 Schema]: {self._cfg_llm_provider_id!r}")
                return self._cfg_llm_provider_id
            logger.warning(f"provider 解析 [2/4 Schema] 命中但校验失败: {self._cfg_llm_provider_id!r}")

        # 3. 事件 scope 默认（async），尝试调用 get_current_chat_provider_id
        method = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(method):
            try:
                pid = await method(umo=umo) if umo else await method()
                if pid:
                    if self._provider_exists(pid):
                        logger.info(f"provider 解析 [3/4 当前对话默认]: {pid!r} (umo={umo!r})")
                        return pid
                    logger.warning(f"provider 解析 [3/4 当前对话默认] 命中但校验失败: {pid!r}")
            except Exception as e:
                logger.debug(f"provider 解析 [3/4 当前对话默认] 调用异常: {e}")

        # 4. 同步兜底
        fallback = self._resolve_default_provider_id()
        logger.info(f"provider 解析 [4/4 兜底]: {fallback!r} (settings_pid={self._settings.get('llm_provider_id')!r}, cfg_pid={self._cfg_llm_provider_id!r})")
        return fallback

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
        # 兜底：provider_manager 为空时从 cmd_config.json 读取
        if not providers_list:
            _, cfg_providers = self._get_providers_from_config()
            for p in cfg_providers:
                providers_list.append({
                    "id": p["id"],
                    "name": f"{p['model']} ({p['id']})" if p["model"] else p["id"],
                    "type": p["type"],
                })
        current = (
            self._settings.get("llm_provider_id")
            or self._cfg_llm_provider_id
            or self._resolve_default_provider_id()
        )
        return json_response({"providers": providers_list, "current": current})

    async def _web_get_settings(self):
        """返回当前插件设置（含默认值填充）。

        使用 all() 而非 overlay_all()，确保 AstrBot 插件配置页（_conf_schema.json）
        中设置的 llm_provider_id 等字段也能被前端读到。
        """
        data = self.config_manager.all()
        return json_response({
            "llm_provider_id": data.get("llm_provider_id", ""),
            "refine_on_search": bool(data.get("refine_on_search", True)),
            "refine_on_import": bool(data.get("refine_on_import", True)),
            "refine_on_verify": bool(data.get("refine_on_verify", True)),
            "enable_active_learn_hint": bool(data.get("enable_active_learn_hint", True)),
            "learn_weight": float(data.get("learn_weight", 0.7)),
            "admin_ids": str(data.get("admin_ids", "")),
            "search_top_k": int(data.get("search_top_k", 5)),
            "default_confidence": float(data.get("default_confidence", 0.6)),
            "chunk_size": int(data.get("chunk_size", 500)),
            "chunk_overlap": int(data.get("chunk_overlap", 50)),
            "verifier_search_source": str(data.get("verifier_search_source", "auto") or "auto"),
        })

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
        """返回 _conf_schema.json 全量字段 + 当前合并值。

        值优先级：self._settings（Dashboard 设置）→ self.config（schema 默认）
        """
        schema = self._load_schema()
        settings = self._settings.all()
        config = self.config or {}
        fields = []
        for name, spec in schema.items():
            if not isinstance(spec, dict):
                continue
            default = spec.get("default")
            # 当前值：settings 优先，否则用 config（即 schema 默认已合并入 config）
            current_val = settings.get(name, config.get(name, default))
            fields.append({
                "name": name,
                "description": spec.get("description", ""),
                "hint": spec.get("hint", ""),
                "type": spec.get("type", "string"),
                "default": default,
                "value": current_val,
            })
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
                return error_response(
                    f"provider_id '{pid}' 不存在", status_code=400
                )
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

    def _apply_config_to_runtime(self, settings: dict) -> None:
        """把保存后的设置立即应用到运行时变量（无需重启 AstrBot）。

        合并优先级：settings（自管存储）覆盖 self.config（schema 默认）。
        """
        cfg = dict(self.config or {})
        cfg.update({k: v for k, v in settings.items() if v is not None})

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
        self._priority_boost = self._priority_boost_max if self._priority_topics else 1.0

        # 上下文注入条数
        try:
            self._context_inject_count = max(
                1, min(10, int(cfg.get("context_inject_count", 3)))
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
            self._default_confidence = max(0.1, min(1.0, float(cfg.get("default_confidence", 0.6))))
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
            return error_response(result.get("error", "导入失败"), status_code=result.get("status_code", 500))
        return json_response({"ok": True, "entry": result["entry"]})

    async def _web_import_md(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_md(payload)
        if not result.get("ok"):
            return error_response(result.get("error", "导入失败"), status_code=result.get("status_code", 500))
        if "entry" in result:
            return json_response({"ok": True, "entry": result["entry"]})
        return json_response({"ok": True, "batch": result["batch"]})

    async def _web_import_pdf(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_pdf(payload)
        if not result.get("ok"):
            return error_response(result.get("error", "导入失败"), status_code=result.get("status_code", 500))
        return json_response({"ok": True, "batch": result["batch"]})

    async def _web_import_docx(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_docx(payload)
        if not result.get("ok"):
            return error_response(result.get("error", "导入失败"), status_code=result.get("status_code", 500))
        return json_response({"ok": True, "batch": result["batch"]})

    async def _web_import_txt(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_txt(payload)
        if not result.get("ok"):
            return error_response(result.get("error", "导入失败"), status_code=result.get("status_code", 500))
        return json_response({"ok": True, "batch": result["batch"]})

    async def _web_import_zip(self):
        payload = await request.json(default={}) or {}
        result = await self.importer.import_zip(payload)
        if not result.get("ok"):
            return error_response(result.get("error", "导入失败"), status_code=result.get("status_code", 500))
        return json_response(result)

    async def _web_builtin_kb_list(self):
        try:
            items = await self.importer.get_builtin_kb_list()
            if items is None:
                return error_response("当前 AstrBot 版本未启用知识库模块（kb_manager 不可用）", status_code=501)
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
            return error_response(result.get("error", "导入失败"), status_code=result.get("status_code", 500))
        return json_response(result)

    async def _web_logs(self):
        """返回本插件最近的日志。"""
        logs = list(self._log_buffer)
        return json_response({"logs": logs, "count": len(logs)})


# v1.1.5.0：_parse_md 已移至 importer.py


class _BufferHandler(logging.Handler):
    """将日志写入内存缓冲区，供 Dashboard 查看。"""

    PLUGIN_LOGGER_PREFIX = "astrbot_plugin_active_learner"

    def __init__(self, buffer: collections.deque):
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        # 严格过滤：只接受本插件 logger 的日志，避免被根 logger / AstrBot 日志污染
        if not record.name or not record.name.startswith(self.PLUGIN_LOGGER_PREFIX):
            return
        try:
            self._buffer.append(self.format(record))
        except Exception:
            pass
