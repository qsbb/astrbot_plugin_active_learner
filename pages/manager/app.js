const bridge = window.AstrBotPluginPage;

const state = {
  scopeType: "",
  scopeId: "",
  page: 1,
  perPage: 20,
  keyword: "",
  total: 0,
  totalPages: 1,
  currentDetailId: null,
  selectedIds: new Set(),
  settings: {
    llm_provider_id: "",
    refine_on_search: true,
    refine_on_import: true,
    refine_on_verify: true,
  },
};

function showToast(msg, isErr = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.toggle("error", isErr);
  t.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => t.classList.add("hidden"), 3000);
}

function scopeParams() {
  const p = {};
  if (state.scopeType && state.scopeId) {
    p.scope_type = state.scopeType;
    p.scope_id = state.scopeId;
  }
  return p;
}

function formatConfidence(c) {
  if (c == null || isNaN(c)) return "—";
  return (c * 100).toFixed(0) + "%";
}

function formatTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { hour12: false });
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function verifiedBadge(entry) {
  if (entry.verified) return '<span class="badge ok">已验证</span>';
  return '<span class="badge warn">未验证</span>';
}

async function loadScopes() {
  try {
    const data = await bridge.apiGet("scopes");
    const select = document.getElementById("scope-select");
    const current = `${state.scopeType}:${state.scopeId}`;
    select.innerHTML = '<option value="">全部作用域</option>';
    for (const s of data.scopes || []) {
      const opt = document.createElement("option");
      opt.value = `${s.scope_type}:${s.scope_id}`;
      opt.textContent = `${s.scope_type}:${s.scope_id} (${s.count})`;
      select.appendChild(opt);
    }
    if (current) select.value = current;
  } catch (e) {
    showToast(`加载 scope 失败: ${e.message}`, true);
  }
}

async function loadStats() {
  try {
    const s = await bridge.apiGet("stats", scopeParams());
    const setVal = (k, v) => {
      const el = document.querySelector(`[data-stat="${k}"]`);
      if (el) el.textContent = v ?? "—";
    };
    setVal("total", s.total ?? 0);
    setVal("verified", s.verified ?? 0);
    setVal("challenged", s.challenged ?? 0);
    setVal("challenged_total", s.challenged_total ?? 0);
    setVal("avg_confidence", s.avg_confidence != null ? formatConfidence(s.avg_confidence) : "—");
    setVal("access_total", s.access_total ?? 0);
  } catch (e) {
    showToast(`加载统计失败: ${e.message}`, true);
  }
}

async function loadMemories() {
  const tbody = document.getElementById("memory-tbody");
  tbody.innerHTML = '<tr class="empty-row"><td colspan="8">加载中…</td></tr>';
  try {
    const params = {
      ...scopeParams(),
      page: state.page,
      per_page: state.perPage,
    };
    if (state.keyword) params.keyword = state.keyword;
    const data = await bridge.apiGet("memories", params);
    state.total = data.total || 0;
    state.totalPages = data.total_pages || 1;
    if (state.page > state.totalPages) {
      state.page = state.totalPages || 1;
      return loadMemories();
    }
    renderTable(data.items || []);
    renderPagination();
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">加载失败：${e.message}</td></tr>`;
  }
}

function renderTable(items) {
  const tbody = document.getElementById("memory-tbody");
  if (!items.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">记忆库为空</td></tr>';
    return;
  }
  tbody.innerHTML = "";
  for (const e of items) {
    const tr = document.createElement("tr");
    const checked = state.selectedIds.has(e.id) ? "checked" : "";
    if (checked) tr.classList.add("selected");
    tr.innerHTML = `
      <td class="col-check">
        <input type="checkbox" data-id="${escapeHtml(e.id)}" ${checked} />
      </td>
      <td class="cell-topic" title="${escapeHtml(e.topic)}">${escapeHtml(e.topic)}</td>
      <td class="cell-preview" title="${escapeHtml(e.content)}">${escapeHtml(truncate(e.content, 80))}</td>
      <td class="cell-scope">${escapeHtml(e.scope_type)}:${escapeHtml(e.scope_id)}</td>
      <td>${formatConfidence(e.confidence)}</td>
      <td>${verifiedBadge(e)}</td>
      <td>${formatTime(e.updated_at)}</td>
      <td class="col-actions">
        <button type="button" data-act="detail" data-id="${escapeHtml(e.id)}">详情</button>
        <button type="button" data-act="verify" data-id="${escapeHtml(e.id)}">验证</button>
        <button type="button" data-act="forget" data-id="${escapeHtml(e.id)}" class="danger">删除</button>
      </td>
    `;
    tbody.appendChild(tr);
  }
  // sync header "select all" checkbox
  _syncSelectAllCheckbox(items);
  _updateSelectionToolbar();
}

// ---------- 多选工具栏 ----------

function _syncSelectAllCheckbox(items) {
  const cb = document.getElementById("select-all-checkbox");
  if (!cb) return;
  if (!items.length) { cb.checked = false; cb.indeterminate = false; return; }
  const allSelected = items.every((e) => state.selectedIds.has(e.id));
  const someSelected = items.some((e) => state.selectedIds.has(e.id));
  cb.checked = allSelected;
  cb.indeterminate = someSelected && !allSelected;
}

function _updateSelectionToolbar() {
  const toolbar = document.getElementById("selection-toolbar");
  const countEl = document.getElementById("selected-count");
  const count = state.selectedIds.size;
  toolbar.classList.toggle("hidden", count === 0);
  countEl.textContent = count ? `已选 ${count} 条` : "";
}

