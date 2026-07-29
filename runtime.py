"""请求生命周期、对比查询和受控并发辅助设施。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional


_COMPARISON_CONNECTORS = re.compile(r"\s*(?:和|与|跟|及|、|vs\.?|versus|对比|相比|还是)\s*", re.IGNORECASE)
_COMPARISON_HINTS = re.compile(
    r"(?:区别|差异|不同|对比|比较|相比|哪个好|哪个更|优缺点|怎么选|如何选|还是|vs\.?)",
    re.IGNORECASE,
)
_LEADING_NOISE = re.compile(
    r"^(?:请问|请|帮我|你觉得|你认为|分析一下|比较一下|对比一下|比较|对比)\s*"
)
_TRAILING_NOISE = re.compile(
    r"\s*(?:(?:有什么|有何|的)?(?:区别|差异|不同|优缺点)(?:是什么|在哪|有哪些)?"
    r"|哪个好|哪个更[0-9a-z\u4e00-\u9fff_-]{0,16}|怎么样|如何|怎么选|如何选|吗|呢|？|\?)\s*$",
    re.IGNORECASE,
)
_GENERIC_TOPIC_TERMS = {
    "wiki",
    "百科",
    "介绍",
    "信息",
    "资料",
    "内容",
    "详情",
    "角色",
    "人物",
    "设定",
    "最新",
    "武器",
    "定位",
    "技能",
    "配音",
    "中配",
    "日配",
    "特点",
    "解释",
    "解释一下",
    "是什么",
    "意思",
    "这个",
    "那个",
    "是",
    "有",
    "和",
    "与",
    "或",
    "吗",
    "呢",
    "吧",
}
_GENERIC_TOPIC_PREFIXES = (
    "麻烦你帮我",
    "可以帮我",
    "我想知道",
    "你知不知道",
    "你知道",
    "你认识",
    "你看看",
    "帮我看看",
    "帮我",
    "告诉我",
    "介绍一下",
    "解释一下",
    "请问",
    "请",
    "看看",
    "关于",
    "如何",
    "怎么",
)
_GENERIC_TOPIC_SUFFIXES = (
    "是什么角色",
    "是什么人物",
    "是什么意思",
    "有什么特点",
    "有哪些特点",
    "相关资料",
    "相关信息",
    "是什么",
    "是谁",
    "怎么样",
    "的资料",
    "的介绍",
    "的信息",
    "百科",
    "wiki",
    "角色",
    "人物",
    "呢",
    "吗",
    "吧",
)


def _clean_comparison_object(value: str) -> str:
    value = _LEADING_NOISE.sub("", value.strip(" \t\r\n，,。！？!?：:"))
    value = _TRAILING_NOISE.sub("", value).strip(" \t\r\n，,。！？!?：:")
    return value


def extract_comparison_objects(query: str, max_objects: int = 4) -> list[str]:
    """从明确的对比问句中提取对象，普通包含“和”的陈述不会误判。"""
    if not query or not _COMPARISON_HINTS.search(query):
        return []
    normalized = re.sub(r"\s+", " ", query).strip()
    parts = _COMPARISON_CONNECTORS.split(normalized)
    objects: list[str] = []
    for part in parts:
        obj = _clean_comparison_object(part)
        if not obj or len(obj) > 80:
            continue
        key = normalize_match_text(obj)
        if key and all(normalize_match_text(existing) != key for existing in objects):
            objects.append(obj)
        if len(objects) >= max_objects:
            break
    return objects if len(objects) >= 2 else []


def normalize_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").lower())


def topic_terms(value: str) -> list[str]:
    """提取主题中的核心实体词，用于阻止仅命中宽泛词的错误短路。"""
    chunks = re.findall(r"[0-9a-z]+|[\u4e00-\u9fff]+", (value or "").lower())
    terms: list[str] = []
    for chunk in chunks:
        cleaned = chunk
        changed = True
        while changed and cleaned:
            changed = False
            for prefix in _GENERIC_TOPIC_PREFIXES:
                if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                    cleaned = cleaned[len(prefix) :]
                    changed = True
                    break
            for suffix in _GENERIC_TOPIC_SUFFIXES:
                if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
                    cleaned = cleaned[: -len(suffix)]
                    changed = True
                    break
        for part in re.split(r"(?:中的|里的|当中的|关于|的)", cleaned):
            normalized = normalize_match_text(part)
            is_single_cjk = bool(re.fullmatch(r"[\u4e00-\u9fff]", normalized))
            if (
                (len(normalized) < 2 and not is_single_cjk)
                or normalized in _GENERIC_TOPIC_TERMS
                or normalized in terms
            ):
                continue
            terms.append(normalized)
    return terms


def entry_covers_topic(topic: str, hit: Any) -> bool:
    """要求命中条目覆盖主题的全部核心实体，而不是只覆盖其中一个宽泛词。"""
    entry = getattr(hit, "entry", hit)
    fields = [
        getattr(entry, "topic", "") or "",
        *(getattr(entry, "keywords", None) or []),
        getattr(entry, "content", "") or "",
    ]
    haystack = normalize_match_text(" ".join(str(field) for field in fields))
    terms = topic_terms(topic)
    return bool(terms) and all(term in haystack for term in terms)


def select_covering_hit(topic: str, hits: Iterable[Any]) -> Any | None:
    """按原排序返回首个完整覆盖主题实体的命中。"""
    return next((hit for hit in hits if entry_covers_topic(topic, hit)), None)


def object_is_covered(obj: str, hits: Iterable[Any]) -> bool:
    """判断单个对比对象是否被任一命中条目的主题、关键词或内容覆盖。"""
    needle = normalize_match_text(obj)
    if not needle:
        return False
    for hit in hits:
        entry = getattr(hit, "entry", hit)
        fields = [
            getattr(entry, "topic", "") or "",
            *(getattr(entry, "keywords", None) or []),
            getattr(entry, "content", "") or "",
        ]
        haystack = normalize_match_text(" ".join(str(field) for field in fields))
        if needle in haystack:
            return True
    return False


def comparison_coverage(objects: Iterable[str], hits: Iterable[Any]) -> dict[str, bool]:
    hit_list = list(hits)
    return {obj: object_is_covered(obj, hit_list) for obj in objects}


def build_missing_comparison_instruction(missing_objects: Iterable[str]) -> str:
    """为覆盖不全的对比查询生成不可弱化的定向搜索要求。"""
    missing = [str(obj).strip() for obj in missing_objects if str(obj).strip()]
    if not missing:
        return ""
    targets = "、".join(f"「{obj}」" for obj in missing)
    return (
        f"【对比信息缺失】当前内部知识尚未覆盖：{targets}。"
        "必须调用 search_and_learn，仅搜索上述缺失对象并补齐信息；"
        "不要重复搜索已有命中的对象，也不能把此要求降级为可选提醒。"
    )


def build_factual_grounding_instruction(
    *,
    has_memory_hits: bool,
    domain_restricted: bool,
) -> str:
    """生成不受主动学习权重影响的事实可靠性约束。"""
    if domain_restricted:
        return ""

    if not has_memory_hits:
        return (
            "【事实可靠性】本地记忆没有可引用记录。涉及具体人名、归属、配音、版本、"
            "数值、时间、搭配或其他易混淆/易变化细节时，只有完全确定才可直接回答；"
            "否则先调用 search_and_learn，工具不可用或无结果就明确表达不确定，"
            "不要凭印象补全后当作事实。"
        )

    return ""


def build_semantic_search_instruction(
    search_propensity: float,
    *,
    has_memory_hits: bool,
) -> str:
    """让主模型在当前推理中判断检索需要，不增加一次额外 LLM 请求。"""
    propensity = max(0.0, min(1.0, float(search_propensity)))
    threshold = 0.75 - 0.35 * propensity
    memory_state = "已有本地候选记忆" if has_memory_hits else "没有可靠的本地候选记忆"
    return (
        f"【自主检索策略】当前检索倾向为 {propensity:.2f}，参考触发阈值为 "
        f"{threshold:.2f}，且{memory_state}。这不是概率，也不是强制搜索比例。"
        "请在本次推理中根据事实不确定性、时效性、实体是否完整匹配以及答错代价，"
        "自行判断是否需要调用 search_and_learn。闲聊、创作、主观交流或已有可靠且实体完整的"
        "依据时不要搜索；对具体但不确定、易变化、实体冲突或用户要求核实时优先搜索。"
        "需要刷新旧记忆时将 force_refresh 设为 true。一次回答最多启动一条搜索链；"
        "search_and_learn 无结果或报错后不要再换用其他搜索工具连续重试，"
        "应明确说明本次未能核实。不要向用户输出倾向值或内部判断分数。"
    )


def should_apply_domain_restriction(
    *,
    admin_bypass: bool,
    enable_cross_domain: bool,
    domains_configured: bool,
    has_hits: bool,
    query_in_scope: bool,
    requires_missing_search: bool,
) -> bool:
    """缺失对象的强制搜索优先于一般领域限制，避免注入互斥指令。"""
    return (
        not admin_bypass
        and not enable_cross_domain
        and domains_configured
        and not has_hits
        and not query_in_scope
        and not requires_missing_search
    )


@dataclass
class RequestLearningState:
    hinted: bool = False
    called: bool = False


def get_request_learning_state(event: Any, create: bool = True) -> Optional[RequestLearningState]:
    """将学习追踪状态绑定到请求 event，避免并发请求互相覆盖。"""
    state = getattr(event, "_active_learner_request_state", None)
    if isinstance(state, RequestLearningState):
        return state
    if not create:
        return None
    state = RequestLearningState()
    try:
        object.__setattr__(event, "_active_learner_request_state", state)
    except Exception:
        try:
            setattr(event, "_active_learner_request_state", state)
        except Exception:
            return None
    return state


def mark_request_learning_hinted(event: Any) -> bool:
    """标记当前请求已收到学习/强制搜索提示。"""
    state = get_request_learning_state(event)
    if state is None:
        return False
    state.hinted = True
    return True


class BackgroundTaskHost:
    """持有后台任务强引用，并在插件卸载时统一取消和回收。"""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    def create(self, awaitable: Awaitable[Any], *, name: Optional[str] = None) -> asyncio.Task:
        if self._closing:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            raise RuntimeError("background task host is closing")
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


class ExternalSearchController:
    """外部搜索的分源超时、总 deadline 与实例级并发/速率限制。"""

    def __init__(
        self,
        *,
        concurrency: int = 3,
        min_interval: float = 0.05,
        source_timeouts: Optional[Mapping[str, float]] = None,
        total_deadline: float = 12.0,
        first_result_grace: float = 0.5,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._min_interval = max(0.0, min_interval)
        self._source_timeouts = dict(source_timeouts or {"web": 8.0, "bilibili": 6.0})
        self._total_deadline = max(0.1, total_deadline)
        self._first_result_grace = max(0.0, first_result_grace)

    async def _rate_limited_call(
        self, source: str, call: Callable[[], Awaitable[list[dict]]], timeout: float
    ) -> list[dict]:
        async with self._semaphore:
            # 每次调用占用并发槽位，并在结束后保留最小间隔，抑制突发请求。
            try:
                return await asyncio.wait_for(call(), timeout=max(0.01, timeout))
            except (asyncio.TimeoutError, TimeoutError):
                return []
            except Exception:
                return []
            finally:
                if self._min_interval > 0:
                    await asyncio.sleep(self._min_interval)

    async def search(
        self,
        calls: Mapping[str, Callable[[], Awaitable[list[dict]]]],
        *,
        deadline: Optional[float] = None,
    ) -> list[dict]:
        if not calls:
            return []
        total = self._total_deadline if deadline is None else max(0.01, deadline)
        tasks = {
            source: asyncio.create_task(
                self._rate_limited_call(
                    source,
                    call,
                    min(self._source_timeouts.get(source, total), total),
                )
            )
            for source, call in calls.items()
        }
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + total
        pending = set(tasks.values())
        completed: set[asyncio.Task] = set()
        grace_deadline: Optional[float] = None

        while pending:
            stop_at = deadline_at if grace_deadline is None else min(deadline_at, grace_deadline)
            timeout = max(0.0, stop_at - loop.time())
            if timeout <= 0:
                break
            just_done, pending = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not just_done:
                break
            completed.update(just_done)
            if grace_deadline is None:
                for task in just_done:
                    if task.cancelled():
                        continue
                    try:
                        value = task.result()
                    except Exception:
                        continue
                    if value:
                        grace_deadline = min(
                            deadline_at, loop.time() + self._first_result_grace
                        )
                        break

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        results: list[dict] = []
        for source, task in tasks.items():
            if task not in completed or task.cancelled():
                continue
            try:
                value = task.result()
            except Exception:
                continue
            for item in value or []:
                if isinstance(item, dict):
                    enriched = dict(item)
                    enriched.setdefault("source_type", source)
                    results.append(enriched)
        return results
