/* 凝心 UI 1.0 — Shared interaction primitives
 * Canonical source: astrbot_plugin_update_manager/ui/series-ui.js
 * No framework dependency. Pages consume window.SeriesUI for overlays and state.
 */
(() => {
  "use strict";

  const VERSION = "1.0.0";
  const THEME_KEY = "ningxin.series.ui.theme";
  const overlays = [];

  function ready(callback) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", callback, { once: true });
    } else {
      callback();
    }
  }

  function markPage() {
    if (!document.body) return;
    document.body.setAttribute("data-series-ui", "1");
    const theme = safeRead(THEME_KEY) || "glass";
    document.body.dataset.siTheme = theme;
  }

  function safeRead(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }

  function safeWrite(key, value) {
    try { window.localStorage.setItem(key, value); } catch (_) { /* private mode */ }
  }

  function setTheme(theme) {
    const value = theme === "dark" ? "dark" : "glass";
    if (document.body) document.body.dataset.siTheme = value;
    safeWrite(THEME_KEY, value);
    document.dispatchEvent(new CustomEvent("seriesui:theme", { detail: { theme: value } }));
  }

  function layer(kind) {
    let node = document.querySelector(`[data-si-${kind}-layer]`);
    if (node) return node;
    node = document.createElement("div");
    node.setAttribute(`data-si-${kind}-layer`, "");
    node.style.cssText = "position:fixed;z-index:1200;pointer-events:none;";
    if (kind === "toast") {
      node.style.cssText += "right:18px;bottom:18px;display:grid;gap:8px;width:min(380px,calc(100vw - 36px));";
    } else {
      node.style.cssText += "inset:0;display:grid;place-items:center;padding:18px;background:rgba(35,39,72,.28);backdrop-filter:blur(12px);";
    }
    document.body.appendChild(node);
    return node;
  }

  function toast(message, type = "info", duration = 2600) {
    const host = layer("toast");
    const item = document.createElement("div");
    item.className = `toast ${type === "error" ? "error" : ""}`;
    item.setAttribute("role", type === "error" ? "alert" : "status");
    item.style.pointerEvents = "auto";
    item.textContent = String(message || "");
    host.appendChild(item);
    window.setTimeout(() => {
      item.style.opacity = "0";
      item.style.transform = "translateY(8px)";
      window.setTimeout(() => item.remove(), 180);
    }, Math.max(800, Number(duration) || 2600));
    return item;
  }

  function modal({ title = "确认操作", message = "", confirmText = "确认", cancelText = "取消", danger = false, input = null } = {}) {
    return new Promise((resolve) => {
      const host = layer("modal");
      const dialog = document.createElement("div");
      dialog.className = "modal-card";
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
      dialog.style.pointerEvents = "auto";
      dialog.style.width = "min(520px, 100%)";
      dialog.style.padding = "20px";

      const heading = document.createElement("h3");
      heading.textContent = String(title);
      const copy = document.createElement("p");
      copy.className = "muted";
      copy.textContent = String(message);

      let field = null;
      if (input && typeof input === "object") {
        field = document.createElement("input");
        field.type = input.type === "password" ? "password" : "text";
        field.value = String(input.value || "");
        field.placeholder = String(input.placeholder || "");
        field.setAttribute("aria-label", String(input.label || "输入内容"));
      }

      const actions = document.createElement("div");
      actions.className = "dialog-actions";
      actions.style.cssText = "display:flex;justify-content:flex-end;gap:8px;margin-top:18px;";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = String(cancelText);
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.className = danger ? "danger" : "primary";
      confirm.textContent = String(confirmText);
      actions.append(cancel, confirm);
      dialog.append(heading, copy);
      if (field) dialog.append(field);
      dialog.append(actions);
      host.appendChild(dialog);
      overlays.push(dialog);

      const finish = (value) => {
        dialog.remove();
        if (!host.childElementCount) host.style.background = "transparent";
        const index = overlays.indexOf(dialog);
        if (index >= 0) overlays.splice(index, 1);
        resolve(value);
      };
      cancel.addEventListener("click", () => finish(null));
      confirm.addEventListener("click", () => finish(field ? field.value : true));
      dialog.addEventListener("keydown", (event) => {
        if (event.key === "Escape") finish(null);
        if (event.key === "Enter" && event.target === field) finish(field.value);
      });
      host.style.background = "rgba(35,39,72,.28)";
      (field || confirm).focus();
    });
  }

  function confirmDialog(options = {}) {
    return modal(options).then((value) => value !== null);
  }

  function promptDialog(options = {}) {
    return modal({ ...options, input: options.input || { label: "输入内容" } });
  }

  async function copyText(value) {
    const text = String(value || "");
    if (!text) return false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* fall back */ }
    const field = document.createElement("textarea");
    field.value = text;
    field.readOnly = true;
    field.style.cssText = "position:fixed;left:-9999px;top:0;";
    document.body.appendChild(field);
    field.focus();
    field.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (_) { copied = false; }
    field.remove();
    return copied;
  }

  function setBusy(button, busy, label = "") {
    if (!button) return () => {};
    const state = button.dataset.siBusyState;
    if (busy && state !== "1") {
      button.dataset.siBusyState = "1";
      button.dataset.siPreviousDisabled = button.disabled ? "1" : "0";
      button.dataset.siPreviousText = button.textContent || "";
      button.disabled = true;
      if (label) button.textContent = label;
    } else if (!busy && state === "1") {
      button.disabled = button.dataset.siPreviousDisabled === "1";
      button.textContent = button.dataset.siPreviousText || button.textContent;
      delete button.dataset.siBusyState;
    }
    return () => setBusy(button, false);
  }

  function decorate() {
    document.querySelectorAll('[role="button"]:not(button):not(a)').forEach((node) => {
      node.tabIndex = node.tabIndex >= 0 ? node.tabIndex : 0;
    });
  }

  function initialize() {
    markPage();
    decorate();
    const observer = new MutationObserver(decorate);
    observer.observe(document.body, { childList: true, subtree: true });
  }

  window.SeriesUI = Object.freeze({
    version: VERSION,
    ready,
    setTheme,
    toast,
    modal,
    confirm: confirmDialog,
    prompt: promptDialog,
    copy: copyText,
    setBusy
  });

  ready(initialize);
})();