async function _batchDelete(ids) {
  if (!ids.length) return;
  const confirmed = confirm(`确定删除选中的 ${ids.length} 条记忆？此操作不可恢复。`);
  if (!confirmed) return;
  let ok = 0, fail = 0;
  for (const id of ids) {
    try {
      await bridge.apiPost(`memory/${id}/forget`, {});
      ok++;
    } catch (_) { fail++; }
  }
  showToast(`批量删除完成：${ok} 条成功${fail ? `，${fail} 条失败` : ""}`, fail > 0);
  state.selectedIds.clear();
  await Promise.all([loadMemories(), loadStats()]);
}

async function batchDeleteSelected() {
  await _batchDelete(Array.from(state.selectedIds));
}

function renderPagination() {
  document.getElementById("page-info").textContent = `第 ${state.page} / ${state.totalPages} 页，共 ${state.total} 条`;
  document.getElementById("page-prev").disabled = state.page <= 1;
  document.getElementById("page-next").disabled = state.page >= state.totalPages;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function showDetail(entryId) {
  state.currentDetailId = entryId;
  const modal = document.getElementById("detail-modal");
  const body = document.getElementById("detail-body");
  const title = document.getElementById("detail-title");
  body.innerHTML = "加载中…";
  modal.classList.remove("hidden");
  try {
    const [entry, versionsResp] = await Promise.all([
      bridge.apiGet(`memory/${entryId}`),
      bridge.apiGet(`memory/${entryId}/versions`),
    ]);
    title.textContent = `详情：${entry.topic}`;
    const kw = (entry.keywords || "").split(/\s+/).filter(Boolean).join(", ") || "—";
    const versions = versionsResp.items || [];
    body.innerHTML = `
      <div class="detail-grid">
        <div class="detail-row"><div class="detail-label">作用域</div><div class="detail-value">${escapeHtml(entry.scope_type)}:${escapeHtml(entry.scope_id)}</div></div>
        <div class="detail-row"><div class="detail-label">置信度</div><div class="detail-value">${formatConfidence(entry.confidence)}</div></div>
        <div class="detail-row"><div class="detail-label">状态</div><div class="detail-value">${verifiedBadge(entry)}</div></div>
        <div class="detail-row"><div class="detail-label">被质疑</div><div class="detail-value">${entry.challenge_count || 0} 次</div></div>
        <div class="detail-row"><div class="detail-label">访问次数</div><div class="detail-value">${entry.access_count || 0}</div></div>
        <div class="detail-row"><div class="detail-label">来源</div><div class="detail-value">${escapeHtml(entry.source || "—")}</div></div>
        <div class="detail-row"><div class="detail-label">关键词</div><div class="detail-value">${escapeHtml(kw)}</div></div>
        <div class="detail-row"><div class="detail-label">创建时间</div><div class="detail-value">${formatTime(entry.created_at)}</div></div>
        <div class="detail-row"><div class="detail-label">更新时间</div><div class="detail-value">${formatTime(entry.updated_at)}</div></div>
      </div>
      <div class="detail-section">
        <div class="detail-label">内容</div>
        <div class="detail-content">${escapeHtml(entry.content)}</div>
      </div>
      ${entry.sources_detail && entry.sources_detail.length ? `
        <div class="detail-section">
          <div class="detail-label">来源详情</div>
          <ul class="detail-sources">
            ${entry.sources_detail.map(s => `<li>${escapeHtml(typeof s === "string" ? s : JSON.stringify(s))}</li>`).join("")}
          </ul>
        </div>
      ` : ""}
      <div class="detail-section">
        <div class="detail-label">历史版本 (${versions.length})</div>
        ${versions.length ? `
          <table class="versions-table">
            <thead><tr><th>版本</th><th>原因</th><th>置信度</th><th>时间</th><th>内容</th></tr></thead>
            <tbody>
              ${versions.map(v => `
                <tr>
                  <td>v${v.version_no}</td>
                  <td>${escapeHtml(v.reason || "—")}</td>
                  <td>${formatConfidence(v.confidence)}</td>
                  <td>${formatTime(v.created_at)}</td>
                  <td>${escapeHtml(truncate(v.content, 100))}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        ` : '<p class="muted">暂无历史版本</p>'}
      </div>
    `;
  } catch (e) {
    body.innerHTML = `<p class="error-msg">加载失败：${e.message}</p>`;
  }
}

function closeModal() {
  document.getElementById("detail-modal").classList.add("hidden");
  state.currentDetailId = null;
}

async function verifyMemory(entryId, btn) {
  const original = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "验证中…";
  }
  const providerSelect = document.getElementById("settings-provider");
  const providerId = providerSelect ? providerSelect.value : "";
  try {
    const result = await bridge.apiPost(`memory/${entryId}/verify`, {
      provider_id: providerId,
    });
    showToast(`验证完成：${result.verdict}（置信度 ${formatConfidence(result.confidence)}）`);
    if (state.currentDetailId === entryId) {
      showDetail(entryId);
    }
    await Promise.all([loadMemories(), loadStats()]);
  } catch (e) {
    showToast(`验证失败：${e.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

async function forgetMemory(entryId) {
  if (!confirm("确认删除该条记忆？此操作不可撤销（版本会留痕）。")) return;
  try {
    await bridge.apiPost(`memory/${entryId}/forget`, {});
    showToast("已删除");
    if (state.currentDetailId === entryId) closeModal();
    await Promise.all([loadScopes(), loadMemories(), loadStats()]);
  } catch (e) {
    showToast(`删除失败：${e.message}`, true);
  }
}

async function exportData() {
  try {
    await bridge.download("export", scopeParams(), "memories.json");
    showToast("已开始下载");
  } catch (e) {
    showToast(`导出失败：${e.message}`, true);
  }
}

async function refreshAll() {
  await Promise.all([loadScopes(), loadStats(), loadMemories(), loadDebug()]);
}

async function loadDebug() {
  const el = document.getElementById("debug-content");
  if (!el) return;
  try {
    const d = await bridge.apiGet("debug");
    const scopesList = (d.scopes || [])
      .map((s) => `<li>${escapeHtml(s.scope_type)}:${escapeHtml(s.scope_id)} — ${s.count} 条</li>`)
      .join("");
    const toolsList = (d.tools_registered || []).join(", ") || "（无）";
    el.innerHTML = `
      <dl>
        <dt>数据库路径</dt><dd>${escapeHtml(d.db_path || "—")}</dd>
        <dt>Schema 版本</dt><dd>v${d.schema_version ?? "—"}</dd>
        <dt>总记忆数</dt><dd>${d.total_memories ?? 0}</dd>
        <dt>Embedder</dt><dd>${d.embedder_available ? "✅ 可用" : "❌ 不可用（降级 FTS5）"}${d.embedder_model ? " (" + escapeHtml(d.embedder_model) + ")" : ""}</dd>
        <dt>已注册工具</dt><dd>${escapeHtml(toolsList)}</dd>
        <dt>关心领域</dt><dd>${d.priority_topics && d.priority_topics.length ? escapeHtml(d.priority_topics.join(", ")) : "（未设置）"}</dd>
        <dt>当前 Boost</dt><dd>${d.priority_boost ?? "—"}</dd>
      </dl>
      ${scopesList ? `<div class="debug-section"><dt>Scope 列表</dt><ul class="debug-scope-list">${scopesList}</ul></div>` : ""}
    `;
  } catch (e) {
    el.innerHTML = `<span class="error-msg">诊断加载失败：${escapeHtml(e.message)}</span>`;
  }
}

function bindEvents() {
  document.getElementById("scope-select").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v) {
      const [t, id] = v.split(":", 2);
      state.scopeType = t;
      state.scopeId = id;
    } else {
      state.scopeType = "";
      state.scopeId = "";
    }
    state.page = 1;
    refreshAll();
  });

  document.getElementById("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.keyword = document.getElementById("search-input").value.trim();
    state.page = 1;
    loadMemories();
  });

  document.getElementById("search-clear").addEventListener("click", () => {
    document.getElementById("search-input").value = "";
    state.keyword = "";
    state.page = 1;
    loadMemories();
  });

  document.getElementById("btn-refresh").addEventListener("click", refreshAll);
  document.getElementById("btn-export").addEventListener("click", exportData);
  document.getElementById("btn-settings").addEventListener("click", openSettingsModal);
  document.getElementById("btn-config").addEventListener("click", openConfigModal);

  document.getElementById("page-prev").addEventListener("click", () => {
    if (state.page > 1) {
      state.page--;
      loadMemories();
    }
  });
  document.getElementById("page-next").addEventListener("click", () => {
    if (state.page < state.totalPages) {
      state.page++;
      loadMemories();
    }
  });

  document.getElementById("memory-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    if (act === "detail") showDetail(id);
    else if (act === "verify") verifyMemory(id, btn);
    else if (act === "forget") forgetMemory(id);
  });

  // 多选：行 checkbox 切换
  document.getElementById("memory-tbody").addEventListener("change", (e) => {
    const cb = e.target.closest('input[type="checkbox"][data-id]');
    if (!cb) return;
    const id = cb.dataset.id;
    if (cb.checked) {
      state.selectedIds.add(id);
    } else {
      state.selectedIds.delete(id);
    }
    cb.closest("tr").classList.toggle("selected", cb.checked);
    _syncSelectAllCheckbox(
      Array.from(document.querySelectorAll("#memory-tbody input[data-id]")).map(
        (c) => ({ id: c.dataset.id })
      )
    );
    _updateSelectionToolbar();
  });

  // 全选/反选/取消
  document.getElementById("select-all-checkbox")?.addEventListener("change", (e) => {
    const checks = Array.from(
      document.querySelectorAll("#memory-tbody input[data-id]")
    );
    if (e.target.checked) {
      checks.forEach((c) => {
        state.selectedIds.add(c.dataset.id);
        c.checked = true;
        c.closest("tr").classList.add("selected");
      });
    } else {
      checks.forEach((c) => {
        state.selectedIds.delete(c.dataset.id);
        c.checked = false;
        c.closest("tr").classList.remove("selected");
      });
    }
    _updateSelectionToolbar();
  });

  document.getElementById("btn-invert")?.addEventListener("click", () => {
    document.querySelectorAll("#memory-tbody input[data-id]").forEach((cb) => {
      const id = cb.dataset.id;
      if (state.selectedIds.has(id)) {
        state.selectedIds.delete(id);
        cb.checked = false;
        cb.closest("tr").classList.remove("selected");
      } else {
        state.selectedIds.add(id);
        cb.checked = true;
        cb.closest("tr").classList.add("selected");
      }
    });
    _syncSelectAllCheckbox(
      Array.from(document.querySelectorAll("#memory-tbody input[data-id]")).map(
        (c) => ({ id: c.dataset.id })
      )
    );
    _updateSelectionToolbar();
  });

  document.getElementById("btn-deselect-all")?.addEventListener("click", () => {
    state.selectedIds.clear();
    document
      .querySelectorAll("#memory-tbody input[type=checkbox]")
      .forEach((cb) => {
        cb.checked = false;
        cb.closest("tr")?.classList.remove("selected");
      });
    _syncSelectAllCheckbox(
      Array.from(document.querySelectorAll("#memory-tbody input[data-id]")).map(
        (c) => ({ id: c.dataset.id })
      )
    );
    _updateSelectionToolbar();
  });

  document.getElementById("btn-batch-delete")?.addEventListener("click", batchDeleteSelected);

  document.querySelector(".modal-close").addEventListener("click", closeModal);
  document.querySelector(".modal-backdrop").addEventListener("click", closeModal);
  document.getElementById("detail-verify").addEventListener("click", () => {
    if (state.currentDetailId) verifyMemory(state.currentDetailId, null);
  });
  document.getElementById("detail-forget").addEventListener("click", () => {
    if (state.currentDetailId) forgetMemory(state.currentDetailId);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });
}

