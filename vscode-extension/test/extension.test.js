"use strict";

// NOTE: this suite runs under `node --test vscode-extension/test/`, which is
// NOT part of the repository's declared validation command set. It is kept as
// the developer-facing detail suite; the assertions that must actually execute
// in CI drive these same exported internals through a real `node` child from
// tests/test_opencode_workforce_integration.py, which pytest does run.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const Module = require("node:module");

// extension.js requires("vscode") at module scope; that module exists only
// inside a running VS Code extension host. None of the pure functions this
// file targets (via __testInternals) touch vscode.* at require time, so a
// minimal stub is enough to load the real module and exercise its real code,
// with no need to string-slice the source text.
const VSCODE_STUB_ID = "\0aiworkhub-vscode-stub";
const originalResolveFilename = Module._resolveFilename;
Module._resolveFilename = function patchedResolveFilename(request, ...rest) {
  if (request === "vscode") return VSCODE_STUB_ID;
  return originalResolveFilename.call(this, request, ...rest);
};
require.cache[VSCODE_STUB_ID] = {
  id: VSCODE_STUB_ID,
  filename: VSCODE_STUB_ID,
  loaded: true,
  exports: {
    workspace: {
      getConfiguration: () => ({ get: (_key, fallback) => fallback }),
      workspaceFolders: [],
    },
    window: {},
    commands: {},
    extensions: { getExtension: () => null },
    ConfigurationTarget: { Global: 1 },
  },
};

const { __testInternals } = require("../extension.js");
const {
  resolveOpencodeConfigJsonPath,
  isOwnedOpencodeMcpEntry,
  repairOpencodeConfigJsonObject,
  readOpencodeConfigDocument,
  atomicWriteJsonPreservingMode,
} = __testInternals;

test("resolveOpencodeConfigJsonPath honors an explicit OPENCODE_CONFIG override", () => {
  const resolved = resolveOpencodeConfigJsonPath({ OPENCODE_CONFIG: "/custom/opencode.json" });
  assert.equal(resolved, "/custom/opencode.json");
});

test("resolveOpencodeConfigJsonPath falls back to XDG_CONFIG_HOME/opencode/opencode.json", () => {
  const resolved = resolveOpencodeConfigJsonPath({ XDG_CONFIG_HOME: "/home/x/.config" });
  assert.equal(resolved, path.join("/home/x/.config", "opencode", "opencode.json"));
});

test("repairOpencodeConfigJsonObject creates a repository-neutral entry with no repo identity keys", () => {
  const { document, changed } = repairOpencodeConfigJsonObject({}, ["python3", "/launcher.py"]);
  assert.equal(changed, true);
  const entry = document.mcp.aiworkhub;
  assert.deepEqual(entry.command, ["python3", "/launcher.py"]);
  assert.equal(entry.type, "local");
  assert.equal(entry.enabled, true);
  for (const key of ["AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO", "AIWORKHUB_REPO_ID"]) {
    assert.equal(Object.prototype.hasOwnProperty.call(entry.environment, key), false);
  }
});

test("repairOpencodeConfigJsonObject strips only AIWorkHub-owned repository identity keys on repair", () => {
  const before = {
    theme: "dark",
    permission: { "*": "allow" },
    mcp: {
      aiworkhub: {
        type: "local",
        command: ["stale-python", "/old/launcher.py"],
        enabled: false,
        environment: {
          AIWORKHUB_REPO_ROOT: "/repo/a",
          AIWORKHUB_REPO: "/repo/a",
          AIWORKHUB_REPO_ID: "repo_deadbeef",
          AIWORKHUB_ALLOW_WRITES: "0",
          SOME_SECRET: "keep-me",
        },
      },
      "unrelated-server": {
        type: "local",
        command: ["node", "unrelated.js"],
        environment: { AIWORKHUB_REPO_ROOT: "/should/not/be/touched" },
      },
    },
  };
  const { document, changed } = repairOpencodeConfigJsonObject(before, ["python3", "/new/launcher.py"]);
  assert.equal(changed, true);
  assert.equal(document.theme, "dark");
  assert.deepEqual(document.permission, { "*": "allow" });
  const entry = document.mcp.aiworkhub;
  assert.deepEqual(entry.command, ["python3", "/new/launcher.py"]);
  assert.equal(entry.enabled, false, "an operator-disabled entry must not be silently re-enabled");
  assert.equal(entry.environment.AIWORKHUB_ALLOW_WRITES, "0", "an operator-set capability gate must not be overwritten");
  assert.equal(entry.environment.SOME_SECRET, "keep-me");
  for (const key of ["AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO", "AIWORKHUB_REPO_ID"]) {
    assert.equal(Object.prototype.hasOwnProperty.call(entry.environment, key), false);
  }
  assert.deepEqual(document.mcp["unrelated-server"].environment, { AIWORKHUB_REPO_ROOT: "/should/not/be/touched" });
});

