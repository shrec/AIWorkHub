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
  const node = Object.assign({
    tagName: "",
    className: "",
    title: "",
    open: false,
    focused: false,
    restored: false,
    attrs: {},
    dataset: {},
    // NF-2026-01399: `style` records CSSOM assignments and `children` records
    // appendChild, which is what lets the Skills-window assertions check th
    // scope, <details> and the VS Code theme variables the window sets.
    style: {},
    children: [],
    ownText: "",
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
    appendChild(child) {
      this.children.push(child);
      return child;
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
  }, initial || {});
  // textContent behaves as the real one does in the two ways that matter:
  // reading concatenates descendant text, and WRITING replaces the children.
  // A plain property would have let a refill stack a second window inside the
  // dialog and still read correctly.
  Object.defineProperty(node, "textContent", {
    get() {
      let text = node.ownText;
      for (const child of node.children) text += child.textContent;
      return text;
    },
    set(value) {
      node.ownText = String(value);
      node.children = [];
    },
    enumerable: true,
    configurable: true,
  });
  return node;
}

function descendants(node, out) {
  const acc = out || [];
  for (const child of node.children) {
    acc.push(child);
    descendants(child, acc);
  }
  return acc;
}

function byTag(node, tag) {
  return descendants(node).filter((child) => child.tagName === tag);
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
    document: {
      getElementById: (id) => nodes[id] || null,
      // NF-2026-01399: the Skills window builds real elements, so the harness
      // has to hand out real-enough ones. The window is never rendered with
      // innerHTML, which is why this is all the DOM surface it needs.
      createElement: (tag) => mockNode({ tagName: String(tag).toUpperCase() }),
    },
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

// The popup cards are contained by the rule every header tile shares: a fixed
// label / value / two-line caption grid, with the caption clamped. The old
// containment -- max-height: 58px + overflow: hidden on these three cards
// only -- is what made them shorter than their row and cut their captions off
// (owner report), so it must not come back.
assert.doesNotMatch(css, /#header-skills,\s*\n#header-tool-recipes,\s*\n#header-semantic-edit-coverage \{[^}]*max-height:/,
  "the popup cards must not be clipped to a max-height again");
const tileBody = css.match(/\.header-insight-card \{([^}]*)\}/);
assert.ok(tileBody, "header tiles must share a containment rule");
assert.match(tileBody[1], /grid-template-rows:\s*auto auto calc\(/, "every tile reserves the same label / value / caption rows");
const captionBody = css.match(/\.header-insight-card > :last-child \{([^}]*)\}/);
assert.ok(captionBody, "tile captions must share a clamp rule");
assert.match(captionBody[1], /-webkit-line-clamp:\s*2/);
assert.match(captionBody[1], /overflow:\s*hidden/);
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

// ---------------------------------------------------------------------------
// NF-2026-01399: the Skills WINDOW. One aggregate string answered none of the
// questions an owner asks, so the dialog now renders KPI tiles, a plain
// pipeline sentence, one table row per record and a per-row details
// expansion. The aggregate line stays the fallback when rows are absent, and
// that is asserted too -- a window that blanked a rows-less snapshot would be
// worse than the string it replaced.
// ---------------------------------------------------------------------------