// ---------- Settings Modal ----------

function openSettingsModal() {
  const modal = document.getElementById("settings-modal");
  modal.classList.remove("hidden");
  Promise.all([loadProviders(), loadSettings()]).catch((e) => {
    showToast(`加载设置失败: ${e.message}`, true);
  });
}

function closeSettingsModal() {
  document.getElementById("settings-modal").classList.add("hidden");
}

async function loadProviders() {
  const select = document.getElementById("settings-provider");
  try {
    const data = await bridge.apiGet("providers");
    const providers = data.providers || [];
    const current = data.current || "";
    select.innerHTML = '<option value="">（使用事件默认 Provider）</option>';
    for (const p of providers) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = `${p.name || p.id} (${p.type || "?"})`;
      select.appendChild(opt);
    }
    select.value = current || "";
    updateNoProviderHint(select.value);
  } catch (e) {
    showToast(`加载 Provider 列表失败: ${e.message}`, true);
  }
}

async function loadSettings() {
  try {
    const s = await bridge.apiGet("settings");
    state.settings = {
      llm_provider_id: s.llm_provider_id || "",
      refine_on_search: s.refine_on_search !== false,
      refine_on_import: s.refine_on_import !== false,
      refine_on_verify: s.refine_on_verify !== false,
    };
    document.getElementById("settings-provider").value = state.settings.llm_provider_id || "";
    document.getElementById("settings-refine-search").checked = state.settings.refine_on_search;
    document.getElementById("settings-refine-import").checked = state.settings.refine_on_import;
    document.getElementById("settings-refine-verify").checked = state.settings.refine_on_verify;
    updateNoProviderHint(state.settings.llm_provider_id);
  } catch (e) {
    showToast(`加载设置失败: ${e.message}`, true);
  }
}

