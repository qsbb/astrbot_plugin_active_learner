"""Embedder 测试：本地 provider 优先、核 "embedding" 路由、失败静默回退。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.embedder import Embedder  # noqa: E402

CONTRACT = {
    "name": "series.model_router@1.0",
    "version": "1.0",
    "read_only": True,
    "capabilities": ("resolve",),
}


class _KernelRouter:
    """核插件实例替身：声明契约并解析 embedding 路由。"""

    def __init__(self, route=None, contract=None) -> None:
        self.route = route if route is not None else _embedding_route()
        self.contract = contract if contract is not None else CONTRACT
        self.kinds: list[str] = []

    def series_model_router_contract(self):
        return self.contract

    def resolve_model_route(self, kind, **_kwargs):
        self.kinds.append(kind)
        return {**self.route, "kind": kind}


def _embedding_route(**overrides):
    route = {"source": "core", "available": True, "provider_id": "core-embed"}
    route.update(overrides)
    return route


class _StarMetadata:
    """模拟 AstrBot ``StarMetadata``：运行实例挂在 star_cls 上。"""

    def __init__(self, **attributes) -> None:
        self._attributes = attributes

    def __getattr__(self, name):
        try:
            return self._attributes[name]
        except KeyError:
            raise AttributeError(name) from None


class _EmbeddingProvider:
    """伪 EmbeddingProvider：只有单条接口（批量会走逐条兜底）。"""

    def __init__(self, dim=2, model_name="fake-embed", fail=False) -> None:
        self._dim = dim
        self.model_name = model_name
        self.fail = fail
        self.queries: list[str] = []

    def get_dim(self):
        return self._dim

    async def get_embedding(self, text):
        if self.fail:
            raise RuntimeError("embedding backend down")
        self.queries.append(text)
        return [float(len(text))] * self._dim


class _BatchEmbeddingProvider(_EmbeddingProvider):
    def __init__(self, **_kwargs) -> None:
        super().__init__(**_kwargs)
        self.batches: list[list[str]] = []

    async def get_embeddings_batch(self, texts):
        self.batches.append(list(texts))
        return [await self.get_embedding(text) for text in texts]


class _Context:
    """AstrBot Context 替身：默认只提供官方 get_registered_star（4.x 真实形态）。"""

    _UNSET = object()

    def __init__(
        self,
        *,
        local_providers=(),
        with_local_api=True,
        router=None,
        providers=None,
        registered_star=_UNSET,
        registered_error=None,
        provider_lookup_error=None,
    ) -> None:
        self.local_providers = list(local_providers)
        self.router = router
        self.providers = dict(providers or {})
        self.registered_star = (
            _StarMetadata(star_cls=router) if registered_star is self._UNSET
            else registered_star
        )
        self.registered_error = registered_error
        self.provider_lookup_error = provider_lookup_error
        self.registered: list[str] = []
        self.provider_lookups: list[str] = []
        if not with_local_api:
            # 模拟 AstrBot 未暴露 embedding provider 列表
            self.get_all_embedding_providers = None

    def get_all_embedding_providers(self):
        return list(self.local_providers)

    def get_registered_star(self, plugin_name):
        self.registered.append(plugin_name)
        if self.registered_error is not None:
            raise self.registered_error
        return self.registered_star

    def get_provider_by_id(self, provider_id):
        self.provider_lookups.append(provider_id)
        if self.provider_lookup_error is not None:
            raise self.provider_lookup_error
        return self.providers.get(provider_id)


def _embedder(context):
    return Embedder(SimpleNamespace(context=context))


# ---------- 核 embedding 路由 ----------


def test_embed_query_uses_kernel_embedding_route():
    """本地没有 provider 时，按核 "embedding" 路由换实例并调 get_embedding。"""
    provider = _EmbeddingProvider(dim=3, model_name="core-embed")
    router = _KernelRouter(_embedding_route(provider_id="core-embed"))
    context = _Context(
        with_local_api=False,
        router=router,
        providers={"core-embed": provider},
    )
    embedder = _embedder(context)

    assert asyncio.run(embedder.embed_query("雨天")) == [2.0, 2.0, 2.0]
    assert router.kinds == ["embedding"]
    assert context.registered == ["astrbot_plugin_update_manager"]
    assert context.provider_lookups == ["core-embed"]
    assert provider.queries == ["雨天"]
    assert embedder.dim == 3
    assert embedder.model_name == "core-embed"
    assert embedder.available is True


def test_embed_batch_uses_kernel_provider():
    provider = _BatchEmbeddingProvider(dim=2)
    context = _Context(
        with_local_api=False,
        router=_KernelRouter(_embedding_route()),
        providers={"core-embed": provider},
    )
    embedder = _embedder(context)

    assert asyncio.run(embedder.embed_batch(["ab", "c"])) == [[2.0, 2.0], [1.0, 1.0]]
    assert provider.batches == [["ab", "c"]]
    assert provider.queries == ["ab", "c"]


def test_embed_batch_falls_back_to_single_embedding_on_kernel_provider():
    provider = _EmbeddingProvider(dim=2)  # 没有批量接口
    context = _Context(
        with_local_api=False,
        router=_KernelRouter(_embedding_route()),
        providers={"core-embed": provider},
    )

    assert asyncio.run(_embedder(context).embed_batch(["ab", "c"])) == [
        [2.0, 2.0],
        [1.0, 1.0],
    ]
    assert provider.queries == ["ab", "c"]


def test_local_provider_keeps_priority_over_kernel_route():
    """本地配置有效时保持现状：核路由连查都不查。"""
    local = _EmbeddingProvider(dim=2, model_name="local-embed")
    kernel = _EmbeddingProvider(dim=4, model_name="kernel-embed")
    router = _KernelRouter(_embedding_route())
    context = _Context(
        local_providers=[local], router=router, providers={"core-embed": kernel}
    )
    embedder = _embedder(context)

    assert asyncio.run(embedder.embed_query("hi")) == [2.0, 2.0]
    assert embedder.dim == 2
    assert embedder.model_name == "local-embed"
    assert router.kinds == []
    assert kernel.queries == []


def test_kernel_provider_is_cached_after_first_lookup():
    provider = _EmbeddingProvider(dim=2)
    router = _KernelRouter(_embedding_route())
    context = _Context(
        with_local_api=False, router=router, providers={"core-embed": provider}
    )
    embedder = _embedder(context)

    assert asyncio.run(embedder.embed_query("a")) == [1.0, 1.0]
    assert asyncio.run(embedder.embed_query("bb")) == [2.0, 2.0]
    # 核只查一次、实例只换一次，之后复用缓存
    assert router.kinds == ["embedding"]
    assert context.provider_lookups == ["core-embed"]
    assert provider.queries == ["a", "bb"]


def test_query_cache_still_applies_for_kernel_provider():
    provider = _EmbeddingProvider(dim=2)
    context = _Context(
        with_local_api=False,
        router=_KernelRouter(_embedding_route()),
        providers={"core-embed": provider},
    )
    embedder = _embedder(context)

    first = asyncio.run(embedder.embed_query("same"))
    second = asyncio.run(embedder.embed_query("same"))

    assert first == second == [4.0, 4.0]
    assert provider.queries == ["same"]


def test_kernel_route_is_re_probed_after_failure():
    """核可能晚于本插件加载（或热重载），失败不写死缓存，下次查询还能恢复。"""

    class _LateKernelContext:
        def __init__(self):
            self.router = None
            self.provider = _EmbeddingProvider(dim=2)

        def get_all_embedding_providers(self):
            return []

        def get_registered_star(self, _plugin_name):
            return _StarMetadata(star_cls=self.router)

        def get_provider_by_id(self, provider_id):
            if self.router is not None and provider_id == "core-embed":
                return self.provider
            return None

    context = _LateKernelContext()
    embedder = _embedder(context)

    assert asyncio.run(embedder.embed_query("a")) is None
    context.router = _KernelRouter(_embedding_route())
    assert asyncio.run(embedder.embed_query("a")) == [1.0, 1.0]
    assert embedder.available is True


def test_available_and_dim_stay_sync_usable():
    """只有核路由时同步属性不能 await：异步预热前后分别是 False / True。"""
    provider = _EmbeddingProvider(dim=3)
    context = _Context(
        with_local_api=False,
        router=_KernelRouter(_embedding_route()),
        providers={"core-embed": provider},
    )
    embedder = _embedder(context)

    assert embedder.available is False
    assert embedder.dim == 0

    asyncio.run(embedder.embed_query("xy"))

    assert embedder.available is True
    assert embedder.dim == 3
    assert isinstance(embedder.model_name, str)


# ---------- 核不可用 / 失败：静默回退现有逻辑 ----------


def test_kernel_unavailable_falls_back_to_fts5():
    context = _Context(with_local_api=False)
    embedder = _embedder(context)

    assert embedder.available is False
    assert asyncio.run(embedder.embed_query("x")) is None
    assert asyncio.run(embedder.embed_batch(["x"])) is None
    assert asyncio.run(embedder.embed_batch([])) == []
    assert embedder.dim == 0


def test_kernel_route_failures_all_fall_back_silently():
    class _WrongKindRouter(_KernelRouter):
        def resolve_model_route(self, kind, **_kwargs):
            self.kinds.append(kind)
            return {**self.route, "kind": "conversation"}

    cases = {
        "no metadata": {"with_local_api": False},
        "metadata is None": {"with_local_api": False, "registered_star": None},
        "registry raises": {
            "with_local_api": False,
            "registered_error": RuntimeError("registry broken"),
        },
        "incompatible contract": {
            "with_local_api": False,
            "router": _KernelRouter(
                _embedding_route(),
                {"name": "series.other@1.0", "version": "1.0", "read_only": True,
                 "capabilities": ("resolve",)},
            ),
        },
        "route is not core": {
            "with_local_api": False,
            "router": _KernelRouter(_embedding_route(source="astrbot")),
        },
        "route unavailable": {
            "with_local_api": False,
            "router": _KernelRouter(_embedding_route(available=False)),
        },
        "kind mismatch": {"with_local_api": False, "router": _WrongKindRouter()},
        "blank provider id": {
            "with_local_api": False,
            "router": _KernelRouter(_embedding_route(provider_id="")),
        },
        "provider not registered": {
            "with_local_api": False,
            "router": _KernelRouter(_embedding_route()),
            "providers": {},
        },
        "provider lookup raises": {
            "with_local_api": False,
            "router": _KernelRouter(_embedding_route()),
            "provider_lookup_error": RuntimeError("provider manager broken"),
        },
    }
    for label, kwargs in cases.items():
        embedder = _embedder(_Context(**kwargs))
        assert asyncio.run(embedder.embed_query("x")) is None, label
        assert asyncio.run(embedder.embed_batch(["x"])) is None, label
        assert embedder.available is False, label
        assert embedder.dim == 0, label


def test_kernel_provider_without_get_embedding_is_rejected():
    context = _Context(
        with_local_api=False,
        router=_KernelRouter(_embedding_route()),
        providers={"core-embed": SimpleNamespace()},
    )
    embedder = _embedder(context)

    assert asyncio.run(embedder.embed_query("x")) is None
    assert embedder.available is False


def test_kernel_embedding_failure_degrades_without_raising():
    provider = _EmbeddingProvider(dim=2, fail=True)
    context = _Context(
        with_local_api=False,
        router=_KernelRouter(_embedding_route()),
        providers={"core-embed": provider},
    )
    embedder = _embedder(context)

    assert asyncio.run(embedder.embed_query("x")) is None
    assert asyncio.run(embedder.embed_batch(["x"])) is None


def test_embedder_without_context_degrades():
    embedder = Embedder(SimpleNamespace())

    assert embedder.available is False
    assert asyncio.run(embedder.embed_query("x")) is None
    assert asyncio.run(embedder.embed_batch(["x"])) is None
