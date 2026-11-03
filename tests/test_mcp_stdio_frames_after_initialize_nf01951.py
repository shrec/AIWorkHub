"""NF-2026-01951: record every stdio frame the real MCP server writes after initialize.

Claude Code tears the AIWorkHub MCP server down 17-40 s after each connect and
logs no cause. This probe spawns ``python -m aiworkhub.server`` exactly the way
``tests/mcp_stdio_client_smoke.py::_server_params`` does (only the repository
binding and the audit paths point at a fixture under pytest's tmp dir), speaks
raw newline-delimited JSON-RPC over the pipes -- no SDK client, so nothing is
filtered -- and records every stdout line with a timestamp and a
classification (response / request / notification / non_json / invalid).

One probe session runs once per pytest session (shared across xdist workers
through pytest's own tmp root) and four tests then assert one hypothesis each
on the recorded frames:

(a) ``notifications/progress`` never follows the response of its call and never
    carries a progressToken the client did not send;
(b) every stdout line is a JSON-RPC 2.0 object;
(c) no unsolicited request or notification arrives in an idle window >= 20 s;
(d) every ``ping`` is answered within 5 s, during an in-flight tool call and
    while idle.

A hypothesis that reproduces keeps its assertion and is marked
``xfail(strict=True)`` with the observed frame or latency in the reason.
Only refusal paths of tools are called and the server runs with writes and
launches closed, so the session is read-only; every file it may write lives
under pytest's tmp dir. Each test's record is also kept as ``probe.json``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SMOKE_PATH = ROOT / "tests" / "mcp_stdio_client_smoke.py"
_SPEC = importlib.util.spec_from_file_location("mcp_stdio_client_smoke_nf01951", _SMOKE_PATH)
assert _SPEC and _SPEC.loader
_smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_smoke)

PROTOCOL_VERSION = "2025-06-18"
PROGRESS_TOOL = "aiworkhub_agent_accept_review"  # @mcp.tool(report_progress=True)
INFLIGHT_TOOL = "aiworkhub_task_health"  # frozen read-only contract tool
IDLE_WINDOW_SECONDS = 22.0
PING_INTERVAL_SECONDS = 2.0
PING_DEADLINE_SECONDS = 5.0
RESPONSE_TIMEOUT_SECONDS = 30.0
STARTUP_TIMEOUT_SECONDS = 60.0
SHUTDOWN_WAIT_SECONDS = 10.0  # per proc.wait; wait + kill-wait = 2x
_SHARED_WAIT_MARGIN_SECONDS = 30.0
# The owner's worst case: startup, three tool calls, the idle window, two
# settle_pings rounds of 2x the ping deadline each, then shutdown; plus margin.
SHARED_WAIT_SECONDS = (
    STARTUP_TIMEOUT_SECONDS
    + 3 * RESPONSE_TIMEOUT_SECONDS
    + IDLE_WINDOW_SECONDS
    + 2 * 2 * PING_DEADLINE_SECONDS
    + 2 * SHUTDOWN_WAIT_SECONDS
    + _SHARED_WAIT_MARGIN_SECONDS
)
_POLL_SECONDS = 0.25
_STDERR_TAIL = 1500
TOKEN_WITH_PROGRESS = "nf01951-progress-token"
TOKEN_INFLIGHT = "nf01951-health-token"
_LINE_PREVIEW = 300


def classify(raw: bytes) -> tuple[str, Any]:
    """Classify one stdout line exactly as a strict JSON-RPC 2.0 client would."""

    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "non_json", None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return "invalid", message
    has_method = isinstance(message.get("method"), str)
    if has_method and "id" in message:
        return "request", message
    if has_method:
        return "notification", message
    if "id" in message and (("result" in message) != ("error" in message)):
        return "response", message
    return "invalid", message


class RawStdioSession:
    """Newline-delimited JSON-RPC over the child's pipes; records every line."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.t0 = time.perf_counter()
        self.frames: list[dict[str, Any]] = []
        self.sent: dict[int, dict[str, Any]] = {}
        self.pings: list[dict[str, Any]] = []
        self.responses: dict[Any, dict[str, Any]] = {}  # one per request sent: bounded
        self.calls: list[dict[str, Any]] = []  # one per tools/call answered
        self._cond = threading.Condition()
        self._next_id = 0
        self.reader = threading.Thread(target=self._read, name="nf01951-stdout", daemon=True)
        self.reader.start()

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            kind, message = classify(raw)
            frame = {
                "t": self.now(),
                "kind": kind,
                "line": raw[:_LINE_PREVIEW].decode("utf-8", errors="replace").rstrip(),
                "id": message.get("id") if isinstance(message, dict) else None,
                "method": message.get("method") if isinstance(message, dict) else None,
                "progress_token": (message.get("params") or {}).get("progressToken")
                if isinstance(message, dict) and isinstance(message.get("params"), dict)
                else None,
            }
            with self._cond:
                self.frames.append(frame)
                if kind == "response":
                    self.responses[frame["id"]] = message
                self._cond.notify_all()

    def _send(self, message: dict[str, Any]) -> float:
        assert self.proc.stdin is not None
        payload = json.dumps(message).encode("utf-8") + b"\n"
        sent_at = self.now()
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()
        return sent_at

    def request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        token = ((params or {}).get("_meta") or {}).get("progressToken")
        self.sent[request_id] = {"method": method, "progress_token": token}
        self.sent[request_id]["sent_at"] = self._send(message)
        return request_id

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def response_frame(self, request_id: int) -> dict[str, Any] | None:
        for frame in self.frames:
            if frame["kind"] == "response" and frame["id"] == request_id:
                return frame
        return None

    def wait_response(self, request_id: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.perf_counter() + timeout
        with self._cond:
            while True:
                if request_id in self.responses:
                    return self.responses[request_id]
                remaining = deadline - time.perf_counter()
                if remaining <= 0 or self.proc.poll() is not None:
                    return None
                self._cond.wait(remaining)

    def ping(self, phase: str, call_id: int | None = None) -> int:
        request_id = self.request("ping")
        self.pings.append(
            {"id": request_id, "phase": phase, "call_id": call_id, "sent_at": self.sent[request_id]["sent_at"]}
        )
        return request_id

    def require_alive(self, context: str) -> None:
        """Fail at once, with the exit code, instead of pinging a dead server."""

        returncode = self.proc.poll()
        if returncode is not None:
            raise AssertionError(f"server exited rc={returncode} during {context}")

    def call_with_pings(self, name: str, arguments: dict[str, Any], token: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if token is not None:
            params["_meta"] = {"progressToken": token}
        call_id = self.request("tools/call", params)
        # Sent right after the call; settle_pings records whether it overlapped.
        self.ping("in_flight", call_id)
        deadline = time.perf_counter() + RESPONSE_TIMEOUT_SECONDS
        while time.perf_counter() < deadline:
            response = self.wait_response(call_id, PING_INTERVAL_SECONDS)
            if response is not None:
                frame = self.response_frame(call_id)
                assert frame is not None
                sent_at = self.sent[call_id]["sent_at"]
                self.calls.append(
                    {"name": name, "id": call_id, "sent_at": sent_at, "call_ms": (frame["t"] - sent_at) * 1000.0}
                )
                return response
            self.require_alive(f"tools/call {name}")
            self.ping("in_flight", call_id)
        raise AssertionError(f"tools/call {name} unanswered after {RESPONSE_TIMEOUT_SECONDS} s")

    def settle_pings(self) -> None:
        for ping in self.pings:
            if "latency" in ping:
                continue
            remaining = ping["sent_at"] + 2 * PING_DEADLINE_SECONDS - self.now()
            self.wait_response(ping["id"], max(remaining, 0.0))
            answered = self.response_frame(ping["id"])
            ping["latency"] = None if answered is None else answered["t"] - ping["sent_at"]
            call_frame = None if ping["call_id"] is None else self.response_frame(ping["call_id"])
            # overlapped: the ping was answered while its call was still in flight.
            ping["overlapped"] = (
                call_frame is not None and answered is not None and answered["t"] < call_frame["t"]
            )


def _drive(session: RawStdioSession) -> dict[str, Any]:
    init_id = session.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "nf01951-frame-probe", "version": "1"},
        },
    )
    # Cold start imports the whole server; give it its own, named, deadline.
    init = session.wait_response(init_id, STARTUP_TIMEOUT_SECONDS)
    assert init is not None and "result" in init, (
        f"initialize failed after {STARTUP_TIMEOUT_SECONDS} s: {init!r} "
        f"server_alive={session.proc.poll() is None}"
    )
    session.notify("notifications/initialized")
    initialized_at = session.now()

    listed_id = session.request("tools/list", {})
    listed = session.wait_response(listed_id, RESPONSE_TIMEOUT_SECONDS)
    assert listed is not None and "result" in listed, f"tools/list failed: {listed!r}"
    names = {tool.get("name") for tool in listed["result"].get("tools", [])}
    missing = [name for name in (PROGRESS_TOOL, INFLIGHT_TOOL) if name not in names]
    assert not missing, f"probe_tool_not_listed:{missing}"

    # (a) the report_progress tool, refused inside the tool -- after the
    # progress wrapper started, before any manager work -- by a contradictory
    # learning close-out (learning_inline_conflict), with and without a token.
    refusal_args = {
        "request_id": "nf01951-none",
        "task_id": "nf01951-none",
        "lesson_candidate": "nf01951 probe: never accepted",
        "no_new_lesson_reason": "no_generalizable_cause",
    }
    calls = [
        (PROGRESS_TOOL, refusal_args, TOKEN_WITH_PROGRESS),
        (PROGRESS_TOOL, refusal_args, None),
        # (d) a synchronous read-only tool, pinged while in flight.
        (INFLIGHT_TOOL, {}, TOKEN_INFLIGHT),
    ]
    for name, arguments, token in calls:
        response = session.call_with_pings(name, arguments, token)
        assert "result" in response, f"tools/call {name} answered without a result: {response!r}"
        assert not response["result"].get("isError"), (
            f"tools/call {name} rejected before the tool ran: {json.dumps(response)[:_LINE_PREVIEW]}"
        )
    session.settle_pings()

    # (c)+(d) idle window: only pings go out, everything else is unsolicited.
    idle_start = session.now()
    while session.now() - idle_start < IDLE_WINDOW_SECONDS:
        ping_id = session.ping("idle")
        session.wait_response(ping_id, PING_DEADLINE_SECONDS)
        session.require_alive("idle window")
        time.sleep(max(0.0, session.sent[ping_id]["sent_at"] + PING_INTERVAL_SECONDS - session.now()))
    idle_end = session.now()
    session.settle_pings()
    return {"initialized_at": initialized_at, "idle_window": [idle_start, idle_end]}


