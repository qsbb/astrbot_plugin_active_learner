"""models 数据模型单元测试。

覆盖：Scope 推导与不可变性、MemoryEntry 默认值/to_dict/from_row 容错、
MemoryVersion 与 SearchHit 代理属性、ID 生成的稳定性与隔离性。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.models import (
    SCOPE_GLOBAL,
    SCOPE_GROUP,
    SCOPE_PRIVATE,
    MemoryEntry,
    MemoryVersion,
    Scope,
    SearchHit,
    make_chunk_id,
    make_memory_id,
    now_ts,
)


# ---------- Scope ----------


def test_scope_str_and_is_global():
    assert str(Scope(SCOPE_PRIVATE, "u1")) == "private:u1", (
        "__str__ 用于日志与缓存键，格式需稳定"
    )
    assert Scope(SCOPE_GLOBAL, "global").is_global is True
    assert Scope(SCOPE_GROUP, "g1").is_global is False


def test_scope_is_frozen_and_hashable():
    scope = Scope(SCOPE_PRIVATE, "u1")
    # frozen dataclass：可作为 dict key / set 元素，且不会被下游意外改写
    assert {scope: 1}[Scope(SCOPE_PRIVATE, "u1")] == 1, "相同取值的 Scope 应视为同一个键"
    with pytest.raises(Exception):
        scope.id = "u2"


def test_scope_from_event_group_takes_priority():
    event = SimpleNamespace(
        message_obj=SimpleNamespace(group_id="g1"), get_sender_id=lambda: "u1"
    )
    # 群聊场景优先按群隔离，保证群内成员共享记忆
    assert Scope.from_event(event) == Scope(SCOPE_GROUP, "g1")


def test_scope_from_event_empty_group_id_falls_back_to_private():
    # AstrBot 私聊时 group_id 是空字符串而非 None，必须按私聊隔离
    event = SimpleNamespace(
        message_obj=SimpleNamespace(group_id=""), get_sender_id=lambda: 12345
    )
    assert Scope.from_event(event) == Scope(SCOPE_PRIVATE, "12345"), (
        "id 需转成 str，避免 int/str 混用导致 scope_id 不匹配"
    )


def test_scope_from_event_broken_event_degrades_to_unknown_user():
    # 取不到 group_id 和 sender_id 时不能抛异常，否则一条畸形消息会打断整个插件
    assert Scope.from_event(object()) == Scope(SCOPE_PRIVATE, "unknown")


# ---------- MemoryEntry ----------


def test_memory_entry_defaults():
    entry = MemoryEntry(
        id="i", scope_type=SCOPE_PRIVATE, scope_id="u", topic="t", content="c"
    )
    assert entry.confidence == 0.3, "新记忆默认低置信度，需经验证才提升"
    assert entry.verified is False
    assert entry.keywords == [] and entry.sources_detail == []
    assert entry.parent_doc_id is None, "非文档 chunk 的记忆没有父文档"
    assert entry.challenge_count == 0 and entry.access_count == 0


def test_memory_entry_mutable_defaults_are_not_shared():
    a = MemoryEntry(id="a", scope_type="p", scope_id="u", topic="t", content="c")
    b = MemoryEntry(id="b", scope_type="p", scope_id="u", topic="t", content="c")
    a.keywords.append("x")
    # field(default_factory=list) 保证每个实例独立，否则关键词会跨记忆串味
    assert b.keywords == [], "默认 list 不可在实例间共享"


def test_to_dict_contains_all_persisted_fields():
    entry = MemoryEntry(
        id="i", scope_type="p", scope_id="u", topic="t", content="c", origin="kb"
    )
    dumped = entry.to_dict()
    # to_dict 是导出/Dashboard 的序列化出口，字段缺失会导致导入端丢数据
    assert set(dumped) == {
        "id",
        "scope_type",
        "scope_id",
        "topic",
        "content",
        "keywords",
        "source",
        "sources_detail",
        "origin",
        "confidence",
        "verified",
        "challenge_count",
        "access_count",
        "created_at",
        "updated_at",
        "last_challenged_at",
        "parent_doc_id",
        "last_accessed_at",
    }
    assert dumped["origin"] == "kb"


def test_from_row_parses_keywords_and_sources_detail():
    entry = MemoryEntry.from_row(
        {
            "id": "i2",
            "scope_type": SCOPE_PRIVATE,
            "scope_id": "u",
            "topic": "t",
            "content": "c",
            "keywords": "alpha beta",
            "sources_detail": '["http://a"]',
            "confidence": "0.7",
            "verified": 1,
        }
    )
    # keywords 在库里是空格分隔字符串，sources_detail 是 JSON 文本
    assert entry.keywords == ["alpha", "beta"]
    assert entry.sources_detail == ["http://a"]
    assert entry.confidence == 0.7, "字符串置信度需转 float 才能参与打分"
    assert entry.verified is True


def test_from_row_tolerates_missing_and_corrupted_fields():
    entry = MemoryEntry.from_row({"id": "i3", "sources_detail": "{bad json"})
    # 老库缺列或 JSON 损坏时必须降级，不能让整次检索崩掉
    assert entry.sources_detail == [], "JSON 解析失败应退化为空列表"
    assert entry.keywords == []
    assert entry.confidence == 0.0 and entry.created_at == 0.0
    assert entry.topic is None, "缺失字段取 None（当前实现不填默认值）"


def test_from_row_verified_string_zero_is_truthy():
    """非空字符串 "0" 经 bool() 变 True，是 from_row 的疑似缺陷。

    仅在从 JSON/CSV 等文本源导入时触发（sqlite 存的是 INTEGER，不受影响）。
    此处固化现状，便于重构时对照。
    """
    assert MemoryEntry.from_row({"id": "x", "verified": "0"}).verified is True


def test_from_row_accepts_sqlite_row():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id TEXT, topic TEXT, keywords TEXT)")
    conn.execute("INSERT INTO t VALUES ('i', 'tp', 'k1 k2')")
    row = conn.execute("SELECT * FROM t").fetchone()
    # sqlite3.Row 缺列时抛 IndexError，from_row 内部已捕获
    entry = MemoryEntry.from_row(row)
    assert entry.id == "i" and entry.keywords == ["k1", "k2"]


# ---------- MemoryVersion / SearchHit ----------


def test_memory_version_to_dict_roundtrip():
    version = MemoryVersion(
        version_id=1,
        memory_id="m",
        version_no=2,
        content="old",
        confidence=0.4,
        source="s",
        reason="challenge",
        created_at=123.0,
    )
    assert version.to_dict() == {
        "version_id": 1,
        "memory_id": "m",
        "version_no": 2,
        "content": "old",
        "confidence": 0.4,
        "source": "s",
        "reason": "challenge",
        "created_at": 123.0,
    }


def test_search_hit_proxies_entry_fields():
    entry = MemoryEntry(
        id="i",
        scope_type="p",
        scope_id="u",
        topic="tp",
        content="ct",
        confidence=0.6,
        verified=True,
    )
    hit = SearchHit(entry=entry, score=1.23)
    # SearchHit 的只读属性透传 entry，模板渲染时可直接用 hit.topic
    assert (hit.topic, hit.content, hit.verified, hit.confidence) == (
        "tp",
        "ct",
        True,
        0.6,
    )
    assert hit.score == 1.23


# ---------- ID 生成 ----------


def test_make_memory_id_is_stable_and_case_insensitive():
    scope = Scope(SCOPE_PRIVATE, "u1")
    assert make_memory_id(scope, "Python") == make_memory_id(scope, " python "), (
        "topic 先 lower+strip，避免大小写/空格差异产生重复记忆"
    )
    assert len(make_memory_id(scope, "t")) == 16, "ID 为 sha1 截断 16 位"


def test_make_memory_id_isolates_scopes():
    # 不同 scope 下同名 topic 不能撞 ID，否则私聊记忆会覆盖群记忆
    assert make_memory_id(Scope(SCOPE_PRIVATE, "u1"), "t") != make_memory_id(
        Scope(SCOPE_GROUP, "u1"), "t"
    )
    assert make_memory_id(Scope(SCOPE_PRIVATE, "u1"), "t") != make_memory_id(
        Scope(SCOPE_PRIVATE, "u2"), "t"
    )


def test_make_chunk_id_is_unique_per_index_and_differs_from_memory_id():
    scope = Scope(SCOPE_PRIVATE, "u1")
    assert make_chunk_id(scope, "doc", 0) != make_chunk_id(scope, "doc", 1), (
        "同文档不同 chunk 必须是不同 ID，否则会互相覆盖折叠成一行"
    )
    assert make_chunk_id(scope, "doc", 0) != make_memory_id(scope, "doc"), (
        "chunk ID 与普通记忆 ID 命名空间隔离"
    )


def test_now_ts_returns_positive_epoch_seconds():
    assert now_ts() > 1_600_000_000, "应返回秒级 epoch 时间戳"