test("repairOpencodeConfigJsonObject repairs every AIWorkHub-owned entry, not just the first", () => {
  // "aiworkhub_ultrafast" is declared FIRST here on purpose: the canonical
  // entry must be picked by name, never by Object.entries ordering.
  const before = {
    mcp: {
      aiworkhub_ultrafast: {
        type: "local",
        command: ["stale-python", "/old/ultrafast-launcher.py"],
        environment: {
          AIWORKHUB_REPO_ROOT: "/repo/a",
          AIWORKHUB_REPO: "/repo/a",
          AIWORKHUB_REPO_ID: "repo_deadbeef",
          KEEP_ME: "ultrafast-flag",
        },
      },
      aiworkhub: {
        type: "local",
        command: ["stale-python", "/old/launcher.py"],
        environment: {
          AIWORKHUB_REPO_ROOT: "/repo/a",
          AIWORKHUB_REPO: "/repo/a",
          AIWORKHUB_REPO_ID: "repo_deadbeef",
        },
      },
    },
  };
  const { document, changed } = repairOpencodeConfigJsonObject(before, ["python3", "/new/launcher.py"]);
  assert.equal(changed, true);
  for (const ownedName of ["aiworkhub", "aiworkhub_ultrafast"]) {
    const entry = document.mcp[ownedName];
    for (const key of ["AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO", "AIWORKHUB_REPO_ID"]) {
      assert.equal(
        Object.prototype.hasOwnProperty.call(entry.environment, key),
        false,
        `${ownedName} must not retain ${key}`,
      );
    }
  }
  assert.equal(document.mcp.aiworkhub_ultrafast.environment.KEEP_ME, "ultrafast-flag");
  // Only the canonical entry is re-pointed at the stable launcher; a distinct
  // owned registration keeps its own launcher.
  assert.deepEqual(document.mcp.aiworkhub.command, ["python3", "/new/launcher.py"]);
  assert.deepEqual(
    document.mcp.aiworkhub_ultrafast.command,
    ["stale-python", "/old/ultrafast-launcher.py"],
    "a deliberately separate owned registration must keep its own command",
  );
});

test("repairOpencodeConfigJsonObject creates the canonical entry rather than hijacking a renamed owned entry", () => {
  const before = {
    mcp: {
      "aiworkhub-renamed": {
        type: "local",
        command: ["python3", "/bin/aiworkhub-mcp-server.py"],
        environment: { AIWORKHUB_REPO_ROOT: "/repo/a", TOKEN: "keep-me" },
      },
    },
  };
  const { document } = repairOpencodeConfigJsonObject(before, ["python3", "/new/launcher.py"]);
  assert.deepEqual(document.mcp.aiworkhub.command, ["python3", "/new/launcher.py"]);
  assert.deepEqual(
    document.mcp["aiworkhub-renamed"].command,
    ["python3", "/bin/aiworkhub-mcp-server.py"],
    "a command-detected owned entry keeps its launcher and only loses repo identity",
  );
  assert.deepEqual(document.mcp["aiworkhub-renamed"].environment, { TOKEN: "keep-me" });
});

test("repairOpencodeConfigJsonObject is idempotent once repaired", () => {
  const first = repairOpencodeConfigJsonObject({}, ["python3", "/launcher.py"]);
  const second = repairOpencodeConfigJsonObject(first.document, ["python3", "/launcher.py"]);
  assert.equal(second.changed, false);
});

test("isOwnedOpencodeMcpEntry recognizes the canonical name and the stable launcher, never an unrelated server", () => {
  assert.equal(isOwnedOpencodeMcpEntry("aiworkhub", { command: ["anything"] }), true);
  assert.equal(isOwnedOpencodeMcpEntry("custom", { command: ["python3", "/bin/aiworkhub-mcp-server.py"] }), true);
  assert.equal(isOwnedOpencodeMcpEntry("custom", { command: ["node", "unrelated.js"] }), false);
});

