"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { test } = require("node:test");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

test("context viewer markers exist in extension.js and app.js", () => {
  for (const marker of [
    'id="open-system-log"',
    'id="open-sessions"',
    'id="open-ai-memory"',
    'id="open-kb"',
    'id="open-operations"',
    'id="open-tool-use"',
    'id="open-settings"',
    'id="sessions-dialog"',
    'id="kb-dialog"',
    'id="settings-dialog"',
    'id="header-context-graph"',
    'id="operations-dialog"',
  ]) {
    assert.ok(extension.includes(marker), `missing context viewer marker: ${marker}`);
  }

  assert.ok(extension.includes('sessions: "aiworkhub_dashboard_sessions"'));
  assert.ok(extension.includes('kb: "aiworkhub_dashboard_kb"'));
  assert.ok(extension.includes('settings: "aiworkhub_dashboard_settings"'));
  assert.ok(extension.includes('SETTINGS_UPDATE_TOOL = "aiworkhub_dashboard_settings_update"'));
  assert.ok(extension.includes('MODEL_SETTINGS_UPDATE_TOOL = "aiworkhub_dashboard_model_settings_update"'));
  assert.ok(extension.includes('SOURCE_GRAPH_SETTINGS_UPDATE_TOOL = "aiworkhub_dashboard_source_graph_settings_update"'));
  assert.ok(app.includes("function renderSessions(payload)"));
  assert.ok(app.includes("function renderKb(payload)"));
  assert.ok(app.includes('type: "requestSessions"'));
  assert.ok(app.includes('type: "requestKb"'));
  assert.ok(app.includes('type: "requestSettings"'));
  assert.ok(app.includes('type: "updateFeatureSetting"'));
  assert.ok(app.includes('type: "updateModelSetting"'));
  assert.ok(app.includes("function renderSettings(payload)"));
  assert.ok(app.includes('["context_graph", elements.headerContextGraph'));
  assert.ok(app.includes('type: "updateSourceGraphLanguage"'));
  assert.ok(app.includes("data-source-graph-language"));
  assert.ok(app.includes('state.settingsTab'));
  assert.ok(app.includes('dataset.settingsTab'));
  assert.ok(app.includes('"retention", "Retention"'));
  assert.ok(app.includes('["models", "Models"]'));
  assert.ok(app.includes("live VS Code/Copilot model"));
  assert.ok(app.includes("discovered in VS Code · no task capability assigned"));
  assert.ok(app.includes('"telemetry", "Telemetry"'));
  assert.ok(css.includes(".diagnostic-icon-button svg"));
  assert.ok(css.includes(".settings-row"));
  assert.ok(css.includes(".settings-tabs"));
  assert.ok(css.includes(".diagnostic-dialog.settings-dialog"));
  assert.ok(css.includes("scrollbar-gutter: stable"));
  assert.ok(css.includes(".settings-metric-grid"));
  assert.ok(css.includes(".settings-model-provider"));
});

test("app.js implements identity-based focus/scroll restore for the Models modal", () => {
  assert.ok(app.includes("function settingsControlIdentity"));
  assert.ok(app.includes("function settingsControlProvider"));
  assert.ok(app.includes("function settingsControlSection"));
  assert.ok(app.includes("function restoreSettingsFocus"));
  assert.ok(app.includes("elements.settingsList.contains(document.activeElement)"));
  assert.ok(app.includes("elements.settingsDialog.scrollTop"));
  assert.ok(app.includes("document.scrollingElement"));
  assert.ok(app.includes("restoreSettingsFocus(previousIdentity)"));
});

test("app.css keeps the settings dialog bounded, single-scroll and wrap-safe", () => {
  assert.ok(css.includes(".diagnostic-dialog.settings-dialog"));
  assert.ok(css.includes("width: min(880px, calc(100vw - 40px))"));
  assert.ok(css.includes("grid-template-rows: auto minmax(0, 1fr) auto"));
  assert.ok(css.includes("height: min(650px, calc(100vh - 60px))"));
  assert.ok(css.includes(".settings-list {"));
  assert.ok(css.includes("height: 100%"));
  assert.ok(css.includes(".settings-state"));
  assert.ok(css.includes(".settings-copy strong"));
  assert.ok(css.includes("overflow-wrap: anywhere"));
  assert.ok(css.includes(".settings-metric-grid,"));
  assert.ok(css.includes(".settings-language-grid { grid-template-columns: 1fr; }"));
});

