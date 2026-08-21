"""``series.control@1.0`` adapter for the knowledge plugin.

The adapter exposes only three non-secret runtime controls.  Its managed
overlay is deliberately separate from the plugin's native ConfigManager
values, so an unavailable controller or an invalid snapshot falls back to
native settings.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


CONTRACT_NAME = "series.control@1.0"
PLUGIN_ID = "astrbot_plugin_active_learner"
SERIES_ID = "ningxin_suxi"

_FIELDS: dict[str, dict[str, Any]] = {
    "embedding_enabled": {
        "type": "bool",
        "default": True,
        "minimum": None,
        "maximum": None,
    },
    "context_inject_count": {
        "type": "int",
        "default": 3,
        "minimum": 1,
        "maximum": 3,
    },
    "search_top_k": {
        "type": "int",
        "default": 5,
        "minimum": 1,
        "maximum": 20,
    },
}


class SeriesControlAdapter:
    """Implement the plugin-owned side of the series control contract."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        data_dir = Path(getattr(plugin, "_db_path", Path.cwd())).parent
        self._path = data_dir / "series-control.json"
        self._overlay: dict[str, Any] = {}
        self._revision = 0
        self._mode = "native"
        self._load()

    def _load(self) -> None:
        """Load only schema-valid overrides; malformed state fails closed."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            self._overlay, self._revision = {}, 0
            return
        if not isinstance(raw, dict):
            self._overlay, self._revision = {}, 0
            return

        revision = raw.get("revision", 0)
        self._revision = (
            revision
            if isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 0
            else 0
        )
        overrides = raw.get("overrides")
        self._overlay = self._clean_values(
            overrides if isinstance(overrides, dict) else {}
        )
        if self._overlay:
            self._mode = "managed"

    @staticmethod
    def _clean_values(values: dict[str, Any]) -> dict[str, Any]:
        """Return schema-valid values and discard unknown/invalid entries."""
        clean: dict[str, Any] = {}
        for name, value in values.items():
            spec = _FIELDS.get(name)
            if spec is None:
                continue
            if spec["type"] == "bool":
                if isinstance(value, bool):
                    clean[name] = value
                continue
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and spec["minimum"] <= value <= spec["maximum"]
            ):
                clean[name] = value
        return clean

    def _persist(self) -> None:
        """Atomically persist the managed overlay."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="series-control-", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "schema_version": 1,
                        "revision": self._revision,
                        "overrides": self._overlay,
                    },
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _native(self, field: str) -> Any:
        manager = getattr(self.plugin, "config_manager", None)
        native_get = getattr(manager, "native_get", None)
        if callable(native_get):
            return native_get(field, _FIELDS[field]["default"])
        if manager is not None:
            return manager.get(field, _FIELDS[field]["default"])
        config = getattr(self.plugin, "config", {})
        return config.get(field, _FIELDS[field]["default"])

    def _native_configured(self, field: str) -> bool:
        manager = getattr(self.plugin, "config_manager", None)
        native_has = getattr(manager, "native_has", None)
        if callable(native_has):
            return bool(native_has(field))
        values = getattr(manager, "values", None)
        if isinstance(values, dict):
            return field in values
        plugin_config = getattr(manager, "_plugin_config", None)
        if isinstance(plugin_config, dict):
            return field in plugin_config
        config = getattr(self.plugin, "config", {})
        return isinstance(config, dict) and field in config

    def effective_value(self, field: str, *, force_overlay: bool = False) -> Any:
        if field not in _FIELDS:
            raise KeyError(field)
        if force_overlay or self._mode == "managed":
            return self._overlay.get(field, self._native(field))
        return self._native(field)

    def series_control_set_mode(self, mode: str) -> dict[str, Any]:
        self._mode = mode if mode in {"native", "managed"} else "native"
        self.sync_runtime()
        return {"success": True, "mode": self._mode}

    def effective_config(self, *, force_overlay: bool = False) -> dict[str, Any]:
        return {
            field: self.effective_value(field, force_overlay=force_overlay)
            for field in _FIELDS
        }

    def sync_runtime(self, *, force_overlay: bool = False) -> None:
        """Apply effective values through the plugin's runtime hook."""
        effective = self.effective_config(force_overlay=force_overlay)
        hook = getattr(self.plugin, "_apply_series_control_runtime", None)
        if callable(hook):
            hook(effective)
            return
        # Small fallback used by older hosts and unit-test fakes.
        self.plugin._embedding_enabled = bool(effective["embedding_enabled"])
        self.plugin._context_inject_count = max(
            1, min(3, int(effective["context_inject_count"]))
        )
        self.plugin._search_top_k = max(1, min(20, int(effective["search_top_k"])))

    def series_control_contract(self) -> dict[str, Any]:
        return {
            "name": CONTRACT_NAME,
            "version": "1.0",
            "series_id": SERIES_ID,
            "plugin_id": PLUGIN_ID,
            "plugin_name": "知",
            "capabilities": [
                "read_schema",
                "read_snapshot",
                "validate_patch",
                "apply_patch",
                "reset_override",
            ],
            "read_only": False,
            "secrets_in_response": False,
            "max_patch_fields": len(_FIELDS),
        }

    def series_control_schema(self) -> dict[str, Any]:
        fields: dict[str, dict[str, Any]] = {}
        for name, spec in _FIELDS.items():
            field = {
                "type": spec["type"],
                "default": spec["default"],
                "control": "overrideable",
                "secret": False,
                "restart_required": False,
            }
            if spec["minimum"] is not None:
                field["minimum"] = spec["minimum"]
                field["maximum"] = spec["maximum"]
            fields[name] = field
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": "1.0",
            "plugin_id": PLUGIN_ID,
            "revision": self._revision,
            "fields": fields,
        }

    def series_control_snapshot(self) -> dict[str, Any]:
        fields: dict[str, dict[str, Any]] = {}
        for name in _FIELDS:
            managed = name in self._overlay
            fields[name] = {
                "native_configured": self._native_configured(name),
                "managed_configured": managed,
                "effective_source": "managed" if managed else "plugin",
                "effective_value": self.effective_value(name),
            }
        return {"status": "ok", "revision": self._revision, "fields": fields}

    def _validate(self, patch: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        if expected_revision != self._revision:
            return {
                "status": "error",
                "reason": "REVISION_CONFLICT",
                "revision": self._revision,
            }
        if not isinstance(patch, dict) or not patch or len(patch) > len(_FIELDS):
            return {
                "status": "error",
                "reason": "INVALID_PATCH",
                "revision": self._revision,
            }
        unknown = next((name for name in patch if name not in _FIELDS), None)
        if unknown is not None:
            return {
                "status": "error",
                "reason": "UNKNOWN_FIELD",
                "field": str(unknown),
            }
        clean = self._clean_values(patch)
        if len(clean) != len(patch):
            for name, value in patch.items():
                if name not in clean:
                    spec = _FIELDS[name]
                    reason = (
                        "INVALID_TYPE"
                        if spec["type"] == "bool" and not isinstance(value, bool)
                        else "INVALID_VALUE"
                    )
                    return {"status": "error", "reason": reason, "field": name}
        return {
            "status": "ok",
            "reason": "VALID",
            "revision": self._revision,
            "patch": clean,
        }

    def validate_series_control_patch(
        self, patch: dict[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        return self._validate(patch, expected_revision)

    def _restore(self, overlay: dict[str, Any], revision: int) -> None:
        self._overlay = dict(overlay)
        self._revision = revision
        try:
            self._persist()
        except Exception:
            # If the failed write never reached os.replace, the prior atomic
            # file remains intact; runtime restoration is still worthwhile.
            pass
        try:
            self.sync_runtime(force_overlay=True)
        except Exception:
            pass

    def apply_series_control_patch(
        self, patch: dict[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        result = self._validate(patch, expected_revision)
        if result.get("status") != "ok":
            return result
        before_overlay = dict(self._overlay)
        before_revision = self._revision
        self._overlay.update(result["patch"])
        self._mode = "managed"
        self._revision += 1
        try:
            self._persist()
            self.sync_runtime(force_overlay=True)
        except Exception:
            self._restore(before_overlay, before_revision)
            return {
                "status": "error",
                "reason": "APPLY_FAILED_ROLLED_BACK",
                "revision": self._revision,
            }
        return {
            "status": "ok",
            "reason": "APPLIED",
            "revision": self._revision,
            "fields": self.series_control_snapshot()["fields"],
        }

    def reset_series_control_override(
        self,
        fields: list[str] | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if expected_revision is not None and expected_revision != self._revision:
            return {
                "status": "error",
                "reason": "REVISION_CONFLICT",
                "revision": self._revision,
            }
        if fields is not None and not isinstance(fields, list):
            return {
                "status": "error",
                "reason": "INVALID_PATCH",
                "revision": self._revision,
            }
        names = list(self._overlay) if fields is None else fields
        if any(name not in _FIELDS for name in names):
            return {
                "status": "error",
                "reason": "UNKNOWN_FIELD",
                "revision": self._revision,
            }
        before_overlay = dict(self._overlay)
        before_revision = self._revision
        for name in names:
            self._overlay.pop(name, None)
        self._revision += 1
        try:
            self._persist()
            self.sync_runtime()
        except Exception:
            self._restore(before_overlay, before_revision)
            return {
                "status": "error",
                "reason": "APPLY_FAILED_ROLLED_BACK",
                "revision": self._revision,
            }
        return {
            "status": "ok",
            "reason": "RESET",
            "revision": self._revision,
            "fields": self.series_control_snapshot()["fields"],
        }
