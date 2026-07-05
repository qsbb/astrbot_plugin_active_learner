"""
AstrBot 主动学习记忆插件
功能：自动检索、主动学习、记忆存储、质疑验证
"""

import json
import time
import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass


# ============== 数据模型 ==============

@dataclass
class KnowledgeEntry:
    """单条知识记忆"""
    id: str                          # 唯一ID (topic的hash)
    topic: str                       # 主题名
    content: str                     # 知识内容（总结后的）
    keywords: list[str]              # 关键词列表（用于匹配）
    source: str                      # 来源描述
    confidence: float                # 置信度 0.0~1.0
    created_at: float                # 创建时间戳
    updated_at: float                # 更新时间戳
    access_count: int = 0            # 被检索次数
    verified: bool = False           # 是否经过验证
    challenge_count: int = 0         # 被质疑次数
    last_challenged_at: float = 0    # 最后质疑时间
    sources_detail: list[str] = field(default_factory=list)  # 详细来源列表

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "content": self.content,
            "keywords": self.keywords,
            "source": self.source,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "access_count": self.access_count,
            "verified": self.verified,
            "challenge_count": self.challenge_count,
            "last_challenged_at": self.last_challenged_at,
            "sources_detail": self.sources_detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KnowledgeEntry":
        return cls(**d)


# ============== 搜索引擎 ==============

