from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.series_diagnostics import (
    diagnostic_clear,
    diagnostic_event,
    diagnostic_events,
)


def test_series_diagnostics_window_never_skips_backlog():
    """P0：积压超过 limit 时必须返回最早窗口，并用 next_seq 追平。"""
    diagnostic_clear()
    for index in range(5):
        diagnostic_event("window.event", f"e{index}")

    first = diagnostic_events(after_seq=0, limit=2)
    assert [event["summary"] for event in first["events"]] == ["e0", "e1"]
    assert first["has_more"] is True
    assert first["truncated"] is True
    assert first["next_seq"] == first["events"][-1]["seq"]

    second = diagnostic_events(after_seq=first["next_seq"], limit=2)
    assert [event["summary"] for event in second["events"]] == ["e2", "e3"]
    assert second["has_more"] is True

    third = diagnostic_events(after_seq=second["next_seq"], limit=2)
    assert [event["summary"] for event in third["events"]] == ["e4"]
    assert third["has_more"] is False
