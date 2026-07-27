"""配置管理与 LLM 并发控制回归测试。

覆盖本轮修复：
- ConfigManager 作为唯一配置源（overlay 覆盖 AstrBot 插件配置页）
- Dashboard 部分更新语义（None 跳过、"" 可清空）
- llm_max_concurrency 生效，后台 LLM 调用不再形成瞬时请求风暴（429 诱因）
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.config_manager import ConfigManager
from astrbot_plugin_active_learner.llm_service import LLMService


# ---------- ConfigManager ----------


def test_overlay_overrides_astrbot_config(tmp_path):
    (tmp_path / "active_learner_settings.json").write_text(
        json.dumps({"llm_provider_id": "dashboard-pid"}), encoding="utf-8"
    )
    cfg = ConfigManager(tmp_path, {"llm_provider_id": "schema-pid", "search_top_k": 5})
    assert cfg.get("llm_provider_id") == "dashboard-pid"
    # 未被 overlay 覆盖的字段仍回落到 AstrBot 插件配置页
    assert cfg.get("search_top_k") == 5


def test_all_includes_schema_defaults_but_overlay_only_returns_dashboard(tmp_path):
    cfg = ConfigManager(tmp_path, {"search_top_k": 5, "learn_weight": 0.7})
    cfg.update(learn_weight=0.9)
    merged = cfg.all()
    assert merged["search_top_k"] == 5
    assert merged["learn_weight"] == 0.9
    # overlay 层只含 Dashboard 显式写入，避免把默认值写回磁盘
    assert cfg.overlay_all() == {"learn_weight": 0.9}


def test_update_is_persisted_and_reloaded(tmp_path):
    ConfigManager(tmp_path, {}).update(llm_provider_id="pid-a", search_top_k=8)
    reloaded = ConfigManager(tmp_path, {"search_top_k": 5})
    assert reloaded.get("llm_provider_id") == "pid-a"
    assert reloaded.get("search_top_k") == 8


def test_none_is_partial_update_while_empty_string_clears(tmp_path):
    cfg = ConfigManager(tmp_path, {})
    cfg.update(llm_provider_id="pid-a", admin_ids="123")
    cfg.update(llm_provider_id=None)
    assert cfg.get("llm_provider_id") == "pid-a"
    # 清空显式选择要用 ""：ConfigManager 如实返回 ""，
    # 由调用方（_web_providers/_web_get_settings）再回落到事件默认 Provider
    cfg.update(llm_provider_id="")
    assert cfg.get("llm_provider_id") == ""
    assert cfg.get("admin_ids") == "123"


def test_corrupted_settings_file_does_not_break_startup(tmp_path):
    (tmp_path / "active_learner_settings.json").write_text("{not json", encoding="utf-8")
    cfg = ConfigManager(tmp_path, {"search_top_k": 5})
    assert cfg.get("search_top_k") == 5


def test_numeric_getters_clamp_and_fallback(tmp_path):
    cfg = ConfigManager(tmp_path, {"search_top_k": 999, "learn_weight": "abc"})
    assert cfg.get_int("search_top_k", 5, min_val=1, max_val=20) == 20
    assert cfg.get_float("learn_weight", 0.7) == 0.7
    assert cfg.get_int("missing_key", 3) == 3


# ---------- LLMService 并发控制 ----------


class _FakeStore:
    def record_token_usage(self, *_args, **_kwargs):
        return None


class _FakeContext:
    """记录 llm_generate 的并发峰值。"""

    def __init__(self, delay=0.02):
        self._delay = delay
        self.active = 0
        self.peak = 0
        self.calls = 0

    async def llm_generate(self, chat_provider_id=None, prompt=""):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls += 1
        try:
            await asyncio.sleep(self._delay)
        finally:
            self.active -= 1
        return type("Resp", (), {"completion_text": "ok"})()


class _FakePlugin:
    def __init__(self, tmp_path, concurrency):
        self.config_manager = ConfigManager(
            tmp_path, {"llm_max_concurrency": concurrency}
        )
        self.context = _FakeContext()
        self.store = _FakeStore()

    async def _resolve_plugin_provider_id(self, umo=""):
        return "pid-a"


def _run_parallel_generate(tmp_path, concurrency, call_count=6):
    async def scenario():
        plugin = _FakePlugin(tmp_path, concurrency)
        service = LLMService(plugin)
        await asyncio.gather(
            *(service.generate(f"prompt-{i}") for i in range(call_count))
        )
        return plugin.context

    return asyncio.run(scenario())


def test_default_concurrency_serializes_llm_calls(tmp_path):
    """默认串行：后台学习 + 工具续轮并发触发时不会打满低 RPM Provider。"""
    ctx = _run_parallel_generate(tmp_path, concurrency=1)
    assert ctx.peak == 1
    assert ctx.calls == 6


def test_configured_concurrency_is_respected(tmp_path):
    ctx = _run_parallel_generate(tmp_path, concurrency=3)
    assert ctx.peak == 3


def test_concurrency_is_clamped_to_safe_range(tmp_path):
    # 上限钳到 4，防止配置写入过大值再次触发 429
    assert _run_parallel_generate(tmp_path, concurrency=99, call_count=8).peak == 4
    # 非法值与非正数回退为串行
    assert _run_parallel_generate(tmp_path, concurrency=0).peak == 1
    assert _run_parallel_generate(tmp_path, concurrency="abc").peak == 1


def test_generate_skips_call_when_provider_unresolved(tmp_path):
    async def scenario():
        plugin = _FakePlugin(tmp_path, 1)

        async def _no_provider(umo=""):
            return ""

        plugin._resolve_plugin_provider_id = _no_provider
        service = LLMService(plugin)
        result = await service.generate("prompt")
        assert result == ""
        assert plugin.context.calls == 0

    asyncio.run(scenario())


def test_generate_releases_semaphore_after_provider_error(tmp_path):
    """调用失败必须释放信号量，否则串行模式下后续调用会永久阻塞。"""

    async def scenario():
        plugin = _FakePlugin(tmp_path, 1)
        calls = {"n": 0}

        async def flaky(chat_provider_id=None, prompt=""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("429 Too Many Requests")
            return type("Resp", (), {"completion_text": "recovered"})()

        plugin.context.llm_generate = flaky
        service = LLMService(plugin)
        assert await service.generate("first") == ""
        assert await asyncio.wait_for(service.generate("second"), timeout=1) == "recovered"

    asyncio.run(scenario())
