from pathlib import Path


PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "manager"


def test_manager_motion_is_bounded_and_accessible():
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    assert "transition: all" not in css
    assert "transition: left" not in css
    assert "transition: width" not in css
    assert "@media (hover: hover) and (pointer: fine)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transform: scale(0.98)" in css


def test_manager_tabs_support_keyboard_and_progress_reports_state():
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    assert 'role="progressbar"' in html
    assert 'aria-valuenow="0"' in html
    assert "function bindTabKeyboardNavigation" in js
    assert 'event.key === "ArrowRight"' in js
    assert 'event.key === "Home"' in js
    assert "btn.tabIndex = active ? 0 : -1" in js
    assert "panel.hidden = !active" in js
    assert 'panel.setAttribute("aria-hidden", String(!active))' in js
    assert 'aria-controls="settings-panel-llm"' in html
    assert 'aria-labelledby="settings-tab-llm"' in html
    assert 'aria-controls="import-text-form"' in html
    assert 'aria-labelledby="import-tab-text"' in html
    assert "@keyframes fade-in" not in css
    assert 'setAttribute("aria-valuenow", String(pct))' in js


def test_manager_binds_controls_before_bounded_bridge_ready():
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    init = js.split("async function init()", 1)[1]

    assert "function bindPageEvents()" in js
    assert "async function waitForBridgeReady" in js
    assert "页面通信初始化超时，可使用页面按钮重试" in js
    assert init.index("bindPageEvents();") < init.index("await waitForBridgeReady();")