assert.match(generated, /function codingFoundationSkillsWindow/);
assert.match(generated, /CODING_FOUNDATION_SKILL_STYLES/);
assert.match(generated, /var\(--vscode-/);

const skillRows = [
  {
    identity: "commit-msg-check",
    version: "1.0.0",
    mined: false,
    stored_lifecycle: "proposed",
    effective_lifecycle: "proposed",
    what_it_does: "Reject <img onerror=alert(1)> in a commit message",
    procedure_steps: ["Reject <img onerror=alert(1)> in a commit message"],
    avoid_rules: [],
    vocabulary: {
      task_family: "commit",
      stage: "post-edit",
      path_or_symbol: "src/aiworkhub/skill_registry.py",
      risk: "medium",
      triggers: ["commit"],
      applicability: [],
    },
    injectable: false,
    injectable_reason: "activation_evidence_below_two_distinct_actors",
    accepted_actor_ids: ["worker.glm.5.3"],
    evidence_by_outcome: { accepted: 1, negative: 0 },
    evidence: [
      {
        outcome: "accepted",
        actor_id: "worker.glm.5.3",
        source: "file:<script>pwn</script>.json",
        note: "",
        resolved: false,
      },
    ],
    injection_count: 0,
    injected_cards: 0,
    selection_count: 0,
    last_selected_at: "",
    last_injected_at: "",
  },
  {
    identity: "ratchet-guard",
    version: "2.0.0",
    mined: false,
    stored_lifecycle: "active",
    effective_lifecycle: "active",
    what_it_does: "Lower the ratchet, never raise it",
    procedure_steps: ["Lower the ratchet, never raise it"],
    avoid_rules: ["never raise the ceiling"],
    vocabulary: {
      task_family: "refactor",
      stage: "pre-commit",
      path_or_symbol: "tests/test_module_size_ratchet.py",
      risk: "low",
      triggers: ["ratchet"],
      applicability: ["src/**"],
    },
    injectable: true,
    injectable_reason: "",
    accepted_actor_ids: ["worker.a", "worker.b"],
    evidence_by_outcome: { accepted: 2, negative: 0 },
    evidence: [
      { outcome: "accepted", actor_id: "worker.a", source: "file:a.json", note: "", resolved: false },
      { outcome: "accepted", actor_id: "worker.b", source: "file:b.json", note: "", resolved: false },
    ],
    injection_count: 2,
    injected_cards: 2,
    selection_count: 2,
    last_selected_at: "2026-10-08T11:00:00Z",
    last_injected_at: "2026-10-08T11:00:00Z",
  },
  {
    identity: "mined.dashboard-projection",
    version: "0.1.0",
    mined: true,
    stored_lifecycle: "proposed",
    effective_lifecycle: "proposed",
    what_it_does: "",
    procedure_steps: [],
    avoid_rules: [],
    vocabulary: {
      task_family: "", stage: "", path_or_symbol: "", risk: "", triggers: [], applicability: [],
    },
    injectable: false,
    injectable_reason: "lifecycle_state_is_proposed_not_active",
    accepted_actor_ids: ["worker.a", "worker.b"],
    evidence_by_outcome: { accepted: 2, negative: 0 },
    evidence: [],
    injection_count: 0,
    injected_cards: 0,
    selection_count: 1,
    last_selected_at: "2026-10-06T09:00:00Z",
    last_injected_at: "",
  },
];

const skillsWithRows = {
  schema_id: internals.CODING_FOUNDATION_SCHEMAS.skills,
  state: "measured",
  count: 3,
  lifecycle: { proposed: 2, active: 1, retired: 0 },
  injectable_count: 1,
  accepted_evidence_count: 5,
  distinct_actor_count: 3,
  active_non_injectable_reasons: [],
  selection_injection: { state: "measured", count: 3 },
  records: skillRows,
  records_total: 3,
  records_truncated: false,
  records_injected_card_count: 2,
};

{
  const harness = popupHarness();
  harness.onMessage({
    data: { type: "snapshot", payload: { snapshot_mode: "full", skills: skillsWithRows } },
  });
  harness.skills.click();
  const text = harness.body.textContent;

  // (a) four KPI tiles.
  for (const label of [
    "Total skills", "Active & injectable", "Waiting for evidence", "Cards that received a skill",
  ]) {
    assert.ok(text.includes(label), `missing KPI tile: ${label}`);
  }

  // (b) one plain-language pipeline sentence.
  assert.match(
    text,
    /1 of 3 skills reach workers; 2 do not, and 1 waits for accepted evidence from 2 different workers\./,
  );

  // (c) one table row per record, headed with scoped th cells.
  const columns = byTag(harness.body, "TH").filter((th) => th.attrs.scope === "col");
  assert.deepStrictEqual(
    columns.map((th) => th.textContent),
    ["Skill", "What it does", "Status", "Evidence", "Used", "Last evidence"],
  );
  assert.strictEqual(byTag(harness.body, "TH").filter((th) => th.attrs.scope === "row").length, 3);
  // Status is carried by the pill's TEXT. Colour never carries it alone.
  const pills = descendants(harness.body)
    .filter((node) => node.className === "coding-foundation-skills-pill");
  assert.deepStrictEqual(
    pills.map((pill) => pill.textContent),
    ["Waiting", "Reaching workers", "Waiting"],
  );
  assert.match(text, /needs accepted evidence from 2 different workers; has 1 \(worker\.glm\.5\.3\)/);
  assert.match(text, /has the evidence but is still proposed/);
  assert.match(text, /reaching workers now/);
  assert.match(text, /accepted ✓ 1 \/ negative ✗ 0 \/ 1 actor/);
  assert.match(text, /Injected into 2 cards, last 2026-10-08T11:00:00Z/);
  // A record never injected says so, and its last selection still says never.
  assert.match(text, /Never injected/);
  assert.match(text, /selected 1 time, last 2026-10-06T09:00:00Z/);
  assert.match(text, /selected 0 times, last never/);
  assert.ok(
    descendants(harness.body).some((node) => node.textContent === "mined"),
    "a mined.* identity must carry the mined badge",
  );

  // (d) a per-row details expansion with steps, vocabulary and evidence.
  assert.strictEqual(byTag(harness.body, "DETAILS").length, 3);
  assert.strictEqual(byTag(harness.body, "SUMMARY").length, 3);
  assert.match(text, /Procedure steps/);
  assert.match(text, /Vocabulary/);
  assert.match(text, /Evidence history/);
  assert.match(text, /task family: commit/);
  assert.match(text, /applies to: src\/\*\*/);
  assert.match(text, /Lifecycle: stored active, effective active/);
  assert.match(text, /No procedure steps recorded/);
  assert.match(text, /No selection vocabulary declared/);
  assert.match(text, /No evidence recorded yet/);

  // Untrusted stored strings render as TEXT, never as markup.
  assert.match(text, /Reject <img onerror=alert\(1\)> in a commit message/);
  assert.match(text, /file:<script>pwn<\/script>\.json/);
  assert.ok(
    descendants(harness.body).every((node) => node.tagName !== "IMG" && node.tagName !== "SCRIPT"),
    "stored text must never be parsed into elements",
  );

  // Layout comes from the VS Code theme variables the dashboard already uses.
  const themed = descendants(harness.body)
    .filter((node) => JSON.stringify(node.style).includes("var(--vscode-"));
  assert.ok(themed.length >= 10, "the window must be themed with VS Code CSS variables");

  // Refilling replaces the window instead of stacking a second one.
  const rendered = harness.body.children.length;
  harness.skills.click();
  assert.strictEqual(harness.body.children.length, rendered);
  assert.strictEqual(byTag(harness.body, "DETAILS").length, 3);
}

{
  // Zero records is an explicit empty state, not an empty table.
  const harness = popupHarness();
  harness.onMessage({
    data: {
      type: "snapshot",
      payload: {
        snapshot_mode: "full",
        skills: {
          schema_id: internals.CODING_FOUNDATION_SCHEMAS.skills,
          state: "no_sample",
          count: 0,
          records: [],
          records_total: 0,
          records_truncated: false,
          records_injected_card_count: 0,
        },
      },
    },
  });
  harness.skills.click();
  assert.match(harness.body.textContent, /No skills yet/);
  assert.strictEqual(byTag(harness.body, "TABLE").length, 0);
  assert.doesNotMatch(harness.body.textContent, /Total skills/);
}

{
  // Rows absent -> the old aggregate line, unchanged. A measured window is
  // then retained across the summary refreshes that omit `records`.
  const harness = popupHarness();
  harness.onMessage({
    data: { type: "snapshot", payload: { snapshot_mode: "full", skills: longSkills } },
  });
  harness.skills.click();
  assert.strictEqual(byTag(harness.body, "TABLE").length, 0);
  assert.match(harness.body.textContent, /1 injectable/);
  assert.match(harness.body.textContent, /activation_evidence_below_two_distinct_actors/);

  harness.onMessage({
    data: { type: "snapshot", payload: { snapshot_mode: "full", skills: skillsWithRows } },
  });
  harness.skills.click();
  assert.strictEqual(byTag(harness.body, "TABLE").length, 1);
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
  assert.strictEqual(byTag(harness.body, "TABLE").length, 1);
}

{
  // An absent `records` is not an empty registry.
  assert.strictEqual(internals.codingFoundationSkillRowModel({ state: "measured" }), null);
  const truncated = internals.codingFoundationSkillRowModel({
    records: [skillRows[0]],
    records_total: "unknown",
    records_truncated: true,
  });
  assert.strictEqual(truncated.totalExact, false);
  assert.strictEqual(truncated.rows.length, 1);
  // Above the bound EVERY derived number is qualified, not just the total: the
  // sentence scopes itself to the shown rows, and so does each tile counted
  // over them.
  assert.strictEqual(
    internals.codingFoundationSkillSentence(truncated),
    "No skill among the 1 shown reaches workers: 0 of 1+ are active; "
      + "1 waits for accepted evidence from 2 different workers.",
  );
  assert.strictEqual(
    internals.codingFoundationSkillSentence({
      rows: [{}, {}, {}], total: 3, totalExact: false, injectable: 1, active: 1, waiting: 1,
    }),
    "at least 1 of 3+ shown skills reach workers; 2 do not, and 1 waits for "
      + "accepted evidence from 2 different workers.",
  );
  assert.deepStrictEqual(
    internals.codingFoundationSkillKpis(truncated)
      .map((tile) => tile.label + ": " + tile.value),
    [
      "Total skills: 1+",
      "Active & injectable: 0 (first 1 shown)",
      "Waiting for evidence: 1 (first 1 shown)",
      "Cards that received a skill: Not measured",
    ],
  );
  // An exact total leaves the tiles unqualified, so the suffix never reads as
  // decoration.
  assert.deepStrictEqual(
    internals.codingFoundationSkillKpis(internals.codingFoundationSkillRowModel(skillsWithRows))
      .map((tile) => tile.value),
    ["3", "1", "1", "2"],
  );
  // Unmeasured and zero are different facts and never share a sentence.
  assert.strictEqual(internals.codingFoundationSkillUsed({}), "Not measured");
  assert.strictEqual(internals.codingFoundationSkillUsed({ injected_cards: 0 }), "Never injected");
  assert.strictEqual(
    internals.codingFoundationSkillUsed({ injected_cards: 1, last_injected_at: "" }),
    "Injected into 1 card, last never",
  );
  // A reason named after an Object.prototype member must not resolve to one.
  assert.strictEqual(
    internals.codingFoundationSkillReason({ injectable: false, injectable_reason: "constructor" }),
    "constructor",
  );
  assert.strictEqual(
    internals.codingFoundationSkillReason({ injectable: false, injectable_reason: "" }),
    "not reaching workers; no reason was recorded",
  );
  assert.strictEqual(
    internals.codingFoundationSkillStatus({ injectable: false, effective_lifecycle: "retired" }),
    "Retired",
  );
  assert.strictEqual(
    internals.codingFoundationSkillSentence({
      rows: [{}], total: 10, totalExact: true, injectable: 0, active: 0, waiting: 9,
    }),
    "No skill reaches workers yet: 0 of 10 are active; 9 wait for accepted evidence from 2 different workers.",
  );
}

assert.match(html, /type="button"/);
assert.doesNotMatch(generated, /\.innerHTML\s*=/);

console.log("dashboard foundation popup: ok");
