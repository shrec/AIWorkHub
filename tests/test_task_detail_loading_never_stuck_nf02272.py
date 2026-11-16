"""NF-2026-02272: the VS Code dashboard task detail must never stay on "Loading task".

Root cause (traced in NF-2026-02272):
* The webview requestTaskDetail (media/app.js) shows detail-loading and posts
  selectTask. Only a taskDetail reply clears it (applyTaskDetail).
* pushTaskDetail (vscode-extension/extension.js) answered a failed call with an
  ``error`` message the webview never routes to applyTaskDetail, and it posted
  nothing at all when the client had been rebound mid-call.

Contract checked here:
* pushTaskDetail replies ``taskDetail {ok:false, task_id, error}`` on the catch
  path (timeout / MCP error) and when the client was rebound mid-call.
* The task-detail call uses its own 60 s timeout; the global MCP_REQUEST_TIMEOUT_MS
  stays at 20 s.
* applyTaskDetail with ok:false clears detail-loading, shows the error, and a
  re-selection of the same task re-requests it.

The host function is extracted verbatim from extension.js and run in Node with
stubbed collaborators; the webview side reuses the DOM shim from
test_live_output_poll_rearm.py (the single definition of that shim). One node
invocation covers this file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_live_output_poll_rearm import _HARNESS_JS as _OWNER_HARNESS_JS

VSCODE_EXT_DIR = Path(__file__).resolve().parents[1] / "vscode-extension"
APP_JS = VSCODE_EXT_DIR / "media" / "app.js"
EXTENSION_JS = VSCODE_EXT_DIR / "extension.js"

# The owner's DOM shim + app.js loader, sliced from its harness source (not copied).
_DOM_SHIM_JS = _OWNER_HARNESS_JS[
    _OWNER_HARNESS_JS.index("class FakeElement {"):
    _OWNER_HARNESS_JS.index("const api = global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__;")
]

_HARNESS_JS = r"""
"use strict";
const assert = require("assert");
const APP_JS = process.env.APP_JS;
const EXTENSION_JS = process.env.EXTENSION_JS;
""" + _DOM_SHIM_JS + r"""
const api = global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__;
assert(api, "formatter/test hook exposed");
const el = (s) => document.querySelector(s);

// -- Host side: the real pushTaskDetail, extracted verbatim from extension.js ----
const extSrc = fs.readFileSync(EXTENSION_JS, "utf8");
const fnStart = extSrc.indexOf("async function pushTaskDetail(view, taskId) {");
assert(fnStart >= 0, "pushTaskDetail is present in extension.js");
let depth = 0; let fnEnd = -1;
for (let i = extSrc.indexOf("{", fnStart); i < extSrc.length; i++) {
  if (extSrc[i] === "{") depth++;
  else if (extSrc[i] === "}") { depth--; if (depth === 0) { fnEnd = i + 1; break; } }
}
assert(fnEnd > fnStart, "pushTaskDetail body closes");
const fnSrc = extSrc.slice(fnStart, fnEnd);
const timeoutMatch = /const TASK_DETAIL_REQUEST_TIMEOUT_MS = (\d+);/.exec(extSrc);
assert(timeoutMatch, "TASK_DETAIL_REQUEST_TIMEOUT_MS declared in extension.js");
const TASK_DETAIL_TIMEOUT_MS = Number(timeoutMatch[1]);
assert.strictEqual(TASK_DETAIL_TIMEOUT_MS, 60000, "task detail has its own 60 s timeout");

const OUTBOUND_TYPES = { taskDetail: "taskDetail", error: "error" };
const DASHBOARD_TOOLS = { taskDetail: "aiworkhub_dashboard_task_detail" };
const sanitizeWebviewPayload = (p) => p;
const sanitizeErrorMessage = (e) => String((e && e.message) || e);
const buildPush = new Function(
  "getDashboardClient", "DASHBOARD_TOOLS", "OUTBOUND_TYPES", "sanitizeWebviewPayload",
  "sanitizeErrorMessage", "TASK_DETAIL_REQUEST_TIMEOUT_MS",
  fnSrc + "\nreturn pushTaskDetail;",
);
const pushFor = (getDashboardClient) => buildPush(
  getDashboardClient, DASHBOARD_TOOLS, OUTBOUND_TYPES, sanitizeWebviewPayload,
  sanitizeErrorMessage, TASK_DETAIL_TIMEOUT_MS,
);

function makeView() {
  return {
    current: null,
    posts: [],
    bindClient(client) { this.current = client; },
    stillBoundTo(client) { return this.current === client; },
    postMessage(message) { this.posts.push(message); },
  };
}
function fakeClient(impl) {
  const calls = [];
  return { calls, callTool(tool, args, timeoutMs) { calls.push({ tool, args, timeoutMs }); return impl(); } };
}

