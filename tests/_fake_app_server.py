#!/usr/bin/env python3
"""Fake executable Codex App Server for callback-bridge wire-protocol tests.

A REAL subprocess (spawned via ``subprocess.Popen``, never a mocked
``subprocess.run`` returncode) that speaks the newline-delimited JSON wire
verified against the installed Codex App Server's own JSON Schema export
(no top-level ``jsonrpc`` member; ``initialize.params`` requires
``clientInfo``; ``thread/resume.params`` requires ``threadId``;
``turn/start.params`` requires ``threadId``+``input``;
``turn/steer.params`` requires ``threadId``+``expectedTurnId``+``input``;
``TurnSteerResponse`` requires ``turnId``). ``thread/resume``'s
``result.thread.status`` is the real discriminated union (``{"type":
"idle"}``, ``{"type": "active", "activeFlags": [...]}``, ``{"type":
"notLoaded"}``, ``{"type": "systemError"}``) and ``result.thread.turns``
is fully populated, each turn's ``status`` one of ``completed`` /
``interrupted`` / ``failed`` / ``inProgress`` -- per
``ThreadResumeResponse``/``Thread``/``ThreadStatus``/``Turn`` in the
installed binary's own v2 App Server schema export.

Strictly enforces sequence: ``initialize`` must be first and successful;
``initialized`` must follow before any further request is accepted;
``thread/resume`` must follow ``initialized`` before ``turn/start`` or
``turn/steer`` is accepted. Any out-of-order or missing step gets a
JSON-RPC-shaped error response (``{"id": ..., "error": {...}}``), never a
silent success.

Flags:
  --busy                    thread/resume always returns a "busy" error
                             (legacy B384/B402 deferral test, kept for
                             back-compat; distinct from the B407 active-
                             thread-status mechanism below)
  --busy-turn               turn/start always returns a "busy" error
  --wrong-turn-completion   turn/start's async turn/completed echoes a
                             mismatched turn id (identity-mismatch test)
  --silent-initialize       initialize never responds (timeout test)
  --crash-after X           exit(1) immediately after replying to method X
                             (X in {"initialize", "thread/resume"})
  --active-one-turn         thread/resume reports status=active with
                             exactly one inProgress turn ("existing-turn-1");
                             turn/steer against that exact id succeeds
                             synchronously (no turn/completed emitted --
                             proves the client never waits for one);
                             turn/start is rejected as forbidden
  --active-zero-inprogress  status=active, zero inProgress turns; both
                             turn/start and turn/steer are rejected as
                             unexpected (the client must defer purely from
                             the resume response, never sending either)
  --active-multi-inprogress status=active, two inProgress turns; same
                             both-forbidden guard as above
  --active-not-steerable    status=active with one inProgress turn
                             ("turn-review"); turn/steer is rejected with
                             a CodexErrorInfo.activeTurnNotSteerable-shaped
                             error (review/compact); turn/start forbidden
  --active-stale-turn       status=active with one inProgress turn
                             ("turn-stale"); turn/steer is always rejected
                             as a stale expectedTurnId race; turn/start
                             forbidden
  --active-wrong-turnid     status=active with one inProgress turn
                             ("turn-good"); turn/steer succeeds but echoes
                             a mismatched turnId (identity-mismatch test)
  --thread-status-notloaded status=notLoaded (unrecognized/ambiguous);
                             turn/start and turn/steer forbidden
  --thread-status-systemerror status=systemError; turn/start and
                             turn/steer forbidden
  --active-inprogress-count N
                             status=active with exactly N inProgress turns
                             (N != 1) -- reproduces the exact B416
                             production shape (e.g. N=45); select_steer_target
                             raises ActiveThreadSteerDeferralError purely
                             from the thread/resume response, so turn/start
                             and turn/steer are both forbidden and never
                             sent for this scenario.
"""
from __future__ import annotations

import json
import sys


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _error(request_id, code: int, message: str, data: dict | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"id": request_id, "error": err}


