import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.graph_memory import (
    build_graph_associations,
    is_complex_query,
    normalize_reconstruction_mode,
    parse_route_selection,
    should_reconstruct,
)
from astrbot_plugin_active_learner.models import SCOPE_GROUP, SCOPE_PRIVATE, Scope
from astrbot_plugin_active_learner.retrieval import RetrievalMixin
from astrbot_plugin_active_learner.storage import MemoryStore


PRIVATE = Scope(SCOPE_PRIVATE, "user-a")
OTHER_PRIVATE = Scope(SCOPE_PRIVATE, "user-b")
GROUP = Scope(SCOPE_GROUP, "group-a")


def test_graph_associations_are_bounded_and_deduplicated():
    associations = build_graph_associations(
        "Quest 语音链路",
        "Quest 通过临调用声完成语音输出。",
        ["Quest", "临", "声", "Quest"],
        supplied=[
            {"cue": "Quest", "tag": "组成与依赖"},
            {"cue": "  ", "tag": "无效"},
            {"cue": "Quest", "tag": "组成与依赖"},
        ],
    )

    keys = {(item.cue, item.tag) for item in associations}
    assert len(associations) <= 24
    assert len(keys) == len(associations)
    assert ("Quest", "组成与依赖") in keys


def test_reconstruction_gate_and_mode_normalization_are_conservative():
    assert normalize_reconstruction_mode("SMART") == "smart"
    assert normalize_reconstruction_mode("unknown") == "fast"
    assert is_complex_query("以前和现在的实现有什么区别，为什么会变化？") is True
    assert is_complex_query("你好") is False
    assert (
        should_reconstruct(
            "普通问题",
            mode="off",
            passive_hit_count=0,
            required_hit_count=3,
        )
        is False
    )
    assert (
        should_reconstruct(
            "以前和现在有什么区别？",
            mode="fast",
            passive_hit_count=1,
            required_hit_count=3,
        )
        is True
    )


def test_route_parser_accepts_only_allowed_ids():
    assert parse_route_selection(
        "SELECT: known-2, invented, known-1, known-2",
        ["known-1", "known-2"],
        max_count=4,
    ) == ["known-2", "known-1"]
    assert parse_route_selection("known-1", ["known-1"], max_count=4) == []


def test_graph_storage_stays_scope_isolated(tmp_path):
    store = MemoryStore(tmp_path / "graph.db")
    try:
        first = store.add_or_update(
            PRIVATE,
            "Quest 语音链路",
            "Quest 通过临调用声完成语音输出。",
            keywords=["Quest", "临", "声"],
            confidence=0.8,
        )
        store.add_or_update(
            OTHER_PRIVATE,
            "另一位用户的 Quest 记录",
            "这条私聊记忆不能跨用户召回。",
            keywords=["Quest"],
            confidence=0.9,
        )
        store.add_or_update(
            GROUP,
            "群聊 Quest 记录",
            "这条群记忆不能进入私聊。",
            keywords=["Quest"],
            confidence=0.9,
        )

        hits = store.search_graph(
            PRIVATE,
            "Quest 语音链路由哪些模块组成？",
            include_global=False,
        )

        assert store._schema_version == 3
        assert store.graph_edge_count(PRIVATE) > 0
        assert [hit.entry.id for hit in hits] == [first.id]
        assert all(hit.retrieval_mode == "graph" for hit in hits)
        assert all(hit.entry.scope_id == PRIVATE.id for hit in hits)
    finally:
        store.close()


def test_graph_edges_follow_updates_and_deletes(tmp_path):
    store = MemoryStore(tmp_path / "sync.db")
    try:
        entry = store.add_or_update(
            PRIVATE,
            "旧线索",
            "旧内容",
            keywords=["旧线索"],
        )
        assert store.search_graph(PRIVATE, "旧线索", include_global=False)
        assert store.update_content(
            entry.id,
            "新内容包含新关系",
            0.7,
            scope=PRIVATE,
        )
        assert store.graph_edge_count(PRIVATE) > 0
        deleted, _ = store.forget(PRIVATE, "旧线索")
        assert deleted is True
        assert store.graph_edge_count(PRIVATE) == 0
    finally:
        store.close()


class _Router(RetrievalMixin):
    def __init__(self, reply):
        self._cfg_llm_provider_id = "provider-a"
        self.llm_service = SimpleNamespace(generate=self._generate)
        self.reply = reply
        self.calls = []

    async def _generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply


def test_smart_router_reorders_only_known_candidates():
    async def scenario():
        hits = [
            SimpleNamespace(
                entry=SimpleNamespace(id="a", content="A"),
                retrieval_path=("cue:A", "tag:A"),
            ),
            SimpleNamespace(
                entry=SimpleNamespace(id="b", content="B"),
                retrieval_path=("cue:B", "tag:B"),
            ),
        ]
        router = _Router("SELECT: invented, b")
        ordered = await router._route_graph_hits("比较 A 和 B", hits)
        assert [hit.entry.id for hit in ordered] == ["b", "a"]
        assert router.calls[0]["provider_id"] == "provider-a"

    asyncio.run(scenario())