class WebSearcher:
    """多源网页搜索：AstrBot内置/插件搜索同级，DuckDuckGo兜底"""

    DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"

    def __init__(self, context: Context = None):
        self._context = context

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        """统一搜索入口：AstrBot内置/插件搜索同级 → DuckDuckGo兜底"""
        # 同级尝试所有已注册的搜索源（AstrBot内置 + bilibili_ai_bot等插件）
        results = await self._search_registered_sources(query, max_results)
        if results:
            return results

        # 兜底：DuckDuckGo
        logger.debug("注册搜索源均不可用，使用DuckDuckGo")
        results = await self._search_duckduckgo(query, max_results)

        # 中文补充搜索
        if not any("\u4e00" <= c <= "\u9fff" for c in query):
            cn_query = f"{query} 是什么 解释"
        else:
            cn_query = f"{query} 详细解释"
        cn_results = await self._search_duckduckgo(cn_query, 3)
        results.extend(cn_results)

        # 去重
        seen = set()
        unique = []
        for r in results:
            key = r["title"][:30]
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique[:max_results]

    async def _search_registered_sources(self, query: str, max_results: int) -> list[dict]:
        """遍历AstrBot内置搜索和已安装插件的搜索能力，同级优先级"""
        if not self._context:
            return []
        try:
            stars = self._context.get_all_stars()
            for star_meta in (stars or []):
                if not star_meta.activated or not star_meta.star_cls:
                    continue
                cls = star_meta.star_cls
                # 按优先级尝试各个搜索方法名
                for method_name in ("_web_search", "web_search", "search_web"):
                    if hasattr(cls, method_name) and callable(getattr(cls, method_name)):
                        try:
                            result = await getattr(cls, method_name)(query)
                            if result:
                                parsed = self._parse_search_result(result, max_results)
                                if parsed:
                                    logger.debug(f"搜索源 {star_meta.name}.{method_name} 返回 {len(parsed)} 条")
                                    return parsed
                        except Exception as e:
                            logger.debug(f"搜索源 {star_meta.name}.{method_name} 失败: {e}")
        except Exception as e:
            logger.debug(f"遍历搜索源失败: {e}")
        return []

    @staticmethod
    def _parse_search_result(result, max_results: int) -> list[dict]:
        """解析搜索结果为统一格式，支持字符串/列表/字典"""
        if not result:
            return []
        if isinstance(result, str):
            items = []
            current = {"title": "", "snippet": "", "url": ""}
            for line in result.strip().split("\n"):
                line = line.strip()
                if not line:
                    if current["snippet"]:
                        items.append(current)
                        current = {"title": "", "snippet": "", "url": ""}
                    continue
                # 提取URL
                url_match = re.search(r'https?://\S+', line)
                if url_match and not current["url"]:
                    current["url"] = url_match.group()
                # 标题行（以-开头或包含:）
                if line.startswith("-") or line.startswith("*"):
                    if current["snippet"]:
                        items.append(current)
                        current = {"title": "", "snippet": "", "url": ""}
                    parts = line.lstrip("-* ").split(":", 1)
                    current["title"] = parts[0].strip()
                    current["snippet"] = parts[1].strip() if len(parts) > 1 else ""
                elif ":" in line and not current["snippet"]:
                    parts = line.split(":", 1)
                    current["title"] = parts[0].strip()
                    current["snippet"] = parts[1].strip()
                else:
                    current["snippet"] += (" " + line) if current["snippet"] else line
            if current["snippet"]:
                items.append(current)
            return items[:max_results]
        elif isinstance(result, list):
            items = []
            for item in result[:max_results]:
                if isinstance(item, dict):
                    items.append({
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", item.get("content", "")),
                        "url": item.get("url", ""),
                    })
                elif isinstance(item, str):
                    items.append({"title": "", "snippet": item, "url": ""})
            return items
        return []

    @staticmethod
    async def _search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
        """DuckDuckGo搜索（降级方案）"""
        results = []
        try:
            async with aiohttp.ClientSession() as session:
                data = {"q": query, "b": ""}
                async with session.post(
                    WebSearcher.DUCKDUCKGO_URL,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as resp:
                    html = await resp.text()
                    pattern = r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</(?:td|span)'
                    matches = re.findall(pattern, html, re.DOTALL)
                    for url, title, snippet in matches[:max_results]:
                        title = re.sub(r"<.*?>", "", title).strip()
                        snippet = re.sub(r"<.*?>", "", snippet).strip()
                        if title and snippet:
                            results.append({"title": title, "url": url, "snippet": snippet})
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
        return results

    # 保留向后兼容的静态方法
    @staticmethod
    async def search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
        return await WebSearcher._search_duckduckgo(query, max_results)

    @staticmethod
    async def search_multi_source(query: str, max_results: int = 8) -> list[dict]:
        searcher = WebSearcher()
        return await searcher.search(query, max_results)


# ============== 主插件类 ==============

@register(
    name="active_learner",
    desc="主动学习记忆插件：自动检索、学习新知识、记忆存储、质疑验证",
    author="AstrBotUser",
    version="1.0.0",
)
class ActiveLearnerPlugin(Star):
    """主动学习记忆插件"""

    # ---------- 生命周期 ----------

    def __init__(self, context: Context):
        super().__init__(context)
        self.memory: dict[str, KnowledgeEntry] = {}
        # 从配置中读取参数
        cfg = context.get_config() if hasattr(context, "get_config") else {}
        plugin_cfg = cfg.get("active_learner", {}) if isinstance(cfg, dict) else {}
        self.max_entries = plugin_cfg.get("max_entries", 500)
        self.min_confidence = plugin_cfg.get("min_confidence", 0.3)
        self.search_threshold = plugin_cfg.get("search_threshold", 0.45)
        self._load_memory()

        # 初始化搜索器（传入context以使用AstrBot内置搜索）
        self.searcher = WebSearcher(context)

        # 注册 LLM 工具
        self.context.add_llm_tools(
            SearchAndLearnTool(self),
            RecallMemoryTool(self),
            ChallengeKnowledgeTool(self),
        )
        logger.info("ActiveLearner 插件已加载，记忆库条数: %d", len(self.memory))

    async def terminate(self):
        self._save_memory()
        logger.info("ActiveLearner 插件已卸载，记忆已保存")

    # ---------- 记忆持久化 ----------

    def _get_data_path(self) -> Path:
        data_dir = StarTools.get_data_dir()
        return data_dir / "memory.json"

    def _load_memory(self):
        path = self._get_data_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data:
                    entry = KnowledgeEntry.from_dict(item)
                    self.memory[entry.id] = entry
                logger.info(f"加载了 {len(self.memory)} 条记忆")
            except Exception as e:
                logger.error(f"加载记忆失败: {e}")

    def _save_memory(self):
        path = self._get_data_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = [e.to_dict() for e in self.memory.values()]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存记忆失败: {e}")

    # ---------- 记忆操作 ----------

    def _make_id(self, topic: str) -> str:
        return hashlib.md5(topic.lower().strip().encode()).hexdigest()[:12]

    def add_or_update_memory(
        self,
        topic: str,
        content: str,
        keywords: list[str],
        source: str,
        confidence: float,
        sources_detail: list[str] | None = None,
    ) -> KnowledgeEntry:
        """添加或更新记忆"""
        entry_id = self._make_id(topic)
        now = time.time()

        if entry_id in self.memory:
            existing = self.memory[entry_id]
            # 如果新信息置信度更高，更新内容
            if confidence >= existing.confidence:
                existing.content = content
                existing.source = source
                existing.confidence = max(confidence, existing.confidence)
                existing.keywords = list(set(existing.keywords + keywords))
                existing.updated_at = now
                if sources_detail:
                    existing.sources_detail = sources_detail
            else:
                # 合并关键词
                existing.keywords = list(set(existing.keywords + keywords))
                existing.updated_at = now
            existing.access_count += 1
            entry = existing
        else:
            entry = KnowledgeEntry(
                id=entry_id,
                topic=topic,
                content=content,
                keywords=keywords,
                source=source,
                confidence=confidence,
                created_at=now,
                updated_at=now,
                access_count=1,
                sources_detail=sources_detail or [],
            )
            self.memory[entry_id] = entry

        # 容量管理
        self._evict_if_needed()
        self._save_memory()
        return entry

    def search_memory(self, query: str, top_k: int = 3) -> list[KnowledgeEntry]:
        """基于关键词搜索记忆库"""
        query_lower = query.lower()
        query_words = set(re.split(r"[\s,，。？！?!\-/]+", query_lower))
        query_words = {w for w in query_words if len(w) > 1}

        scored = []
        for entry in self.memory.values():
            score = 0
            # 关键词匹配
            for kw in entry.keywords:
                kw_lower = kw.lower()
                if kw_lower in query_lower:
                    score += 2.0
                elif any(w in kw_lower or kw_lower in w for w in query_words):
                    score += 1.0

            # 主题匹配
            if entry.topic.lower() in query_lower:
                score += 3.0

            # 置信度加权
            score *= entry.confidence

            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def _evict_if_needed(self):
        """容量淘汰"""
        if len(self.memory) <= self.max_entries:
            return

        # 淘汰策略：优先淘汰低置信度+长时间未访问的
        entries = sorted(
            self.memory.values(),
            key=lambda e: (e.confidence * 0.6 + (e.access_count / 100) * 0.4),
        )
        to_remove = len(self.memory) - self.max_entries
        for entry in entries[:to_remove]:
            del self.memory[entry.id]
        logger.info(f"淘汰了 {to_remove} 条低质量记忆")

    def get_stats(self) -> dict:
        """获取记忆库统计"""
        if not self.memory:
            return {"total": 0}

        entries = list(self.memory.values())
        return {
            "total": len(entries),
            "verified": sum(1 for e in entries if e.verified),
            "challenged": sum(1 for e in entries if e.challenge_count > 0),
            "avg_confidence": sum(e.confidence for e in entries) / len(entries),
            "most_accessed": max(entries, key=lambda e: e.access_count).topic if entries else None,
        }

    # ---------- 上下文注入钩子 ----------

    @filter.on_llm_request()
    async def inject_memory_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """每次LLM请求前，检索相关记忆并注入上下文"""
        user_msg = event.get_message_str()
        if not user_msg or len(user_msg) < 3:
            return

        # 搜索相关记忆
        relevant = self.search_memory(user_msg, top_k=3)
        if not relevant:
            return

        # 构建注入上下文
        context_parts = []
        for entry in relevant:
            entry.access_count += 1
            status = "✅已验证" if entry.verified else f"⚠️置信度{entry.confidence:.0%}"
            context_parts.append(
                f"[记忆] {entry.topic}（{status}）: {entry.content}"
            )

        if context_parts:
            injection = (
                "以下是从记忆库中检索到的可能相关的信息，"
                "请参考但不要直接照搬，如不确定请说明：\n"
                + "\n".join(context_parts)
            )
            req.extra_user_content_parts.append(TextPart(text=injection))
            logger.debug(f"注入了 {len(relevant)} 条记忆上下文")

        self._save_memory()

    # ---------- 质疑检测钩子 ----------

    @filter.on_llm_request()
    async def detect_challenge(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """检测用户是否在质疑某条记忆"""
        user_msg = event.get_message_str()
        if not user_msg:
            return

        # 质疑关键词模式
        challenge_patterns = [
            r"(?:这|这个|这个信息|这条|这个说法)(?:不对|错了吧|有问题|不准确|过时|假的|瞎说)",
            r"(?:你确定|真的吗|靠谱吗|可信吗|是不是搞错了)",
            r"(?:质疑|怀疑|反驳|纠正|勘误)",
            r"(?:不对吧|错了吧|假的吧|编的吧)",
        ]

        is_challenge = any(re.search(p, user_msg) for p in challenge_patterns)
        if not is_challenge:
            return

        # 找到可能被质疑的记忆
        relevant = self.search_memory(user_msg, top_k=1)
        if not relevant:
            return

        entry = relevant[0]
        entry.challenge_count += 1
        entry.last_challenged_at = time.time()

        # 注入质疑上下文
        challenge_context = (
            f"[质疑提示] 用户正在质疑关于「{entry.topic}」的记忆。"
            f"当前记忆内容：{entry.content}（置信度{entry.confidence:.0%}）\n"
            f"请认真对待用户的质疑，如果你也不确定，建议使用 verify_knowledge 工具进行验证。"
        )
        req.extra_user_content_parts.append(TextPart(text=challenge_context))
        self._save_memory()

    # ---------- 管理指令 ----------

    @filter.command("memory")
    async def memory_stats(self, event: AstrMessageEvent):
        """查看记忆库统计"""
        stats = self.get_stats()
        if stats["total"] == 0:
            yield event.plain_result("📝 记忆库为空，我会在聊天中自动学习新知识~")
            return

        text = (
            f"📝 记忆库统计\n"
            f"━━━━━━━━━━\n"
            f"总条数: {stats['total']}\n"
            f"已验证: {stats['verified']}\n"
            f"被质疑: {stats['challenged']}\n"
            f"平均置信度: {stats['avg_confidence']:.0%}\n"
            f"最常访问: {stats['most_accessed']}\n"
            f"━━━━━━━━━━\n"
            f"指令: /memory list | search <关键词> | info <主题> | forget <主题>"
        )
        yield event.plain_result(text)

    @filter.command_group("memory")
    def memory_cmd(self):
        pass

    @memory_cmd.command("list")
    async def memory_list(self, event: AstrMessageEvent, page: int = 1):
        """列出记忆条目"""
        entries = sorted(self.memory.values(), key=lambda e: e.updated_at, reverse=True)
        if not entries:
            yield event.plain_result("📝 记忆库为空")
            return

        per_page = 10
        total_pages = (len(entries) + per_page - 1) // per_page
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        page_entries = entries[start : start + per_page]

        lines = [f"📝 记忆列表 ({page}/{total_pages}页)\n"]
        for i, e in enumerate(page_entries, start + 1):
            verified = "✅" if e.verified else "❓"
            lines.append(f"{i}. {verified} {e.topic} (置信度{e.confidence:.0%}, 访问{e.access_count}次)")
        lines.append(f"\n使用 /memory list <页码> 翻页")

        yield event.plain_result("\n".join(lines))

    @memory_cmd.command("search")
    async def memory_search(self, event: AstrMessageEvent, keyword: str):
        """搜索记忆"""
        results = self.search_memory(keyword, top_k=5)
        if not results:
            yield event.plain_result(f"🔍 未找到与「{keyword}」相关的记忆")
            return

        lines = [f"🔍 搜索「{keyword}」的结果:\n"]
        for e in results:
            verified = "✅" if e.verified else "❓"
            lines.append(f"{verified} {e.topic}")
            lines.append(f"   {e.content[:80]}...")
            lines.append(f"   置信度: {e.confidence:.0%} | 来源: {e.source}\n")

        yield event.plain_result("\n".join(lines))

    @memory_cmd.command("info")
    async def memory_info(self, event: AstrMessageEvent, topic: str):
        """查看某条记忆详情"""
        results = self.search_memory(topic, top_k=1)
        if not results:
            yield event.plain_result(f"❌ 未找到关于「{topic}」的记忆")
            return

        e = results[0]
        text = (
            f"📖 记忆详情: {e.topic}\n"
            f"━━━━━━━━━━\n"
            f"内容: {e.content}\n"
            f"关键词: {', '.join(e.keywords)}\n"
            f"来源: {e.source}\n"
            f"置信度: {e.confidence:.0%}\n"
            f"已验证: {'是✅' if e.verified else '否❌'}\n"
            f"被质疑: {e.challenge_count}次\n"
            f"访问次数: {e.access_count}\n"
            f"创建时间: {time.strftime('%Y-%m-%d %H:%M', time.localtime(e.created_at))}\n"
            f"更新时间: {time.strftime('%Y-%m-%d %H:%M', time.localtime(e.updated_at))}"
        )
        yield event.plain_result(text)

    @memory_cmd.command("forget")
    async def memory_forget(self, event: AstrMessageEvent, topic: str):
        """删除某条记忆"""
        entry_id = self._make_id(topic)
        if entry_id in self.memory:
            del self.memory[entry_id]
            self._save_memory()
            yield event.plain_result(f"🗑️ 已删除关于「{topic}」的记忆")
        else:
            # 模糊匹配
            results = self.search_memory(topic, top_k=1)
            if results and results[0].id in self.memory:
                del self.memory[results[0].id]
                self._save_memory()
                yield event.plain_result(f"🗑️ 已删除关于「{results[0].topic}」的记忆")
            else:
                yield event.plain_result(f"❌ 未找到关于「{topic}」的记忆")

    @memory_cmd.command("verify")
    async def memory_verify(self, event: AstrMessageEvent, topic: str):
        """手动触发验证"""
        results = self.search_memory(topic, top_k=1)
        if not results:
            yield event.plain_result(f"❌ 未找到关于「{topic}」的记忆，请先学习该主题")
            return

        entry = results[0]
        yield event.plain_result(f"🔍 正在验证「{entry.topic}」，请稍候...")

        # 触发验证（通过LLM工具）
        provider_id = await self.context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )
        if not provider_id:
            yield event.plain_result("❌ 未找到可用的LLM提供商")
            return

        from astrbot.core.agent.tool import ToolSet

        llm_resp = await self.context.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt=(
                f"请验证以下知识是否准确：\n"
                f"主题：{entry.topic}\n"
                f"内容：{entry.content}\n"
                f"请使用 verify_knowledge 工具进行多源验证。"
            ),
            tools=ToolSet([SearchAndLearnTool(self), ChallengeKnowledgeTool(self)]),
            max_steps=5,
            tool_call_timeout=30,
        )

        # 读取验证结果（工具会更新entry）
        updated = self.memory.get(entry.id)
        if updated:
            status = "✅ 验证通过" if updated.verified else "⚠️ 验证未完全通过"
            yield event.plain_result(
                f"{status}: {updated.topic}\n"
                f"当前置信度: {updated.confidence:.0%}\n"
                f"内容: {updated.content}"
            )
        else:
            yield event.plain_result("验证完成，但记忆条目已被更新")

    @memory_cmd.command("export")
    async def memory_export(self, event: AstrMessageEvent):
        """导出记忆库"""
        if not self.memory:
            yield event.plain_result("📝 记忆库为空，无需导出")
            return

        data = [e.to_dict() for e in self.memory.values()]
        export_path = StarTools.get_data_dir() / "memory_export.json"
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        yield event.plain_result(f"📦 记忆库已导出到: {export_path}")


