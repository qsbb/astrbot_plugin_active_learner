from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    PLUGIN_ROOT / "request_context.py"
    if (PLUGIN_ROOT / "request_context.py").is_file()
    else PLUGIN_ROOT / "core" / "request_context.py"
)
SPEC = importlib.util.spec_from_file_location(
    f"_request_context_under_test_{PLUGIN_ROOT.name}", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
request_context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(request_context)


class Event:
    def __init__(self):
        self.extra = {}

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


def test_contract_identity_and_lazy_reuse():
    event = Event()
    first = request_context.ensure_context(
        event, request_context.PHASE_LLM_REQUEST
    )
    second = request_context.ensure_context(
        event, request_context.PHASE_DECORATING_RESULT
    )

    assert request_context.REQUEST_CONTEXT_CONTRACT_NAME == "ningxin.request_context"
    assert request_context.REQUEST_CONTEXT_CONTRACT_VERSION == "1.0"
    assert first is second
    assert second["phase"] == request_context.PHASE_DECORATING_RESULT


def test_phase_never_moves_backwards():
    context = request_context.new_context(
        request_id="fixed", phase=request_context.PHASE_LLM_RESPONSE
    )

    request_context.advance_phase(context, request_context.PHASE_MESSAGE)

    assert context["phase"] == request_context.PHASE_LLM_RESPONSE
    assert context["request_id"] == "fixed"


def test_owner_namespaces_are_isolated_and_cross_readable():
    context = request_context.new_context()
    request_context.set_flag(context, request_context.OWNER_RELATIONSHIP, "ready", True)
    request_context.set_artifact(
        context,
        request_context.OWNER_CONVERSATION_FLOW,
        "plan",
        {"segments": ["one", "two"]},
    )

    assert request_context.get_flag(
        context, request_context.OWNER_RELATIONSHIP, "ready"
    )
    assert request_context.get_artifact(
        context, request_context.OWNER_CONVERSATION_FLOW, "plan"
    ) == {"segments": ["one", "two"]}
    assert context["flags"][request_context.OWNER_RELATIONSHIP] == {"ready": True}


@pytest.mark.parametrize(
    "value",
    [
        object(),
        ("tuple",),
        math.nan,
        math.inf,
        {"nested": object()},
        {1: "non-string key"},
    ],
)
def test_non_json_values_are_rejected(value):
    context = request_context.new_context()

    with pytest.raises(request_context.RequestContextError):
        request_context.set_artifact(
            context, request_context.OWNER_VOICE_HUB, "invalid", value
        )


def test_invalid_existing_context_is_rebuilt_without_leaking_old_data():
    event = Event()
    event.extra[request_context.REQUEST_CONTEXT_EXTRA_KEY] = {
        "version": 0,
        "request_id": "stale",
        "phase": "unknown",
        "flags": {"foreign": {"secret": "must-not-survive"}},
        "artifacts": {},
        "diagnostics": {},
    }

    rebuilt = request_context.ensure_context(event, request_context.PHASE_MESSAGE)

    assert rebuilt["version"] == request_context.REQUEST_CONTEXT_VERSION
    assert rebuilt["request_id"] != "stale"
    assert rebuilt["flags"] == {}

