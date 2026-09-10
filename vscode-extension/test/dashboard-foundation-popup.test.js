"use strict";

const assert = require("assert");
const fs = require("fs");
const Module = require("module");
const path = require("path");
const vm = require("vm");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const extensionRoot = path.dirname(extensionPath);
const originalLoad = Module._load;

function mockUri(fsPath, query = "") {
  return {
    fsPath,
    query,
    with(changes) {
      return mockUri(changes.fsPath || fsPath, changes.query ?? query);
    },
    toString() {
      return `vscode-webview-resource:${fsPath}${query ? `?${query}` : ""}`;
    },
  };
}

const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: () => 10000, inspect: () => ({}) }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
  },
  Uri: {
    joinPath: (...parts) => mockUri(path.join(...parts.map((part) => part.fsPath || part))),
  },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};

Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};

let extension;
try {
  extension = require(extensionPath);
} finally {
  Module._load = originalLoad;
}

const internals = extension.__testInternals;
const css = fs.readFileSync(path.join(extensionRoot, "media", "app.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const appSource = fs.readFileSync(path.join(extensionRoot, "media", "app.js"), "utf8");
const html = internals.getHtmlForWebview(
  { cspSource: "https://example.vscode-cdn.net", asWebviewUri: (uri) => uri },
  { fsPath: extensionRoot },
);
const generated = internals.codingFoundationDashboardSource();

function mockNode(initial) {
  const listeners = {};
  return {
    textContent: "",
    title: "",
    open: false,
    focused: false,
    restored: false,
    attrs: {},
    dataset: {},
    listeners,
    setAttribute(name, next) {
      this.attrs[name] = next;
    },
    getAttribute(name) {
      return this.attrs[name];
    },
    addEventListener(type, fn) {
      listeners[type] = listeners[type] || [];
      listeners[type].push(fn);
    },
    click(target) {
      const event = { target: target || this, currentTarget: this };
      for (const fn of listeners.click || []) fn(event);
    },
    showModal() {
      this.open = true;
      this.focused = true;
      this.restored = false;
    },
    close() {
      this.open = false;
      this.focused = false;
      this.restored = true;
    },
    ...initial,
  };
}

function popupHarness() {
  const skills = mockNode({ attrs: { "data-state": "pending" } });
  const recipes = mockNode({ attrs: { "data-state": "pending" } });
  const semantic = mockNode({ attrs: { "data-state": "pending" } });
  const rules = mockNode({ attrs: { "data-state": "pending" } });
  const dialog = mockNode();
  const title = mockNode();
  const summary = mockNode();
  const body = mockNode();
  const nodes = {
    "header-development-rules": rules,
    "header-development-rules-value": mockNode(),
    "header-development-rules-detail": mockNode(),
    "header-skills": skills,
    "header-skills-value": mockNode(),
    "header-skills-detail": mockNode(),
    "header-tool-recipes": recipes,
    "header-tool-recipes-value": mockNode(),
    "header-tool-recipes-detail": mockNode(),
    "header-semantic-edit-coverage": semantic,
    "header-semantic-edit-coverage-value": mockNode(),
    "header-semantic-edit-coverage-detail": mockNode(),
    "coding-foundation-dialog": dialog,
    "coding-foundation-dialog-title": title,
    "coding-foundation-dialog-summary": summary,
    "coding-foundation-dialog-body": body,
  };
  let onMessage = null;
  vm.runInNewContext(generated, {
    document: { getElementById: (id) => nodes[id] || null },
    window: {
      addEventListener(type, listener) {
        if (type === "message") onMessage = listener;
      },
    },
  });
  return { nodes, onMessage, skills, recipes, semantic, dialog, title, summary, body };
}

const longSkills = {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.skills,
  state: "measured",
  count: 12,
  lifecycle: { proposed: 3, active: 7, retired: 2 },
  injectable_count: 1,
  accepted_evidence_count: 9,
  distinct_actor_count: 4,
  active_non_injectable_reasons: [
    "activation_evidence_below_two_distinct_actors",
    "unresolved_negative_evidence",
    "missing_injection_receipt_for_selected_skill",
    "provider_manifest_declared_but_runtime_capability_was_not_observed",
    "selected_skill_actor_identity_did_not_match_the_invocation_receipt_actor",
    "workspace_policy_prevented_injection_until_explicit_operator_approval",
  ],
  selection_injection: { state: "measured", count: 6 },
};

const longRecipes = {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.tool_recipes,
  state: "measured",
  count: 29,
  discovery_count: 29,
  usage: {
    state: "measured",
    used_count: 8,
    unused_count: 21,
    run_count: 82,
    attributed_run_count: 11,
    unattributed_run_count: 71,
    distinct_actor_count: 3,
  },
};

const longSemantic = {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.semantic_edit_coverage,
  state: "measured",
  measured_runs: 15,
  unmeasured_runs: 46,
  changed_paths: 22,
  range_count: 40,
  bytes_changed: 18432,
  paths_raw_only: 7,
  declared_exceptions: 4,
  derived_exceptions: 2,
  byte_coverage_rate: 87.7,
  adapters: [
    {
      name: "codex_cli",
      measured_attempts: 9,
      unmeasured_attempts: 12,
      semantic_only_attempts: 4,
      raw_only_attempts: 3,
      mixed_attempts: 2,
    },
    {
      name: "claude_cli",
      measured_attempts: 4,
      unmeasured_attempts: 20,
      semantic_only_attempts: 1,
      raw_only_attempts: 8,
      mixed_attempts: 1,
    },
    {
      name: "gemini_cli",
      measured_attempts: 2,
      unmeasured_attempts: 14,
      semantic_only_attempts: 0,
      raw_only_attempts: 9,
      mixed_attempts: 0,
    },
  ],
};

assert.match(html, /<button class="header-insight-card" id="header-skills"/);
assert.match(html, /<button class="header-insight-card" id="header-tool-recipes"/);
assert.match(html, /<button class="header-insight-card" id="header-semantic-edit-coverage"/);
assert.match(html, /aria-haspopup="dialog"/);
assert.match(html, /<dialog class="diagnostic-dialog coding-foundation-dialog" id="coding-foundation-dialog"/);
assert.match(html, /id="coding-foundation-dialog-title"/);
assert.match(html, /data-close-dialog="coding-foundation-dialog"/);
assert.doesNotMatch(generated, /innerHTML/);
assert.doesNotMatch(appSource, /openCodingFoundationHeaderDialog/);
assert.doesNotMatch(appSource, /codingFoundationDialog\.addEventListener\("click"/);
assert.strictEqual((generated.match(/card\.addEventListener\("click"/g) || []).length, 1);

const skillsBody = css.match(/#header-skills,\s*\n#header-tool-recipes,\s*\n#header-semantic-edit-coverage \{([^}]*max-height:[^}]*)\}/);
assert.ok(skillsBody, "popup header cards must share a containment rule");
assert.match(skillsBody[1], /max-height:\s*58px/);
assert.match(skillsBody[1], /overflow:\s*hidden/);
const dialogBody = css.match(/\.coding-foundation-dialog-body \{([^}]*)\}/);
assert.ok(dialogBody, "popup body rule missing");
assert.match(dialogBody[1], /overflow:\s*auto/);
assert.match(dialogBody[1], /overflow-wrap:\s*anywhere/);
assert.match(dialogBody[1], /contain:\s*paint/);
assert.match(dialogBody[1], /scrollbar-gutter:\s*stable/);
assert.match(dialogBody[1], /overscroll-behavior:\s*contain/);
assert.doesNotMatch(css, /html\s*\{[^}]*overflow:\s*hidden/);

{
  const harness = popupHarness();
  assert.strictEqual(typeof harness.onMessage, "function");
  harness.onMessage({
    data: {
      type: "snapshotSummary",
      payload: {
        snapshot_mode: "summary",
        omitted_fields: internals.CODING_FOUNDATION_CARD_KEYS,
      },
    },
  });
  harness.skills.click();
  assert.strictEqual(harness.dialog.open, true);
  assert.strictEqual(harness.dialog.focused, true);
  assert.strictEqual(harness.title.textContent, "Skills");
  assert.strictEqual(harness.body.textContent, "Awaiting the full snapshot");
  assert.doesNotMatch(harness.body.textContent, /injectable/);

  harness.onMessage({
    data: {
      type: "snapshot",
      payload: {
        snapshot_mode: "full",
        skills: longSkills,
        tool_recipes: longRecipes,
        semantic_edit_coverage: longSemantic,
      },
    },
  });
  const headerSkillsDetail = harness.nodes["header-skills-detail"].textContent;
  assert.strictEqual(harness.nodes["header-skills-value"].textContent, "12 skills");
  assert.match(headerSkillsDetail, /3 proposed · 7 active · 2 retired/);
  assert.ok(headerSkillsDetail.length <= 72, "Skills header summary must remain bounded");
  assert.doesNotMatch(headerSkillsDetail, /injectable|actors|activation_evidence/);
  assert.doesNotMatch(harness.nodes["header-tool-recipes-detail"].textContent, /unattributed|actors/);
  assert.doesNotMatch(harness.nodes["header-semantic-edit-coverage-detail"].textContent, /codex_cli|gemini_cli|paths/);
  assert.ok(harness.body.textContent.length > 240, "Skills popup must preserve a >240-character breakdown");
  assert.match(harness.body.textContent, /1 injectable/);
  for (const reason of longSkills.active_non_injectable_reasons) {
    assert.ok(harness.body.textContent.includes(reason), `Skills popup omitted reason: ${reason}`);
  }

  harness.recipes.click();
  assert.strictEqual(harness.title.textContent, "Tool Recipes");
  assert.match(harness.body.textContent, /8 used/);
  assert.match(harness.body.textContent, /71 unattributed/);
  assert.match(harness.body.textContent, /3 actors/);

  harness.semantic.click();
  assert.strictEqual(harness.title.textContent, "Semantic Edit");
  assert.match(harness.body.textContent, /46 unmeasured/);
  assert.match(harness.body.textContent, /codex_cli 9 measured\/12 unmeasured/);
  assert.match(harness.body.textContent, /gemini_cli 2 measured\/14 unmeasured/);
  assert.match(harness.body.textContent, /22 paths/);

  const close = { dataset: { closeDialog: "coding-foundation-dialog" } };
  harness.dialog.click(harness.dialog);
  assert.strictEqual(harness.dialog.open, false);
  assert.strictEqual(harness.dialog.restored, true);
  assert.ok(close.dataset.closeDialog);

  harness.skills.click();
  assert.strictEqual(harness.dialog.open, true);
  harness.dialog.close();
  assert.strictEqual(harness.dialog.restored, true);
}

{
  const harness = popupHarness();
  harness.onMessage({
    data: { type: "snapshot", payload: { snapshot_mode: "full", skills: longSkills } },
  });
  harness.skills.click();
  assert.match(harness.body.textContent, /4 actors/);
  harness.semantic.click();
  assert.strictEqual(harness.title.textContent, "Semantic Edit");
  assert.strictEqual(harness.body.textContent, "Awaiting the full snapshot");
  assert.doesNotMatch(harness.body.textContent, /actors|injectable/);
}

assert.match(html, /type="button"/);
assert.doesNotMatch(generated, /\.innerHTML\s*=/);

console.log("dashboard foundation popup: ok");
