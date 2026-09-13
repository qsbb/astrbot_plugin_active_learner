/* 凝心 UI 1.0 — Shared interaction primitives
 * Canonical source: astrbot_plugin_update_manager/ui/series-ui.js
 * No framework dependency. Pages consume window.SeriesUI for overlays and state.
 */
(() => {
  "use strict";

  const VERSION = "1.0.2";
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
      node.style.zIndex = "1400";
      node.style.cssText += "right:18px;bottom:18px;display:grid;gap:8px;width:min(380px,calc(100vw - 36px));";
    } else {
      node.style.cssText += "inset:0;display:grid;place-items:center;padding:18px;background:rgba(35,39,72,.28);backdrop-filter:blur(12px);";
    }
    document.body.appendChild(node);
    return node;
  }

  function toast(message, type = "info", duration = 2600, action = null) {
    const host = layer("toast");
    const item = document.createElement("div");
    item.className = `toast ${type === "error" ? "error" : ""}`;
    item.setAttribute("role", type === "error" ? "alert" : "status");
    item.style.pointerEvents = "auto";
    const body = document.createElement("span");
    body.className = "toast-text";
    body.textContent = String(message || "");
    item.appendChild(body);
    let removed = false;
    let dismiss = () => {};
    if (action && typeof action.onClick === "function") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "toast-action";
      button.textContent = String(action.label || "撤销");
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        dismiss();
        action.onClick();
      });
      item.appendChild(button);
    }
    host.appendChild(item);
    dismiss = () => {
      if (removed) return;
      removed = true;
      item.style.opacity = "0";
      item.style.transform = "translateY(8px)";
      window.setTimeout(() => item.remove(), 180);
    };
    // 带撤销操作时给足反应时间（默认 5.2 秒），普通提示仍是 2.6 秒。
    const hold = action ? Math.max(5200, Number(duration) || 0) : Math.max(800, Number(duration) || 2600);
    window.setTimeout(dismiss, hold);
    return item;
  }

  const dialogStack = [];
  let dialogDepth = 0;
  let dialogSequence = 0;
  const inertStates = [];

  function setBackgroundInert(inert) {
    if (inert) {
      if (dialogDepth === 0) {
        inertStates.length = 0;
        [...document.body.children]
          .filter((node) => !node.matches("[data-si-toast-layer], [data-si-dialog-layer], script, style, link, noscript"))
          .forEach((node) => {
            inertStates.push({
              node,
              inert: node.inert,
              ariaHidden: node.getAttribute("aria-hidden"),
            });
            node.inert = true;
            node.setAttribute("aria-hidden", "true");
          });
      }
      dialogDepth += 1;
      return;
    }
    dialogDepth = Math.max(0, dialogDepth - 1);
    if (dialogDepth !== 0) return;
    inertStates.forEach(({ node, inert, ariaHidden }) => {
      node.inert = inert;
      if (ariaHidden === null) node.removeAttribute("aria-hidden");
      else node.setAttribute("aria-hidden", ariaHidden);
    });
    inertStates.length = 0;
  }

  function syncDialogStackFocus() {
    dialogStack.forEach((item, index) => {
      const active = index === dialogStack.length - 1;
      item.element.inert = !active;
      if (active) item.element.removeAttribute("aria-hidden");
      else item.element.setAttribute("aria-hidden", "true");
    });
  }

  function createDialogLayer() {
    const node = document.createElement("div");
    node.className = "si-dialog-layer";
    node.setAttribute("data-si-dialog-layer", "");
    node.style.cssText = "position:fixed;inset:0;z-index:1300;display:grid;place-items:center;padding:18px;background:rgba(35,39,72,.28);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);";
    document.body.appendChild(node);
    return node;
  }

  function focusableElements(root) {
    return [...root.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )].filter((node) => (
      (node.offsetParent !== null || node === document.activeElement) &&
      node.getAttribute("tabindex") !== "-1" &&
      !node.hidden &&
      node.getAttribute("aria-hidden") !== "true"
    ));
  }

  function resolveFocus(value, root) {
    if (!value) return null;
    const node = value instanceof Node
      ? value
      : (typeof value === "string" ? root.querySelector(value) : null);
    return node && root.contains(node) ? node : null;
  }

  function dialog(options = {}) {
    const {
      title = "对话框",
      body = "",
      actions = [],
      onAction = null,
      onClose = null,
      onKeydown = null,
      initialFocus = null,
      initialActionId = null,
      closeOnBackdrop = true,
      closeOnEscape = true,
      className = "",
      bodyClassName = "",
      width = "min(760px, 100%)",
    } = options;

    const host = createDialogLayer();
    const card = document.createElement("div");
    card.className = `modal-card ${className}`.trim();
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("tabindex", "-1");
    card.style.pointerEvents = "auto";
    card.style.width = width;

    const header = document.createElement("header");
    header.className = "modal-header";
    const titleId = `si-dialog-title-${++dialogSequence}`;
    card.setAttribute("aria-labelledby", titleId);
    const heading = document.createElement("h3");
    heading.id = titleId;
    heading.textContent = String(title);
    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "modal-close";
    closeButton.setAttribute("aria-label", "关闭");
    closeButton.textContent = "✕";
    header.append(heading, closeButton);

    const bodyHost = document.createElement("div");
    bodyHost.className = `modal-body ${bodyClassName}`.trim();
    const appendBody = (value) => {
      bodyHost.replaceChildren();
      if (value instanceof Node) bodyHost.appendChild(value);
      else if (typeof value === "string") {
        const paragraph = document.createElement("p");
        paragraph.className = "dialog-message";
        paragraph.textContent = value;
        bodyHost.appendChild(paragraph);
      }
    };
    appendBody(body);

    const footer = document.createElement("footer");
    footer.className = "modal-footer";
    const actionButtons = new Map();
    actions.forEach((action) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.siDialogAction = action.id;
      if (action.variant) button.className = action.variant;
      button.textContent = String(action.label || action.id);
      if (action.disabled) button.disabled = true;
      button.addEventListener("click", async () => {
        if (closed) return;
        const busyLabel = action.busyLabel || "";
        if (busyLabel) setBusy(button, true, busyLabel);
        try {
          const result = onAction ? await onAction(action.id, controller) : undefined;
          if (action.closeOnClick !== false && !closed) {
            controller.close(result === undefined ? action.id : result);
          }
        } catch (error) {
          toast(error?.message || String(error), "error");
        } finally {
          if (busyLabel) setBusy(button, false);
        }
      });
      actionButtons.set(action.id, button);
      footer.appendChild(button);
    });
    if (actions.length) card.append(header, bodyHost, footer);
    else card.append(header, bodyHost);
    host.appendChild(card);

    const previousFocus = document.activeElement;
    let closed = false;
    let resolveClosed;
    const closedPromise = new Promise((resolve) => { resolveClosed = resolve; });

    const close = (value = null) => {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", handleDocumentKeydown, true);
      const index = dialogStack.indexOf(controller);
      if (index >= 0) dialogStack.splice(index, 1);
      host.remove();
      document.removeEventListener("focusin", handleFocusIn, true);
      setBackgroundInert(false);
      syncDialogStackFocus();
      if (previousFocus && previousFocus.isConnected && typeof previousFocus.focus === "function") {
        previousFocus.focus();
      } else {
        const parentDialog = dialogStack[dialogStack.length - 1];
        parentDialog?.element?.focus?.();
      }
      try {
        if (typeof onClose === "function") onClose(value);
      } catch (error) {
        console.error(error);
      } finally {
        resolveClosed(value);
      }
    };

    const controller = {
      element: card,
      closed: closedPromise,
      close,
      isOpen: () => !closed,
      setBusy: (actionId, busy, label = "") => {
        const button = actionButtons.get(actionId);
        if (button) setBusy(button, busy, label);
      },
      setDisabled: (actionId, disabled) => {
        const button = actionButtons.get(actionId);
        if (button) button.disabled = Boolean(disabled);
      },
      update: (patch = {}) => {
        if (Object.prototype.hasOwnProperty.call(patch, "title")) {
          heading.textContent = String(patch.title);
        }
        if (Object.prototype.hasOwnProperty.call(patch, "body")) {
          appendBody(patch.body);
        }
      },
    };

    function handleFocusIn(event) {
      if (closed || dialogStack[dialogStack.length - 1] !== controller) return;
      if (!card.contains(event.target)) {
        (focusableElements(card)[0] || card).focus();
      }
    }
    document.addEventListener("focusin", handleFocusIn, true);

    const handleDocumentKeydown = (event) => {
      if (!controller.isOpen() || dialogStack[dialogStack.length - 1] !== controller) return;
      if (event.key === "Escape" && closeOnEscape) {
        event.preventDefault();
        event.stopPropagation();
        close(null);
        return;
      }
      if (typeof onKeydown === "function") onKeydown(event, controller);
    };
    document.addEventListener("keydown", handleDocumentKeydown, true);

    host.addEventListener("keydown", (event) => {
      if (event.key !== "Tab" || dialogStack[dialogStack.length - 1] !== controller) return;
      const focusable = focusableElements(card);
      if (!focusable.length) {
        event.preventDefault();
        card.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    host.addEventListener("click", (event) => {
      if (closeOnBackdrop && event.target === host) close(null);
    });
    closeButton.addEventListener("click", () => close(null));

    dialogStack.push(controller);
    setBackgroundInert(true);
    syncDialogStackFocus();
    const actionTarget = initialActionId ? actionButtons.get(initialActionId) : null;
    const target = resolveFocus(initialFocus, card) || actionTarget || focusableElements(card)[0] || card;
    target.focus();
    return controller;
  }

  function modal({ title = "确认操作", message = "", confirmText = "确认", cancelText = "取消", danger = false, input = null } = {}) {
    return new Promise((resolve) => {
      let field = null;
      const body = document.createElement("div");
      const copy = document.createElement("p");
      copy.className = "muted dialog-message";
      copy.style.whiteSpace = "pre-line";
      copy.textContent = String(message);
      body.appendChild(copy);
      if (input && typeof input === "object") {
        field = document.createElement("input");
        field.type = input.type === "password" ? "password" : "text";
        field.value = String(input.value || "");
        field.placeholder = String(input.placeholder || "");
        field.setAttribute("aria-label", String(input.label || "输入内容"));
        body.appendChild(field);
      }
      dialog({
        title,
        body,
        width: "min(520px, 100%)",
        initialFocus: field,
        initialActionId: field ? null : "confirm",
        closeOnBackdrop: false,
        actions: [
          { id: "cancel", label: cancelText },
          { id: "confirm", label: confirmText, variant: danger ? "danger" : "primary" },
        ],
        onAction: (id) => (id === "cancel" ? null : (field ? field.value : true)),
        onKeydown: (event, controller) => {
          if (event.key === "Enter" && event.target === field) {
            event.preventDefault();
            controller.close(field.value);
          }
        },
        onClose: (value) => resolve(value),
      });
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

  function bindTabs(root, tabSelector, panelSelector, activeAttribute = 'data-si-tab') {
    const scope = root || document;
    const tabs = [...scope.querySelectorAll(tabSelector)];
    const panels = [...scope.querySelectorAll(panelSelector)];
    const activate = (value) => {
      tabs.forEach((tab) => {
        const active = tab.getAttribute(activeAttribute) === value;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
      });
      panels.forEach((panel) => {
        panel.hidden = panel.getAttribute(activeAttribute.replace('tab', 'panel')) !== value;
      });
    };
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => activate(tab.getAttribute(activeAttribute)));
      tab.addEventListener('keydown', (event) => {
        let next = index;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        else return;
        event.preventDefault();
        const value = tabs[next].getAttribute(activeAttribute);
        activate(value);
        tabs[next].focus();
      });
    });
    if (tabs[0]) activate(tabs.find((tab) => tab.classList.contains('active'))?.getAttribute(activeAttribute) || tabs[0].getAttribute(activeAttribute));
    return { activate };
  }

  const SWITCH_INPUT_SELECTOR = 'input[type="checkbox"].si-toggle, .switch input[type="checkbox"], .si-switch input[type="checkbox"], input[type="checkbox"][role="switch"]';

  function syncSwitchState(input) {
    input.setAttribute('aria-checked', input.checked ? 'true' : 'false');
  }

  function enhanceSwitches(root) {
    (root || document).querySelectorAll(SWITCH_INPUT_SELECTOR).forEach((input) => {
      if (input.dataset.siSwitchBound !== '1') {
        input.dataset.siSwitchBound = '1';
        input.setAttribute('role', 'switch');
        input.addEventListener('change', () => syncSwitchState(input));
      }
      syncSwitchState(input);
    });
  }

  function decorate() {
    document.querySelectorAll('[role="button"]:not(button):not(a)').forEach((node) => {
      node.tabIndex = node.tabIndex >= 0 ? node.tabIndex : 0;
    });
    enhanceSwitches(document);
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
    dialog,
    modal,
    confirm: confirmDialog,
    prompt: promptDialog,
    copy: copyText,
    setBusy,
    bindTabs,
    enhanceSwitches
  });

  ready(initialize);
})();
