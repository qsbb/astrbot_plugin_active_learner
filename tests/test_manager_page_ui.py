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


def test_manager_slang_tab_structure_and_actions():
    """v1.2.0 Phase 4：黑话管理标签页的关键元素与前端路由调用。"""
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")

    # 顶栏入口与弹窗骨架
    assert 'id="btn-slang"' in html
    assert 'id="slang-modal"' in html
    # 三个标签页：候选队列 / 已学词条 / 拉黑列表（tab 可访问性模式与设置页一致）
    assert 'id="slang-tab-candidates"' in html
    assert 'id="slang-tab-entries"' in html
    assert 'id="slang-tab-blocklist"' in html
    assert 'aria-controls="slang-panel-candidates"' in html
    assert 'aria-labelledby="slang-tab-candidates"' in html
    # 三张表的 tbody
    assert 'id="slang-candidates-tbody"' in html
    assert 'id="slang-entries-tbody"' in html
    assert 'id="slang-blocklist-tbody"' in html

    # JS：加载/渲染/操作函数与 tab 切换（键盘导航复用通用绑定）
    assert "async function loadSlangData" in js
    assert "function renderSlangCandidates" in js
    assert "function renderSlangEntries" in js
    assert "function renderSlangBlocklist" in js
    assert "async function slangAction" in js
    assert "function switchSlangTab" in js
    assert 'bindTabKeyboardNavigation("#slang-modal .tab-btn", switchSlangTab)' in js
    assert "bindSlangEvents();" in js
    # 前端调用的路由与后端注册保持一致
    assert 'apiGet("slang/candidates"' in js
    assert 'apiGet("slang/entries"' in js
    assert 'apiGet("slang/blocklist")' in js
    assert '"slang/promote"' in js
    assert '"slang/reject"' in js
    assert '"slang/block"' in js
    assert '"slang/unblock"' in js
    # 拉黑是破坏性操作，必须走确认弹窗
    assert "确认拉黑" in js
