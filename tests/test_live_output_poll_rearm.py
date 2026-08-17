"""SCAN-E3FF: two confirmed defects in the VS Code dashboard Live Output panel
(vscode-extension/media/app.js), both ending with a panel that is stuck or
lying with no honest error shown.

ONE (HIGH) -- Live Output stopped polling forever on any non-ok response.
``renderLiveOutput`` returned early when ``info.ok === false`` without re-arming
the poll chain (the only ``scheduleNextLiveOutputPoll`` call was at the end of
the success path). A task selected right after launch answers ``ok: false /
output_unavailable`` until the worker starts streaming, so the timer was never
rescheduled and the panel showed the error permanently until manual
re-selection. The fix shows a transient "Waiting for output..." state (never the
red terminal ``fail`` class) and re-arms the chain on this terminal outcome too.

TWO (MEDIUM) -- a stale ``taskDetail`` reply resurrected a cleared panel.
The handler rendered whatever arrived; ``state.detailRequest`` was bumped but the
reply's identity was never compared to the current selection. Task T selected,
another window archives it so ``clearTaskDetail`` runs, then T's in-flight reply
lands and rebinds Archive/Restore to a phantom task. The fix routes the reply
through ``applyTaskDetail``, which drops any reply whose task identity no longer
matches ``state.selectedTaskId`` -- mirroring ``renderLiveOutput``'s existing
``liveOutputTaskId`` guard for the sibling poll reply.

The deep behavioural coverage runs the real ``media/app.js`` inside a Node
harness (a minimal fake DOM + captured ``vscode.postMessage``/``setTimeout``);
this file drives that harness via subprocess and adds static-source guardrails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

VSCODE_EXT_DIR = Path(__file__).resolve().parents[1] / "vscode-extension"
APP_JS = VSCODE_EXT_DIR / "media" / "app.js"

# Node harness: loads the real app.js under a fake DOM and exercises the two
# repaired code paths end to end. It prints a distinct marker per proven claim
# and exits non-zero (via assert) on any regression.
_HARNESS_JS = r"""
"use strict";
const assert = require("assert");
const APP_JS = process.env.APP_JS;

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName; this.className = ""; this.children = []; this.dataset = {};
    this.style = {}; this.hidden = false; this.open = false; this.disabled = false;
    this.checked = false; this.value = ""; this.title = ""; this.type = ""; this.id = "";
    this._textContent = ""; this._innerHTML = "";
  }
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  appendChild(node) {
    if (node && node.__fragment) { for (const c of node.children) this.children.push(c); return node; }
    this.children.push(node); return node;
  }
  prepend(...nodes) { this.children.unshift(...nodes); }
  insertBefore(node) { this.children.unshift(node); return node; }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  removeChild(node) { this.children = this.children.filter((c) => c !== node); return node; }
  remove() {}
  addEventListener() {} removeEventListener() {}
  setAttribute(name, value) { this[name] = String(value); }
  removeAttribute(name) { delete this[name]; }
  getAttribute(name) { return this[name] !== undefined ? String(this[name]) : null; }
  closest() { return null; } matches() { return false; } contains() { return false; } focus() {}
  querySelector() { return new FakeElement("div"); }
  querySelectorAll() { return []; }
  get classList() { return { add() {}, remove() {}, toggle() {}, contains() { return false; } }; }
  get textContent() {
    const t = this.children.map((c) => (c && c.textContent) ? c.textContent : "").join("");
    return this._textContent + t;
  }
  set textContent(v) { this._textContent = String(v); this.children = []; }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) {
    this._innerHTML = String(v);
    this.value = this._innerHTML.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  }
}
class FakeDocument {
  constructor() { this.elements = new Map(); }
  createElement(tag) { return new FakeElement(tag); }
  createDocumentFragment() { const f = new FakeElement("#fragment"); f.__fragment = true; return f; }
  querySelector(sel) { if (!this.elements.has(sel)) this.elements.set(sel, new FakeElement("div")); return this.elements.get(sel); }
  querySelectorAll() { return []; }
  addEventListener() {}
}
const timers = []; const posted = [];
const document = new FakeDocument();
global.document = document;
global.window = { setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length; }, clearTimeout: () => {}, addEventListener: () => {} };
global.acquireVsCodeApi = () => ({ getState: () => ({}), setState: () => {}, postMessage: (m) => { posted.push(m); } });
global.Intl = Intl;
require(APP_JS);
const api = global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__;
assert(api, "formatter/test hook exposed");
const reqCount = () => posted.filter((m) => m && m.type === "requestLiveOutput").length;
const el = (s) => document.querySelector(s);