_ACTIVE_STATUS_FLAGS = (
    "--active-one-turn",
    "--active-zero-inprogress",
    "--active-multi-inprogress",
    "--active-not-steerable",
    "--active-stale-turn",
    "--active-wrong-turnid",
    "--thread-status-notloaded",
    "--thread-status-systemerror",
    "--active-inprogress-count",
)


def _thread_status_and_turns() -> tuple[dict, list[dict]]:
    """Build this run's (status, turns) pair for thread/resume from argv flags."""
    if "--active-inprogress-count" in sys.argv:
        idx = sys.argv.index("--active-inprogress-count")
        count = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 2
        return {"type": "active", "activeFlags": []}, [
            {"id": f"turn-{i}", "items": [], "status": "inProgress"} for i in range(count)
        ]
    if "--active-one-turn" in sys.argv:
        return {"type": "active", "activeFlags": []}, [
            {"id": "existing-turn-1", "items": [], "status": "inProgress"}
        ]
    if "--active-zero-inprogress" in sys.argv:
        return {"type": "active", "activeFlags": []}, [
            {"id": "turn-done", "items": [], "status": "completed"}
        ]
    if "--active-multi-inprogress" in sys.argv:
        return {"type": "active", "activeFlags": []}, [
            {"id": "turn-x", "items": [], "status": "inProgress"},
            {"id": "turn-y", "items": [], "status": "inProgress"},
        ]
    if "--active-not-steerable" in sys.argv:
        return {"type": "active", "activeFlags": []}, [
            {"id": "turn-review", "items": [], "status": "inProgress"}
        ]
    if "--active-stale-turn" in sys.argv:
        return {"type": "active", "activeFlags": []}, [
            {"id": "turn-stale", "items": [], "status": "inProgress"}
        ]
    if "--active-wrong-turnid" in sys.argv:
        return {"type": "active", "activeFlags": []}, [
            {"id": "turn-good", "items": [], "status": "inProgress"}
        ]
    if "--thread-status-notloaded" in sys.argv:
        return {"type": "notLoaded"}, []
    if "--thread-status-systemerror" in sys.argv:
        return {"type": "systemError"}, []
    return {"type": "idle"}, []


