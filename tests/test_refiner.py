"""refiner 模块单元测试。

覆盖：4 个精炼入口（搜索结果 / 导入 / 对话片段 / 批量）的正常与降级路径、
结构化响应解析（摘要、关键词切分、置信度钳制）、融合判断，以及字段正则提取。

LLM 依赖由本文件内的 fake 注入，不联网、不调用真实 provider。
环境无 pytest-asyncio，异步方法统一用 asyncio.run 驱动。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.refiner import (
    KnowledgeRefiner,
    MergeDecision,
    RefineResult,
)


# ---------- 测试替身 ----------


class _FakeLLMService:
    """按调用顺序返回预置文本。Exception 元素用于覆盖抛异常路径。"""

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


class _FakePlugin:
    def __init__(self, llm_replies=()):
        self.llm_service = _FakeLLMService(llm_replies)


def _refiner(*replies):
    plugin = _FakePlugin(llm_replies=replies)
    return KnowledgeRefiner(plugin), plugin


_GOOD_REPLY = (
    "SUMMARY: 三体是刘慈欣创作的科幻长篇小说，首部出版于 2008 年。\n"
    "KEYWORDS: 三体, 刘慈欣, 科幻小说\n"
    "CONFIDENCE: 85\n"
    "REASON: 三个来源一致且信息完整"
)


# ---------- 数据类默认值 ----------


def test_refine_result_defaults():
    result = RefineResult(summary="s")
    # 默认 refined=True、置信度 0.5：调用方靠 refined 区分是否为降级结果
    assert result.keywords == []
    assert result.confidence == 0.5
    assert result.reasoning == ""
    assert result.refined is True


def test_refine_result_keywords_not_shared_between_instances():
    a, b = RefineResult(summary="a"), RefineResult(summary="b")
    a.keywords.append("x")
    # field(default_factory=list) 保证实例独立，否则关键词会跨条目串味
    assert b.keywords == []


def test_merge_decision_defaults_to_no_merge():
    decision = MergeDecision()
    # 默认不融合：融合是破坏性操作，必须由 LLM 显式判 yes 才执行
    assert decision.should_merge is False
    assert (decision.target_topic, decision.target_id, decision.merge_reason) == (
        "",
        "",
        "",
    )
    assert decision.refined is True


# ---------- _extract_field ----------


def test_extract_field_returns_match_stripped():
    got = KnowledgeRefiner._extract_field(
        "SUMMARY:   摘要正文  \n", r"SUMMARY:\s*(.+)", "fb"
    )
    # 提取后 strip，避免 LLM 输出的前后空白进入摘要
    assert got == "摘要正文"


def test_extract_field_returns_fallback_when_no_match():
    got = KnowledgeRefiner._extract_field("无相关字段", r"SUMMARY:\s*(.+)", "兜底值")
    # 不匹配时返回 fallback，让上层能拿到原始内容而非空串
    assert got == "兜底值"


def test_extract_field_dotall_crosses_newlines():
    text = "SUMMARY: 第一行\n第二行\nKEYWORDS: a"
    got = KnowledgeRefiner._extract_field(
        text, r"SUMMARY:\s*(.+?)(?=\n[A-Z]+:|\Z)", "fb"
    )
    # 用 DOTALL + 前视到下一个大写字段名，支持多行摘要
    assert got == "第一行\n第二行"


# ---------- _parse_result ----------


def test_parse_result_full_fields():
    refiner, _ = _refiner()
    result = refiner._parse_result(_GOOD_REPLY, fallback_summary="原文", topic="三体")
    assert result.summary.startswith("三体是刘慈欣创作的")
    assert result.keywords == ["三体", "刘慈欣", "科幻小说"]
    assert result.confidence == 0.85, "CONFIDENCE 是 0-100 整数，需除以 100"
    assert result.reasoning == "三个来源一致且信息完整"
    assert result.refined is True


@pytest.mark.parametrize("reply", ["", "   ", "\n\t "])
def test_parse_result_returns_none_for_blank_reply(reply):
    refiner, _ = _refiner()
    # 空/纯空白响应返回 None，由上层决定降级行为
    assert refiner._parse_result(reply, fallback_summary="原文", topic="t") is None


def test_parse_result_uses_fallback_summary_when_field_missing():
    refiner, _ = _refiner()
    result = refiner._parse_result(
        "KEYWORDS: 甲, 乙", fallback_summary="原始长文", topic="t"
    )
    # 格式错误缺 SUMMARY 时用 fallback_summary，保证摘要不为空
    assert result.summary == "原始长文"
    assert result.refined is True, "只要响应非空就算 refined，即使字段缺失"


def test_parse_result_falls_back_to_topic_when_no_keywords():
    refiner, _ = _refiner()
    result = refiner._parse_result(
        "SUMMARY: 摘要", fallback_summary="fb", topic="备用主题"
    )
    # 无关键词时用 topic 兜底，保证条目仍可被关键词检索命中
    assert result.keywords == ["备用主题"]


def test_parse_result_empty_keywords_and_empty_topic():
    refiner, _ = _refiner()
    result = refiner._parse_result("SUMMARY: 摘要", fallback_summary="fb", topic="")
    # topic 也为空时只能返回空关键词列表
    assert result.keywords == []


@pytest.mark.parametrize(
    "keywords_line,expected",
    [
        ("KEYWORDS: 三体, 刘慈欣、科幻 硬科幻", ["三体", "刘慈欣", "科幻", "硬科幻"]),
        ("KEYWORDS: 三体，刘慈欣", ["三体", "刘慈欣"]),
        ("KEYWORDS: ab, c, de", ["ab", "de"]),
    ],
)
def test_parse_result_keyword_splitting_and_length_filter(keywords_line, expected):
    refiner, _ = _refiner()
    result = refiner._parse_result(
        f"SUMMARY: s\n{keywords_line}", fallback_summary="fb", topic=""
    )
    # 半角/全角逗号、顿号、空格都作分隔符；长度 <2 的碎片被丢弃（单字符 "c" 被过滤）
    assert result.keywords == expected


def test_parse_result_drops_single_chinese_character_keywords():
    """长度 <2 的过滤按字符数算，中文单字关键词会被整体丢弃。

    如「猫」「狗」这类合法单字实体名无法保留，是疑似缺陷，此处固化现状。
    """
    refiner, _ = _refiner()
    result = refiner._parse_result(
        "SUMMARY: s\nKEYWORDS: 猫, 狗", fallback_summary="fb", topic=""
    )
    assert result.keywords == []


def test_parse_result_keywords_capped_at_eight():
    refiner, _ = _refiner()
    line = ", ".join(f"词{i}" for i in range(12))
    result = refiner._parse_result(
        f"SUMMARY: s\nKEYWORDS: {line}", fallback_summary="fb", topic="t"
    )
    # 关键词最多保留 8 个，防止索引膨胀
    assert len(result.keywords) == 8
    assert result.keywords[0] == "词0"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("CONFIDENCE: 0", 0.0),
        ("CONFIDENCE: 100", 1.0),
        ("CONFIDENCE: 150", 1.0),
        ("CONFIDENCE: 42", 0.42),
    ],
)
def test_parse_result_confidence_clamped(line, expected):
    refiner, _ = _refiner()
    result = refiner._parse_result(
        f"SUMMARY: s\n{line}", fallback_summary="fb", topic="t"
    )
    # 越界分数钳到 0-1，避免污染检索排序
    assert result.confidence == pytest.approx(expected)


def test_parse_result_non_numeric_confidence_keeps_default():
    refiner, _ = _refiner()
    result = refiner._parse_result(
        "SUMMARY: s\nCONFIDENCE: 高\nREASON: r", fallback_summary="fb", topic="t"
    )
    # 正则只匹配 \d+，"高"匹配不到 → confidence_str 为空 → 保持默认 0.5
    assert result.confidence == 0.5


def test_parse_result_blank_summary_swallows_next_field():
    """SUMMARY 值为空白时，摘要会吞掉下一行字段名。

    正则 `SUMMARY:\\s*(.+?)` 的 `\\s*` 会跨过换行，导致 KEYWORDS 整行被当成摘要正文，
    fallback_summary 反而不生效。属于疑似缺陷，此处固化现状。
    """
    refiner, _ = _refiner()
    result = refiner._parse_result(
        "SUMMARY:    \nKEYWORDS: 三体, 刘慈欣\nCONFIDENCE: 70",
        fallback_summary="兜底原文",
        topic="备用主题",
    )
    assert result.summary == "KEYWORDS: 三体, 刘慈欣"
    # 关键词是对全文独立匹配的，不受摘要越界影响，仍能正确解析
    assert result.keywords == ["三体", "刘慈欣"]
    assert result.confidence == 0.7
    # fallback_summary 因此没有生效，脏摘要被当成正常结果落库
    assert result.refined is True


def test_parse_result_whitespace_fallback_summary_stays_whitespace():
    refiner, _ = _refiner()
    result = refiner._parse_result("CONFIDENCE: 70", fallback_summary="   ", topic="t")
    # 无 SUMMARY 字段时 summary 取 fallback；strip 后为空则再次回落同一 fallback
    assert result.summary == "   "


# ---------- _safe_generate ----------


def test_safe_generate_passes_keyword_arguments():
    refiner, plugin = _refiner("回答")
    text = asyncio.run(refiner._safe_generate("pid-a", "问题"))
    assert text == "回答"
    assert plugin.llm_service.calls == [{"prompt": "问题", "provider_id": "pid-a"}], (
        "必须以关键字参数透传给 LLMService.generate，签名不符会在运行期才炸"
    )


def test_safe_generate_propagates_exception():
    """当前实现直接 await，docstring 声称的「失败返回空串」由 LLMService 保证。

    LLMService 之外的异常（如信号量、参数错误）会冒泡，此处固化真实行为。
    """
    refiner, _ = _refiner(RuntimeError("provider 崩了"))
    with pytest.raises(RuntimeError, match="provider 崩了"):
        asyncio.run(refiner._safe_generate("pid", "prompt"))


# ---------- refine_search_results ----------


def test_refine_search_results_without_provider_degrades():
    refiner, plugin = _refiner("不应被调用")
    long_text = "搜索正文" * 300
    result = asyncio.run(refiner.refine_search_results("三体", long_text, ["u1"], ""))
    # 无 provider 直接降级：截断前 500 字、topic 作关键词、refined=False
    assert result.refined is False
    assert result.summary == long_text[:500]
    assert len(result.summary) == 500
    assert result.keywords == ["三体"]
    assert result.confidence == 0.5
    assert result.reasoning == "未配置 LLM provider，跳过精炼"
    assert plugin.llm_service.calls == [], "无 provider 时不能发起 LLM 调用"


def test_refine_search_results_without_provider_and_empty_inputs():
    refiner, _ = _refiner()
    result = asyncio.run(refiner.refine_search_results("", "", [], ""))
    # topic 与正文都为空时摘要与关键词都为空，但不能抛异常
    assert (result.summary, result.keywords, result.refined) == ("", [], False)


def test_refine_search_results_normal_reply_and_prompt_shape():
    refiner, plugin = _refiner(_GOOD_REPLY)
    result = asyncio.run(
        refiner.refine_search_results("三体", "搜索结果正文", ["u1", "u2", "u3"], "pid")
    )
    assert result.refined is True
    assert result.keywords == ["三体", "刘慈欣", "科幻小说"]
    assert result.confidence == 0.85
    prompt = plugin.llm_service.calls[0]["prompt"]
    # 来源数量要注入 prompt，让 LLM 据此评估置信度
    assert "来源数量：3" in prompt
    assert "为「三体」生成结构化知识卡" in prompt
    assert "SUMMARY: <摘要>" in prompt


def test_refine_search_results_truncates_search_text_in_prompt():
    refiner, plugin = _refiner(_GOOD_REPLY)
    huge = "甲" * 5000
    asyncio.run(refiner.refine_search_results("t", huge, [], "pid"))
    prompt = plugin.llm_service.calls[0]["prompt"]
    # 搜索正文截断到 3000 字，防止超出 context
    assert "甲" * 3000 in prompt
    assert "甲" * 3001 not in prompt


def test_refine_search_results_empty_reply_degrades():
    refiner, _ = _refiner("")
    result = asyncio.run(
        refiner.refine_search_results("三体", "原始搜索正文", ["u1"], "pid")
    )
    # LLM 空返回 → _parse_result 返回 None → 降级返回原始内容
    assert result.refined is False
    assert result.summary == "原始搜索正文"
    assert result.reasoning == "LLM 精炼失败，降级返回原始内容"


def test_refine_search_results_malformed_reply_keeps_refined_with_fallback():
    refiner, _ = _refiner("我不确定这个问题。")
    result = asyncio.run(
        refiner.refine_search_results("三体", "原始搜索正文", ["u1"], "pid")
    )
    # 响应非空但格式全错：摘要回落原文、关键词回落 topic，refined 仍为 True
    assert result.refined is True, "非空响应即视为已精炼，是当前实现的判定口径"
    assert result.summary == "原始搜索正文"
    assert result.keywords == ["三体"]
    assert result.confidence == 0.5


def test_refine_search_results_propagates_exception():
    refiner, _ = _refiner(TimeoutError("LLM 超时"))
    with pytest.raises(TimeoutError, match="LLM 超时"):
        asyncio.run(refiner.refine_search_results("t", "text", [], "pid"))


# ---------- refine_import ----------


def test_refine_import_without_provider_keeps_full_raw_content():
    refiner, plugin = _refiner()
    raw = "原始导入内容" * 200
    result = asyncio.run(refiner.refine_import("主题", raw, ""))
    # 与 refine_search_results 不同：导入降级时不截断，完整保留原文
    assert result.summary == raw
    assert result.refined is False
    assert result.keywords == ["主题"]
    assert plugin.llm_service.calls == []


def test_refine_import_normal_reply():
    refiner, plugin = _refiner(_GOOD_REPLY)
    result = asyncio.run(refiner.refine_import("三体", "原始长文", "pid"))
    assert result.refined is True
    assert result.confidence == 0.85
    assert (
        "请把以下内容蒸馏为简洁、准确的知识卡" in plugin.llm_service.calls[0]["prompt"]
    )


def test_refine_import_returns_none_on_empty_reply():
    """refine_import 直接返回 _parse_result 的结果，空响应时返回 None。

    同族方法 refine_search_results / refine_snippet 都对 None 做了降级，
    只有本方法没有，调用方若不判空会 AttributeError，列为疑似缺陷。
    """
    refiner, _ = _refiner("")
    assert asyncio.run(refiner.refine_import("t", "raw", "pid")) is None


def test_refine_import_malformed_reply_falls_back_to_raw():
    refiner, _ = _refiner("模型答非所问")
    result = asyncio.run(refiner.refine_import("主题", "原始长文", "pid"))
    # 格式错误但响应非空时摘要回落原始内容
    assert result.summary == "原始长文"
    assert result.refined is True


def test_refine_import_propagates_exception():
    refiner, _ = _refiner(ValueError("参数错误"))
    with pytest.raises(ValueError, match="参数错误"):
        asyncio.run(refiner.refine_import("t", "raw", "pid"))


# ---------- refine_snippet ----------


def test_refine_snippet_without_provider_uses_lower_confidence():
    refiner, plugin = _refiner()
    result = asyncio.run(refiner.refine_snippet("主题", "对话原文", ""))
    # 对话片段未经精炼时可信度更低，默认 0.4（低于导入的 0.5）
    assert result.confidence == 0.4
    assert result.summary == "对话原文"
    assert result.refined is False
    assert result.reasoning == "未配置 LLM provider，直接存储原始片段"
    assert plugin.llm_service.calls == []


def test_refine_snippet_normal_reply():
    refiner, plugin = _refiner(_GOOD_REPLY)
    result = asyncio.run(refiner.refine_snippet("三体", "用户: 三体是谁写的", "pid"))
    assert result.refined is True
    assert result.keywords == ["三体", "刘慈欣", "科幻小说"]
    prompt = plugin.llm_service.calls[0]["prompt"]
    # prompt 需强调剔除闲聊，否则对话噪声会被存进知识库
    assert "剔除闲聊和无关内容" in prompt
    assert "用户在对话中提到了「三体」" in prompt


def test_refine_snippet_empty_reply_degrades_to_raw_snippet():
    refiner, _ = _refiner("")
    result = asyncio.run(refiner.refine_snippet("主题", "对话原文", "pid"))
    # 解析失败时降级存原片段，置信度同样压到 0.4
    assert result.refined is False
    assert result.summary == "对话原文"
    assert result.confidence == 0.4
    assert result.reasoning == "精炼解析失败，降级存储原始片段"


def test_refine_snippet_propagates_exception():
    refiner, _ = _refiner(RuntimeError("片段精炼失败"))
    with pytest.raises(RuntimeError, match="片段精炼失败"):
        asyncio.run(refiner.refine_snippet("t", "s", "pid"))


# ---------- refine_import_batch ----------


def test_refine_import_batch_without_provider_degrades_all():
    refiner, plugin = _refiner()
    results = asyncio.run(
        refiner.refine_import_batch(["t1", "t2"], ["raw1", "raw2"], "")
    )
    # 无 provider 时整体降级，逐条保留原文
    assert [r.summary for r in results] == ["raw1", "raw2"]
    assert all(r.refined is False for r in results)
    assert [r.reasoning for r in results] == ["未配置 provider", "未配置 provider"]
    assert plugin.llm_service.calls == []


def test_refine_import_batch_all_success():
    refiner, plugin = _refiner(_GOOD_REPLY, _GOOD_REPLY)
    results = asyncio.run(
        refiner.refine_import_batch(["t1", "t2"], ["raw1", "raw2"], "pid")
    )
    # 顺序执行（避免 provider 限流），每个 chunk 一次调用
    assert len(plugin.llm_service.calls) == 2
    assert all(r.refined is True for r in results)
    assert all(r.confidence == 0.85 for r in results)


def test_refine_import_batch_isolates_single_chunk_failure():
    refiner, _ = _refiner(_GOOD_REPLY, RuntimeError("第二块炸了"), _GOOD_REPLY)
    results = asyncio.run(
        refiner.refine_import_batch(["t1", "t2", "t3"], ["raw1", "raw2", "raw3"], "pid")
    )
    # 单 chunk 异常被捕获并降级，不影响其余 chunk 继续处理
    assert [r.refined for r in results] == [True, False, True]
    assert results[1].summary == "raw2"
    assert "第二块炸了" in results[1].reasoning
    assert results[1].keywords == ["t2"]


def test_refine_import_batch_zips_to_shortest_list():
    refiner, plugin = _refiner(_GOOD_REPLY)
    results = asyncio.run(
        refiner.refine_import_batch(["t1", "t2", "t3"], ["raw1"], "pid")
    )
    # zip 按最短列表对齐，多余的 topic 被静默丢弃（长度不匹配不会报错）
    assert len(results) == 1
    assert len(plugin.llm_service.calls) == 1


def test_refine_import_batch_empty_input():
    refiner, _ = _refiner()
    # 空输入返回空列表，不能抛异常
    assert asyncio.run(refiner.refine_import_batch([], [], "pid")) == []


# ---------- check_merge ----------


def test_check_merge_without_provider_returns_unrefined():
    refiner, plugin = _refiner("不应被调用")
    decision = asyncio.run(
        refiner.check_merge("糖猫", "s", ["k"], "米雪儿", "s2", ["k2"], "")
    )
    # 无 provider 时不做判断，交由调用方决定降级行为
    assert decision.refined is False
    assert decision.should_merge is False
    assert plugin.llm_service.calls == []


def test_check_merge_yes_sets_target_topic():
    refiner, plugin = _refiner(
        "DECISION: yes\nTARGET: 米雪儿\nREASON: 糖猫是米雪儿外号"
    )
    decision = asyncio.run(
        refiner.check_merge(
            "糖猫",
            "糖猫是米雪儿的外号",
            ["糖猫"],
            "米雪儿",
            "米雪儿简介",
            ["米雪儿"],
            "pid",
        )
    )
    assert decision.should_merge is True
    # target_topic 取自入参 existing_topic，而不是 LLM 输出的 TARGET
    assert decision.target_topic == "米雪儿"
    assert decision.merge_reason == "糖猫是米雪儿外号"
    assert decision.refined is True
    prompt = plugin.llm_service.calls[0]["prompt"]
    # 双方主题/内容/关键词都要进 prompt，LLM 才能判断是否同一实体
    assert "主题：米雪儿" in prompt and "主题：糖猫" in prompt
    assert "关键词：糖猫" in prompt


def test_check_merge_yes_leaves_target_id_empty():
    """DECISION: yes 时 target_id 始终为空串，实现从未填充该字段。

    调用方若依赖 target_id 定位记忆会拿不到值，列为疑似缺陷。
    """
    refiner, _ = _refiner("DECISION: yes\nTARGET: 米雪儿\nREASON: 别名")
    decision = asyncio.run(
        refiner.check_merge("糖猫", "s", [], "米雪儿", "s2", [], "pid")
    )
    assert decision.should_merge is True
    assert decision.target_id == ""


@pytest.mark.parametrize(
    "decision_line", ["DECISION: no", "DECISION: NO", "DECISION: 否"]
)
def test_check_merge_non_yes_means_no_merge(decision_line):
    refiner, _ = _refiner(f"{decision_line}\nREASON: 两者无关")
    decision = asyncio.run(
        refiner.check_merge("量子纠缠", "s", [], "米雪儿", "s2", [], "pid")
    )
    # 只有小写 yes 才融合；其余取值（含中文、大写 NO）一律不融合
    assert decision.should_merge is False
    assert decision.merge_reason == "两者无关"
    assert decision.refined is True


def test_check_merge_uppercase_yes_is_normalized():
    refiner, _ = _refiner("DECISION: YES\nTARGET: 米雪儿\nREASON: 别名")
    decision = asyncio.run(
        refiner.check_merge("糖猫", "s", [], "米雪儿", "s2", [], "pid")
    )
    # decision 先 lower() 再比较，大写 YES 也应生效
    assert decision.should_merge is True


@pytest.mark.parametrize("reply", ["", "   \n\t"])
def test_check_merge_blank_reply_returns_unrefined(reply):
    refiner, _ = _refiner(reply)
    decision = asyncio.run(refiner.check_merge("a", "s", [], "b", "s2", [], "pid"))
    # 空或纯空白响应不能当作"不融合"的结论，需标记 refined=False 让上层降级
    assert decision.refined is False
    assert decision.should_merge is False


def test_check_merge_malformed_reply_defaults_to_no_merge():
    refiner, _ = _refiner("这两条知识看起来有点像，但我不确定。")
    decision = asyncio.run(refiner.check_merge("a", "s", [], "b", "s2", [], "pid"))
    # 格式错误时提取不到 DECISION → 保守不融合，但 refined=True（响应非空）
    assert decision.should_merge is False
    assert decision.refined is True
    assert decision.merge_reason == ""


def test_check_merge_keywords_are_truncated_and_placeholder_when_empty():
    refiner, plugin = _refiner("DECISION: no\nREASON: r")
    asyncio.run(
        refiner.check_merge(
            "新主题",
            "s",
            [f"n{i}" for i in range(8)],
            "已有主题",
            "s2",
            [],
            "pid",
        )
    )
    prompt = plugin.llm_service.calls[0]["prompt"]
    # 关键词最多取 5 个并用顿号连接；空关键词列表显示"无"
    assert "关键词：n0、n1、n2、n3、n4" in prompt
    assert "n5" not in prompt
    assert "关键词：无" in prompt


def test_check_merge_truncates_long_summaries_in_prompt():
    refiner, plugin = _refiner("DECISION: no\nREASON: r")
    huge = "乙" * 900
    asyncio.run(refiner.check_merge("a", huge, [], "b", huge, [], "pid"))
    prompt = plugin.llm_service.calls[0]["prompt"]
    # 两侧内容都截断到 500 字，控制 prompt 体积
    assert "乙" * 500 in prompt
    assert "乙" * 501 not in prompt


def test_check_merge_propagates_exception():
    refiner, _ = _refiner(ConnectionError("网络中断"))
    with pytest.raises(ConnectionError, match="网络中断"):
        asyncio.run(refiner.check_merge("a", "s", [], "b", "s2", [], "pid"))