def _run_probe(work: Path) -> dict[str, Any]:
    repo = work / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("NF-2026-01951 stdio frame probe fixture\n", encoding="utf-8")
    audit_dir = work / "audit"
    audit_dir.mkdir()
    params = _smoke._server_params(audit_dir / "mcp_audit.jsonl", None)
    params.env["AIWORKHUB_REPO"] = str(repo)
    params.env["AIWORKHUB_REPO_ROOT"] = str(repo)
    spawned_at = time.perf_counter()
    with (work / "server.stderr.log").open("wb") as stderr_sink, subprocess.Popen(
        [params.command, *params.args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_sink,
        env=params.env,
        cwd=str(repo),
    ) as proc:
        session = RawStdioSession(proc)
        failure: Exception | None = None
        try:
            timing = _drive(session)
        except (AssertionError, OSError) as exc:  # OSError covers BrokenPipeError
            failure = exc
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:  # the child already closed its end
                    pass
            try:
                returncode = proc.wait(timeout=SHUTDOWN_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait(timeout=SHUTDOWN_WAIT_SECONDS)
            session.reader.join(timeout=SHUTDOWN_WAIT_SECONDS)
    probe_wall_seconds = time.perf_counter() - spawned_at
    if failure is not None:
        # Name the cause: a server that died, or one still starting up.
        stderr_tail = (work / "server.stderr.log").read_bytes()[-_STDERR_TAIL:]
        raise AssertionError(
            f"{type(failure).__name__}: {failure} | server_returncode={returncode} "
            f"frames_seen={len(session.frames)} at+{session.now():.1f}s "
            f"probe_wall_seconds={probe_wall_seconds:.1f} | "
            f"stderr_tail={stderr_tail.decode('utf-8', errors='replace')!r}"
        ) from failure
    return {
        **timing,
        "frames": session.frames,
        "sent": {str(k): v for k, v in session.sent.items()},
        "sent_tokens": sorted(t for t in (v["progress_token"] for v in session.sent.values()) if t),
        "pings": session.pings,
        "calls": session.calls,
        "returncode": returncode,
        "probe_wall_seconds": probe_wall_seconds,
    }


def _claim(lock: Path) -> bool:
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


@pytest.fixture(scope="session")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the probe once per pytest session, also under pytest-xdist.

    Workers share pytest's tmp root; the first one to claim the lock runs the
    probe and publishes its record, the others read that record.
    """

    base = tmp_path_factory.getbasetemp()
    shared = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    record = shared / "nf01951_probe.json"
    if _claim(shared / "nf01951_probe.lock"):
        work = tmp_path_factory.mktemp("nf01951")
        try:
            result: dict[str, Any] = _run_probe(work)
        except BaseException as exc:  # published so waiting workers fail with the cause
            _publish(record, {"error": f"{type(exc).__name__}: {exc}"})
            raise
        (work / "probe.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        _publish(record, result)
    else:
        result = _await_record(record)
    if "error" in result:
        pytest.fail(f"probe_failed:{result['error']}")
    return result


def _publish(record: Path, result: dict[str, Any]) -> None:
    """Write the shared record atomically: readers see nothing or all of it."""

    staging = record.with_name(record.name + ".tmp")
    staging.write_text(json.dumps(result), encoding="utf-8")
    deadline = time.monotonic() + SHUTDOWN_WAIT_SECONDS
    while True:
        try:
            os.replace(staging, record)
            return
        except PermissionError:  # Windows: a reader holds the target open
            if time.monotonic() > deadline:
                raise
            time.sleep(_POLL_SECONDS)


def _await_record(record: Path) -> dict[str, Any]:
    deadline = time.monotonic() + SHARED_WAIT_SECONDS
    last_error = "absent"
    while time.monotonic() <= deadline:
        try:
            loaded = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if isinstance(loaded, dict):
                return loaded
            last_error = f"not_an_object:{type(loaded).__name__}"
        time.sleep(_POLL_SECONDS)
    pytest.fail(f"probe_record_unreadable_after:{SHARED_WAIT_SECONDS}s:{record}:{last_error}")


def _response_time(probe: dict[str, Any], request_id: str | None) -> float | None:
    for frame in probe["frames"]:
        if frame["kind"] == "response" and str(frame["id"]) == request_id:
            return frame["t"]
    return None


def test_probe_recorded_the_whole_session(probe: dict[str, Any]) -> None:
    """The probe is not vacuous: every request was answered, both phases pinged."""

    answered = {str(f["id"]) for f in probe["frames"] if f["kind"] == "response"}
    assert set(probe["sent"]) <= answered, sorted(set(probe["sent"]) - answered)
    tools_called = [v for v in probe["sent"].values() if v["method"] == "tools/call"]
    assert len(tools_called) == 3
    assert probe["sent_tokens"] == sorted([TOKEN_WITH_PROGRESS, TOKEN_INFLIGHT])
    start, end = probe["idle_window"]
    assert end - start >= 20.0
    assert {p["phase"] for p in probe["pings"]} == {"in_flight", "idle"}
    assert probe["returncode"] == 0, f"server exit code {probe['returncode']} after stdin EOF"


def test_a_no_progress_after_response_or_for_unsent_token(probe: dict[str, Any]) -> None:
    call_by_token = {
        v["progress_token"]: k
        for k, v in probe["sent"].items()
        if v["method"] == "tools/call" and v["progress_token"]
    }
    offenders = []
    for frame in probe["frames"]:
        if frame["kind"] != "notification" or frame["method"] != "notifications/progress":
            continue
        token = frame["progress_token"]
        if token not in probe["sent_tokens"]:
            offenders.append(f"unsent_token:{frame['line']}")
            continue
        answered = _response_time(probe, call_by_token.get(token))
        if answered is not None and frame["t"] > answered:
            offenders.append(f"after_response(+{frame['t'] - answered:.3f}s):{frame['line']}")
    assert not offenders, offenders


def test_b_every_stdout_line_is_jsonrpc_2_0(probe: dict[str, Any]) -> None:
    assert probe["frames"], "no stdout frame recorded"
    bad = [f"{f['kind']}:{f['line']}" for f in probe["frames"] if f["kind"] in {"non_json", "invalid"}]
    assert not bad, bad


def test_c_no_unsolicited_frames_during_idle_window(probe: dict[str, Any]) -> None:
    start, end = probe["idle_window"]
    unsolicited = [
        f"{f['kind']}@+{f['t'] - start:.3f}s:{f['line']}"
        for f in probe["frames"]
        if start <= f["t"] <= end and f["kind"] != "response"
    ]
    assert not unsolicited, unsolicited


def test_d_every_ping_answered_within_deadline(probe: dict[str, Any]) -> None:
    slow = [
        f"{p['phase']}:id={p['id']}:latency="
        + ("unanswered" if p["latency"] is None else f"{p['latency']:.3f}s")
        for p in probe["pings"]
        if p["latency"] is None or p["latency"] > PING_DEADLINE_SECONDS
    ]
    assert not slow, slow
    # A ping read only after its call's response proves nothing about a ping
    # during a long call: require at least one that really overlapped it.
    in_flight = [p for p in probe["pings"] if p["phase"] == "in_flight"]
    assert in_flight, "no in_flight ping recorded"
    overlapped = [p for p in in_flight if p["overlapped"]]
    if not overlapped:
        call_ms = ",".join(f"{c['name']}={c['call_ms']:.1f}ms" for c in probe["calls"])
        pytest.skip(f"in_flight_not_exercised: {call_ms}")
