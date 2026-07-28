"""storage.MemoryStore 单元测试。

覆盖：建表与 schema 迁移、增删改查、FTS5 检索、软删除留痕、版本历史、
去重合并、访问计数、容量淘汰、向量存取与混合检索、token 用量、黑话候选。

所有用例都在 pytest 的 tmp_path 下建临时 sqlite 库，不触碰真实数据目录。
storage.py 本身没有 async 方法，因此无需 asyncio 驱动。
"""

import sqlite3
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.models import (
    SCOPE_GLOBAL,
    SCOPE_GROUP,
    SCOPE_PRIVATE,
    MemoryEntry,
    Scope,
    make_chunk_id,
    make_memory_id,
)
from astrbot_plugin_active_learner.storage import (
    SCHEMA_SQL,
    SELECT_COLS,
    MemoryStore,
    _build_match_query,
    _column_exists,
    _migrate_schema,
)

PRIVATE = Scope(SCOPE_PRIVATE, "u1")
GLOBAL = Scope(SCOPE_GLOBAL, "global")
GROUP = Scope(SCOPE_GROUP, "g9")


class FakeEmbedder:
    """假 embedder：只暴露 storage 真正用到的三个接口。

    storage.search_by_vector / _load_scope_vectors 仅依赖 dim、available
    和 _matrix_cache 三者，因此无需联网或真实 provider。
    """

    def __init__(self, dim: int = 4, available: bool = True):
        self.dim = dim
        self._available = available
        self._matrix_cache: dict = {}

    @property
    def available(self) -> bool:
        return self._available


@pytest.fixture
def store(tmp_path):
    """默认 store：max_entries 足够大，避免用例被淘汰逻辑干扰。"""
    st = MemoryStore(tmp_path / "sub" / "mem.db", max_entries=500)
    yield st
    st.close()


def _make_store(tmp_path, name="mem.db", **kwargs):
    return MemoryStore(tmp_path / name, **kwargs)


# ---------- A. 模块级工具函数 ----------


def test_build_match_query_joins_tokens_with_or():
    # 多 token 需各自加引号做短语查询，再用 OR 连接，避免 FTS5 语法歧义
    assert _build_match_query("hello world") == '"hello" OR "world"'


def test_build_match_query_keeps_cjk_tokens():
    # 中文按空格切分后整段加引号；unicode61 分词器可正常匹配
    assert _build_match_query("你好 世界") == '"你好" OR "世界"'


@pytest.mark.parametrize("query", ["", "   ", '"""', "---***", "():^\\/"])
def test_build_match_query_returns_empty_for_no_usable_token(query):
    # 纯特殊字符会被清洗成空白，必须返回空串让调用方短路，否则 FTS5 会抛语法错
    assert _build_match_query(query) == ""


def test_build_match_query_truncates_to_100_chars():
    # 超长查询先截断到 100 字符再分词，防止极长输入拖慢全文检索
    q = "a" * 50 + " " + "b" * 200
    result = _build_match_query(q)
    assert '"' + "a" * 50 + '"' in result
    # 第二个 token 只剩 100-51=49 个 b，说明截断发生在分词之前
    assert '"' + "b" * 49 + '"' in result


def test_column_exists_true_and_false(store):
    conn = store._conn
    assert _column_exists(conn, "memories", "topic") is True
    assert _column_exists(conn, "memories", "no_such_column") is False


def test_column_exists_swallows_error_on_missing_table(store):
    # PRAGMA 对不存在的表会报错，函数需吞掉异常返回 False，保证迁移不中断
    assert _column_exists(store._conn, "no_such_table", "x") is False


def test_select_cols_all_present_after_init(store):
    """SELECT_COLS 里每一列都必须真实存在，否则所有查询都会炸。"""
    for col in (c.strip() for c in SELECT_COLS.split(",")):
        assert _column_exists(store._conn, "memories", col), f"缺列 {col}"


# ---------- B. 初始化与 schema 迁移 ----------


def test_init_creates_db_file_and_parent_dirs(tmp_path):
    # 父目录不存在时应自动 mkdir(parents=True)，否则首次装插件会失败
    path = tmp_path / "deep" / "nested" / "mem.db"
    st = MemoryStore(path)
    assert path.exists()
    st.close()


def test_init_applies_latest_schema_version(store):
    # 全新库应一次性迁移到最新版本 2（parent_doc_id/last_accessed_at + origin）
    assert store._schema_version == 2


def test_init_enables_wal_mode(store):
    # WAL 模式是并发写入与崩溃恢复的前提，需确认 PRAGMA 真的生效
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_migration_from_legacy_schema_adds_columns_and_backfills(tmp_path):
    """老库（只有 SCHEMA_SQL 建的表、没有 v1/v2 新列）应被迁移并回填。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO memories (id, scope_type, scope_id, topic, content,"
        " created_at, updated_at, confidence)"
        " VALUES ('oldid','private','u1','old topic','old content',12345.0,12345.0,0.4)"
    )
    conn.commit()
    # 前置确认：SCHEMA_SQL 本身不含 v1/v2 列，迁移才有意义
    assert _column_exists(conn, "memories", "parent_doc_id") is False
    assert _column_exists(conn, "memories", "origin") is False
    conn.close()

    st = MemoryStore(db)
    assert st._schema_version == 2
    entry = st.get_entry_by_id("oldid")
    # last_accessed_at 需从 created_at 回填，否则衰减评分会把老记忆算成"刚访问过"
    assert entry.last_accessed_at == 12345.0
    assert entry.origin == "", "origin 新列默认空串"
    assert entry.parent_doc_id is None
    st.close()


def test_migration_is_idempotent_on_reopen(tmp_path):
    # 重复打开同一库不应重复 ALTER（否则 duplicate column 报错）
    db = tmp_path / "reopen.db"
    st1 = MemoryStore(db)
    entry = st1.add_or_update(PRIVATE, "t", "c", confidence=0.5)
    st1.close()
    st2 = MemoryStore(db)
    assert st2._schema_version == 2
    assert st2.get_entry_by_id(entry.id) is not None, "迁移不应破坏已有数据"
    st2.close()


def test_migrate_schema_called_twice_returns_same_version():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    assert _migrate_schema(conn) == 2
    # 第二次调用走 current==2 的短路分支，不再执行任何 ALTER
    assert _migrate_schema(conn) == 2
    conn.close()


def test_migrate_schema_without_version_table_raises_after_alter():
    """schema_version 表缺失时：SELECT 的 except 分支被走到，但随后 INSERT 会抛错。

    这是真实行为（缺陷记录）：_migrate_schema 只兜住了读取失败，
    没兜住写入失败。生产路径上 __init__ 先 executescript 建表所以不会触发。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, created_at REAL)")
    with pytest.raises(sqlite3.OperationalError):
        _migrate_schema(conn)
    # ALTER 已在抛错前执行，证明 except 分支确实把 current 置成了 0
    assert _column_exists(conn, "memories", "parent_doc_id") is True
    conn.close()


def test_close_is_idempotent(tmp_path):
    # close 内部 try/except 吞异常，重复关闭不应抛错（插件重载时会重复调用）
    st = MemoryStore(tmp_path / "c.db")
    st.close()
    st.close()


def test_close_swallows_exception(tmp_path):
    class Boom:
        def close(self):
            raise RuntimeError("boom")

    st = MemoryStore(tmp_path / "b.db")
    real = st._conn
    st._conn = Boom()
    st.close()  # 不应抛出
    real.close()


# ---------- C. add_or_update：插入、去重合并 ----------


def test_add_or_update_inserts_with_all_fields(store):
    entry = store.add_or_update(
        PRIVATE,
        "Python",
        "Python 是一门语言",
        keywords=["py", "lang"],
        source="src1",
        sources_detail=["d1"],
        confidence=0.7,
        origin="conversation",
    )
    assert entry.topic == "Python"
    assert entry.content == "Python 是一门语言"
    assert entry.keywords == ["py", "lang"], "keywords 以空格拼接入库、按空格切回"
    assert entry.source == "src1"
    assert entry.sources_detail == ["d1"], "sources_detail 走 JSON 序列化往返"
    assert entry.confidence == 0.7
    assert entry.origin == "conversation"
    assert entry.verified is False
    assert entry.challenge_count == 0
    # INSERT 语句把 access_count 硬编码成 1（不是 0），新建即算一次访问
    assert entry.access_count == 1
    assert entry.created_at > 0 and entry.updated_at == entry.created_at
    assert entry.last_challenged_at == 0.0


def test_add_or_update_id_follows_make_memory_id(store):
    # ID 必须与 make_memory_id 一致，否则 search_by_topic 之类的按 ID 查找会失效
    entry = store.add_or_update(PRIVATE, "Redis", "缓存", confidence=0.5)
    assert entry.id == make_memory_id(PRIVATE, "Redis")


def test_add_or_update_defaults_for_optional_args(store):
    # keywords/sources_detail 默认 None，需被规范化为空列表而不是 None
    entry = store.add_or_update(PRIVATE, "Bare", "内容")
    assert entry.keywords == []
    assert entry.sources_detail == []
    assert entry.source == ""
    assert entry.origin == ""
    assert entry.confidence == 0.3, "默认置信度 0.3"


