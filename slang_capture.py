"""群黑话被动捕获模块。

提供候选词提取（纯 regex，不调 LLM）、批量学习 prompt 构建与响应解析。
设计目标：把 N 次单条 LLM 调用合并为 1 次批量调用，最小化 token 消耗。
"""

from __future__ import annotations

import re
from typing import Any

# 候选词字符集：1-15 个中英文字符/数字/下划线，不含标点空格
_PHRASE_CHARS = r"[\w\u4e00-\u9fa5]{1,15}"

# 提问型模式：捕获 X
_QUESTION_PATTERNS = [
    re.compile(rf"什么是({_PHRASE_CHARS})"),
    re.compile(rf"啥是({_PHRASE_CHARS})"),
    re.compile(rf"({_PHRASE_CHARS})是什么"),
    re.compile(rf"({_PHRASE_CHARS})是啥"),
    re.compile(rf"({_PHRASE_CHARS})啥意思"),
    re.compile(rf"({_PHRASE_CHARS})咋用"),
    re.compile(rf"懂({_PHRASE_CHARS})吗"),
    re.compile(rf"({_PHRASE_CHARS})怎么用"),
]

# 解释型模式：捕获 X（"X就是Y"、"X说白了Y"、"X指的是Y"）
_EXPLAIN_PATTERNS = [
    re.compile(rf"({_PHRASE_CHARS})就是"),
    re.compile(rf"({_PHRASE_CHARS})说白了"),
    re.compile(rf"({_PHRASE_CHARS})指的是"),
    re.compile(rf"({_PHRASE_CHARS})简称"),
]

_STOP_WORDS = {
    "这", "那", "我", "你", "他", "她", "它", "啥", "什么", "怎么",
    "为何", "这个", "那个", "为啥", "咱们", "你们", "他们", "她们",
    "啥意思", "咋", "咋用", "这么", "那么", "which", "what", "how",
    "why", "who", "where", "when",
}


def extract_candidates(text: str) -> list[tuple[str, str]]:
    """从消息文本提取候选黑话词。返回 [(phrase, context), ...]，去重。

    context 取 phrase 在原文中出现位置的前后 ±30 字片段，便于后续 LLM 学习时有上下文。
    """
    if not text or len(text) < 4:
        return []

    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    for pattern in _QUESTION_PATTERNS + _EXPLAIN_PATTERNS:
        for m in pattern.finditer(text):
            phrase = m.group(1).strip()
            if not phrase or phrase in _STOP_WORDS or len(phrase) < 2:
                continue
            if phrase in seen:
                continue
            seen.add(phrase)
            # 取 phrase 在原文中的位置，截取前后 ±30 字作为上下文
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            context = text[start:end].strip()
            results.append((phrase, context))

    return results


def build_batch_prompt(candidates: list[dict]) -> str:
    """构建批量学习 prompt。1 次 LLM 调用处理 K 个候选词。

    candidates: list[dict]，每个 dict 含 phrase / context / occurrences 字段。
    """
    lines = ["你是知识工程师。以下是群聊中频繁出现的黑话/术语，请逐个简短解释。", ""]
    lines.append("候选词：")
    for i, c in enumerate(candidates, 1):
        phrase = c.get("phrase", "")
        occ = c.get("occurrences", 1)
        ctx = (c.get("context") or "").strip()
        # 截断上下文避免 prompt 过长
        if len(ctx) > 60:
            ctx = ctx[:60] + "..."
        lines.append(f"{i}. 「{phrase}」(出现 {occ} 次, 上下文片段: \"{ctx}\")")
    lines.append("")
    lines.append("要求：")
    lines.append("1. 每个词输出 SUMMARY ≤150 字（定义/典型用法/使用场景）")
    lines.append("2. 每个词输出 KEYWORDS 3-5 个，便于检索")
    lines.append("3. 不确定的标 CONFIDENCE 30-50；非常确定标 70-90")
    lines.append("4. 严格按以下格式输出（每个候选词一组，分隔符 ===）：")
    lines.append("")
    lines.append("=== <候选词原样回写> ===")
    lines.append("SUMMARY: <解释>")
    lines.append("KEYWORDS: <关键词1>, <关键词2>, ...")
    lines.append("CONFIDENCE: <0-100>")
    lines.append("=== <下一个候选词> ===")
    lines.append("SUMMARY: ...")
    return "\n".join(lines)


# 匹配 "=== <phrase> ===" 头部
_SECTION_HEADER = re.compile(r"^===\s*(.+?)\s*===", re.MULTILINE)
# 字段提取
_SUMMARY_RE = re.compile(r"SUMMARY:\s*(.+?)(?=\n[A-Z]+:|\n===|\Z)", re.DOTALL)
_KEYWORDS_RE = re.compile(r"KEYWORDS:\s*(.+?)(?=\n[A-Z]+:|\n===|\Z)", re.DOTALL)
_CONFIDENCE_RE = re.compile(r"CONFIDENCE:\s*(\d+)")


def parse_batch_response(response: str, candidates: list[dict]) -> list[dict]:
    """解析 LLM 批量响应。返回 [{phrase, summary, keywords, confidence}, ...]。

    解析失败的候选词不返回（外层会标记 learned=1 但不入库，避免无限重试）。
    """
    if not response or not response.strip():
        return []

    # 用 === <phrase> === 切分响应为多个 section
    sections = _SECTION_HEADER.split(response)
    # split 后: [pre_text, phrase1, body1, phrase2, body2, ...]
    # 第一个元素是头部前缀（通常为空），之后成对 (phrase, body)
    phrase_to_body: dict[str, str] = {}
    for i in range(1, len(sections) - 1, 2):
        phrase = sections[i].strip()
        body = sections[i + 1]
        if phrase and body:
            phrase_to_body[phrase] = body

    if not phrase_to_body:
        return []

    # 候选词大小写不敏感匹配
    candidate_phrases = {c["phrase"]: c for c in candidates}
    phrase_lower_map = {p.lower(): p for p in candidate_phrases}

    results: list[dict] = []
    matched_phrases: set[str] = set()
    for resp_phrase, body in phrase_to_body.items():
        # 优先精确匹配，否则小写匹配
        original = candidate_phrases.get(resp_phrase)
        if original is None:
            lower = resp_phrase.lower()
            original_candidate = phrase_lower_map.get(lower)
            if original_candidate is None:
                continue
            original = original_candidate
        phrase = original["phrase"]
        if phrase in matched_phrases:
            continue
        matched_phrases.add(phrase)

        # 提取字段
        summary_m = _SUMMARY_RE.search(body)
        keywords_m = _KEYWORDS_RE.search(body)
        confidence_m = _CONFIDENCE_RE.search(body)

        if not summary_m:
            continue

        summary = summary_m.group(1).strip()
        if not summary:
            continue

        # 关键词
        keywords: list[str] = []
        if keywords_m:
            kw_str = keywords_m.group(1).strip()
            parts = re.split(r"[,，、\s]+", kw_str)
            keywords = [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2][:8]
        if not keywords:
            keywords = [phrase]

        # 置信度
        confidence = 0.5
        if confidence_m:
            try:
                score = int(confidence_m.group(1))
                confidence = max(0.0, min(1.0, score / 100.0))
            except (ValueError, TypeError):
                pass

        results.append({
            "phrase": phrase,
            "summary": summary,
            "keywords": keywords,
            "confidence": confidence,
        })

    return results
