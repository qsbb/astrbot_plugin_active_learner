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
    shared = (PAGE_DIR / "series-ui.js").read_text(encoding="utf-8")

    assert 'role="progressbar"' in html
    assert 'aria-valuenow="0"' in html
    # tab 点击切换与方向键导航统一走共享层 SeriesUI.bindTabs（含 ArrowRight/Home/End）
    assert "function bindPageTabs(" in js
    assert "window.SeriesUI?.bindTabs" in js
    assert 'window.SeriesUI.bindTabs(root, tabSelector, panelSelector, "data-tab")' in js
    assert "function bindTabs(" in shared
    assert "event.key === 'ArrowRight'" in shared
    assert "event.key === 'Home'" in shared
    assert "tab.tabIndex = active ? 0 : -1" in shared
    assert "panel.hidden = " in shared
    # 面板可见性由 hidden 属性驱动（bindTabs 只切 hidden，不再维护 active/aria-hidden）
    assert ".tab-panel:not([hidden])" in css
    assert ".settings-panel:not([hidden])" in css
    assert "tab-panel.active" not in js
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
    # tab 切换（含方向键导航）复用共享层 SeriesUI.bindTabs
    assert 'bindPageTabs(document.getElementById("slang-modal"), ".tab-btn", ".tab-panel")' in js
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

    # 方案 A：主记忆列表从 9 列表格改为两行式行卡；黑话弹窗仍保留表格语义。
    assert 'id="memory-tbody" class="memory-list"' in html
    assert 'class="memory-list-head"' in html
    assert 'class="memory-card' not in html  # 行卡由 JS 渲染，避免静态骨架与渲染逻辑漂移
    assert "grid-template-columns: 120px minmax(180px, 1fr) auto;" not in css
    assert "#slang-modal .memory-table {" in css


def test_manager_memory_cards_follow_two_line_design():
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    # 行卡结构：标题 / 预览 / meta 三段，操作收进 kebab 菜单
    assert 'class="memory-card' in js
    assert 'class="memory-topic"' in js
    assert 'class="memory-preview"' in js
    assert 'class="memory-meta"' in js
    assert 'class="kebab"' in js or "class=\"kebab\"" in js
    assert 'class="kebab-menu"' in js
    assert "function closeKebabMenus()" in js
    assert 'data-kebab=' in js
    # 移动端两列网格 + kebab 菜单位置
    assert ".memory-card {" in css
    assert "grid-template-columns: auto minmax(0, 1fr) auto;" in css
    assert ".kebab-menu {" in css
    # 顶部动作为 导入 / 主动学习 / 更多 三个入口，其余收进更多菜单
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="more-menu"' in html
    assert html.count("action-main") == 2
    assert 'id="btn-refresh"' in html and 'id="btn-settings"' in html


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
    # 转义统一走共享层 SeriesUI.escapeHtml/escapeHtmlAttr，本地保留最小兜底
    assert "const escapeHtml = (...args) =>" in js
    assert "const escapeHtmlAttr = (...args) =>" in js
    assert "window.SeriesUI?.escapeHtmlAttr" in js
    assert '"&": "&amp;"' in js
    shared_js = (PAGE_DIR / "series-ui.js").read_text(encoding="utf-8")
    assert "function escapeHtmlAttr(" in shared_js
    assert '"&quot;"' in shared_js


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
