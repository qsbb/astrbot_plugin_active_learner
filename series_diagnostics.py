"""Isolated, bounded diagnostics for the series maintenance page.

Follows the same pattern as astrbot_plugin_relationship/series_diagnostics.py.
The active_learner plugin also maintains its own _BufferHandler in web_api.py
for backward compatibility; this module provides the standard series-level
diagnostic_event / diagnostic_events / diagnostic_clear interface that other
凝心溯溪 plugins expect.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

PLUGIN_ID = "astrbot_plugin_active_learner"
PLUGIN_NAME = "知"
DIAGNOSTIC_CONTRACT = "series.diagnostics@1.0"
_MAX_EVENTS = 1000
_SENSITIVE_KEY = re.compile(
    r"(?:token|api[_-]?key|secret|password|authorization|cookie|umo|"
    r"user[_-]?id|group[_-]?id|platform[_-]?id|"
    r"^(?:account|person|session|requester|recipient|target|identity|filename|file[_-]?path|path|location|latitude|longitude|prompt|response|reply|query|topic|content|message|claim|snippet|url|new_settings)(?:[_-]?id)?$|"
    r"^scope(?:[_-]?id)?$)",
    re.IGNORECASE,
)
_LONG_NUMBER = re.compile(r"(?<![\w.])[0-9]{6,}(?![\w.])")
_ACTOR_ID = re.compile(
    r"(?i)\b(?:user|group|account|person|session)[-_:][A-Za-z0-9_-]+\b"
)
_URL_QUERY = re.compile(r"(https?://[^\s?]+)\?[^\s]+", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_OPAQUE_VALUE = re.compile(
    r"(?<![\w])(?=[A-Za-z0-9_-]{20,}(?![\w]))"
    r"(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])"
    r"[A-Za-z0-9_-]+"
)
_SECRET_VALUE = re.compile(
    r"(?i)(token|api[_-]?key|secret|password|authorization|cookie|umo|"
    r"user[_-]?id|group[_-]?id|platform[_-]?id)(?:\s*[:=]\s*|\s+)"
    r"(?:bearer\s+)?([^,\s]+)"
)
_PRIVATE_VALUE = re.compile(
    r"(?i)(user_text|prompt|response|reply|query|topic|content|scope|message|"
    r"new_settings)\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|[^,\s]+)"
)


def _safe_text(value: Any, *, limit: int = 320) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = _SECRET_VALUE.sub(r"\1=<已隐藏>", text)
    text = _PRIVATE_VALUE.sub(r"\1=<已隐藏>", text)
    text = _EMAIL.sub("<已隐藏邮箱>", text)
    text = _OPAQUE_VALUE.sub("<已隐藏随机标识>", text)
    text = _ACTOR_ID.sub("<已隐藏标识>", text)
    text = _URL_QUERY.sub(r"\1?[已隐藏参数]", text)
    text = _URL.sub("<已隐藏网址>", text)
    text = _LONG_NUMBER.sub("<已隐藏标识>", text)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _safe_details(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in details.items():
        name = str(key)[:64]
        if _SENSITIVE_KEY.search(name):
            continue
        if isinstance(value, bool | int | float) or value is None:
            result[name] = value
        elif isinstance(value, (str, bytes)):
            result[name] = _safe_text(
                value.decode(errors="replace") if isinstance(value, bytes) else value,
                limit=2000 if name.lower() == "log_detail" else 160,
            )
        elif isinstance(value, (list, tuple)):
            result[name] = [_safe_text(item, limit=80) for item in value[:8]]
    return result


class DiagnosticBuffer(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
        self._stream_id = uuid.uuid4().hex
        self._sequence = 0
        self._lock = threading.Lock()

    def append(
        self, level: str, code: str, summary: Any, details: Any = None
    ) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            event = {
                "seq": self._sequence,
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "plugin_id": PLUGIN_ID,
                "plugin_name": PLUGIN_NAME,
                "level": str(level).upper(),
                "code": _safe_text(code, limit=80),
                "summary": _safe_text(summary),
                "details": _safe_details(details),
            }
            self._events.append(event)
            return event

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name or not record.name.startswith(PLUGIN_ID):
            return
        try:
            module = _safe_text(record.module or "plugin", limit=40)
            details: dict[str, Any] = {
                "module": module,
                "function": _safe_text(record.funcName or "", limit=60),
                "line": max(0, int(record.lineno or 0)),
            }
            if record.getMessage():
                details["log_detail"] = _safe_text(record.getMessage(), limit=2000)
            if record.exc_info and record.exc_info[0] is not None:
                details["exception_type"] = record.exc_info[0].__name__
            self.append(
                record.levelname,
                f"logger.{record.levelname.lower()}.{module}",
                f"{module} recorded a {record.levelname} diagnostic event",
                details,
            )
        except Exception:
            pass

    def snapshot(self, *, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        after, size = max(0, int(after_seq or 0)), min(1000, max(1, int(limit or 200)))
        with self._lock:
            pending = [item for item in self._events if item["seq"] > after]
            # 只返回最早窗口，消费者按 next_seq 继续追平；绝不能跳到全局
            # 最新 seq，否则中间积压事件会被静默跳过。
            events = pending[:size]
            has_more = len(pending) > size
            first = self._events[0]["seq"] if self._events else self._sequence + 1
            return {
                "contract": DIAGNOSTIC_CONTRACT,
                "plugin_id": PLUGIN_ID,
                "plugin_name": PLUGIN_NAME,
                "stream_id": self._stream_id,
                "events": events,
                "next_seq": events[-1]["seq"] if events else self._sequence,
                "dropped_before": max(0, first - 1),
                "has_more": has_more,
                "truncated": has_more,
            }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._stream_id = uuid.uuid4().hex


_buffer = DiagnosticBuffer()
_logger = logging.getLogger(PLUGIN_ID)
_logger.setLevel(logging.DEBUG)


def isolate_logger() -> None:
    _logger.propagate = False
    for handler in list(_logger.handlers):
        if not isinstance(handler, (logging.NullHandler, DiagnosticBuffer)):
            _logger.removeHandler(handler)
    if not any(isinstance(handler, logging.NullHandler) for handler in _logger.handlers):
        _logger.addHandler(logging.NullHandler())
    if _buffer not in _logger.handlers:
        _logger.addHandler(_buffer)


isolate_logger()


def diagnostic_event(
    code: str, summary: Any, *, level: str = "INFO", details: Any = None
) -> dict[str, Any]:
    isolate_logger()
    return _buffer.append(level, code, summary, details)


def diagnostic_events(*, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
    return _buffer.snapshot(after_seq=after_seq, limit=limit)


def diagnostic_clear() -> None:
    _buffer.clear()

# ---------------------------------------------------------------- 联动健康（series.diagnostics@1.1）
# 事件流只做历史；当前状态由 diagnostic_state_payload() 纯读返回。
# 唯一 writer 原则：谁调用谁记录；只有状态迁移才发一条事件（heartbeat 不进事件流）。

LINK_STATE_LIMIT = 64
_link_states: dict[str, dict[str, Any]] = {}
_link_lock = threading.Lock()


def _link_now() -> str:
    try:
        return datetime.now(UTC).isoformat(timespec="seconds")
    except Exception:  # pragma: no cover - 兜底
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_link_state(
    link_id: Any,
    *,
    state: str,
    peer_plugin_id: str = "",
    contract: str = "",
    contract_version: str = "",
    method: str = "",
    reason_code: str = "",
    fallback: str = "",
) -> dict[str, Any]:
    """记录一条联动链路的当前状态；状态迁移时发一条诊断事件。"""
    key = _safe_text(link_id, limit=120)
    if not key:
        return {}
    normalized = str(state or "unknown").strip().lower() or "unknown"
    now_text = _link_now()
    with _link_lock:
        previous = _link_states.get(key)
        changed = (
            previous is None
            or previous.get("state") != normalized
            or previous.get("reason_code") != str(reason_code or "")
            or previous.get("contract_version") != str(contract_version or "")
        )
        entry = {
            "state": normalized,
            "since": (previous or {}).get("since", now_text) if previous and not changed else now_text,
            "last_attempt_at": now_text,
            "last_success_at": (
                now_text
                if normalized == "ready"
                else str((previous or {}).get("last_success_at", ""))
            ),
            "reason_code": _safe_text(reason_code, limit=80),
            "peer_plugin_id": _safe_text(peer_plugin_id, limit=120),
            "contract": _safe_text(contract, limit=120),
            "contract_version": _safe_text(contract_version, limit=40),
            "method": _safe_text(method, limit=80),
            "fallback": _safe_text(fallback, limit=120),
            "consecutive_failures": (
                0
                if normalized == "ready"
                else int((previous or {}).get("consecutive_failures", 0)) + 1
            ),
            "observed_at": now_text,
        }
        _link_states[key] = entry
        if len(_link_states) > LINK_STATE_LIMIT:
            oldest = min(
                _link_states.items(), key=lambda item: item[1].get("observed_at", "")
            )
            _link_states.pop(oldest[0], None)
    if changed:
        level = "INFO" if normalized == "ready" else "WARNING"
        diagnostic_event(
            f"contract.link.{normalized}",
            f"{key} -> {normalized}",
            level=level,
            details={
                "link_id": key,
                "state": normalized,
                "peer_plugin_id": entry["peer_plugin_id"],
                "contract": entry["contract"],
                "contract_version": entry["contract_version"],
                "method": entry["method"],
                "reason_code": entry["reason_code"],
                "fallback": entry["fallback"],
                "consecutive_failures": entry["consecutive_failures"],
            },
        )
    return dict(entry)


def diagnostic_state_payload() -> dict[str, Any]:
    """返回当前联动状态快照（纯读：不写事件、不改计数）。"""
    with _link_lock:
        links = {key: dict(value) for key, value in _link_states.items()}
    return {
        "contract": "series.diagnostics@1.1",
        "plugin_id": PLUGIN_ID,
        "plugin_name": PLUGIN_NAME,
        "stream_id": str(getattr(_buffer, "_stream_id", "")),
        "observed_at": _link_now(),
        "links": links,
    }
