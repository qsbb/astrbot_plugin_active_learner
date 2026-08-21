import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.series_control import SeriesControlAdapter


class _Manager:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Plugin:
    def __init__(self, tmp_path: Path, values=None):
        values = values or {
            "embedding_enabled": True,
            "context_inject_count": 3,
            "search_top_k": 5,
        }
        self._db_path = tmp_path / "memory.db"
        self.config_manager = _Manager(dict(values))
        self.config = dict(values)
        self._embedding_enabled = True
        self._context_inject_count = 3
        self._search_top_k = 5


class _RuntimePlugin(_Plugin):
    def _apply_series_control_runtime(self, effective):
        self._embedding_enabled = bool(effective["embedding_enabled"])
        self._context_inject_count = int(effective["context_inject_count"])
        self._search_top_k = int(effective["search_top_k"])
        self.embedder = object() if self._embedding_enabled else None


def test_contract_schema_exposes_exact_safe_fields(tmp_path):
    adapter = SeriesControlAdapter(_Plugin(tmp_path))
    assert adapter.series_control_contract()["name"] == "series.control@1.0"
    assert adapter.series_control_contract()["capabilities"] == [
        "read_schema",
        "read_snapshot",
        "validate_patch",
        "apply_patch",
        "reset_override",
    ]
    schema = adapter.series_control_schema()
    assert set(schema["fields"]) == {
        "embedding_enabled",
        "context_inject_count",
        "search_top_k",
    }
    assert all(spec["secret"] is False for spec in schema["fields"].values())
    assert schema["fields"]["context_inject_count"]["minimum"] == 1
    assert schema["fields"]["context_inject_count"]["maximum"] == 3
    assert schema["fields"]["search_top_k"]["minimum"] == 1
    assert schema["fields"]["search_top_k"]["maximum"] == 20


def test_snapshot_reports_native_and_managed_sources(tmp_path):
    adapter = SeriesControlAdapter(
        _Plugin(tmp_path, {"search_top_k": 5})
    )
    snapshot = adapter.series_control_snapshot()
    assert snapshot["fields"]["search_top_k"]["native_configured"] is True
    assert snapshot["fields"]["embedding_enabled"]["native_configured"] is False
    assert snapshot["fields"]["embedding_enabled"]["effective_value"] is True
    assert snapshot["fields"]["embedding_enabled"]["effective_source"] == "plugin"


def test_apply_updates_all_runtime_fields_and_persists(tmp_path):
    plugin = _Plugin(tmp_path)
    adapter = SeriesControlAdapter(plugin)
    adapter.series_control_set_mode("managed")
    result = adapter.apply_series_control_patch(
        {
            "embedding_enabled": False,
            "context_inject_count": 1,
            "search_top_k": 9,
        },
        expected_revision=0,
    )
    assert result["status"] == "ok"
    assert result["revision"] == 1
    assert plugin._embedding_enabled is False
    assert plugin._context_inject_count == 1
    assert plugin._search_top_k == 9
    assert adapter.series_control_snapshot()["fields"]["search_top_k"]["effective_source"] == "managed"

    restarted = SeriesControlAdapter(_Plugin(tmp_path))
    restarted.series_control_set_mode("managed")
    assert restarted.effective_config() == {
        "embedding_enabled": False,
        "context_inject_count": 1,
        "search_top_k": 9,
    }


def test_runtime_hook_handles_embedding_lifecycle(tmp_path):
    plugin = _RuntimePlugin(tmp_path)
    plugin.embedder = object()
    adapter = SeriesControlAdapter(plugin)
    adapter.series_control_set_mode("managed")
    adapter.apply_series_control_patch({"embedding_enabled": False}, expected_revision=0)
    assert plugin.embedder is None
    adapter.reset_series_control_override(expected_revision=1)
    assert plugin.embedder is not None


def test_type_range_unknown_and_revision_validation(tmp_path):
    adapter = SeriesControlAdapter(_Plugin(tmp_path))
    assert adapter.validate_series_control_patch({"unknown": True}, expected_revision=0)["reason"] == "UNKNOWN_FIELD"
    assert adapter.validate_series_control_patch({"embedding_enabled": 1}, expected_revision=0)["reason"] == "INVALID_TYPE"
    assert adapter.validate_series_control_patch({"context_inject_count": True}, expected_revision=0)["reason"] == "INVALID_VALUE"
    assert adapter.validate_series_control_patch({"context_inject_count": 0}, expected_revision=0)["reason"] == "INVALID_VALUE"
    assert adapter.validate_series_control_patch({"search_top_k": 21}, expected_revision=0)["reason"] == "INVALID_VALUE"
    assert adapter.apply_series_control_patch({"search_top_k": 6}, expected_revision=1)["reason"] == "REVISION_CONFLICT"


def test_reset_restores_native_values(tmp_path):
    plugin = _Plugin(tmp_path)
    adapter = SeriesControlAdapter(plugin)
    adapter.apply_series_control_patch({"search_top_k": 10}, expected_revision=0)
    result = adapter.reset_series_control_override(expected_revision=1)
    assert result["status"] == "ok"
    assert plugin._search_top_k == 5
    assert adapter.series_control_snapshot()["fields"]["search_top_k"]["effective_source"] == "plugin"


def test_apply_failure_restores_previous_overlay_and_revision(tmp_path, monkeypatch):
    plugin = _Plugin(tmp_path)
    adapter = SeriesControlAdapter(plugin)
    original = adapter._persist

    def fail_persist():
        raise OSError("read-only")

    monkeypatch.setattr(adapter, "_persist", fail_persist)
    result = adapter.apply_series_control_patch({"search_top_k": 12}, expected_revision=0)
    assert result["reason"] == "APPLY_FAILED_ROLLED_BACK"
    assert adapter._overlay == {}
    assert adapter._revision == 0
    monkeypatch.setattr(adapter, "_persist", original)


def test_reset_failure_restores_previous_overlay_and_revision(tmp_path, monkeypatch):
    plugin = _Plugin(tmp_path)
    adapter = SeriesControlAdapter(plugin)
    adapter.apply_series_control_patch({"search_top_k": 12}, expected_revision=0)
    original = adapter._persist

    def fail_persist():
        raise OSError("read-only")

    monkeypatch.setattr(adapter, "_persist", fail_persist)
    result = adapter.reset_series_control_override(expected_revision=1)
    assert result["reason"] == "APPLY_FAILED_ROLLED_BACK"
    assert adapter._overlay == {"search_top_k": 12}
    assert adapter._revision == 1
    monkeypatch.setattr(adapter, "_persist", original)