def main() -> int:
    busy = "--busy" in sys.argv
    busy_turn = "--busy-turn" in sys.argv
    wrong_turn = "--wrong-turn-completion" in sys.argv
    silent_initialize = "--silent-initialize" in sys.argv
    active_scenario = any(flag in sys.argv for flag in _ACTIVE_STATUS_FLAGS)
    crash_after = ""
    if "--crash-after" in sys.argv:
        idx = sys.argv.index("--crash-after")
        if idx + 1 < len(sys.argv):
            crash_after = sys.argv[idx + 1]

    initialized_ok = False
    got_initialized_notification = False
    resumed_thread_id: str | None = None

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        request_id = msg.get("id")
        is_request = "id" in msg and method is not None

        if method == "initialize":
            if not is_request:
                continue
            params = msg.get("params") or {}
            client_info = params.get("clientInfo")
            if not isinstance(client_info, dict) or not client_info.get("name"):
                _write(_error(request_id, -32602, "invalid initialize params: clientInfo required"))
                continue
            initialized_ok = True
            if silent_initialize:
                continue
            _write({
                "id": request_id,
                "result": {
                    "codexHome": "/tmp/fake-codex-home",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                    "userAgent": "geoai-fake-app-server/1",
                },
            })
            if crash_after == "initialize":
                return 1
            continue

        if method == "initialized":
            if not initialized_ok:
                # Notifications get no response, but a premature 'initialized'
                # must not silently arm the rest of the sequence.
                continue
            got_initialized_notification = True
            continue

        if method == "thread/resume":
            if not is_request:
                continue
            if not (initialized_ok and got_initialized_notification):
                _write(_error(request_id, -32600, "thread/resume before initialize/initialized"))
                continue
            params = msg.get("params") or {}
            thread_id = params.get("threadId")
            if not thread_id:
                _write(_error(request_id, -32602, "thread/resume requires threadId"))
                continue
            if busy:
                _write(_error(request_id, -32000, "thread is busy with another turn"))
                continue
            resumed_thread_id = thread_id
            status, turns = _thread_status_and_turns()
            thread = {
                "id": thread_id,
                "preview": "",
                "modelProvider": "openai",
                "createdAt": 0,
                "updatedAt": 0,
                "status": status,
                "ephemeral": False,
                "cwd": "/home/shrek/GeoAI",
                "cliVersion": "0.144.1",
                "source": "appServer",
                "turns": turns,
                "sessionId": "fake-session",
            }
            _write({
                "id": request_id,
                "result": {
                    "thread": thread,
                    "model": "gpt-5",
                    "modelProvider": "openai",
                    "cwd": "/home/shrek/GeoAI",
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "sandbox": {"type": "workspaceWrite"},
                },
            })
            if crash_after == "thread/resume":
                return 1
            continue

        if method == "turn/start":
            if not is_request:
                continue
            if resumed_thread_id is None:
                _write(_error(request_id, -32600, "turn/start before thread/resume"))
                continue
            if active_scenario:
                _write(_error(
                    request_id, -32000,
                    "turn_start_forbidden_on_active_thread: a steerable/"
                    "active thread must never receive a second turn/start",
                ))
                continue
            params = msg.get("params") or {}
            thread_id = params.get("threadId")
            turn_input = params.get("input")
            if not thread_id or not turn_input:
                _write(_error(request_id, -32602, "turn/start requires threadId and input"))
                continue
            if busy_turn:
                _write(_error(request_id, -32000, "thread is busy with another turn"))
                continue
            turn_id = f"turn-{request_id}"
            started_turn = {"id": turn_id, "items": [], "status": "inProgress"}
            # Synchronous ack for the request itself.
            _write({"id": request_id, "result": {"turn": started_turn}})
            # Async notifications: turn/started then turn/completed, both
            # correlated by threadId (per the real protocol, these carry no
            # request id of their own).
            _write({
                "method": "turn/started",
                "params": {"threadId": thread_id, "turn": started_turn},
            })
            completed_turn_id = "wrong-turn-id" if wrong_turn else turn_id
            _write({
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": completed_turn_id, "items": [], "status": "completed"},
                },
            })
            continue

        if method == "turn/steer":
            if not is_request:
                continue
            if resumed_thread_id is None:
                _write(_error(request_id, -32600, "turn/steer before thread/resume"))
                continue
            params = msg.get("params") or {}
            thread_id = params.get("threadId")
            expected_turn_id = params.get("expectedTurnId")
            turn_input = params.get("input")
            if not thread_id or not expected_turn_id or not turn_input:
                _write(_error(
                    request_id, -32602,
                    "turn/steer requires threadId, expectedTurnId and input",
                ))
                continue
            if "--active-zero-inprogress" in sys.argv or "--active-multi-inprogress" in sys.argv:
                _write(_error(
                    request_id, -32000,
                    "turn_steer_unexpected: no unambiguous inProgress turn "
                    "existed at thread/resume time",
                ))
                continue
            if "--thread-status-notloaded" in sys.argv or "--thread-status-systemerror" in sys.argv:
                _write(_error(
                    request_id, -32000,
                    "turn_steer_unexpected: thread was not reported active",
                ))
                continue
            if "--active-not-steerable" in sys.argv:
                _write(_error(
                    request_id, -32001,
                    "activeTurnNotSteerable: turn is in review mode",
                    data={"codexErrorInfo": {"activeTurnNotSteerable": {"turnKind": "review"}}},
                ))
                continue
            if "--active-stale-turn" in sys.argv:
                _write(_error(
                    request_id, -32002,
                    "expectedTurnId does not match the currently active turn (stale)",
                ))
                continue
            if "--active-wrong-turnid" in sys.argv:
                _write({"id": request_id, "result": {"turnId": "mismatched-turn-id"}})
                continue
            # Default happy path (--active-one-turn): synchronous ack only
            # -- deliberately no turn/completed notification, proving the
            # client never waits for one after a successful turn/steer.
            _write({"id": request_id, "result": {"turnId": expected_turn_id}})
            continue

        # Unknown/unsupported method.
        if is_request:
            _write(_error(request_id, -32601, f"method not found: {method}"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
