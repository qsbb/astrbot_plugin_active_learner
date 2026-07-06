"""数据模型定义。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional


SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"
SCOPE_GLOBAL = "global"


@dataclass(frozen=True)
class Scope:
    """记忆作用域。

    - private: scope_id = user_id（私聊隔离）
    - group:   scope_id = group_id（群内共享）
    - global:  scope_id = 'global'（所有人共享，仅管理员可写）
    """

    type: str
    id: str

    @staticmethod
    def from_event(event) -> "Scope":
        """从 AstrMessageEvent 推算 scope。

        注意：AstrBot 私聊时 message_obj.group_id 为空字符串 ""，不是 None。
        """
        gid = ""
        try:
            gid = event.message_obj.group_id or ""
        except Exception:
            gid = ""
        if gid:
            return Scope(SCOPE_GROUP, str(gid))
        try:
            uid = event.get_sender_id()
        except Exception:
            uid = "unknown"
        return Scope(SCOPE_PRIVATE, str(uid))

    @property
    def is_global(self) -> bool:
        return self.type == SCOPE_GLOBAL

    def __str__(self) -> str:
        return f"{self.type}:{self.id}"


@dataclass
class MemoryEntry:
    """单条知识记忆。"""

    id: str
    scope_type: str
    scope_id: str
    topic: str
    content: str
    keywords: list[str] = field(default_factory=list)
    source: str = ""
    sources_detail: list[str] = field(default_factory=list)
    origin: str = ""
    confidence: float = 0.3
    verified: bool = False
    challenge_count: int = 0
    access_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_challenged_at: float = 0.0
    # v1.1.2.0 新增字段
    parent_doc_id: Optional[str] = None
    last_accessed_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "topic": self.topic,
            "content": self.content,
            "keywords": self.keywords,
            "source": self.source,
            "sources_detail": self.sources_detail,
            "origin": self.origin,
            "confidence": self.confidence,
            "verified": self.verified,
            "challenge_count": self.challenge_count,
            "access_count": self.access_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_challenged_at": self.last_challenged_at,
            "parent_doc_id": self.parent_doc_id,
            "last_accessed_at": self.last_accessed_at,
        }

    @classmethod
    def from_row(cls, row) -> "MemoryEntry":
        """从 sqlite3.Row 或 dict 构造。"""
        def get(key, default=None):
            if isinstance(row, dict):
                return row.get(key, default)
            try:
                return row[key]
            except (KeyError, IndexError):
                return default
        try:
            sources_detail = json.loads(get("sources_detail") or "[]")
        except Exception:
            sources_detail = []
        keywords_str = get("keywords") or ""
        keywords = keywords_str.split() if keywords_str else []
        return cls(
            id=get("id"),
            scope_type=get("scope_type"),
            scope_id=get("scope_id"),
            topic=get("topic"),
            content=get("content"),
            keywords=keywords,
            source=get("source") or "",
            sources_detail=sources_detail,
            origin=get("origin") or "",
            confidence=float(get("confidence") or 0.0),
            verified=bool(get("verified")),
            challenge_count=int(get("challenge_count") or 0),
            access_count=int(get("access_count") or 0),
            created_at=float(get("created_at") or 0.0),
            updated_at=float(get("updated_at") or 0.0),
            last_challenged_at=float(get("last_challenged_at") or 0.0),
            parent_doc_id=get("parent_doc_id"),
            last_accessed_at=float(get("last_accessed_at") or 0.0),
        )


@dataclass
class MemoryVersion:
    """记忆历史版本（质疑纠错或验证失败时留痕）。"""

    version_id: int
    memory_id: str
    version_no: int
    content: str
    confidence: float
    source: str
    reason: str
    created_at: float

    def to_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "memory_id": self.memory_id,
            "version_no": self.version_no,
            "content": self.content,
            "confidence": self.confidence,
            "source": self.source,
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass
class SearchHit:
    """搜索结果（带评分）。"""

    entry: MemoryEntry
    score: float

    @property
    def topic(self) -> str:
        return self.entry.topic

    @property
    def content(self) -> str:
        return self.entry.content

    @property
    def verified(self) -> bool:
        return self.entry.verified

    @property
    def confidence(self) -> float:
        return self.entry.confidence


def make_memory_id(scope: Scope, topic: str) -> str:
    """生成记忆 ID：scope_type|scope_id|topic 的 sha1 截断。

    保证不同 scope 下同名 topic 不冲突。
    """
    import hashlib
    raw = f"{scope.type}|{scope.id}|{topic.lower().strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def make_chunk_id(scope: Scope, parent_doc_id: str, chunk_idx: int) -> str:
    """生成 chunk 专用 ID。

    与 make_memory_id 隔离，避免同一文档的多个 chunk 因共享 topic 而折叠成一行。
    """
    import hashlib
    raw = f"chunk|{scope.type}|{scope.id}|{parent_doc_id}|{chunk_idx}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def now_ts() -> float:
    return time.time()
