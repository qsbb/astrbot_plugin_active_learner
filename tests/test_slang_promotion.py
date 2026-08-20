"""黑话作用域晋升（Phase 3）单元测试（v1.2.0）。

覆盖：
- slang_promotion.normalize_phrase 归一化；
- slang_promotion.check_cross_group_promotion 群数门槛、general hint 门槛、
  content/confidence/keywords 归并、外部验证旁路（require_general_hint=False）；
- storage.get_slang_entries_by_topic 跨群聚合与 scope_hint 解析兜底；
- storage.promote_slang_to_global 成功/词条不存在/global scope 输入三条路径；
- slang_recall.find_known_slang 群义优先去重。

storage 用例在 tmp_path 下建临时 sqlite 库，不触碰真实数据目录。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.models import (
    SCOPE_GLOBAL,
    SCOPE_GROUP,
    Scope,
)
from astrbot_plugin_active_learner.slang_promotion import (
    check_cross_group_promotion,
    normalize_phrase,
)
from astrbot_plugin_active_learner.slang_recall import find_known_slang
from astrbot_plugin_active_learner.storage import MemoryStore

GLOBAL = Scope(SCOPE_GLOBAL, SCOPE_GLOBAL)
GROUP_A = Scope(SCOPE_GROUP, "gA")
GROUP_B = Scope(SCOPE_GROUP, "gB")
GROUP_C = Scope(SCOPE_GROUP, "gC")


def _entry(
    scope_type="group",
    scope_id="g1",
    topic="yyds",
    content="永远的神",
    confidence=0.6,
    keywords=None,
    scope_hint="local",
):
    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "topic": topic,
        "content": content,
        "confidence": confidence,
        "keywords": keywords if keywords is not None else [topic],
        "scope_hint": scope_hint,
    }


@pytest.fixture
def store(tmp_path):
    st = MemoryStore(tmp_path / "mem.db", max_entries=500)
    yield st
    st.close()


def _add_slang(store, scope, topic, content="释义", confidence=0.6, hint="local",
               keywords=None):
    """按 Phase 2 的真实入库格式写一条黑话词条。"""
    return store.add_or_update(
        scope,
        topic,
        content,
        keywords=keywords or [topic],
        source="群黑话自动学习",
        sources_detail=[json.dumps({"slang_scope_hint": hint}, ensure_ascii=False)],
        confidence=confidence,
        origin="slang",
    )


# ---------- normalize_phrase ----------


def test_normalize_phrase_lowercase_and_strip_whitespace():
    assert normalize_phrase("YYDS") == "yyds"
    assert normalize_phrase(" yyds ") == "yyds"
    assert normalize_phrase("y y d\ts") == "yyds"
    assert normalize_phrase("绝绝子　") == "绝绝子"  # 全角空格也算空白


def test_normalize_phrase_empty_and_none():
    assert normalize_phrase("") == ""
    assert normalize_phrase(None) == ""


# ---------- check_cross_group_promotion ----------


def test_promotion_hit_with_enough_groups_and_general_hint():
    entries = [
        _entry(scope_id="gA", scope_hint="local", confidence=0.5),
        _entry(scope_id="gB", scope_hint="general", confidence=0.7,
               content="通用释义", keywords=["yyds", "缩写"]),
    ]
    plan = check_cross_group_promotion(entries, min_groups=2)
    assert plan is not None
    assert plan["topic"] == "yyds"
    # content 取置信度最高那条
    assert plan["content"] == "通用释义"
    # 置信度 = 最高 + 0.1
    assert plan["confidence"] == pytest.approx(0.8)
    assert plan["from_groups"] == ["gA", "gB"]
    # keywords 为并集去重
    assert plan["keywords"] == ["yyds", "缩写"]


def test_promotion_rejected_when_not_enough_groups():
    # 同一群里有多条同 topic 记录也只算一个群（正常不会，但 scope_id 去重兜底）
    entries = [
        _entry(scope_id="gA", scope_hint="general"),
        _entry(scope_id="gA", scope_hint="general", confidence=0.9),
    ]
    assert check_cross_group_promotion(entries, min_groups=2) is None


def test_promotion_rejected_when_all_local_hints():
    entries = [
        _entry(scope_id="gA", scope_hint="local"),
        _entry(scope_id="gB", scope_hint="local"),
        _entry(scope_id="gC", scope_hint="local"),
    ]
    assert check_cross_group_promotion(entries, min_groups=2) is None


def test_promotion_external_verify_bypasses_general_hint():
    # 外部验证通道：require_general_hint=False 时全是 local 也可晋升
    entries = [
        _entry(scope_id="gA", scope_hint="local"),
        _entry(scope_id="gB", scope_hint="local"),
    ]
    plan = check_cross_group_promotion(
        entries, min_groups=2, require_general_hint=False
    )
    assert plan is not None
    assert plan["from_groups"] == ["gA", "gB"]


def test_promotion_confidence_capped_at_nine_tenths():
    entries = [
        _entry(scope_id="gA", scope_hint="general", confidence=0.85),
        _entry(scope_id="gB", scope_hint="local", confidence=0.9),
    ]
    plan = check_cross_group_promotion(entries, min_groups=2)
    # 0.9 + 0.1 = 1.0，封顶 0.9
    assert plan["confidence"] == pytest.approx(0.9)


def test_promotion_keywords_capped_at_eight():
    entries = [
        _entry(scope_id="gA", scope_hint="general",
               keywords=[f"k{i}" for i in range(6)]),
        _entry(scope_id="gB", scope_hint="local",
               keywords=[f"k{i}" for i in range(4, 12)]),
    ]
    plan = check_cross_group_promotion(entries, min_groups=2)
    assert len(plan["keywords"]) == 8
    assert plan["keywords"][0] == "k0"


def test_promotion_ignores_global_and_private_entries():
    # global 条目是晋升结果不是证据；private 条目不参与跨群比较
    entries = [
        _entry(scope_type="global", scope_id="global", scope_hint="general"),
        _entry(scope_type="private", scope_id="u1", scope_hint="general"),
        _entry(scope_id="gA", scope_hint="general"),
    ]
    assert check_cross_group_promotion(entries, min_groups=2) is None


def test_promotion_empty_entries():
    assert check_cross_group_promotion([], min_groups=2) is None


# ---------- storage.get_slang_entries_by_topic ----------


def test_get_slang_entries_by_topic_aggregates_across_groups(store):
    _add_slang(store, GROUP_A, "yyds", content="A群释义", confidence=0.5, hint="local")
    _add_slang(store, GROUP_B, "yyds", content="B群释义", confidence=0.7, hint="general")
    entries = store.get_slang_entries_by_topic("yyds")
    assert len(entries) == 2
    by_scope = {e["scope_id"]: e for e in entries}
    assert by_scope["gA"]["scope_hint"] == "local"
    assert by_scope["gB"]["scope_hint"] == "general"
    assert by_scope["gB"]["content"] == "B群释义"
    assert by_scope["gB"]["keywords"] == ["yyds"]


def test_get_slang_entries_by_topic_case_insensitive(store):
    _add_slang(store, GROUP_A, "YYDS", hint="general")
    _add_slang(store, GROUP_B, "yyds", hint="local")
    entries = store.get_slang_entries_by_topic("YyDs")
    assert {e["scope_id"] for e in entries} == {"gA", "gB"}


def test_get_slang_entries_by_topic_excludes_non_slang(store):
    store.add_or_update(GROUP_A, "yyds", "普通记忆", origin="conversation")
    assert store.get_slang_entries_by_topic("yyds") == []


def test_get_slang_entries_by_topic_bad_sources_detail_falls_back_local(store):
    _add_slang(store, GROUP_A, "绝绝子", hint="general")
    # 人为破坏 sources_detail，解析失败应兜底 local
    entry_id = store.get_slang_entries_by_topic("绝绝子")[0]
    store._conn.execute(
        "UPDATE memories SET sources_detail = ? WHERE topic = '绝绝子'",
        ("这不是JSON",),
    )
    entries = store.get_slang_entries_by_topic("绝绝子")
    assert entries[0]["scope_hint"] == "local"
    assert entry_id["topic"] == "绝绝子"


def test_get_slang_entries_by_topic_missing_hint_falls_back_local(store):
    # sources_detail 结构合法但没有 slang_scope_hint 键
    store.add_or_update(
        GROUP_A, "破防了", "心态崩了",
        sources_detail=[json.dumps({"other_key": 1})],
        origin="slang",
    )
    entries = store.get_slang_entries_by_topic("破防了")
    assert entries[0]["scope_hint"] == "local"


# ---------- storage.promote_slang_to_global ----------


def test_promote_slang_to_global_success(store):
    _add_slang(store, GROUP_A, "yyds", content="永远的神", confidence=0.66,
               hint="general", keywords=["yyds", "缩写"])
    assert store.promote_slang_to_global(GROUP_A, "yyds") is True
    entries = store.get_slang_entries_by_topic("yyds")
    by_scope = {e["scope_type"]: e for e in entries}
    global_entry = by_scope["global"]
    assert global_entry["content"] == "永远的神"
    assert global_entry["confidence"] == pytest.approx(0.66)
    assert global_entry["keywords"] == ["yyds", "缩写"]
    # sources_detail 保留原 hint 并追加管理员晋升留痕
    detail = store.get_slang_entries_by_topic("yyds")
    global_row = [e for e in detail if e["scope_type"] == "global"][0]
    sources = json.loads(global_row["sources_detail"])
    parsed = [json.loads(s) for s in sources]
    assert any(d.get("slang_scope_hint") == "general" for d in parsed)
    admin_mark = [d for d in parsed if d.get("promoted_by_admin")]
    assert admin_mark and admin_mark[0]["from_scope"] == "group:gA"
    # 群条目不受影响
    assert by_scope["group"]["scope_id"] == "gA"


def test_promote_slang_to_global_updates_existing_global(store):
    _add_slang(store, GROUP_A, "yyds", content="旧释义", confidence=0.5)
    assert store.promote_slang_to_global(GROUP_A, "yyds") is True
    # 换个群用更高置信度再晋升一次：走更新路径，不产生重复行
    _add_slang(store, GROUP_B, "yyds", content="新释义", confidence=0.8)
    assert store.promote_slang_to_global(GROUP_B, "yyds") is True
    globals_ = [e for e in store.get_slang_entries_by_topic("yyds")
                if e["scope_type"] == "global"]
    assert len(globals_) == 1
    assert globals_[0]["content"] == "新释义"
    assert globals_[0]["confidence"] == pytest.approx(0.8)


def test_promote_slang_to_global_not_found(store):
    assert store.promote_slang_to_global(GROUP_A, "不存在的词") is False
    # 同 topic 但不是 slang origin 也不算
    store.add_or_update(GROUP_A, "普通词", "内容", origin="conversation")
    assert store.promote_slang_to_global(GROUP_A, "普通词") is False


def test_promote_slang_to_global_rejects_global_scope(store):
    _add_slang(store, GROUP_A, "yyds")
    assert store.promote_slang_to_global(GLOBAL, "yyds") is False


# ---------- slang_recall 群义优先去重 ----------


def test_find_known_slang_prefers_group_over_global():
    # global 条目排在前面（如置信度更高）时，去重仍保留群条目
    group_entry = {
        "topic": "yyds", "keywords": ["yyds"], "content": "本群说法",
        "confidence": 0.5, "scope_type": "group",
    }
    global_entry = {
        "topic": "yyds", "keywords": ["yyds"], "content": "通用释义",
        "confidence": 0.9, "scope_type": "global",
    }
    hits = find_known_slang("yyds 太强了", [global_entry, group_entry])
    assert len(hits) == 1
    assert hits[0]["scope_type"] == "group"
    assert hits[0]["content"] == "本群说法"


def test_find_known_slang_group_priority_keeps_position_sorting():
    # 群义优先只影响同 topic 去重，不影响不同词条按出现位置排序
    group_entry = {
        "topic": "破防了", "keywords": ["破防了"], "content": "本群说法",
        "confidence": 0.5, "scope_type": "group",
    }
    global_entry = {
        "topic": "yyds", "keywords": ["yyds"], "content": "通用释义",
        "confidence": 0.9, "scope_type": "global",
    }
    hits = find_known_slang("yyds！我破防了", [global_entry, group_entry])
    assert [h["topic"] for h in hits] == ["yyds", "破防了"]


def test_promote_slang_to_global_merges_history_sources(store):
    """v1.2.0：重复晋升时 global 条目合并历史 sources_detail，不丢留痕。"""
    _add_slang(store, GROUP_A, "yyds", content="旧释义", confidence=0.5,
               hint="general")
    assert store.promote_slang_to_global(GROUP_A, "yyds") is True
    _add_slang(store, GROUP_B, "yyds", content="新释义", confidence=0.8)
    assert store.promote_slang_to_global(GROUP_B, "yyds") is True
    global_row = [e for e in store.get_slang_entries_by_topic("yyds")
                  if e["scope_type"] == "global"][0]
    parsed = [json.loads(s) for s in json.loads(global_row["sources_detail"])]
    marks = [d for d in parsed if d.get("promoted_by_admin")]
    # 两次晋升的留痕都保留
    assert {m["from_scope"] for m in marks} == {"group:gA", "group:gB"}
    # 第一次的 general hint 也没有被覆盖
    assert any(d.get("slang_scope_hint") == "general" for d in parsed)
    # 内容/置信度仍走更新路径
    assert global_row["content"] == "新释义"
    assert global_row["confidence"] == pytest.approx(0.8)