// -- Defect ONE: a non-ok response is transient and re-arms the poll chain --
{
  api.startLiveOutputPolling("task-1");
  assert.strictEqual(reqCount(), 1, "initial poll posted");
  const before = timers.length;
  api.renderLiveOutput({ ok: false, task_id: "task-1", error: "output_unavailable", reason: "no_process_log_record_for_task" });
  const s = el("#detail-live-output-state");
  assert(!/\bfail\b/.test(s.className), "non-ok must not be a terminal fail state, got: " + s.className);
  assert(/wait/i.test(s.textContent) && /output/i.test(s.textContent), "message says what is awaited: " + s.textContent);
  console.log("REARM_TRANSIENT_STATE_OK");
  assert(timers.length > before, "poll chain re-armed after non-ok (a timer was scheduled)");
  timers[timers.length - 1].fn();
  assert.strictEqual(reqCount(), 2, "re-armed chain issued the next poll");
  assert.strictEqual(posted.filter((m) => m.type === "requestLiveOutput").pop().taskId, "task-1", "next poll targets the same task");
  console.log("REARM_SECOND_REQUEST_OK");
  // Worker now streams: the second render (ok: true) must actually paint output.
  api.renderLiveOutput({ ok: true, task_id: "task-1", output: "streamed-worker-line-XYZ\n", next_cursor: 42, liveness_state: "running" });
  assert(el("#detail-live-output-container").textContent.includes("streamed-worker-line-XYZ"), "second render painted the streamed output");
  console.log("REARM_SECOND_RENDER_OK");
}

// -- Defect TWO: a stale taskDetail reply must never render ----------------
{
  // Positive control: a reply for the currently-selected task still renders.
  api.requestTaskDetail("TASK-U");
  api.applyTaskDetail({ ok: true, task: { task_id: "TASK-U", topic: "dashboard" } });
  assert.strictEqual(el("#detail-content").hidden, false, "matching reply rendered the panel");
  assert.strictEqual(el("#detail-archive-task").dataset.taskId, "TASK-U", "Archive bound to the selected task");
  console.log("STALE_POSITIVE_RENDER_OK");
  // Reselect-different-task race: a late reply for the previous task is dropped.
  api.applyTaskDetail({ ok: true, task: { task_id: "TASK-T", topic: "dashboard" } });
  assert.strictEqual(el("#detail-archive-task").dataset.taskId, "TASK-U", "stale reply did not rebind Archive to another task");
  assert.strictEqual(el("#detail-heading").textContent, "TASK-U", "stale reply did not overwrite the heading");
  console.log("STALE_DROP_DIFFERENT_TASK_OK");
  // Archived-while-in-flight: select, clear (archived elsewhere), late reply lands.
  api.requestTaskDetail("TASK-T");
  api.clearTaskDetail();
  assert.strictEqual(el("#detail-content").hidden, true, "panel cleared before the late reply");
  api.applyTaskDetail({ ok: true, task: { task_id: "TASK-T", topic: "dashboard" } });
  assert.strictEqual(el("#detail-content").hidden, true, "late reply kept the panel cleared");
  assert.strictEqual(el("#detail-empty").hidden, false, "empty placeholder stayed visible");
  assert.strictEqual(el("#detail-heading").textContent, "No task selected", "heading stayed cleared");
  assert.notStrictEqual(el("#detail-archive-task").dataset.taskId, "TASK-T", "Archive never bound to the phantom task");
  console.log("STALE_DROP_CLEARED_OK");
}

