import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.retrieval import RetrievalMixin


class _External:
    def __init__(self):
        self.call_names = []

    async def search(self, calls, deadline):
        self.call_names = list(calls)
        return []


class _Sources(RetrievalMixin):
    def __init__(self, enabled_urls=1, only_top=False):
        self._knowledge_source_priority = ["url", "web", "bilibili"]
        self._web_search_only_highest_priority = only_top
        self._enable_web_search = True
        self.url_sources = SimpleNamespace(
            enabled_count=enabled_urls,
            search=lambda *args, **kwargs: None,
        )
        self.searcher = SimpleNamespace(is_available=True)
        self.bili_source = SimpleNamespace(is_available=lambda: True)
        self._external_search = _External()


class _InjectedRetrieval(RetrievalMixin):
    def __init__(self, hits):
        self._hits = hits
        self.embedder = None
        self._context_inject_count = 3
        self._priority_topics = []
        self._hybrid_weights = (0.4, 0.6)
        self._enable_scope_fallback = True
        self._decay_half_life_days = 30
        self._priority_boost = 1.0

    async def _search_memory_once(self, scope, query, query_vec=None):
        return list(self._hits)


def test_source_priority_accepts_url_and_removes_duplicates():
    assert RetrievalMixin._parse_source_priority("URL,web,url,bilibili,bad") == [
        "url",
        "web",
        "bilibili",
    ]
    assert RetrievalMixin._parse_source_priority("") == ["url", "web", "bilibili"]


def test_only_highest_priority_skips_unconfigured_url_source():
    sources = _Sources(enabled_urls=0, only_top=True)
    assert sources._is_source_enabled("url") is False
    assert sources._is_source_enabled("web") is True
    assert sources._is_source_enabled("bilibili") is False


def test_external_search_registers_all_available_sources():
    async def scenario():
        sources = _Sources(enabled_urls=1, only_top=False)
        await sources._search_external_sources("topic")
        assert sources._external_search.call_names == ["url", "web", "bilibili"]

    asyncio.run(scenario())


def test_external_search_preserves_configured_quality_order():
    async def scenario():
        sources = _Sources(enabled_urls=1, only_top=False)
        sources._knowledge_source_priority = ["web", "url", "bilibili"]
        await sources._search_external_sources("topic")
        assert sources._external_search.call_names == ["web", "url", "bilibili"]

    asyncio.run(scenario())


def test_automatic_injection_filters_partial_entity_overlap():
    async def scenario():
        wrong = SimpleNamespace(
            score=0.99,
            entry=SimpleNamespace(
                id="wrong",
                topic="卡拉彼丘令（牢令）角色",
                keywords=["卡拉彼丘", "令"],
                content="另一位角色",
                confidence=0.95,
                verified=False,
            ),
        )
        matching = SimpleNamespace(
            score=0.5,
            entry=SimpleNamespace(
                id="matching",
                topic="卡拉彼丘 诺诺",
                keywords=["卡拉彼丘", "诺诺"],
                content="诺诺的资料",
                confidence=0.5,
                verified=False,
            ),
        )
        scope = SimpleNamespace()
        mismatched_hits, _, _ = await _InjectedRetrieval([wrong])._retrieve_memory(
            scope, "卡拉彼丘 诺诺 角色 武器 定位 技能"
        )
        matched_hits, _, _ = await _InjectedRetrieval(
            [wrong, matching]
        )._retrieve_memory(scope, "卡拉彼丘 诺诺 角色 武器 定位 技能")
        assert mismatched_hits == []
        assert [hit.entry.id for hit in matched_hits] == ["matching"]

    asyncio.run(scenario())
