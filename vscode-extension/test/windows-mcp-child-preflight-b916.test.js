// B916: Windows MCP child interpreter preflight tests.
//
// Strategy: real fs + real child_process.spawnSync; each "Python" stub is a
// tiny per-file node script whose exit code and stderr are baked into the
// script body.  Only vscode is mocked (via Module._load, before extension.js
// executes) -- builtins fs/child_process remain real so venv stubs test the
// true preflight code path.

"use strict";

const { test, describe, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const childProcess = require("node:child_process");

// ── Mock vscode before extension.js loads ────────────────────────────────
const Module = require("module");
const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") {
    return {
      workspace: {
        getConfiguration: () => ({ get: () => "" }),
        workspaceFolders: [],
      },
      window: {
        createOutputChannel: () => ({
          appendLine: () => { },
          show: () => { },
          dispose: () => { },
        }),
        showInformationMessage: () => { },
        showQuickPick: () => Promise.resolve(null),
      },
      Uri: { file: (p) => ({ fsPath: p, toString: () => p }) },
      EventEmitter: class {},
      CancellationTokenSource: class {},
      ProgressLocation: { Notification: 15 },
    };
  }
  return origLoad.apply(this, arguments);
};

const { __testInternals } = require("../extension.js");
const findPythonCommand = __testInternals.findPythonCommand;
const findPythonCommandForLaunch = __testInternals.findPythonCommandForLaunch;
const _preflightPythonCandidate = __testInternals._preflightPythonCandidate;
const _buildPreflightDiagnostic = __testInternals._buildPreflightDiagnostic;

// ── Helpers ──────────────────────────────────────────────────────────────

/** Write a node script whose behaviour is baked into the file body. */
function writePythonStub(filePath, exitCode, stderrText) {
  // Absolute interpreter keeps these fixtures independent of PATH so the
  // two all-fallback-failure tests can deliberately hide host Python.  The
  // release workflow installs aiworkhub before running extension tests;
  // without this isolation its real `python` becomes a valid fallback and
  // correctly returns no diagnostic, making the negative test environment-
  // dependent.
  const lines = [`#!${process.execPath}`];
  // Ignore -c "import ..." args -- just exit with the baked code.
  if (stderrText) {
    lines.push(`process.stderr.write(${JSON.stringify(String(stderrText))});`);
  }
  lines.push(`process.exit(${Number(exitCode)});`);
  fs.writeFileSync(filePath, lines.join("\n") + "\n", { mode: 0o755 });
}

/** Create a .venv or venv tree under root with Scripts/python.exe. */
function createWindowsVenv(root, venvName, exitCode, stderr) {
  const scriptsDir = path.join(root, venvName, "Scripts");
  fs.mkdirSync(scriptsDir, { recursive: true });
  writePythonStub(path.join(scriptsDir, "python.exe"), exitCode, stderr);
}

/** Temp project root under os.tmpdir(). */
function createTempRoot() {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "b916-"));
  const root = path.join(base, "project");
  fs.mkdirSync(root, { recursive: true });
  return {
    root,
    rm() { try { fs.rmSync(base, { recursive: true, force: true }); } catch (_) {} },
  };
}

// ── Low-level: _preflightPythonCandidate ─────────────────────────────────

// These fixtures are executable Node scripts carrying a Python-like filename.
// They deliberately simulate Windows candidate paths while running on a POSIX
// host; native Windows cannot execute a shebang script named ``python.exe``.
const posixStubOnly = process.platform === "win32"
  ? "POSIX executable-stub fixture; native Windows has a real-interpreter smoke below"
  : false;

describe("_preflightPythonCandidate", { skip: posixStubOnly }, () => {
  let tmp;
  beforeEach(() => { tmp = createTempRoot(); });
  afterEach(() => tmp.rm());

  test("ok when child exits 0", () => {
    const stub = path.join(tmp.root, "good");
    writePythonStub(stub, 0);
    const r = _preflightPythonCandidate(stub, []);
    assert.ok(r.ok);
  });

  test("not ok with exitCode on non-zero exit", () => {
    // Use a temp stub that exits 3 (avoids node -e/-c conflict).
    const stub = path.join(tmp.root, "bad");
    writePythonStub(stub, 3, "some error output");
    const r = _preflightPythonCandidate(stub, []);
    assert.ok(!r.ok);
    assert.equal(r.exitCode, 3);
  });

  test("error on spawn failure", () => {
    const r = _preflightPythonCandidate("/nonexistent/xyzzy_no_such_file_42", []);
    assert.ok(!r.ok);
    assert.ok(r.error && r.error.length > 0);
  });
});

