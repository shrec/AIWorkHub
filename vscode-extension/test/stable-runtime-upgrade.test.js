"use strict";

// A VSIX upgrade must not remove executable Python code from beneath an
// already-running Codex/MCP process.  Each installed build is copied to one
// immutable content-addressed generation under extension-global storage; the
// next build advances current.json but retains the previous generation.

const assert = require("assert");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: () => "", inspect: () => ({}), update: async () => {} }),
  },
  window: { createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }) },
  commands: {},
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((p) => p.fsPath || p).join("/") }) },
  ConfigurationTarget: { Global: 1 },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
let extension;
try {
  delete require.cache[extensionPath];
  extension = require(extensionPath);
} finally {
  Module._load = originalLoad;
}

function makeInstalledExtension(root, version, marker) {
  const ext = path.join(root, `shrec.aiworkhub-${version}`);
  const pkg = path.join(ext, "runtime", "aiworkhub");
  fs.mkdirSync(pkg, { recursive: true });
  fs.writeFileSync(path.join(pkg, "__init__.py"), `__version__ = ${JSON.stringify(version)}\n`);
  fs.writeFileSync(path.join(pkg, "server.py"), `MARKER = ${JSON.stringify(marker)}\n`);
  fs.mkdirSync(path.join(ext, "bin"), { recursive: true });
  fs.writeFileSync(path.join(ext, "bin", "aiworkhub-app-server-mux"), "#!/usr/bin/env python3\n");
  if (process.platform !== "win32") fs.chmodSync(path.join(ext, "bin", "aiworkhub-app-server-mux"), 0o755);
  return ext;
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-stable-runtime-"));
try {
  const installs = path.join(tmp, "extensions");
  const storage = path.join(tmp, "global-storage");
  const oldExt = makeInstalledExtension(installs, "0.6.49", "old-live-process");
  const newExt = makeInstalledExtension(installs, "0.6.50", "new-process");

  const oldGeneration = extension.__testInternals.materializeStableRuntimeGeneration({
    extensionUri: { fsPath: oldExt },
    extension: { packageJSON: { version: "0.6.49" } },
    globalStorageUri: { fsPath: storage },
  });
  assert.ok(fs.existsSync(path.join(oldGeneration.runtimeDir, "aiworkhub", "server.py")));

  // Simulate VS Code deleting the old installed extension during upgrade.
  fs.rmSync(oldExt, { recursive: true, force: true });

  const newGeneration = extension.__testInternals.materializeStableRuntimeGeneration({
    extensionUri: { fsPath: newExt },
    extension: { packageJSON: { version: "0.6.50" } },
    globalStorageUri: { fsPath: storage },
  });

  assert.notStrictEqual(oldGeneration.generationRoot, newGeneration.generationRoot);
  assert.ok(fs.existsSync(path.join(oldGeneration.runtimeDir, "aiworkhub", "server.py")),
    "upgrade must retain the old generation for live processes");
  assert.ok(fs.existsSync(path.join(newGeneration.runtimeDir, "aiworkhub", "server.py")));
  const current = JSON.parse(fs.readFileSync(path.join(storage, "runtime", "current.json"), "utf8"));
  assert.strictEqual(current.runtime_dir, newGeneration.runtimeDir);
  assert.strictEqual(current.version, "0.6.50");

  // Same-version reinstall with different bytes gets a new content-addressed
  // generation rather than mutating code under a live process.
  fs.writeFileSync(path.join(newExt, "runtime", "aiworkhub", "server.py"), "MARKER = 'repacked'\n");
  const repacked = extension.__testInternals.materializeStableRuntimeGeneration({
    extensionUri: { fsPath: newExt },
    extension: { packageJSON: { version: "0.6.50" } },
    globalStorageUri: { fsPath: storage },
  });
  assert.notStrictEqual(repacked.generationRoot, newGeneration.generationRoot);
  assert.ok(fs.existsSync(path.join(newGeneration.runtimeDir, "aiworkhub", "server.py")));
  console.log("AIWorkHub stable runtime upgrade regression passed");
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