def test_add_or_update_same_topic_merges_instead_of_duplicating(store):
    first = store.add_or_update(
        PRIVATE,
        "Merge",
        "v1",
        keywords=["a", "b"],
        confidence=0.8,
        origin="conversation",
    )
    second = store.add_or_update(
        PRIVATE, "Merge", "v2", keywords=["b", "c"], confidence=0.3, origin="manual"
    )
    assert first.id == second.id
    assert store.count_all() == 1, "同 scope 同 topic 只应有一行"
    assert second.content == "v2", "内容以新值覆盖"
    # keywords 用 dict.fromkeys 去重且保序，b 不重复出现
    assert second.keywords == ["a", "b", "c"]
    # 置信度取 max，避免一次低置信更新把已验证的高置信记忆打回去
    assert second.confidence == 0.8
    # origin 已非空时保持原值（CASE WHEN），防止来源被后续写入覆盖
    assert second.origin == "conversation"


def test_add_or_update_merge_is_case_insensitive_on_topic(store):
    # make_memory_id 对 topic 做 lower().strip()，大小写/空白差异视为同一条
    a = store.add_or_update(PRIVATE, "Docker", "x", confidence=0.5)
    b = store.add_or_update(PRIVATE, "  docker  ", "y", confidence=0.5)
    assert a.id == b.id
    assert store.count_all() == 1


def test_add_or_update_merge_increments_access_count(store):
    first = store.add_or_update(PRIVATE, "Hit", "v1", confidence=0.5)
    second = store.add_or_update(PRIVATE, "Hit", "v2", confidence=0.5)
    # UPDATE 分支带 access_count + 1，合并本身被当作一次访问
    assert second.access_count == first.access_count + 1


def test_add_or_update_fills_origin_when_previously_empty(store):
    store.add_or_update(PRIVATE, "Fill", "v1", confidence=0.5)  # origin=''
    updated = store.add_or_update(PRIVATE, "Fill", "v2", confidence=0.5, origin="kb")
    # 原 origin 为空串时才允许被新值填充
    assert updated.origin == "kb"


def test_add_or_update_isolates_scopes(store):
    a = store.add_or_update(PRIVATE, "Same", "私聊内容", confidence=0.5)
    b = store.add_or_update(GROUP, "Same", "群聊内容", confidence=0.5)
    c = store.add_or_update(GLOBAL, "Same", "全局内容", confidence=0.5)
    # 同名 topic 在不同 scope 下必须是三条独立记忆（ID 含 scope 前缀哈希）
    assert len({a.id, b.id, c.id}) == 3
    assert store.count_all() == 3
    assert store.get_entry_by_id(a.id).content == "私聊内容"


# ---------- D. update_content 与版本快照 ----------


def test_update_content_returns_false_for_missing_id(store):
    # 找不到行时直接返回 False，不应插入新行
    assert store.update_content("no_such_id", "x", 0.9) is False
    assert store.count_all() == 0


def test_update_content_writes_version_snapshot_on_change(store):
    entry = store.add_or_update(
        PRIVATE, "Redis", "Redis 是缓存", confidence=0.5, source="s1"
    )
    assert (
        store.update_content(
            entry.id, "Redis 是内存数据库", 0.8, source="s2", reason="fix"
        )
        is True
    )

    versions = store.list_versions(entry.id)
    assert len(versions) == 1
    # 快照保存的是"改动前"的旧值，用于回溯
    assert versions[0].content == "Redis 是缓存"
    assert versions[0].confidence == 0.5
    assert versions[0].source == "s1"
    assert versions[0].reason == "fix"
    assert versions[0].version_no == 1

    now_entry = store.get_entry_by_id(entry.id)
    assert now_entry.content == "Redis 是内存数据库"
    assert now_entry.confidence == 0.8
    assert now_entry.source == "s2"


def test_update_content_default_reason_is_update(store):
    entry = store.add_or_update(PRIVATE, "T", "原内容", confidence=0.5)
    store.update_content(entry.id, "新内容完全不同", 0.6)
    # reason 传空串时回落到 "update"
    assert store.list_versions(entry.id)[0].reason == "update"


def test_update_content_snapshots_on_confidence_drop_even_if_content_same(store):
    entry = store.add_or_update(PRIVATE, "T", "内容不变", confidence=0.9)
    # 内容一模一样但置信度跌超过 0.15 → conf_drop 分支同样要留痕
    assert store.update_content(entry.id, "内容不变", 0.5, reason="drop") is True
    versions = store.list_versions(entry.id)
    assert len(versions) == 1
    assert versions[0].confidence == 0.9
    assert versions[0].reason == "drop"


def test_update_content_no_snapshot_when_nothing_meaningful_changed(store):
    entry = store.add_or_update(PRIVATE, "T", "内容不变", confidence=0.5)
    # 内容相同且置信度跌幅 0.1 未超过 0.15 阈值 → 不产生快照，避免版本表膨胀
    assert store.update_content(entry.id, "内容不变", 0.4) is True
    assert store.list_versions(entry.id) == []


def test_update_content_snapshot_false_skips_version(store):
    entry = store.add_or_update(PRIVATE, "T", "旧内容", confidence=0.5)
    # 显式 snapshot=False 时即使内容大改也不留痕
    assert (
        store.update_content(entry.id, "彻底不同的新内容", 0.9, snapshot=False) is True
    )
    assert store.list_versions(entry.id) == []
    assert store.get_entry_by_id(entry.id).content == "彻底不同的新内容"


