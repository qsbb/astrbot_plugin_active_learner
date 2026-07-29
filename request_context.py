"""凝心溯溪系列统一请求上下文（``ningxin.request_context.v1``）。

本模块是系列共享契约的**字节一致副本**：六个插件各自仓库独立发布，无法共享
import，故以同名文件分发，由系列级测试 ``tests/test_series_request_context.py``
静态断言各副本内容完全一致。修改时必须同步全部副本。

设计约束（与 CONVENTIONS.md 第 12 节一致）：

- 载体是 ``event.set_extra`` / ``event.get_extra`` 上键为 ``ningxin.request_context.v1``
  的**普通 dict**。禁止放入插件实例、dataclass、自定义类对象，只允许 JSON 可
  表达的普通值。跨插件传对象会把一方的内部实现变成另一方的隐式依赖，热重载后
  还会出现「同名不同类」的 isinstance 失败。
- 任一插件都可**惰性创建**：先到者建骨架，后到者复用，不依赖加载顺序，也不需要
  一个「必须先跑」的初始化插件。
- **字段单写者**：``flags`` / ``artifacts`` / ``diagnostics`` 按 owner 分区，
  每个插件只写自己的分区。``version`` / ``request_id`` 创建后不可变。``phase``
  是唯一的多写者字段，但只允许按 ``PHASE_ORDER`` 单调前进，不可回退。
"""

from __future__ import annotations

import math
import uuid
from typing import Any

REQUEST_CONTEXT_CONTRACT_NAME = "ningxin.request_context"
REQUEST_CONTEXT_CONTRACT_VERSION = "1.0"
REQUEST_CONTEXT_EXTRA_KEY = "ningxin.request_context.v1"
REQUEST_CONTEXT_VERSION = 1

PHASE_CREATED = "created"
PHASE_MESSAGE = "message"
PHASE_COMMAND = "command"
PHASE_LLM_REQUEST = "llm_request"
PHASE_LLM_RESPONSE = "llm_response"
PHASE_DECORATING_RESULT = "decorating_result"

# 数值越大越晚发生；phase 只能单调前进，避免并发钩子把阶段写回早期值。
PHASE_ORDER: dict[str, int] = {
    PHASE_CREATED: 0,
    PHASE_MESSAGE: 10,
    PHASE_COMMAND: 20,
    PHASE_LLM_REQUEST: 30,
    PHASE_LLM_RESPONSE: 40,
    PHASE_DECORATING_RESULT: 50,
}

OWNER_ACTIVE_LEARNER = "active_learner"
OWNER_CONVERSATION_FLOW = "conversation_flow"
OWNER_IDENTITY_GUARDIAN = "identity_guardian"
OWNER_RELATIONSHIP = "relationship"
OWNER_UPDATE_MANAGER = "update_manager"
OWNER_VOICE_HUB = "voice_hub"

KNOWN_OWNERS: frozenset[str] = frozenset(
    {
        OWNER_ACTIVE_LEARNER,
        OWNER_CONVERSATION_FLOW,
        OWNER_IDENTITY_GUARDIAN,
        OWNER_RELATIONSHIP,
        OWNER_UPDATE_MANAGER,
        OWNER_VOICE_HUB,
    }
)

# 单个 owner 的原因码上限。原因码用于诊断，不做业务判定，无需无界增长。
MAX_REASONS_PER_OWNER = 32
MAX_PROMPT_FRAGMENTS_PER_OWNER = 16
MAX_PROMPT_FRAGMENT_CHARS = 24000
PROMPT_FRAGMENTS_ARTIFACT = "prompt_fragments"

class RequestContextError(RuntimeError):
    """上下文契约被违反（越界写入、非法值、phase 回退等）。"""


