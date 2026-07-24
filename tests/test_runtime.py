import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.runtime import (
    BackgroundTaskHost,
    ExternalSearchController,
    build_missing_comparison_instruction,
    comparison_coverage,
    extract_comparison_objects,
    get_request_learning_state,
    mark_request_learning_hinted,
    should_apply_domain_restriction,
)


def test_extract_comparison_objects_chinese_and_english():
    assert extract_comparison_objects("Python 和 Go 有什么区别？") == ["Python", "Go"]
    assert extract_comparison_objects("Redis vs. Memcached 怎么选") == ["Redis", "Memcached"]


def test_extract_comparison_objects_strips_natural_question_phrases():
    assert extract_comparison_objects("你觉得A对比B怎么样") == ["A", "B"]
    assert extract_comparison_objects("A相比B如何") == ["A", "B"]
    assert extract_comparison_objects("A和B哪个更强") == ["A", "B"]


def test_non_comparison_sentence_is_not_split():
    assert extract_comparison_objects("我喜欢咖啡和茶") == []


def test_comparison_coverage_is_per_object():
    hits = [
        SimpleNamespace(
            entry=SimpleNamespace(
                topic="Python 并发", keywords=["Python"], content="线程与协程"
            )
        )
    ]
    assert comparison_coverage(["Python", "Go"], hits) == {
        "Python": True,
        "Go": False,
    }


def test_missing_comparison_instruction_only_names_missing_objects():
    instruction = build_missing_comparison_instruction(["Go"])
    assert "仅搜索上述缺失对象" in instruction
    assert "「Go」" in instruction
    assert "Python" not in instruction
    assert build_missing_comparison_instruction([]) == ""


def test_request_learning_state_is_isolated():
    first = SimpleNamespace()
    second = SimpleNamespace()
    first_state = get_request_learning_state(first)
    second_state = get_request_learning_state(second)
    first_state.hinted = True
    first_state.called = True
    assert second_state.hinted is False
    assert second_state.called is False
    assert get_request_learning_state(first, create=False) is first_state


def test_missing_object_prompt_marks_request_hinted():
    event = SimpleNamespace()
    assert mark_request_learning_hinted(event) is True
    assert get_request_learning_state(event, create=False).hinted is True


def test_missing_object_search_suppresses_conflicting_domain_restriction():
    common = dict(
        admin_bypass=False,
        enable_cross_domain=False,
        domains_configured=True,
        has_hits=False,
        query_in_scope=False,
    )
    assert should_apply_domain_restriction(
        **common, requires_missing_search=False
    ) is True
    assert should_apply_domain_restriction(
        **common, requires_missing_search=True
    ) is False


def test_external_search_source_timeout_keeps_fast_result():
    async def scenario():
        controller = ExternalSearchController(
            concurrency=2,
            min_interval=0,
            source_timeouts={"web": 0.2, "bilibili": 0.02},
            total_deadline=0.1,
        )

        async def fast():
            await asyncio.sleep(0.005)
            return [{"title": "fast"}]

        async def slow():
            await asyncio.sleep(1)
            return [{"title": "slow"}]

        results = await controller.search({"web": fast, "bilibili": slow})
        assert results == [{"title": "fast", "source_type": "web"}]

    asyncio.run(scenario())


def test_external_search_cancels_slow_source_after_first_nonempty_grace():
    async def scenario():
        controller = ExternalSearchController(
            concurrency=2,
            min_interval=0,
            total_deadline=1,
            first_result_grace=0.03,
        )
        slow_cancelled = asyncio.Event()

        async def fast():
            await asyncio.sleep(0.005)
            return [{"title": "fast"}]

        async def slow():
            try:
                await asyncio.sleep(1)
                return [{"title": "slow"}]
            finally:
                slow_cancelled.set()

        started = time.perf_counter()
        results = await controller.search({"web": fast, "bilibili": slow})
        elapsed = time.perf_counter() - started
        assert results == [{"title": "fast", "source_type": "web"}]
        assert elapsed < 0.2
        assert slow_cancelled.is_set()

    asyncio.run(scenario())


def test_external_search_waits_full_deadline_until_a_nonempty_result():
    async def scenario():
        controller = ExternalSearchController(
            concurrency=2,
            min_interval=0,
            total_deadline=0.2,
            first_result_grace=0.01,
        )

        async def empty_fast():
            await asyncio.sleep(0.005)
            return []

        async def nonempty_later():
            await asyncio.sleep(0.04)
            return [{"title": "later"}]

        results = await controller.search(
            {"web": empty_fast, "bilibili": nonempty_later}
        )
        assert results == [{"title": "later", "source_type": "bilibili"}]

    asyncio.run(scenario())


def test_external_search_respects_concurrency_limit():
    async def scenario():
        controller = ExternalSearchController(
            concurrency=1, min_interval=0, total_deadline=1
        )
        active = 0
        peak = 0

        async def call():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

        await controller.search({"web": call, "bilibili": call})
        assert peak == 1

    asyncio.run(scenario())


def test_background_task_host_cancels_tasks():
    async def scenario():
        host = BackgroundTaskHost()
        cancelled = asyncio.Event()

        async def worker():
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        host.create(worker(), name="test-worker")
        assert host.task_count == 1
        await asyncio.sleep(0)
        await host.close()
        assert cancelled.is_set()
        assert host.task_count == 0

    asyncio.run(scenario())
