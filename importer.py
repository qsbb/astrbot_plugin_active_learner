"""文档导入器。

集中管理所有导入格式：纯文本 / Markdown / PDF / DOCX / TXT / ZIP / 内置知识库。
从 main.py 中分离，降低主文件体积（约 650 行）。
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("active_learner")

from .chunker import chunk_docx, chunk_markdown, chunk_pdf, chunk_text
from .models import Scope, make_chunk_id


class Importer:
    """文档导入器。依赖 plugin 的存储/精炼/嵌入能力。"""

    def __init__(self, plugin):
        self._plugin = plugin

    # ========== 公开 API 入口 ==========

    async def import_text(self, payload: dict) -> dict:
        """导入纯文本。返回 {ok, entry} 或 {ok, error, status_code}。"""
        topic = (payload.get("topic") or "").strip()
        content = payload.get("content") or ""
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        keywords = payload.get("keywords") or None
        refine = bool(payload.get("refine", True)) and bool(
            self._plugin._settings.get("refine_on_import", True)
        )
        if not topic or not content or not scope_type:
            return {"ok": False, "error": "topic, content, scope_type required", "status_code": 400}

        scope = Scope(type=scope_type, id=scope_id)
        try:
            final_content = content
            final_keywords = keywords
            final_confidence = self._plugin._default_confidence
            source_tag = "手动导入"
            if refine:
                provider_id = await self._plugin._resolve_plugin_provider_id()
                result = await self._plugin.refiner.refine_import(topic, content, provider_id)
                final_content = result.summary
                final_keywords = result.keywords or keywords
                final_confidence = result.confidence
                source_tag = "手动导入+精炼" if result.refined else "手动导入+未精炼"
                if not result.refined:
                    logger.warning(f"导入「{topic}」精炼降级为原内容")
            entry = self._plugin.store.add_or_update(
                scope=scope, topic=topic, content=final_content,
                keywords=final_keywords, source=source_tag,
                sources_detail=None, confidence=final_confidence,
            )
        except Exception as e:
            return {"ok": False, "error": f"导入失败: {e}", "status_code": 500}

        logger.info(f"导入文本: {topic} (scope: {scope}, source: {source_tag})")
        return {"ok": True, "entry": entry.to_dict()}

    async def import_md(self, payload: dict) -> dict:
        """导入 Markdown。支持长文档分块。返回 {ok, entry/entries, ...}。"""
        content = payload.get("content") or ""
        filename = (payload.get("filename") or "").strip()
        topic = (payload.get("topic") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(
            self._plugin._settings.get("refine_on_import", True)
        )
        chunk_size = int(payload.get("chunk_size", self._plugin._chunk_size))
        chunk_overlap = int(payload.get("chunk_overlap", self._plugin._chunk_overlap))

        if not content or not scope_type:
            return {"ok": False, "error": "content, scope_type required", "status_code": 400}

        content_clean, extracted_topic = _parse_md(content)
        if not topic:
            topic = extracted_topic or (filename.rsplit(".", 1)[0] if filename else "未命名")

        scope = Scope(type=scope_type, id=scope_id)
        chunks = chunk_markdown(content_clean, max_size=chunk_size, overlap=chunk_overlap)
        if not chunks:
            return {"ok": False, "error": "MD 内容为空", "status_code": 400}

        # 单 chunk
        if len(chunks) == 1:
            try:
                final_content = chunks[0]
                final_keywords = None
                final_confidence = self._plugin._default_confidence
                base_source = f"MD导入 ({filename})" if filename else "MD导入"
                source_tag = base_source
                if refine:
                    provider_id = await self._plugin._resolve_plugin_provider_id()
                    result = await self._plugin.refiner.refine_import(topic, chunks[0], provider_id)
                    final_content = result.summary
                    final_keywords = result.keywords
                    final_confidence = result.confidence
                    source_tag = f"{base_source}+精炼" if result.refined else f"{base_source}+未精炼"
                entry = self._plugin.store.add_or_update(
                    scope=scope, topic=topic, content=final_content,
                    keywords=final_keywords, source=source_tag,
                    sources_detail=None, confidence=final_confidence,
                )
            except Exception as e:
                return {"ok": False, "error": f"导入失败: {e}", "status_code": 500}
            logger.info(f"导入 MD: {topic} (scope: {scope}, source: {source_tag})")
            return {"ok": True, "entry": entry.to_dict()}

        # 多 chunk
        parent_doc_id = uuid.uuid4().hex[:16]
        source_label = f"MD导入 ({filename})" if filename else "MD导入"
        batch = await self._import_chunks_batch_data(
            chunks=chunks, scope=scope, parent_doc_id=parent_doc_id,
            base_topic=topic, source_label=source_label, refine=refine,
        )
        return {"ok": True, "batch": batch}

    async def import_pdf(self, payload: dict) -> dict:
        """导入 PDF。返回 {ok, batch} 或错误。"""
        import base64 as _b64

        b64 = payload.get("base64") or ""
        filename = (payload.get("filename") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(
            self._plugin._settings.get("refine_on_import", True)
        )
        chunk_size = int(payload.get("chunk_size", self._plugin._chunk_size))
        chunk_overlap = int(payload.get("chunk_overlap", self._plugin._chunk_overlap))

        if not b64 or not scope_type:
            return {"ok": False, "error": "base64, scope_type required", "status_code": 400}

        try:
            file_bytes = _b64.b64decode(b64)
        except Exception as e:
            return {"ok": False, "error": f"base64 解码失败: {e}", "status_code": 400}

        try:
            chunks = chunk_pdf(file_bytes, max_size=chunk_size, overlap=chunk_overlap)
        except ImportError as e:
            return {"ok": False, "error": str(e), "status_code": 400}
        except Exception as e:
            return {"ok": False, "error": f"PDF 解析失败: {e}", "status_code": 500}

        if not chunks:
            return {"ok": False, "error": "PDF 未提取到任何文本", "status_code": 400}

        scope = Scope(type=scope_type, id=scope_id)
        parent_doc_id = uuid.uuid4().hex[:16]
        base_topic = filename.rsplit(".", 1)[0] if filename else "PDF文档"
        source_label = f"PDF导入 ({filename})" if filename else "PDF导入"
        batch = await self._import_chunks_batch_data(
            chunks=chunks, scope=scope, parent_doc_id=parent_doc_id,
            base_topic=base_topic, source_label=source_label, refine=refine,
        )
        return {"ok": True, "batch": batch}

    async def import_docx(self, payload: dict) -> dict:
        """导入 DOCX。返回 {ok, batch} 或错误。"""
        import base64 as _b64

        b64 = payload.get("base64") or ""
        filename = (payload.get("filename") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(
            self._plugin._settings.get("refine_on_import", True)
        )
        chunk_size = int(payload.get("chunk_size", self._plugin._chunk_size))
        chunk_overlap = int(payload.get("chunk_overlap", self._plugin._chunk_overlap))

        if not b64 or not scope_type:
            return {"ok": False, "error": "base64, scope_type required", "status_code": 400}

        try:
            file_bytes = _b64.b64decode(b64)
        except Exception as e:
            return {"ok": False, "error": f"base64 解码失败: {e}", "status_code": 400}

        try:
            chunks = chunk_docx(file_bytes, max_size=chunk_size, overlap=chunk_overlap)
        except ImportError as e:
            return {"ok": False, "error": str(e), "status_code": 400}
        except Exception as e:
            return {"ok": False, "error": f"DOCX 解析失败: {e}", "status_code": 500}

        if not chunks:
            return {"ok": False, "error": "DOCX 未提取到任何文本", "status_code": 400}

        scope = Scope(type=scope_type, id=scope_id)
        parent_doc_id = uuid.uuid4().hex[:16]
        base_topic = filename.rsplit(".", 1)[0] if filename else "DOCX文档"
        source_label = f"DOCX导入 ({filename})" if filename else "DOCX导入"
        batch = await self._import_chunks_batch_data(
            chunks=chunks, scope=scope, parent_doc_id=parent_doc_id,
            base_topic=base_topic, source_label=source_label, refine=refine,
        )
        return {"ok": True, "batch": batch}

    async def import_txt(self, payload: dict) -> dict:
        """导入 TXT。返回 {ok, batch} 或错误。"""
        import base64 as _b64

        b64 = payload.get("base64") or ""
        filename = (payload.get("filename") or "").strip()
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(
            self._plugin._settings.get("refine_on_import", True)
        )
        chunk_size = int(payload.get("chunk_size", self._plugin._chunk_size))
        chunk_overlap = int(payload.get("chunk_overlap", self._plugin._chunk_overlap))

        if not b64 or not scope_type:
            return {"ok": False, "error": "base64, scope_type required", "status_code": 400}

        try:
            file_bytes = _b64.b64decode(b64)
        except Exception as e:
            return {"ok": False, "error": f"base64 解码失败: {e}", "status_code": 400}

        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("gbk", errors="replace")

        chunks = chunk_text(text, max_size=chunk_size, overlap=chunk_overlap)
        if not chunks:
            return {"ok": False, "error": "TXT 内容为空", "status_code": 400}

        scope = Scope(type=scope_type, id=scope_id)
        parent_doc_id = uuid.uuid4().hex[:16]
        base_topic = filename.rsplit(".", 1)[0] if filename else "TXT文档"
        source_label = f"TXT导入 ({filename})" if filename else "TXT导入"
        batch = await self._import_chunks_batch_data(
            chunks=chunks, scope=scope, parent_doc_id=parent_doc_id,
            base_topic=base_topic, source_label=source_label, refine=refine,
        )
        return {"ok": True, "batch": batch}

    async def import_zip(self, payload: dict) -> dict:
        """批量导入 ZIP 中的 .md 文件。返回 {ok, total, success, failed, results}。"""
        import base64 as _b64

        b64 = payload.get("base64") or ""
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(
            self._plugin._settings.get("refine_on_import", True)
        )
        if not b64 or not scope_type:
            return {"ok": False, "error": "base64, scope_type required", "status_code": 400}

        try:
            raw = _b64.b64decode(b64)
        except Exception as e:
            return {"ok": False, "error": f"base64 解码失败: {e}", "status_code": 400}
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except Exception as e:
            return {"ok": False, "error": f"无法读取 zip: {e}", "status_code": 400}

        scope = Scope(type=scope_type, id=scope_id)
        results = []
        success_count = 0
        for name in zf.namelist():
            if name.endswith("/") or not name.lower().endswith(".md"):
                continue
            try:
                md_bytes = zf.read(name)
                try:
                    md_content = md_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    md_content = md_bytes.decode("gbk", errors="replace")
                md_clean, extracted_topic = _parse_md(md_content)
                topic = extracted_topic or name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                final_content = md_clean
                final_keywords = None
                final_confidence = self._plugin._default_confidence
                base_source = f"ZIP导入 ({name})"
                source_tag = base_source
                if refine:
                    provider_id = await self._plugin._resolve_plugin_provider_id()
                    result = await self._plugin.refiner.refine_import(topic, md_clean, provider_id)
                    final_content = result.summary
                    final_keywords = result.keywords
                    final_confidence = result.confidence
                    source_tag = f"{base_source}+精炼" if result.refined else f"{base_source}+未精炼"
                entry = self._plugin.store.add_or_update(
                    scope=scope, topic=topic, content=final_content,
                    keywords=final_keywords, source=source_tag,
                    sources_detail=None, confidence=final_confidence,
                )
                success_count += 1
                results.append({"file": name, "topic": topic, "entry_id": entry.id, "ok": True})
            except Exception as e:
                results.append({"file": name, "ok": False, "error": str(e)})

        logger.info(
            f"批量导入 ZIP: {success_count}/{len(results)} 成功 (scope: {scope}, refine={refine})"
        )
        return {
            "ok": True, "total": len(results),
            "success": success_count, "failed": len(results) - success_count,
            "results": results,
        }

    # ========== 内置知识库导入 ==========

    async def get_builtin_kb_list(self) -> Optional[list[dict]]:
        """获取 AstrBot 内置知识库列表。不可用时返回 None。"""
        km = self._get_kb_manager()
        if km is None:
            return None
        try:
            kbs = await km.list_kbs()
        except Exception as e:
            logger.error(f"读取知识库列表失败: {e}", exc_info=True)
            raise
        items = []
        for kb in kbs:
            doc_count = 0
            try:
                docs = await km.kb_db.list_documents_by_kb(kb.kb_id, limit=10000)
                doc_count = len(docs)
            except Exception:
                pass
            items.append({
                "kb_id": kb.kb_id,
                "kb_name": kb.kb_name,
                "description": kb.description or "",
                "emoji": kb.emoji or "📚",
                "doc_count": doc_count,
            })
        return items

    async def get_builtin_kb_documents(self, kb_id: str) -> Optional[dict]:
        """获取指定 KB 的文档列表。返回 {items, kb_id, kb_name} 或 None（KB 不存在）。"""
        km = self._get_kb_manager()
        if km is None:
            return None
        try:
            kb_helper = await km.get_kb(kb_id)
            if kb_helper is None:
                return None
            kb_name = kb_helper.kb.kb_name
            docs = await km.kb_db.list_documents_by_kb(kb_id, limit=10000)
        except Exception as e:
            logger.error(f"读取 KB 文档列表失败 (kb_id={kb_id}): {e}", exc_info=True)
            raise
        items = [{
            "doc_id": d.doc_id,
            "doc_name": d.doc_name,
            "file_type": d.file_type,
            "chunk_count": d.chunk_count,
            "file_size": d.file_size,
            "created_at": float(d.created_at.timestamp()) if d.created_at else 0,
        } for d in docs]
        return {"items": items, "kb_id": kb_id, "kb_name": kb_name}

    async def import_builtin_kb(self, payload: dict) -> dict:
        """从内置 KB 批量导入。返回 {ok, total, success, failed, results}。"""
        kb_id = (payload.get("kb_id") or "").strip()
        doc_ids = payload.get("doc_ids") or []
        scope_type = (payload.get("scope_type") or "").strip()
        scope_id = (payload.get("scope_id") or "").strip()
        refine = bool(payload.get("refine", True)) and bool(
            self._plugin._settings.get("refine_on_import", True)
        )
        try:
            chunk_size = int(payload.get("chunk_size", self._plugin._chunk_size))
            chunk_overlap = int(payload.get("chunk_overlap", self._plugin._chunk_overlap))
        except (TypeError, ValueError):
            return {"ok": False, "error": "chunk_size / chunk_overlap 必须是整数", "status_code": 400}

        if not kb_id or not doc_ids or not scope_type:
            return {"ok": False, "error": "kb_id, doc_ids, scope_type required", "status_code": 400}
        if not isinstance(doc_ids, list):
            return {"ok": False, "error": "doc_ids 必须是数组", "status_code": 400}

        km = self._get_kb_manager()
        if km is None:
            return {
                "ok": False,
                "error": "当前 AstrBot 版本未启用知识库模块（kb_manager 不可用）",
                "status_code": 501,
            }
        try:
            kb_helper = await km.get_kb(kb_id)
            if kb_helper is None:
                return {"ok": False, "error": f"知识库 {kb_id} 不存在", "status_code": 404}
        except Exception as e:
            logger.error(f"获取内置 KB 失败 (kb_id={kb_id}): {e}", exc_info=True)
            return {"ok": False, "error": f"获取内置 KB 失败: {e}", "status_code": 500}

        scope = Scope(type=scope_type, id=scope_id)
        results = []
        success_count = 0

        for doc_id in doc_ids:
            try:
                doc = None
                try:
                    doc = await km.kb_db.get_document_by_id(doc_id)
                except Exception:
                    pass
                if doc is None:
                    results.append({"doc_id": doc_id, "ok": False, "error": "文档不存在"})
                    continue

                chunks_text = await self._read_builtin_doc_chunks(kb_helper, doc_id)
                if not chunks_text:
                    results.append({
                        "doc_id": doc_id, "doc_name": doc.doc_name,
                        "ok": False, "error": "文档无文本（chunks 为空）",
                    })
                    continue

                full_text = "\n\n".join(chunks_text)
                new_chunks = chunk_text(full_text, max_size=chunk_size, overlap=chunk_overlap)
                if not new_chunks:
                    results.append({
                        "doc_id": doc_id, "doc_name": doc.doc_name,
                        "ok": False, "error": "重新分块后无内容",
                    })
                    continue

                parent_doc_id = uuid.uuid4().hex[:16]
                base_topic = doc.doc_name.rsplit(".", 1)[0] if doc.doc_name else "内置KB文档"
                source_label = f"内置KB导入 ({kb_helper.kb.kb_name}/{doc.doc_name})"
                batch_data = await self._import_chunks_batch_data(
                    chunks=new_chunks, scope=scope, parent_doc_id=parent_doc_id,
                    base_topic=base_topic, source_label=source_label, refine=refine,
                )
                success_count += 1
                results.append({
                    "doc_id": doc_id, "doc_name": doc.doc_name,
                    "ok": True, "chunks": len(new_chunks), "batch": batch_data,
                })
            except Exception as e:
                results.append({"doc_id": doc_id, "ok": False, "error": str(e)})

        logger.info(
            f"内置 KB 批量导入: {success_count}/{len(doc_ids)} 成功 "
            f"(kb_id={kb_id}, scope={scope})"
        )
        return {
            "ok": True, "total": len(doc_ids),
            "success": success_count, "failed": len(doc_ids) - success_count,
            "results": results,
        }

    # ========== 批量 chunk 处理 ==========

    async def _import_chunks_batch_data(
        self, chunks: list[str], scope: Scope,
        parent_doc_id: str, base_topic: str,
        source_label: str, refine: bool,
    ) -> dict:
        """共享的批量 chunk 入库逻辑。

        - 批量精炼（如有 provider）
        - 批量嵌入（如有 embedder）
        - 每个 chunk 用 make_chunk_id 生成独立 ID
        - 写入后失效向量矩阵缓存
        """
        plugin = self._plugin

        refine_results = None
        if refine:
            provider_id = await plugin._resolve_plugin_provider_id()
            if provider_id:
                try:
                    refine_results = await plugin.refiner.refine_import_batch(
                        topics=[f"{base_topic} #{i+1}" for i in range(len(chunks))],
                        raw_contents=chunks,
                        provider_id=provider_id,
                    )
                except Exception as e:
                    logger.warning(f"批量精炼失败，降级为原内容: {e}")
                    refine_results = None

        embed_vecs = None
        if plugin.embedder is not None and plugin.embedder.available:
            try:
                embed_vecs = await plugin.embedder.embed_batch(chunks)
            except Exception as e:
                logger.warning(f"批量嵌入失败: {e}")
                embed_vecs = None

        results = []
        success_count = 0
        for i, chunk in enumerate(chunks):
            try:
                chunk_id = make_chunk_id(scope, parent_doc_id, i)
                topic = f"{base_topic} #{i+1}"

                if refine_results and i < len(refine_results) and refine_results[i].refined:
                    final_content = refine_results[i].summary
                    final_keywords = refine_results[i].keywords
                    final_confidence = refine_results[i].confidence
                    source_tag = f"{source_label}+精炼"
                else:
                    final_content = chunk
                    final_keywords = None
                    final_confidence = 0.5
                    source_tag = source_label

                entry = plugin.store.add_chunk(
                    chunk_id=chunk_id, scope=scope, topic=topic,
                    content=final_content, keywords=final_keywords,
                    source=source_tag, confidence=final_confidence,
                    parent_doc_id=parent_doc_id,
                )

                if embed_vecs and i < len(embed_vecs) and embed_vecs[i]:
                    try:
                        plugin.store.save_embedding(
                            chunk_id, embed_vecs[i],
                            plugin.embedder.dim, plugin.embedder.model_name,
                        )
                    except Exception as e:
                        logger.debug(f"保存向量失败 chunk {i}: {e}")

                success_count += 1
                results.append({"chunk": i + 1, "topic": topic, "entry_id": entry.id, "ok": True})
            except Exception as e:
                results.append({"chunk": i + 1, "ok": False, "error": str(e)})

        if plugin.embedder is not None:
            plugin.embedder.invalidate_matrix_cache(f"{scope.type}:{scope.id}")

        logger.info(
            f"导入 {source_label}: {success_count}/{len(chunks)} chunks 成功 "
            f"(scope: {scope}, refine={'yes' if refine_results else 'no'})"
        )
        return {
            "ok": True, "total": len(chunks),
            "success": success_count, "failed": len(chunks) - success_count,
            "parent_doc_id": parent_doc_id, "results": results,
        }

    # ========== 内部方法 ==========

    def _get_kb_manager(self):
        """获取 AstrBot KnowledgeBaseManager。"""
        return getattr(self._plugin.context, "kb_manager", None)

    async def _read_builtin_doc_chunks(self, kb_helper, doc_id: str) -> list[str]:
        """读取某文档的所有 chunk 文本。支持 API 优先 + SQLite 降级。"""
        try:
            storage = getattr(kb_helper.vec_db, "document_storage", None) if kb_helper.vec_db else None
            if storage is not None:
                rows = await storage.get_documents({"kb_doc_id": doc_id}, limit=10000)
                if rows:
                    def _idx(r):
                        meta = r.get("metadata") or {}
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except Exception:
                                meta = {}
                        return meta.get("chunk_index", 0)
                    rows_sorted = sorted(rows, key=_idx)
                    return [r.get("text", "") for r in rows_sorted if r.get("text")]
        except Exception as e:
            logger.debug(f"读 chunks (API 失败, 降级读 SQLite): {e}")

        try:
            import sqlite3 as _sqlite3
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                data_root = Path(get_astrbot_data_path())
            except ImportError:
                data_root = Path(self._plugin._db_path).parent.parent
            kb_id = kb_helper.kb.kb_id
            db_path = data_root / "knowledge_base" / kb_id / "doc.db"
            if not db_path.exists():
                logger.debug(f"内置 KB doc.db 不存在: {db_path}")
                return []
            conn = _sqlite3.connect(str(db_path), check_same_thread=False)
            try:
                rows = conn.execute(
                    """SELECT text, json_extract(metadata,'$.chunk_index') AS idx
                       FROM documents
                       WHERE json_extract(metadata,'$.kb_doc_id') = ?
                       ORDER BY idx""",
                    (doc_id,),
                ).fetchall()
                return [r[0] for r in rows if r[0]]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"读 chunks SQLite 失败: {e}")
            return []


# ========== 模块级工具函数 ==========

def _parse_md(content: str) -> tuple[str, str]:
    """解析 Markdown：去除 YAML frontmatter，提取首个 # 标题。

    返回 (clean_content, title)。无标题时 title 为空字符串。
    """
    title = ""
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            content = content[end + 4:].lstrip("\n")
    for line in content.split("\n", 30):
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
        if stripped and not stripped.startswith("#"):
            break
    return content, title
