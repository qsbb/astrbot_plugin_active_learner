"""SQLite + FTS5 记忆存储层。

支持按用户/群聊双层隔离（scope_type + scope_id），
关键词全文检索（FTS5），以及记忆版本化（memory_versions 表）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .models import (
    SCOPE_GLOBAL,
    MemoryEntry,
    MemoryVersion,
    Scope,
    SearchHit,
    make_memory_id,
    now_ts,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  content TEXT NOT NULL,
  keywords TEXT DEFAULT '',
  source TEXT DEFAULT '',
  sources_detail TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.3,
  verified INTEGER DEFAULT 0,
  challenge_count INTEGER DEFAULT 0,
  access_count INTEGER DEFAULT 0,
  created_at REAL DEFAULT 0,
  updated_at REAL DEFAULT 0,
  last_challenged_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scope ON memories(scope_type, scope_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  topic, content, keywords,
  content='memories', content_rowid='rowid', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, topic, content, keywords)
  VALUES (new.rowid, new.topic, new.content, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, topic, content, keywords)
  VALUES ('delete', old.rowid, old.topic, old.content, old.keywords);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, topic, content, keywords)
  VALUES ('delete', old.rowid, old.topic, old.content, old.keywords);
  INSERT INTO memories_fts(rowid, topic, content, keywords)
  VALUES (new.rowid, new.topic, new.content, new.keywords);
END;

CREATE TABLE IF NOT EXISTS memory_versions (
  version_id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  content TEXT,
  confidence REAL,
  source TEXT,
  reason TEXT,
  created_at REAL,
  FOREIGN KEY(memory_id) REFERENCES memories(id)
);
CREATE INDEX IF NOT EXISTS idx_ver_mem ON memory_versions(memory_id);
"""

# 查询用列顺序（与 MemoryEntry.from_row 对齐，含 keywords）
SELECT_COLS = (
    "id, scope_type, scope_id, topic, content, "
    "source, sources_detail, keywords, confidence, verified, "
    "challenge_count, access_count, "
    "created_at, updated_at, last_challenged_at"
)


def _build_match_query(query: str) -> str:
    """构造 FTS5 MATCH 表达式，安全处理特殊字符。

    将查询拆分为 token，每个用双引号包裹（短语查询），
    用 OR 连接。FTS5 unicode61 对中文按字分词，可正常匹配。
    """
    cleaned = re.sub(r'["\'\-\*\(\):^\\/]+', ' ', query)
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return ""
    return ' OR '.join(f'"{t}"' for t in tokens)


