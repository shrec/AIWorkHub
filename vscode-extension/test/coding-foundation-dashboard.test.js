"use strict";

const assert = require("assert");
const crypto = require("crypto");
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

function mockSlot(value, detail) {
  return {
    card: {
      title: "",
      attrs: {},
      setAttribute(name, next) {
        this.attrs[name] = next;
      },
    },
    value: { textContent: value },
    detail: { textContent: detail },
  };
}

function mockElements() {
  return {
    development_rules: mockSlot("No sample", "No evidence"),
    skills: mockSlot("No sample", "No evidence"),
    tool_recipes: mockSlot("No sample", "No evidence"),
    semantic_edit_coverage: mockSlot("No sample", "No evidence"),
  };
}

function snapshotHtml() {
  return internals.getHtmlForWebview(
    {
      cspSource: "https://example.vscode-cdn.net",
      asWebviewUri: (uri) => uri,
    },
    { fsPath: extensionRoot },
  );
}

function contentAddressedAssetUri(fileName) {
  const assetPath = path.join(extensionRoot, "media", fileName);
  const digest = crypto.createHash("sha256").update(fs.readFileSync(assetPath)).digest("hex").slice(0, 16);
  return `vscode-webview-resource:${assetPath}?v=${digest}`;
}

const html = snapshotHtml();
const stylesheetUri = contentAddressedAssetUri("app.css");
const scriptUri = contentAddressedAssetUri("app.js");
assert.ok(html.includes(`href="${stylesheetUri}"`), "stylesheet URI must include its content identity");
assert.ok(html.includes(`src="${scriptUri}"`), "script URI must include its content identity");
const insights = html.match(/<div class="header-insights"[\s\S]*?<\/div>\s*<\/header>/);
assert.ok(insights, "header-insights grid missing");
assert.match(insights[0], /id="header-development-rules"/);
assert.match(insights[0], />Development Rules</);
assert.match(insights[0], /id="header-skills"/);
assert.match(insights[0], />Skills</);
assert.match(insights[0], /id="header-tool-recipes"/);
assert.match(insights[0], />Tool Recipes</);
assert.match(insights[0], /id="header-semantic-edit-coverage"/);
assert.match(insights[0], />Semantic Edit</);
// NF-2026-00675: the static markup must not assert "No sample" before any
// projection has arrived. The default snapshot omits all three fields, so a
// first paint that claims no sample is a measured verdict about a thing
// nobody looked at -- which is what the owner read as the rule count vanishing.
assert.match(insights[0], /id="header-development-rules-value">Loading/);
assert.match(insights[0], /id="header-skills-value">Loading/);
assert.match(insights[0], /id="header-tool-recipes-value">Loading/);
assert.match(insights[0], /id="header-semantic-edit-coverage-value">Loading/);
assert.doesNotMatch(insights[0], /id="header-development-rules-value">No sample/);
assert.match(insights[0], /id="header-development-rules" data-state="pending"/);
assert.doesNotMatch(insights[0], /id="header-development-rules-value">0/);
assert.doesNotMatch(insights[0], /id="header-skills-value">0</);
assert.doesNotMatch(insights[0], /id="header-tool-recipes-value">0</);
assert.doesNotMatch(insights[0], /id="header-semantic-edit-coverage-value">0</);
assert.doesNotMatch(insights[0], /id="header-tool-recipes-value">0</);
assert.match(insights[0], /id="header-storage"/);
assert.match(insights[0], /id="header-preflight"/);
assert.match(insights[0], /class="header-insight-card"/);
assert.doesNotMatch(insights[0], /overflow\s*:\s*(auto|scroll)/i);
assert.doesNotMatch(insights[0], /position\s*:\s*sticky/i);
assert.match(html, /bindCodingFoundationDashboard/);
assert.match(html, /snapshotSummary/);

