"""知：series.webui@2.0 统一面板契约、受控导入与制品导出测试。"""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.importer import Importer  # noqa: E402
from astrbot_plugin_active_learner.models import Scope  # noqa: E402
from astrbot_plugin_active_learner import series_webui  # noqa: E402
from astrbot_plugin_active_learner.series_webui import (  # noqa: E402
    SeriesWebUIPanels,
)
from astrbot_plugin_active_learner.storage import MemoryStore  # noqa: E402

REPO_DIR = Path(__file__).resolve().parents[1]


class _FakePlugin:
    """最小可用插件面：真实 MemoryStore + 真实 Importer，不依赖 AstrBot。"""

    def __init__(self, tmp_path: Path) -> None:
        self.store = MemoryStore(tmp_path / "memory.db")
        self._settings = {"refine_on_import": False}
        self._default_confidence = 0.6
        self._chunk_size = 500
        self._chunk_overlap = 50
        self.embedder = None
        self.importer = Importer(self)
        self.config: dict = {}

    def _resolve_plugin_provider_id(self) -> str:
        return ""


@pytest.fixture()
def adapter(tmp_path: Path) -> SeriesWebUIPanels:
    plugin = _FakePlugin(tmp_path)
    try:
        yield SeriesWebUIPanels(plugin)
    finally:
        plugin.store.close()


def _record(filename: str, data: bytes, mime: str = "application/octet-stream") -> dict:
    """模拟核 WebUI 解析后的 file 字段（含 artifact_id，插件不读它）。"""
    return {
        "artifact_id": "a" * 32,
        "filename": filename,
        "mime": mime,
        "size": len(data),
        "data": data,
    }


def _action(adapter: SeriesWebUIPanels, action: str, payload: dict) -> dict:
    return asyncio.run(adapter.panel_action("transfer", action, payload))


# ---------- 契约声明 ----------


def test_contract_declares_webui_2_0_artifacts_and_file_upload(adapter):
    contract = adapter.contract()

    assert contract["name"] == "series.webui@2.0"
    assert contract["version"] == "2.0"
    assert contract["plugin_id"] == "astrbot_plugin_active_learner"
    assert contract["series_id"] == "ningxin_suxi"
    assert {"artifacts", "file_upload"} <= set(contract["capabilities"])
    # 既有 control / diagnostics 能力声明必须继续存在（module / diagnostics 契约）。
    assert {"control", "diagnostics"} <= set(contract["module_capabilities"])
    assert {"control", "diagnostics"} <= set(series_webui.MODULE_CAPABILITIES)


def test_main_entry_delegates_contract_but_keeps_control_and_diagnostics():
    source = (REPO_DIR / "main.py").read_text(encoding="utf-8")

    assert "self._series_webui = SeriesWebUIPanels(self)" in source
    assert "def webui_panels_contract(self)" in source
    assert "async def webui_panel_action(" in source
    assert '"capabilities": ["control", "diagnostics"]' in source


def test_transfer_actions_carry_stable_metadata(adapter):
    actions = {item["id"]: item for item in adapter.transfer_actions()}

    assert set(actions) == {"import_file", "export_scope"}
    for item in actions.values():
        assert item["min_role"] == "admin"
        assert item["effect"] in {"idempotent", "non_idempotent"}
        assert item["confirm"]
        assert item["payload_fields"]
    imported = actions["import_file"]
    assert imported["effect"] == "non_idempotent"
    assert imported["idempotency_required"] is True
    # 核支持动作级超时声明（1~60s），导入/导出都需要比缺省 3s 更宽的预算。
    assert imported["timeout_seconds"] == 30
    assert actions["export_scope"]["timeout_seconds"] == 15
    file_field = imported["payload_fields"][0]
    assert file_field["type"] == "file"
    assert file_field["required"] is True
    for item in actions.values():
        scope_field = [f for f in item["payload_fields"] if f["name"] == "scope"][0]
        assert scope_field["type"] == "select"
        assert all(value and label for value, label in scope_field["options"])


# ---------- 只读面板不回归 ----------


def test_overview_and_memories_panels_stay_read_only(adapter):
    adapter.plugin.store.add_or_update(
        Scope("global", "global"), "地球半径", "约 6371 公里", confidence=0.9
    )

    overview = adapter.panel_data("overview")
    assert overview["success"] is True
    assert overview["actions"] == []
    assert {"key": "item", "label": "项目"} in list(overview["columns"])
    assert overview["rows"][0]["item"] == "记忆总数"

    memories = adapter.panel_data("memories")
    assert memories["success"] is True
    assert memories["actions"] == []
    assert memories["rows"][0]["topic"] == "地球半径"


