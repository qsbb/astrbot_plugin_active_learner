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
  tbody.innerHTML = '<tr class="empty-row"><td colspan="7">加载中…</td></tr>';
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
    tbody.innerHTML = `<tr class="empty-row"><td colspan="7">加载失败：${e.message}</td></tr>`;
  }
}

function renderTable(items) {
  const tbody = document.getElementById("memory-tbody");
  if (!items.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">记忆库为空</td></tr>';
    return;
  }
  tbody.innerHTML = "";
  for (const e of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
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
  try {
    const result = await bridge.apiPost(`memory/${entryId}/verify`, {});
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

async function init() {
  await bridge.ready();
  bindEvents();
  bindImportEvents();
  bindSettingsEvents();
  await refreshAll();
}

init().catch((e) => showToast(`初始化失败：${e.message}`, true));
