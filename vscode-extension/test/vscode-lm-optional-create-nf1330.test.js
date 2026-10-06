"use strict";

// NF1330: optional new-file authority is separate from mandatory create
// outputs. These tests build the producer-style request/contract state and
// then exercise the actual host finalization helper for both the explicit
// required-create metadata path and the legacy fail-closed path.

const assert = require("assert");
const Module = require("module");
const path = require("path");
const { test } = require("node:test");

const originalLoad = Module._load;
const extensionPath = path.resolve(__dirname, "..", "extension.js");

function buildVscodeStub() {
  const channels = new Map();
  const outputChannel = () => ({
    appendLine() {},
    append() {},
    show() {},
    hide() {},
    dispose() {},
  });
  return new Proxy(
    {},
    {
      get(target, prop) {
        if (prop === "EventEmitter") {
          return class EventEmitter {
            constructor() {
              this.event = () => ({ dispose() {} });
            }
            fire() {}
          };
        }
        if (prop === "Uri") {
          return {
            file(filePath) {
              return { fsPath: String(filePath), path: String(filePath) };
            },
            parse(value) {
              return { fsPath: String(value) };
            },
          };
        }
        if (prop === "window") {
          return {
            createOutputChannel(name) {
              if (!channels.has(name)) channels.set(name, outputChannel());
              return channels.get(name);
            },
            showInformationMessage() {},
            showWarningMessage() {},
            showErrorMessage() {},
            activeTextEditor: undefined,
            visibleTextEditors: [],
          };
        }
        if (prop === "workspace") {
          return {
            workspaceFolders: [],
            getConfiguration() {
              return { get() { return undefined; } };
            },
            onDidChangeConfiguration() {
              return { dispose() {} };
            },
            fs: undefined,
          };
        }
        if (prop === "commands") {
          return {
            registerCommand() {
              return { dispose() {} };
            },
          };
        }
        if (prop === "languages") {
          return {
            registerCodeLensProvider() {
              return { dispose() {} };
            },
            registerCompletionItemProvider() {
              return { dispose() {} };
            },
          };
        }
        if (prop === "env") {
          return { appName: "nf1330-test", remoteName: undefined };
        }
        if (prop === "LanguageModelToolResultPart") {
          return class LanguageModelToolResultPart {
            constructor(callId, content) {
              this.callId = callId;
              this.content = content;
            }
          };
        }
        if (prop === "LanguageModelToolInvocationOptions") {
          return undefined;
        }
        return () => ({ dispose() {} });
      },
    }
  );
}

function loadExtension() {
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") return buildVscodeStub();
    return originalLoad.apply(this, arguments);
  };
  try {
    return require(extensionPath);
  } finally {
    Module._load = originalLoad;
  }
}

function contractMap() {
  return new Map([
    ["mgr.py", { action: "edit", current_sha256: "mgr-sha", line_count: 1 }],
    ["helper.py", { action: "create", current_sha256: "", line_count: 0 }],
    ["newtest.py", { action: "create", current_sha256: "", line_count: 0 }],
  ]);
}

function createItem(relative, content) {
  return { path: relative, content };
}

test("optional untouched new file may omit final output when required metadata is present", () => {
  const extension = loadExtension();
  const internals = extension.__testInternals;
  assert.strictEqual(
    typeof internals.vscodeLmRequiredCreateError,
    "function",
    "host finalization helper must be exported for the contract test"
  );
  const items = [createItem("newtest.py", "def test_new(): pass\n")];
  const error = internals.vscodeLmRequiredCreateError(
    items,
    contractMap(),
    "content",
    "v3_create",
    ["newtest.py"]
  );
  assert.strictEqual(error, "", "optional helper.py omission must be valid");
});

test("required new file omission still blocks finalization", () => {
  const extension = loadExtension();
  const internals = extension.__testInternals;
  const items = [createItem("helper.py", "def helper(): pass\n")];
  const error = internals.vscodeLmRequiredCreateError(
    items,
    contractMap(),
    "content",
    "v3_create",
    ["newtest.py"]
  );
  assert.match(
    error,
    /final_edit_fidelity_rejected:missing_required_create:newtest\.py/
  );
});

test("chosen optional create must be nonempty and nonplaceholder", () => {
  const extension = loadExtension();
  const internals = extension.__testInternals;
  const blankError = internals.vscodeLmRequiredCreateError(
    [createItem("newtest.py", "def test_new(): pass\n"), createItem("helper.py", "   \n")],
    contractMap(),
    "content",
    "v3_create",
    ["newtest.py"]
  );
  assert.match(blankError, /empty_required_create:helper\.py/);
  const ellipsisError = internals.vscodeLmRequiredCreateError(
    [createItem("newtest.py", "def test_new(): pass\n"), createItem("helper.py", "...")],
    contractMap(),
    "content",
    "v3_create",
    ["newtest.py"]
  );
  assert.strictEqual(ellipsisError, "", "host helper only checks presence/emptiness; Python final parser enforces ellipsis fidelity");
});

test("legacy request without explicit metadata fails closed on every create contract", () => {
  const extension = loadExtension();
  const internals = extension.__testInternals;
  const error = internals.vscodeLmRequiredCreateError(
    [createItem("newtest.py", "def test_new(): pass\n")],
    contractMap(),
    "content",
    "v3_create",
    undefined
  );
  assert.match(error, /missing_required_create:helper\.py/);
});

test("explicit empty required metadata allows all optional creates to be omitted", () => {
  const extension = loadExtension();
  const internals = extension.__testInternals;
  const error = internals.vscodeLmRequiredCreateError(
    [],
    contractMap(),
    "content",
    "v3_create",
    []
  );
  assert.strictEqual(error, "");
});
