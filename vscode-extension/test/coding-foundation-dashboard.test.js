"use strict";

const assert = require("assert");
const crypto = require("crypto");
const fs = require("fs");
const Module = require("module");
const path = require("path");

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
assert.match(insights[0], /id="header-development-rules-value">No sample/);
assert.match(insights[0], /id="header-skills-value">No sample/);
assert.match(insights[0], /id="header-tool-recipes-value">No sample/);
assert.doesNotMatch(insights[0], /id="header-development-rules-value">0/);
assert.doesNotMatch(insights[0], /id="header-skills-value">0</);
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
    selection: { state: "measured", count: 2 },
    invocation: { state: "measured", count: 3 },
    outcome: { state: "measured", count: 1 },
  },
  tool_recipes: {
    schema_id: internals.CODING_FOUNDATION_SCHEMAS.tool_recipes,
    state: "measured",
    count: 5,
    discovery_count: 5,
    invocation: { state: "measured", count: 8 },
    cache: { state: "measured", eligible_count: 3, ineligible_count: 5 },
  },
}, elements);
assert.strictEqual(elements.development_rules.value.textContent, "4 rules");
assert.match(elements.development_rules.detail.textContent, /3 resolved/);
assert.match(elements.development_rules.detail.textContent, /1 viol/);
assert.strictEqual(elements.development_rules.card.attrs["data-state"], "measured");
assert.strictEqual(elements.skills.value.textContent, "6 skills");
assert.match(elements.skills.detail.textContent, /1 proposed · 4 active · 1 retired/);
assert.strictEqual(elements.tool_recipes.value.textContent, "5 recipes");
assert.match(elements.tool_recipes.detail.textContent, /8 uses/);
assert.match(elements.tool_recipes.detail.textContent, /3 cache-ok/);

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

console.log("coding foundation dashboard: ok");