// ── Sanitisation ─────────────────────────────────────────────────────────

describe("sanitisation", { skip: posixStubOnly }, () => {
  test("redacts 24+ char tokens in stderr", () => {
    const stub = path.join(os.tmpdir(), "b916-sanitise-stub");
    writePythonStub(stub, 1, "err: x_abcdefghijklmnopqrstuvwx_01 extra");
    try {
      const r = _preflightPythonCandidate(stub, []);
      assert.ok(r.stderrTail.includes("[REDACTED]"));
      assert.ok(!r.stderrTail.includes("x_abcdefghij"));
    } finally {
      try { fs.unlinkSync(stub); } catch (_) {}
    }
  });
});

// ── _buildPreflightDiagnostic ────────────────────────────────────────────

describe("_buildPreflightDiagnostic", () => {
  test("empty for [] or all-ok", () => {
    assert.equal(_buildPreflightDiagnostic([]), "");
    assert.equal(_buildPreflightDiagnostic([{ ok: true, candidate: "py" }]), "");
  });

  test("includes failed, omits ok", () => {
    const d = _buildPreflightDiagnostic([
      { ok: false, candidate: "a", exitCode: 1, stderrTail: "E1" },
      { ok: true, candidate: "b" },
      { ok: false, candidate: "c", error: "ENOENT" },
    ]);
    assert.ok(d.includes("Python interpreter preflight diagnostics"));
    assert.ok(d.includes("a:") && d.includes("E1"));
    assert.ok(d.includes("c:") && d.includes("ENOENT"));
    assert.ok(!d.includes("b:"), "ok candidate should not appear");
  });

  test("caps at ~1200 chars", () => {
    const diags = Array.from({ length: 30 }, (_, i) => ({
      ok: false, candidate: `py${i}`, stderrTail: "X".repeat(200),
    }));
    const r = _buildPreflightDiagnostic(diags);
    assert.ok(r.length <= 1300, `got ${r.length}`);
    assert.ok(r.includes("truncated"));
  });
});

// ── Linux non-regression ─────────────────────────────────────────────────

describe("findPythonCommand (Linux)", () => {
  const orig = process.platform;
  beforeEach(() => { Object.defineProperty(process, "platform", { value: "linux" }); });
  afterEach(() => { Object.defineProperty(process, "platform", { value: orig }); });

  test("existence-based fallback to python3", () => {
    const r = findPythonCommand("/nonexistent/path/99");
    assert.equal(r.command, "python3");
    assert.deepEqual(r.argsPrefix, []);
  });
});

// ── Windows findPythonCommand (real temp dirs, real spawnSync) ───────────

