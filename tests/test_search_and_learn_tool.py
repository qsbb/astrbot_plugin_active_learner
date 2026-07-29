import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.runtime import get_request_learning_state
from astrbot_plugin_active_learner.tools import SearchAndLearnTool


class _Store:
    def __init__(self):
        self.search_calls = 0

    def search(self, scope, topic, top_k=3):
        self.search_calls += 1
        return [
            SimpleNamespace(
                entry=SimpleNamespace(
                    topic="旧知识", content="旧内容", confidence=0.8
                )
            )
        ]


class _Plugin:
    def __init__(self):
        self._enable_web_search = True
        self._knowledge_source_priority = ("web",)
        self._web_search_only_highest_priority = False
        self._active_learn_was_called = False
        self.store = _Store()
        self.external_calls = 0

    async def _search_external_sources(self, query, web_limit, bili_limit):
        self.external_calls += 1
        return []


def _context():
    event = SimpleNamespace(
        message_obj=SimpleNamespace(group_id=""),
        get_sender_id=lambda: "user-1",
    )
    return SimpleNamespace(context=SimpleNamespace(event=event)), event


def _tool(plugin):
    tool = SearchAndLearnTool()
    object.__setattr__(tool, "_plugin", plugin)
    return tool


def test_tool_schema_exposes_force_refresh():
    schema = SearchAndLearnTool().parameters["properties"]["force_refresh"]
    assert schema["type"] == "boolean"
    assert schema["default"] is False


def test_default_call_still_uses_existing_local_memory():
    async def scenario():
        plugin = _Plugin()
        context, _ = _context()
        result = await _tool(plugin).call(context, topic="测试主题", query="测试主题")
        assert "本地记忆" in result
        assert plugin.store.search_calls == 1
        assert plugin.external_calls == 0

    asyncio.run(scenario())


def test_force_refresh_skips_local_memory_and_calls_external_search():
    async def scenario():
        plugin = _Plugin()
        context, event = _context()
        state = get_request_learning_state(event)
        result = await _tool(plugin).call(
            context,
            topic="测试主题",
            query="测试主题 最新资料",
            force_refresh=True,
        )
        assert "未找到结果" in result
        assert plugin.store.search_calls == 0
        assert plugin.external_calls == 1
        assert state.called is True
        assert plugin._active_learn_was_called is True

    asyncio.run(scenario())