const elements = mockElements();
internals.renderCodingFoundationCards({
  development_rules: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.development_rules,
    state: "measured",
    declared_rule_count: 4,
    resolved_rule_count: 3,
    violation_evidence_state: "measured",
    violation_count: 1,
    version: "v1",
  },
  skills: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.skills,
    state: "measured",
    count: 6,
    lifecycle: { proposed: 1, active: 4, retired: 1 },
    injectable_count: 0,
    accepted_evidence_count: 5,
    distinct_actor_count: 2,
    active_non_injectable_reasons: [
      "activation_evidence_below_two_distinct_actors",
      "unresolved_negative_evidence",
    ],
    active_non_injectable_reasons_truncated: false,
    selection_injection: { state: "measured", count: 2 },
    outcome: { state: "measured", count: 1 },
  },
  tool_recipes: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.tool_recipes,
    state: "measured",
    count: 5,
    discovery_count: 5,
    invocation: { state: "measured", count: 8 },
    usage: {
      state: "measured",
      registered_count: 5,
      used_count: 2,
      unused_count: 3,
      run_count: 8,
      attributed_run_count: 3,
      unattributed_run_count: 5,
      distinct_actor_count: 1,
    },
  },
  semantic_edit_coverage: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.semantic_edit_coverage,
    state: "measured",
    measured_runs: 3,
    unmeasured_runs: 1,
    changed_paths: 4,
    range_count: 5,
    bytes_changed: 400,
    paths_raw_only: 2,
    declared_exceptions: 1,
    derived_exceptions: 1,
    byte_coverage_rate: 50,
    adapters: [
      {
        name: "codex_cli",
        measured_attempts: 3,
        unmeasured_attempts: 0,
        semantic_only_attempts: 1,
        raw_only_attempts: 1,
        mixed_attempts: 1,
      },
      {
        name: "claude_cli",
        measured_attempts: 0,
        unmeasured_attempts: 1,
        semantic_only_attempts: 0,
        raw_only_attempts: 0,
        mixed_attempts: 0,
      },
    ],
    token_savings_available: false,
    cost_savings_available: false,
  },
}, elements);
assert.strictEqual(elements.development_rules.value.textContent, "4 rules");
assert.match(elements.development_rules.detail.textContent, /3 resolved/);
assert.match(elements.development_rules.detail.textContent, /1 viol/);
assert.strictEqual(elements.development_rules.card.attrs["data-state"], "measured");
assert.strictEqual(elements.skills.value.textContent, "6 skills");
assert.match(elements.skills.detail.textContent, /1 proposed · 4 active · 1 retired/);
assert.match(elements.skills.detail.textContent, /0 injectable/);
assert.match(elements.skills.detail.textContent, /2 selection\/injection receipts/);
assert.match(elements.skills.detail.textContent, /5 accepted/);
assert.match(elements.skills.detail.textContent, /2 actors/);
assert.match(elements.skills.detail.textContent, /activation_evidence_below_two_distinct_actors/);
assert.match(elements.skills.detail.textContent, /unresolved_negative_evidence/);
assert.strictEqual(elements.tool_recipes.value.textContent, "5 recipes");
assert.match(elements.tool_recipes.detail.textContent, /2 used/);
assert.match(elements.tool_recipes.detail.textContent, /3 unused/);
assert.match(elements.tool_recipes.detail.textContent, /8 runs/);
assert.match(elements.tool_recipes.detail.textContent, /5 unattributed/);
assert.doesNotMatch(elements.tool_recipes.detail.textContent, /8 uses/);
assert.strictEqual(elements.semantic_edit_coverage.value.textContent, "3 measured");
assert.match(elements.semantic_edit_coverage.detail.textContent, /1 unmeasured/);
assert.match(elements.semantic_edit_coverage.detail.textContent, /4 paths/);
assert.match(elements.semantic_edit_coverage.detail.textContent, /5 ranges/);
assert.match(elements.semantic_edit_coverage.detail.textContent, /2 raw-only/);
assert.match(elements.semantic_edit_coverage.detail.textContent, /codex_cli 3 measured\/0 unmeasured/);
assert.match(elements.semantic_edit_coverage.detail.textContent, /1 semantic\/1 raw\/1 mixed/);
assert.match(elements.semantic_edit_coverage.detail.textContent, /claude_cli 0 measured\/1 unmeasured/);
assert.doesNotMatch(elements.semantic_edit_coverage.detail.textContent, /token/);
assert.doesNotMatch(elements.semantic_edit_coverage.detail.textContent, /cost/);

const preservedSkillsValue = elements.skills.value.textContent;
const preservedSkillsDetail = elements.skills.detail.textContent;
const preservedRecipesValue = elements.tool_recipes.value.textContent;
const preservedRecipesDetail = elements.tool_recipes.detail.textContent;
internals.renderCodingFoundationCards({
  development_rules: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.development_rules,
    state: "unavailable",
    reason: "storage_not_ready",
  },
  tool_recipes: {},
  header_storage: { state: "measured", count: 0 },
}, elements);
assert.strictEqual(elements.development_rules.value.textContent, "Unavailable");
assert.strictEqual(elements.development_rules.detail.textContent, "storage_not_ready");
assert.strictEqual(elements.skills.value.textContent, preservedSkillsValue);
assert.strictEqual(elements.skills.detail.textContent, preservedSkillsDetail);
assert.strictEqual(elements.tool_recipes.value.textContent, preservedRecipesValue);
assert.strictEqual(elements.tool_recipes.detail.textContent, preservedRecipesDetail);
assert.match(html, /id="header-storage-managed">Calculating</);
assert.match(html, /id="header-preflight-value">Checking</);