function withTempConfigHome(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-opencode-test-"));
  try {
    return fn(dir);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test("resolveOpencodeConfigJsonPath prefers opencode.json over opencode.jsonc when both exist", () => {
  withTempConfigHome((configHome) => {
    const dir = path.join(configHome, "opencode");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "opencode.json"), "{}\n", "utf8");
    fs.writeFileSync(path.join(dir, "opencode.jsonc"), "{}\n", "utf8");
    const resolved = resolveOpencodeConfigJsonPath({ XDG_CONFIG_HOME: configHome });
    assert.equal(resolved, path.join(dir, "opencode.json"));
  });
});

test("resolveOpencodeConfigJsonPath falls back to the existing opencode.jsonc when no opencode.json exists", () => {
  withTempConfigHome((configHome) => {
    const dir = path.join(configHome, "opencode");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "opencode.jsonc"), "{\n  // comment\n}\n", "utf8");
    const resolved = resolveOpencodeConfigJsonPath({ XDG_CONFIG_HOME: configHome });
    assert.equal(resolved, path.join(dir, "opencode.jsonc"));
  });
});

test("readOpencodeConfigDocument returns an empty document when no config file exists yet", () => {
  withTempConfigHome((configHome) => {
    const configPath = path.join(configHome, "opencode", "opencode.json");
    const result = readOpencodeConfigDocument(configPath);
    assert.deepEqual(result, { ok: true, exists: false, document: {} });
  });
});

test("readOpencodeConfigDocument fails closed on opencode.jsonc content with comments and trailing commas", () => {
  withTempConfigHome((configHome) => {
    const configPath = path.join(configHome, "opencode.jsonc");
    const original = '{\n  // a comment OpenCode accepts but JSON.parse does not\n  "theme": "dark",\n  "mcp": {},\n}\n';
    fs.writeFileSync(configPath, original, "utf8");
    const result = readOpencodeConfigDocument(configPath);
    assert.equal(result.ok, false);
    assert.equal(typeof result.reason, "string");
    assert.equal(
      fs.readFileSync(configPath, "utf8"),
      original,
      "a JSONC config with comments must stay byte-for-byte untouched",
    );
  });
});

test("readOpencodeConfigDocument fails closed on malformed/corrupt JSON content", () => {
  withTempConfigHome((configHome) => {
    const configPath = path.join(configHome, "opencode.json");
    const original = "{ this is not json ";
    fs.writeFileSync(configPath, original, "utf8");
    const result = readOpencodeConfigDocument(configPath);
    assert.equal(result.ok, false);
    assert.equal(
      fs.readFileSync(configPath, "utf8"),
      original,
      "an unparseable config must stay byte-for-byte untouched",
    );
  });
});

test(
  "readOpencodeConfigDocument fails closed on an unreadable (permission-denied) config",
  { skip: process.platform === "win32" },
  () => {
    withTempConfigHome((configHome) => {
      if (process.getuid && process.getuid() === 0) return;
      const configPath = path.join(configHome, "opencode.json");
      const original = '{"theme":"dark"}\n';
      fs.writeFileSync(configPath, original, "utf8");
      fs.chmodSync(configPath, 0o000);
      try {
        const result = readOpencodeConfigDocument(configPath);
        assert.equal(result.ok, false);
      } finally {
        fs.chmodSync(configPath, 0o600);
      }
    });
  },
);

test(
  "atomicWriteJsonPreservingMode preserves an existing file's permission bits instead of the umask default",
  { skip: process.platform === "win32" },
  () => {
    withTempConfigHome((configHome) => {
      const configPath = path.join(configHome, "opencode.json");
      fs.writeFileSync(configPath, "{}\n", "utf8");
      fs.chmodSync(configPath, 0o640);
      atomicWriteJsonPreservingMode(configPath, { theme: "dark" });
      const mode = fs.statSync(configPath).mode & 0o777;
      assert.equal(mode, 0o640);
      assert.deepEqual(JSON.parse(fs.readFileSync(configPath, "utf8")), { theme: "dark" });
    });
  },
);

test(
  "atomicWriteJsonPreservingMode creates a brand-new secret-bearing config with a restrictive mode",
  { skip: process.platform === "win32" },
  () => {
    withTempConfigHome((configHome) => {
      const configPath = path.join(configHome, "opencode.json");
      atomicWriteJsonPreservingMode(configPath, { theme: "dark" });
      const mode = fs.statSync(configPath).mode & 0o777;
      assert.equal(mode, 0o600);
    });
  },
);
