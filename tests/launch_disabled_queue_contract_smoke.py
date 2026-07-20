#!/usr/bin/env python3
"""Smoke test for the disabled-by-default MCP launch-queue contract (B117).

Proves, with NO process launched and NO network/API call:
  1. default state is DISABLED (launch_enabled False, no launcher implemented);
  2. a launch attempt WITHOUT the env gate is REFUSED (attempt_start raises);
  3. dry-run returns the intent without side effects (no file/dir written, no
     forbidden call fired);
  4. even with BOTH env gates forced ON, launch stays disabled and refused
     (defense in depth: no env flag can start a process in this contract);
  5. the four required schemas (launch request, process state, process log,
     result handoff) are defined and round-trip;
  6. process-log records env as NAME->status tokens only (never a value/secret);
  7. the module source contains no process-spawn / shell / network code.

Isolation-safe & parallel-safe: hermetic env (saved/restored), a per-run temp
cwd, and the module is loaded by FILE PATH so the package __init__ (which a
concurrent worker may be editing) is never imported.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.abspath(
    os.path.join(HERE, "..", "src", "geoai_task_mcp", "launch_queue_contract.py")
)

# --- forbidden-call sentinels ----------------------------------------------
# Any invocation of a process-spawn / shell / network primitive fails the test.
_HITS: list[str] = []


def _boom(name: str):
    def _f(*_a, **_k):
        _HITS.append(name)
        raise AssertionError(f"FORBIDDEN call invoked by contract: {name}")

    return _f


def _install_guards() -> None:
    for attr in (
        "Popen", "run", "call", "check_call", "check_output",
        "getoutput", "getstatusoutput",
    ):
        if hasattr(subprocess, attr):
            setattr(subprocess, attr, _boom(f"subprocess.{attr}"))
    for attr in (
        "system", "popen", "fork", "forkpty",
        "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
        "spawnv", "spawnve", "spawnvp", "spawnl", "spawnle", "spawnlp",
        "posix_spawn", "posix_spawnp",
    ):
        if hasattr(os, attr):
            setattr(os, attr, _boom(f"os.{attr}"))
    # Network: guard the actual connect path but keep socket.socket a real class
    # (replacing it with a function breaks any later `class X(socket)` import).
    try:
        socket.socket.connect = _boom("socket.socket.connect")  # type: ignore[assignment]
    except (AttributeError, TypeError):
        pass
    socket.create_connection = _boom("socket.create_connection")  # type: ignore[assignment]


def _load_module():
    spec = importlib.util.spec_from_file_location("launch_queue_contract_b117", MODULE_PATH)
    assert spec and spec.loader, "cannot build import spec for module"
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve annotations via sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # runs module top-level (stdlib imports only)
    return mod


def _with_env(**kv):
    """Context-less env setter returning a restore callable."""
    saved = {k: os.environ.get(k) for k in kv}

    def _apply():
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    _apply()
    return _restore


REQUIRED_LR = {
    "request_id", "task_id", "runner", "topic", "adapter_id", "binary",
    "argv_template", "cwd_pin", "created_ts", "priority", "state",
}
REQUIRED_LOG = {
    "request_id", "task_id", "runner", "topic", "adapter_id", "from_state",
    "to_state", "decision", "blocked_reason", "env_gate_status", "ts",
}
REQUIRED_HANDOFF = {
    "task_id", "runner", "topic", "adapter_id", "changed_paths", "tests_run",
    "verdict", "token_cost_report", "audit_log_ref", "review_command",
    "finalizer", "worker_must_not",
}
# Substrings that would indicate real launch / shell / network code.
FORBIDDEN_SOURCE = (
    "import subprocess", "subprocess.", "os.system(", "os.popen(", "os.fork(",
    "os.exec", "os.spawn", "posix_spawn(", "pty.spawn", "Popen(",
    "import socket", "socket.socket(", "urllib", "http.client", "httpx",
    "requests.get(", "requests.post(", "shell=True",
)


def main() -> int:
    # Load first (guard-free), then install forbidden-call sentinels so the
    # contract runs entirely under the guards.
    m = _load_module()
    _install_guards()

    # ------------------------------------------------------------------ (1)
    # Default state DISABLED, hermetically (gates unset regardless of ambient).
    restore = _with_env(GEOAI_TASK_MCP_ALLOW_LAUNCH=None, GEOAI_TASK_MCP_ALLOW_WRITES=None)
    try:
        assert m.LAUNCH_IMPLEMENTED is False, "LAUNCH_IMPLEMENTED must be False"
        assert m.WORKFLOW_SWITCH_ENABLED is False, "workflow_switch must be False"
        assert m.PARENT_QUEUE_MUTATION_ENABLED is False, "parent_queue_mutation must be False"
        assert m.launch_enabled() is False, "launch_enabled must be False by default"
        assert m.env_gates_open() is False, "env_gates must be closed by default"
        assert m.writes_allowed() is False and m.launch_flag_set() is False

        gs = m.gate_status()
        assert gs["launch_enabled"] is False and gs["env_gates_open"] is False
        assert gs["workflow_switch_enabled"] is False
        assert gs["parent_queue_mutation_enabled"] is False

        req = m.enqueue_intent(
            task_id="T_DEMO", runner="claude_x", topic="task_mcp",
            adapter_id="claude_cli", argv_template=["claude", "-p", "<prompt>"],
        )
        assert req.state == m.ProcessState.QUEUED

        # ---------------------------------------------------------------- (2)
        # Launch attempt WITHOUT the env gate is REFUSED.
        refused = False
        try:
            m.attempt_start(req)
        except m.LaunchDisabledError as e:
            refused = True
            assert "no process" in str(e).lower()
        assert refused, "attempt_start must refuse (raise) when gates unset"

        dec = m.evaluate_launch(req)
        assert dec.permitted is False
        assert dec.state == m.ProcessState.BLOCKED_LAUNCH_DISABLED
        assert m.ALLOW_LAUNCH_ENV in dec.blocked_reason
        assert "launcher_not_implemented_in_contract" in dec.blocked_reason

        # ---------------------------------------------------------------- (3)
        # Dry-run returns the intent without side effects (temp cwd stays empty).
        with tempfile.TemporaryDirectory(prefix="lqc_b117_") as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                before = sorted(os.listdir(td))
                plan = m.describe_intent(req)
                after = sorted(os.listdir(td))
            finally:
                os.chdir(prev)
        assert before == after == [], "dry-run must not write any file"
        assert plan["mode"] == "launch_queue_contract_dryrun"
        assert plan["would_run_argv"] == [], "no argv is ever run"
        assert plan["decision"]["permitted"] is False
        assert plan["authority_flags"]["process_launch"] is False
        assert plan["authority_flags"]["network_call"] is False
        assert plan["intent"]["binary"] == "claude"

        # audit=True must refuse (no writer in the pure contract).
        audit_refused = False
        try:
            m.describe_intent(req, audit=True)
        except m.LaunchDisabledError:
            audit_refused = True
        assert audit_refused, "describe_intent(audit=True) must refuse to write"
    finally:
        restore()

    # ------------------------------------------------------------------ (4)
    # Both gates forced ON -> launch STILL disabled and refused (defense-in-depth).
    restore = _with_env(GEOAI_TASK_MCP_ALLOW_LAUNCH="1", GEOAI_TASK_MCP_ALLOW_WRITES="1")
    try:
        assert m.env_gates_open() is True, "both gates set should read as open"
        assert m.launch_enabled() is False, "launch_enabled must stay False even when gated"
        still_refused = False
        try:
            m.attempt_start(req)
        except m.LaunchDisabledError:
            still_refused = True
        assert still_refused, "attempt_start must refuse even with both gates ON"
        dec2 = m.evaluate_launch(req)
        assert dec2.permitted is False
        assert "launcher_not_implemented_in_contract" in dec2.blocked_reason
    finally:
        restore()

    # Only one gate set -> gates NOT open.
    restore = _with_env(GEOAI_TASK_MCP_ALLOW_LAUNCH="1", GEOAI_TASK_MCP_ALLOW_WRITES=None)
    try:
        assert m.env_gates_open() is False, "one gate is insufficient"
    finally:
        restore()

    # ------------------------------------------------------------------ (5)
    # All four required schemas defined and round-trip.
    assert [s.value for s in m.ProcessState] == [
        "queued", "planned", "blocked_launch_disabled", "would_run", "terminal"
    ]
    lr = m.enqueue_intent(
        task_id="T2", runner="codex_x", topic="task_mcp", adapter_id="codex_cli"
    ).as_dict()
    assert REQUIRED_LR.issubset(lr), f"launch_request schema missing: {REQUIRED_LR - set(lr)}"
    assert lr["binary"] == "codex"

    dec = m.evaluate_launch(m.enqueue_intent(
        task_id="T3", runner="dsr", topic="task_mcp", adapter_id="deepseek_manual"))
    assert dec.permitted is False
    # manual adapter is never a local launchable command.
    assert "manual_external_adapter_never_local_launch" in dec.blocked_reason

    log = m.build_log_entry(
        m.enqueue_intent(task_id="T4", runner="r", topic="task_mcp", adapter_id="claude_cli"),
        dec,
    ).as_dict()
    assert REQUIRED_LOG.issubset(log), f"process_log schema missing: {REQUIRED_LOG - set(log)}"

    handoff = m.ResultHandoff(
        task_id="T5", runner="claude_x", topic="task_mcp", adapter_id="claude_cli",
        changed_paths=["a.py"], tests_run=["t.sh"], verdict="pending",
    ).as_dict()
    assert REQUIRED_HANDOFF.issubset(handoff), (
        f"result_handoff schema missing: {REQUIRED_HANDOFF - set(handoff)}")
    assert handoff["finalizer"] == "codex"
    assert "review" in handoff["review_command"]
    assert "self-review own executed task" in handoff["worker_must_not"]

    # ------------------------------------------------------------------ (6)
    # Process log records env as NAME->status tokens only, never a value/secret.
    restore = _with_env(
        GEOAI_TASK_MCP_ALLOW_LAUNCH="1",
        GEOAI_TASK_MCP_ALLOW_WRITES="1",
        GEOAI_TASK_MCP_API_TOKEN="super-secret-value-should-never-appear",
    )
    try:
        tokens = m.env_gate_status_tokens()
        assert set(tokens.values()) <= {"<set>", "<unset>"}, tokens
        # only the two explicit gate names are recorded; no secret name/value leaks
        assert set(tokens) == {m.ALLOW_LAUNCH_ENV, m.ALLOW_WRITES_ENV}
        blob = repr(m.build_log_entry(
            m.enqueue_intent(task_id="T6", runner="r", topic="task_mcp",
                             adapter_id="claude_cli"),
            m.evaluate_launch(m.enqueue_intent(
                task_id="T6", runner="r", topic="task_mcp", adapter_id="claude_cli")),
        ).as_dict())
        assert "super-secret-value-should-never-appear" not in blob
    finally:
        restore()

    # ------------------------------------------------------------------ (7)
    # Static source scan: no process-spawn / shell / network code.
    with open(MODULE_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    found = [tok for tok in FORBIDDEN_SOURCE if tok in src]
    assert not found, f"forbidden process/network tokens in module source: {found}"

    # contract summary invariants.
    cs = m.contract_summary()
    assert cs["launch_enabled"] is False
    assert cs["workflow_switch_enabled"] is False
    assert cs["parent_queue_mutation_enabled"] is False
    assert cs["env_gates"]["both_required_for_future_launch"] is True

    # No forbidden call ever fired.
    assert _HITS == [], f"forbidden calls were invoked: {_HITS}"

    print("PASS: launch-queue contract is disabled-by-default, refuses launch, "
          "no process/network call, schemas round-trip, no secret leak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