# ============== LLM 工具 ==============

@pydantic_dataclass
class SearchAndLearnTool(FunctionTool[AstrAgentContext]):
    """搜索并学习新知识的工具。当遇到不知道的问题时自动调用。"""

    name: str = "search_and_learn"
    description: str = (
        "搜索网络并学习新知识。当用户问了一个你不确定或不知道的问题时，"
        "使用此工具搜索、总结并记忆。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "要学习的主题或问题",
                },
                "query": {
                    "type": "string",
                    "description": "搜索关键词，用于在网络上搜索",
                },
            },
            "required": ["topic", "query"],
        }
    )


    def __init__(self, plugin: ActiveLearnerPlugin):
        super().__init__()
        object.__setattr__(self, "_plugin", plugin)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        topic = kwargs.get("topic", "")
        query = kwargs.get("query", topic)

        if not topic:
            return ToolExecResult("请提供要学习的主题")

        plugin = self._plugin

        # 1. 多源搜索（优先AstrBot内置，降级DuckDuckGo）
        search_results = await plugin.searcher.search(query, max_results=6)

        if not search_results:
            return ToolExecResult(f"搜索「{query}」未找到结果，无法学习该主题")

        # 2. 整理搜索结果
        snippets = []
        sources = []
        for r in search_results:
            snippets.append(f"标题: {r['title']}\n摘要: {r['snippet']}")
            sources.append(f"{r['title']} ({r['url']})")

        search_text = "\n---\n".join(snippets)

        # 3. 用LLM总结
        event = context.context.event
        provider_id = await plugin.context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )

        summary_prompt = (
            f"请根据以下搜索结果，对「{topic}」进行准确、简洁的总结。\n"
            f"要求：\n"
            f"1. 提取关键事实，避免主观判断\n"
            f"2. 标注信息的可信度（高/中/低）\n"
            f"3. 如果搜索结果相互矛盾，请指出分歧\n"
            f"4. 总结控制在200字以内\n"
            f"5. 提取3-5个关键词\n\n"
            f"搜索结果：\n{search_text}"
        )

        try:
            llm_resp = await plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=summary_prompt,
            )
            summary = llm_resp.completion_text
        except Exception as e:
            logger.error(f"LLM总结失败: {e}")
            # 降级：直接拼接搜索结果
            summary = f"搜索结果摘要：{search_results[0]['snippet']}"

        # 4. 提取关键词
        keywords = [topic]
        # 从搜索结果中提取高频词
        all_text = " ".join(r["title"] + " " + r["snippet"] for r in search_results)
        # 简单分词：按空格和标点分割
        words = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", all_text)
        word_freq = {}
        for w in words:
            w_lower = w.lower()
            if w_lower not in keywords and len(w_lower) > 1:
                word_freq[w_lower] = word_freq.get(w_lower, 0) + 1
        # 取频率最高的几个
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        keywords.extend([w for w, _ in sorted_words[:5]])

        # 5. 计算置信度
        # 基于搜索结果数量和一致性
        confidence = min(0.7, 0.3 + len(search_results) * 0.07)
        if len(search_results) >= 3:
            confidence = min(0.85, confidence + 0.1)

        # 6. 存入记忆
        entry = plugin.add_or_update_memory(
            topic=topic,
            content=summary,
            keywords=keywords,
            source=f"网络搜索 ({len(search_results)}个来源)",
            confidence=confidence,
            sources_detail=sources,
        )

        logger.info(f"学习了新知识: {topic} (置信度{confidence:.0%})")

        return ToolExecResult(
            f"已学习「{topic}」并存入记忆库。\n"
            f"总结: {summary[:200]}...\n"
            f"置信度: {confidence:.0%}\n"
            f"来源数: {len(sources)}"
        )


