// The system log is pruned on EVERY recorded line, and every `[mcp stderr]`
// chunk and tool call records one. The previous prune re-serialized the whole
// retained array on each iteration of a pop loop, so once the retained set
// crossed the 1 MiB cap a single logged line cost ~89 ms -- 100 lines blocked
// the extension-host thread for ~9 s. A host that stops answering VS Code's
// ping is terminated, and every extension in that window dies with it.
const assert = require("assert");
const Module = require("module");

const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "vscode") return "vscode-stub";
  return originalResolve.call(this, request, ...rest);
};
require.cache["vscode-stub"] = {
  id: "vscode-stub",
  filename: "vscode-stub",
  loaded: true,
  exports: {
    workspace: { getConfiguration: () => ({ get: (_k, d) => d, update: async () => {} }), workspaceFolders: [] },
    window: { createOutputChannel: () => ({ appendLine() {}, dispose() {} }) },
    commands: { registerCommand: () => ({ dispose() {} }) },
    Uri: { joinPath: () => ({ fsPath: "" }) },
    extensions: { getExtension: () => null },
    ConfigurationTarget: { Global: 1 },
  },
};

try {
  const { __testInternals } = require("../extension.js");
  const { recordSystemLog, systemLogSnapshot, clearSystemLogs } = __testInternals;

  clearSystemLogs();

  // Fill well past the 1 MiB retention cap so the size-trimming path engages.
  const LINES = 300;
  const wide = "x".repeat(800);
  const started = Date.now();
  for (let i = 0; i < LINES; i += 1) recordSystemLog(`[mcp stderr] ${i} ${wide}`);
  const elapsed = Date.now() - started;

  const snapshot = systemLogSnapshot();
  const bytes = Buffer.byteLength(JSON.stringify(snapshot), "utf8");

  // Caps still hold.
  assert.ok(snapshot.length > 0, "entries must be retained");
  assert.ok(snapshot.length <= 1200, `entry cap exceeded: ${snapshot.length}`);
  assert.ok(bytes <= 1024 * 1024, `byte cap exceeded: ${bytes}`);

  // Newest-first ordering is preserved, and the newest line survives trimming.
  assert.ok(
    snapshot[0].sequence > snapshot[snapshot.length - 1].sequence,
    "snapshot must stay newest-first",
  );
  assert.ok(snapshot[0].message.startsWith(String(LINES - 1)), "newest line must be retained");

  // Cost must stay linear. The old quadratic shape needed ~26 s for these
  // 300 lines; the linear one needs well under a second.
  assert.ok(
    elapsed < 5000,
    `pruning ${LINES} lines took ${elapsed} ms -- the quadratic prune is back`,
  );

  clearSystemLogs();
  console.log(`system-log-prune-cost: PASS (${LINES} lines in ${elapsed} ms, ${snapshot.length} retained)`);
} finally {
  Module._resolveFilename = originalResolve;
}