def test_update_content_sets_verified_when_passed(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.update_content(entry.id, "已验证内容", 0.9, verified=True)
    assert store.get_entry_by_id(entry.id).verified is True
    store.update_content(entry.id, "又改回去了", 0.9, verified=False)
    assert store.get_entry_by_id(entry.id).verified is False


def test_update_content_keeps_verified_untouched_when_none(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.set_verified(entry.id, True)
    # verified=None 走不含 verified 的 UPDATE 分支，已验证状态不能被顺手清掉
    store.update_content(entry.id, "新内容", 0.9)
    assert store.get_entry_by_id(entry.id).verified is True


def test_update_content_keeps_old_keywords_when_not_given(store):
    entry = store.add_or_update(
        PRIVATE, "T", "内容", keywords=["k1", "k2"], confidence=0.5
    )
    store.update_content(entry.id, "新内容", 0.9)
    # keywords=None 时沿用旧值，否则更新内容会意外清空关键词
    assert store.get_entry_by_id(entry.id).keywords == ["k1", "k2"]
    store.update_content(entry.id, "更新内容", 0.9, keywords=["k3"])
    assert store.get_entry_by_id(entry.id).keywords == ["k3"], "显式传入则整体替换"


# ---------- E. 计数器与标记 ----------


def test_inc_challenge_bumps_count_and_timestamp(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.inc_challenge(entry.id)
    store.inc_challenge(entry.id)
    got = store.get_entry_by_id(entry.id)
    assert got.challenge_count == 2
    assert got.last_challenged_at > 0, "质疑需记录时间，供冷却判断"


def test_inc_access_bumps_only_access_count(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    before = store.get_entry_by_id(entry.id)
    store.inc_access(entry.id)
    after = store.get_entry_by_id(entry.id)
    assert after.access_count == before.access_count + 1
    # inc_access 不触碰 updated_at，避免"读"污染"写"时间线导致列表排序抖动
    assert after.updated_at == before.updated_at


def test_set_verified_with_and_without_confidence(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.set_verified(entry.id, True, confidence=0.95)
    got = store.get_entry_by_id(entry.id)
    assert got.verified is True
    assert got.confidence == 0.95

    store.set_verified(entry.id, False)
    got2 = store.get_entry_by_id(entry.id)
    assert got2.verified is False
    # confidence=None 时不改置信度，仅翻转 verified 标记
    assert got2.confidence == 0.95


def test_update_last_accessed_sets_given_ts(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.update_last_accessed(entry.id, 555.0)
    assert store.get_entry_by_id(entry.id).last_accessed_at == 555.0
    # 不传 ts 时用当前时间，应远大于刚才写入的 555
    store.update_last_accessed(entry.id)
    assert store.get_entry_by_id(entry.id).last_accessed_at > 555.0


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.inc_challenge("ghost"),
        lambda s: s.inc_access("ghost"),
        lambda s: s.set_verified("ghost", True),
        lambda s: s.set_verified("ghost", True, confidence=0.5),
        lambda s: s.update_last_accessed("ghost", 1.0),
    ],
)
def test_counter_updates_on_missing_id_are_silent_noops(store, call):
    # 这些方法都是裸 UPDATE，命中 0 行时静默通过；调用方（如异步任务）
    # 可能在记忆已被淘汰后才调用，不应抛错
    call(store)
    assert store.count_all() == 0


# ---------- F. forget：软删除留痕 ----------


def test_forget_saves_version_then_deletes_row(store):
    entry = store.add_or_update(
        PRIVATE, "Redis", "Redis 是缓存", confidence=0.6, source="s1"
    )
    ok, target = store.forget(PRIVATE, "Redis")
    assert ok is True
    assert target.id == entry.id
    # 行被物理删除，但版本表留痕，便于事后追溯"删了什么"
    assert store.get_entry_by_id(entry.id) is None
    versions = store.list_versions(entry.id)
    assert len(versions) == 1
    assert versions[0].reason == "manual_forget"
    assert versions[0].content == "Redis 是缓存"
    assert versions[0].confidence == 0.6


def test_forget_returns_false_when_nothing_matches(store):
    store.add_or_update(PRIVATE, "exact", "内容", confidence=0.5)
    ok, target = store.forget(PRIVATE, "完全不相关的词")
    assert (ok, target) == (False, None)
    assert store.count_all() == 1, "未命中时不得误删"


def test_forget_returns_false_when_query_has_no_usable_token(store):
    store.add_or_update(PRIVATE, "exact", "内容", confidence=0.5)
    # "***" 被清洗成空 match 表达式 → search 直接返回 []，forget 短路
    assert store.forget(PRIVATE, "***") == (False, None)
    assert store.count_all() == 1


def test_forget_does_not_touch_other_scope(store):
    store.add_or_update(PRIVATE, "Shared", "共同内容 alpha", confidence=0.5)
    group_entry = store.add_or_update(GROUP, "Shared", "共同内容 alpha", confidence=0.5)
    store.forget(PRIVATE, "Shared")
    # forget 底层 search 带 scope 过滤，群记忆不应被私聊的忘记操作波及
    assert store.get_entry_by_id(group_entry.id) is not None


def test_forget_does_not_delete_global_fallback_from_private_scope(store):
    global_entry = store.add_or_update(
        GLOBAL, "Shared", "共同内容 alpha", confidence=0.9
    )

    assert store.forget(PRIVATE, "Shared") == (False, None)
    assert store.get_entry_by_id(global_entry.id) is not None


def test_forget_uses_fuzzy_search_and_may_delete_a_different_topic(store):
    """缺陷记录：forget 走 FTS 模糊检索取 top1，而非按 topic 精确定位。

    这里 "alpha" 只是 "alpha beta" 的一部分，且另一条记忆正文里也含 alpha。
    高置信度那条被 FTS 打分排到第一而被删除，用户如果本意是删 gamma 就会误删。
    """
    store.add_or_update(PRIVATE, "alpha beta", "主题一正文", confidence=0.9)
    store.add_or_update(PRIVATE, "gamma", "正文里也出现 alpha 这个词", confidence=0.2)
    ok, target = store.forget(PRIVATE, "alpha")
    assert ok is True
    # 实际删掉的是打分最高的 "alpha beta"，而不是唯一精确匹配
    assert target.topic == "alpha beta"
    remaining = [e.topic for e in store.list_memories(PRIVATE)[0]]
    assert remaining == ["gamma"]


def test_list_versions_orders_by_version_no_and_returns_empty_for_unknown(store):
    entry = store.add_or_update(PRIVATE, "T", "v0", confidence=0.9)
    store.update_content(entry.id, "v1 内容改了", 0.9, reason="r1")
    store.update_content(entry.id, "v2 内容又改了", 0.9, reason="r2")
    store.forget(PRIVATE, "T")
    versions = store.list_versions(entry.id)
    assert [v.version_no for v in versions] == [1, 2, 3], "version_no 递增且升序返回"
    assert [v.reason for v in versions] == ["r1", "r2", "manual_forget"]
    assert store.list_versions("ghost") == []


# ---------- G. 容量淘汰 ----------


def test_evict_removes_lowest_scoring_entry_when_over_capacity(tmp_path):
    st = _make_store(tmp_path, "ev.db", max_entries=2)
    keep_hi = st.add_or_update(PRIVATE, "high", "内容", confidence=0.9)
    keep_mid = st.add_or_update(PRIVATE, "mid", "内容", confidence=0.6)
    # 第三条置信度最高，插入后被淘汰的应是分数最低的 mid
    st.add_or_update(PRIVATE, "top", "内容", confidence=0.99)
    topics = sorted(e.topic for e in st.list_memories(PRIVATE)[0])
    assert len(topics) == 2, "容量上限 2，超出即淘汰"
    assert topics == ["high", "top"]
    assert st.get_entry_by_id(keep_mid.id) is None
    assert st.get_entry_by_id(keep_hi.id) is not None
    st.close()


def test_evict_disabled_when_max_entries_is_zero(tmp_path):
    st = _make_store(tmp_path, "ev0.db", max_entries=0)
    for i in range(5):
        st.add_or_update(PRIVATE, f"T{i}", "内容", confidence=0.5)
    # max_entries <= 0 表示不限容量，直接短路返回 0
    assert st.count_all() == 5
    st.close()


def test_evict_only_counts_current_scope(tmp_path):
    st = _make_store(tmp_path, "evs.db", max_entries=2)
    for i in range(2):
        st.add_or_update(PRIVATE, f"P{i}", "内容", confidence=0.5)
    for i in range(2):
        st.add_or_update(GROUP, f"G{i}", "内容", confidence=0.5)
    # 淘汰按 scope 独立计数，群记忆不会因为私聊写满而被挤掉
    assert st.count_all() == 4
    assert st.list_memories(PRIVATE)[1] == 2
    assert st.list_memories(GROUP)[1] == 2
    st.close()


def test_add_or_update_returns_none_when_new_entry_is_evicted_immediately(tmp_path):
    """缺陷记录：add_or_update 声明返回 MemoryEntry，实际可能返回 None。

    淘汰在 INSERT 之后执行，若新记忆恰是当前 scope 里分数最低的一条，
    它会被自己触发的淘汰删掉，get_entry_by_id 随即查不到 → 返回 None。
    调用方若直接访问 .id 会 AttributeError。
    """
    st = _make_store(tmp_path, "evn.db", max_entries=1)
    st.add_or_update(PRIVATE, "high", "内容", confidence=0.9)
    result = st.add_or_update(PRIVATE, "low", "内容", confidence=0.05)
    assert result is None, "新插入的低分记忆被自身触发的淘汰删除"
    assert [e.topic for e in st.list_memories(PRIVATE)[0]] == ["high"]
    st.close()


# ---------- H. get_entry_by_id / search（FTS5） ----------


def test_get_entry_by_id_returns_none_for_unknown(store):
    assert store.get_entry_by_id("ghost") is None


def test_search_returns_empty_for_unusable_query(store):
    store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    # match 表达式为空时必须在查询前短路，避免把 "" 丢给 FTS5 触发 OperationalError
    assert store.search(PRIVATE, "***") == []
    assert store.search(PRIVATE, "") == []


def test_search_matches_topic_content_and_keywords(store):
    store.add_or_update(
        PRIVATE,
        "Python",
        "Python is a script language",
        keywords=["脚本"],
        confidence=0.5,
    )
    # FTS5 索引了 topic/content/keywords 三列，任一列命中都算
    assert [h.entry.topic for h in store.search(PRIVATE, "Python")] == ["Python"]
    assert [h.entry.topic for h in store.search(PRIVATE, "script")] == ["Python"]
    assert [h.entry.topic for h in store.search(PRIVATE, "脚本")] == ["Python"]


def test_search_is_case_insensitive_for_ascii(store):
    store.add_or_update(PRIVATE, "Python", "内容", confidence=0.5)
    # unicode61 会做大小写折叠，用户随手大小写也能召回
    for q in ("python", "PYTHON", "PyThOn"):
        assert len(store.search(PRIVATE, q)) == 1, f"{q} 应命中"


def test_search_matches_full_cjk_run(store):
    store.add_or_update(PRIVATE, "你好", "这是中文内容测试", confidence=0.6)
    # 整段连续汉字与索引 token 完全一致时可以命中
    assert len(store.search(PRIVATE, "这是中文内容测试")) == 1


def test_search_cjk_substring_query_misses(store):
    """缺陷记录：unicode61 把连续汉字整段当作 **一个** token，不做中文分词。

    因此正文 "这是中文内容测试" 只产生一个 token，查询 "中文" / "内容"
    这类子串都无法命中——中文场景下 FTS5 召回严重依赖 keywords 列或整句复现。
    """
    store.add_or_update(PRIVATE, "标题", "这是中文内容测试", confidence=0.6)
    assert store.search(PRIVATE, "中文") == [], "子串查询召回为空"
    assert store.search(PRIVATE, "内容") == []
    # 只有把词单独放进 keywords（空格分隔）才能被检索到
    store.add_or_update(
        PRIVATE, "标题2", "这是中文内容测试", keywords=["中文"], confidence=0.6
    )
    assert [h.entry.topic for h in store.search(PRIVATE, "中文")] == ["标题2"]


def test_search_includes_global_but_excludes_other_scope(store):
    store.add_or_update(PRIVATE, "mine", "共享词 alpha", confidence=0.5)
    store.add_or_update(GLOBAL, "globalone", "共享词 alpha", confidence=0.5)
    store.add_or_update(GROUP, "otherone", "共享词 alpha", confidence=0.5)
    topics = sorted(h.entry.topic for h in store.search(PRIVATE, "alpha", top_k=10))
    # SQL 的 scope 条件是"当前 scope 或 global"，其他 scope 硬过滤掉，
    # 保证群记忆不会泄漏进私聊回答
    assert topics == ["globalone", "mine"]


def test_search_scores_by_confidence_plus_access_bonus(store):
    entry = store.add_or_update(PRIVATE, "T", "命中词 alpha", confidence=0.5)
    hit = store.search(PRIVATE, "alpha")[0]
    # score = confidence + min(access,50)/50*0.1；新建 access_count=1 → +0.002
    assert hit.score == pytest.approx(0.5 + (1 / 50.0) * 0.1)
    assert hit.entry.id == entry.id
    assert hit.confidence == 0.5, "SearchHit 代理 entry.confidence"


def test_search_ranks_higher_confidence_first(store):
    store.add_or_update(PRIVATE, "low", "共享词 alpha", confidence=0.2)
    store.add_or_update(PRIVATE, "high", "共享词 alpha", confidence=0.9)
    assert [h.entry.topic for h in store.search(PRIVATE, "alpha", top_k=2)] == [
        "high",
        "low",
    ]


def test_search_respects_top_k(store):
    for i in range(5):
        store.add_or_update(PRIVATE, f"T{i}", "共享词 alpha", confidence=0.5)
    assert len(store.search(PRIVATE, "alpha", top_k=2)) == 2


def test_search_increments_access_count_of_returned_hits(store):
    entry = store.add_or_update(PRIVATE, "T", "命中词 alpha", confidence=0.5)
    before = store.get_entry_by_id(entry.id).access_count
    store.search(PRIVATE, "alpha")
    # 命中即累计访问，作为热度信号参与淘汰与衰减
    assert store.get_entry_by_id(entry.id).access_count == before + 1


def test_search_returns_empty_when_no_row_matches(store):
    store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    assert store.search(PRIVATE, "完全不存在的词汇") == []


def test_search_by_topic_exact_and_missing(store):
    store.add_or_update(PRIVATE, "Merge", "内容", confidence=0.5)
    # search_by_topic 走 make_memory_id，因此大小写无关但要求 topic 整体一致
    assert store.search_by_topic(PRIVATE, "MERGE").topic == "Merge"
    assert store.search_by_topic(PRIVATE, "Mer") is None, "不是模糊匹配"
    assert store.search_by_topic(GROUP, "Merge") is None, "scope 不同即查不到"


def test_search_by_topic_rejects_id_with_mismatched_stored_scope(store):
    forged_id = make_memory_id(PRIVATE, "Forged")
    store.add_chunk(forged_id, GROUP, "Forged", "其他群的内容")

    assert store.search_by_topic(PRIVATE, "Forged") is None


def test_search_fts_returns_negated_bm25(store):
    store.add_or_update(PRIVATE, "T", "命中词 alpha", confidence=0.5)
    results = store._search_fts(PRIVATE, "alpha", 10)
    assert len(results) == 1
    entry, score = results[0]
    assert isinstance(entry, MemoryEntry)
    # bm25 原始值为负，取负后应为正，方便统一按"越大越好"排序
    assert score > 0


def test_search_fts_hard_filters_unrelated_scopes(store):
    store.add_or_update(PRIVATE, "mine", "共享词 alpha", confidence=0.5)
    store.add_or_update(GLOBAL, "globalone", "共享词 alpha", confidence=0.5)
    store.add_or_update(GROUP, "otherone", "共享词 alpha", confidence=0.5)
    topics = sorted(e.topic for e, _ in store._search_fts(PRIVATE, "alpha", 10))
    assert topics == ["globalone", "mine"]


def test_search_fts_can_disable_global_fallback(store):
    store.add_or_update(PRIVATE, "mine", "共享词 alpha", confidence=0.5)
    store.add_or_update(GLOBAL, "globalone", "共享词 alpha", confidence=0.5)
    topics = [
        e.topic
        for e, _ in store._search_fts(
            PRIVATE, "alpha", 10, include_global=False
        )
    ]
    assert topics == ["mine"]


def test_search_fts_empty_query_short_circuits(store):
    assert store._search_fts(PRIVATE, "***", 10) == []


# ---------- I. 向量存取 ----------


def test_save_and_get_embedding_roundtrip(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.save_embedding(entry.id, [1.0, 0.0, 0.5, -1.0], 4, "fake-model")
    got = store.get_embedding_for_memory(entry.id)
    assert got is not None
    blob, dim, model = got
    assert dim == 4
    assert model == "fake-model"
    # float32 存 BLOB：4 维 × 4 字节 = 16 字节
    assert len(blob) == 16


def test_get_embedding_for_missing_memory_returns_none(store):
    assert store.get_embedding_for_memory("ghost") is None


def test_save_embedding_replaces_existing(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.save_embedding(entry.id, [1.0, 0.0], 2, "m1")
    store.save_embedding(entry.id, [0.0, 1.0, 0.0, 0.0], 4, "m2")
    _, dim, model = store.get_embedding_for_memory(entry.id)
    # memory_id 是主键 + INSERT OR REPLACE，换模型后旧向量必须被顶掉而非并存
    assert (dim, model) == (4, "m2")
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM memories_embedding WHERE memory_id = ?", (entry.id,)
    ).fetchone()[0]
    assert rows == 1


def test_save_embedding_falls_back_to_struct_without_numpy(store, monkeypatch):
    """numpy 缺失时应退化为 struct.pack，产出的字节与 numpy 版本一致。"""
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    # 把 numpy 置成 None 会让函数内的 import numpy 抛 ImportError，走 except 分支
    monkeypatch.setitem(sys.modules, "numpy", None)
    vec = [1.0, 0.0, 0.5, -1.0]
    store.save_embedding(entry.id, vec, 4, "no-numpy")
    blob, dim, model = store.get_embedding_for_memory(entry.id)
    assert blob == b"".join(struct.pack("<f", v) for v in vec)
    assert (dim, model) == (4, "no-numpy")


def test_load_scope_vectors_empty_db_returns_zero_matrix(store):
    emb = FakeEmbedder(dim=4)
    matrix, ids = store._load_scope_vectors(PRIVATE, emb)
    assert ids == []
    assert matrix.shape == (0, 4), "无向量时返回 (0, dim) 空矩阵而非 None"
    # 空结果也要写缓存，避免每次检索都重扫表
    assert "scope:private:u1:global:1" in emb._matrix_cache


def test_load_scope_vectors_uses_scope_specific_cache(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.save_embedding(entry.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    emb = FakeEmbedder(dim=4)
    matrix, ids = store._load_scope_vectors(PRIVATE, emb)
    assert ids == [entry.id]
    assert matrix.shape == (1, 4)
    # 第二次调用直接命中缓存，返回同一对象
    cached = store._load_scope_vectors(PRIVATE, emb)
    assert cached[1] is ids


def test_load_scope_vectors_excludes_unrelated_scopes(store):
    mine = store.add_or_update(PRIVATE, "mine", "内容", confidence=0.5)
    shared = store.add_or_update(GLOBAL, "shared", "内容", confidence=0.5)
    other = store.add_or_update(GROUP, "other", "内容", confidence=0.5)
    for entry in (mine, shared, other):
        store.save_embedding(entry.id, [1.0, 0.0, 0.0, 0.0], 4, "m")

    _, ids = store._load_scope_vectors(PRIVATE, FakeEmbedder(dim=4))

    assert set(ids) == {mine.id, shared.id}
    assert other.id not in ids


def test_load_scope_vectors_skips_dim_mismatch(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.save_embedding(entry.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    # embedder.dim=8 与库里 4 维不符 → 该向量被跳过，避免矩阵拼接时维度炸掉
    emb = FakeEmbedder(dim=8)
    matrix, ids = store._load_scope_vectors(PRIVATE, emb)
    assert ids == []
    assert matrix.shape == (0, 8)


def test_load_scope_vectors_skips_malformed_blob(store, tmp_path):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    # 直接写入长度非 4 字节对齐的脏 BLOB，np.frombuffer 会抛错 → 被 except 跳过
    store._conn.execute(
        "INSERT OR REPLACE INTO memories_embedding"
        " (memory_id, embedding, dim, model, created_at) VALUES (?, ?, ?, ?, ?)",
        (entry.id, b"\x01\x02\x03", 4, "broken", 0.0),
    )
    emb = FakeEmbedder(dim=4)
    matrix, ids = store._load_scope_vectors(PRIVATE, emb)
    assert ids == [], "脏数据不应让整次检索失败"
    assert matrix.shape == (0, 4)


def test_search_by_vector_ranks_by_cosine_similarity(store):
    a = store.add_or_update(PRIVATE, "cats", "猫是猫科动物", confidence=0.6)
    b = store.add_or_update(PRIVATE, "dogs", "狗是犬科动物", confidence=0.6)
    store.save_embedding(a.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    store.save_embedding(b.id, [0.0, 1.0, 0.0, 0.0], 4, "m")
    ranked = store.search_by_vector(
        PRIVATE, [1.0, 0.0, 0.0, 0.0], 5, FakeEmbedder(dim=4)
    )
    assert [mid for mid, _ in ranked] == [a.id, b.id]
    # 与自身完全同向 → 余弦相似度 1；正交 → 0
    assert ranked[0][1] == pytest.approx(1.0)
    assert ranked[1][1] == pytest.approx(0.0)


def test_search_by_vector_respects_top_k(store):
    for i in range(3):
        e = store.add_or_update(PRIVATE, f"T{i}", "内容", confidence=0.5)
        store.save_embedding(e.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    assert (
        len(store.search_by_vector(PRIVATE, [1.0, 0.0, 0.0, 0.0], 2, FakeEmbedder()))
        == 2
    )


def test_search_by_vector_returns_empty_without_vectors(store):
    store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    # 没有任何 embedding 行时应直接返回 []，让上层降级为纯 FTS
    assert (
        store.search_by_vector(PRIVATE, [1.0, 0.0, 0.0, 0.0], 5, FakeEmbedder()) == []
    )


def test_search_by_vector_matrix_cache_goes_stale_after_new_embedding(store):
    """缺陷记录：storage 只读 embedder._matrix_cache，从不主动失效它。

    新增向量后若复用同一个 embedder，检索仍看到旧矩阵，新记忆无法被向量召回，
    直到调用方自己 invalidate_matrix_cache。
    """
    a = store.add_or_update(PRIVATE, "one", "正文一", confidence=0.5)
    store.save_embedding(a.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    emb = FakeEmbedder(dim=4)
    assert len(store.search_by_vector(PRIVATE, [1.0, 0.0, 0.0, 0.0], 5, emb)) == 1

    b = store.add_or_update(PRIVATE, "two", "正文二", confidence=0.5)
    store.save_embedding(b.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    assert len(store.search_by_vector(PRIVATE, [1.0, 0.0, 0.0, 0.0], 5, emb)) == 1, (
        "缓存未失效，新向量查不到"
    )

    emb._matrix_cache.clear()  # 模拟调用方手动失效
    assert len(store.search_by_vector(PRIVATE, [1.0, 0.0, 0.0, 0.0], 5, emb)) == 2


# ---------- J. search_hybrid ----------


def test_search_hybrid_returns_empty_for_unusable_query(store):
    store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    # 无可用 token 且无向量 → all_ids 为空，直接返回 []
    assert store.search_hybrid(PRIVATE, "***") == []


def test_search_hybrid_returns_empty_when_nothing_matches(store):
    store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    assert store.search_hybrid(PRIVATE, "zzznomatchzzz") == []


def test_search_hybrid_works_without_embedder(store):
    entry = store.add_or_update(PRIVATE, "T", "命中词 alpha", confidence=0.5)
    hits = store.search_hybrid(PRIVATE, "alpha", top_k=3, track_access=False)
    assert len(hits) == 1
    assert hits[0].entry.id == entry.id
    assert hits[0].score > 0, "无 embedder 时应降级为纯 FTS 且仍给出正分"


def test_search_hybrid_pure_fts_still_gets_vector_half_weight(store):
    """缺陷记录：没有向量时 vec_norm 仍是 0.5，而不是 0。

    normalize_scores([0.0]) 对"全等值"返回 [0.5]，所以 vec_weight 那 0.6
    的份额被凭空计入。这里单条命中的最终分 = 0.5(hybrid) * 1.0(penalty) * confidence，
    若向量份额真为 0，则应是 0.4*0.5=0.2 而非 0.5。
    """
    store.add_or_update(PRIVATE, "T", "命中词 alpha", confidence=0.5)
    hit = store.search_hybrid(PRIVATE, "alpha", top_k=1, track_access=False)[0]
    # 新建记忆 last_accessed_at=0 → 回落 created_at ≈ now → decay ≈ 1
    assert hit.score == pytest.approx(0.5 * 0.5, rel=1e-3)


def test_search_hybrid_vector_boosts_matching_entry(store):
    a = store.add_or_update(PRIVATE, "cats", "cats are animals", confidence=0.6)
    b = store.add_or_update(PRIVATE, "dogs", "dogs are animals", confidence=0.6)
    store.save_embedding(a.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    store.save_embedding(b.id, [0.0, 1.0, 0.0, 0.0], 4, "m")
    hits = store.search_hybrid(
        PRIVATE,
        "animals",
        top_k=2,
        embedder=FakeEmbedder(dim=4),
        query_vec=[1.0, 0.0, 0.0, 0.0],
        track_access=False,
    )
    # 两条 FTS 分相同（归一化各 0.5），向量相似度 1 vs 0 决定排序
    assert [h.entry.topic for h in hits] == ["cats", "dogs"]
    # cats: (0.4*0.5 + 0.6*1.0) * 1.0 * 0.6 = 0.48
    assert hits[0].score == pytest.approx(0.48, rel=1e-3)
    # dogs: (0.4*0.5 + 0.6*0.0) * 1.0 * 0.6 = 0.12
    assert hits[1].score == pytest.approx(0.12, rel=1e-3)


def test_search_hybrid_skips_vector_when_embedder_unavailable(store):
    a = store.add_or_update(PRIVATE, "cats", "cats are animals", confidence=0.6)
    store.save_embedding(a.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    hits = store.search_hybrid(
        PRIVATE,
        "animals",
        top_k=2,
        embedder=FakeEmbedder(dim=4, available=False),
        query_vec=[1.0, 0.0, 0.0, 0.0],
        track_access=False,
    )
    # available=False → 不做向量检索，退回 0.5 的空向量份额
    assert hits[0].score == pytest.approx(0.5 * 0.6, rel=1e-3)


def test_search_hybrid_skips_vector_when_query_vec_missing(store):
    a = store.add_or_update(PRIVATE, "cats", "cats are animals", confidence=0.6)
    store.save_embedding(a.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    # query_vec=None：即使 embedder 可用也不查向量（向量必须在锁外算好再传入）
    hits = store.search_hybrid(
        PRIVATE, "animals", top_k=2, embedder=FakeEmbedder(dim=4), track_access=False
    )
    assert hits[0].score == pytest.approx(0.5 * 0.6, rel=1e-3)


def test_search_hybrid_hard_filters_unrelated_scopes(store):
    """current 与 global 可见，其他私聊/群聊不能仅靠降权混入结果。"""
    store.add_or_update(PRIVATE, "shared", "alpha beta gamma", confidence=0.5)
    store.add_or_update(GLOBAL, "shared", "alpha beta gamma", confidence=0.5)
    store.add_or_update(GROUP, "shared", "alpha beta gamma", confidence=0.5)
    hits = store.search_hybrid(PRIVATE, "alpha", top_k=3, track_access=False)
    assert len(hits) == 2
    assert [h.entry.scope_type for h in hits] == [
        SCOPE_PRIVATE,
        SCOPE_GLOBAL,
    ]
    assert hits[0].score == pytest.approx(0.5 * 1.0 * 0.5, rel=1e-3)
    assert hits[1].score == pytest.approx(0.5 * 0.9 * 0.5, rel=1e-3)


def test_search_hybrid_scope_fallback_only_controls_global(store):
    store.add_or_update(PRIVATE, "mine", "alpha", confidence=0.5)
    store.add_or_update(GLOBAL, "shared", "alpha", confidence=0.5)
    store.add_or_update(GROUP, "other", "alpha", confidence=0.5)

    hits = store.search_hybrid(
        PRIVATE,
        "alpha",
        top_k=3,
        enable_scope_fallback=False,
        track_access=False,
    )

    assert [h.entry.topic for h in hits] == ["mine"]


def test_search_hybrid_decay_lowers_stale_entries(store):
    fresh = store.add_or_update(PRIVATE, "freshone", "alpha beta gamma", confidence=0.5)
    stale = store.add_or_update(PRIVATE, "staleone", "alpha beta gamma", confidence=0.5)
    import time as _t

    # 把 stale 的 last_accessed_at 推到 60 天前；半衰期 30 天 → decay ≈ 0.25
    store.update_last_accessed(stale.id, _t.time() - 60 * 86400)
    store.update_last_accessed(fresh.id, _t.time())
    hits = store.search_hybrid(
        PRIVATE, "alpha", top_k=2, decay_half_life_days=30.0, track_access=False
    )
    assert [h.entry.id for h in hits] == [fresh.id, stale.id], "久未访问的记忆应被压后"
    assert hits[1].score == pytest.approx(hits[0].score * 0.25, rel=1e-2)


def test_search_hybrid_priority_topics_boost_score(store):
    store.add_or_update(PRIVATE, "cats", "alpha about animals", confidence=0.5)
    store.add_or_update(PRIVATE, "dogs", "alpha about animals", confidence=0.5)
    plain = store.search_hybrid(PRIVATE, "alpha", top_k=2, track_access=False)
    base = {h.entry.topic: h.score for h in plain}

    boosted = store.search_hybrid(
        PRIVATE,
        "alpha",
        top_k=2,
        priority_topics=["cats"],
        priority_boost=2.0,
        track_access=False,
    )
    got = {h.entry.topic: h.score for h in boosted}
    # 命中关心领域的 topic 按 boost 倍放大，未命中的保持原分
    assert got["cats"] == pytest.approx(base["cats"] * 2.0, rel=1e-3)
    assert got["dogs"] == pytest.approx(base["dogs"], rel=1e-3)
    assert boosted[0].entry.topic == "cats"


def test_search_hybrid_priority_matches_keywords_too(store):
    store.add_or_update(
        PRIVATE, "unrelated", "alpha content", keywords=["料理"], confidence=0.5
    )
    plain = store.search_hybrid(PRIVATE, "alpha", top_k=1, track_access=False)[0].score
    boosted = store.search_hybrid(
        PRIVATE,
        "alpha",
        top_k=1,
        priority_topics=["料理"],
        priority_boost=1.5,
        track_access=False,
    )[0].score
    # keywords 也参与优先话题匹配，不只看 topic
    assert boosted == pytest.approx(plain * 1.5, rel=1e-3)


def test_search_hybrid_priority_boost_one_is_noop(store):
    store.add_or_update(PRIVATE, "cats", "alpha content", confidence=0.5)
    plain = store.search_hybrid(PRIVATE, "alpha", top_k=1, track_access=False)[0].score
    same = store.search_hybrid(
        PRIVATE,
        "alpha",
        top_k=1,
        priority_topics=["cats"],
        priority_boost=1.0,
        track_access=False,
    )[0].score
    # boost<=1.0 等于关闭该特性（文档明确 1.0 表示关闭）
    assert same == pytest.approx(plain, rel=1e-9)


def test_search_hybrid_respects_top_k(store):
    for i in range(5):
        store.add_or_update(PRIVATE, f"T{i}", "alpha content", confidence=0.5)
    assert len(store.search_hybrid(PRIVATE, "alpha", top_k=2, track_access=False)) == 2


def test_search_hybrid_custom_weights_change_ranking(store):
    a = store.add_or_update(PRIVATE, "vec", "alpha content one", confidence=0.5)
    store.add_or_update(PRIVATE, "novec", "alpha content two", confidence=0.5)
    store.save_embedding(a.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    # vec_weight=0 时向量分完全不参与，两条只剩相同的 FTS 分
    hits = store.search_hybrid(
        PRIVATE,
        "alpha",
        top_k=2,
        embedder=FakeEmbedder(dim=4),
        query_vec=[1.0, 0.0, 0.0, 0.0],
        fts_weight=1.0,
        vec_weight=0.0,
        track_access=False,
    )
    assert hits[0].score == pytest.approx(hits[1].score, rel=1e-6)


def test_search_hybrid_track_access_false_leaves_counters(store):
    entry = store.add_or_update(PRIVATE, "T", "alpha content", confidence=0.5)
    before = store.get_entry_by_id(entry.id)
    store.search_hybrid(PRIVATE, "alpha", top_k=1, track_access=False)
    after = store.get_entry_by_id(entry.id)
    # 分阶段检索（向量回退前的 FTS 预检）靠这个开关避免重复计数
    assert after.access_count == before.access_count
    assert after.last_accessed_at == before.last_accessed_at


def test_search_hybrid_track_access_true_updates_counters(store):
    entry = store.add_or_update(PRIVATE, "T", "alpha content", confidence=0.5)
    before = store.get_entry_by_id(entry.id)
    store.search_hybrid(PRIVATE, "alpha", top_k=1, track_access=True)
    after = store.get_entry_by_id(entry.id)
    assert after.access_count == before.access_count + 1
    assert after.last_accessed_at > before.last_accessed_at, (
        "命中需刷新访问时间供衰减用"
    )


def test_search_hybrid_finds_entry_only_reachable_by_vector(store):
    # 正文完全不含查询词，只能靠向量召回，验证 FTS ∪ 向量的并集逻辑
    a = store.add_or_update(PRIVATE, "semantic", "完全无关的正文", confidence=0.5)
    store.add_or_update(PRIVATE, "ftsonly", "alpha content", confidence=0.5)
    store.save_embedding(a.id, [1.0, 0.0, 0.0, 0.0], 4, "m")
    hits = store.search_hybrid(
        PRIVATE,
        "alpha",
        top_k=5,
        embedder=FakeEmbedder(dim=4),
        query_vec=[1.0, 0.0, 0.0, 0.0],
        track_access=False,
    )
    topics = {h.entry.topic for h in hits}
    assert topics == {"semantic", "ftsonly"}


def test_track_search_hits_bumps_access_and_timestamp(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    hits = [type("H", (), {"entry": entry})()]
    before = store.get_entry_by_id(entry.id).access_count
    store.track_search_hits(hits, ts=999.0)
    got = store.get_entry_by_id(entry.id)
    assert got.access_count == before + 1
    assert got.last_accessed_at == 999.0


def test_track_search_hits_defaults_ts_to_now(store):
    entry = store.add_or_update(PRIVATE, "T", "内容", confidence=0.5)
    store.track_search_hits([type("H", (), {"entry": entry})()])
    # ts=None 时取当前时间
    assert store.get_entry_by_id(entry.id).last_accessed_at > 0


def test_track_search_hits_with_empty_list_is_noop(store):
    store.track_search_hits([])  # 不应抛错


# ---------- K. get_entries_by_ids / add_chunk ----------


def test_get_entries_by_ids_returns_matching_only(store):
    a = store.add_or_update(PRIVATE, "A", "内容", confidence=0.5)
    b = store.add_or_update(PRIVATE, "B", "内容", confidence=0.5)
    got = store.get_entries_by_ids([a.id, b.id, "ghost"])
    # 不存在的 ID 被静默忽略，不会抛错也不会补空位
    assert sorted(e.topic for e in got) == ["A", "B"]


def test_get_entries_by_ids_empty_input_short_circuits(store):
    # 空列表必须提前返回，否则 IN () 会是非法 SQL
    assert store.get_entries_by_ids([]) == []


def test_id_reads_require_matching_scope_when_scope_is_supplied(store):
    mine = store.add_or_update(PRIVATE, "mine", "内容", confidence=0.5)
    shared = store.add_or_update(GLOBAL, "shared", "内容", confidence=0.5)
    other = store.add_or_update(GROUP, "other", "内容", confidence=0.5)

    assert store.get_entry_by_id(mine.id, PRIVATE).id == mine.id
    assert store.get_entry_by_id(other.id, PRIVATE) is None
    assert store.get_entry_by_id(shared.id, PRIVATE) is None
    assert (
        store.get_entry_by_id(shared.id, PRIVATE, include_global=True).id
        == shared.id
    )

    visible = store.get_entries_by_ids(
        [mine.id, shared.id, other.id], PRIVATE, include_global=True
    )
    assert {entry.id for entry in visible} == {mine.id, shared.id}


def test_id_mutations_and_version_reads_reject_other_scope(store):
    other = store.add_or_update(GROUP, "other", "原内容", confidence=0.5)
    store.update_content(other.id, "生成一条历史版本", 0.6)
    before = store.get_entry_by_id(other.id)

    assert (
        store.update_content(
            other.id, "越权内容", 0.9, verified=True, scope=PRIVATE
        )
        is False
    )
    store.inc_challenge(other.id, PRIVATE)
    store.inc_access(other.id, PRIVATE)
    store.set_verified(other.id, True, confidence=1.0, scope=PRIVATE)
    store.update_last_accessed(other.id, 999.0, scope=PRIVATE)

    after = store.get_entry_by_id(other.id)
    assert after.content == before.content
    assert after.confidence == before.confidence
    assert after.verified == before.verified
    assert after.challenge_count == before.challenge_count
    assert after.access_count == before.access_count
    assert after.last_accessed_at == before.last_accessed_at
    assert store.list_versions(other.id, PRIVATE) == []
    assert len(store.list_versions(other.id, GROUP)) == 1


def test_add_chunk_inserts_with_parent_doc_id(store):
    chunk_id = make_chunk_id(PRIVATE, "doc1", 0)
    entry = store.add_chunk(
        chunk_id,
        PRIVATE,
        "文档分块",
        "分块正文",
        keywords=["k1"],
        source="src",
        confidence=0.7,
        parent_doc_id="doc1",
        now=1000.0,
        origin="kb",
    )
    assert entry.id == chunk_id, "chunk 用调用方给的 ID，不走 topic 哈希"
    assert entry.parent_doc_id == "doc1"
    assert entry.confidence == 0.7
    assert entry.origin == "kb"
    assert entry.keywords == ["k1"]
    assert entry.source == "src"
    assert entry.sources_detail == []
    # add_chunk 的 access_count 硬编码为 0（与 add_or_update 的 1 不同）
    assert entry.access_count == 0
    # now 显式传入时三个时间戳都用它，保证同一文档各 chunk 时间一致
    assert entry.created_at == 1000.0
    assert entry.updated_at == 1000.0
    assert entry.last_accessed_at == 1000.0


def test_add_chunk_defaults_now_to_current_time(store):
    chunk_id = make_chunk_id(PRIVATE, "doc1", 1)
    # now=0.0 是"未提供"的哨兵值，需回落到 now_ts()
    entry = store.add_chunk(chunk_id, PRIVATE, "t", "c", parent_doc_id="doc1")
    assert entry.created_at > 0
    assert entry.confidence == 0.5, "chunk 默认置信度 0.5"
    assert entry.keywords == []
    assert entry.origin == ""


def test_add_chunk_replaces_on_same_id(store):
    chunk_id = make_chunk_id(PRIVATE, "doc1", 0)
    store.add_chunk(chunk_id, PRIVATE, "t", "旧正文", parent_doc_id="doc1")
    entry = store.add_chunk(chunk_id, PRIVATE, "t", "新正文", parent_doc_id="doc1")
    # INSERT OR REPLACE：重复导入同一文档不会产生重复 chunk
    assert entry.content == "新正文"
    assert store.count_all() == 1


def test_add_chunk_siblings_do_not_collapse(store):
    # 同一文档不同分块共享 topic，但 chunk_id 不同 → 必须各自独立成行
    ids = [make_chunk_id(PRIVATE, "doc1", i) for i in range(3)]
    for i, cid in enumerate(ids):
        store.add_chunk(
            cid, PRIVATE, "同一标题", f"第 {i} 段正文", parent_doc_id="doc1"
        )
    assert store.count_all() == 3
    assert len(set(ids)) == 3


def test_add_chunk_is_searchable_via_fts(store):
    chunk_id = make_chunk_id(PRIVATE, "doc1", 0)
    store.add_chunk(chunk_id, PRIVATE, "标题", "chunk alpha body", parent_doc_id="doc1")
    # chunk 走 INSERT 触发器同样写入 FTS 索引
    assert [h.entry.id for h in store.search(PRIVATE, "alpha")] == [chunk_id]


# ---------- L. list_memories 分页 ----------


def test_list_memories_paginates_and_reports_totals(store):
    for i in range(7):
        store.add_or_update(PRIVATE, f"topic{i}", f"正文 {i}", confidence=0.5)
    page1, total, total_pages = store.list_memories(PRIVATE, page=1, per_page=3)
    assert total == 7
    assert total_pages == 3, "7 条按每页 3 条切成 3 页（向上取整）"
    assert len(page1) == 3
    page2 = store.list_memories(PRIVATE, page=2, per_page=3)[0]
    page3 = store.list_memories(PRIVATE, page=3, per_page=3)[0]
    assert len(page3) == 1, "末页只剩 1 条"
    # 三页并集应恰好覆盖全部且无重复（不断言页内顺序：同批写入 updated_at 可能相同）
    all_ids = [e.id for e in page1 + page2 + page3]
    assert len(set(all_ids)) == 7


def test_list_memories_clamps_page_out_of_range(store):
    for i in range(5):
        store.add_or_update(PRIVATE, f"topic{i}", "正文", confidence=0.5)
    # page 超上界被夹到末页，page<1 被夹到第 1 页，避免负 OFFSET
    assert len(store.list_memories(PRIVATE, page=99, per_page=2)[0]) == 1
    assert len(store.list_memories(PRIVATE, page=0, per_page=2)[0]) == 2
    assert len(store.list_memories(PRIVATE, page=-5, per_page=2)[0]) == 2


def test_list_memories_empty_scope_returns_one_page(store):
    entries, total, total_pages = store.list_memories(Scope(SCOPE_PRIVATE, "nobody"))
    # 空结果的 total_pages 用 max(1, ...) 兜底为 1，方便前端渲染"第 1/1 页"
    assert (entries, total, total_pages) == ([], 0, 1)


def test_list_memories_orders_by_updated_at_desc(store):
    # 用 add_chunk 显式指定 now，才能构造确定的 updated_at 顺序
    for i, ts in enumerate((100.0, 300.0, 200.0)):
        store.add_chunk(f"id{i}", PRIVATE, f"t{i}", "正文", now=ts)
    topics = [e.topic for e in store.list_memories(PRIVATE, per_page=10)[0]]
    assert topics == ["t1", "t2", "t0"], "按 updated_at 降序，最近更新的在前"


def test_list_memories_is_scope_isolated(store):
    store.add_or_update(PRIVATE, "p", "正文", confidence=0.5)
    store.add_or_update(GROUP, "g", "正文", confidence=0.5)
    assert [e.topic for e in store.list_memories(PRIVATE)[0]] == ["p"]
    assert [e.topic for e in store.list_memories(GROUP)[0]] == ["g"]


# ---------- M. stats / export ----------


def test_stats_aggregates_current_scope(store):
    a = store.add_or_update(PRIVATE, "A", "正文", confidence=0.4)
    b = store.add_or_update(PRIVATE, "B", "正文", confidence=0.6)
    store.set_verified(a.id, True)
    store.inc_challenge(b.id)
    store.inc_access(b.id)
    store.add_or_update(GROUP, "OTHER", "正文", confidence=0.9)  # 不应计入

    st = store.stats(PRIVATE)
    assert st["total"] == 2
    assert st["verified"] == 1
    assert st["challenged"] == 1, "challenged 统计的是被质疑过的条数，而非质疑总次数"
    assert st["avg_confidence"] == pytest.approx(0.5)
    assert st["most_accessed"] == "B"
    assert st["scope_type"] == SCOPE_PRIVATE
    assert st["scope_id"] == "u1"


def test_stats_empty_scope_returns_zero_shaped_dict(store):
    empty = Scope(SCOPE_PRIVATE, "nobody")
    st = store.stats(empty)
    # 空 scope 走专门的早返回分支，字段齐全但全零，防止前端取键报错
    assert st == {
        "total": 0,
        "verified": 0,
        "challenged": 0,
        "avg_confidence": 0.0,
        "most_accessed": None,
        "scope_type": SCOPE_PRIVATE,
        "scope_id": "nobody",
    }


def test_export_scope_returns_dicts(store):
    store.add_or_update(PRIVATE, "A", "正文", keywords=["k"], confidence=0.5)
    store.add_or_update(GROUP, "B", "正文", confidence=0.5)
    exported = store.export_scope(PRIVATE)
    assert len(exported) == 1, "只导出当前 scope"
    assert isinstance(exported[0], dict), "导出为 dict 便于 JSON 序列化"
    assert exported[0]["topic"] == "A"
    assert exported[0]["keywords"] == ["k"]
    # to_dict 的键集合即导出契约，缺键会打断导入侧
    for key in ("id", "scope_type", "scope_id", "content", "confidence", "origin"):
        assert key in exported[0]


def test_export_scope_empty(store):
    assert store.export_scope(Scope(SCOPE_PRIVATE, "nobody")) == []


def test_update_config_changes_only_given_fields(store):
    store.update_config(max_entries=3)
    assert store._max_entries == 3
    assert store._min_confidence == 0.3, "未传的字段保持原值"
    store.update_config(min_confidence=0.7)
    assert (store._max_entries, store._min_confidence) == (3, 0.7)
    store.update_config()
    # 两个都传 None 时是空操作，不应把配置清零
    assert (store._max_entries, store._min_confidence) == (3, 0.7)


def test_update_config_takes_effect_on_next_insert(tmp_path):
    st = _make_store(tmp_path, "cfg.db", max_entries=0)
    for i in range(3):
        st.add_or_update(PRIVATE, f"T{i}", "正文", confidence=0.5)
    assert st.count_all() == 3
    # 收紧容量后，下一次插入才触发淘汰（淘汰只在写路径里做）
    st.update_config(max_entries=1)
    st.add_or_update(PRIVATE, "New", "正文", confidence=0.99)
    assert st.count_all() == 1
    st.close()


# ---------- N. Dashboard 跨 scope 视图 ----------


def test_count_all_and_list_scopes(store):
    store.add_or_update(PRIVATE, "A", "正文", confidence=0.5)
    store.add_or_update(PRIVATE, "B", "正文", confidence=0.5)
    store.add_or_update(GROUP, "C", "正文", confidence=0.5)
    assert store.count_all() == 3
    scopes = store.list_scopes()
    # 按记忆数降序，private 有 2 条应排在前
    assert scopes[0] == {"scope_type": SCOPE_PRIVATE, "scope_id": "u1", "count": 2}
    assert scopes[1] == {"scope_type": SCOPE_GROUP, "scope_id": "g9", "count": 1}


def test_count_all_and_list_scopes_on_empty_db(store):
    assert store.count_all() == 0
    assert store.list_scopes() == []


def test_global_stats_aggregates_across_scopes(store):
    a = store.add_or_update(PRIVATE, "A", "正文", confidence=0.4)
    b = store.add_or_update(GROUP, "B", "正文", confidence=0.6)
    store.set_verified(a.id, True)
    store.inc_challenge(b.id)
    store.inc_challenge(b.id)

    gs = store.global_stats()
    assert gs["total"] == 2, "跨 scope 汇总，不做 scope 过滤"
    assert gs["verified"] == 1
    assert gs["challenged"] == 1, "被质疑过的条数"
    assert gs["challenged_total"] == 2, "质疑总次数（challenge_count 求和）"
    assert gs["avg_confidence"] == pytest.approx(0.5)
    assert gs["access_total"] == 2, "两条各自新建时 access_count=1"
    # global_stats 不绑定具体 scope，这两个键固定为 None
    assert gs["scope_type"] is None
    assert gs["scope_id"] is None


def test_global_stats_on_empty_db(store):
    gs = store.global_stats()
    assert gs["total"] == 0
    assert gs["avg_confidence"] == 0.0
    assert gs["access_total"] == 0
    assert gs["challenged_total"] == 0


# ---------- O. LLM token 用量 ----------


def test_record_token_usage_sums_total(store):
    store.record_token_usage("p1", 10, 5)
    stats = store.get_token_usage_stats()
    assert stats["total"]["prompt_tokens"] == 10
    assert stats["total"]["completion_tokens"] == 5
    # total_tokens 由 prompt + completion 在写入时算好
    assert stats["total"]["total_tokens"] == 15
    assert stats["total"]["calls"] == 1


def test_get_token_usage_stats_windows_and_providers(store):
    store.record_token_usage("p1", 10, 5)
    store.record_token_usage("p1", 20, 10)
    store.record_token_usage("p2", 1, 1)
    stats = store.get_token_usage_stats()
    for label in ("1d", "3d", "7d", "total"):
        assert stats[label]["calls"] == 3, f"{label} 窗口内应含全部 3 次调用"
        assert stats[label]["total_tokens"] == 47
    # per_provider 按 total 降序，p1 用量更大
    assert [p["provider_id"] for p in stats["per_provider"]] == ["p1", "p2"]
    assert stats["per_provider"][0]["calls"] == 2
    assert stats["per_provider"][0]["total_tokens"] == 45


def test_get_token_usage_stats_excludes_records_outside_window(store):
    import time as _t

    now = _t.time()
    # 直接按时间戳插入历史记录：10 天前的既不在 7d 窗口也不在 per_provider 里，
    # 但必须计入 total（total 不带时间条件）
    store._conn.execute(
        "INSERT INTO llm_token_usage (ts, provider_id, prompt_tokens,"
        " completion_tokens, total_tokens) VALUES (?, 'old', 100, 100, 200)",
        (now - 10 * 86400,),
    )
    store.record_token_usage("new", 1, 1)
    stats = store.get_token_usage_stats()
    assert stats["1d"]["calls"] == 1
    assert stats["7d"]["calls"] == 1
    assert stats["total"]["calls"] == 2
    assert stats["total"]["total_tokens"] == 202
    assert [p["provider_id"] for p in stats["per_provider"]] == ["new"]


def test_get_token_usage_stats_on_empty_table(store):
    stats = store.get_token_usage_stats()
    # COALESCE 保证空表下也是 0 而不是 None，前端可直接做算术
    assert stats["total"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }
    assert stats["per_provider"] == []


def test_cleanup_old_token_usage_deletes_only_expired(store):
    import time as _t

    now = _t.time()
    store._conn.execute(
        "INSERT INTO llm_token_usage (ts, provider_id, prompt_tokens,"
        " completion_tokens, total_tokens) VALUES (?, 'old', 1, 1, 2)",
        (now - 40 * 86400,),
    )
    store.record_token_usage("new", 1, 1)
    # 默认保留 30 天：40 天前那条被删，刚写的保留
    assert store.cleanup_old_token_usage() == 1
    assert store.get_token_usage_stats()["total"]["calls"] == 1


def test_cleanup_old_token_usage_returns_zero_when_nothing_expired(store):
    store.record_token_usage("p1", 1, 1)
    assert store.cleanup_old_token_usage(days=30) == 0


def test_cleanup_old_token_usage_negative_days_wipes_all(store):
    store.record_token_usage("p1", 1, 1)
    store.record_token_usage("p2", 1, 1)
    # days 为负会把 cutoff 推到未来，等价于清空；返回真实删除条数
    assert store.cleanup_old_token_usage(days=-1) == 2
    assert store.get_token_usage_stats()["total"]["calls"] == 0


# ---------- P. list_all_memories ----------


def test_list_all_memories_spans_scopes(store):
    store.add_or_update(PRIVATE, "A", "正文", confidence=0.5)
    store.add_or_update(GROUP, "B", "正文", confidence=0.5)
    store.add_or_update(GLOBAL, "C", "正文", confidence=0.5)
    entries, total, total_pages = store.list_all_memories(page=1, per_page=10)
    assert total == 3, "跨所有 scope 统计"
    assert total_pages == 1
    assert sorted(e.topic for e in entries) == ["A", "B", "C"]


def test_list_all_memories_filters_by_keyword_on_topic_or_content(store):
    store.add_or_update(PRIVATE, "Redis 缓存", "正文无关", confidence=0.5)
    store.add_or_update(PRIVATE, "别的标题", "正文里提到 Redis", confidence=0.5)
    store.add_or_update(PRIVATE, "无关", "无关正文", confidence=0.5)
    entries, total, _ = store.list_all_memories(keyword="Redis")
    # keyword 走 LIKE，topic 或 content 命中其一即可（与 FTS 分词无关，可子串匹配）
    assert total == 2
    assert sorted(e.topic for e in entries) == ["Redis 缓存", "别的标题"]


def test_list_all_memories_keyword_matches_cjk_substring(store):
    store.add_or_update(PRIVATE, "标题", "这是中文内容测试", confidence=0.5)
    # LIKE 是子串匹配，能补上 FTS5 中文分词的短板
    assert store.list_all_memories(keyword="中文")[1] == 1


def test_list_all_memories_keyword_no_match(store):
    store.add_or_update(PRIVATE, "A", "正文", confidence=0.5)
    entries, total, total_pages = store.list_all_memories(keyword="zzz")
    assert (entries, total, total_pages) == ([], 0, 1)


def test_list_all_memories_paginates_both_branches(store):
    for i in range(5):
        store.add_or_update(PRIVATE, f"topic{i}", "共同正文", confidence=0.5)
    # 无 keyword 分支
    assert len(store.list_all_memories(page=1, per_page=2)[0]) == 2
    assert store.list_all_memories(page=1, per_page=2)[2] == 3
    assert len(store.list_all_memories(page=99, per_page=2)[0]) == 1, "超界夹到末页"
    # 带 keyword 分支也要各自分页
    assert len(store.list_all_memories(page=1, per_page=2, keyword="topic")[0]) == 2
    assert len(store.list_all_memories(page=99, per_page=2, keyword="topic")[0]) == 1


def test_list_all_memories_on_empty_db(store):
    assert store.list_all_memories() == ([], 0, 1)


# ---------- Q. 群黑话候选 ----------


def test_add_slang_candidate_accumulates_occurrences(store):
    store.add_slang_candidate(PRIVATE, "yyds", "ctx1")
    store.add_slang_candidate(PRIVATE, "yyds", "ctx2")
    pending = store.list_pending_slang(PRIVATE)
    assert len(pending) == 1, "UNIQUE(scope,phrase) 冲突时累加而非插新行"
    assert pending[0]["occurrences"] == 2
    # context 用 excluded.context 刷成最新一次的上下文
    assert pending[0]["context"] == "ctx2"
    assert pending[0]["first_seen"] > 0
    assert pending[0]["last_seen"] >= pending[0]["first_seen"]


def test_list_pending_slang_orders_by_occurrences_desc(store):
    store.add_slang_candidate(PRIVATE, "low", "c")
    for _ in range(3):
        store.add_slang_candidate(PRIVATE, "high", "c")
    assert [x["phrase"] for x in store.list_pending_slang(PRIVATE)] == ["high", "low"]


def test_list_pending_slang_respects_limit(store):
    for p in ("a", "b", "c"):
        store.add_slang_candidate(PRIVATE, p, "c")
    assert len(store.list_pending_slang(PRIVATE, limit=2)) == 2


def test_mark_slang_learned_removes_from_pending(store):
    store.add_slang_candidate(PRIVATE, "yyds", "c")
    store.add_slang_candidate(PRIVATE, "awsl", "c")
    store.mark_slang_learned(PRIVATE, "yyds")
    # learned=1 后不再出现在待学列表，避免重复学习
    assert [x["phrase"] for x in store.list_pending_slang(PRIVATE)] == ["awsl"]


def test_mark_slang_learned_unknown_phrase_is_noop(store):
    store.add_slang_candidate(PRIVATE, "yyds", "c")
    store.mark_slang_learned(PRIVATE, "not_exist")
    assert len(store.list_pending_slang(PRIVATE)) == 1


def test_slang_is_scope_isolated(store):
    store.add_slang_candidate(PRIVATE, "yyds", "c")
    store.add_slang_candidate(GROUP, "yyds", "c")
    # 同一 phrase 在不同 scope 下互不影响（UNIQUE 键含 scope）
    assert len(store.list_pending_slang(PRIVATE)) == 1
    assert len(store.list_pending_slang(GROUP)) == 1
    store.mark_slang_learned(PRIVATE, "yyds")
    assert store.list_pending_slang(PRIVATE) == []
    assert len(store.list_pending_slang(GROUP)) == 1


def test_get_last_batch_time_zero_until_something_learned(store):
    # 无任何 learned 记录时 MAX() 返回 NULL，需转成 0.0 而不是 None，
    # 否则调用方做时间差计算会 TypeError
    assert store.get_last_batch_time(PRIVATE) == 0.0
    store.add_slang_candidate(PRIVATE, "yyds", "c")
    assert store.get_last_batch_time(PRIVATE) == 0.0, "仅有候选未学习仍为 0"
    store.mark_slang_learned(PRIVATE, "yyds")
    assert store.get_last_batch_time(PRIVATE) > 0


def test_get_last_batch_time_is_scope_isolated(store):
    store.add_slang_candidate(PRIVATE, "yyds", "c")
    store.mark_slang_learned(PRIVATE, "yyds")
    assert store.get_last_batch_time(GROUP) == 0.0