function updateNoProviderHint(providerId) {
  const hint = document.getElementById("settings-no-provider-hint");
  if (!providerId) {
    hint.classList.remove("hidden");
  } else {
    hint.classList.add("hidden");
  }
}

async function saveSettings() {
  const payload = {
    llm_provider_id: document.getElementById("settings-provider").value,
    refine_on_search: document.getElementById("settings-refine-search").checked,
    refine_on_import: document.getElementById("settings-refine-import").checked,
    refine_on_verify: document.getElementById("settings-refine-verify").checked,
  };
  try {
    const result = await bridge.apiPost("settings", payload);
    state.settings = {
      llm_provider_id: result.llm_provider_id || "",
      refine_on_search: result.refine_on_search !== false,
      refine_on_import: result.refine_on_import !== false,
      refine_on_verify: result.refine_on_verify !== false,
    };
    showToast("设置已保存");
    closeSettingsModal();
  } catch (e) {
    showToast(`保存失败: ${e.message}`, true);
  }
}

function bindSettingsEvents() {
  document
    .querySelectorAll("#settings-modal .modal-close, #settings-modal .modal-backdrop, #settings-cancel")
    .forEach((el) => {
      el.addEventListener("click", closeSettingsModal);
    });
  document.getElementById("settings-save").addEventListener("click", saveSettings);
  document.getElementById("btn-refresh-providers").addEventListener("click", loadProviders);
  document.getElementById("settings-provider").addEventListener("change", (e) => {
    updateNoProviderHint(e.target.value);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSettingsModal();
  });
}

// ---------- Import Modal ----------

function openImportModal() {
  document.getElementById("import-modal").classList.remove("hidden");
  document.getElementById("import-result").classList.add("hidden");
}

function closeImportModal() {
  document.getElementById("import-modal").classList.add("hidden");
}

function showImportResult(html, isError = false) {
  const el = document.getElementById("import-result");
  el.innerHTML = html;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
}

function switchImportTab(tabName) {
  document.querySelectorAll("#import-modal .tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabName);
  });
  document.querySelectorAll("#import-modal .tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.panel === tabName);
  });
}

async function submitImportText(form) {
  const payload = {
    topic: form.topic.value.trim(),
    content: form.content.value,
    scope_type: form.scope_type.value,
    scope_id: form.scope_id.value.trim(),
    refine: form.refine.checked,
  };
  if (!payload.topic || !payload.content || !payload.scope_type) {
    showImportResult("❌ 主题、内容、Scope 类型为必填项", true);
    return;
  }
  try {
    const result = await bridge.apiPost("import_text", payload);
    showImportResult(`✅ 已导入记忆：<strong>${escapeHtml(result.entry.topic)}</strong>`);
    form.reset();
    await refreshAll();
  } catch (e) {
    showImportResult(`❌ 导入失败：${escapeHtml(e.message || String(e))}`, true);
  }
}

