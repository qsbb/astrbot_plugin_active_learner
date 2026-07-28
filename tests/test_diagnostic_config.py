"""诊断页与运行时配置同步回归测试。

v1.2.9：_sync_config_snapshot / _web_debug 随 main.py 分层重构迁至 web_api.py 的
WebApiMixin，本文件只跟随源码位置调整，断言与被测行为均未改动。
"""

import ast
import textwrap
from pathlib import Path


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


def test_page_assets_have_version_query_to_avoid_stale_webview_cache():
    html = _INDEX.read_text(encoding="utf-8")
    assert 'href="./style.css?v=1.2.8"' in html
    assert 'src="./app.js?v=1.2.8"' in html
