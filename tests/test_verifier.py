"""verifier 模块单元测试。

覆盖：搜索源选择、关键词提取、多源收集与去重、LLM 自辩论提示词分支、
仲裁结果解析、来源一致性判定、置信度调整、reason 标签，以及 run() 全流程。

所有 LLM / 搜索依赖均由本文件内的 fake 注入，不联网、不调用真实 provider。
环境无 pytest-asyncio，异步方法统一用 asyncio.run 驱动。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.models import MemoryEntry
from astrbot_plugin_active_learner.verifier import (
    VerificationResult,
    Verifier,
    _DebateResult,
)


# ---------- 测试替身 ----------


class _FakeLLMService:
    """按调用顺序返回预置文本的假 LLMService。

    replies 中的元素若为 Exception 实例则抛出，用于覆盖"LLM 抛异常"路径。
    队列耗尽后返回空串，用于覆盖"空返回"路径。
    """

    def __init__(self, replies=()):
        self._replies = list(replies)
        self.calls = []

    async def generate(self, prompt="", provider_id=None):
        self.calls.append({"prompt": prompt, "provider_id": provider_id})
        if not self._replies:
            return ""
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class _FakeStore:
    """记录 update_content 的入参，断言 run() 的落库副作用。"""

    def __init__(self):
        self.updates = []

    def update_content(self, **kwargs):
        self.updates.append(kwargs)
        return True


class _FakePlugin:
    """最小可用的 plugin 替身，只提供 Verifier 真正读到的属性。"""

    def __init__(
        self,
        config=None,
        llm_replies=(),
        search_batches=None,
        enable_web_search=True,
        only_highest_priority=False,
        priority=None,
    ):
        self.config = config if config is not None else {}
        self.llm_service = _FakeLLMService(llm_replies)
        self.store = _FakeStore()
        self._enable_web_search = enable_web_search
        self._web_search_only_highest_priority = only_highest_priority
        self._knowledge_source_priority = priority if priority is not None else ["web"]
        # 每次 _search_external_sources 调用按顺序返回一批结果
        self._search_batches = list(search_batches or [])
        self.search_calls = []

    async def _search_external_sources(
        self, query, web_limit=5, bili_limit=3, allowed_sources=None
    ):
        self.search_calls.append(
            {
                "query": query,
                "web_limit": web_limit,
                "bili_limit": bili_limit,
                "allowed_sources": set(allowed_sources or set()),
            }
        )
        if not self._search_batches:
            return []
        batch = self._search_batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return [dict(item) for item in batch]


def _source(title, snippet="片段", source_type="web"):
    return {"title": title, "snippet": snippet, "source_type": source_type}


def _entry(content="原始内容", topic="测试主题", confidence=0.4):
    return MemoryEntry(
        id="mem-1",
        scope_type="private",
        scope_id="u1",
        topic=topic,
        content=content,
        confidence=confidence,
        source="来源A",
    )


_ARBITER_CORRECT = (
    "VERDICT: 正确\n"
    "CONFIDENCE: 88\n"
    "CONTENT: 修正后的内容文本\n"
    "REASON: 两个独立来源互相印证"
)


# ---------- VerificationResult ----------


def test_to_text_renders_verdict_confidence_and_consistency():
    result = VerificationResult(
        verdict="correct",
        confidence=0.876,
        content="c",
        reasoning="推理过程",
        sources_count=3,
        sources_consistent=True,
    )
    text = result.to_text()
    # 面向用户的文案：verdict 需翻译成中文、置信度按百分比取整
    assert "✅ 正确" in text
    assert "88%" in text, "0.876 按 :.0% 渲染应为 88%"
    assert "来源数: 3（来源一致）" in text
    assert text.endswith("推理过程"), "推理过程固定拼在末尾，供用户查看依据"


def test_to_text_marks_inconsistent_sources_and_unknown_verdict():
    result = VerificationResult(
        verdict="weird",
        confidence=0.0,
        content="",
        reasoning="r",
        sources_count=0,
        sources_consistent=False,
    )
    text = result.to_text()
    # 未知 verdict 无中文映射时原样输出，避免丢信息
    assert "验证结论: weird" in text
    assert "来源存在分歧" in text


def test_debug_info_defaults_to_empty_dict():
    # debug_info 为 None 时必须变成 {}，否则 Dashboard 读取会 TypeError
    assert VerificationResult("correct", 1.0, "c", "r", 1, True).debug_info == {}


# ---------- _get_search_source ----------


def test_get_search_source_returns_config_value_by_default():
    verifier = Verifier(_FakePlugin(config={"verifier_search_source": "WEB+BILIBILI"}))
    # 配置值统一转小写，避免大小写不一致导致后续 in 判断失配
    assert verifier._get_search_source() == "web+bilibili"


def test_get_search_source_falls_back_to_auto_on_empty_config():
    # 配置缺失或为空字符串时回落 auto（同时用 web 和 bilibili）
    assert Verifier(_FakePlugin())._get_search_source() == "auto"
    empty_cfg = _FakePlugin(config={"verifier_search_source": ""})
    assert Verifier(empty_cfg)._get_search_source() == "auto"


def test_get_search_source_forced_to_llm_when_web_search_disabled():
    verifier = Verifier(
        _FakePlugin(config={"verifier_search_source": "web"}, enable_web_search=False)
    )
    # 联网搜索总开关关闭时，必须无条件降级纯 LLM，不能绕过总开关发请求
    assert verifier._get_search_source() == "llm"


@pytest.mark.parametrize(
    "priority,expected",
    [
        (["web", "bilibili"], "web"),
        (["bilibili", "web"], "bilibili"),
        (["local"], "llm"),
        ([], "web"),
    ],
)
def test_get_search_source_only_highest_priority(priority, expected):
    verifier = Verifier(
        _FakePlugin(
            config={"verifier_search_source": "auto"},
            only_highest_priority=True,
            priority=priority,
        )
    )
    # 仅用最高优先级来源时，取 priority[0]；非 web/bilibili 的来源没有搜索实现，退回 llm
    assert verifier._get_search_source() == expected


# ---------- _safe_llm_generate ----------


def test_safe_llm_generate_short_circuits_without_provider():
    plugin = _FakePlugin(llm_replies=["不应被调用"])
    verifier = Verifier(plugin)
    text = asyncio.run(verifier._safe_llm_generate("", "prompt"))
    # provider_id 为空时直接返回占位文案，且不能真的发起调用（省一次无效请求）
    assert text == "（LLM 不可用）"
    assert plugin.llm_service.calls == []


def test_safe_llm_generate_returns_placeholder_on_empty_reply():
    plugin = _FakePlugin(llm_replies=[""])
    text = asyncio.run(Verifier(plugin)._safe_llm_generate("pid", "prompt"))
    # LLMService 约定失败返回空串，这里转成占位文案，保证下游拼 prompt 不会出现空洞
    assert text == "（LLM 调用失败）"


def test_safe_llm_generate_passes_prompt_and_provider_through():
    plugin = _FakePlugin(llm_replies=["回答"])
    text = asyncio.run(Verifier(plugin)._safe_llm_generate("pid-a", "问题"))
    assert text == "回答"
    assert plugin.llm_service.calls == [{"prompt": "问题", "provider_id": "pid-a"}], (
        "必须以关键字参数透传，签名不匹配会在运行期才炸"
    )


def test_safe_llm_generate_propagates_llm_exception():
    """当前实现没有 try/except，LLM 抛异常会直接冒泡。

    方法名与 docstring 声称「容错」，此处固化真实行为，作为疑似缺陷的回归锚点。
    """
    plugin = _FakePlugin(llm_replies=[RuntimeError("provider 500")])
    with pytest.raises(RuntimeError, match="provider 500"):
        asyncio.run(Verifier(plugin)._safe_llm_generate("pid", "prompt"))


# ---------- _extract_keywords ----------


def test_extract_keywords_parses_keywords_line():
    plugin = _FakePlugin(llm_replies=["KEYWORDS: 三体, 刘慈欣, 科幻小说"])
    keywords, prompt, reply = asyncio.run(
        Verifier(plugin)._extract_keywords("三体", "三体是刘慈欣的科幻小说", "pid")
    )
    assert keywords == ["三体", "刘慈欣", "科幻小说"]
    # prompt / reply 一并返回用于写 debug_info，方便管理页复现
    assert "提取 3-5 个适合搜索引擎检索的关键词" in prompt
    assert reply.startswith("KEYWORDS:")


def test_extract_keywords_truncates_to_five():
    plugin = _FakePlugin(llm_replies=["KEYWORDS: a, b, c, d, e, f, g"])
    keywords, _, _ = asyncio.run(Verifier(plugin)._extract_keywords("t", "c", "pid"))
    # 关键词过多会让搜索 query 变噪声，实现固定截断前 5 个
    assert keywords == ["a", "b", "c", "d", "e"]


def test_extract_keywords_falls_back_to_topic_on_malformed_reply():
    plugin = _FakePlugin(llm_replies=["抱歉，我无法完成该任务。"])
    keywords, _, reply = asyncio.run(
        Verifier(plugin)._extract_keywords("量子纠缠", "claim", "pid")
    )
    # 格式错误（无 KEYWORDS 前缀）时用 topic 兜底，保证后续搜索仍有 query
    assert keywords == ["量子纠缠"]
    assert reply == "抱歉，我无法完成该任务。"


def test_extract_keywords_empty_topic_yields_empty_list():
    plugin = _FakePlugin(llm_replies=["KEYWORDS: ,,, "])
    keywords, _, _ = asyncio.run(Verifier(plugin)._extract_keywords("", "c", "pid"))
    # KEYWORDS 行只有分隔符时切分结果全为空，topic 也为空 → 返回 []
    assert keywords == []


def test_extract_keywords_without_provider_uses_placeholder_reply():
    plugin = _FakePlugin()
    keywords, _, reply = asyncio.run(
        Verifier(plugin)._extract_keywords("主题", "c", "")
    )
    # 无 provider 时 reply 是占位文案，不含 KEYWORDS，因此走 topic 兜底
    assert reply == "（LLM 不可用）"
    assert keywords == ["主题"]


# ---------- _collect_sources ----------


def test_collect_sources_auto_uses_both_sources_and_keyword_query():
    plugin = _FakePlugin(
        search_batches=[
            [_source("主搜索结果"), _source("B站视频", source_type="bilibili")],
            [_source("事实核查页")],
        ]
    )
    sources = asyncio.run(
        Verifier(plugin)._collect_sources(
            "三体", "claim", "auto", ["三体", "刘慈欣", "科幻", "多余词"]
        )
    )
    main_call, alt_call = plugin.search_calls
    # auto 模式同时放开 web 与 bilibili，主 query 取前 3 个关键词拼接
    assert main_call["allowed_sources"] == {"web", "bilibili"}
    assert main_call["query"] == "三体 刘慈欣 科幻"
    # 第二次是事实核查补搜：只走 web，query 为 topic + 首关键词
    assert alt_call["allowed_sources"] == {"web"}
    assert alt_call["query"] == "三体 三体"
    assert alt_call["bili_limit"] == 0
    # 补搜结果的 source_type 被改写成 web_factcheck，才能在一致性判定中算作独立来源
    assert [s["source_type"] for s in sources] == ["web", "bilibili", "web_factcheck"]


def test_collect_sources_uses_topic_when_no_keywords():
    plugin = _FakePlugin(search_batches=[[_source("结果")]])
    asyncio.run(Verifier(plugin)._collect_sources("纯主题", "claim", "web", []))
    # 无关键词时退回 topic 作为 query；且没有关键词就不做补搜，只调用一次
    assert len(plugin.search_calls) == 1
    assert plugin.search_calls[0]["query"] == "纯主题"


def test_collect_sources_bilibili_only_skips_web_and_factcheck():
    plugin = _FakePlugin(search_batches=[[_source("B站", source_type="bilibili")]])
    sources = asyncio.run(
        Verifier(plugin)._collect_sources("t", "claim", "bilibili", ["kw"])
    )
    # bilibili 模式不放开 web，也不触发只走 web 的补搜
    assert plugin.search_calls[0]["allowed_sources"] == {"bilibili"}
    assert len(plugin.search_calls) == 1
    assert len(sources) == 1


def test_collect_sources_factcheck_ignores_non_web_items():
    plugin = _FakePlugin(
        search_batches=[
            [_source("主结果")],
            [_source("补搜里的B站", source_type="bilibili")],
        ]
    )
    sources = asyncio.run(
        Verifier(plugin)._collect_sources("t", "claim", "web", ["kw"])
    )
    # 补搜只接受 source_type == 'web' 的条目，其他类型丢弃
    assert [s["title"] for s in sources] == ["主结果"]


def test_collect_sources_dedups_by_title_prefix():
    long_a = "同一篇文章的超长标题" * 4 + "尾部A"
    long_b = "同一篇文章的超长标题" * 4 + "尾部B"
    plugin = _FakePlugin(
        search_batches=[[_source(long_a), _source(long_b), _source("")], []]
    )
    sources = asyncio.run(
        Verifier(plugin)._collect_sources("t", "claim", "web", ["kw"])
    )
    # 去重键是 title[:30]，前 30 字符相同即视为重复；空标题直接丢弃
    assert [s["title"] for s in sources] == [long_a]


def test_collect_sources_returns_empty_for_unknown_config():
    plugin = _FakePlugin(search_batches=[[_source("x")]])
    sources = asyncio.run(
        Verifier(plugin)._collect_sources("t", "claim", "unknown", ["kw"])
    )
    # 未知 source_cfg 既不启用 web 也不启用 bilibili，直接返回空且不发搜索
    assert sources == []
    assert plugin.search_calls == []


def test_collect_sources_propagates_search_exception():
    plugin = _FakePlugin(search_batches=[RuntimeError("搜索服务不可用")])
    with pytest.raises(RuntimeError, match="搜索服务不可用"):
        asyncio.run(Verifier(plugin)._collect_sources("t", "claim", "web", ["kw"]))


# ---------- _llm_debate ----------


def test_llm_debate_three_rounds_with_sources():
    plugin = _FakePlugin(llm_replies=["支持方论证", "质疑方论证", _ARBITER_CORRECT])
    result, prompts, replies = asyncio.run(
        Verifier(plugin)._llm_debate(
            "三体",
            "三体是刘慈欣写的",
            [_source("百科", "刘慈欣创作"), _source("视频", "解读", "bilibili")],
            "pid",
        )
    )
    # 固定三轮：支持方 → 质疑方 → 仲裁，step 名用于管理页展示
    assert [p["step"] for p in prompts] == [
        "debate_round_a_supportive",
        "debate_round_b_skeptical",
        "debate_round_c_arbiter",
    ]
    assert [r["text"] for r in replies] == [
        "支持方论证",
        "质疑方论证",
        _ARBITER_CORRECT,
    ]
    # 有来源时 Round A 要求引用来源编号，并把来源正文注入 prompt
    assert "引用具体来源编号" in prompts[0]["text"]
    assert "[来源1] (web) 百科" in prompts[0]["text"]
    assert "[来源2] (bilibili) 视频" in prompts[0]["text"]
    # 质疑方 prompt 需带上来源与支持方原文，才能针对性反驳
    assert "来源不足或引用偏差" in prompts[1]["text"]
    assert "支持方论证" in prompts[1]["text"]
    # 仲裁 prompt 同时含两方论证
    assert "质疑方论证" in prompts[2]["text"]
    assert result.verdict == "correct"


def test_llm_debate_llm_only_switches_prompt_wording():
    plugin = _FakePlugin(llm_replies=["A", "B", _ARBITER_CORRECT])
    _, prompts, _ = asyncio.run(
        Verifier(plugin)._llm_debate("t", "claim", [], "pid", llm_only=True)
    )
    round_a, round_b = prompts[0]["text"], prompts[1]["text"]
    # 纯 LLM 模式下不得要求引用来源（无来源可引），改为基于知识库判断
    assert "引用具体来源编号" not in round_a
    assert "请基于你的知识" in round_a
    assert "搜索来源" not in round_b
    assert "知识盲区或过时信息" in round_b


def test_llm_debate_without_sources_but_not_llm_only_uses_placeholder():
    plugin = _FakePlugin(llm_replies=["A", "B", _ARBITER_CORRECT])
    _, prompts, _ = asyncio.run(
        Verifier(plugin)._llm_debate("t", "claim", [], "pid", llm_only=False)
    )
    # sources 为空但未标记 llm_only 时，来源段填占位说明，避免 prompt 出现空块
    assert "（无外部搜索源，请基于你的知识库判断）" in prompts[0]["text"]


def test_llm_debate_truncates_sources_to_eight():
    plugin = _FakePlugin(llm_replies=["A", "B", _ARBITER_CORRECT])
    sources = [_source(f"标题{i}") for i in range(12)]
    _, prompts, _ = asyncio.run(
        Verifier(plugin)._llm_debate("t", "claim", sources, "pid")
    )
    # 只注入前 8 条，控制 prompt 长度防止超 context
    assert "[来源8] (web) 标题7" in prompts[0]["text"]
    assert "[来源9]" not in prompts[0]["text"]


def test_llm_debate_all_empty_replies_degrade_to_inconclusive():
    plugin = _FakePlugin(llm_replies=[])
    result, _, replies = asyncio.run(
        Verifier(plugin)._llm_debate("t", "claim", [], "pid")
    )
    # 三轮全空返回时被转成占位文案，仲裁解析不到 VERDICT → inconclusive
    assert all(r["text"] == "（LLM 调用失败）" for r in replies)
    assert result.verdict == "inconclusive"
    assert result.confidence == 0.5


def test_llm_debate_propagates_exception_from_arbiter_round():
    plugin = _FakePlugin(llm_replies=["A", "B", TimeoutError("仲裁超时")])
    with pytest.raises(TimeoutError, match="仲裁超时"):
        asyncio.run(Verifier(plugin)._llm_debate("t", "claim", [], "pid"))


# ---------- _parse_debate_result ----------


@pytest.mark.parametrize(
    "verdict_line,expected",
    [
        ("VERDICT: 正确", "correct"),
        ("VERDICT: 完全正确", "correct"),
        ("VERDICT: 部分正确", "partial"),
        ("VERDICT: 错误", "wrong"),
        ("VERDICT: 说法错误", "wrong"),
        ("VERDICT: 无法确认", "inconclusive"),
        ("VERDICT: ", "inconclusive"),
    ],
)
def test_parse_debate_result_verdict_mapping(verdict_line, expected):
    verifier = Verifier(_FakePlugin())
    result = verifier._parse_debate_result(f"{verdict_line}\nCONFIDENCE: 60")
    # 中文 verdict 到内部枚举的映射是后续所有决策的入口，必须逐一固定
    assert result.verdict == expected


def test_parse_debate_result_full_fields():
    text = (
        "VERDICT: 部分正确\n"
        "CONFIDENCE: 72\n"
        "CONTENT: 三体由刘慈欣创作，\n首版于 2008 年\n"
        "REASON: 出版年份与来源略有分歧"
    )
    result = Verifier(_FakePlugin())._parse_debate_result(text)
    assert result.verdict == "partial"
    assert result.confidence == 0.72, "CONFIDENCE 是 0-100 整数，需除以 100"
    # CONTENT 用 DOTALL + 前视到 REASON，允许跨行
    assert result.content == "三体由刘慈欣创作，\n首版于 2008 年"
    assert result.reasoning == "出版年份与来源略有分歧"


def test_parse_debate_result_defaults_when_fields_missing():
    result = Verifier(_FakePlugin())._parse_debate_result(
        "模型跑偏了，输出了一段散文。"
    )
    # 格式完全错误时：verdict 保守取 inconclusive、置信度 0.5、reasoning 退回原文
    assert result.verdict == "inconclusive"
    assert result.confidence == 0.5
    assert result.content == ""
    assert result.reasoning == "模型跑偏了，输出了一段散文。"


def test_parse_debate_result_empty_text():
    result = Verifier(_FakePlugin())._parse_debate_result("")
    # 空文本不应抛异常，走全默认值
    assert (result.verdict, result.confidence, result.reasoning) == (
        "inconclusive",
        0.5,
        "",
    )


@pytest.mark.parametrize(
    "raw,expected",
    [("CONFIDENCE: 150", 1.0), ("CONFIDENCE: 0", 0.0), ("CONFIDENCE: 999", 1.0)],
)
def test_parse_debate_result_clamps_confidence(raw, expected):
    result = Verifier(_FakePlugin())._parse_debate_result(f"VERDICT: 正确\n{raw}")
    # 模型可能输出越界分数，实现钳到 0-100 后再归一化
    assert result.confidence == expected


def test_parse_debate_result_verdict_with_both_correct_and_wrong_is_wrong():
    """「正确」与「错误」同时出现时判为 wrong。

    如「该说法正确，但细节有错误」会被判成 wrong，属于关键词匹配的固有歧义，
    此处固化现状并在报告中列为疑似缺陷。
    """
    result = Verifier(_FakePlugin())._parse_debate_result(
        "VERDICT: 该说法正确，但细节有错误\nCONFIDENCE: 60"
    )
    assert result.verdict == "wrong"


# ---------- _check_consistency ----------


@pytest.mark.parametrize(
    "types,verdict,expected",
    [
        (["web", "bilibili"], "correct", True),
        (["web", "web_factcheck"], "wrong", True),
        (["web", "bilibili"], "partial", False),
        (["web", "bilibili"], "inconclusive", False),
        (["web", "web", "web"], "correct", True),
        (["web", "web"], "correct", False),
        (["web", "web", "web"], "wrong", False),
        ([], "correct", False),
    ],
)
def test_check_consistency_rules(types, verdict, expected):
    sources = [_source(f"t{i}", source_type=t) for i, t in enumerate(types)]
    debate = _DebateResult(verdict=verdict, confidence=0.8, content="", reasoning="")
    # 规则一：来源类型 ≥2 种且结论为 correct/wrong；规则二：同类来源 ≥3 条且 correct
    assert Verifier(_FakePlugin())._check_consistency(sources, debate) is expected


# ---------- _adjust_confidence ----------


@pytest.mark.parametrize(
    "old,verdict,consistent,llm_only,expected",
    [
        (0.4, "correct", True, False, 0.6),
        (0.4, "correct", False, False, 0.5),
        (0.4, "correct", True, True, 0.55),
        (0.95, "correct", True, False, 1.0),
        (0.4, "wrong", True, False, 0.15),
        (0.2, "wrong", True, False, 0.1),
        (0.4, "partial", False, False, 0.45),
        (0.99, "partial", False, False, 1.0),
        (0.4, "inconclusive", True, False, 0.4),
        (0.4, "unknown", True, False, 0.4),
    ],
)
def test_adjust_confidence(old, verdict, consistent, llm_only, expected):
    got = Verifier(_FakePlugin())._adjust_confidence(
        old, verdict, consistent, llm_only=llm_only
    )
    # correct 加成受一致性/纯 LLM 影响；wrong 重罚但有 0.1 下限；上限钳 1.0
    assert got == pytest.approx(expected), (
        f"{verdict}/consistent={consistent}/llm_only={llm_only} 的置信度调整不符"
    )


# ---------- _reason_tag ----------


@pytest.mark.parametrize(
    "verdict,consistent,llm_only,expected",
    [
        ("correct", True, False, "verify_passed"),
        ("correct", True, True, "verify_passed_llm"),
        ("correct", False, False, "verify_inconclusive"),
        ("wrong", False, False, "challenge_corrected"),
        ("wrong", True, True, "challenge_corrected"),
        ("partial", True, False, "verify_partial"),
        ("inconclusive", True, False, "verify_inconclusive"),
    ],
)
def test_reason_tag(verdict, consistent, llm_only, expected):
    got = Verifier(_FakePlugin())._reason_tag(verdict, consistent, llm_only=llm_only)
    # reason 会写进 memory_versions，用于回溯某次改写的原因，取值需稳定
    assert got == expected


def test_reason_tag_correct_but_inconsistent_is_inconclusive():
    """correct 且来源不一致时落到 verify_inconclusive，而非 verify_passed。

    此时置信度实际是上调的（+0.10），reason 标签却是 inconclusive，语义不一致，
    在报告中列为疑似缺陷。
    """
    assert (
        Verifier(_FakePlugin())._reason_tag("correct", False) == "verify_inconclusive"
    )


# ---------- run() 全流程 ----------


def test_run_multi_source_correct_updates_memory_and_debug_info():
    plugin = _FakePlugin(
        config={"verifier_search_source": "auto"},
        llm_replies=[
            "KEYWORDS: 三体, 刘慈欣",
            "支持方",
            "质疑方",
            _ARBITER_CORRECT,
        ],
        search_batches=[
            [_source("百科"), _source("B站解读", source_type="bilibili")],
            [_source("核查站")],
        ],
    )
    entry = _entry(confidence=0.4)
    result = asyncio.run(Verifier(plugin).run(entry, "pid-a"))

    # 三类来源 + correct → 一致，置信度 0.4 + 0.20
    assert result.sources_count == 3
    assert result.sources_consistent is True
    assert result.confidence == pytest.approx(0.6)
    assert result.verdict == "correct"
    # verdict=correct 不改写内容，仍是原 entry.content
    assert result.content == "原始内容"

    update = plugin.store.updates[0]
    assert update["entry_id"] == "mem-1"
    assert update["verified"] is True, "correct 且置信度≥0.5 才算通过验证"
    assert update["reason"] == "verify_passed"
    assert update["snapshot"] is True, "必须写版本快照，便于回溯改写历史"
    assert update["source"].startswith("来源A | 验证于 ")

    # debug_info 供管理页排查：4 次 LLM 调用（关键词 + 三轮辩论）都要留痕
    debug = result.debug_info
    assert debug["keywords"] == ["三体", "刘慈欣"]
    assert debug["source_cfg"] == "auto"
    assert debug["provider_id"] == "pid-a"
    assert [p["step"] for p in debug["prompts"]] == [
        "extract_keywords",
        "debate_round_a_supportive",
        "debate_round_b_skeptical",
        "debate_round_c_arbiter",
    ]
    assert len(debug["replies"]) == 4
    assert len(debug["sources"]) == 3


def test_run_llm_only_config_skips_search_entirely():
    plugin = _FakePlugin(
        config={"verifier_search_source": "llm"},
        llm_replies=["KEYWORDS: kw", "支持方", "质疑方", _ARBITER_CORRECT],
        search_batches=[[_source("不应被用到")]],
    )
    result = asyncio.run(Verifier(plugin).run(_entry(confidence=0.4), "pid"))
    # 配置为 llm 时完全不发搜索请求
    assert plugin.search_calls == []
    assert result.sources_count == 0
    # llm_only 下一致性恒为 True，加成用 0.15
    assert result.sources_consistent is True
    assert result.confidence == pytest.approx(0.55)
    assert plugin.store.updates[0]["reason"] == "verify_passed_llm"


def test_run_degrades_to_llm_only_when_fewer_than_two_sources():
    plugin = _FakePlugin(
        config={"verifier_search_source": "web"},
        llm_replies=["KEYWORDS: kw", "支持方", "质疑方", _ARBITER_CORRECT],
        search_batches=[[_source("唯一来源")], []],
    )
    result = asyncio.run(Verifier(plugin).run(_entry(confidence=0.4), "pid"))
    # 只有 1 个来源不足以交叉验证，降级为纯 LLM：一致性为 True，加成 0.15
    assert result.sources_count == 1
    assert result.sources_consistent is True
    assert result.confidence == pytest.approx(0.55)
    assert plugin.store.updates[0]["reason"] == "verify_passed_llm"


def test_run_wrong_verdict_rewrites_content_and_lowers_confidence():
    plugin = _FakePlugin(
        config={"verifier_search_source": "auto"},
        llm_replies=[
            "KEYWORDS: kw",
            "支持方",
            "质疑方",
            "VERDICT: 错误\nCONFIDENCE: 20\nCONTENT: 正确版本内容\nREASON: 与权威来源矛盾",
        ],
        search_batches=[
            [_source("A"), _source("B", source_type="bilibili")],
            [_source("C")],
        ],
    )
    result = asyncio.run(Verifier(plugin).run(_entry(confidence=0.4), "pid"))
    # wrong 时用仲裁给出的 CONTENT 覆盖原内容
    assert result.content == "正确版本内容"
    assert result.confidence == pytest.approx(0.15), "wrong 固定扣 0.25"
    update = plugin.store.updates[0]
    assert update["verified"] is False, "wrong 不能标记为已验证"
    assert update["reason"] == "challenge_corrected"
    assert update["content"] == "正确版本内容"
    assert result.reasoning == "与权威来源矛盾"


def test_run_partial_verdict_rewrites_content_and_bumps_confidence():
    plugin = _FakePlugin(
        config={"verifier_search_source": "llm"},
        llm_replies=[
            "KEYWORDS: kw",
            "支持方",
            "质疑方",
            "VERDICT: 部分正确\nCONFIDENCE: 55\nCONTENT: 补充边界条件后的内容\nREASON: 主体成立",
        ],
    )
    result = asyncio.run(Verifier(plugin).run(_entry(confidence=0.5), "pid"))
    # partial 也接受修正内容，且置信度轻微上调 0.05
    assert result.content == "补充边界条件后的内容"
    assert result.confidence == pytest.approx(0.55)
    assert plugin.store.updates[0]["verified"] is True, (
        "partial 且置信度≥0.5 同样算已验证"
    )
    assert plugin.store.updates[0]["reason"] == "verify_partial"


def test_run_keeps_original_content_when_wrong_but_no_content_field():
    plugin = _FakePlugin(
        config={"verifier_search_source": "llm"},
        llm_replies=[
            "KEYWORDS: kw",
            "支持方",
            "质疑方",
            "VERDICT: 错误\nCONFIDENCE: 10\nREASON: 缺少修正内容",
        ],
    )
    result = asyncio.run(Verifier(plugin).run(_entry(confidence=0.4), "pid"))
    # 仲裁没给 CONTENT 时不能把内容清空，必须保留原文
    assert result.content == "原始内容"
    assert plugin.store.updates[0]["content"] == "原始内容"


def test_run_inconclusive_keeps_confidence_and_is_not_verified():
    plugin = _FakePlugin(
        config={"verifier_search_source": "llm"},
        llm_replies=["KEYWORDS: kw", "支持方", "质疑方", "模型没按格式输出"],
    )
    entry = _entry(confidence=0.62)
    result = asyncio.run(Verifier(plugin).run(entry, "pid"))
    # inconclusive 保持原置信度不变，避免反复验证把置信度磨没
    assert result.confidence == pytest.approx(0.62)
    assert result.verdict == "inconclusive"
    assert plugin.store.updates[0]["verified"] is False
    assert plugin.store.updates[0]["reason"] == "verify_inconclusive"


def test_run_partial_below_threshold_is_not_verified():
    plugin = _FakePlugin(
        config={"verifier_search_source": "llm"},
        llm_replies=[
            "KEYWORDS: kw",
            "支持方",
            "质疑方",
            "VERDICT: 部分正确\nCONFIDENCE: 80\nCONTENT: x\nREASON: r",
        ],
    )
    # 原置信度 0.2 + 0.05 = 0.25 < 0.5，verified 阈值卡在最终置信度而非仲裁分数
    asyncio.run(Verifier(plugin).run(_entry(confidence=0.2), "pid"))
    assert plugin.store.updates[0]["verified"] is False
    assert plugin.store.updates[0]["confidence"] == pytest.approx(0.25)


def test_run_uses_explicit_claim_instead_of_entry_content():
    plugin = _FakePlugin(
        config={"verifier_search_source": "llm"},
        llm_replies=["KEYWORDS: kw", "支持方", "质疑方", _ARBITER_CORRECT],
    )
    result = asyncio.run(
        Verifier(plugin).run(
            _entry(content="库里的旧说法"), "pid", claim="用户质疑的新说法"
        )
    )
    # 显式传 claim 时，验证对象是 claim；但更新落库的基线仍是 entry.content
    assert result.debug_info["claim"] == "用户质疑的新说法"
    assert "用户质疑的新说法" in plugin.llm_service.calls[0]["prompt"]
    assert result.content == "库里的旧说法"


def test_run_without_provider_still_completes_and_writes_store():
    plugin = _FakePlugin(config={"verifier_search_source": "llm"})
    result = asyncio.run(Verifier(plugin).run(_entry(confidence=0.3), "", claim=None))
    # 无 provider 时全程走占位文案，结论 inconclusive，但流程不能崩、仍需落库
    assert plugin.llm_service.calls == []
    assert result.verdict == "inconclusive"
    assert result.confidence == pytest.approx(0.3)
    assert len(plugin.store.updates) == 1


def test_run_propagates_search_failure():
    plugin = _FakePlugin(
        config={"verifier_search_source": "web"},
        llm_replies=["KEYWORDS: kw"],
        search_batches=[ConnectionError("网络不可达")],
    )
    # 搜索异常没有被 run() 捕获，会冒泡到调用方；同时不会产生落库副作用
    with pytest.raises(ConnectionError, match="网络不可达"):
        asyncio.run(Verifier(plugin).run(_entry(), "pid"))
    assert plugin.store.updates == []
