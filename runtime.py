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
_SEARCH_VERB = r"(?:搜(?:索)?|检索|查(?:询)?|核实|核对|验证)"
_NEGATED_SEARCH_REQUEST = re.compile(
    rf"(?:不用|不要|别|无需|不必|不需要)\s*"
    rf"(?:(?:你|再|重新|去|帮我|替我|给我)\s*){{0,3}}{_SEARCH_VERB}",
    re.IGNORECASE,
)
_SELF_DIRECTED_SEARCH = re.compile(
    rf"(?:^|[，。！？!?\s])我(?:刚|已经|刚才|之前|先|去|来|自己|再)+\s*{_SEARCH_VERB}",
    re.IGNORECASE,
)
_EXPLICIT_SEARCH_REQUESTS = (
    re.compile(
        rf"(?:请|麻烦|劳驾|帮我|帮忙|替我|能不能|可不可以|可以帮我|"
        rf"你(?:再|重新|去|帮我|给我)?|再|重新|联网|上网)\s*"
        rf"(?:再|重新|去|帮我|给我)?\s*{_SEARCH_VERB}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_SEARCH_VERB}(?:一?下|一遍|看看|看|清楚|资料|信息|来源|出处|吧|呗)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[.!?，。！？]\s*)(?:please\s+|can\s+you\s+|could\s+you\s+|"
        r"would\s+you\s+)?(?:search|look(?:\s+(?:this|that|it))?\s+up|"
        r"browse|verify|fact[- ]?check)\b",
        re.IGNORECASE,
    ),
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


def detect_explicit_search_request(query: str) -> bool:
    """识别用户明确要求联网搜索或事实核验的指令。"""
    normalized = re.sub(r"\s+", " ", (query or "").strip())
    if (
        not normalized
        or _NEGATED_SEARCH_REQUEST.search(normalized)
        or _SELF_DIRECTED_SEARCH.search(normalized)
    ):
        return False
    return any(pattern.search(normalized) for pattern in _EXPLICIT_SEARCH_REQUESTS)


def build_factual_grounding_instruction(
    *,
    explicit_search_requested: bool,
    has_memory_hits: bool,
    domain_restricted: bool,
) -> str:
    """生成不受主动学习权重影响的事实可靠性约束。"""
    if domain_restricted:
        if not explicit_search_requested:
            return ""
        return (
            "【事实核验受领域限制】用户明确要求搜索或核实，但当前领域策略禁止联网搜索。"
            "请直接说明当前无法核实，不得声称已经搜索，也不要凭印象补全具体事实。"
        )

    if explicit_search_requested:
        return (
            "【强制事实核验】用户明确要求搜索、查询或核实，这不是可选的学习建议，"
            "也不受主动学习开关或权重影响。回答可核查的具体事实前，必须调用 "
            "search_and_learn，并将 force_refresh 设为 true，以跳过旧的本地记忆并实际检索外部来源。"
            "只有在明确验证某条已存记忆时，才可改用 verify_knowledge。"
            "工具不可用、无结果或没有返回外部来源时，请明确说尚未核实；"
            "不得把训练数据印象或旧记忆冒充本次搜索结果。"
        )

    if not has_memory_hits:
        return (
            "【事实可靠性】本地记忆没有可引用记录。涉及具体人名、归属、配音、版本、"
            "数值、时间、搭配或其他易混淆/易变化细节时，只有完全确定才可直接回答；"
            "否则先调用 search_and_learn，工具不可用或无结果就明确表达不确定，"
            "不要凭印象补全后当作事实。"
        )

    return ""


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
