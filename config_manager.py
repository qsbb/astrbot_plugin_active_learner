"""统一配置管理。

职责：
- 封装三层配置源：AstrBot config → SettingsStore（Dashboard）→ 运行时缓存
- 对外提供统一的 get/set/update/all 接口
- 每次 update 立即持久化并应用到运行时

消除 config 读取逻辑分散在 __init__、_apply_config_to_runtime、_web_save_settings 的现状。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("active_learner")


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

    def __init__(self, data_dir: Path, plugin_config: dict[str, Any]):
        self._path = data_dir / "active_learner_settings.json"
        self._lock = threading.Lock()
        # 全局配置 + 默认值
        self._astrbot_cfg: dict[str, Any] = {}
        # Dashboard 存储覆盖层
        self._overlay: dict[str, Any] = {}
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
                        self._overlay = data
                    else:
                        self._overlay = {}
                else:
                    self._overlay = {}
            except (OSError, json.JSONDecodeError, ValueError):
                self._overlay = {}

    def _merge_initial(self, plugin_config: dict[str, Any]) -> None:
        """初始化时合并，使 Dashboard 设置覆盖 AstrBot 插件配置。"""
        self._astrbot_cfg = dict(plugin_config)
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
            # 过滤 None（None 表示清空，保留原值）
            filtered = {k: v for k, v in kwargs.items() if v is not None}
            self._overlay.update(filtered)
            self._astrbot_cfg.update(filtered)

            # 原子写入
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(
                    json.dumps(self._overlay, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self._path)
            except OSError:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

            return dict(self._overlay)

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
