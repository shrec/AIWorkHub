"use strict";

// NF-2026-00008. readRepositoryManifestInfo binds a repository by opening the
// manifest with O_NOFOLLOW and requiring the OPEN descriptor to be the same file
// the preceding lstat saw. That check compared BOTH dev and ino.
//
// Measured on Windows (Node v22.16.0): fs.lstatSync(file).dev is 0 while
// fs.fstatSync(fd).dev is the real volume serial (2410075111 on this host), for
// the SAME file, with identical inodes. So `openedStat.dev !== manifestStat.dev`
// was unconditionally true and EVERY valid manifest came back as
// "manifest-unreadable" -- no repository could bind at all.
//
// A dev of 0 means "this platform did not report a device", not "a different
// volume". The binding is unchanged where the platform does report one, and the
// inode must still match exactly and be non-zero everywhere.

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
    registerWebviewPanelSerializer: () => ({ dispose: () => {} }),
    registerWebviewViewProvider: () => ({ dispose: () => {} }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
    setStatusBarMessage: () => {},
    showErrorMessage: () => {},
    showInformationMessage: () => {},
    showQuickPick: async () => undefined,
  },
  commands: { registerCommand: () => ({ dispose: () => {} }) },
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((p) => p.fsPath || p).join("/") }) },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};

const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
let extensionModule;
try {
  delete require.cache[extensionPath];
  extensionModule = require(extensionPath);
} finally {
  Module._load = originalLoad;
}

const { readRepositoryManifestInfo } = extensionModule.__testInternals;

function writeRepo(root, repoId, repoName) {
  fs.mkdirSync(path.join(root, ".aiworkhub"), { recursive: true });
  fs.writeFileSync(
    path.join(root, ".aiworkhub", "project.json"),
    JSON.stringify({
      schema_id: "aiworkhub.project_manifest.v1",
      manifest_version: 1,
      layout_version: 1,
      repo_id: repoId,
      repo_name: repoName,
      layout: {
        durable: {
          tasking: "tasking",
          source_graph: "source_graph",
          sessions: "sessions",
          memory: "memory",
          kb: "kb",
          config: "config",
        },
        runtime: { path: "runtime", durable: false, ignored: true },
      },
    }) + "\n",
    "utf8",
  );
}

const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-manifest-nf8-"));
try {
  // The reproduction itself: this host really does disagree between lstat and
  // fstat about dev, and the binding must survive that.
  {
    const probe = path.join(scratch, "probe.json");
    fs.writeFileSync(probe, "{}", "utf8");
    const linkStat = fs.lstatSync(probe);
    const fd = fs.openSync(probe, fs.constants.O_RDONLY);
    let openedStat;
    try {
      openedStat = fs.fstatSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    assert.strictEqual(
      openedStat.ino,
      linkStat.ino,
      "the same file must report the same inode through both stat forms",
    );
    if (process.platform === "win32" && linkStat.dev === 0) {
      // Not every Windows/Node build reproduces the historical failure mode
      // (observed on Node v22.16.0): some report no device from lstat while
      // fstat reports the real volume for the identical file. Others (e.g.
      // Node v20.20.2 on GitHub Actions' windows-latest runner) report the
      // same real, non-zero device from both calls. Only assert the
      // reproduction shape when this host actually exhibits it; either way,
      // the binding call below is what proves the production fix works.
      assert.notStrictEqual(openedStat.dev, 0, "Windows fstat reports a device");
    }
  }

  const repoRoot = path.join(scratch, "repo");
  fs.mkdirSync(repoRoot, { recursive: true });
  writeRepo(repoRoot, "repo_nf8_identity_binding", "nf8");

  const info = readRepositoryManifestInfo(repoRoot, "nf8");
  assert.strictEqual(
    info.repoId,
    "repo_nf8_identity_binding",
    `a valid manifest must bind, got ${info.repoId} (${info.storageStatus})`,
  );

  // A missing manifest is still missing, and a directory in its place is still
  // invalid: the relaxation is about the device field only.
  const emptyRoot = path.join(scratch, "empty");
  fs.mkdirSync(emptyRoot, { recursive: true });
  assert.strictEqual(readRepositoryManifestInfo(emptyRoot, "empty").repoId, "manifest-missing");

  const directoryRoot = path.join(scratch, "directory-manifest");
  fs.mkdirSync(path.join(directoryRoot, ".aiworkhub", "project.json"), { recursive: true });
  assert.strictEqual(
    readRepositoryManifestInfo(directoryRoot, "dir").repoId,
    "manifest-invalid",
  );

  // Malformed JSON is still refused rather than bound.
  const brokenRoot = path.join(scratch, "broken");
  fs.mkdirSync(path.join(brokenRoot, ".aiworkhub"), { recursive: true });
  fs.writeFileSync(path.join(brokenRoot, ".aiworkhub", "project.json"), "{ not json", "utf8");
  assert.notStrictEqual(
    readRepositoryManifestInfo(brokenRoot, "broken").repoId,
    "repo_nf8_identity_binding",
  );
} finally {
  fs.rmSync(scratch, { recursive: true, force: true });
}

console.log("AIWorkHub NF-2026-00008 manifest identity binding regression passed");
