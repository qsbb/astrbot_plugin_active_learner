"""Optional adapter for 核's versioned model-router contract."""

from __future__ import annotations

import inspect
from typing import Any

ROUTER_PLUGIN_NAME = "astrbot_plugin_update_manager"


async def resolve_provider_id(context: Any, kind: str = "conversation") -> str:
    getter = getattr(context, "get_star_instance", None)
    if not callable(getter):
        return ""
    try:
        plugin = getter(ROUTER_PLUGIN_NAME)
        if inspect.isawaitable(plugin):
            plugin = await plugin
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
        if not isinstance(route, dict) or route.get("source") != "core" or route.get("available") is not True:
            return ""
        provider_id = str(provider_id or "").strip()[:256]
        provider_getter = getattr(context, "get_provider_by_id", None)
        if callable(provider_getter) and provider_getter(provider_id) is None:
            return ""
        return provider_id
    except Exception:
        return ""