class MemoryStore:
    """SQLite 记忆存储，线程安全（用 Lock 保护）。"""

    def __init__(self, db_path: Path, max_entries: int = 500, min_confidence: float = 0.3):
        self._db_path = db_path
        self._max_entries = max_entries
        self._min_confidence = min_confidence
        self._lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------- 写操作 ----------

    def add_or_update(
        self,
        scope: Scope,
        topic: str,
        content: str,
        keywords: Optional[list[str]] = None,
        source: str = "",
        sources_detail: Optional[list[str]] = None,
        confidence: float = 0.3,
    ) -> MemoryEntry:
        """添加或更新记忆。同 scope 同 topic 命中则合并。"""
        keywords = keywords or []
        sources_detail = sources_detail or []
        entry_id = make_memory_id(scope, topic)
        keywords_str = " ".join(keywords)
        sources_json = json.dumps(sources_detail, ensure_ascii=False)
        now = now_ts()

        with self._lock:
            row = self._conn.execute(
                "SELECT id, keywords, confidence, content FROM memories WHERE id = ?",
                (entry_id,),
            ).fetchone()

            if row:
                existing_keywords = (row["keywords"] or "").split()
                merged_keywords = list(dict.fromkeys(existing_keywords + keywords))
                merged_keywords_str = " ".join(merged_keywords)
                new_confidence = max(confidence, row["confidence"])
                self._conn.execute(
                    """UPDATE memories
                       SET content = ?, keywords = ?, source = ?, sources_detail = ?,
                           confidence = ?, updated_at = ?, access_count = access_count + 1
                       WHERE id = ?""",
                    (
                        content, merged_keywords_str, source, sources_json,
                        new_confidence, now, entry_id,
                    ),
                )
                return self.get_entry_by_id(entry_id)  # type: ignore[return-value]
            else:
                self._conn.execute(
                    """INSERT INTO memories
                       (id, scope_type, scope_id, topic, content, keywords,
                        source, sources_detail, confidence, verified,
                        challenge_count, access_count, created_at, updated_at, last_challenged_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?, ?, 0)""",
                    (
                        entry_id, scope.type, scope.id, topic, content, keywords_str,
                        source, sources_json, confidence, now, now,
                    ),
                )
                self._evict_if_needed_locked(scope)
                return self.get_entry_by_id(entry_id)  # type: ignore[return-value]

    def update_content(
        self,
        entry_id: str,
        content: str,
        confidence: float,
        source: str = "",
        verified: Optional[bool] = None,
        keywords: Optional[list[str]] = None,
        reason: str = "",
        snapshot: bool = True,
    ) -> bool:
        """更新记忆内容。snapshot=True 时先写一条版本快照。"""
        now = now_ts()
        with self._lock:
            row = self._conn.execute(
                "SELECT content, confidence, source, keywords FROM memories WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if not row:
                return False

            old_content = row["content"] or ""
            old_conf = float(row["confidence"] or 0.0)
            old_source = row["source"] or ""
            old_keywords_str = row["keywords"] or ""

            content_changed = abs(len(content) - len(old_content)) > 30 or content != old_content
            conf_drop = (old_conf - confidence) > 0.15

            if snapshot and (content_changed or conf_drop):
                self._save_version_locked(
                    entry_id, old_content, old_conf, old_source, reason or "update"
                )

            keywords_str = " ".join(keywords) if keywords else old_keywords_str
            if verified is not None:
                self._conn.execute(
                    """UPDATE memories SET content = ?, confidence = ?, source = ?,
                       keywords = ?, verified = ?, updated_at = ? WHERE id = ?""",
                    (content, confidence, source, keywords_str, 1 if verified else 0, now, entry_id),
                )
            else:
                self._conn.execute(
                    """UPDATE memories SET content = ?, confidence = ?, source = ?,
                       keywords = ?, updated_at = ? WHERE id = ?""",
                    (content, confidence, source, keywords_str, now, entry_id),
                )
            return True

    def inc_challenge(self, entry_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE memories
                   SET challenge_count = challenge_count + 1, last_challenged_at = ?
                   WHERE id = ?""",
                (now_ts(), entry_id),
            )

    def inc_access(self, entry_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                (entry_id,),
            )

    def set_verified(self, entry_id: str, verified: bool, confidence: Optional[float] = None) -> None:
        now = now_ts()
        with self._lock:
            if confidence is not None:
                self._conn.execute(
                    "UPDATE memories SET verified = ?, confidence = ?, updated_at = ? WHERE id = ?",
                    (1 if verified else 0, confidence, now, entry_id),
                )
            else:
                self._conn.execute(
                    "UPDATE memories SET verified = ?, updated_at = ? WHERE id = ?",
                    (1 if verified else 0, now, entry_id),
                )

    def forget(self, scope: Scope, topic: str) -> tuple[bool, Optional[MemoryEntry]]:
        """软删除：先写版本留痕，再 DELETE。返回 (是否删除, 被删除的 entry)。"""
        entry = self.search(scope, topic, top_k=1)
        if not entry:
            return False, None
        target = entry[0].entry
        with self._lock:
            self._save_version_locked(
                target.id, target.content, target.confidence, target.source, "manual_forget"
            )
            self._conn.execute("DELETE FROM memories WHERE id = ?", (target.id,))
            return True, target

    def _save_version_locked(self, entry_id: str, content: str, confidence: float, source: str, reason: str) -> None:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no FROM memory_versions WHERE memory_id = ?",
            (entry_id,),
        ).fetchone()
        next_no = row["next_no"] if row else 1
        self._conn.execute(
            """INSERT INTO memory_versions
               (memory_id, version_no, content, confidence, source, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (entry_id, next_no, content, confidence, source, reason, now_ts()),
        )

    def _evict_if_needed_locked(self, scope: Scope) -> int:
        """容量淘汰：仅限当前 scope。返回淘汰数量。"""
        if self._max_entries <= 0:
            return 0
        cur = self._conn.execute(
            "SELECT COUNT(*) AS c FROM memories WHERE scope_type = ? AND scope_id = ?",
            (scope.type, scope.id),
        ).fetchone()
        total = cur["c"] if cur else 0
        if total <= self._max_entries:
            return 0
        to_remove = total - self._max_entries
        rows = self._conn.execute(
            """SELECT id FROM memories
               WHERE scope_type = ? AND scope_id = ?
               ORDER BY (confidence * 0.6 + (access_count / 100.0) * 0.4) ASC
               LIMIT ?""",
            (scope.type, scope.id, to_remove),
        ).fetchall()
        for r in rows:
            self._conn.execute("DELETE FROM memories WHERE id = ?", (r["id"],))
        return to_remove

    # ---------- 读操作 ----------

    def get_entry_by_id(self, entry_id: str) -> Optional[MemoryEntry]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {SELECT_COLS} FROM memories WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if not row:
                return None
            return MemoryEntry.from_row(row)

    def search(self, scope: Scope, query: str, top_k: int = 3) -> list[SearchHit]:
        """FTS5 全文检索 + scope 过滤 + 置信度加权排序。"""
        match_expr = _build_match_query(query)
        if not match_expr:
            return []

        # 给 SELECT_COLS 每列加 m. 前缀
        m_cols = ", ".join(f"m.{c.strip()}" for c in SELECT_COLS.split(","))

        with self._lock:
            try:
                rows = self._conn.execute(
                    f"""SELECT {m_cols}
                        FROM memories_fts f
                        JOIN memories m ON m.rowid = f.rowid
                        WHERE memories_fts MATCH ?
                          AND ((m.scope_type = ? AND m.scope_id = ?) OR m.scope_type = ?)
                        LIMIT ?""",
                    (match_expr, scope.type, scope.id, SCOPE_GLOBAL, top_k * 3),
                ).fetchall()
            except sqlite3.OperationalError:
                return []

            if not rows:
                return []

            hits: list[SearchHit] = []
            for r in rows:
                entry = MemoryEntry.from_row(r)
                # 评分：置信度为主，访问次数微调
                score = entry.confidence + (min(entry.access_count, 50) / 50.0) * 0.1
                hits.append(SearchHit(entry=entry, score=score))

        hits.sort(key=lambda h: h.score, reverse=True)
        # 命中后增加 access_count
        for h in hits[:top_k]:
            self.inc_access(h.entry.id)
        return hits[:top_k]

    def search_by_topic(self, scope: Scope, topic: str) -> Optional[MemoryEntry]:
        """按 topic 精确查找（同 scope）。"""
        entry_id = make_memory_id(scope, topic)
        return self.get_entry_by_id(entry_id)

    def list_memories(self, scope: Scope, page: int = 1, per_page: int = 10) -> tuple[list[MemoryEntry], int, int]:
        """分页列出当前 scope 记忆。返回 (entries, total, total_pages)。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE scope_type = ? AND scope_id = ?",
                (scope.type, scope.id),
            ).fetchone()
            total = cur["c"] if cur else 0
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = max(1, min(page, total_pages))
            offset = (page - 1) * per_page
            rows = self._conn.execute(
                f"""SELECT {SELECT_COLS} FROM memories
                    WHERE scope_type = ? AND scope_id = ?
                    ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (scope.type, scope.id, per_page, offset),
            ).fetchall()
            entries = []
            for r in rows:
                entries.append(MemoryEntry.from_row(r))
            return entries, total, total_pages

    def list_versions(self, entry_id: str) -> list[MemoryVersion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_versions WHERE memory_id = ? ORDER BY version_no ASC",
                (entry_id,),
            ).fetchall()
            return [
                MemoryVersion(
                    version_id=r["version_id"],
                    memory_id=r["memory_id"],
                    version_no=r["version_no"],
                    content=r["content"] or "",
                    confidence=float(r["confidence"] or 0.0),
                    source=r["source"] or "",
                    reason=r["reason"] or "",
                    created_at=float(r["created_at"] or 0.0),
                )
                for r in rows
            ]

    def stats(self, scope: Scope) -> dict:
        with self._lock:
            cur = self._conn.execute(
                """SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) AS verified,
                    SUM(CASE WHEN challenge_count > 0 THEN 1 ELSE 0 END) AS challenged,
                    AVG(confidence) AS avg_conf,
                    MAX(access_count) AS max_access
                   FROM memories
                   WHERE scope_type = ? AND scope_id = ?""",
                (scope.type, scope.id),
            ).fetchone()
            if not cur or cur["total"] == 0:
                return {
                    "total": 0, "verified": 0, "challenged": 0,
                    "avg_confidence": 0.0, "most_accessed": None,
                    "scope_type": scope.type, "scope_id": scope.id,
                }
            most_row = self._conn.execute(
                """SELECT topic FROM memories
                   WHERE scope_type = ? AND scope_id = ?
                   ORDER BY access_count DESC LIMIT 1""",
                (scope.type, scope.id),
            ).fetchone()
            return {
                "total": cur["total"],
                "verified": cur["verified"] or 0,
                "challenged": cur["challenged"] or 0,
                "avg_confidence": float(cur["avg_conf"] or 0.0),
                "most_accessed": most_row["topic"] if most_row else None,
                "scope_type": scope.type,
                "scope_id": scope.id,
            }

    def export_scope(self, scope: Scope) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT {SELECT_COLS} FROM memories
                    WHERE scope_type = ? AND scope_id = ?
                    ORDER BY updated_at DESC""",
                (scope.type, scope.id),
            ).fetchall()
            result = []
            for r in rows:
                entry = MemoryEntry.from_row(r)
                result.append(entry.to_dict())
            return result

    def update_config(self, max_entries: Optional[int] = None, min_confidence: Optional[float] = None) -> None:
        if max_entries is not None:
            self._max_entries = max_entries
        if min_confidence is not None:
            self._min_confidence = min_confidence

    # ---------- Dashboard 用：跨 scope 视图 ----------

    def list_scopes(self) -> list[dict]:
        """列出所有非空 (scope_type, scope_id) 组合，按记忆数降序。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT scope_type, scope_id, COUNT(*) AS count
                   FROM memories
                   GROUP BY scope_type, scope_id
                   ORDER BY count DESC, scope_type ASC, scope_id ASC""",
            ).fetchall()
            return [
                {"scope_type": r["scope_type"], "scope_id": r["scope_id"], "count": r["count"]}
                for r in rows
            ]

    def global_stats(self) -> dict:
        """跨所有 scope 的汇总统计。"""
        with self._lock:
            cur = self._conn.execute(
                """SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) AS verified,
                    SUM(CASE WHEN challenge_count > 0 THEN 1 ELSE 0 END) AS challenged,
                    SUM(challenge_count) AS challenged_total,
                    AVG(confidence) AS avg_conf,
                    SUM(access_count) AS access_total
                   FROM memories""",
            ).fetchone()
            if not cur or cur["total"] == 0:
                return {
                    "total": 0, "verified": 0, "challenged": 0,
                    "challenged_total": 0, "avg_confidence": 0.0,
                    "access_total": 0, "scope_type": None, "scope_id": None,
                }
            return {
                "total": cur["total"],
                "verified": cur["verified"] or 0,
                "challenged": cur["challenged"] or 0,
                "challenged_total": cur["challenged_total"] or 0,
                "avg_confidence": float(cur["avg_conf"] or 0.0),
                "access_total": cur["access_total"] or 0,
                "scope_type": None,
                "scope_id": None,
            }

    def list_all_memories(
        self,
        page: int = 1,
        per_page: int = 20,
        keyword: Optional[str] = None,
    ) -> tuple[list[MemoryEntry], int, int]:
        """跨所有 scope 分页列出记忆。返回 (entries, total, total_pages)。

        keyword 非空时按 topic/content LIKE 过滤。
        """
        with self._lock:
            if keyword:
                like = f"%{keyword}%"
                count_row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM memories WHERE topic LIKE ? OR content LIKE ?",
                    (like, like),
                ).fetchone()
                total = count_row["c"] if count_row else 0
                total_pages = max(1, (total + per_page - 1) // per_page)
                page = max(1, min(page, total_pages))
                offset = (page - 1) * per_page
                rows = self._conn.execute(
                    f"""SELECT {SELECT_COLS} FROM memories
                        WHERE topic LIKE ? OR content LIKE ?
                        ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                    (like, like, per_page, offset),
                ).fetchall()
            else:
                count_row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM memories",
                ).fetchone()
                total = count_row["c"] if count_row else 0
                total_pages = max(1, (total + per_page - 1) // per_page)
                page = max(1, min(page, total_pages))
                offset = (page - 1) * per_page
                rows = self._conn.execute(
                    f"""SELECT {SELECT_COLS} FROM memories
                        ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                    (per_page, offset),
                ).fetchall()
            return [MemoryEntry.from_row(r) for r in rows], total, total_pages
