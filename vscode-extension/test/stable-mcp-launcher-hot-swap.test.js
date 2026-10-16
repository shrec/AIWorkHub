"use strict";

// NF-2026-01546: the stable launcher is a stdio supervisor. A running client
// keeps one connection while current.json moves to a newer generation.

const assert = require("assert");
const childProcess = require("child_process");
const fs = require("fs");
const os = require("os");
const Module = require("module");
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
const { materializeStableMcpLauncher } = extensionModule.__testInternals;

const python = process.env.AIWORKHUB_TEST_PYTHON || (process.platform === "win32" ? "python" : "python3");
const probe = childProcess.spawnSync(python, ["--version"], { stdio: "ignore", timeout: 30000 });
if (probe.error || probe.status !== 0) {
  console.log("stable-mcp-launcher-hot-swap: skipped (no python)");
  process.exit(0);
}

const fakeServer = (generation) => [
  "import json, os, sys, time",
  `GEN = ${JSON.stringify(generation)}`,
  "def reply(i, result):",
  "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': i, 'result': result}) + '\\n')",
  "    sys.stdout.flush()",
  "def main():",
  "    for line in sys.stdin:",
  "        line = line.strip()",
  "        if not line:",
  "            continue",
  "        msg = json.loads(line)",
  "        method = msg.get('method')",
  "        i = msg.get('id')",
  "        if method is None or i is None:",
  "            continue",
  "        if method == 'initialize':",
  "            reply(i, {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake', 'version': GEN}})",
  "        elif method == 'tools/list':",
  "            reply(i, {'tools': [{'name': 'gen_' + GEN}]})",
  "        elif method == 'tools/call':",
  "            args = (msg.get('params') or {}).get('arguments') or {}",
  "            if args.get('crash'):",
  "                os._exit(3)",
  "            if args.get('silent'):",
  "                continue",
  "            if args.get('slow'):",
  "                time.sleep(float(args['slow']))",
  "            reply(i, {'content': [{'type': 'text', 'text': GEN}]})",
  "        else:",
  "            reply(i, {})",
  "    return 0",
  "",
].join("\n");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-hot-swap-"));
const storage = path.join(root, "storage");
const installGeneration = (name) => {
  const runtime = path.join(storage, "runtime", "generations", name, "runtime");
  fs.mkdirSync(path.join(runtime, "aiworkhub"), { recursive: true });
  fs.writeFileSync(path.join(runtime, "aiworkhub", "__init__.py"), "", "utf8");
  fs.writeFileSync(path.join(runtime, "aiworkhub", "server.py"), fakeServer(name), "utf8");
  return runtime;
};
const point = (name, runtime) => {
  fs.writeFileSync(
    path.join(storage, "runtime", "current.json"),
    JSON.stringify({ schema_id: "aiworkhub.stable_runtime.v1", generation: name, runtime_dir: runtime }),
    "utf8",
  );
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Node pipe stdio hangs inside the validation sandbox, so the NDJSON protocol
// is driven from Python (anonymous pipes) and Node only reads its log file.
const DRIVER = String.raw`import json, os, subprocess, sys, threading, time

launcher, storage = sys.argv[1], sys.argv[2]
messages = []
stderr_chunks = []
lock = threading.Lock()
stage = "start"


def enter(name):
    global stage
    stage = name
    print(f"stage {name}", flush=True)


def point(name):
    runtime = os.path.join(storage, "runtime", "generations", name, "runtime")
    with open(os.path.join(storage, "runtime", "current.json"), "w", encoding="utf-8") as fh:
        json.dump({"schema_id": "aiworkhub.stable_runtime.v1", "generation": name, "runtime_dir": runtime}, fh)


proc = subprocess.Popen([sys.executable, launcher], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def read_stdout():
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            msg = {"raw": line}
        with lock:
            messages.append(msg)


def drain_stderr():
    for raw in proc.stderr:
        with lock:
            stderr_chunks.append(raw.decode("utf-8", "replace"))


threading.Thread(target=read_stdout, daemon=True).start()
threading.Thread(target=drain_stderr, daemon=True).start()


def snapshot():
    with lock:
        return list(messages)


def stderr_text():
    with lock:
        return "".join(stderr_chunks)


def send(message):
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8"))
    proc.stdin.flush()


def wait_for(pred, label, timeout=30):
    deadline = time.time() + timeout
    while True:
        found = pred()
        if found:
            return found
        if time.time() > deadline:
            raise AssertionError(f"timed out waiting for {label}")
        time.sleep(0.02)


def reply(i):
    return wait_for(lambda: next((m for m in snapshot() if m.get("id") == i), None), f"reply {i}")


def is_list_changed(m):
    return m.get("method") == "notifications/tools/list_changed"


def list_changed_count():
    return sum(1 for m in snapshot() if is_list_changed(m))


def index_of(target):
    return next(i for i, m in enumerate(snapshot()) if m is target)


def list_name(response):
    return response["result"]["tools"][0]["name"]


def call_text(response):
    return response["result"]["content"][0]["text"]


def run():
    enter("a:initialize")
    send({"id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
    init = reply(1)
    assert init["result"]["capabilities"]["tools"]["listChanged"] is True, "listChanged capability"
    send({"method": "notifications/initialized"})
    send({"id": 2, "method": "tools/list"})
    assert list_name(reply(2)) == "gen_A", "first list from gen_A"
    assert list_changed_count() == 0, "no list_changed while nothing changed"

    enter("b:flip-B")
    point("B")
    send({"id": 3, "method": "tools/list"})
    third = reply(3)
    assert list_name(third) == "gen_B", "list after flip from gen_B"
    changed_at = next((i for i, m in enumerate(snapshot()) if is_list_changed(m)), -1)
    assert changed_at >= 0, "list_changed was sent"
    assert changed_at < index_of(third), "list_changed precedes the response from the new generation"
    assert list_changed_count() == 1, "exactly one list_changed after first flip"

    enter("c:in-flight")
    send({"id": 4, "method": "tools/call", "params": {"name": "slow", "arguments": {"slow": 2}}})
    time.sleep(0.3)
    point("C")
    send({"id": 5, "method": "tools/list"})
    assert call_text(reply(4)) == "B", "slow call answered by B"
    assert list_name(reply(5)) == "gen_B", "an in-flight child keeps serving until idle"
    assert list_changed_count() == 1, "no swap while a request was in flight"
    send({"id": 6, "method": "tools/list"})
    sixth = reply(6)
    assert list_name(sixth) == "gen_C", "list after idle from gen_C"
    assert list_changed_count() == 2, "second list_changed after the idle swap"
    second_at = next(i for i, m in enumerate(snapshot()) if i > changed_at and is_list_changed(m))
    assert second_at < index_of(sixth), "second list_changed precedes the gen_C response"

    enter("d:cancelled")
    send({"id": 9, "method": "tools/call", "params": {"name": "silent", "arguments": {"silent": True}}})
    time.sleep(0.3)
    send({"method": "notifications/cancelled", "params": {"requestId": 9}})
    point("B")
    send({"id": 10, "method": "tools/list"})
    assert list_name(reply(10)) == "gen_B", "a cancelled request does not block the swap"
    assert list_changed_count() == 3, "list_changed after the swap past a cancelled request"
    assert not any(m.get("id") == 9 for m in snapshot()), "the cancelled request was never answered"

    enter("e:crash")
    send({"id": 7, "method": "tools/call", "params": {"name": "crash", "arguments": {"crash": True}}})
    crashed = reply(7)
    assert crashed["error"]["code"] == -32603, "crash error code"
    assert crashed["error"]["message"] == "aiworkhub_mcp_child_exited", "crash error message"
    send({"id": 8, "method": "tools/list"})
    assert list_name(reply(8)) == "gen_B", "respawned child serves gen_B"

    enter("e:eof")
    proc.stdin.close()
    code = proc.wait(timeout=15)
    assert code == 0, f"supervisor exit code {code}"


exit_code = 1
try:
    run()
    print("stable-mcp-launcher-hot-swap: PASS", flush=True)
    exit_code = 0
except Exception as exc:
    print(f"FAIL at stage {stage}: {exc!r}", flush=True)
    print("supervisor stderr: " + stderr_text(), flush=True)
finally:
    if proc.poll() is None:
        proc.kill()
sys.exit(exit_code)
`;

async function main() {
  const runtimes = { A: installGeneration("A"), B: installGeneration("B"), C: installGeneration("C") };
  point("A", runtimes.A);
  const launcher = materializeStableMcpLauncher({
    globalStorageUri: { fsPath: storage },
    extensionUri: { fsPath: "/extension" },
  });

  const driverPath = path.join(root, "driver.py");
  fs.writeFileSync(driverPath, DRIVER, "utf8");
  const logPath = path.join(root, "driver.log");
  const fd = fs.openSync(logPath, "w");
  let result;
  try {
    result = childProcess.spawnSync(python, [driverPath, launcher, storage], { stdio: ["ignore", fd, fd], timeout: 120000 });
  } finally {
    fs.closeSync(fd);
  }
  const log = fs.readFileSync(logPath, "utf8");
  process.stdout.write(log);
  assert.strictEqual(result.status, 0, log);
  assert.ok(log.includes("stable-mcp-launcher-hot-swap: PASS"), log);
}

main()
  .then(
    () => 0,
    (err) => {
      console.error(err);
      return 1;
    },
  )
  .then(async (code) => {
    await Promise.race([
      fs.promises.rm(root, { recursive: true, force: true, maxRetries: 2 }).catch(() => {}),
      sleep(5000),
    ]);
    process.exit(code);
  });