async function submitImportMd(form) {
  const file = form.file.files[0];
  if (!file) {
    showImportResult("❌ 请选择 Markdown 文件", true);
    return;
  }
  const btn = form.querySelector('button[type="submit"]');
  const original = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "导入中…";
  }
  try {
    const content = await file.text();
    const payload = {
      filename: file.name,
      topic: form.topic.value || "",
      content,
      scope_type: form.scope_type.value,
      scope_id: form.scope_id.value || "",
      refine: form.refine.checked,
      chunk_size: parseInt(form.chunk_size?.value || "500", 10),
      chunk_overlap: parseInt(form.chunk_overlap?.value || "50", 10),
    };
    const result = await bridge.apiPost("import_md", payload);
    // v2.4.0：MD 可能返回单条 entry 或批量 chunks
    if (result.entry) {
      showImportResult(`✅ 已导入：<strong>${escapeHtml(result.entry.topic)}</strong>`);
    } else if (result.total != null) {
      const lines = [
        `✅ 分块导入完成：成功 ${result.success} / 总计 ${result.total}（失败 ${result.failed}）`,
      ];
      if (result.results && result.results.length) {
        lines.push('<ul class="import-detail-list">');
        for (const r of result.results) {
          if (r.ok) {
            lines.push(`<li>✅ ${escapeHtml(r.topic || "")} (chunk #${r.chunk})</li>`);
          } else {
            lines.push(`<li>❌ chunk #${r.chunk}：${escapeHtml(r.error || "未知错误")}</li>`);
          }
        }
        lines.push("</ul>");
      }
      showImportResult(lines.join(""));
    } else {
      showImportResult("✅ 已导入");
    }
    form.reset();
    await refreshAll();
  } catch (e) {
    showImportResult(`❌ 导入失败：${escapeHtml(e.message || String(e))}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

async function submitImportFile(form, endpoint, label) {
  const file = form.file.files[0];
  if (!file) {
    showImportResult(`❌ 请选择${label}文件`, true);
    return;
  }
  const btn = form.querySelector('button[type="submit"]');
  const original = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "导入中…";
  }
  try {
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
    }
    const base64 = btoa(binary);
    const payload = {
      filename: file.name,
      base64,
      scope_type: form.scope_type.value,
      scope_id: form.scope_id.value || "",
      refine: form.refine.checked,
      chunk_size: parseInt(form.chunk_size?.value || "500", 10),
      chunk_overlap: parseInt(form.chunk_overlap?.value || "50", 10),
    };
    const result = await bridge.apiPost(endpoint, payload);
    const lines = [
      `✅ ${label}分块导入完成：成功 ${result.success} / 总计 ${result.total}（失败 ${result.failed}）`,
    ];
    if (result.results && result.results.length) {
      lines.push('<ul class="import-detail-list">');
      for (const r of result.results) {
        if (r.ok) {
          lines.push(`<li>✅ ${escapeHtml(r.topic || "")} (chunk #${r.chunk})</li>`);
        } else {
          lines.push(`<li>❌ chunk #${r.chunk}：${escapeHtml(r.error || "未知错误")}</li>`);
        }
      }
      lines.push("</ul>");
    }
    showImportResult(lines.join(""));
    form.reset();
    await refreshAll();
  } catch (e) {
    showImportResult(`❌ ${label}导入失败：${escapeHtml(e.message || String(e))}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

async function submitImportZip(form) {
  const file = form.file.files[0];
  if (!file) {
    showImportResult("❌ 请选择 ZIP 文件", true);
    return;
  }
  const btn = form.querySelector('button[type="submit"]');
  const original = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "导入中…";
  }
  try {
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    const base64 = btoa(binary);
    const payload = {
      filename: file.name,
      base64,
      scope_type: form.scope_type.value,
      scope_id: form.scope_id.value || "",
      refine: form.refine.checked,
    };
    const result = await bridge.apiPost("import_zip", payload);
    const lines = [
      `✅ 批量导入完成：成功 ${result.success} / 总计 ${result.total}（失败 ${result.failed}）`,
    ];
    if (result.results && result.results.length) {
      lines.push('<ul class="import-detail-list">');
      for (const r of result.results) {
        if (r.ok) {
          lines.push(`<li>✅ ${escapeHtml(r.file)} → ${escapeHtml(r.topic || "")}</li>`);
        } else {
          lines.push(`<li>❌ ${escapeHtml(r.file)}：${escapeHtml(r.error || "未知错误")}</li>`);
        }
      }
      lines.push("</ul>");
    }
    showImportResult(lines.join(""));
    form.reset();
    await refreshAll();
  } catch (e) {
    showImportResult(`❌ 批量导入失败：${escapeHtml(e.message || String(e))}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

function bindImportEvents() {
  document.getElementById("btn-import").addEventListener("click", openImportModal);

  document.querySelectorAll("#import-modal .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchImportTab(btn.dataset.tab));
  });

  document
    .querySelectorAll("#import-modal .modal-close, #import-modal .modal-backdrop")
    .forEach((el) => {
      el.addEventListener("click", closeImportModal);
    });

  document.getElementById("import-text-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitImportText(e.target);
  });
  document.getElementById("import-md-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitImportMd(e.target);
  });
  document.getElementById("import-zip-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitImportZip(e.target);
  });
  document.getElementById("import-pdf-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitImportFile(e.target, "import_pdf", "PDF");
  });
  document.getElementById("import-docx-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitImportFile(e.target, "import_docx", "Word");
  });
  document.getElementById("import-txt-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitImportFile(e.target, "import_txt", "TXT");
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeImportModal();
  });
}

