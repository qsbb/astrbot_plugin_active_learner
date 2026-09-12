"""``series.webui@2.0`` 统一面板适配层（知）。

核 WebUI（astrbot_plugin_update_manager）在统一接管模式下通过进程内方法调用
``webui_panels_contract()`` / ``webui_panel_data(panel)`` /
``webui_panel_action(panel, action, payload)``；具体实现集中在本模块，主入口只做转发。

安全与边界（沿用 series.webui@1.1 动作元数据，再按 2.0 追加制品传输）：

- 面板数据只返回普通 JSON 值（columns/rows/actions/artifacts），不返回对象或路径；
- 动作声明稳定 id、``min_role``、``effect`` 与必要 ``confirm``，核网关与插件本层各校验一次；
- 文件上传走核的 ArtifactStore：插件只接收
  ``{artifact_id, filename, mime, size, data}``，不接触上传临时路径，也不接受浏览器直传路径；
- 导入目标 scope 只能从受控选项（global + 现存 scope）中选择，拒绝任意字符串；
- 文件大小 / 扩展名 / 编码 / JSON 形状一律 fail-closed，错误码稳定供核前端展示；
- 导出只读所选 scope，经 ``store.export_scope`` 生成 JSON bytes 并作为 ``artifacts`` 返回，
  不含本地路径、内部 ID 或跨 scope 数据。
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any, Mapping

from .chunker import chunk_markdown, chunk_text
from .models import SCOPE_GLOBAL, SCOPE_GROUP, SCOPE_PRIVATE, Scope

CONTRACT_NAME = "series.webui@2.0"
CONTRACT_VERSION = "2.0"
PLUGIN_ID = "astrbot_plugin_active_learner"
SERIES_ID = "ningxin_suxi"

# 本模块自己声明的 webui 能力；control / diagnostics 仍由
# series.module@1.0 与 series.diagnostics@1.0 契约声明，这里只做交叉引用。
WEBUI_CAPABILITIES = ("artifacts", "file_upload")
MODULE_CAPABILITIES = ("control", "diagnostics")

TRANSFER_PANEL_ID = "transfer"
IMPORT_ACTION_ID = "import_file"
EXPORT_ACTION_ID = "export_scope"

# 上限取核 ArtifactStore 上限（单文件 10MB）以内，并按核 3 秒动作超时收紧。
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_JSON_ENTRIES = 200
MAX_TEXT_CHUNKS = 200
MAX_EXPORT_ENTRIES = 5000
MAX_SCOPE_OPTIONS = 100
MAX_TOPIC_CHARS = 200
MAX_ENTRY_CONTENT_CHARS = 50_000
MAX_KEYWORDS = 32
MAX_KEYWORD_CHARS = 64
MAX_FILENAME_CHARS = 120

_FILE_KINDS = (
    (".json", "json"),
    (".md", "md"),
    (".markdown", "md"),
    (".txt", "txt"),
)

_SCOPE_KINDS = {
    SCOPE_GLOBAL: "全局",
    SCOPE_GROUP: "群聊",
    SCOPE_PRIVATE: "私聊",
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")

EXPORT_FORMAT = "active_learner.knowledge.export"
EXPORT_FORMAT_VERSION = "1.0"


def _failure(code: str, message: str = "") -> dict[str, Any]:
    """稳定错误码 + 可选中文说明；前端按 code 展示、按 message 提示。"""
    payload: dict[str, Any] = {"success": False, "error": code}
    if message:
        payload["message"] = message
    return payload


def _display_filename(filename: str) -> str:
    """只取末段文件名用作来源标记，绝不参与任何路径拼接。"""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:MAX_FILENAME_CHARS]


class SeriesWebUIPanels:
    """实现知插件侧的 series.webui@2.0 面板契约。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ---------- 契约声明 ----------

    def contract(self) -> dict[str, Any]:
        """返回 series.webui@2.0 契约与面板/动作声明。"""
        return {
            "name": CONTRACT_NAME,
            "version": CONTRACT_VERSION,
            "plugin_id": PLUGIN_ID,
            "series_id": SERIES_ID,
            "state_owner": "plugin",
            "standalone": {
                "available": True,
                "entry": "/pages/manager",
                "pages": ["manager"],
            },
            "capabilities": list(WEBUI_CAPABILITIES),
            "module_capabilities": list(MODULE_CAPABILITIES),
            "panels": [
                {
                    "id": "overview",
                    "title": "知识概览",
                    "description": "记忆统计与 scope 概况",
                },
                {
                    "id": "memories",
                    "title": "记忆列表",
                    "description": "最近 50 条记忆（只读）",
                },
                {
                    "id": TRANSFER_PANEL_ID,
                    "title": "知识导入 / 导出",
                    "description": "从受控 Scope 选项导入 JSON/TXT/MD，或导出该 Scope",
                    "actions": self.transfer_actions(),
                },
            ],
        }

    def transfer_actions(self) -> list[dict[str, Any]]:
        scope_field = {
            "name": "scope",
            "label": "目标 Scope",
            "type": "select",
            "required": True,
            "options": tuple(
                (choice["value"], choice["label"]) for choice in self.scope_choices()
            ),
            "hint": "只能导入/导出到列表中的现有 Scope",
        }
        return [
            {
                "id": IMPORT_ACTION_ID,
                "label": "导入知识文件",
                "effect": "non_idempotent",
                "idempotency_required": True,
                "min_role": "admin",
                "revision_required": False,
                # 核支持动作级超时声明（1~60s，缺省 3s）；导入可能需要更多时间。
                "timeout_seconds": 30,
                "confirm": "确认导入该知识文件？内容会写入所选 Scope 的记忆库。",
                "payload_fields": (
                    {
                        "name": "file",
                        "label": "知识文件（.json / .txt / .md）",
                        "type": "file",
                        "required": True,
                        "hint": "JSON 需为导出格式或记忆对象数组",
                    },
                    scope_field,
                ),
            },
            {
                "id": EXPORT_ACTION_ID,
                "label": "导出该 Scope",
                "effect": "idempotent",
                "min_role": "admin",
                "revision_required": False,
                "timeout_seconds": 15,
                "confirm": "确认导出？导出文件包含该 Scope 的全部知识正文。",
                "payload_fields": (scope_field,),
            },
        ]

    # ---------- 面板数据 ----------

    def panel_data(self, panel: str) -> dict[str, Any]:
        if panel == "overview":
            return self._overview_data()
        if panel == "memories":
            return self._memories_data()
        if panel == TRANSFER_PANEL_ID:
            return self._transfer_data()
        return _failure("UNKNOWN_PANEL")

    def _overview_data(self) -> dict[str, Any]:
        stats = self.plugin.store.global_stats()
        scopes = self.plugin.store.list_scopes()
        rows = [
            {"item": "记忆总数", "value": stats.get("total", 0)},
            {"item": "已验证", "value": stats.get("verified", 0)},
            {"item": "挑战中", "value": stats.get("challenged", 0)},
            {
                "item": "平均置信度",
                "value": f"{float(stats.get('avg_confidence') or 0.0):.2f}",
            },
            {"item": "总访问次数", "value": stats.get("access_total", 0)},
            {"item": "Scope 数量", "value": len(scopes)},
        ]
        return {
            "success": True,
            "title": "知识概览",
            "columns": [
                {"key": "item", "label": "项目"},
                {"key": "value", "label": "数值"},
            ],
            "rows": rows,
            "actions": [],
        }

    def _memories_data(self) -> dict[str, Any]:
        entries, total, total_pages = self.plugin.store.list_all_memories(
            page=1, per_page=50
        )
        rows = []
        for entry in entries:
            data = entry.to_dict()
            content = str(data.get("content") or "").replace("\n", " ")[:120]
            rows.append(
                {
                    "id": data.get("id", ""),
                    "topic": data.get("topic", ""),
                    "content": content,
                    "verified": "是" if data.get("verified") else "否",
                    "confidence": f"{float(data.get('confidence') or 0.0):.0%}",
                }
            )
        return {
            "success": True,
            "title": "记忆列表",
            "description": (
                f"共 {total} 条，展示最近 {len(rows)} 条（第 1/{total_pages} 页）"
            ),
            "columns": [
                {"key": "id", "label": "ID"},
                {"key": "topic", "label": "主题"},
                {"key": "content", "label": "内容"},
                {"key": "verified", "label": "已验证"},
                {"key": "confidence", "label": "置信度"},
            ],
            "rows": rows,
            "actions": [],
        }

    def _transfer_data(self) -> dict[str, Any]:
        choices = self.scope_choices()
        rows = [
            {
                "scope": choice["value"],
                "kind": choice["kind"],
                "count": choice["count"],
            }
            for choice in choices
        ]
        return {
            "success": True,
            "title": "知识导入 / 导出",
            "description": (
                f"可操作 Scope {len(choices)} 个；导入不调用模型精炼，保证在核动作超时内完成"
            ),
            "columns": (
                {"key": "scope", "label": "Scope"},
                {"key": "kind", "label": "类型"},
                {"key": "count", "label": "记忆条数"},
            ),
            "rows": rows,
            "actions": self.transfer_actions(),
            "footer": (
                "导入上限：单文件 2MB、JSON 200 条、文本 200 分块；"
                "导出上限：5000 条 / 8MB。超出或损坏的文件会 fail-closed 并给出错误码。"
                "如需导入到列表外的新 Scope，请使用插件独立 Page 的导入功能。"
            ),
        }

    # ---------- 动作执行 ----------

    async def panel_action(
        self,
        panel: str,
        action: str,
        payload: Mapping[str, Any] | None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del context  # 幂等/令牌由核网关校验，插件层不重复解释
        if panel != TRANSFER_PANEL_ID:
            return _failure("UNKNOWN_PANEL")
        if not isinstance(payload, Mapping):
            return _failure("INVALID_PAYLOAD")
        if action == IMPORT_ACTION_ID:
            return await self._run_import(payload)
        if action == EXPORT_ACTION_ID:
            return self._run_export(payload)
        return _failure("UNKNOWN_ACTION")

    async def _run_import(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        scope, failure = self._resolve_scope(payload.get("scope"))
        if failure is not None:
            return failure
        info, failure = self._resolve_file(payload.get("file"))
        if failure is not None:
            return failure
        assert info is not None and scope is not None  # 由上面两处校验保证

        data: bytes = info["data"]
        filename: str = info["name"]
        kind: str = info["kind"]
        if len(data) > MAX_IMPORT_BYTES:
            return _failure(
                "FILE_TOO_LARGE",
                f"文件 {len(data)} 字节，超过上限 {MAX_IMPORT_BYTES} 字节",
            )
        try:
            if kind == "json":
                return await self._import_json(data, filename, scope)
            if kind == "md":
                return await self._import_markdown(data, filename, scope)
            return await self._import_text(data, filename, scope)
        except Exception:  # noqa: BLE001 — 面板动作必须 fail-closed，不向上抛细节
            return _failure("IMPORT_FAILED", "导入失败，请查看插件诊断日志")

    def _run_export(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        scope, failure = self._resolve_scope(payload.get("scope"))
        if failure is not None:
            return failure
        assert scope is not None
        try:
            entries = self.plugin.store.export_scope(scope)
        except Exception:  # noqa: BLE001
            return _failure("EXPORT_FAILED", "导出失败，请查看插件诊断日志")
        if len(entries) > MAX_EXPORT_ENTRIES:
            return _failure(
                "EXPORT_TOO_LARGE",
                f"{len(entries)} 条超过导出上限 {MAX_EXPORT_ENTRIES} 条",
            )
        document = {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "count": len(entries),
            "memories": [self._export_entry(entry) for entry in entries],
        }
        data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        if len(data) > MAX_EXPORT_BYTES:
            return _failure(
                "EXPORT_TOO_LARGE",
                f"导出 {len(data)} 字节，超过上限 {MAX_EXPORT_BYTES} 字节",
            )
        filename = self._export_filename(scope)
        return {
            "success": True,
            "message": f"已导出 {len(entries)} 条知识：{filename}",
            "count": len(entries),
            "artifacts": [
                {"filename": filename, "mime": "application/json", "data": data}
            ],
        }

    # ---------- 文件解析（复用 importer 能力，只做体积与形状适配） ----------

    def _resolve_file(
        self, raw: Any
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """把核解析后的 file 字段规范化为 {name, kind, data}。"""
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        if not isinstance(raw, Mapping):
            return None, _failure("FILE_REQUIRED", "请上传知识文件")
        data = raw.get("data")
        if not isinstance(data, (bytes, bytearray)):
            return None, _failure("FILE_REQUIRED", "上传文件缺少内容")
        if not data:
            return None, _failure("FILE_EMPTY", "上传文件为空")
        name = _display_filename(str(raw.get("filename") or ""))
        if not name:
            return None, _failure("FILE_REQUIRED", "上传文件缺少文件名")
        lower = name.lower()
        kind = next(
            (value for suffix, value in _FILE_KINDS if lower.endswith(suffix)), ""
        )
        if not kind:
            return None, _failure(
                "UNSUPPORTED_FILE_TYPE", "仅支持 .json / .txt / .md 文件"
            )
        return {"name": name, "kind": kind, "data": bytes(data)}, None

    async def _import_json(
        self, data: bytes, filename: str, scope: Scope
    ) -> dict[str, Any]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return _failure("INVALID_FILE_ENCODING", "JSON 文件必须是 UTF-8 编码")
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            return _failure("INVALID_JSON", "JSON 解析失败")
        entries, failure = self._extract_entries(raw, filename)
        if failure is not None:
            return failure
        assert entries is not None

        imported = 0
        failed_codes: list[str] = []
        for entry in entries:
            result = await self.plugin.importer.import_text(
                {
                    "topic": entry["topic"],
                    "content": entry["content"],
                    "keywords": entry["keywords"],
                    "scope_type": scope.type,
                    "scope_id": scope.id,
                    "refine": False,
                    "source_label": "统一面板导入",
                    "origin": f"import:{filename}",
                }
            )
            if isinstance(result, Mapping) and result.get("ok"):
                imported += 1
            else:
                # 只回传稳定错误码：底层异常文本可能含本地路径，不进入面板响应。
                if len(failed_codes) < 5:
                    failed_codes.append("IMPORT_ENTRY_FAILED")
        if imported == 0:
            return _failure("IMPORT_FAILED", f"{len(entries)} 条全部导入失败")
        payload: dict[str, Any] = {
            "success": True,
            "message": f"已导入 {imported}/{len(entries)} 条到 {scope.type}:{scope.id}",
            "imported": imported,
            "total": len(entries),
            "failed": len(entries) - imported,
        }
        if failed_codes:
            payload["errors"] = failed_codes
        return payload

    def _extract_entries(
        self, raw: Any, filename: str
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        """校验并规范化 JSON 形状；文件自带的 scope 字段一律忽略。"""
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, Mapping) and isinstance(raw.get("memories"), list):
            items = raw["memories"]
        elif isinstance(raw, Mapping) and "content" in raw:
            items = [raw]
        else:
            return None, _failure(
                "INVALID_JSON_SHAPE",
                "JSON 必须是记忆对象数组，或含 memories 数组的导出文件",
            )
        if not items:
            return None, _failure("INVALID_JSON_SHAPE", "没有可导入的记忆条目")
        if len(items) > MAX_JSON_ENTRIES:
            return None, _failure(
                "JSON_ENTRY_LIMIT",
                f"{len(items)} 条超过单次导入上限 {MAX_JSON_ENTRIES} 条",
            )

        stem = filename.rsplit(".", 1)[0][:MAX_TOPIC_CHARS] or "导入知识"
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                return None, _failure(
                    "INVALID_JSON_SHAPE", f"第 {index + 1} 条不是对象"
                )
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                return None, _failure(
                    "INVALID_JSON_SHAPE", f"第 {index + 1} 条缺少 content"
                )
            if len(content) > MAX_ENTRY_CONTENT_CHARS:
                return None, _failure(
                    "INVALID_JSON_SHAPE",
                    f"第 {index + 1} 条 content 超过 {MAX_ENTRY_CONTENT_CHARS} 字符",
                )
            topic_raw = item.get("topic")
            if topic_raw is None or not str(topic_raw).strip():
                topic = f"{stem} #{index + 1}"
            else:
                topic = str(topic_raw).strip()[:MAX_TOPIC_CHARS]
            keywords_raw = item.get("keywords")
            keywords: list[str] = []
            if keywords_raw not in (None, [], ()):
                if not isinstance(keywords_raw, (list, tuple)):
                    return None, _failure(
                        "INVALID_JSON_SHAPE", f"第 {index + 1} 条 keywords 不是数组"
                    )
                if len(keywords_raw) > MAX_KEYWORDS:
                    return None, _failure(
                        "INVALID_JSON_SHAPE",
                        f"第 {index + 1} 条 keywords 超过 {MAX_KEYWORDS} 个",
                    )
                for keyword in keywords_raw:
                    if not isinstance(keyword, str):
                        return None, _failure(
                            "INVALID_JSON_SHAPE",
                            f"第 {index + 1} 条 keywords 含非字符串项",
                        )
                    keyword = keyword.strip()
                    if not keyword:
                        continue
                    keywords.append(keyword[:MAX_KEYWORD_CHARS])
            entries.append(
                {"topic": topic, "content": content, "keywords": keywords or None}
            )
        return entries, None

    async def _import_text(
        self, data: bytes, filename: str, scope: Scope
    ) -> dict[str, Any]:
        try:
            text = _decode_text(data)
        except ValueError:
            return _failure("INVALID_FILE_ENCODING", "TXT 文件必须是 UTF-8 或 GBK 编码")
        if not text.strip():
            return _failure("FILE_EMPTY", "TXT 内容为空")
        chunks = chunk_text(
            text, max_size=self._chunk_size(), overlap=self._chunk_overlap()
        )
        if len(chunks) > MAX_TEXT_CHUNKS:
            return _failure(
                "FILE_TOO_LARGE",
                f"分块 {len(chunks)} 段超过上限 {MAX_TEXT_CHUNKS} 段",
            )
        result = await self.plugin.importer.import_txt(
            {
                "base64": base64.b64encode(data).decode("ascii"),
                "filename": filename,
                "scope_type": scope.type,
                "scope_id": scope.id,
                "refine": False,
            }
        )
        return self._batch_result(result, filename, scope, len(chunks))

    async def _import_markdown(
        self, data: bytes, filename: str, scope: Scope
    ) -> dict[str, Any]:
        try:
            text = _decode_text(data)
        except ValueError:
            return _failure(
                "INVALID_FILE_ENCODING", "Markdown 文件必须是 UTF-8 或 GBK 编码"
            )
        if not text.strip():
            return _failure("FILE_EMPTY", "Markdown 内容为空")
        chunks = chunk_markdown(
            text, max_size=self._chunk_size(), overlap=self._chunk_overlap()
        )
        if len(chunks) > MAX_TEXT_CHUNKS:
            return _failure(
                "FILE_TOO_LARGE",
                f"分块 {len(chunks)} 段超过上限 {MAX_TEXT_CHUNKS} 段",
            )
        result = await self.plugin.importer.import_md(
            {
                "content": text,
                "filename": filename,
                "scope_type": scope.type,
                "scope_id": scope.id,
                "refine": False,
            }
        )
        return self._batch_result(result, filename, scope, len(chunks))

    @staticmethod
    def _batch_result(
        result: Any, filename: str, scope: Scope, expected_chunks: int
    ) -> dict[str, Any]:
        if not isinstance(result, Mapping) or not result.get("ok"):
            code = "IMPORT_FAILED"
            if isinstance(result, Mapping):
                code = str(result.get("error") or code)[:80]
            return _failure(code, "导入失败")
        batch = result.get("batch")
        if isinstance(batch, Mapping):
            written = int(batch.get("success") or 0)
            total = int(batch.get("total") or expected_chunks)
            payload: dict[str, Any] = {
                "success": True,
                "message": f"已导入 {written}/{total} 段到 {scope.type}:{scope.id}",
                "imported": written,
                "total": total,
                "failed": int(batch.get("failed") or max(total - written, 0)),
            }
            if payload["failed"]:
                payload["errors"] = ["IMPORT_CHUNK_FAILED"]
            return payload
        return {
            "success": True,
            "message": f"已导入 1 条到 {scope.type}:{scope.id}",
            "imported": 1,
            "total": 1,
            "failed": 0,
        }

    # ---------- 受控 Scope 选项 ----------

    def scope_choices(self) -> list[dict[str, Any]]:
        """返回受控 scope 选项：global + 现存 scope（按记忆数排序）。"""
        choices: list[dict[str, Any]] = [
            {
                "value": f"{SCOPE_GLOBAL}:{SCOPE_GLOBAL}",
                "label": "全局（所有人共享）",
                "kind": _SCOPE_KINDS[SCOPE_GLOBAL],
                "count": 0,
            }
        ]
        seen = {choices[0]["value"]}
        try:
            scopes = self.plugin.store.list_scopes()
        except Exception:  # noqa: BLE001 — 选项枚举失败时只保留 global
            scopes = []
        for item in scopes or []:
            if not isinstance(item, Mapping):
                continue
            scope_type = str(item.get("scope_type") or "")
            scope_id = str(item.get("scope_id") or "")
            if scope_type not in _SCOPE_KINDS or not _valid_scope_id(scope_type, scope_id):
                continue
            count = item.get("count")
            count = count if isinstance(count, int) and not isinstance(count, bool) else 0
            value = f"{scope_type}:{scope_id}"
            if value in seen:
                # global 已在首个选项里，用真实条数回填即可。
                if scope_type == SCOPE_GLOBAL:
                    choices[0]["count"] = count
                continue
            seen.add(value)
            choices.append(
                {
                    "value": value,
                    "label": f"{_SCOPE_KINDS[scope_type]} · {scope_id}（{count} 条）",
                    "kind": _SCOPE_KINDS[scope_type],
                    "count": count,
                }
            )
            if len(choices) >= MAX_SCOPE_OPTIONS:
                break
        return choices

    def _resolve_scope(
        self, raw: Any
    ) -> tuple[Scope | None, dict[str, Any] | None]:
        value = str(raw or "").strip()
        allowed = {choice["value"] for choice in self.scope_choices()}
        if not value or value not in allowed:
            return None, _failure(
                "INVALID_SCOPE",
                "请选择面板列出的受控 Scope；列表外的新 Scope 请用插件独立 Page 导入",
            )
        scope_type, _, scope_id = value.partition(":")
        if scope_type == SCOPE_GLOBAL:
            return Scope(SCOPE_GLOBAL, SCOPE_GLOBAL), None
        return Scope(scope_type, scope_id), None

    # ---------- 导出辅助 ----------

    @staticmethod
    def _export_entry(entry: Any) -> dict[str, Any]:
        data = entry.to_dict() if hasattr(entry, "to_dict") else dict(entry or {})
        keywords = data.get("keywords")
        return {
            "topic": str(data.get("topic") or "")[:MAX_TOPIC_CHARS],
            "content": str(data.get("content") or ""),
            "keywords": [
                str(item)[:MAX_KEYWORD_CHARS]
                for item in (keywords if isinstance(keywords, list) else [])
            ],
            "verified": bool(data.get("verified")),
            "confidence": float(data.get("confidence") or 0.0),
        }

    @staticmethod
    def _export_filename(scope: Scope) -> str:
        safe = _SAFE_NAME.sub("_", scope.id)[:32] or "scope"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"active-learner-{scope.type}-{safe}-{stamp}.json"

    # ---------- 运行参数 ----------

    def _chunk_size(self) -> int:
        return max(100, min(5000, int(getattr(self.plugin, "_chunk_size", 500))))

    def _chunk_overlap(self) -> int:
        return max(0, min(1000, int(getattr(self.plugin, "_chunk_overlap", 50))))


def _valid_scope_id(scope_type: str, scope_id: str) -> bool:
    if scope_type == SCOPE_GLOBAL:
        return scope_id == SCOPE_GLOBAL
    return bool(scope_id) and len(scope_id) <= 128 and ":" not in scope_id


def _decode_text(data: bytes) -> str:
    """UTF-8 优先、GBK 兜底；两者都失败时抛 ValueError（fail-closed）。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gbk")
        except UnicodeDecodeError as exc:
            raise ValueError("INVALID_FILE_ENCODING") from exc
