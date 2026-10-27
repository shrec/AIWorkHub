"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const extensionSource = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8").replace(/\r\n/g, "\n");
const plain = (value) => JSON.parse(JSON.stringify(value));

function extractSlice(source, startMarker, endMarker, label) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `${label}: start marker not found`);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(end, -1, `${label}: end marker not found`);
  return source.slice(start, end + endMarker.length);
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

// Drives the shipped handleInboundMessage with a stubbed vscode.window, the
// way manager-chat-panel.test.js drives the Delete/Close modal.
function loadHarness({ inputs = [], warnings = [] } = {}) {
  const allowed = extractSlice(
    extensionSource,
    "const ALLOWED_INBOUND_MESSAGE_TYPES = new Set([",
    "]);",
    "ALLOWED_INBOUND_MESSAGE_TYPES",
  );
  const idRe = extractSlice(extensionSource, "const NEEDFIX_ID_RE = ", ";\n", "NEEDFIX_ID_RE");
  const helper = extractSlice(
    extensionSource,
    "const NEEDFIX_REASON_ACTIONS = new Set([",
    "return choice === label ? { reason, duplicateParentId } : null;\n}",
    "confirmNeedfixRequest",
  );
  const handler = extractSlice(
    extensionSource,
    "function handleInboundMessage(view, message) {",
    '      pushManagerLoopEvents(view, sessionId, Number.isFinite(afterSeq) && afterSeq >= 0 ? afterSeq : 0);\n      break;\n    }\n    default:\n      break;\n  }\n}',
    "handleInboundMessage",
  );

  const inputQueue = [...inputs];
  const warningQueue = [...warnings];
  const inputCalls = [];
  const warningCalls = [];
  const actionCalls = [];
  const context = {
    runNeedfixAction: (view, action, args) => {
      actionCalls.push({ action, args: plain(args) });
    },
    vscode: {
      window: {
        showInputBox: (options) => {
          inputCalls.push(options);
          return Promise.resolve(inputQueue.shift());
        },
        showWarningMessage: (message, options, ...actions) => {
          warningCalls.push({ message, options, actions });
          return Promise.resolve(warningQueue.length ? warningQueue.shift() : actions[0]);
        },
      },
    },
  };
  vm.createContext(context);
  vm.runInContext(`"use strict";\n${allowed}\n${idRe}\n${helper}\n${handler}\nthis.handle = handleInboundMessage;`, context);
  return { handle: context.handle, inputCalls, warningCalls, actionCalls };
}

const ID = "NF-2026-01467";

test("confirmed reject forwards the host-collected reason with host-set confirm", async () => {
  const h = loadHarness({ inputs: ["  not reproducible  "] });
  h.handle({}, { type: "needfixTransition", needfixId: ID, action: "reject", reason: "webview", confirm: false });
  await flush();
  assert.equal(h.inputCalls.length, 1);
  assert.equal(h.inputCalls[0].prompt, "reject reason");
  assert.equal(h.inputCalls[0].ignoreFocusOut, true);
  assert.equal(h.warningCalls.length, 1);
  assert.equal(h.warningCalls[0].message, `Confirm reject for ${ID}?`);
  assert.deepEqual(plain(h.warningCalls[0].options), { modal: true });
  assert.deepEqual(h.warningCalls[0].actions, ["Reject"]);
  assert.equal(h.actionCalls.length, 1);
  assert.equal(h.actionCalls[0].action, "transition");
  assert.equal(h.actionCalls[0].args.needfix_id, ID);
  assert.equal(h.actionCalls[0].args.action, "reject");
  assert.equal(h.actionCalls[0].args.reason, "not reproducible");
  assert.equal(h.actionCalls[0].args.confirm, true);
  assert.equal(h.actionCalls[0].args.include_item, true);
});

test("a webview-supplied confirm or reason never reaches the tool", async () => {
  const h = loadHarness({ inputs: ["host reason"], warnings: [undefined] });
  h.handle({}, { type: "needfixPurge", needfixId: ID, reason: "webview reason", confirm: true });
  await flush();
  assert.equal(h.actionCalls.length, 0);
});

test("a dismissed modal makes no call", async () => {
  const h = loadHarness({ inputs: ["why"], warnings: [undefined] });
  h.handle({}, { type: "needfixTransition", needfixId: ID, action: "resolve" });
  await flush();
  assert.equal(h.warningCalls.length, 1);
  assert.equal(h.actionCalls.length, 0);
});