// ---------- Config Modal（直接读取 _conf_schema.json 渲染） ----------

const configState = {
  fields: [],
  original: {},
};

function openConfigModal() {
  const modal = document.getElementById("config-modal");
  modal.classList.remove("hidden");
  loadConfigSchema().catch((e) => {
    showToast(`加载配置失败: ${e.message}`, true);
  });
}

function closeConfigModal() {
  document.getElementById("config-modal").classList.add("hidden");
}

async function loadConfigSchema() {
  const container = document.getElementById("config-form");
  container.innerHTML = '<p class="muted">加载中…</p>';
  try {
    const data = await bridge.apiGet("config_schema");
    configState.fields = data.fields || [];
    configState.original = {};
    for (const f of configState.fields) {
      configState.original[f.name] = f.value;
    }
    renderConfigForm(configState.fields);
  } catch (e) {
    container.innerHTML = `<p class="error-msg">加载失败：${escapeHtml(e.message)}</p>`;
    throw e;
  }
}

function renderConfigForm(fields) {
  const container = document.getElementById("config-form");
  if (!fields.length) {
    container.innerHTML = '<p class="muted">未读取到任何配置字段</p>';
    return;
  }
  container.innerHTML = "";
  for (const f of fields) {
    const card = document.createElement("div");
    card.className = "config-field";
    const isBool = f.type === "bool";
    const label = document.createElement("label");
    label.className = isBool ? "config-bool" : "config-input";
    if (isBool) {
      label.innerHTML = `
        <input type="checkbox" data-field="${escapeHtml(f.name)}" ${f.value ? "checked" : ""} />
        <span class="config-field-name">${escapeHtml(f.description || f.name)}</span>
        <span class="config-field-key">${escapeHtml(f.name)}</span>
      `;
    } else {
      const inputAttrs = configInputAttrs(f);
      label.innerHTML = `
        <span class="config-field-name">${escapeHtml(f.description || f.name)}</span>
        <span class="config-field-key">${escapeHtml(f.name)}</span>
        <input ${inputAttrs} data-field="${escapeHtml(f.name)}" value="${escapeHtmlAttr(f.value)}" />
      `;
    }
    card.appendChild(label);
    if (f.hint) {
      const hint = document.createElement("p");
      hint.className = "config-hint";
      hint.textContent = f.hint;
      card.appendChild(hint);
    }
    if (f.default !== undefined && f.default !== null && f.default !== "") {
      const def = document.createElement("p");
      def.className = "config-default";
      def.textContent = `默认值：${typeof f.default === "boolean" ? (f.default ? "true" : "false") : f.default}`;
      card.appendChild(def);
    }
    container.appendChild(card);
  }
}

function configInputAttrs(f) {
  if (f.type === "int") {
    return `type="number" step="1"`;
  }
  if (f.type === "float") {
    return `type="number" step="0.01"`;
  }
  return `type="text"`;
}

