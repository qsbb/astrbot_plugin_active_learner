"""插件自管的持久化设置存储。

路径：StarTools.get_data_dir() / "active_learner_settings.json"
设计：threading.Lock 保证并发安全 + 原子 os.replace 写入
容错：文件缺失/损坏时返回 {}，不抛异常
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class SettingsStore:
    """插件设置存储。所有读写都在锁保护下进行。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> dict[str, Any]:
        """从磁盘加载设置。文件缺失或损坏时返回 {}，不抛异常。"""
        with self._lock:
            try:
                if self._path.exists():
                    raw = self._path.read_text(encoding="utf-8")
                    data = json.loads(raw) if raw.strip() else {}
                    if not isinstance(data, dict):
                        data = {}
                    self._data = data
            except (OSError, json.JSONDecodeError, ValueError):
                self._data = {}
            return dict(self._data)

    def save(self, data: dict[str, Any]) -> None:
        """原子写入：先写 .tmp，再 os.replace 替换。"""
        with self._lock:
            self._data = dict(data)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self._path)
            except OSError:
                # 写失败不抛，下次启动会从盘上重新加载
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def update(self, **kwargs: Any) -> dict[str, Any]:
        """合并写入并立即持久化。返回更新后的全量设置。"""
        with self._lock:
            self._data.update({k: v for k, v in kwargs.items() if v is not None})
            # 立即持久化（重用 save 的原子写逻辑，但 save 会重置 _data，所以直接复制逻辑）
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self._path)
            except OSError:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
            return dict(self._data)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)
