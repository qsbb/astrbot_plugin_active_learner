"""Provider 校验回归测试：Dashboard 里能选到的 provider 必须能保存。

AstrBot 4.x 的 ProviderManager 暴露 provider_insts / inst_map / providers_config，
Provider 实例只有 provider_config 和 meta()，没有 id / name 属性。此前
_provider_exists 读 pm.providers（不存在）并退回 cmd_config.json，
导致「下拉框有、保存说没有」。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ---- stub astrbot 运行时（本仓库测试不依赖真实 AstrBot） ----
_astrbot = types.ModuleType("astrbot")
_astrbot_api = types.ModuleType("astrbot.api")
_astrbot_star = types.ModuleType("astrbot.api.star")


class _StarTools:
    @staticmethod
    def get_data_dir():
        return Path("/tmp/astrbot_plugin_active_learner_test_data")


_astrbot_star.Context = object
_astrbot_star.StarTools = _StarTools
_astrbot_api.star = _astrbot_star

_astrbot_web = types.ModuleType("astrbot.api.web")


def _error_response(msg, status_code=400):
    return {"ok": False, "error": msg, "status_code": status_code}


def _json_response(payload):
    return payload


class _Request:
    payload: ClassVar[dict] = {}

    async def json(self, default=None):
        return dict(self.payload)


_astrbot_web.error_response = _error_response
_astrbot_web.json_response = _json_response
_astrbot_web.file_response = lambda *args, **kwargs: None
_astrbot_web.request = _Request()
_astrbot_api.web = _astrbot_web

sys.modules.setdefault("astrbot", _astrbot)
sys.modules.setdefault("astrbot.api", _astrbot_api)
sys.modules.setdefault("astrbot.api.star", _astrbot_star)
sys.modules.setdefault("astrbot.api.web", _astrbot_web)

from astrbot_plugin_active_learner import web_api
from astrbot_plugin_active_learner.web_api import WebApiMixin

PID = "雾里云free/space-bunny"


class _Provider:
    """模拟 AstrBot 4.x Provider：无 id / name 属性，只有 provider_config + meta()。"""

    def __init__(self, provider_id: str, model: str = "space-bunny") -> None:
        self.provider_config = {
            "id": provider_id,
            "type": "openai_chat_completion",
            "model": model,
        }
        self._model = model

    def get_model(self) -> str:
        return self._model

    def meta(self):
        return SimpleNamespace(
            id=self.provider_config["id"],
            model=self._model,
            type="openai_chat_completion",
        )


class _Manager:
    """模拟 AstrBot 4.x ProviderManager：没有 providers 属性。"""

    def __init__(self, insts: list[_Provider] | None = None) -> None:
        self.provider_insts = list(insts or [])
        self.inst_map = {p.provider_config["id"]: p for p in self.provider_insts}
        self.providers_config = [dict(p.provider_config) for p in self.provider_insts]
        self.curr_provider_inst = self.provider_insts[0] if self.provider_insts else None


class _Context:
    def __init__(self, manager: _Manager) -> None:
        self.provider_manager = manager

    def get_all_providers(self):
        return self.provider_manager.provider_insts


def _make_api(manager: _Manager) -> WebApiMixin:
    api = WebApiMixin.__new__(WebApiMixin)
    api.context = _Context(manager)
    api._cfg_llm_provider_id = ""
    return api


def _patch_config(monkeypatch, api, providers, default=""):
    monkeypatch.setattr(
        api,
        "_get_providers_from_config",
        lambda: (default, list(providers)),
    )


def _cfg_provider(provider_id: str, enable: bool = True) -> dict:
    return {"id": provider_id, "type": "openai_chat_completion", "model": "", "enable": enable}


def test_provider_exists_accepts_provider_only_in_manager(monkeypatch):
    """回归：provider 在 manager 里、但 cmd_config 是另一份列表时必须放行。"""
    api = _make_api(_Manager([_Provider(PID)]))
    _patch_config(monkeypatch, api, [_cfg_provider("other-provider")])

    assert api._provider_exists(PID) is True


def test_provider_exists_rejects_unknown_when_candidates_available():
    api = _make_api(_Manager([_Provider("loaded-provider")]))

    assert api._provider_exists("deleted-provider") is False


def test_provider_exists_falls_back_to_config_when_manager_empty(monkeypatch):
    api = _make_api(_Manager([]))
    _patch_config(monkeypatch, api, [_cfg_provider("cfg-only")])

    assert api._provider_exists("cfg-only") is True
    assert api._provider_exists("missing") is False


def test_provider_exists_is_lenient_without_any_source(monkeypatch):
    api = _make_api(_Manager([]))
    _patch_config(monkeypatch, api, [])

    assert api._provider_exists("anything") is True


def test_default_provider_resolves_using_provider_config(monkeypatch):
    """get_using_provider 返回实例时，要从 provider_config 取 id。"""
    api = _make_api(_Manager([_Provider("first-loaded"), _Provider("current")]))
    api.context.get_using_provider = lambda: api.context.provider_manager.provider_insts[-1]
    _patch_config(monkeypatch, api, [_cfg_provider("from-file")])

    assert api._resolve_default_provider_id() == "current"


def test_default_provider_prefers_loaded_instances_over_config_file(monkeypatch):
    manager = _Manager([_Provider("loaded-provider")])
    manager.curr_provider_inst = None
    api = _make_api(manager)
    _patch_config(monkeypatch, api, [_cfg_provider("from-file")])

    assert api._resolve_default_provider_id() == "loaded-provider"


def test_save_settings_accepts_listed_provider(monkeypatch):
    """端到端回归：保存 Dashboard 设置不再误报 provider 不存在。"""
    api = _make_api(_Manager([_Provider(PID)]))
    _patch_config(monkeypatch, api, [_cfg_provider("from-file")])
    settings = {"llm_provider_id": ""}

    class _ConfigManager:
        def update(self, **kwargs):
            settings.update(kwargs)
            return dict(settings)

    api.config_manager = _ConfigManager()
    api._load_schema = dict
    api._apply_config_to_runtime = lambda updated: None
    web_api.request.payload = {"llm_provider_id": PID}

    resp = asyncio.run(api._web_save_settings())

    assert "error" not in resp
    assert resp["llm_provider_id"] == PID
    assert settings["llm_provider_id"] == PID


def test_save_settings_still_rejects_deleted_provider(monkeypatch):
    api = _make_api(_Manager([_Provider("loaded-provider")]))
    _patch_config(monkeypatch, api, [_cfg_provider("loaded-provider")])
    settings = {"llm_provider_id": ""}

    class _ConfigManager:
        def update(self, **kwargs):
            settings.update(kwargs)
            return dict(settings)

    api.config_manager = _ConfigManager()
    api._load_schema = dict
    api._apply_config_to_runtime = lambda updated: None
    web_api.request.payload = {"llm_provider_id": "deleted-provider"}

    resp = asyncio.run(api._web_save_settings())

    assert resp["ok"] is False
    assert "不存在" in resp["error"]
    assert settings["llm_provider_id"] == ""
