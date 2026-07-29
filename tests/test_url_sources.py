import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.url_sources import (
    UrlSourceRegistry,
    mediawiki_api_url,
    normalize_source_url,
)


class _Searcher:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    async def fetch_url(self, url, max_chars=12000):
        self.calls.append((url, max_chars))
        return self.text


def test_url_validation_and_mediawiki_api_derivation():
    assert normalize_source_url("HTTPS://Example.COM/wiki/Test#part") == (
        "https://example.com/wiki/Test"
    )
    assert mediawiki_api_url("https://zh.wikipedia.org/wiki/诺诺") == (
        "https://zh.wikipedia.org/w/api.php"
    )
    assert mediawiki_api_url("https://wiki.example.org/w/api.php") == (
        "https://wiki.example.org/w/api.php"
    )
    with pytest.raises(ValueError):
        normalize_source_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        normalize_source_url("https://user:secret@example.org/wiki")


def test_registry_persists_toggles_and_deletion(tmp_path):
    registry = UrlSourceRegistry(tmp_path, _Searcher())
    added = registry.add_source(
        name="测试百科",
        url="https://example.org/wiki/",
        source_type="mediawiki",
    )
    assert registry.enabled_count == 1

    reloaded = UrlSourceRegistry(tmp_path, _Searcher())
    assert reloaded.list_sources()[0]["id"] == added["id"]
    reloaded.set_enabled(added["id"], False)
    assert reloaded.enabled_count == 0
    assert reloaded.remove_source(added["id"]) is True
    assert reloaded.list_sources() == []


def test_duplicate_url_is_rejected(tmp_path):
    registry = UrlSourceRegistry(tmp_path, _Searcher())
    registry.add_source(name="A", url="https://example.org/page")
    with pytest.raises(ValueError, match="已存在"):
        registry.add_source(name="B", url="https://example.org/page#fragment")


def test_fixed_page_only_returns_when_core_topic_is_present(tmp_path):
    async def scenario():
        searcher = _Searcher("卡拉彼丘中的诺诺是一位游戏角色。这里是她的背景资料。")
        registry = UrlSourceRegistry(tmp_path, searcher)
        registry.add_source(name="角色页", url="https://example.org/characters")

        found = await registry.search("卡拉彼丘 诺诺", max_results=3)
        assert len(found) == 1
        assert found[0]["title"] == "角色页"
        assert "诺诺" in found[0]["snippet"]
        assert await registry.search("卡拉彼丘 令", max_results=3) == []
        assert await registry.search("另一个完全无关的主题", max_results=3) == []

    asyncio.run(scenario())