def test_transfer_panel_rows_and_actions_are_plain_data(adapter):
    adapter.plugin.store.add_or_update(
        Scope("private", "u-1"), "生日", "3 月 1 日", confidence=0.8
    )

    data = adapter.panel_data("transfer")

    assert data["success"] is True
    assert [column["key"] for column in data["columns"]] == ["scope", "kind", "count"]
    assert [row["scope"] for row in data["rows"]][0] == "global:global"
    assert "private:u-1" in [row["scope"] for row in data["rows"]]
    for row in data["rows"]:
        for value in row.values():
            assert isinstance(value, (str, int, float, bool, type(None)))
    assert [item["id"] for item in data["actions"]] == ["import_file", "export_scope"]


def test_unknown_panel_and_action_fail_closed(adapter):
    assert adapter.panel_data("nope") == {"success": False, "error": "UNKNOWN_PANEL"}
    assert _action(adapter, "drop_everything", {})["error"] == "UNKNOWN_ACTION"
    assert asyncio.run(adapter.panel_action("nope", "export_scope", {}))["error"] == (
        "UNKNOWN_PANEL"
    )


# ---------- 导入 ----------


def test_import_json_uses_importer_and_ignores_file_scope(adapter):
    document = {
        "format": "active_learner.knowledge.export",
        "memories": [
            {
                "topic": "潮汐锁定",
                "content": "月球始终以同一面朝向地球。",
                "keywords": ["月球", "潮汐"],
                # 文件自带 scope 必须被忽略，不能跨 scope 写入。
                "scope_type": "private",
                "scope_id": "victim-user",
            }
        ],
    }
    payload = {
        "file": _record("kb.json", json.dumps(document).encode("utf-8")),
        "scope": "global:global",
    }

    result = _action(adapter, "import_file", payload)

    assert result["success"] is True
    assert result["imported"] == 1
    entries = adapter.plugin.store.export_scope(Scope("global", "global"))
    assert [item["topic"] for item in entries] == ["潮汐锁定"]
    assert entries[0]["source"] == "统一面板导入"
    assert adapter.plugin.store.export_scope(Scope("private", "victim-user")) == []


def test_import_json_accepts_plain_list(adapter):
    payload = {
        "file": _record(
            "notes.json", json.dumps([{"topic": "A", "content": "甲"}]).encode()
        ),
        "scope": "global:global",
    }

    result = _action(adapter, "import_file", payload)

    assert result["success"] is True
    assert result["total"] == 1


def test_import_txt_and_md_reuse_existing_importer(adapter):
    # 先在 u-9 落一条，使其成为受控选项（面板不提供列表外的新 scope）。
    adapter.plugin.store.add_or_update(
        Scope("private", "u-9"), "占位", "占位内容", confidence=0.5
    )
    txt_result = _action(
        adapter,
        "import_file",
        {
            "file": _record("笔记.txt", "第一段知识。".encode("utf-8"), "text/plain"),
            "scope": "global:global",
        },
    )
    md_result = _action(
        adapter,
        "import_file",
        {
            "file": _record(
                "文档.md", "## 标题\n\n正文知识。".encode("utf-8"), "text/markdown"
            ),
            "scope": "private:u-9",
        },
    )

    assert txt_result["success"] is True and txt_result["imported"] >= 1
    assert md_result["success"] is True and md_result["total"] >= 1
    topics = [
        item["topic"]
        for item in adapter.plugin.store.export_scope(Scope("private", "u-9"))
    ]
    assert any(topic.startswith("文档") for topic in topics)


@pytest.mark.parametrize(
    ("record", "scope", "code"),
    [
        (None, "global:global", "FILE_REQUIRED"),
        (_record("kb.json", b""), "global:global", "FILE_EMPTY"),
        (_record("kb.pdf", b"%PDF-1.4"), "global:global", "UNSUPPORTED_FILE_TYPE"),
        (_record("kb.json", b"\xff\xfe\x00"), "global:global", "INVALID_FILE_ENCODING"),
        (_record("kb.json", b"{not json"), "global:global", "INVALID_JSON"),
        (_record("kb.json", b'{"foo": 1}'), "global:global", "INVALID_JSON_SHAPE"),
        (_record("kb.json", b'[{"content": "x"}]'), "private:nope", "INVALID_SCOPE"),
        (_record("kb.json", b'[{"content": "x"}]'), "../../etc/passwd", "INVALID_SCOPE"),
        (_record("kb.json", b'[{"topic": "x"}]'), "global:global", "INVALID_JSON_SHAPE"),
    ],
)
def test_import_fail_closed_codes(adapter, record, scope, code):
    payload: dict = {"scope": scope}
    if record is not None:
        payload["file"] = record

    result = _action(adapter, "import_file", payload)

    assert result["success"] is False
    assert result["error"] == code


