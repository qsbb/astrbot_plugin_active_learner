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


def test_memory_table_uses_compact_time_and_source_labels():
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert 'return "会话";' in js
    assert 'function fullTime(ts)' in js
    assert 'month: "2-digit"' in js
    assert 'title="${escapeHtml(fullTime(e.updated_at))}"' in js


def test_manager_url_sources_and_mobile_memory_meta_are_structured():
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    assert 'class="memory-table memory-table-primary"' in html
    assert "grid-template-columns: 120px minmax(180px, 1fr) auto;" not in css
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto;" in css
    assert "#slang-modal .memory-table {" in css
    assert 'content: "作用域："' in css
    assert 'content: "来源："' in css
    assert 'content: "更新时间："' in css


def test_detail_modal_uses_two_column_desktop_grid():
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")
    assert ".detail-grid {" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert ".detail-grid .detail-row:nth-child(6)" in css


def test_settings_center_merges_schema_config_and_groups_every_field():
    import json
    import re

    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    schema = json.loads((PAGE_DIR.parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))

    assert 'data-tab="advanced"' in html
    assert 'id="settings-panel-advanced"' in html
    assert 'id="config-search"' in html
    assert 'id="config-modal"' not in html
    assert "function openSettingsModal(tabName = \"llm\")" in js
    assert "function configGroupFor(" in js
    mapping_block = js[js.index("const CONFIG_GROUP_MAP = {") : js.index("const configState = {")]
    mapped = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*):", mapping_block, re.M))
    assert mapped == set(schema)


def test_log_actions_are_not_nested_inside_summary():
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    summary = html.split('<summary>📜 插件日志</summary>', 1)[1].split("</details>", 1)[0]
    assert "btn-refresh-logs" in summary
    assert '<summary>' not in summary.split('<div class="log-toolbar">', 1)[0]
    assert "log-toolbar" in html


def test_config_search_preserves_dirty_edits_and_invalidates_stale_cache():
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert "function syncConfigFormState(" in js
    assert "syncConfigFormState();" in js
    assert "configState.dirty" in js
    assert "configState.stale" in js
    assert "configState.dirty = false;" in js
    assert "configState.stale = true;" in js
    assert 'data-toast-fallback' in html
    assert "function escapeHtmlAttr(" in js
    assert '"&": "&amp;"' in js
    assert '"\\"": "&quot;"' in js


def test_manager_page_shortens_and_copies_long_scope_ids():
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    assert "function shortId(value, head = 10, tail = 6)" in js
    assert "function idChip(value, label = \"标识\")" in js
    assert "function scopeCell(entry)" in js
    assert 'data-copy-id="${escapeHtml(text)}"' in js
    # 三处作用域单元格 + 详情弹窗 + 调试列表统一走 scopeCell
    assert js.count("scopeCell(e)") == 2
    assert js.count("scopeCell(c)") == 1
    assert '${scopeCell(entry)}' in js
    assert '${scopeCell(s)}' in js
    # 复制走共享 SeriesUI.copy，失败降级为提示
    assert "window.SeriesUI?.copy ? await window.SeriesUI.copy(value) : false" in js
    assert "已复制完整标识" in js
    # 作用域筛选改用选项 data，作用域 ID 含冒号也不会被截断
    assert 'v.split(":", 2)' not in js
    assert 'opt.dataset.scopeType = String(s.scope_type ?? "");' in js
    assert 'opt.dataset.scopeId = String(s.scope_id ?? "");' in js
    assert "const option = e.target.selectedOptions?.[0];" in js
    assert 'state.scopeId = option.dataset.scopeId || "";' in js
    assert '.id-chip' in css
