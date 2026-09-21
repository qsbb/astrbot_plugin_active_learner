"""Optional adapter for 核's versioned model-router contract."""

from __future__ import annotations

import inspect
from typing import Any

ROUTER_PLUGIN_NAME = "astrbot_plugin_update_manager"
# AstrBot 4.x 把运行实例挂在 StarMetadata.star_cls 上，其余是旧版/兼容别名；
# 顺序与「核」自己的 adapter（core/adapters/astrbot.py）保持一致。
_STAR_INSTANCE_ATTRIBUTES = (
    "star_cls",
    "star",
    "instance",
    "star_instance",
    "plugin",
)


async def _resolve_router_plugin(context: Any) -> Any | None:
    """取核插件实例；任何一步失败都返回 None（fail-closed，绝不上抛）。

    1. 非官方 ``context.get_star_instance(name)``：部分集成/旧版 AstrBot 提供，
       返回值可能是 awaitable，存在就优先用；
    2. 官方 ``context.get_registered_star(name) -> StarMetadata | None``：官方
       Context 只有这个入口，运行实例挂在 star_cls / star / instance /
       star_instance / plugin 上（属性值是 class 而非实例时跳过继续找）。
    """
    getter = getattr(context, "get_star_instance", None)
    if callable(getter):
        try:
            plugin = getter(ROUTER_PLUGIN_NAME)
            if inspect.isawaitable(plugin):
                plugin = await plugin
            if plugin is not None and not isinstance(plugin, type):
                return plugin
        except Exception:
            pass

    metadata_getter = getattr(context, "get_registered_star", None)
    if not callable(metadata_getter):
        return None
    try:
        metadata = metadata_getter(ROUTER_PLUGIN_NAME)
        if inspect.isawaitable(metadata):
            metadata = await metadata
    except Exception:
        return None
    if metadata is None or isinstance(metadata, type):
        return None
    for attribute in _STAR_INSTANCE_ATTRIBUTES:
        try:
            instance = getattr(metadata, attribute, None)
        except Exception:
            continue
        if instance is None or isinstance(instance, type):
            continue
        return instance
    return None


async def resolve_provider_id(context: Any, kind: str = "conversation") -> str:
    plugin = await _resolve_router_plugin(context)
    try:
        declare = getattr(plugin, "series_model_router_contract", None)
        if not callable(declare):
            return ""
        contract = declare()
        if (
            not isinstance(contract, dict)
            or contract.get("name") != "series.model_router@1.0"
            or str(contract.get("version") or "").split(".", 1)[0] != "1"
            or contract.get("read_only") is not True
            or "resolve" not in (contract.get("capabilities") or ())
        ):
            return ""
        route = plugin.resolve_model_route(kind, plugin_override=None)
        if inspect.isawaitable(route):
            route = await route
        provider_id = route.get("provider_id") if isinstance(route, dict) else ""
        if (
            not isinstance(route, dict)
            or route.get("kind") != kind          # kind 必须与请求一致
            or route.get("source") != "core"
            or route.get("available") is not True
        ):
            return ""
        provider_id = str(provider_id or "").strip()[:256]
        provider_getter = getattr(context, "get_provider_by_id", None)
        if callable(provider_getter) and provider_getter(provider_id) is None:
            return ""
        return provider_id
    except Exception:
        return ""


async def resolve_model_route(context: Any, kind: str) -> dict:
    """向核索取完整路由 dict（provider_id / model / voice / source / available）。

    契约不可用、版本不兼容、source 非 core、available 非 True 或 kind 不匹配时
    一律返回空 dict，调用方按「核不可用」处理并回退本地逻辑。
    """
    if not isinstance(kind, str) or not kind.strip():
        return {}
    kind = kind.strip()
    plugin = await _resolve_router_plugin(context)
    try:
        declare = getattr(plugin, "series_model_router_contract", None)
        resolver = getattr(plugin, "resolve_model_route", None)
        if not callable(declare) or not callable(resolver):
            return {}
        contract = declare()
        if (
            not isinstance(contract, dict)
            or contract.get("name") != "series.model_router@1.0"
            or str(contract.get("version") or "").split(".", 1)[0] != "1"
            or contract.get("read_only") is not True
            or "resolve" not in (contract.get("capabilities") or ())
        ):
            return {}
        try:
            route = resolver(kind, plugin_override=None)
        except TypeError:
            route = resolver(kind)
        if inspect.isawaitable(route):
            route = await route
    except Exception:
        return {}
    if not isinstance(route, dict):
        return {}
    if (
        route.get("kind") != kind
        or route.get("source") != "core"
        or route.get("available") is not True
    ):
        return {}
    return dict(route)