def _is_plain_value(value: Any, _depth: int = 0) -> bool:
    """判断是否为可放入上下文的普通值。

    只接受 None / bool / int / 有限 float / str 以及由它们构成的 list / dict
    （dict 键必须是 str）。显式拒绝 tuple、非有限浮点数、插件实例、dataclass
    实例与任意自定义对象，确保值能按严格 JSON 语义跨插件传递。
    """
    if _depth > 6:
        return False
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_plain_value(item, _depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_plain_value(item, _depth + 1)
            for key, item in value.items()
        )
    return False


def _require_plain(value: Any, label: str) -> None:
    if not _is_plain_value(value):
        raise RequestContextError(
            f"{label} must be a plain JSON-like value; "
            f"plugin instances and dataclasses are rejected (got {type(value).__name__})"
        )


def _require_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner:
        raise RequestContextError("owner must be a non-empty str")
    return owner


def new_request_id() -> str:
    """生成请求标识。不含会话内容，可安全写入日志。"""
    return uuid.uuid4().hex


def new_context(request_id: str | None = None, phase: str = PHASE_CREATED) -> dict[str, Any]:
    """构造一个空骨架（不绑定 event），供惰性创建与测试使用。"""
    if phase not in PHASE_ORDER:
        raise RequestContextError(f"unknown phase: {phase!r}")
    return {
        "version": REQUEST_CONTEXT_VERSION,
        "request_id": request_id or new_request_id(),
        "phase": phase,
        "flags": {},
        "artifacts": {},
        "diagnostics": {},
    }


def is_valid_context(context: Any) -> bool:
    """校验形状。版本不符或结构异常时返回 False，调用方应重建而非强行使用。"""
    if not isinstance(context, dict):
        return False
    if context.get("version") != REQUEST_CONTEXT_VERSION:
        return False
    if not isinstance(context.get("request_id"), str) or not context["request_id"]:
        return False
    if context.get("phase") not in PHASE_ORDER:
        return False
    return all(
        isinstance(context.get(section), dict)
        for section in ("flags", "artifacts", "diagnostics")
    )


def get_event_extra(event: Any, key: str) -> Any:
    """读取 event extra，兼容缺少 ``get_extra`` 的旧框架与测试桩。"""
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            pass
    return getattr(event, key, None)


def set_event_extra(event: Any, key: str, value: Any) -> None:
    """写入 event extra，兼容缺少 ``set_extra`` 的旧框架与测试桩。"""
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        try:
            setter(key, value)
            return
        except Exception:
            pass
    try:
        setattr(event, key, value)
    except Exception:
        pass


def peek_context(event: Any) -> dict[str, Any] | None:
    """只读探测：存在且合法时返回，否则 None。不创建、不修改。"""
    context = get_event_extra(event, REQUEST_CONTEXT_EXTRA_KEY)
    return context if is_valid_context(context) else None


def ensure_context(event: Any, phase: str | None = None) -> dict[str, Any]:
    """惰性获取或创建上下文，并可选地推进 phase。

    任一插件在任一钩子里调用都安全：先到者创建骨架，后到者原地复用同一 dict。
    发现残留的非法值（如旧版本、被覆写成非 dict）时重建，不抛异常打断业务链路。
    """
    context = get_event_extra(event, REQUEST_CONTEXT_EXTRA_KEY)
    if not is_valid_context(context):
        context = new_context(phase=phase or PHASE_CREATED)
        set_event_extra(event, REQUEST_CONTEXT_EXTRA_KEY, context)
    else:
        # 幂等回写：某些平台的 extra 容器会在链路中被替换，回写保证键仍在。
        set_event_extra(event, REQUEST_CONTEXT_EXTRA_KEY, context)
    if phase is not None:
        advance_phase(context, phase)
    return context


def advance_phase(context: dict[str, Any], phase: str) -> str:
    """按 PHASE_ORDER 单调推进 phase；试图回退时保持原值。"""
    if phase not in PHASE_ORDER:
        raise RequestContextError(f"unknown phase: {phase!r}")
    current = context.get("phase", PHASE_CREATED)
    current_rank = PHASE_ORDER.get(current, 0)
    if PHASE_ORDER[phase] > current_rank:
        context["phase"] = phase
    return context["phase"]