internals.renderCodingFoundationCards({
  development_rules: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.development_rules,
    state: "no_sample",
    declared_rule_count: 0,
    resolved_rule_count: 0,
    violation_count: 0,
  },
  skills: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.skills,
    state: "no_sample",
    count: 0,
  },
  tool_recipes: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.tool_recipes,
    state: "unavailable",
    count: 0,
  },
}, elements);
assert.strictEqual(elements.development_rules.value.textContent, "No sample");
assert.strictEqual(elements.development_rules.detail.textContent, "No evidence");
assert.strictEqual(elements.skills.value.textContent, "No sample");
assert.notStrictEqual(elements.development_rules.value.textContent, "0");
assert.notStrictEqual(elements.skills.value.textContent, "0 skills");
assert.strictEqual(elements.tool_recipes.value.textContent, "Unavailable");
assert.notStrictEqual(elements.tool_recipes.value.textContent, "0 recipes");

const noSample = internals.codingFoundationCardModel("skills", { state: "no_sample", count: 0 });
assert.strictEqual(noSample.value, "No sample");
const unavailable = internals.codingFoundationCardModel("tool_recipes", { state: "unavailable" });
assert.strictEqual(unavailable.value, "Unavailable");
assert.strictEqual(internals.codingFoundationCardModel("skills", {}), null);
assert.strictEqual(internals.codingFoundationHeaderMarkup().includes("header-insight-card"), true);

const unknownUsage = internals.codingFoundationCardModel("tool_recipes", {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.tool_recipes,
  state: "measured",
  count: 5,
  usage: { state: "unknown" },
  invocation: { state: "measured", count: 8 },
});
assert.strictEqual(unknownUsage.value, "5 recipes");
assert.match(unknownUsage.detail, /UNKNOWN usage/);
assert.doesNotMatch(unknownUsage.detail, /8 uses/);

const zeroUsage = internals.codingFoundationCardModel("tool_recipes", {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.tool_recipes,
  state: "measured",
  count: 4,
  usage: {
    state: "measured",
    registered_count: 4,
    used_count: 0,
    unused_count: 4,
    run_count: 0,
    attributed_run_count: 0,
    unattributed_run_count: 0,
    distinct_actor_count: 0,
  },
});
assert.match(zeroUsage.detail, /0 used/);
assert.match(zeroUsage.detail, /4 unused/);
assert.match(zeroUsage.detail, /0 runs/);

const unattributed = internals.codingFoundationCardModel("tool_recipes", {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.tool_recipes,
  state: "measured",
  count: 1,
  usage: {
    state: "measured",
    registered_count: 1,
    used_count: 1,
    unused_count: 0,
    run_count: 2,
    attributed_run_count: 0,
    unattributed_run_count: 2,
    distinct_actor_count: 0,
  },
});
assert.match(unattributed.detail, /2 unattributed/);
assert.match(unattributed.detail, /0 attributed/);

const unknownEdit = internals.codingFoundationCardModel("semantic_edit_coverage", {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.semantic_edit_coverage,
  state: "unknown",
});
assert.strictEqual(unknownEdit.value, "UNKNOWN");
assert.notStrictEqual(unknownEdit.value, "0");

const mixedAdapters = internals.codingFoundationCardModel("semantic_edit_coverage", {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.semantic_edit_coverage,
  state: "measured",
  measured_runs: 2,
  unmeasured_runs: 1,
  adapters: [
    {
      name: "codex_cli",
      measured_attempts: 2,
      unmeasured_attempts: 0,
      semantic_only_attempts: 1,
      raw_only_attempts: 0,
      mixed_attempts: 1,
    },
    {
      name: "claude_cli",
      measured_attempts: 0,
      unmeasured_attempts: 1,
      semantic_only_attempts: 0,
      raw_only_attempts: 0,
      mixed_attempts: 0,
    },
  ],
  token_savings_available: false,
});
assert.strictEqual(mixedAdapters.value, "2 measured");
assert.match(mixedAdapters.detail, /codex_cli 2 measured\/0 unmeasured/);
assert.match(mixedAdapters.detail, /1 semantic\/0 raw\/1 mixed/);
assert.match(mixedAdapters.detail, /claude_cli 0 measured\/1 unmeasured/);
assert.doesNotMatch(mixedAdapters.detail, /token/);

