"""Page-level unsaved-change guard contract."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / "pages/manager/app.js"


def _executable_source(source: str) -> str:
    """Drop compatibility comments before checking forbidden runtime APIs."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)//.*$", "", source)


def test_page_dirty_guard_contract() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    executable = _executable_source(source)

    assert "hasUnsavedChanges" in source
    assert "window.SeriesUI.confirm" in source
    assert 'window.addEventListener("beforeunload"' in source
    assert "returnValue" in source
    assert "未保存的修改" in source
    assert "当前页面还有未保存的改动，离开将放弃这些改动。" in source
    assert "放弃修改" in source
    assert "继续编辑" in source

    assert "window.confirm(" not in executable
    assert "alert(" not in executable
    assert "localStorage" not in executable
    assert "sessionStorage" not in executable