def test_import_rejects_oversized_and_too_many_entries(adapter):
    oversize = b"a" * (series_webui.MAX_IMPORT_BYTES + 1)
    assert (
        _action(
            adapter,
            "import_file",
            {
                "file": _record("big.txt", oversize, "text/plain"),
                "scope": "global:global",
            },
        )["error"]
        == "FILE_TOO_LARGE"
    )

    many = [{"topic": f"t{i}", "content": "c"} for i in range(series_webui.MAX_JSON_ENTRIES + 1)]
    assert (
        _action(
            adapter,
            "import_file",
            {
                "file": _record("many.json", json.dumps(many).encode()),
                "scope": "global:global",
            },
        )["error"]
        == "JSON_ENTRY_LIMIT"
    )


def test_import_rejects_too_many_text_chunks(adapter):
    adapter.plugin._chunk_size = 100
    text = "知识。" * (series_webui.MAX_TEXT_CHUNKS * 100)

    result = _action(
        adapter,
        "import_file",
        {
            "file": _record("long.txt", text.encode("utf-8"), "text/plain"),
            "scope": "global:global",
        },
    )

    assert result["success"] is False
    assert result["error"] == "FILE_TOO_LARGE"


# ---------- 导出 ----------


def test_export_returns_artifact_with_only_selected_scope(adapter):
    adapter.plugin.store.add_or_update(
        Scope("global", "global"), "公开知识", "所有人可见", confidence=0.9
    )
    adapter.plugin.store.add_or_update(
        Scope("private", "u-7"), "私密知识", "只属于 u-7", confidence=0.7
    )

    result = _action(adapter, "export_scope", {"scope": "global:global"})

    assert result["success"] is True
    assert result["count"] == 1
    artifact = result["artifacts"][0]
    assert artifact["mime"] == "application/json"
    assert re.fullmatch(
        r"active-learner-global-global-\d{8}-\d{6}\.json", artifact["filename"]
    )
    assert isinstance(artifact["data"], bytes)
    document = json.loads(artifact["data"].decode("utf-8"))
    assert document["count"] == 1
    assert [item["topic"] for item in document["memories"]] == ["公开知识"]
    # 不得暴露本地路径、内部 ID 或跨 scope 数据
    assert "私密知识" not in artifact["data"].decode("utf-8")
    assert "/home/" not in artifact["data"].decode("utf-8")
    assert set(document["memories"][0]) == {
        "topic",
        "content",
        "keywords",
        "verified",
        "confidence",
    }


def test_export_filename_is_path_safe(adapter):
    adapter.plugin.store.add_or_update(
        Scope("group", "12/../../etc"), "群知识", "内容", confidence=0.5
    )

    value = "group:12/../../etc"
    assert value in [choice["value"] for choice in adapter.scope_choices()]
    result = _action(adapter, "export_scope", {"scope": value})

    assert result["success"] is True
    filename = result["artifacts"][0]["filename"]
    assert "/" not in filename and ".." not in filename
    assert re.fullmatch(r"active-learner-group-[A-Za-z0-9_-]+-\d{8}-\d{6}\.json", filename)


def test_export_empty_scope_is_still_valid_artifact(adapter):
    result = _action(adapter, "export_scope", {"scope": "global:global"})

    assert result["success"] is True
    assert result["count"] == 0
    document = json.loads(result["artifacts"][0]["data"].decode("utf-8"))
    assert document["memories"] == []


def test_export_rejects_unknown_scope(adapter):
    result = _action(adapter, "export_scope", {"scope": "private:someone-else"})

    assert result["success"] is False
    assert result["error"] == "INVALID_SCOPE"


def test_export_result_round_trips_through_import(adapter):
    adapter.plugin.store.add_or_update(
        Scope("global", "global"), "往返知识", "导出后再导入", confidence=0.9
    )
    exported = _action(adapter, "export_scope", {"scope": "global:global"})
    artifact = exported["artifacts"][0]

    imported = _action(
        adapter,
        "import_file",
        {
            "file": _record(artifact["filename"], artifact["data"], artifact["mime"]),
            "scope": "global:global",
        },
    )

    assert imported["success"] is True
    assert imported["imported"] == 1