test("a cancelled input box shows no modal and makes no call", async () => {
  const h = loadHarness({ inputs: [undefined] });
  h.handle({}, { type: "needfixTransition", needfixId: ID, action: "defer" });
  await flush();
  assert.equal(h.inputCalls.length, 1);
  assert.equal(h.warningCalls.length, 0);
  assert.equal(h.actionCalls.length, 0);
});

test("reject and purge with an empty reason make no call", async () => {
  for (const [type, action] of [["needfixTransition", "reject"], ["needfixPurge", "purge"]]) {
    const h = loadHarness({ inputs: ["   "] });
    h.handle({}, { type, needfixId: ID, action });
    await flush();
    assert.equal(h.warningCalls.length, 0, action);
    assert.equal(h.actionCalls.length, 0, action);
  }
});

test("triage with an empty reason is still allowed", async () => {
  const h = loadHarness({ inputs: [""] });
  h.handle({}, { type: "needfixTransition", needfixId: ID, action: "triage" });
  await flush();
  assert.equal(h.actionCalls.length, 1);
  assert.equal(h.actionCalls[0].args.reason, "");
});

test("duplicate with an invalid canonical id makes no call", async () => {
  const h = loadHarness({ inputs: ["not-an-id"] });
  h.handle({}, { type: "needfixTransition", needfixId: ID, action: "duplicate" });
  await flush();
  assert.equal(h.inputCalls[0].prompt, "Canonical NeedFix ID");
  assert.equal(h.warningCalls.length, 0);
  assert.equal(h.actionCalls.length, 0);
});

test("duplicate with a valid canonical id forwards duplicate_parent_id", async () => {
  const h = loadHarness({ inputs: ["NF-2026-00001"] });
  h.handle({}, { type: "needfixTransition", needfixId: ID, action: "duplicate" });
  await flush();
  assert.equal(h.actionCalls.length, 1);
  assert.equal(h.actionCalls[0].args.duplicate_parent_id, "NF-2026-00001");
  assert.equal(h.actionCalls[0].args.confirm, true);
});

test("purge, archive and restore keep their argument shapes", async () => {
  const purge = loadHarness({ inputs: ["cleanup"] });
  purge.handle({}, { type: "needfixPurge", needfixId: ID, action: "purge" });
  const archive = loadHarness({ inputs: ["done"] });
  archive.handle({}, { type: "needfixArchive", needfixId: ID, action: "archive" });
  const restore = loadHarness();
  restore.handle({}, { type: "needfixRestore", needfixId: ID, action: "restore" });
  await flush();
  assert.deepEqual(purge.actionCalls, [{ action: "purge", args: { needfix_id: ID, audit_reason: "cleanup", confirm: true } }]);
  assert.deepEqual(archive.actionCalls, [{ action: "archive", args: { needfix_id: ID, reason: "done", confirm: true } }]);
  assert.deepEqual(restore.actionCalls, [{ action: "restore", args: { needfix_id: ID, target_status: "captured", confirm: true } }]);
  assert.equal(restore.inputCalls.length, 0);
});

test("confirmed convertCommit forwards the plan digest and plan", async () => {
  const h = loadHarness();
  const taskPlan = { objective: "x" };
  h.handle({}, { type: "needfixConvertCommit", needfixId: ID, taskPlan, planDigest: "sha256:abc" });
  await flush();
  assert.equal(h.inputCalls.length, 0);
  assert.equal(h.warningCalls[0].message, `Create a task from ${ID}? This does not launch a worker.`);
  assert.deepEqual(h.warningCalls[0].actions, ["Create task"]);
  assert.deepEqual(h.actionCalls, [{
    action: "convertCommit",
    args: { needfix_id: ID, confirm: true, task_plan: taskPlan, plan_digest: "sha256:abc" },
  }]);
});

test("convertPreview needs no confirmation", async () => {
  const h = loadHarness();
  h.handle({}, { type: "needfixConvertPreview", needfixId: ID, taskPlan: { objective: "x" } });
  await flush();
  assert.equal(h.warningCalls.length, 0);
  assert.equal(h.inputCalls.length, 0);
  assert.deepEqual(h.actionCalls, [{ action: "convertPreview", args: { needfix_id: ID, task_plan: { objective: "x" } } }]);
});