console.log("live-output-poll-rearm.harness: ok");
"""


def _read_app() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _run_harness() -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if node is None:
        pytest.skip("required executable 'node' is unavailable in this sandbox")
    return subprocess.run(
        [node, "-"],
        input=_HARNESS_JS,
        env={"APP_JS": str(APP_JS), "PATH": os.environ.get("PATH", "")},
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


def test_app_js_syntax_is_valid() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("required executable 'node' is unavailable in this sandbox")
    result = subprocess.run(
        [node, "--check", str(APP_JS)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr


# -- Defect ONE ------------------------------------------------------------
def test_non_ok_response_is_transient_not_terminal(harness) -> None:
    # A non-ok poll response is a brief "Waiting for output..." state (never the
    # red terminal `fail` class) whose message names what is awaited.
    assert "REARM_TRANSIENT_STATE_OK" in harness.stdout


def test_non_ok_response_rearms_the_poll_chain(harness) -> None:
    # The re-arm terminal outcome fires the next poll for the same task.
    assert "REARM_SECOND_REQUEST_OK" in harness.stdout


def test_second_render_shows_streamed_output(harness) -> None:
    # ok:false then ok:true -> the second render paints the streamed output,
    # with no manual re-selection.
    assert "REARM_SECOND_RENDER_OK" in harness.stdout


# -- Defect TWO ------------------------------------------------------------
def test_matching_task_detail_reply_still_renders(harness) -> None:
    assert "STALE_POSITIVE_RENDER_OK" in harness.stdout


def test_stale_reply_for_other_task_is_dropped(harness) -> None:
    assert "STALE_DROP_DIFFERENT_TASK_OK" in harness.stdout


def test_archived_while_in_flight_reply_keeps_panel_cleared(harness) -> None:
    assert "STALE_DROP_CLEARED_OK" in harness.stdout


def test_harness_completed(harness) -> None:
    assert "live-output-poll-rearm.harness: ok" in harness.stdout


# -- Static-source guardrails (run even without node) ----------------------
def test_non_ok_branch_rearms_in_source() -> None:
    app = _read_app()
    branch = app.split("if (info.ok === false) {", 1)
    assert len(branch) == 2, "non-ok branch missing from renderLiveOutput"
    after = branch[1]
    # The re-arm must happen inside the non-ok branch, before the success path.
    rearm = after.index("scheduleNextLiveOutputPoll(taskId);")
    success_path = after.index("appendLiveOutputText(String(info.output")
    assert rearm < success_path, "non-ok branch must re-arm the poll chain before the success path"
    assert 'className = "validation-label fail"' not in after[:rearm], "non-ok state must not use the terminal fail class"


def _function_body(app: str, signature: str) -> str:
    # Extract a full function body by brace-matching from the signature's first
    # ``{`` to its matching ``}`` -- unlike ``split("}", 1)`` this does not stop
    # at the first nested object literal, so the whole guard is inspected.
    start = app.index(signature)
    brace = app.index("{", start)
    depth = 0
    for i in range(brace, len(app)):
        if app[i] == "{":
            depth += 1
        elif app[i] == "}":
            depth -= 1
            if depth == 0:
                return app[brace : i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def test_task_detail_stale_reply_guard_present() -> None:
    app = _read_app()
    assert "function applyTaskDetail(payload)" in app
    assert "applyTaskDetail(message.payload);" in app, "taskDetail switch case must route through applyTaskDetail"
    # Inspect the whole applyTaskDetail body (brace-matched, not sliced at the
    # first nested object literal): the guard must derive the reply's identity
    # and compare it against the current selection, then early-return before any
    # render call so a reply that lost the race never paints.
    body = _function_body(app, "function applyTaskDetail(payload)")
    assert "replyTaskId" in body, "guard must derive the reply's task identity"
    assert "state.selectedTaskId" in body, "guard must compare against the current selection identity"
    guard_pos = body.index("state.selectedTaskId")
    render_pos = body.index("renderTaskDetail(")
    assert guard_pos < render_pos, "identity guard must run before renderTaskDetail"
    assert "return;" in body[:render_pos], "a lost-race reply must return before rendering"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", str(Path(__file__))]))
