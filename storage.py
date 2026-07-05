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
from typing import Any, Optional

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

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at REAL
);

CREATE TABLE IF NOT EXISTS memories_embedding (
  memory_id TEXT PRIMARY KEY,
  embedding BLOB,
  dim INTEGER,
  model TEXT,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_embed_mem ON memories_embedding(memory_id);
CREATE INDEX IF NOT EXISTS idx_embed_scope ON memories_embedding(memory_id);
"""

# 查询用列顺序（与 MemoryEntry.from_row 对齐，含 keywords + v2.4 新字段）
SELECT_COLS = (
    "id, scope_type, scope_id, topic, content, "
    "source, sources_detail, keywords, confidence, verified, "
    "challenge_count, access_count, "
    "created_at, updated_at, last_challenged_at, "
    "parent_doc_id, last_accessed_at"
)


def _column_exists(conn, table: str, column: str) -> bool:
    """检查表中是否存在某列。"""
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column for row in cur.fetchall())
    except Exception:
        return False


def _migrate_schema(conn) -> int:
    """执行版本化迁移。返回当前 schema 版本。"""
    import time as _time
    # 检查 schema_version 表是否有记录。MAX(version) 在空表上返回 NULL，需显式处理。
    try:
        cur = conn.execute("SELECT MAX(version) AS v FROM schema_version")
        row = cur.fetchone()
        val = row[0] if row else None
        current = val if val is not None else 0
    except Exception:
        current = 0

    if current < 1:
        # v1: 加 parent_doc_id 和 last_accessed_at 列
        if not _column_exists(conn, "memories", "parent_doc_id"):
            conn.execute("ALTER TABLE memories ADD COLUMN parent_doc_id TEXT")
        if not _column_exists(conn, "memories", "last_accessed_at"):
            conn.execute("ALTER TABLE memories ADD COLUMN last_accessed_at REAL DEFAULT 0")
        # 回填 last_accessed_at = created_at
        conn.execute("UPDATE memories SET last_accessed_at = created_at WHERE last_accessed_at = 0 OR last_accessed_at IS NULL")
        conn.execute("INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (1, ?)", (_time.time(),))
        current = 1
    return current


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
        self._schema_version = _migrate_schema(self._conn)

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
        """FTS5 全文检索 + scope 过滤 + 置信度加权排序。

        v2.4.0 起推荐用 search_hybrid 替代（带向量检索）。
        本方法保留作为降级路径和无 embedding provider 时的兜底。
        """
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

    # ---------- v2.4.0 向量检索 ----------

    def _search_fts(
        self,
        scope: Scope,
        query: str,
        limit: int,
    ) -> list[tuple[MemoryEntry, float]]:
        """FTS5 检索 + bm25 分数。返回 [(entry, bm25_score), ...]。

        bm25 分数越小越好（FTS5 默认），调用方需取负转为"越大越好"。
        """
        match_expr = _build_match_query(query)
        if not match_expr:
            return []
        m_cols = ", ".join(f"m.{c.strip()}" for c in SELECT_COLS.split(","))
        try:
            rows = self._conn.execute(
                f"""SELECT {m_cols}, bm25(memories_fts) AS bm25
                    FROM memories_fts f
                    JOIN memories m ON m.rowid = f.rowid
                    WHERE memories_fts MATCH ?
                      AND ((m.scope_type = ? AND m.scope_id = ?) OR m.scope_type = ?)
                    LIMIT ?""",
                (match_expr, scope.type, scope.id, SCOPE_GLOBAL, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        results = []
        for r in rows:
            entry = MemoryEntry.from_row(r)
            # bm25 越小越好，取负转为越大越好
            bm25_score = -float(r["bm25"]) if r["bm25"] is not None else 0.0
            results.append((entry, bm25_score))
        return results

    def _load_scope_vectors(
        self,
        scope: Scope,
        embedder,
    ) -> tuple[Any, list[str]]:
        """加载 scope 的所有向量到 numpy 矩阵。带内存缓存。

        必须在 self._lock 内调用。
        """
        import numpy as np
        scope_key = f"{scope.type}:{scope.id}"
        if scope_key in embedder._matrix_cache:
            return embedder._matrix_cache[scope_key]
        rows = self._conn.execute(
            """SELECT e.memory_id, e.embedding, e.dim
               FROM memories_embedding e
               JOIN memories m ON m.id = e.memory_id
               WHERE m.scope_type = ? AND m.scope_id = ?
               ORDER BY e.memory_id""",
            (scope.type, scope.id),
        ).fetchall()
        if not rows:
            result = (np.zeros((0, embedder.dim), dtype=np.float32), [])
            embedder._matrix_cache[scope_key] = result
            return result
        ids = []
        vecs = []
        for r in rows:
            try:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                if embedder.dim > 0 and len(vec) != embedder.dim:
                    continue  # 维度不匹配，跳过
                ids.append(r["memory_id"])
                vecs.append(vec)
            except Exception:
                continue
        if not vecs:
            matrix = np.zeros((0, embedder.dim), dtype=np.float32)
        else:
            matrix = np.vstack(vecs).astype(np.float32)
        result = (matrix, ids)
        embedder._matrix_cache[scope_key] = result
        return result

    def search_by_vector(
        self,
        scope: Scope,
        query_vec: list[float],
        top_k: int,
        embedder,
    ) -> list[tuple[str, float]]:
        """numpy 余弦相似度检索。返回 [(memory_id, similarity), ...]。

        必须在 self._lock 内调用；query_vec 必须在锁外计算。
        """
        from .embedder import cosine_similarity_batch
        matrix, ids = self._load_scope_vectors(scope, embedder)
        if len(ids) == 0:
            return []
        scores = cosine_similarity_batch(query_vec, matrix)
        if scores is None:
            return []
        ranked = sorted(zip(ids, scores.tolist()), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    def search_hybrid(
        self,
        scope: Scope,
        query: str,
        top_k: int = 5,
        embedder=None,
        fts_weight: float = 0.4,
        vec_weight: float = 0.6,
        enable_scope_fallback: bool = True,
        decay_half_life_days: float = 30.0,
        query_vec: Optional[list[float]] = None,
        priority_topics: Optional[list[str]] = None,
        priority_boost: float = 1.3,
    ) -> list[SearchHit]:
        """混合检索：FTS5 + 向量 + scope 回退 + 衰减分数。

        query_vec 必须在锁外计算后传入（避免阻塞写入）。
        无 query_vec 时降级为纯 FTS5。
        priority_topics：关心的领域列表（小写），命中 topic/keywords 的记忆按 priority_boost 加权。
        priority_boost：关心领域命中时的分数乘子，1.0 等于关闭。调用方可动态衰减以实现
                       “连续非关心查询后逐步淡化优先级”的效果。
        """
        import time as _time
        from .embedder import normalize_scores
        from .models import SCOPE_GROUP, SCOPE_PRIVATE

        now = _time.time()

        with self._lock:
            # 1. FTS5 检索（当前 scope + global）
            fts_results = self._search_fts(scope, query, top_k * 5)

            # 2. 向量检索（如有 query_vec 和 embedder）
            vec_results: list[tuple[str, float]] = []
            if query_vec is not None and embedder is not None and embedder.available:
                vec_results = self.search_by_vector(scope, query_vec, top_k * 5, embedder)
                # 也检索 global scope
                if scope.type != SCOPE_GLOBAL:
                    global_vec_results = self.search_by_vector(
                        Scope(SCOPE_GLOBAL, SCOPE_GLOBAL), query_vec, top_k * 5, embedder
                    )
                    vec_results.extend(global_vec_results)

            # 3. 合并 FTS 和向量结果（按 memory_id 去重）
            all_ids: set[str] = set()
            fts_map: dict[str, float] = {}
            for entry, score in fts_results:
                all_ids.add(entry.id)
                fts_map[entry.id] = score
            vec_map: dict[str, float] = {}
            for mid, score in vec_results:
                all_ids.add(mid)
                vec_map[mid] = max(vec_map.get(mid, 0.0), score)  # 取较大值去重

            if not all_ids:
                return []

            # 4. 拉取所有 entry 详情
            entries: dict[str, MemoryEntry] = {}
            scope_map: dict[str, str] = {}  # id → "private"|"group"|"global"
            for mid in all_ids:
                row = self._conn.execute(
                    f"SELECT {SELECT_COLS} FROM memories WHERE id = ?",
                    (mid,),
                ).fetchone()
                if row:
                    entry = MemoryEntry.from_row(row)
                    entries[mid] = entry
                    # 判断 scope 来源（用于 penalty）
                    if entry.scope_type == SCOPE_GLOBAL:
                        scope_map[mid] = "global"
                    elif entry.scope_type == scope.type and entry.scope_id == scope.id:
                        scope_map[mid] = "current"
                    else:
                        scope_map[mid] = "other"

            # 5. 分数归一化 + 加权 + scope penalty + decay
            fts_scores = [fts_map.get(mid, 0.0) for mid in all_ids]
            vec_scores = [vec_map.get(mid, 0.0) for mid in all_ids]
            fts_norm = normalize_scores(fts_scores) if fts_scores else [0.5] * len(all_ids)
            vec_norm = normalize_scores(vec_scores) if vec_scores else [0.0] * len(all_ids)

            hits: list[SearchHit] = []
            for i, mid in enumerate(all_ids):
                if mid not in entries:
                    continue
                entry = entries[mid]
                # 加权混合分数
                hybrid = fts_weight * fts_norm[i] + vec_weight * vec_norm[i]
                # scope penalty
                scope_tag = scope_map.get(mid, "other")
                if scope_tag == "current":
                    penalty = 1.0
                elif scope_tag == "global":
                    penalty = 0.6 if enable_scope_fallback else 0.0
                else:
                    # other scope（如 group 但当前是 private）—— 仅 fallback 启用时保留
                    penalty = 0.8 if enable_scope_fallback else 0.0
                # decay score: confidence * 0.5^(days / half_life)
                last_access = entry.last_accessed_at or entry.created_at
                days = max(0.0, (now - last_access) / 86400.0)
                decay = 0.5 ** (days / max(1.0, decay_half_life_days))
                final_score = (hybrid * penalty) * (entry.confidence * decay if entry.confidence > 0 else decay)
                # 关心领域加权：topic 或 keywords 命中任一优先话题则按 priority_boost 加权
                if priority_topics and priority_boost > 1.0:
                    topic_lower = (entry.topic or "").lower()
                    kws = entry.keywords or []
                    text_to_check = topic_lower + " " + " ".join(k.lower() for k in kws)
                    if any(pt in text_to_check for pt in priority_topics):
                        final_score *= priority_boost
                hits.append(SearchHit(entry=entry, score=final_score))

            hits.sort(key=lambda h: h.score, reverse=True)
            top_hits = hits[:top_k]

        # 命中后增加 access_count + 更新 last_accessed_at（锁外，避免长锁）
        for h in top_hits:
            self.inc_access(h.entry.id)
            self.update_last_accessed(h.entry.id, now)
        return top_hits

    def update_last_accessed(self, entry_id: str, ts: float = None) -> None:
        """更新 last_accessed_at 字段。"""
        from .models import now_ts
        if ts is None:
            ts = now_ts()
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_accessed_at = ? WHERE id = ?",
                (ts, entry_id),
            )

    def get_entries_by_ids(self, entry_ids: list[str]) -> list[MemoryEntry]:
        """按 ID 批量查询 entry（引用 footer 用）。"""
        if not entry_ids:
            return []
        with self._lock:
            placeholders = ",".join("?" * len(entry_ids))
            rows = self._conn.execute(
                f"SELECT {SELECT_COLS} FROM memories WHERE id IN ({placeholders})",
                entry_ids,
            ).fetchall()
            return [MemoryEntry.from_row(r) for r in rows]

    def save_embedding(
        self,
        memory_id: str,
        vec: list[float],
        dim: int,
        model: str,
    ) -> None:
        """保存向量到 memories_embedding 表。"""
        import struct
        from .models import now_ts
        # 转 BLOB：float32 数组
        try:
            import numpy as np
            arr = np.asarray(vec, dtype=np.float32)
            blob = arr.tobytes()
        except ImportError:
            # numpy 不可用时退化为 struct.pack
            blob = b"".join(struct.pack("<f", float(v)) for v in vec)
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO memories_embedding
                   (memory_id, embedding, dim, model, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (memory_id, blob, dim, model, now_ts()),
            )

    def get_embedding_for_memory(self, memory_id: str) -> Optional[tuple[bytes, int, str]]:
        """读取单条记忆的向量。返回 (blob, dim, model) 或 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT embedding, dim, model FROM memories_embedding WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if not row:
                return None
            return (row["embedding"], int(row["dim"] or 0), row["model"] or "")

    def add_chunk(
        self,
        chunk_id: str,
        scope: Scope,
        topic: str,
        content: str,
        keywords: Optional[list[str]] = None,
        source: str = "",
        confidence: float = 0.5,
        parent_doc_id: str = "",
        now: float = 0.0,
    ) -> MemoryEntry:
        """直接插入 chunk 条目（用调用方提供的 chunk_id，不走 add_or_update 的 topic 哈希）。

        用于文档分块入库：每个 chunk 共享 parent_doc_id，用 make_chunk_id 生成独立 ID。
        """
        keywords = keywords or []
        keywords_str = " ".join(keywords)
        sources_json = "[]"
        if not now:
            now = now_ts()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, scope_type, scope_id, topic, content, keywords,
                    source, sources_detail, confidence, verified,
                    challenge_count, access_count, created_at, updated_at, last_challenged_at,
                    parent_doc_id, last_accessed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 0, ?, ?)""",
                (chunk_id, scope.type, scope.id, topic, content, keywords_str,
                 source, sources_json, confidence, now, now, parent_doc_id, now),
            )
            return self.get_entry_by_id(chunk_id)  # type: ignore[return-value]

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

    def count_all(self) -> int:
        """返回所有 scope 的记忆总数。"""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
            return row["c"] if row else 0

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