// ── Minimal, dependency-free DOM harness ───────────────────────────────────
// Executes the real vscode-extension/media/app.js inside a vm context so the
// Models modal's actual render/focus/scroll code runs under test, without
// installing jsdom or any other package. Only the handful of DOM primitives
// app.js actually touches are implemented; anything else auto-vivifies a
// generic stub so unrelated render paths never crash the load.
function toCamel(kebab) {
  return kebab.replace(/-([a-z0-9])/g, (_, c) => c.toUpperCase());
}

function makeTextNode(text) {
  return { textContent: String(text), parentNode: null, children: [], tagName: null, dataset: {} };
}

function selectorParts(selector) {
  const parts = [];
  const re = /(#[^.#[\s]+)|(\.[^.#[\s]+)|(\[[^\]]+\])|([a-zA-Z][\w-]*)/g;
  let m;
  while ((m = re.exec(selector))) parts.push(m[0]);
  return parts;
}

function attrValue(el, attr) {
  if (attr.startsWith("data-")) return el.dataset ? el.dataset[toCamel(attr.slice(5))] : undefined;
  return el._attrs ? el._attrs[attr] : undefined;
}

function matchesPart(el, part) {
  if (!el) return false;
  if (part[0] === "#") return el.id === part.slice(1);
  if (part[0] === ".") return Boolean(el.classList) && el.classList.contains(part.slice(1));
  if (part[0] === "[") {
    const inner = part.slice(1, -1);
    const eq = inner.indexOf("=");
    if (eq === -1) return attrValue(el, inner.trim()) !== undefined;
    const attr = inner.slice(0, eq).trim();
    const value = inner.slice(eq + 1).trim().replace(/^['"]|['"]$/g, "");
    return attrValue(el, attr) === value;
  }
  return Boolean(el.tagName) && el.tagName.toLowerCase() === part.toLowerCase();
}

function matchesSelector(el, selector) {
  const parts = selectorParts(selector);
  return parts.length > 0 && parts.every((part) => matchesPart(el, part));
}

function queryAll(root, selector) {
  const results = [];
  const walk = (node) => {
    for (const child of node.children || []) {
      if (matchesSelector(child, selector)) results.push(child);
      walk(child);
    }
  };
  walk(root);
  return results;
}

class FakeClassList {
  constructor(el) {
    this.el = el;
    this.set = new Set();
  }
  add(...names) { names.forEach((n) => this.set.add(n)); }
  remove(...names) { names.forEach((n) => this.set.delete(n)); }
  toggle(name, force) {
    const has = this.set.has(name);
    const next = force === undefined ? !has : Boolean(force);
    if (next) this.set.add(name);
    else this.set.delete(name);
    return next;
  }
  contains(name) { return this.set.has(name); }
}

class FakeFragment {
  constructor() {
    this.__isFragment = true;
    this.children = [];
  }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  append(...nodes) { nodes.forEach((n) => this.appendChild(typeof n === "string" ? makeTextNode(n) : n)); }
}

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parentNode = null;
    this._attrs = {};
    this.dataset = {};
    this._listeners = {};
    this._text = "";
    this.classList = new FakeClassList(this);
    this.id = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.type = "";
    this.open = false;
    this.style = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
  }
  get className() { return Array.from(this.classList.set).join(" "); }
  get childNodes() { return this.children; }
  set className(value) {
    this.classList.set = new Set(String(value || "").split(/\s+/).filter(Boolean));
  }
  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((c) => c.textContent || "").join("");
  }
  set textContent(value) {
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    this._text = value == null ? "" : String(value);
  }
  setAttribute(name, value) { this._attrs[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null; }
  removeChildRef(node) { this.children = this.children.filter((c) => c !== node); }
  appendChild(node) {
    if (node && node.__isFragment) {
      const kids = node.children.slice();
      node.children = [];
      kids.forEach((k) => this.appendChild(k));
      return node;
    }
    if (node && node.parentNode && typeof node.parentNode.removeChildRef === "function") {
      node.parentNode.removeChildRef(node);
    }
    node.parentNode = this;
    this.children.push(node);
    return node;
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(typeof n === "string" ? makeTextNode(n) : n)); }
  prepend(...nodes) {
    nodes.reverse().forEach((n) => {
      const node = typeof n === "string" ? makeTextNode(n) : n;
      node.parentNode = this;
      this.children.unshift(node);
    });
  }
  replaceChildren(...nodes) {
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    this._text = "";
    nodes.forEach((n) => this.appendChild(typeof n === "string" ? makeTextNode(n) : n));
  }
  remove() { if (this.parentNode) this.parentNode.removeChildRef(this); }
  contains(node) {
    let cur = node;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parentNode;
    }
    return false;
  }
  closest(selector) {
    let cur = this;
    while (cur) {
      if (matchesSelector(cur, selector)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }
  matches(selector) { return matchesSelector(this, selector); }
  querySelector(selector) { return queryAll(this, selector)[0] || null; }
  querySelectorAll(selector) { return queryAll(this, selector); }
  addEventListener(type, handler) { (this._listeners[type] ||= []).push(handler); }
  removeEventListener(type, handler) {
    if (this._listeners[type]) this._listeners[type] = this._listeners[type].filter((h) => h !== handler);
  }
  // Test-only: invokes handlers registered via addEventListener, simulating a
  // delegated DOM event (`event.target` is the descendant that "received" it).
  _trigger(type, target) {
    const evt = { type, target: target || this, currentTarget: this, preventDefault() {}, stopPropagation() {} };
    for (const handler of this._listeners[type] || []) handler(evt);
    return evt;
  }
  focus() { FAKE_DOC._activeElement = this; }
  blur() { if (FAKE_DOC._activeElement === this) FAKE_DOC._activeElement = FAKE_DOC.body; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  scrollIntoView() {}
}

let FAKE_DOC = null;

class FakeDocument {
  constructor() {
    this.body = new FakeElement("body");
    this.documentElement = new FakeElement("html");
    this.scrollingElement = this.documentElement;
    this._idRegistry = new Map();
    this._activeElement = this.body;
    this._listeners = {};
    FAKE_DOC = this;
  }
  get activeElement() { return this._activeElement; }
  createElement(tag) { return new FakeElement(tag); }
  createDocumentFragment() { return new FakeFragment(); }
  getElementById(id) {
    if (!this._idRegistry.has(id)) {
      const el = new FakeElement("div");
      el.id = id;
      this._idRegistry.set(id, el);
    }
    return this._idRegistry.get(id);
  }
  querySelector(selector) {
    if (selector[0] === "#") return this.getElementById(selector.slice(1));
    return this.body.querySelector(selector);
  }
  querySelectorAll(selector) {
    if (selector[0] === "#") {
      const el = this.querySelector(selector);
      return el ? [el] : [];
    }
    return this.body.querySelectorAll(selector);
  }
  addEventListener(type, handler) { (this._listeners[type] ||= []).push(handler); }
}

function buildDomHarness() {
  const document = new FakeDocument();
  const sandbox = {
    document,
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
  };
  sandbox.window = sandbox;
  sandbox.__windowListeners = {};
  sandbox.addEventListener = (type, handler) => { (sandbox.__windowListeners[type] ||= []).push(handler); };
  sandbox.__posted = [];
  sandbox.acquireVsCodeApi = () => ({
    postMessage: (msg) => { sandbox.__posted.push(msg); },
    getState: () => sandbox.__state || {},
    setState: (s) => { sandbox.__state = s; },
  });
  const context = vm.createContext(sandbox);
  vm.runInContext(app, context, { filename: "app.js" });
  const run = (code) => vm.runInContext(code, context);
  return { context, run, document };
}

function baseModelPolicyPayload(workers, providers) {
  return {
    ok: true,
    revision: 3,
    features: { source_graph: true, session_manager: true, ai_memory: true, knowledge_base: true, context_graph: false },
    model_policy: {
      ok: true,
      revision: 5,
      providers,
      catalog: { discovered_model_count: workers.length, workers },
    },
    source_graph_policy: {
      ok: true,
      revision: 1,
      enabled_count: 1,
      language_count: 1,
      languages: [{ id: "js", label: "JavaScript", capability: "semantic", extensions: [".js"], enabled: true }],
    },
    retention_policy: {
      logs_days: 7, terminal_runs_days: 30, archived_tasks_days: 90,
      source_graph_generations: 3, worktree_max_bytes: 1000000,
    },
  };
}

const WORKERS_WITH_GLM = [
  { provider: "openai", adapter: "vscode_lm", model: "gpt-a", worker_id: "w1", effective_enabled: true, catalog_enabled: true },
  { provider: "openai", adapter: "vscode_lm", model: "gpt-b", worker_id: "w2", effective_enabled: true, catalog_enabled: true },
  { provider: "glm", adapter: "vscode_lm", model: "glm-4", worker_id: "w3", effective_enabled: true, catalog_enabled: true },
  { provider: "glm", adapter: "vscode_lm", model: "glm-4-air", worker_id: "w4", effective_enabled: true, catalog_enabled: true },
];

function renderPayload(run, workers, providers) {
  const payload = baseModelPolicyPayload(workers, providers);
  run(`renderSettings(${JSON.stringify(payload)});`);
}

// Each run() call compiles as its own vm script sharing the harness's global
// lexical scope, so top-level `const`/`let` names collide across repeated
// calls. Wrapping every multi-statement snippet in an IIFE keeps them scoped.
function focusModelsTab(run) {
  run(`(() => {
    const tabButton = Array.from(elements.settingsList.querySelectorAll("[data-settings-tab]"))
      .find((b) => b.dataset.settingsTab === "models");
    elements.settingsList._trigger("click", tabButton);
  })();`);
}

function focusRoute(run, provider, model) {
  run(`(() => {
    const route = Array.from(elements.settingsList.querySelectorAll("input"))
      .find((i) => i.dataset.modelProvider === ${JSON.stringify(provider)} && i.dataset.modelName === ${JSON.stringify(model)});
    route.focus();
  })();`);
}

test("Models modal preserves active control identity, Models tab and scroll across repeated re-renders", () => {
  const { run } = buildDomHarness();
  const providers = { openai: true, glm: true };

  renderPayload(run, WORKERS_WITH_GLM, providers);
  focusModelsTab(run);
  assert.strictEqual(run("state.settingsTab"), "models");

  for (let iteration = 0; iteration < 2; iteration += 1) {
    // Focus the glm-4 route checkbox and establish distinct scroll positions
    // at every level the fix is responsible for (settings-list, the dialog
    // itself, and the page behind it).
    focusRoute(run, "glm", "glm-4");
    run("elements.settingsList.scrollTop = 120; elements.settingsList.scrollHeight = 5000;");
    run("elements.settingsDialog.scrollTop = 7;");
    run("document.scrollingElement.scrollTop = 33;");

    renderPayload(run, WORKERS_WITH_GLM, providers);

    assert.strictEqual(run("state.settingsTab"), "models", "settings tab must not reset on re-render");
    assert.strictEqual(
      run('Array.from(elements.settingsList.querySelectorAll("[data-settings-panel]")).find((s) => s.dataset.settingsPanel === "models").hidden'),
      false,
      "Models panel must remain visible after re-render",
    );
    assert.strictEqual(run("elements.settingsList.children.length > 0"), true, "settings list must not render blank");
    assert.strictEqual(run("document.activeElement && document.activeElement.dataset.modelProvider"), "glm");
    assert.strictEqual(run("document.activeElement && document.activeElement.dataset.modelName"), "glm-4");
    assert.strictEqual(run("elements.settingsList.scrollTop"), 120);
    assert.strictEqual(run("elements.settingsDialog.scrollTop"), 7);
    assert.strictEqual(run("document.scrollingElement.scrollTop"), 33);
  }
});

test("Models modal falls back to a surviving provider control when the focused route disappears", () => {
  const { run } = buildDomHarness();
  const providers = { openai: true, glm: true };
  renderPayload(run, WORKERS_WITH_GLM, providers);

  focusRoute(run, "glm", "glm-4");
  run("elements.settingsList.scrollTop = 45; elements.settingsList.scrollHeight = 5000;");

  const workersWithoutGlm4 = WORKERS_WITH_GLM.filter((w) => w.model !== "glm-4");
  renderPayload(run, workersWithoutGlm4, providers);

  assert.strictEqual(run("elements.settingsList.children.length > 0"), true);
  // The exact glm-4 row is gone; the fallback must still land on a real,
  // currently visible control that belongs to the same surviving provider
  // (either the provider switch itself or a sibling route) -- never nothing.
  assert.strictEqual(run("document.activeElement && document.activeElement.dataset.modelProvider"), "glm");
  assert.strictEqual(
    run('Boolean(document.activeElement && elements.settingsList.contains(document.activeElement))'),
    true,
    "fallback control must be a real node still attached to the settings list",
  );
  assert.strictEqual(run("elements.settingsList.scrollTop"), 45);
});

test("Models modal falls back to the Models tab when the entire provider disappears", () => {
  const { run } = buildDomHarness();
  const providers = { openai: true, glm: true };
  renderPayload(run, WORKERS_WITH_GLM, providers);

  focusModelsTab(run);
  focusRoute(run, "glm", "glm-4");

  const openaiOnlyWorkers = WORKERS_WITH_GLM.filter((w) => w.provider !== "glm");
  renderPayload(run, openaiOnlyWorkers, { openai: true });

  assert.strictEqual(run("elements.settingsList.children.length > 0"), true, "modal must not go blank");
  assert.strictEqual(run("state.settingsTab"), "models", "tab selection itself must survive the fallback");
  assert.strictEqual(run("document.activeElement && document.activeElement.dataset.settingsTab"), "models");
});

test("Settings dialog renders a nonblank loading shell before the first settings payload", () => {
  const { run } = buildDomHarness();

  run('elements.openSettings._trigger("click", elements.openSettings);');

  assert.strictEqual(run("elements.settingsDialog.open"), true);
  assert.strictEqual(run("elements.settingsSummary.textContent"), "Loading repository feature settings");
  assert.strictEqual(run("elements.settingsList.children.length > 0"), true);
  assert.match(run("elements.settingsList.textContent"), /Loading repository feature settings/);
  assert.strictEqual(run('elements.settingsList.getAttribute("aria-busy")'), null);
  assert.deepStrictEqual(run("__posted.map((msg) => msg.type)"), ["ready", "requestSettings"]);
});

test("Models modal preserves last-good body and recovers pending controls after invalid and error payloads", () => {
  const { run } = buildDomHarness();
  const providers = { openai: true, glm: true };
  renderPayload(run, WORKERS_WITH_GLM, providers);
  focusModelsTab(run);
  focusRoute(run, "glm", "glm-4");
  run("elements.settingsDialog.showModal(); elements.settingsList.scrollTop = 88; elements.settingsDialog.scrollTop = 6; document.scrollingElement.scrollTop = 21;");

  run(`(() => {
    const route = Array.from(elements.settingsList.querySelectorAll("input"))
      .find((i) => i.dataset.modelProvider === "glm" && i.dataset.modelName === "glm-4");
    route.checked = false;
    elements.settingsList._trigger("change", route);
  })();`);

  assert.strictEqual(run("elements.settingsDialog.open"), true);
  assert.strictEqual(run('elements.settingsList.getAttribute("aria-busy")'), "true");
  assert.strictEqual(run("document.activeElement && document.activeElement.disabled"), true);
  assert.strictEqual(run("document.activeElement && document.activeElement.dataset.settingsPending"), "true");
  assert.deepStrictEqual(JSON.parse(run("JSON.stringify(__posted.slice(-1)[0])")), {
    type: "updateModelSetting",
    provider: "glm",
    adapter: "vscode_lm",
    model: "glm-4",
    enabled: false,
    expectedRevision: 5,
  });

  run('renderSettings({ ok: true, revision: 6, features: null });');
  assert.strictEqual(run("elements.settingsDialog.open"), true);
  assert.strictEqual(run("state.settingsTab"), "models");
  assert.strictEqual(run("elements.settingsList.children.length > 0"), true);
  assert.match(run("elements.settingsList.textContent"), /Repository model routes/);
  assert.strictEqual(run('elements.settingsList.getAttribute("aria-busy")'), "false");
  assert.strictEqual(run("document.activeElement && document.activeElement.dataset.modelProvider"), "glm");
  assert.strictEqual(run("document.activeElement && document.activeElement.dataset.modelName"), "glm-4");
  assert.strictEqual(run("document.activeElement && document.activeElement.disabled"), false);
  assert.strictEqual(run("elements.settingsList.scrollTop"), 88);
  assert.strictEqual(run("elements.settingsDialog.scrollTop"), 6);
  assert.strictEqual(run("document.scrollingElement.scrollTop"), 21);

  run('renderSettings({ ok: false, error: "Rejected settings update" });');
  assert.strictEqual(run("elements.settingsDialog.open"), true);
  assert.strictEqual(run("elements.settingsSummary.textContent"), "Rejected settings update");
  assert.match(run("elements.settingsList.textContent"), /Repository model routes/);
  assert.strictEqual(run("document.activeElement && document.activeElement.disabled"), false);
});

test("Settings CSS declares stable desktop and narrow modal geometry with list-only scroll", () => {
  assert.ok(css.includes("min-width: min(520px, calc(100vw - 40px))"));
  assert.ok(css.includes("max-height: min(650px, calc(100vh - 60px))"));
  assert.ok(css.includes("width: calc(100vw - 24px)"));
  assert.ok(css.includes("max-height: calc(100vh - 24px)"));
  assert.ok(css.includes("overflow: hidden"));
  assert.ok(css.includes("overflow-y: auto"));
  assert.ok(css.includes("overscroll-behavior: contain"));
});