// Execute the exact script embedded in the Webview. Direct unit calls above do
// not detect helper functions accidentally omitted from the generated source.
{
  const generatedElements = mockElements();
  const nodes = {};
  for (const [kind, slot] of Object.entries(generatedElements)) {
    const id = `header-${kind.replace(/_/g, "-")}`;
    nodes[id] = slot.card;
    nodes[`${id}-value`] = slot.value;
    nodes[`${id}-detail`] = slot.detail;
  }
  let onMessage = null;
  vm.runInNewContext(internals.codingFoundationDashboardSource(), {
    document: { getElementById: (id) => nodes[id] || null },
    window: {
      addEventListener(type, listener) {
        if (type === "message") onMessage = listener;
      },
    },
  });
  assert.strictEqual(typeof onMessage, "function");

  onMessage({
    data: {
      type: "snapshotSummary",
      payload: {
        snapshot_mode: "summary",
        full_snapshot_available: true,
        omitted_fields: internals.CODING_FOUNDATION_CARD_KEYS,
      },
    },
  });
  assert.strictEqual(generatedElements.skills.value.textContent, "Loading");

  onMessage({
    data: {
      type: "snapshot",
      payload: {
        snapshot_mode: "full",
        development_rules: { state: "measured", declared_rule_count: 20 },
        skills: { state: "measured", count: 4, lifecycle: { proposed: 4, active: 0, retired: 0 } },
        tool_recipes: { state: "measured", count: 29, usage: { state: "measured", used_count: 8, unused_count: 21, run_count: 82 } },
        semantic_edit_coverage: { state: "measured", measured_runs: 15, unmeasured_runs: 46, byte_coverage_rate: 87.7 },
      },
    },
  });
  assert.strictEqual(generatedElements.development_rules.value.textContent, "20 rules");
  assert.strictEqual(generatedElements.skills.value.textContent, "4 skills");
  assert.strictEqual(generatedElements.tool_recipes.value.textContent, "29 recipes");
  assert.strictEqual(generatedElements.semantic_edit_coverage.value.textContent, "15 measured");
}

console.log("coding foundation dashboard: ok");

// NF-2026-00675: an omitted field reads as pending, and a summary refresh must
// never blank a card the full snapshot already measured. The owner read
// "Unavailable / No evidence" on Development Rules while the full snapshot
// carried 20 rules the whole time.
{
  const pendingSlot = (value, detail) => ({
    card: { title: "", attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } },
    value: { textContent: value },
    detail: { textContent: detail },
  });
  const cards = {
    development_rules: pendingSlot("Loading", "Awaiting the full snapshot"),
    skills: pendingSlot("Loading", "Awaiting the full snapshot"),
    tool_recipes: pendingSlot("Loading", "Awaiting the full snapshot"),
    semantic_edit_coverage: pendingSlot("Loading", "Awaiting the full snapshot"),
  };

  internals.renderCodingFoundationCards({
    snapshot_mode: "summary",
    full_snapshot_available: true,
    omitted_fields: ["development_rules", "skills", "tool_recipes", "semantic_edit_coverage"],
  }, cards);
  assert.strictEqual(cards.development_rules.value.textContent, "Loading");
  assert.strictEqual(cards.development_rules.card.attrs["data-state"], "pending");
  assert.notStrictEqual(cards.development_rules.value.textContent, "Unavailable");
  assert.notStrictEqual(cards.development_rules.detail.textContent, "No evidence");

  internals.renderCodingFoundationCards({
    snapshot_mode: "full",
    development_rules: {
      schema_id: "aiworkhub.dashboard.development_rules.v1",
      state: "measured",
      declared_rule_count: 20,
      resolved_rule_count: 8,
    },
  }, cards);
  assert.strictEqual(cards.development_rules.value.textContent, "20 rules");
  assert.strictEqual(cards.development_rules.card.attrs["data-state"], "measured");

  internals.renderCodingFoundationCards({
    snapshot_mode: "summary",
    full_snapshot_available: true,
    omitted_fields: ["development_rules"],
  }, cards);
  assert.strictEqual(cards.development_rules.value.textContent, "20 rules");
}
