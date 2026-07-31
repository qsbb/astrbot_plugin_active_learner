"""诊断页与运行时配置同步回归测试。

v1.2.9：_sync_config_snapshot / _web_debug 随 main.py 分层重构迁至 web_api.py 的
WebApiMixin，本文件只跟随源码位置调整，断言与被测行为均未改动。
"""

import ast
import collections
import logging
import re
import textwrap
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
_WEB_API = _ROOT / "web_api.py"
_APP = _ROOT / "pages" / "manager" / "app.js"
_INDEX = _ROOT / "pages" / "manager" / "index.html"


def _extract_function(name: str):
    source = _WEB_API.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            text = textwrap.dedent(ast.get_source_segment(source, node))
            namespace = {}
            exec(compile(text, f"<{name}>", "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{_WEB_API.name} 中未找到 {name}")


class _ConfigManager:
    def all(self):
        return {"learn_weight": 0.7, "enable_bilibili": False}


class _Plugin:
    config_manager = _ConfigManager()
    config = {"learn_weight": 0.1, "enable_bilibili": True}


_sync_config_snapshot = _extract_function("_sync_config_snapshot")


def test_save_refreshes_shared_plugin_config_snapshot():
    """管理页保存后，直接读 plugin.config 的模块也必须立即拿到新值。"""
    plugin = _Plugin()
    merged = _sync_config_snapshot(
        plugin, {"learn_weight": 1.0, "enable_bilibili": True, "ignored": None}
    )
    assert merged["learn_weight"] == 1.0
    assert plugin.config["learn_weight"] == 1.0
    assert plugin.config["enable_bilibili"] is True
    assert "ignored" not in plugin.config


def test_debug_endpoint_uses_unified_config_and_omits_secrets():
    source = _WEB_API.read_text(encoding="utf-8")
    debug_src = textwrap.dedent(
        ast.get_source_segment(
            source,
            next(
                node
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "_web_debug"
            ),
        )
    )
    assert "self.config_manager.all()" in debug_src
    assert '"config": config_snapshot' in debug_src
    assert '"runtime": runtime_snapshot' in debug_src
    # docstring 可以说明敏感字段不会返回；真正要禁止的是读取或写入响应。
    assert 'cfg.get("provider_settings"' not in debug_src
    assert '"provider_settings":' not in debug_src
    assert 'cfg.get("api_key"' not in debug_src.lower()
    assert '"api_key":' not in debug_src.lower()


def test_diagnostic_page_renders_config_and_runtime_values():
    app = _APP.read_text(encoding="utf-8")
    assert "当前配置（统一配置源）" in app
    assert "实际运行值" in app
    assert "cfg.learn_weight" in app
    assert "runtime.learn_weight" in app
    assert "cfg.effective_provider_id" in app


def test_manager_page_exposes_explicit_bilibili_switch():
    app = _APP.read_text(encoding="utf-8")
    html = _INDEX.read_text(encoding="utf-8")
    assert 'id="settings-enable-bilibili"' in html
    assert "enable_bilibili: s.enable_bilibili === true" in app
    assert 'document.getElementById("settings-enable-bilibili").checked' in app
    web_api = _WEB_API.read_text(encoding="utf-8")
    assert 'self._enable_bilibili = bool(cfg.get("enable_bilibili", False))' in web_api
    assert "self._register_llm_tools()" in web_api


def test_page_assets_have_version_query_to_avoid_stale_webview_cache():
    import re

    html = _INDEX.read_text(encoding="utf-8")
    style_match = re.search(r'href="\./style\.css\?v=([^"]+)"', html)
    script_match = re.search(r'src="\./app\.js\?v=([^"]+)"', html)
    assert style_match and style_match.group(1)
    assert script_match and script_match.group(1)
    assert style_match.group(1) == script_match.group(1)


def test_series_diagnostic_contract_keeps_legacy_page_and_structured_buffer():
    main = (_ROOT / "main.py").read_text(encoding="utf-8")
    web_api = _WEB_API.read_text(encoding="utf-8")
    assert "def diagnostic_log_contract" in main
    assert '"name": "series.diagnostics"' in main
    assert "def diagnostic_events" in main
    assert "def diagnostic_clear" in main
    assert '"astrbot_log_propagation": False' in main
    assert '"plugin.ready"' in main
    assert '"plugin.terminated"' in main
    assert '"seq": self._sequence' in web_api
    assert '"stream_id": self._stream_id' in web_api
    assert 'item.get("text", "")' in web_api
    assert "record.module" in web_api
    assert "详细信息仅在知的独立日志页查看" in web_api
    assert '_SERIES_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})' in web_api
    assert "logger.propagate = False" in main


def test_series_diagnostic_snapshot_hides_raw_logger_message():
    source = _WEB_API.read_text(encoding="utf-8")
    node = next(
        item
        for item in ast.walk(ast.parse(source))
        if isinstance(item, ast.ClassDef) and item.name == "_BufferHandler"
    )
    namespace = {
        "Any": Any,
        "UTC": UTC,
        "collections": collections,
        "datetime": datetime,
        "logging": logging,
        "re": re,
        "threading": threading,
        "uuid": uuid,
    }
    class_source = "from __future__ import annotations\n" + textwrap.dedent(
        ast.get_source_segment(source, node)
    )
    exec(compile(class_source, "<_BufferHandler>", "exec"), namespace)
    buffer = collections.deque(maxlen=200)
    handler = namespace["_BufferHandler"](buffer)
    handler.emit(
        logging.LogRecord(
            "astrbot_plugin_active_learner.test",
            logging.WARNING,
            __file__,
            12,
            "private chat body %s",
            ("user-a",),
            None,
        )
    )
    payload = handler.snapshot()
    assert "private chat body" in buffer[-1]["text"]
    assert "private chat body" not in str(payload["events"])
    assert "user-a" not in str(payload["events"])
    assert payload["stream_id"] == handler.snapshot()["stream_id"]
    old_stream_id = payload["stream_id"]
    handler.clear()
    assert handler.snapshot()["stream_id"] != old_stream_id
