"""slang_capture 模块单元测试。

覆盖：extract_candidates（中文黑话、纯符号、超长文本、空输入、停用词与去重）、
build_batch_prompt（编号、上下文截断、缺字段兜底）、
parse_batch_response（合法分段、非法/无分隔符文本、缺字段、多余字段、
关键词切分与置信度钳制）。

注意：模块的批量响应协议是 "=== <phrase> ===" 分段 + SUMMARY/KEYWORDS/CONFIDENCE
字段，不是 JSON。断言按真实实现书写；发现的疑似缺陷用测试锁定当前行为，不改生产代码。
纯 regex 实现，无 IO、不联网、不调 LLM。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.slang_capture import (
    _STOP_WORDS,
    build_batch_prompt,
    extract_candidates,
    parse_batch_response,
)


def _phrases(text):
    return [p for p, _ in extract_candidates(text)]


# ---------- extract_candidates：空/过短/无效输入 ----------


@pytest.mark.parametrize(
    "text",
    [
        "",  # 空串
        None,  # None 被 not text 兜住
        "a",
        "abc",  # 长度 < 4 直接返回
        "什么是",  # 长度不足，未进入 regex
    ],
)
def test_extract_candidates_empty_input(text):
    # 短于 4 字或空输入直接返回空列表，避免无意义的 regex 扫描
    assert extract_candidates(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "！！！！！！！！",  # 纯中文标点
        "?!?!?!?!?!",  # 纯 ASCII 符号
        "。。。，，，；；；",
        "        ",  # 纯空格
        "+-*/=<>[]{}()",
        "这是一句没有任何提问或解释句式的普通闲聊",  # 不含任何模式
        "нет проблем совсем",  # 非中英文字符不在字符集内
    ],
)
def test_extract_candidates_no_match(text):
    # 无标点、无空格的短语字符集决定了纯符号文本不可能命中任何模式
    assert extract_candidates(text) == []


# ---------- extract_candidates：提问型与解释型模式 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("什么是绝绝子", ["绝绝子"]),
        ("啥是破防了", ["破防了"]),
        ("绝绝子是什么", ["绝绝子"]),
        ("破防了是啥", ["破防了"]),
        ("绝绝子啥意思", ["绝绝子"]),
        ("PPT咋用", ["PPT"]),
        ("懂绝绝子吗", ["绝绝子"]),
        ("这个梗怎么用", ["这个梗"]),
        ("破防了就是心态崩了", ["破防了"]),
        # "就是" 模式排在 "说白了" 之前，且短语贪婪匹配，故先命中更长的串
        ("绝绝子说白了就是夸人", ["绝绝子说白了", "绝绝子"]),
        ("nga指的是论坛", ["nga"]),
        ("yyds简称永远的神", ["yyds"]),
    ],
)
def test_extract_candidates_patterns(text, expected):
    # 8 个提问型 + 4 个解释型模式各自能命中，且短语为捕获组内容
    assert _phrases(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("什么是这个", []),  # "这个" 在停用词表
        ("什么是什么", []),  # "什么" 在停用词表
        ("什么是他们", []),
        ("what是什么", []),  # 英文停用词同样过滤
        ("什么是ab", ["ab"]),  # 恰好 2 字，未被长度过滤
        ("什么是我", []),  # 单字被 len < 2 过滤
    ],
)
def test_extract_candidates_filters(text, expected):
    # 停用词与单字短语被丢弃，避免把代词/疑问词当黑话入库
    assert _phrases(text) == expected


@pytest.mark.parametrize(
    "word", ["这个", "那个", "咱们", "你们", "他们", "她们", "什么", "怎么", "为何"]
)
def test_stop_words_are_actually_filtered(word):
    # 停用词表内的多字词即便命中模式也会被丢弃
    assert word in _STOP_WORDS
    assert _phrases(f"什么是{word}") == [], f"停用词 {word} 不应被提取"


def test_extract_candidates_dedup_across_patterns():
    # 同一短语被多个模式/多处命中时只保留首次结果
    text = "什么是绝绝子，绝绝子是什么，绝绝子啥意思"
    assert _phrases(text) == ["绝绝子"]


def test_extract_candidates_multiple_distinct_phrases():
    # 逗号充当短语边界，两个不同句式各自命中一个短语
    text = "什么是破防了，nga指的是论坛"
    assert set(_phrases(text)) == {"破防了", "nga"}


def test_extract_candidates_context_window():
    # context 为命中位置前后各 ±30 字，命中片段本身也在内
    text = "闲聊" * 40 + "什么是绝绝子" + "尾巴" * 40
    results = extract_candidates(text)
    assert len(results) == 1
    phrase, context = results[0]
    assert "绝绝子" in phrase
    assert phrase in context, "上下文应包含短语本身"
    assert len(context) <= 30 + len("什么是绝绝子") + 15 + 30
    assert context in text


def test_extract_candidates_context_clamped_at_boundaries():
    # 命中位于文本开头时 start 被 max(0,...) 钳制，不会越界
    _, context = extract_candidates("什么是绝绝子啊")[0]
    assert context.startswith("什么是")


def test_extract_candidates_very_long_text():
    # 超长文本（约 2 万字）仍只返回去重后的候选，context 不随文本长度膨胀
    text = ("这是一段很长的群聊记录。" * 800) + "什么是绝绝子。" + ("后续闲聊。" * 800)
    results = extract_candidates(text)
    assert len(results) == 1
    phrase, context = results[0]
    assert phrase.startswith("绝绝子")
    assert len(context) < 100, "上下文窗口固定，不应随原文长度增长"


def test_extract_candidates_phrase_length_cap():
    # 短语字符集上限 15，超长连续串只取前 15 字
    phrase = _phrases("什么是" + "甲" * 40)[0]
    assert len(phrase) == 15


def test_extract_candidates_stops_at_punctuation():
    # 标点不在短语字符集内，天然充当边界
    assert _phrases("什么是绝绝子，我不懂") == ["绝绝子"]


def test_extract_candidates_greedy_swallows_trailing_words():
    # 疑似缺陷：无标点时贪婪匹配会把后续词一并吞入短语（此处锁定现状）
    assert _phrases("什么是绝绝子啊我不懂") == ["绝绝子啊我不懂"]


def test_extract_candidates_space_breaks_match():
    # 空格不在字符集内，"什么是  绝绝子" 无法命中
    assert extract_candidates("什么是  绝绝子") == []


# ---------- build_batch_prompt ----------


def test_build_batch_prompt_structure():
    prompt = build_batch_prompt(
        [
            {"phrase": "绝绝子", "context": "什么是绝绝子", "occurrences": 3},
            {"phrase": "yyds", "context": "yyds是什么", "occurrences": 1},
        ]
    )
    # 候选按 1 起编号，并携带出现次数与上下文
    assert "1. 「绝绝子」(出现 3 次" in prompt
    assert "2. 「yyds」(出现 1 次" in prompt
    assert "什么是绝绝子" in prompt
    # 输出协议说明必须在 prompt 内，否则 LLM 无法产出可解析格式
    for token in ("SUMMARY:", "KEYWORDS:", "CONFIDENCE:", "==="):
        assert token in prompt


def test_build_batch_prompt_empty_candidates():
    # 空候选列表仍产出完整骨架（表头 + 要求），只是没有候选行
    prompt = build_batch_prompt([])
    assert "候选词：" in prompt
    assert "要求：" in prompt
    assert "1. 「" not in prompt


@pytest.mark.parametrize(
    ("candidate", "expected_fragment"),
    [
        ({"phrase": "词"}, "1. 「词」(出现 1 次"),  # occurrences 默认 1
        ({}, "1. 「」(出现 1 次"),  # phrase 缺失兜底为空串
        ({"phrase": "词", "context": None}, ' "")'),  # context 为 None 兜底空串
    ],
)
def test_build_batch_prompt_missing_fields(candidate, expected_fragment):
    assert expected_fragment in build_batch_prompt([candidate])


def test_build_batch_prompt_truncates_long_context():
    # 上下文超过 60 字被截断并补省略号，控制 prompt token 量
    prompt = build_batch_prompt([{"phrase": "词", "context": "长" * 200}])
    assert "长" * 60 + "..." in prompt
    assert "长" * 61 not in prompt


def test_build_batch_prompt_keeps_short_context_intact():
    prompt = build_batch_prompt([{"phrase": "词", "context": "短上下文"}])
    # 未超 60 字则原样保留（省略号只能出现在协议模板行，不在候选行）
    assert '"短上下文"' in prompt
    candidate_line = next(ln for ln in prompt.splitlines() if ln.startswith("1. "))
    assert "..." not in candidate_line


# ---------- parse_batch_response：无效输入 ----------


_GOOD_RESPONSE = """=== 绝绝子 ===
SUMMARY: 网络流行语，用于夸赞事物极好，也可反讽极差。
KEYWORDS: 绝绝子, 网络用语, 流行语
CONFIDENCE: 85
=== yyds ===
SUMMARY: 永远的神的拼音缩写，多用于电竞与追星场景。
KEYWORDS: yyds, 缩写, 电竞
CONFIDENCE: 70
"""

_CANDIDATES = [{"phrase": "绝绝子"}, {"phrase": "yyds"}]


@pytest.mark.parametrize(
    "response",
    [
        "",
        None,
        "   ",
        "\n\n\t ",
        "这是一段没有任何分隔符的普通回答，无法解析。",  # 无 === 头部
        "== 绝绝子 ==\nSUMMARY: 分隔符位数不对",  # 头部不是三个等号
        '{"phrase": "绝绝子", "summary": "模型返回了 JSON"}',  # 非约定格式
        "=== 绝绝子 ===",  # 有头部但 body 为空
    ],
)
def test_parse_batch_response_invalid(response):
    # 空/非法/无分段的响应统一返回空列表，外层据此跳过入库
    assert parse_batch_response(response, _CANDIDATES) == []


def test_parse_batch_response_empty_candidates():
    # 候选列表为空时无可匹配项
    assert parse_batch_response(_GOOD_RESPONSE, []) == []


def test_parse_batch_response_unknown_phrase_skipped():
    # 模型编造了不在候选内的词，必须丢弃
    resp = "=== 不在候选里 ===\nSUMMARY: 解释文本。\n"
    assert parse_batch_response(resp, _CANDIDATES) == []


# ---------- parse_batch_response：合法响应 ----------


def test_parse_batch_response_full():
    out = parse_batch_response(_GOOD_RESPONSE, _CANDIDATES)
    assert [r["phrase"] for r in out] == ["绝绝子", "yyds"]
    assert out[0]["summary"].startswith("网络流行语")
    assert out[0]["keywords"] == ["绝绝子", "网络用语", "流行语"]
    # CONFIDENCE 百分制被换算为 0-1 浮点
    assert out[0]["confidence"] == 0.85
    assert out[1]["confidence"] == 0.70
    assert set(out[0]) == {"phrase", "summary", "keywords", "confidence"}


def test_parse_batch_response_partial_match():
    # 响应只覆盖部分候选时，未覆盖的候选不出现在结果中
    out = parse_batch_response(_GOOD_RESPONSE, [{"phrase": "yyds"}])
    assert [r["phrase"] for r in out] == ["yyds"]


def test_parse_batch_response_dedups_repeated_section():
    # 同一候选出现两段时，dict 覆盖导致后一段生效，且只产出一条
    resp = "=== 绝绝子 ===\nSUMMARY: 第一段。\n=== 绝绝子 ===\nSUMMARY: 第二段。\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert len(out) == 1
    assert out[0]["summary"] == "第二段。"


def test_parse_batch_response_ignores_extra_fields():
    # 多余字段（EXTRA / NOTE）被忽略，不影响三个已知字段
    resp = (
        "=== 绝绝子 ===\n"
        "SUMMARY: 解释文本。\n"
        "KEYWORDS: 关键词一, 关键词二\n"
        "CONFIDENCE: 60\n"
        "EXTRA: 多余字段\n"
        "NOTE: 另一个多余字段\n"
    )
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert out == [
        {
            "phrase": "绝绝子",
            "summary": "解释文本。",
            "keywords": ["关键词一", "关键词二"],
            "confidence": 0.6,
        }
    ]


def test_parse_batch_response_tolerates_leading_preamble():
    # 头部之前的寒暄文本被 split 丢进前缀，不影响解析
    resp = "好的，以下是解释：\n\n=== 绝绝子 ===\nSUMMARY: 解释文本。\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert out[0]["summary"] == "解释文本。"


# ---------- parse_batch_response：缺字段 ----------


def test_parse_batch_response_missing_summary_skipped():
    # 没有 SUMMARY 的候选被跳过（无摘要则无入库价值）
    resp = "=== 绝绝子 ===\nKEYWORDS: 关键词一, 关键词二\nCONFIDENCE: 40\n"
    assert parse_batch_response(resp, [{"phrase": "绝绝子"}]) == []


def test_parse_batch_response_missing_keywords_falls_back_to_phrase():
    resp = "=== 绝绝子 ===\nSUMMARY: 解释文本。\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert out[0]["keywords"] == ["绝绝子"], "缺 KEYWORDS 时用短语本身兜底"
    assert out[0]["confidence"] == 0.5, "缺 CONFIDENCE 时默认 0.5"


def test_parse_batch_response_all_short_keywords_fall_back():
    # 关键词过滤掉长度 < 2 的碎片；全被过滤后回退为短语
    resp = "=== 绝绝子 ===\nSUMMARY: 解释文本。\nKEYWORDS: a, b, c\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert out[0]["keywords"] == ["绝绝子"]


def test_parse_batch_response_empty_summary_swallows_next_line():
    # 疑似缺陷：SUMMARY 后为空时，正则跨行吞掉下一行 KEYWORDS 作为摘要
    resp = "=== 绝绝子 ===\nSUMMARY:\nKEYWORDS: 关键词一, 关键词二\nCONFIDENCE: 40\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert out[0]["summary"] == "KEYWORDS: 关键词一, 关键词二"


def test_parse_batch_response_candidate_without_phrase_key_raises():
    # 疑似缺陷：候选缺 phrase 键时直接 KeyError，未做防御
    with pytest.raises(KeyError):
        parse_batch_response(_GOOD_RESPONSE, [{"context": "x"}])


def test_parse_batch_response_case_insensitive_fallback_raises():
    # 疑似缺陷：大小写回退分支把 str 当 dict 索引，抛 TypeError
    resp = "=== YYDS ===\nSUMMARY: 大写回写。\n"
    with pytest.raises(TypeError):
        parse_batch_response(resp, [{"phrase": "yyds"}])


# ---------- parse_batch_response：关键词与置信度细节 ----------


@pytest.mark.parametrize(
    ("kw_line", "expected"),
    [
        ("关键词一, 关键词二", ["关键词一", "关键词二"]),  # 英文逗号
        ("关键词一，关键词二", ["关键词一", "关键词二"]),  # 中文逗号
        ("关键词一、关键词二", ["关键词一", "关键词二"]),  # 顿号
        ("关键词一 关键词二", ["关键词一", "关键词二"]),  # 空格
        ("关键词一,，、 关键词二", ["关键词一", "关键词二"]),  # 混合分隔符
    ],
)
def test_parse_batch_response_keyword_separators(kw_line, expected):
    resp = f"=== 绝绝子 ===\nSUMMARY: 解释文本。\nKEYWORDS: {kw_line}\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert out[0]["keywords"] == expected


def test_parse_batch_response_keywords_capped_at_eight():
    kw = ", ".join(f"k{i:02d}" for i in range(12))
    resp = f"=== 绝绝子 ===\nSUMMARY: 解释文本。\nKEYWORDS: {kw}\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert len(out[0]["keywords"]) == 8, "关键词最多保留 8 个"
    assert out[0]["keywords"][0] == "k00"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0.0),
        ("50", 0.5),
        ("100", 1.0),
        ("200", 1.0),  # 超上限被钳到 1.0
        ("999", 1.0),
    ],
)
def test_parse_batch_response_confidence_clamped(raw, expected):
    resp = f"=== 绝绝子 ===\nSUMMARY: 解释文本。\nCONFIDENCE: {raw}\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert out[0]["confidence"] == expected


@pytest.mark.parametrize("raw", ["高", "abc", "-30", "八十"])
def test_parse_batch_response_non_numeric_confidence_defaults(raw):
    # 正则只吃数字，非数字（含负号）视为未提供 → 默认 0.5
    resp = f"=== 绝绝子 ===\nSUMMARY: 解释文本。\nCONFIDENCE: {raw}\n"
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert out[0]["confidence"] == 0.5


def test_parse_batch_response_multiline_summary():
    # SUMMARY 允许跨行，直到下一个大写字段或分段头
    resp = (
        "=== 绝绝子 ===\n"
        "SUMMARY: 第一行解释。\n继续第二行解释。\n"
        "KEYWORDS: 关键词一, 关键词二\n"
    )
    out = parse_batch_response(resp, [{"phrase": "绝绝子"}])
    assert "继续第二行解释。" in out[0]["summary"]


def test_extract_then_build_then_parse_roundtrip():
    # 端到端串联：提取 → 构建 prompt → 解析模型回写，短语需保持一致
    candidates = [
        {"phrase": p, "context": c, "occurrences": 1}
        for p, c in extract_candidates("什么是绝绝子，nga指的是论坛")
    ]
    assert {c["phrase"] for c in candidates} == {"绝绝子", "nga"}
    prompt = build_batch_prompt(candidates)
    assert "绝绝子" in prompt and "nga" in prompt
    resp = (
        "=== 绝绝子 ===\nSUMMARY: 夸赞用语。\nCONFIDENCE: 80\n"
        "=== nga ===\nSUMMARY: 游戏论坛。\nCONFIDENCE: 90\n"
    )
    out = parse_batch_response(resp, candidates)
    assert [r["phrase"] for r in out] == ["绝绝子", "nga"]
    assert [r["confidence"] for r in out] == [0.8, 0.9]