describe("findPythonCommand (Windows)", { skip: posixStubOnly }, () => {
  const orig = process.platform;
  let tmp;

  beforeEach(() => {
    Object.defineProperty(process, "platform", { value: "win32" });
    tmp = createTempRoot();
  });
  afterEach(() => {
    Object.defineProperty(process, "platform", { value: orig });
    try { tmp.rm(); } catch (_) {}
  });

  test("selects .venv when preflight passes", () => {
    createWindowsVenv(tmp.root, ".venv", 0);
    const r = findPythonCommand(tmp.root);
    assert.ok(r.command.includes(".venv"), `got ${r.command}`);
    assert.equal(r.preflightDiagnostic, null);
  });

  test("falls back to venv when .venv broken", () => {
    createWindowsVenv(tmp.root, ".venv", 1, "ImportError: aiworkhub.server");
    createWindowsVenv(tmp.root, "venv", 0);
    const r = findPythonCommand(tmp.root);
    assert.ok(r.command.includes("venv") && !r.command.includes(".venv"),
      `got ${r.command}`);
    assert.equal(r.preflightDiagnostic, null);
  });

  test("falls back to system when both repo venvs broken", () => {
    createWindowsVenv(tmp.root, ".venv", 1, "ImportError");
    createWindowsVenv(tmp.root, "venv", 1, "ImportError");
    const previousPath = process.env.PATH;
    process.env.PATH = tmp.root;
    let r;
    try {
      r = findPythonCommand(tmp.root);
    } finally {
      process.env.PATH = previousPath;
    }
    // final fallback: py or python -- with diagnostic
    assert.ok(r.preflightDiagnostic && r.preflightDiagnostic.length > 0,
      "expected diagnostic");
    const diag = r.preflightDiagnostic;
    assert.ok(diag.includes("Python interpreter preflight diagnostics"), diag);
    assert.ok(diag.includes(".venv") || diag.includes("venv"), diag);
  });

  test("path with spaces", () => {
    const spaced = path.join(tmp.root, "my project with spaces");
    fs.mkdirSync(spaced, { recursive: true });
    createWindowsVenv(spaced, ".venv", 0);
    const r = findPythonCommand(spaced);
    assert.ok(r.command.includes(".venv"), `got ${r.command}`);
    assert.equal(r.preflightDiagnostic, null);
  });

  test("bounded diagnostic when every candidate fails", () => {
    createWindowsVenv(tmp.root, ".venv", 1, "ImportError: cannot import aiworkhub");
    createWindowsVenv(tmp.root, "venv", 127, "some other error");
    const previousPath = process.env.PATH;
    process.env.PATH = tmp.root;
    let r;
    try {
      r = findPythonCommand(tmp.root);
    } finally {
      process.env.PATH = previousPath;
    }
    assert.ok(r.preflightDiagnostic);
    assert.ok(r.preflightDiagnostic.length <= 1300);
    assert.ok(r.preflightDiagnostic.includes(".venv") || r.preflightDiagnostic.includes("venv"));
  });
});

test("Windows configuration resolution skips synchronous preflight", () => {
  const originalPlatform = process.platform;
  const originalSpawnSync = childProcess.spawnSync;
  let spawnCalls = 0;
  Object.defineProperty(process, "platform", { value: "win32" });
  childProcess.spawnSync = () => {
    spawnCalls += 1;
    throw new Error("configuration resolution must not spawn");
  };
  try {
    const resolved = findPythonCommand("C:\\missing-repository", { preflight: false });
    assert.equal(resolved.command, "py");
    assert.deepEqual(resolved.argsPrefix, ["-3"]);
    assert.equal(spawnCalls, 0);
  } finally {
    childProcess.spawnSync = originalSpawnSync;
    Object.defineProperty(process, "platform", { value: originalPlatform });
  }
});

test("Windows launch preflight yields the extension-host event loop", async () => {
  const originalPlatform = process.platform;
  const originalSpawnSync = childProcess.spawnSync;
  const originalExecFile = childProcess.execFile;
  let eventLoopYielded = false;
  let spawnCalls = 0;
  let execCalls = 0;
  Object.defineProperty(process, "platform", { value: "win32" });
  childProcess.spawnSync = () => {
    spawnCalls += 1;
    throw new Error("launch resolution must not block");
  };
  childProcess.execFile = (command, args, options, callback) => {
    execCalls += 1;
    setImmediate(() => callback(null, "", ""));
  };
  setImmediate(() => { eventLoopYielded = true; });
  try {
    const resolved = await findPythonCommandForLaunch("C:\\missing-repository");
    assert.equal(resolved.command, "py");
    assert.deepEqual(resolved.argsPrefix, ["-3"]);
    assert.equal(spawnCalls, 0);
    assert.equal(execCalls, 1);
    assert.equal(eventLoopYielded, true);
  } finally {
    childProcess.execFile = originalExecFile;
    childProcess.spawnSync = originalSpawnSync;
    Object.defineProperty(process, "platform", { value: originalPlatform });
  }
});

test(
  "native Windows resolves and preflights the installed system Python",
  { skip: process.platform !== "win32" },
  () => {
    const repoRoot = path.resolve(__dirname, "..", "..");
    const resolved = findPythonCommand(repoRoot);
    const preflight = _preflightPythonCandidate(resolved.command, resolved.argsPrefix || []);
    assert.ok(preflight.ok, JSON.stringify({ resolved, preflight }));
  },
);