(async () => {
  // (1) The call rejects (timeout / MCP error): a taskDetail ok:false reply is posted.
  {
    const view = makeView();
    const client = fakeClient(() => Promise.reject(new Error("request timed out after 60000 ms")));
    await pushFor(() => client)(view, "T-1");
    assert.strictEqual(view.posts.length, 1, "exactly one reply for the failed call");
    assert.strictEqual(view.posts[0].type, "taskDetail", "reply is a taskDetail message");
    assert.strictEqual(view.posts[0].payload.ok, false, "reply is ok:false");
    assert.strictEqual(view.posts[0].payload.task_id, "T-1", "reply names the task");
    assert.ok(/timed out/.test(view.posts[0].payload.error), "reply carries the error text");
    assert.strictEqual(client.calls[0].timeoutMs, TASK_DETAIL_TIMEOUT_MS, "task detail call uses the dedicated timeout");
    console.log("TASK_DETAIL_TIMEOUT_REPLY_OK");
  }

  // (2) The client is rebound while the call is in flight: a taskDetail ok:false reply is posted.
  {
    const view = makeView();
    const clientB = fakeClient(() => Promise.resolve({ ok: true }));
    const clientA = fakeClient(() => { view.current = clientB; return Promise.resolve({ ok: true, task: { task_id: "T-2" } }); });
    await pushFor(() => clientA)(view, "T-2");
    assert.strictEqual(view.posts.length, 1, "exactly one reply after a rebind");
    assert.strictEqual(view.posts[0].type, "taskDetail", "rebind reply is a taskDetail message");
    assert.deepStrictEqual(
      view.posts[0].payload,
      { ok: false, task_id: "T-2", error: "task_detail_client_rebound" },
      "rebind reply is ok:false with a named cause",
    );
    console.log("TASK_DETAIL_REBOUND_REPLY_OK");
  }

  // (3) Positive control: a bound, successful call still replies with the card.
  {
    const view = makeView();
    const client = fakeClient(() => Promise.resolve({ ok: true, task: { task_id: "T-3" } }));
    await pushFor(() => client)(view, "T-3");
    assert.strictEqual(view.posts.length, 1, "exactly one reply on success");
    assert.deepStrictEqual(view.posts[0].payload, { ok: true, task: { task_id: "T-3" } }, "success payload passes through");
    console.log("TASK_DETAIL_SUCCESS_REPLY_OK");
  }

  // (4) Webview: ok:false clears Loading and shows the error.
  api.requestTaskDetail("T-9");
  assert.strictEqual(el("#detail-loading").hidden, false, "Loading shown while the request is in flight");
  api.applyTaskDetail({ ok: false, task_id: "T-9", error: "Task detail timed out" });
  assert.strictEqual(el("#detail-loading").hidden, true, "ok:false clears Loading");
  assert.strictEqual(el("#detail-error").hidden, false, "ok:false shows the error panel");
  assert.ok(el("#detail-error").textContent.includes("Task detail timed out"), "error text is shown");
  console.log("TASK_DETAIL_OK_FALSE_CLEARS_LOADING_OK");

  // (5) Retry: re-selecting the same task re-requests it.
  const selectCount = () => posted.filter((m) => m && m.type === "selectTask" && m.taskId === "T-9").length;
  const before = selectCount();
  api.requestTaskDetail("T-9");
  assert.strictEqual(selectCount(), before + 1, "re-selecting the same task posts selectTask again");
  assert.strictEqual(el("#detail-loading").hidden, false, "retry shows Loading again");
  assert.strictEqual(el("#detail-error").hidden, true, "retry hides the previous error");
  console.log("TASK_DETAIL_RETRY_RESELECT_OK");

  console.log("task-detail-loading-never-stuck.harness: ok");
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


def _run_harness() -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if node is None:
        pytest.skip("required executable 'node' is unavailable in this sandbox")
    return subprocess.run(
        [node, "-"],
        input=_HARNESS_JS,
        # Node 24 on Windows aborts at startup (ncrypto::CSPRNG) without SYSTEMROOT.
        env={
            "APP_JS": str(APP_JS),
            "EXTENSION_JS": str(EXTENSION_JS),
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="module")
def harness() -> subprocess.CompletedProcess:
    result = _run_harness()
    assert result.returncode == 0, f"harness failed\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    return result


def test_harness_completes_all_claims(harness: subprocess.CompletedProcess) -> None:
    assert "task-detail-loading-never-stuck.harness: ok" in harness.stdout


@pytest.mark.parametrize(
    "marker",
    [
        "TASK_DETAIL_TIMEOUT_REPLY_OK",
        "TASK_DETAIL_REBOUND_REPLY_OK",
        "TASK_DETAIL_SUCCESS_REPLY_OK",
        "TASK_DETAIL_OK_FALSE_CLEARS_LOADING_OK",
        "TASK_DETAIL_RETRY_RESELECT_OK",
    ],
)
def test_claim_marker_printed(harness: subprocess.CompletedProcess, marker: str) -> None:
    assert marker in harness.stdout, harness.stdout


def test_global_request_timeout_unchanged() -> None:
    source = (VSCODE_EXT_DIR / "extension.js").read_text(encoding="utf-8")
    assert re.search(r"const MCP_REQUEST_TIMEOUT_MS = 20000;", source)


def test_task_detail_timeout_is_dedicated_60s() -> None:
    source = (VSCODE_EXT_DIR / "extension.js").read_text(encoding="utf-8")
    assert re.search(r"const TASK_DETAIL_REQUEST_TIMEOUT_MS = 60000;", source)