def _owner_section(context: dict[str, Any], section: str, owner: str) -> dict[str, Any]:
    _require_owner(owner)
    bucket = context.setdefault(section, {})
    if not isinstance(bucket, dict):
        bucket = {}
        context[section] = bucket
    owned = bucket.setdefault(owner, {})
    if not isinstance(owned, dict):
        owned = {}
        bucket[owner] = owned
    return owned


def set_flag(context: dict[str, Any], owner: str, name: str, value: Any) -> None:
    """写入自身分区的 flag。仅接受普通值。"""
    _require_plain(value, f"flag {owner}.{name}")
    _owner_section(context, "flags", owner)[name] = value


def get_flag(context: dict[str, Any], owner: str, name: str, default: Any = None) -> Any:
    """读取任意 owner 的 flag（跨插件只读是允许的）。"""
    bucket = context.get("flags")
    if not isinstance(bucket, dict):
        return default
    owned = bucket.get(owner)
    if not isinstance(owned, dict):
        return default
    return owned.get(name, default)


def set_artifact(context: dict[str, Any], owner: str, name: str, value: Any) -> None:
    """写入自身分区的产物。仅接受普通值，禁止放实例或数据类。"""
    _require_plain(value, f"artifact {owner}.{name}")
    _owner_section(context, "artifacts", owner)[name] = value


def get_artifact(
    context: dict[str, Any], owner: str, name: str, default: Any = None
) -> Any:
    bucket = context.get("artifacts")
    if not isinstance(bucket, dict):
        return default
    owned = bucket.get(owner)
    if not isinstance(owned, dict):
        return default
    return owned.get(name, default)


