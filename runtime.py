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
