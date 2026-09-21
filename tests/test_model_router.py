"""核统一模型路由适配层测试（series.model_router@1.x，知）。

重点覆盖：官方 Context 只有 get_registered_star 时仍能拿到核实例，
非官方 get_star_instance 存在时优先，以及各种解析失败一律 fail-closed。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.model_router import (  # noqa: E402
    _resolve_router_plugin,
    resolve_model_route,
    resolve_provider_id,
)

CONTRACT = {
    "name": "series.model_router@1.0",
    "version": "1.0",
    "read_only": True,
    "capabilities": ("resolve", "status"),
}


class _Router:
    """核插件实例的最小替身：契约 + 路由解析。"""

    def __init__(self, route, contract=None) -> None:
        self.route = route
        self.contract = contract if contract is not None else CONTRACT
        self.kinds: list[str] = []

    def series_model_router_contract(self):
        return self.contract

    def resolve_model_route(self, kind, **_kwargs):
        self.kinds.append(kind)
        return {**self.route, "kind": kind}


def _core_route(**overrides):
    route = {"source": "core", "available": True, "provider_id": "core-chat"}
    route.update(overrides)
    return route


class _StarMetadata:
    """模拟 AstrBot ``StarMetadata``：运行实例只挂在属性上。"""

    def __init__(self, **attributes) -> None:
        self._attributes = attributes

    def __getattr__(self, name):
        try:
            return self._attributes[name]
        except KeyError:
            raise AttributeError(name) from None


class _OfficialContext:
    """只有官方 API 的环境：有 get_registered_star，没有 get_star_instance。"""

    def __init__(self, metadata=None, providers=None) -> None:
        self.metadata = metadata
        # providers=None 表示“任何非空 provider_id 都存在”，传入集合则做真实复核。
        self.providers = None if providers is None else set(providers)
        self.registered: list[str] = []

    def get_registered_star(self, plugin_name):
        self.registered.append(plugin_name)
        if isinstance(self.metadata, Exception):
            raise self.metadata
        return self.metadata

    def get_provider_by_id(self, provider_id):
        if not provider_id:
            return None
        if self.providers is None:
            return object()
        return object() if provider_id in self.providers else None


def test_official_api_only_context_resolves_core_router():
    """AstrBot 4.x 没有 get_star_instance：官方 API 回退必须真的拿到核。"""
    router = _Router(_core_route(model="core-model"))
    context = _OfficialContext(_StarMetadata(star_cls=router))

    assert asyncio.run(resolve_provider_id(context, "conversation")) == "core-chat"
    route = asyncio.run(resolve_model_route(context, "conversation"))
    assert route["provider_id"] == "core-chat"
    assert route["model"] == "core-model"
    assert router.kinds == ["conversation", "conversation"]
    assert context.registered == ["astrbot_plugin_update_manager"] * 2


def test_get_star_instance_takes_priority():
    """非官方 API 存在时必须优先，且不再走官方回退。"""
    preferred = _Router(_core_route(provider_id="preferred"))
    fallback = _Router(_core_route(provider_id="fallback"))

    class _HybridContext:
        def __init__(self):
            self.registered: list[str] = []

        def get_star_instance(self, _plugin_name):
            return preferred

        def get_registered_star(self, plugin_name):
            self.registered.append(plugin_name)
            return _StarMetadata(star_cls=fallback)

        def get_provider_by_id(self, _provider_id):
            return object()

    context = _HybridContext()
    assert asyncio.run(resolve_provider_id(context, "conversation")) == "preferred"
    assert fallback.kinds == []
    assert context.registered == []


def test_get_star_instance_failure_falls_back_to_official_api():
    """非官方入口抛异常（或返回 None）时继续走官方 API，不能整体失败。"""

    class _BrokenInstanceContext:
        def get_star_instance(self, _plugin_name):
            raise RuntimeError("legacy lookup broken")

        def get_registered_star(self, _plugin_name):
            return _StarMetadata(star_cls=_Router(_core_route()))

        def get_provider_by_id(self, _provider_id):
            return object()

    assert (
        asyncio.run(resolve_provider_id(_BrokenInstanceContext(), "conversation"))
        == "core-chat"
    )


def test_awaitable_lookups_are_supported():
    """两个入口都可能是 awaitable（async 方法/协程返回值）。"""

    class _AsyncInstanceContext:
        async def get_star_instance(self, _plugin_name):
            return _Router(_core_route(provider_id="async-instance"))

        def get_provider_by_id(self, _provider_id):
            return object()

    class _AwaitableMetadata:
        def __await__(self):
            async def _resolve():
                return _StarMetadata(star_cls=_Router(_core_route()))

            return _resolve().__await__()

    class _AsyncMetadataContext:
        def get_registered_star(self, _plugin_name):
            return _AwaitableMetadata()

        def get_provider_by_id(self, _provider_id):
            return object()

    assert (
        asyncio.run(resolve_provider_id(_AsyncInstanceContext(), "conversation"))
        == "async-instance"
    )
    assert (
        asyncio.run(resolve_provider_id(_AsyncMetadataContext(), "conversation"))
        == "core-chat"
    )


def test_missing_registered_star_is_fail_closed():
    context = _OfficialContext(None)
    assert asyncio.run(resolve_provider_id(context, "conversation")) == ""
    assert asyncio.run(resolve_model_route(context, "conversation")) == {}


def test_raising_registered_star_is_fail_closed():
    context = _OfficialContext(RuntimeError("registry unavailable"))
    assert asyncio.run(resolve_provider_id(context, "conversation")) == ""
    assert asyncio.run(resolve_model_route(context, "conversation")) == {}


def test_context_without_any_lookup_api_is_fail_closed():
    class _BareContext:
        pass

    assert asyncio.run(_resolve_router_plugin(_BareContext())) is None
    assert asyncio.run(resolve_provider_id(_BareContext(), "conversation")) == ""


def test_star_cls_holding_a_class_is_skipped():
    """star_cls 是 class（而非实例）时跳过继续找，绝不能把 class 当实例用。"""
    class_only = _OfficialContext(_StarMetadata(star_cls=_Router))
    assert asyncio.run(resolve_provider_id(class_only, "conversation")) == ""

    later_alias = _OfficialContext(
        _StarMetadata(star_cls=_Router, star_instance=_Router(_core_route()))
    )
    assert asyncio.run(resolve_provider_id(later_alias, "conversation")) == "core-chat"


def test_instance_attribute_aliases_are_supported():
    for attribute in ("star_cls", "star", "instance", "star_instance", "plugin"):
        context = _OfficialContext(_StarMetadata(**{attribute: _Router(_core_route())}))
        assert (
            asyncio.run(resolve_provider_id(context, "conversation")) == "core-chat"
        ), attribute


def test_broken_metadata_attributes_are_tolerated():
    class _ExplodingMetadata:
        @property
        def star_cls(self):
            raise TypeError("cannot read star_cls")

        plugin = _Router(_core_route())

    context = _OfficialContext(_ExplodingMetadata())
    assert asyncio.run(resolve_provider_id(context, "conversation")) == "core-chat"


def test_metadata_without_instance_attributes_is_fail_closed():
    for metadata in (_StarMetadata(star_cls=None), _StarMetadata(), object()):
        context = _OfficialContext(metadata)
        assert asyncio.run(resolve_provider_id(context, "conversation")) == ""
        assert asyncio.run(resolve_model_route(context, "conversation")) == {}


def test_resolved_instance_is_validated_against_contract():
    """解析成功不等于可用：原有契约/路由校验链保持不变。"""
    bad_contract = {
        "name": "series.other@1.0",
        "version": "1.0",
        "read_only": True,
        "capabilities": ("resolve",),
    }

    def _resolve(router=None, *, providers=None):
        context = _OfficialContext(_StarMetadata(star_cls=router), providers=providers)
        return asyncio.run(resolve_provider_id(context, "conversation"))

    assert _resolve(_Router(_core_route(), bad_contract)) == ""
    assert _resolve(_Router(_core_route(), {**CONTRACT, "read_only": False})) == ""
    assert _resolve(_Router(_core_route(source="astrbot"))) == ""
    # provider 已从 AstrBot 消失时同样 fail-closed
    assert _resolve(_Router(_core_route()), providers=()) == ""