def add_prompt_fragment(
    context: dict[str, Any],
    owner: str,
    key: str,
    content: str,
    *,
    priority: int = 100,
    source: str = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Register one deterministic prompt fragment in the owner's artifact area.

    Re-registering the same key replaces its content without changing insertion order.
    Empty or oversized content is rejected so diagnostics cannot become an unbounded
    second prompt store.
    """
    _require_owner(owner)
    if not isinstance(key, str) or not key.strip():
        raise RequestContextError("prompt fragment key must be a non-empty str")
    if not isinstance(content, str):
        raise RequestContextError("prompt fragment content must be a str")
    content = content.strip()
    if not content:
        return False
    if len(content) > MAX_PROMPT_FRAGMENT_CHARS:
        raise RequestContextError("prompt fragment content exceeds size limit")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise RequestContextError("prompt fragment priority must be an int")
    if metadata is None:
        metadata = {}
    elif not isinstance(metadata, dict):
        raise RequestContextError("prompt fragment metadata must be a dict")
    else:
        metadata = dict(metadata)
    _require_plain(metadata, f"prompt fragment {owner}.{key} metadata")

    owned = _owner_section(context, "artifacts", owner)
    fragments = owned.setdefault(PROMPT_FRAGMENTS_ARTIFACT, [])
    if not isinstance(fragments, list):
        fragments = []
        owned[PROMPT_FRAGMENTS_ARTIFACT] = fragments

    normalized_key = key.strip()
    payload = {
        "key": normalized_key,
        "content": content,
        "priority": priority,
        "source": source.strip() if isinstance(source, str) else owner,
        "metadata": metadata,
    }
    for index, item in enumerate(fragments):
        if isinstance(item, dict) and item.get("key") == normalized_key:
            payload["index"] = int(item.get("index", index))
            fragments[index] = payload
            return True
    if len(fragments) >= MAX_PROMPT_FRAGMENTS_PER_OWNER:
        return False
    payload["index"] = len(fragments)
    fragments.append(payload)
    return True


def get_prompt_fragments(
    context: dict[str, Any], owners: list[str] | tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    """Return validated, sorted and deduplicated prompt fragments."""
    artifacts = context.get("artifacts")
    if not isinstance(artifacts, dict):
        return []
    selected = set(owners) if owners is not None else None
    collected: list[dict[str, Any]] = []
    for owner, owned in artifacts.items():
        if selected is not None and owner not in selected:
            continue
        if not isinstance(owner, str) or not isinstance(owned, dict):
            continue
        fragments = owned.get(PROMPT_FRAGMENTS_ARTIFACT)
        if not isinstance(fragments, list):
            continue
        for fallback_index, item in enumerate(fragments):
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            content = item.get("content")
            priority = item.get("priority", 100)
            if (
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(content, str)
                or not content.strip()
                or isinstance(priority, bool)
                or not isinstance(priority, int)
            ):
                continue
            raw_index = item.get("index", fallback_index)
            index = (
                raw_index
                if isinstance(raw_index, int) and not isinstance(raw_index, bool)
                else fallback_index
            )
            raw_metadata = item.get("metadata")
            metadata = (
                raw_metadata
                if isinstance(raw_metadata, dict) and _is_plain_value(raw_metadata)
                else {}
            )
            source = item.get("source")
            collected.append(
                {
                    "owner": owner,
                    "key": key.strip(),
                    "content": content.strip(),
                    "priority": priority,
                    "source": source if isinstance(source, str) and source else owner,
                    "index": index,
                    "metadata": metadata,
                }
            )

    collected.sort(
        key=lambda item: (item["priority"], item["owner"], item["index"], item["key"])
    )
    rendered: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_content: set[str] = set()
    for item in collected:
        normalized_key = item["key"].casefold()
        normalized_content = " ".join(item["content"].split())
        if normalized_key in seen_keys or normalized_content in seen_content:
            continue
        seen_keys.add(normalized_key)
        seen_content.add(normalized_content)
        rendered.append(item)
    return rendered


def render_prompt_fragments(
    context: dict[str, Any], owners: list[str] | tuple[str, ...] | None = None
) -> dict[str, Any]:
    """Render registered fragments and expose a plain diagnostic description."""
    fragments = get_prompt_fragments(context, owners)
    text = "\n\n".join(item["content"] for item in fragments)
    return {
        "text": text,
        "chars": len(text),
        "fragments": [
            {
                "owner": item["owner"],
                "key": item["key"],
                "priority": item["priority"],
                "source": item["source"],
                "chars": len(item["content"]),
                "metadata": item["metadata"],
            }
            for item in fragments
        ],
    }


def add_reason(context: dict[str, Any], owner: str, reason: str) -> list[str]:
    """追加自身分区的原因码：大写稳定码，去重，超上限后丢弃新值。

    原因码只用于诊断与测试断言，不参与业务判定，因此不含用户内容、配置值或秘密。
    """
    if not isinstance(reason, str) or not reason:
        raise RequestContextError("reason must be a non-empty str")
    owned = _owner_section(context, "diagnostics", owner)
    reasons = owned.setdefault("reasons", [])
    if not isinstance(reasons, list):
        reasons = []
        owned["reasons"] = reasons
    if reason not in reasons and len(reasons) < MAX_REASONS_PER_OWNER:
        reasons.append(reason)
    return reasons


def get_reasons(context: dict[str, Any], owner: str) -> list[str]:
    bucket = context.get("diagnostics")
    if not isinstance(bucket, dict):
        return []
    owned = bucket.get(owner)
    if not isinstance(owned, dict):
        return []
    reasons = owned.get("reasons")
    return list(reasons) if isinstance(reasons, list) else []


def note(event: Any, owner: str, reason: str, phase: str | None = None) -> dict[str, Any]:
    """一行式便捷入口：惰性建上下文 + 推进 phase + 记一个原因码。"""
    context = ensure_context(event, phase)
    add_reason(context, owner, reason)
    return context
