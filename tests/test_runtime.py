import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.runtime import (
    BackgroundTaskHost,
    ExternalSearchController,
    build_factual_grounding_instruction,
    build_missing_comparison_instruction,
    build_semantic_search_instruction,
    comparison_coverage,
    entry_covers_topic,
    extract_comparison_objects,
    get_request_learning_state,
    mark_request_learning_hinted,
    select_covering_hit,
    should_apply_domain_restriction,
    topic_terms,
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


def test_topic_coverage_requires_every_core_entity():
    unrelated = SimpleNamespace(
        entry=SimpleNamespace(
            topic="卡拉彼丘令（牢令）角色",
            keywords=["卡拉彼丘", "令"],
            content="这是另一位角色。",
        )
    )
    matching = SimpleNamespace(
        entry=SimpleNamespace(
            topic="卡拉彼丘 诺诺",
            keywords=["卡拉彼丘", "诺诺"],
            content="诺诺是游戏角色。",
        )
    )
    assert topic_terms("卡拉彼丘 诺诺") == ["卡拉彼丘", "诺诺"]
    assert topic_terms("卡拉彼丘 令") == ["卡拉彼丘", "令"]
    assert entry_covers_topic("卡拉彼丘 诺诺", unrelated) is False
    assert entry_covers_topic("卡拉彼丘 诺诺", matching) is True
    assert select_covering_hit("卡拉彼丘 诺诺", [unrelated, matching]) is matching
    assert topic_terms("你看看诺诺是什么角色") == ["诺诺"]
    assert topic_terms("卡拉彼丘中的诺诺有什么特点") == ["卡拉彼丘", "诺诺"]
    assert topic_terms("这个是什么意思") == []


def test_semantic_search_policy_is_continuous_and_not_keyword_driven():
    low = build_semantic_search_instruction(0.2, has_memory_hits=False)
    high = build_semantic_search_instruction(0.9, has_memory_hits=True)
    assert "自主检索策略" in low
    assert "0.20" in low and "0.90" in high
    assert "不是概率" in low
    assert "关键词" not in low
    assert "一次回答最多启动一条搜索链" in high
    assert "不要再换用其他搜索工具连续重试" in high


def test_missing_memory_builds_fact_guard_without_forcing_refresh():
    instruction = build_factual_grounding_instruction(
        has_memory_hits=False,
        domain_restricted=False,
    )
    assert "事实可靠性" in instruction
    assert "不要凭印象" in instruction
    assert "force_refresh" not in instruction
    assert (
        build_factual_grounding_instruction(
            has_memory_hits=True,
            domain_restricted=False,
        )
        == ""
    )


def test_domain_restriction_owns_its_prompt_without_duplicate_fact_guard():
    assert (
        build_factual_grounding_instruction(
            has_memory_hits=False,
            domain_restricted=True,
        )
        == ""
    )


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


def test_fact_guard_exempts_subjective_and_roleplay_content():
    """现象：问好感/印象/冷知识被固定回"不知道，没见过"。

    事实可靠性约束必须显式豁免角色内主观感受、玩笑、创作与日常闲聊，
    否则模型会把任何没有本地记忆的提问都当成待核实事实而一律否认。
    """
    instruction = build_factual_grounding_instruction(
        has_memory_hits=False,
        domain_restricted=False,
    )
    assert "主观感受" in instruction
    assert "玩笑" in instruction
    assert "创作" in instruction
    assert "日常闲聊" in instruction
    assert "不要用「不知道」「没见过」敷衍" in instruction


def test_fact_guard_prefers_reporting_search_results():
    """现象：搜到了也不说，直接回不知道。

    约束应要求搜到就把查到的内容自然说出来，而不是一律推给"不确定"。
    """
    instruction = build_factual_grounding_instruction(
        has_memory_hits=False,
        domain_restricted=False,
    )
    assert "搜到就把查到的内容自然说出来" in instruction
    assert "说清来源与把握" in instruction


def test_domain_restriction_prompt_does_not_demand_flat_denial():
    """领域限制不应要求模型用生硬否认堵死话题。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "直接明确回复你不知道/没玩过/没见过" not in source
    assert "不要用「不知道／没玩过／没见过」" in source
    assert "按人设聊一聊" in source


def test_semantic_search_instruction_does_not_force_uncertainty_disclaimer():
    """真正生效的那条策略不得强制"声明未核实"（固定否认的直接来源）。"""
    instruction = build_semantic_search_instruction(0.7, has_memory_hits=False)
    assert "应明确说明本次未能核实" not in instruction
    assert "本地没有候选记忆不代表这个问题不能回答" in instruction
    assert "不要因为查不到就退缩或反复说不知道" in instruction


def test_semantic_search_instruction_keeps_honesty_boundary():
    """去掉强制表态后，仍须保留"不虚构、不冒充已核实"的正向边界。"""
    instruction = build_semantic_search_instruction(0.7, has_memory_hits=False)
    assert "不要虚构真实经历、共同往事、引用来源或检索行为" in instruction
    assert "不要把不确定的外部事实说成已经核实" in instruction
    assert "没检索过就不要说「我查过了」" in instruction


def test_semantic_search_instruction_still_guides_real_fact_search():
    """豁免闲聊不等于放弃检索：时效性/不确定/要求核实时仍要搜。"""
    instruction = build_semantic_search_instruction(0.7, has_memory_hits=False)
    assert "对具体但不确定、易变化、实体冲突或用户要求核实时优先搜索" in instruction
    assert "一次回答最多启动一条搜索链" in instruction


def test_fact_guard_does_not_blanket_exempt_self_questions():
    """「关于你自己」不能整体豁免：设定一致性、官方细节、共同往事仍要有据。"""
    instruction = build_factual_grounding_instruction(
        has_memory_hits=False,
        domain_restricted=False,
    )
    assert "但「关于你自己」不等于全部豁免" in instruction
    assert "官方剧情、配音、版本数据这类细节仍按外部事实处理" in instruction
    assert "对方提到的共同往事也要有据可依" in instruction


def test_both_paths_share_search_honesty_rule():
    """两条注入路径对"未检索不能说查过"保持一致，避免规则冲突。"""
    hint = build_semantic_search_instruction(0.7, has_memory_hits=False)
    grounding = build_factual_grounding_instruction(
        has_memory_hits=False, domain_restricted=False
    )
    # 两条路径都要有"不虚构"与"未检索不说查过"
    for text in (hint, grounding):
        assert "虚构" in text
        assert "查过" in text
