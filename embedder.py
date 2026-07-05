"""向量嵌入层：封装 AstrBot EmbeddingProvider，提供查询/批量嵌入 + 内存缓存。

设计要点：
1. 自动取第一个可用的 EmbeddingProvider（零配置）
2. 单条查询带 LRU 缓存（避免重复嵌入相同查询）
3. scope → (numpy 矩阵, memory_id 列表) 内存缓存，写时失效
4. 无 provider 时返回 None，调用方降级为纯 FTS5
5. 查询嵌入必须在 storage._lock 外完成（避免阻塞写入）
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Optional

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    np = None  # type: ignore

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("active_learner")


# 单条查询缓存上限
_QUERY_CACHE_SIZE = 256
# 单次批量嵌入上限（避免 API 限流）
_BATCH_BUDGET = 256


class Embedder:
    """向量嵌入封装。线程不安全（调用方需自行加锁管理 matrix_cache）。"""

    def __init__(self, plugin):
        self._plugin = plugin
        self._provider: Any = None
        self._provider_checked: bool = False
        self._dim: int = 0
        self._model_name: str = ""
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        # scope_key → (vectors ndarray shape=(n, dim), ids list[str])
        self._matrix_cache: dict[str, tuple[Any, list[str]]] = {}

    def _resolve_provider(self) -> Any:
        """取第一个可用的 embedding provider。无则返回 None（降级 FTS5）。"""
        if self._provider_checked:
            return self._provider
        self._provider_checked = True
        try:
            method = getattr(self._plugin.context, "get_all_embedding_providers", None)
            if not callable(method):
                return None
            providers = method() or []
            if not providers:
                logger.info("未配置 Embedding Provider，向量检索将降级为纯 FTS5")
                return None
            self._provider = providers[0]
            try:
                self._dim = int(self._provider.get_dim())
            except Exception:
                self._dim = 0
            try:
                self._model_name = str(
                    getattr(self._provider, "model_name", "")
                    or getattr(self._provider, "id", "")
                    or "unknown"
                )
            except Exception:
                self._model_name = "unknown"
            logger.info(
                f"Embedder 已就绪: provider={self._model_name}, dim={self._dim}"
            )
            return self._provider
        except Exception as e:
            logger.warning(f"解析 Embedding Provider 失败，降级为 FTS5: {e}")
            return None

    @property
    def available(self) -> bool:
        """是否可用（已配置 embedding provider）。"""
        return self._resolve_provider() is not None

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    async def embed_query(self, text: str) -> Optional[list[float]]:
        """单条查询向量，带 LRU 缓存。失败返回 None（降级 FTS5）。"""
        if not text:
            return None
        provider = self._resolve_provider()
        if provider is None:
            return None

        # 命中缓存
        if text in self._query_cache:
            self._query_cache.move_to_end(text)
            return self._query_cache[text]

        try:
            vec = await provider.get_embedding(text)
            if not vec:
                return None
            # 写入缓存
            self._query_cache[text] = vec
            if len(self._query_cache) > _QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)
            return vec
        except Exception as e:
            logger.warning(f"查询嵌入失败: {e}")
            return None

    async def embed_batch(self, texts: list[str]) -> Optional[list[list[float]]]:
        """批量嵌入，带 budget 限制。失败返回 None。"""
        if not texts:
            return []
        provider = self._resolve_provider()
        if provider is None:
            return None

        # 超过 budget 分批处理
        if len(texts) > _BATCH_BUDGET:
            results: list[list[float]] = []
            for i in range(0, len(texts), _BATCH_BUDGET):
                batch = texts[i : i + _BATCH_BUDGET]
                vecs = await self._embed_batch_inner(provider, batch)
                if vecs is None:
                    return None
                results.extend(vecs)
            return results
        return await self._embed_batch_inner(provider, texts)

    async def _embed_batch_inner(self, provider: Any, texts: list[str]) -> Optional[list[list[float]]]:
        """内部批量嵌入，优先用 get_embeddings_batch（带 retry）。"""
        try:
            method = getattr(provider, "get_embeddings_batch", None)
            if callable(method):
                return await method(texts)
            method = getattr(provider, "get_embeddings", None)
            if callable(method):
                return await method(texts)
            # 兜底：逐条嵌入
            results = []
            for t in texts:
                v = await provider.get_embedding(t)
                if v:
                    results.append(v)
            return results if results else None
        except Exception as e:
            logger.warning(f"批量嵌入失败: {e}")
            return None

    def invalidate_matrix_cache(self, scope_key: Optional[str] = None) -> None:
        """写入时调用：失效整个 scope 的向量矩阵缓存。scope_key=None 失效所有。"""
        if scope_key is None:
            self._matrix_cache.clear()
        else:
            self._matrix_cache.pop(scope_key, None)

    def clear_query_cache(self) -> None:
        """清空查询缓存（embedding model 切换时调用）。"""
        self._query_cache.clear()


def cosine_similarity_batch(query_vec: list[float], matrix: Any) -> Optional[Any]:
    """批量计算余弦相似度。

    Args:
        query_vec: 查询向量，shape=(dim,)
        matrix: numpy ndarray shape=(n, dim)
    Returns:
        numpy array shape=(n,) 的相似度分数，或 None（降级）
    """
    if not _NUMPY_AVAILABLE or matrix is None or len(matrix) == 0:
        return None
    try:
        q = np.asarray(query_vec, dtype=np.float32)
        if q.ndim == 1:
            q = q.reshape(1, -1)
        # 归一化
        q_norm = np.linalg.norm(q, axis=1, keepdims=True)
        q_norm = np.where(q_norm == 0, 1, q_norm)
        q_unit = q / q_norm

        m_norm = np.linalg.norm(matrix, axis=1, keepdims=True)
        m_norm = np.where(m_norm == 0, 1, m_norm)
        m_unit = matrix / m_norm

        # cosine = q_unit @ m_unit.T
        scores = (q_unit @ m_unit.T).flatten()
        return scores
    except Exception as e:
        logger.warning(f"余弦相似度计算失败: {e}")
        return None


def normalize_scores(scores: list[float]) -> list[float]:
    """min-max 归一化到 [0, 1]。空列表或全相同值返回全 0.5。"""
    if not scores:
        return []
    mn = min(scores)
    mx = max(scores)
    if mx - mn < 1e-9:
        return [0.5] * len(scores)
    return [(s - mn) / (mx - mn) for s in scores]
