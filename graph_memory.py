"""Lightweight associative graph helpers for reconstructive knowledge recall.

The graph is an index over existing knowledge, not an independent fact store.  Cues
and tags may guide retrieval, while answerable content always comes from ``memories``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence


GRAPH_RECONSTRUCTION_MODES = {"off", "fast", "smart"}

_MAX_CUE_CHARS = 80
_MAX_TAG_CHARS = 60
_COMPLEX_QUERY_PATTERNS = (
    r"(?:为什么|为何|原因|导致|影响|结果|所以|因果)",
    r"(?:什么时候|何时|之前|之后|先后|当时|最近|上次|时间线)",
    r"(?:版本|更新|变化|变更|以前|现在|后来)",
    r"(?:比较|相比|区别|差异|共同|关系|关联|分别|哪个|哪种)",
    r"(?:基于|结合|根据).{0,24}(?:判断|推断|说明|得出)",
)
_TAG_RULES = (
    ("时间与事件", r"(?:时间|日期|年|月|日|之前|之后|当时|后来|最近|版本|更新|变化)"),
    ("原因与影响", r"(?:因为|原因|导致|影响|结果|所以|因果|作用)"),
    ("比较与差异", r"(?:比较|相比|区别|差异|共同|优缺点|更适合)"),
    ("组成与依赖", r"(?:组成|包含|依赖|基于|架构|模块|流程|步骤|关系|关联)"),
    ("定义与属性", r"(?:是|属于|定义|属性|特点|功能|能力|用于|支持)"),
)
_CUE_SPLIT_RE = re.compile(r"[\s,，、;；:：/\\|·()（）\[\]【】<>《》]+")
_ROUTE_SPLIT_RE = re.compile(r"[\s,，、;；]+")


@dataclass(frozen=True)
class GraphAssociation:
    """One cue-tag edge distilled from an existing knowledge entry."""

    cue: str
    tag: str
    weight: float = 0.7
    source: str = "deterministic"


@dataclass(frozen=True)
class GraphCandidate:
    """A bounded graph traversal candidate returned by the storage layer."""

    memory_id: str
    cue: str
    tag: str
    hops: int
    score: float
    path: tuple[str, ...]


def normalize_graph_text(value: str, *, max_chars: int = _MAX_CUE_CHARS) -> str:
    """Normalize user-facing cue/tag text into a compact comparison key."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"[^0-9a-z_\u4e00-\u9fff]+", "", text)
    return text[:max_chars]


def clean_graph_label(value: str, *, max_chars: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def normalize_reconstruction_mode(value: object) -> str:
    mode = str(value or "fast").strip().lower()
    return mode if mode in GRAPH_RECONSTRUCTION_MODES else "fast"


def _relation_tag(topic: str, content: str) -> str:
    text = f"{topic} {content[:500]}"
    for label, pattern in _TAG_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return "主题与事实"


def _cue_values(topic: str, keywords: Sequence[str]) -> list[str]:
    values: list[str] = []
    for raw in [topic, *keywords]:
        label = clean_graph_label(raw, max_chars=_MAX_CUE_CHARS)
        if not label:
            continue
        values.append(label)
        if raw == topic:
            values.extend(
                part
                for part in _CUE_SPLIT_RE.split(label)
                if 2 <= len(normalize_graph_text(part)) <= 32
            )
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        norm = normalize_graph_text(value)
        if len(norm) < 2 or norm in seen:
            continue
        seen.add(norm)
        result.append(value)
        if len(result) >= 10:
            break
    return result


def build_graph_associations(
    topic: str,
    content: str,
    keywords: Sequence[str] | None = None,
    supplied: Iterable[object] | None = None,
) -> list[GraphAssociation]:
    """Build deterministic edges and merge strictly bounded LLM-supplied edges."""
    cues = _cue_values(topic, list(keywords or []))
    default_tag = _relation_tag(topic, content)
    associations = [
        GraphAssociation(cue=cue, tag=default_tag, weight=0.75) for cue in cues
    ]

    for item in supplied or []:
        cue = tag = ""
        if isinstance(item, GraphAssociation):
            cue, tag = item.cue, item.tag
        elif isinstance(item, dict):
            cue, tag = str(item.get("cue", "")), str(item.get("tag", ""))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            cue, tag = str(item[0]), str(item[1])
        cue = clean_graph_label(cue, max_chars=_MAX_CUE_CHARS)
        tag = clean_graph_label(tag, max_chars=_MAX_TAG_CHARS)
        if len(normalize_graph_text(cue)) < 2 or len(normalize_graph_text(tag)) < 2:
            continue
        associations.append(
            GraphAssociation(cue=cue, tag=tag, weight=1.0, source="llm_distilled")
        )

    deduped: dict[tuple[str, str], GraphAssociation] = {}
    for association in associations:
        key = (
            normalize_graph_text(association.cue),
            normalize_graph_text(association.tag, max_chars=_MAX_TAG_CHARS),
        )
        if not all(key):
            continue
        current = deduped.get(key)
        if current is None or association.weight > current.weight:
            deduped[key] = association
    return list(deduped.values())[:24]


def query_tag_hints(query: str) -> set[str]:
    """Return normalized relation tags implied by the query wording."""
    hints = {
        normalize_graph_text(label, max_chars=_MAX_TAG_CHARS)
        for label, pattern in _TAG_RULES
        if re.search(pattern, query, re.IGNORECASE)
    }
    return hints


def is_complex_query(query: str) -> bool:
    """Conservative gate for queries that may benefit from multi-hop navigation."""
    text = str(query or "").strip()
    if any(
        re.search(pattern, text, re.IGNORECASE) for pattern in _COMPLEX_QUERY_PATTERNS
    ):
        return True
    # Multiple explicit clauses are often compositional, but avoid treating ordinary
    # short punctuation as complex.
    clauses = [
        part for part in re.split(r"[，,；;。!?！？]", text) if len(part.strip()) >= 4
    ]
    return len(clauses) >= 3


def should_reconstruct(
    query: str,
    *,
    mode: str,
    passive_hit_count: int,
    required_hit_count: int,
    comparison_coverage: dict[str, bool] | None = None,
) -> bool:
    """Decide whether local graph reconstruction should supplement passive recall."""
    if normalize_reconstruction_mode(mode) == "off":
        return False
    coverage = comparison_coverage or {}
    if coverage and not all(coverage.values()):
        return True
    if passive_hit_count == 0:
        return True
    return passive_hit_count < max(1, required_hit_count) and is_complex_query(query)


def parse_route_selection(
    reply: str,
    allowed_ids: Sequence[str],
    *,
    max_count: int,
) -> list[str]:
    """Parse a smart-router reply without accepting invented memory identifiers."""
    allowed = set(allowed_ids)
    match = re.search(r"SELECT\s*:\s*(.+?)(?:\n|$)", str(reply or ""), re.IGNORECASE)
    if not match:
        return []
    selected: list[str] = []
    for token in _ROUTE_SPLIT_RE.split(match.group(1).strip()):
        candidate = token.strip().strip("[](){}'\"")
        if candidate in allowed and candidate not in selected:
            selected.append(candidate)
            if len(selected) >= max(1, max_count):
                break
    return selected