@pydantic_dataclass
class RecallMemoryTool(FunctionTool[AstrAgentContext]):
    """从记忆库中检索知识的工具。"""

    name: str = "recall_memory"
    description: str = (
        "从记忆库中检索已学习的知识。当用户问到之前讨论过的话题时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要检索的关键词或主题",
                },
            },
            "required": ["query"],
        }
    )


    def __init__(self, plugin: ActiveLearnerPlugin):
        super().__init__()
        object.__setattr__(self, "_plugin", plugin)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        query = kwargs.get("query", "")
        if not query:
            return ToolExecResult("请提供要检索的关键词")

        plugin = self._plugin
        results = plugin.search_memory(query, top_k=3)

        if not results:
            return ToolExecResult(f"记忆库中未找到关于「{query}」的知识")

        parts = []
        for entry in results:
            entry.access_count += 1
            status = "✅已验证" if entry.verified else f"❓置信度{entry.confidence:.0%}"
            parts.append(
                f"【{entry.topic}】{status}\n"
                f"内容: {entry.content}\n"
                f"来源: {entry.source}"
            )

        plugin._save_memory()
        return ToolExecResult("\n\n".join(parts))


@pydantic_dataclass
class ChallengeKnowledgeTool(FunctionTool[AstrAgentContext]):
    """验证和质疑已有知识的工具。当用户对某条信息提出质疑时使用。"""

    name: str = "verify_knowledge"
    description: str = (
        "验证某条知识的准确性。通过多源搜索交叉验证，更新置信度。"
        "当用户质疑某条信息或需要确认准确性时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "要验证的主题",
                },
                "claim": {
                    "type": "string",
                    "description": "需要验证的具体内容/说法",
                },
            },
            "required": ["topic", "claim"],
        }
    )


    def __init__(self, plugin: ActiveLearnerPlugin):
        super().__init__()
        object.__setattr__(self, "_plugin", plugin)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        topic = kwargs.get("topic", "")
        claim = kwargs.get("claim", "")

        if not topic:
            return ToolExecResult("请提供要验证的主题")

        plugin = self._plugin

        # 1. 从多个角度搜索验证
        verification_queries = [
            f"{topic} 事实核查",
            f"{topic} 是否正确",
            f"{topic} 官方信息",
            f"{claim} 验证 真假",
        ]

        all_results = []
        for q in verification_queries:
            results = await plugin.searcher.search(q, max_results=3)
            all_results.extend(results)

        if not all_results:
            return ToolExecResult(f"无法搜索到关于「{topic}」的验证信息")

        # 2. 分析验证结果
        snippets = [f"- {r['title']}: {r['snippet']}" for r in all_results[:8]]
        search_text = "\n".join(snippets)

        # 3. LLM交叉验证
        event = context.context.event
        provider_id = await plugin.context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )

        verify_prompt = (
            f"请根据以下多源搜索结果，验证以下说法是否准确：\n\n"
            f"主题：{topic}\n"
            f"说法：{claim}\n\n"
            f"搜索结果：\n{search_text}\n\n"
            f"请给出：\n"
            f"1. 验证结论（正确/部分正确/错误/无法确认）\n"
            f"2. 置信度评分（0-100）\n"
            f"3. 修正后的内容（如果需要修正）\n"
            f"4. 验证依据"
        )

        try:
            llm_resp = await plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=verify_prompt,
            )
            verification = llm_resp.completion_text
        except Exception as e:
            logger.error(f"验证LLM调用失败: {e}")
            verification = "验证过程中出现错误，无法完成验证"

        # 4. 更新记忆
        entry_id = plugin._make_id(topic)
        entry = plugin.memory.get(entry_id)

        if entry:
            # 根据验证结果调整置信度
            if "正确" in verification and "错误" not in verification:
                entry.confidence = min(1.0, entry.confidence + 0.15)
                entry.verified = True
            elif "错误" in verification:
                entry.confidence = max(0.1, entry.confidence - 0.3)
                entry.verified = False
                # 如果置信度太低，标记为需要修正
                if entry.confidence < 0.3:
                    entry.content = f"[待修正] {entry.content}"
            elif "部分正确" in verification:
                entry.confidence = max(0.3, entry.confidence - 0.1)
                entry.verified = False

            entry.updated_at = time.time()
            plugin._save_memory()

        # 5. 返回验证结果
        sources_count = len(all_results)
        result_text = (
            f"验证完成：{topic}\n"
            f"━━━━━━━━━━\n"
            f"{verification}\n"
            f"━━━━━━━━━━\n"
            f"参考来源数: {sources_count}\n"
        )

        if entry:
            result_text += f"记忆库置信度已更新为: {entry.confidence:.0%}"

        return ToolExecResult(result_text)