function escapeHtmlAttr(v) {
  if (v == null) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

function collectConfigPayload() {
  const payload = {};
  for (const f of configState.fields) {
    const el = document.querySelector(`#config-form [data-field="${cssEscape(f.name)}"]`);
    if (!el) continue;
    if (f.type === "bool") {
      payload[f.name] = !!el.checked;
    } else if (f.type === "int") {
      const raw = el.value.trim();
      if (raw === "") continue;
      const v = parseInt(raw, 10);
      if (isNaN(v)) {
        throw new Error(`字段 ${f.name} 必须是整数`);
      }
      payload[f.name] = v;
    } else if (f.type === "float") {
      const raw = el.value.trim();
      if (raw === "") continue;
      const v = parseFloat(raw);
      if (isNaN(v)) {
        throw new Error(`字段 ${f.name} 必须是数字`);
      }
      payload[f.name] = v;
    } else {
      payload[f.name] = el.value;
    }
  }
  return payload;
}

function cssEscape(name) {
  if (window.CSS && CSS.escape) return CSS.escape(name);
  return String(name).replace(/(["\\])/g, "\\$1");
}

async function saveConfig() {
  const btn = document.getElementById("config-save");
  const original = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "保存中…";
  }
  let payload;
  try {
    payload = collectConfigPayload();
  } catch (e) {
    showToast(`校验失败：${e.message}`, true);
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
    return;
  }
  try {
    const result = await bridge.apiPost("settings", payload);
    const changed = Object.keys(payload).length;
    showToast(`✅ 已保存 ${changed} 项配置并即时生效`);
    // 更新本地 original，便于"恢复默认"判断
    for (const k of Object.keys(payload)) {
      configState.original[k] = payload[k];
    }
    closeConfigModal();
    await loadDebug();
  } catch (e) {
    showToast(`保存失败：${escapeHtml(e.message || String(e))}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

function resetConfigToDefault() {
  if (!confirm("确认将所有字段重置为 schema 默认值？此操作只填入表单，需点击「保存」才生效。")) {
    return;
  }
  for (const f of configState.fields) {
    const el = document.querySelector(`#config-form [data-field="${cssEscape(f.name)}"]`);
    if (!el) continue;
    if (f.type === "bool") {
      el.checked = !!f.default;
    } else {
      el.value = f.default !== undefined && f.default !== null ? escapeHtmlAttr(f.default) : "";
    }
  }
  showToast("已填入默认值，请点击「保存」生效");
}

function bindConfigEvents() {
  document
    .querySelectorAll("#config-modal .modal-close, #config-modal .modal-backdrop, #config-cancel")
    .forEach((el) => {
      el.addEventListener("click", closeConfigModal);
    });
  document.getElementById("config-save").addEventListener("click", saveConfig);
  document.getElementById("config-reset").addEventListener("click", resetConfigToDefault);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeConfigModal();
  });
}

async function init() {
  await bridge.ready();
  bindEvents();
  bindImportEvents();
  bindSettingsEvents();
  bindConfigEvents();
  bindBuiltinKbEvents();
  await refreshAll();
}

init().catch((e) => showToast(`初始化失败：${e.message}`, true));

// ---------- Builtin KB Modal（从 AstrBot 内置知识库导入） ----------

const builtinKbState = {
  selectedKbId: "",
  selectedDocIds: new Set(),
  docs: [],
};

function openBuiltinKbModal() {
  const modal = document.getElementById("builtin-kb-modal");
  modal.classList.remove("hidden");
  const errBox = document.getElementById("builtin-kb-error");
  if (errBox) errBox.classList.add("hidden");
  const progress = document.getElementById("builtin-kb-progress");
  if (progress) progress.classList.add("hidden");
  loadBuiltinKbList();
}

function closeBuiltinKbModal() {
  document.getElementById("builtin-kb-modal").classList.add("hidden");
}

function showBuiltinKbError(msg) {
  const errBox = document.getElementById("builtin-kb-error");
  if (!errBox) return;
  errBox.textContent = msg;
  errBox.classList.remove("hidden");
}

async function loadBuiltinKbList() {
  const listEl = document.getElementById("builtin-kb-list");
  listEl.innerHTML = '<p class="muted">加载中…</p>';
  try {
    const data = await bridge.apiGet("builtin_kb/list");
    renderBuiltinKbList(data.items || []);
  } catch (e) {
    listEl.innerHTML = "";
    const msg = e.message || String(e);
    const hint = msg.includes("status code 5")
      ? "（详细错误已记录到 AstrBot 日志，可在 data/logs/ 查看）"
      : "";
    showBuiltinKbError(`读取知识库列表失败：${escapeHtml(msg)}${hint}`);
  }
}

function renderBuiltinKbList(items) {
  const listEl = document.getElementById("builtin-kb-list");
  if (!items.length) {
    listEl.innerHTML = '<p class="muted">尚无知识库。请先在 AstrBot Dashboard 创建并上传文档。</p>';
    return;
  }
  listEl.innerHTML = "";
  items.forEach((kb) => {
    const div = document.createElement("div");
    div.className = "kb-item";
    div.dataset.kbId = kb.kb_id;
    div.innerHTML = `
      <div class="kb-item-header">
        <span class="kb-emoji">${escapeHtml(kb.emoji || "📚")}</span>
        <div class="kb-item-info">
          <div class="kb-name">${escapeHtml(kb.kb_name)}</div>
          <div class="kb-meta">${kb.doc_count} 个文档</div>
        </div>
      </div>
      ${kb.description ? `<div class="kb-desc">${escapeHtml(kb.description)}</div>` : ""}
    `;
    div.addEventListener("click", () => selectBuiltinKb(kb.kb_id, div));
    listEl.appendChild(div);
  });
}

async function selectBuiltinKb(kbId, el) {
  document.querySelectorAll("#builtin-kb-list .kb-item").forEach((n) => n.classList.remove("active"));
  if (el) el.classList.add("active");
  builtinKbState.selectedKbId = kbId;
  builtinKbState.selectedDocIds.clear();
  updateBuiltinKbSelectedCount();
  await loadBuiltinKbDocuments(kbId);
}

async function loadBuiltinKbDocuments(kbId) {
  const docsEl = document.getElementById("builtin-kb-docs");
  const titleEl = document.getElementById("builtin-kb-docs-title");
  docsEl.innerHTML = '<p class="muted">加载中…</p>';
  if (titleEl) titleEl.textContent = "文档列表";
  try {
    const data = await bridge.apiGet(`builtin_kb/${kbId}/documents`);
    builtinKbState.docs = data.items || [];
    renderBuiltinKbDocs(builtinKbState.docs);
    if (titleEl) titleEl.textContent = `文档列表（${data.kb_name || ""}）`;
  } catch (e) {
    docsEl.innerHTML = "";
    const msg = e.message || String(e);
    const hint = msg.includes("status code 5")
      ? "（详细错误已记录到 AstrBot 日志，可在 data/logs/ 查看）"
      : "";
    showBuiltinKbError(`读取文档列表失败：${escapeHtml(msg)}${hint}`);
  }
}

function renderBuiltinKbDocs(items) {
  const docsEl = document.getElementById("builtin-kb-docs");
  if (!items.length) {
    docsEl.innerHTML = '<p class="muted">该知识库无文档。</p>';
    return;
  }
  docsEl.innerHTML = "";
  items.forEach((doc) => {
    const div = document.createElement("div");
    div.className = "doc-item";
    const icon = fileIcon(doc.file_type);
    const sizeStr = formatFileSize(doc.file_size);
    const dateStr = doc.created_at ? formatTime(doc.created_at) : "";
    div.innerHTML = `
      <label class="doc-checkbox">
        <input type="checkbox" data-doc-id="${escapeHtml(doc.doc_id)}" />
        <span class="doc-icon">${icon}</span>
      </label>
      <div class="doc-info">
        <div class="doc-name">${escapeHtml(doc.doc_name)}</div>
        <div class="doc-meta">
          <span class="badge badge-type">${escapeHtml(doc.file_type || "未知")}</span>
          <span class="badge">${doc.chunk_count} chunks</span>
          ${sizeStr ? `<span class="badge">${sizeStr}</span>` : ""}
          ${dateStr ? `<span class="muted">${dateStr}</span>` : ""}
        </div>
      </div>
    `;
    const checkbox = div.querySelector('input[type="checkbox"]');
    checkbox.addEventListener("change", () => toggleBuiltinKbDoc(doc.doc_id, checkbox.checked));
    docsEl.appendChild(div);
  });
}

function fileIcon(fileType) {
  const ft = (fileType || "").toLowerCase();
  if (ft === "pdf") return "📄";
  if (ft === "doc" || ft === "docx") return "📝";
  if (ft === "md" || ft === "markdown") return "📑";
  if (ft === "txt") return "📃";
  if (ft === "html") return "🌐";
  return "📄";
}

function formatFileSize(bytes) {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function toggleBuiltinKbDoc(docId, checked) {
  if (checked) {
    builtinKbState.selectedDocIds.add(docId);
  } else {
    builtinKbState.selectedDocIds.delete(docId);
  }
  updateBuiltinKbSelectedCount();
}

function updateBuiltinKbSelectedCount() {
  const countEl = document.getElementById("builtin-kb-selected-count");
  const importBtn = document.getElementById("builtin-kb-import");
  if (countEl) countEl.textContent = `已选 ${builtinKbState.selectedDocIds.size} 个`;
  if (importBtn) importBtn.disabled = builtinKbState.selectedDocIds.size === 0;
}

function builtinKbSelectAll() {
  if (!builtinKbState.docs.length) return;
  builtinKbState.docs.forEach((d) => builtinKbState.selectedDocIds.add(d.doc_id));
  document.querySelectorAll('#builtin-kb-docs input[type="checkbox"]').forEach((cb) => {
    cb.checked = true;
  });
  updateBuiltinKbSelectedCount();
}

function builtinKbSelectNone() {
  builtinKbState.selectedDocIds.clear();
  document.querySelectorAll('#builtin-kb-docs input[type="checkbox"]').forEach((cb) => {
    cb.checked = false;
  });
  updateBuiltinKbSelectedCount();
}

async function importBuiltinKb() {
  if (!builtinKbState.selectedKbId || builtinKbState.selectedDocIds.size === 0) {
    showToast("请先选择知识库和文档", true);
    return;
  }
  const scopeType = document.getElementById("builtin-kb-scope-type").value;
  const scopeId = document.getElementById("builtin-kb-scope-id").value.trim();
  const chunkSize = parseInt(document.getElementById("builtin-kb-chunk-size").value, 10) || 500;
  const chunkOverlap = parseInt(document.getElementById("builtin-kb-chunk-overlap").value, 10) || 50;
  const refine = document.getElementById("builtin-kb-refine").checked;
  if (!scopeType) {
    showToast("请选择 Scope 类型", true);
    return;
  }

  const btn = document.getElementById("builtin-kb-import");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "导入中…";
  const progressEl = document.getElementById("builtin-kb-progress");
  progressEl.classList.remove("hidden");
  progressEl.classList.remove("error");
  progressEl.innerHTML = `<p class="muted">正在导入 ${builtinKbState.selectedDocIds.size} 个文档…</p>`;

  try {
    const payload = {
      kb_id: builtinKbState.selectedKbId,
      doc_ids: Array.from(builtinKbState.selectedDocIds),
      scope_type: scopeType,
      scope_id: scopeId,
      refine,
      chunk_size: chunkSize,
      chunk_overlap: chunkOverlap,
    };
    const result = await bridge.apiPost("builtin_kb/import", payload);
    const success = result.success || 0;
    const total = result.total || 0;
    const failed = result.failed || 0;
    let html = `<p>✅ 导入完成：<strong>${success}/${total}</strong> 个文档成功</p>`;
    if (Array.isArray(result.results)) {
      const failedItems = result.results.filter((r) => !r.ok);
      if (failedItems.length) {
        html += '<div class="import-failed-list"><strong>失败列表：</strong><ul>';
        failedItems.forEach((r) => {
          const name = r.doc_name || r.doc_id;
          html += `<li>${escapeHtml(name)}: ${escapeHtml(r.error || "未知错误")}</li>`;
        });
        html += "</ul></div>";
      }
    }
    progressEl.innerHTML = html;
    showToast(`内置 KB 导入完成：${success}/${total} 成功`);
    if (success > 0) {
      await refreshAll();
    }
  } catch (e) {
    progressEl.classList.add("error");
    const errMsg = e.message || String(e);
    const hint = errMsg.includes("status code 5")
      ? "（详细错误已记录到 AstrBot 日志，可在 data/logs/ 查看）"
      : "";
    progressEl.innerHTML = `<p>❌ 导入失败：${escapeHtml(errMsg)}${hint}</p>`;
    showToast(`导入失败：${errMsg}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function bindBuiltinKbEvents() {
  document.getElementById("btn-builtin-kb").addEventListener("click", openBuiltinKbModal);
  document
    .querySelectorAll("#builtin-kb-modal .modal-close, #builtin-kb-modal .modal-backdrop")
    .forEach((el) => {
      el.addEventListener("click", closeBuiltinKbModal);
    });
  document.getElementById("builtin-kb-cancel").addEventListener("click", closeBuiltinKbModal);
  document.getElementById("builtin-kb-select-all").addEventListener("click", builtinKbSelectAll);
  document.getElementById("builtin-kb-select-none").addEventListener("click", builtinKbSelectNone);
  document.getElementById("builtin-kb-import").addEventListener("click", importBuiltinKb);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeBuiltinKbModal();
  });
}
