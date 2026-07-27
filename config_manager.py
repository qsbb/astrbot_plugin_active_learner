"""统一配置管理。

职责：
- 封装两层配置源：AstrBot 插件配置页（_conf_schema.json）→ Dashboard overlay
- 对外提供统一的 get/set/update/all 接口
- 每次 update 立即原子持久化并应用到运行时

本模块是插件唯一的配置来源：Dashboard 设置页与运行时读取同一份数据，
避免出现「页面显示与实际生效的 Provider 不一致」。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional



class ConfigManager:
    """统一配置管理器。

    数据源优先级（高 → 低）：
    1. update()/set() 写入的自管存储（Dashboard 设置页面）
    2. AstrBot 插件配置页（_conf_schema.json）
    3. 代码默认值

    用法：
        cfg = ConfigManager(data_dir, plugin_config)
        val = cfg.get("learn_weight", 0.7)
        updated = cfg.update(learn_weight=0.9)  # 立即持久化
    """

    # overlay 文件中保存「写入当时 AstrBot 侧取值」的键名。
    # 不属于用户配置，读写时都要排除。
    _BASELINE_KEY = "__astrbot_baseline__"

    def __init__(
        self,
        data_dir: Path,
        plugin_config: dict[str, Any],
        native_config: Any = None,
    ):
        self._path = data_dir / "active_learner_settings.json"
        self._lock = threading.Lock()
        # 全局配置 + 默认值
        self._astrbot_cfg: dict[str, Any] = {}
        # Dashboard 存储覆盖层
        self._overlay: dict[str, Any] = {}
        # 写入 overlay 当时 AstrBot 插件配置页的取值快照
        self._baseline: dict[str, Any] = {}
        # AstrBot 侧的原始取值（不含 overlay），用于生成 baseline。
        # 必须与 _astrbot_cfg 分开：后者会被 overlay 和 update() 覆盖，
        # 用它取 baseline 会把「管理页刚写入的值」误记成「插件配置页的值」。
        self._plugin_config: dict[str, Any] = {}
        # AstrBot 原生配置对象（带 save_config），用于把管理页修改回写插件配置页
        self._native_config = (
            native_config if hasattr(native_config, "save_config") else None
        )
        self._load()

        # 合并初始：Dashboard 设置覆盖 AstrBot 配置
        self._merge_initial(plugin_config)

    def _load(self) -> None:
        """从磁盘加载 Dashboard 存储。"""
        with self._lock:
            try:
                if self._path.exists():
                    raw = self._path.read_text(encoding="utf-8")
                    data = json.loads(raw) if raw.strip() else {}
                    if isinstance(data, dict):
                        raw_baseline = data.pop(self._BASELINE_KEY, None)
                        self._baseline = (
                            dict(raw_baseline) if isinstance(raw_baseline, dict) else {}
                        )
                        self._overlay = data
                    else:
                        self._overlay = {}
                        self._baseline = {}
                else:
                    self._overlay = {}
                    self._baseline = {}
            except (OSError, json.JSONDecodeError, ValueError):
                self._overlay = {}
                self._baseline = {}

    def _merge_initial(self, plugin_config: dict[str, Any]) -> None:
        """初始化时合并两层配置，并让「更晚一次的修改」胜出。

        overlay（管理页写入）默认优先于 AstrBot 插件配置页。但若只有这一条规则，
        用户在 AstrBot 插件配置页改的值会被旧 overlay 永久压制——页面上显示新值、
        实际运行仍是旧值，表现为「配置页怎么改都没用」。

        解决办法：写入 overlay 时顺带记录当时 AstrBot 侧的取值（baseline）。
        本次启动若发现 AstrBot 侧的值已不等于 baseline，说明用户后来在插件配置页
        改过它，此时丢弃该字段的过期 overlay，以插件配置页为准。
        """
        self._astrbot_cfg = dict(plugin_config)
        self._plugin_config = dict(plugin_config)

        stale = [
            key
            for key in self._overlay
            if key in self._baseline
            and key in plugin_config
            and plugin_config[key] != self._baseline[key]
        ]
        for key in stale:
            self._overlay.pop(key, None)
            self._baseline.pop(key, None)
        if stale:
            self._persist_unlocked()

        self._astrbot_cfg.update(
            {k: v for k, v in self._overlay.items() if v is not None}
        )

    def get(self, key: str, default: Any = None) -> Any:
        """读取配置。

        优先级：overlay（Dashboard 写入） > astrbot_cfg（插件配置页） > default
        """
        with self._lock:
            # 1. Dashboard 覆盖
            if key in self._overlay:
                val = self._overlay[key]
                if val is not None:
                    return val
            # 2. AstrBot 配置
            if key in self._astrbot_cfg:
                return self._astrbot_cfg[key]
        return default

    def set(self, key: str, value: Any) -> None:
        """设置单值并持久化。"""
        self.update(**{key: value})

    def get_int(
        self, key: str, default: int = 0, min_val: Optional[int] = None, max_val: Optional[int] = None
    ) -> int:
        """读取整型配置，带范围钳制。"""
        try:
            val = int(self.get(key, default))
            if min_val is not None:
                val = max(min_val, val)
            if max_val is not None:
                val = min(max_val, val)
            return val
        except (TypeError, ValueError):
            return default

    def get_float(
        self, key: str, default: float = 0.0,
        min_val: Optional[float] = None, max_val: Optional[float] = None,
    ) -> float:
        """读取浮点配置，带范围钳制。"""
        try:
            val = float(self.get(key, default))
            if min_val is not None:
                val = max(min_val, val)
            if max_val is not None:
                val = min(max_val, val)
            return val
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """读取布尔配置。"""
        try:
            return bool(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def update(self, **kwargs: Any) -> dict[str, Any]:
        """合并写入并立即持久化。返回更新后的全量设置（含 overlay 层）。

        持久化原子性：先写 .tmp 再 os.replace。
        """
        with self._lock:
            # 过滤 None：None 表示调用方不更新该字段（部分更新模式）。
            # 清空字段应传空字符串 ""（"" 非 None，能正常通过）。
            filtered = {k: v for k, v in kwargs.items() if v is not None}
            self._overlay.update(filtered)
            self._astrbot_cfg.update(filtered)

            # 回写 AstrBot 插件配置页，让两个页面显示同一份值。
            # 成功回写的字段，其 baseline 直接记为新值：此后插件配置页若再被改动，
            # 就能被识别为「更晚的修改」并压过 overlay。
            native_ok = self._write_native(filtered)
            for key, val in filtered.items():
                if native_ok:
                    # 插件配置页已同步为新值
                    self._plugin_config[key] = val
                    self._baseline[key] = val
                elif key in self._plugin_config:
                    # 回写不可用：AstrBot 侧仍是旧值，如实记录
                    self._baseline[key] = self._plugin_config[key]
                else:
                    # AstrBot 侧本就没有该字段，不记基线。
                    # 否则下次启动 schema 默认值一出现就会被误判成「用户改过配置页」。
                    self._baseline.pop(key, None)

            self._persist_unlocked()
            return dict(self._overlay)

    def _write_native(self, values: dict[str, Any]) -> bool:
        """把配置回写到 AstrBot 原生配置对象并持久化。

        返回是否写入成功。原生对象不可用（旧版 AstrBot 或测试环境）时返回 False，
        此时仅依赖 overlay 层，行为与修复前一致。
        """
        if self._native_config is None or not values:
            return False
        try:
            self._native_config.update(values)
            self._native_config.save_config()
            return True
        except Exception:
            return False

    def _persist_unlocked(self) -> None:
        """原子落盘 overlay + baseline。调用方需已持有锁。"""
        payload = dict(self._overlay)
        if self._baseline:
            payload[self._BASELINE_KEY] = self._baseline
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def all(self) -> dict[str, Any]:
        """返回合并后的全量设置（overlay + astrbot_cfg）。"""
        with self._lock:
            merged = dict(self._astrbot_cfg)
            merged.update({k: v for k, v in self._overlay.items() if v is not None})
            return merged

    def overlay_all(self) -> dict[str, Any]:
        """仅返回 Dashboard 写入的 overlay 层（不含 AstrBot 配置默认值）。"""
        with self._lock:
            return dict(self._overlay)
