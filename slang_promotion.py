"""黑话作用域晋升判定模块（v1.2.0）。

黑话写入永远只落触发群的 scope；global scope 没有任何自动写入路径，
唯一例外是"有证据的晋升"：同一词条在足够多的不同群里都被学到，
且至少一个群的 LLM 判定其为通用梗（scope_hint='general'），才允许晋升 global。
本模块只做纯函数判定与方案归并，不查库、不调 LLM、不做网络请求。
"""

from __future__ import annotations

from .constants import (
    _SLANG_PROMOTION_CONFIDENCE_BONUS,
    _SLANG_PROMOTION_CONFIDENCE_CAP,
    _SLANG_PROMOTION_MAX_KEYWORDS,
)


def normalize_phrase(phrase: str) -> str:
    """归一化词条用于跨群比较：小写 + 去除所有空白（"YYDS"/"yyds "/"y yds" 视为同词）。"""
    return "".join((phrase or "").split()).lower()


def check_cross_group_promotion(
    entries: list[dict],
    min_groups: int,
    require_general_hint: bool = True,
) -> dict | None:
    """判定同形词条是否满足跨群共现晋升条件，满足则返回晋升方案，否则 None。

    entries: get_slang_entries_by_topic 返回的跨 scope 条目列表，
             每个 dict 至少含 scope_type/scope_id/topic/content/confidence/keywords/scope_hint。
    min_groups: 要求的不同 group scope_id 数量下限。
    require_general_hint: True 时要求这些群条目里至少一个 scope_hint='general'
             （LLM 已判断是通用梗）；外部验证通道已另行取证时传 False 跳过该条件。

    只有 group scope 的条目算证据：global 条目是晋升结果而非证据，
    private 条目不参与跨群比较。
    """
    if not entries or min_groups < 1:
        return None

    group_entries = [
        e
        for e in entries
        if e.get("scope_type") == "group" and e.get("scope_id")
    ]
    group_ids = {e["scope_id"] for e in group_entries}
    if len(group_ids) < min_groups:
        return None
    if require_general_hint and not any(
        e.get("scope_hint") == "general" for e in group_entries
    ):
        return None

    # 释义/置信度取置信度最高的那条群条目
    best = max(group_entries, key=lambda e: float(e.get("confidence") or 0.0))

    # 关键词取各条目并集，保序去重，封顶 _SLANG_PROMOTION_MAX_KEYWORDS 个
    keywords: list[str] = []
    for e in group_entries:
        for kw in e.get("keywords") or []:
            kw = (kw or "").strip()
            if kw and kw not in keywords:
                keywords.append(kw)
    keywords = keywords[:_SLANG_PROMOTION_MAX_KEYWORDS]

    # 跨群共现比单群更可信，置信度加成但封顶，避免晋升词条反压群词条太多
    confidence = min(
        _SLANG_PROMOTION_CONFIDENCE_CAP,
        float(best.get("confidence") or 0.0) + _SLANG_PROMOTION_CONFIDENCE_BONUS,
    )

    return {
        "topic": best.get("topic") or "",
        "content": best.get("content") or "",
        "confidence": confidence,
        "from_groups": sorted(group_ids),
        "keywords": keywords,
    }
