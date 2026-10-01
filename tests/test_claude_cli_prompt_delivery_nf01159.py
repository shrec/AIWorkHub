"""NF-2026-01159: the worker prompt must reach the child's stdin, or the launch must fail loudly.

The defect: a ``claude_cli`` plan deliberately carries no positional prompt
(``runtime_adapters`` builds ``claude -p --output-format stream-json ...`` and
puts the prompt in ``plan.stdin_text``), the launcher wrote those bytes from an
unjoined daemon thread that swallowed every error, and
``worker_supervisor.main`` mapped a zero-byte read to the same ``None`` that
means "this plan has no prompt".  ``supervise`` then gave the child
``stdin=DEVNULL`` and ``claude -p`` exited immediately with ``Input must be
provided either through stdin or as a prompt argument when using --print``,
changing nothing.

These tests drive the real ``launch_isolated`` into the real
``worker_supervisor`` process with a fake child, and prove the three facts the
old code could not tell apart: the bytes arrive exactly and EOF follows, a
declared prompt that arrives empty is refused as ``worker_prompt_not_delivered``
before any child is spawned, and a plan with no prompt still gets DEVNULL.

Nothing here is platform-gated: every test runs on Windows and on POSIX.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher,
    runtime_adapters,
    worker_ai_tools_mcp,
    worker_supervisor,
    worker_workspace,
)

_REQUEST_ID = "R-nf01159"
_TASK_ID = "T-nf01159"
# Deliberately not ASCII-only and deliberately larger than one buffered pipe
# write, so an assertion on the byte count is not the same assertion as one on
# the text, and a partial write cannot pass as a whole one.
_PROMPT = "NF-2026-01159 prompt — keep $TOKEN literal\n" + ("x" * 9_000)
_PROMPT_BYTES = _PROMPT.encode("utf-8")

# Reads its whole stdin, records the exact bytes, and only then records that
# ``read()`` returned -- which it can only do once the write end is closed.
_FAKE_CHILD = """\
import pathlib
import sys

record = pathlib.Path(sys.argv[1])
data = sys.stdin.buffer.read()
record.write_bytes(data)
record.with_suffix(".eof").write_text("eof", encoding="utf-8")
"""

# A child that must never run: its only job is to leave proof if it did.
_FORBIDDEN_CHILD = """\
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text("spawned", encoding="utf-8")
"""


def _write_0600(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON at mode 0600 without a ``chmod`` call.

    ``worker_supervisor._load_spec`` refuses a group/other-readable spec on
    POSIX, and the worker sandbox denies ``chmod``; ``os.open`` sets the mode
    at creation instead, which needs neither.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    handle = os.open(path, flags, 0o600)
    with os.fdopen(handle, "wb") as stream:
        stream.write(json.dumps(payload, default=str).encode("utf-8"))


def _wait_for(path: Path, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _supervisor_script() -> Path:
    return Path(worker_supervisor.__file__).resolve()


class _StdinSink:
    """Stands in for the supervisor's stdin pipe and records what was written."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Manager:
    """The collaborators ``launch_isolated`` reads off ``self``.

    Everything the prompt travels through -- the plan, the worker MCP config
    injection, the spec write, the supervisor spawn and the pipe -- is real;
    only the surrounding task lifecycle is stubbed.
    """

    def __init__(self, repo: Path, process_dir: Path, plan: Any, *, real_popen: bool) -> None:
        self.repo = repo
        self.process_dir = process_dir
        self.plan = plan
        self.real_popen = real_popen
        self.events: list[dict[str, Any]] = []
        self.popen_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.stdin_sinks: list[_StdinSink] = []
        self._live: dict[str, Any] = {}
        self._lock = contextlib.nullcontext()

    def _append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self.events.append(event)
        event.setdefault("request_id", _REQUEST_ID)
        return event

    def _blocked(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return process_launcher.ProcessManager._blocked(self, *args, **kwargs)

    def _preflight_card(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "request_id": _REQUEST_ID,
            "allowed_writes": ["src/a.py"],
            "required_outputs": ["src/a.py"],
        }

    def _with_dependency_inputs(self, card: dict[str, Any]) -> dict[str, Any]:
        return dict(card)

    def _resolve_provider_env(
        self, _adapter_id: str, model: str | None
    ) -> tuple[None, str | None]:
        return None, model

    def _launch_reservation(self, _event: dict[str, Any]) -> Any:
        return contextlib.nullcontext()

    def _terminal_authority_grant_path(self, request_id: str) -> Path:
        return self.process_dir / f"{request_id}.authority.json"

    def _terminal_authority_key(self) -> bytes:
        return b"nf01159-key"

    def _build_adapter(self, **_kwargs: Any) -> Any:
        return self.plan

    def _popen(self, argv: list[str], **kwargs: Any) -> Any:
        self.popen_calls.append((list(argv), dict(kwargs)))
        if self.real_popen:
            return subprocess.Popen(argv, **kwargs)
        sink = _StdinSink() if kwargs.get("stdin") is subprocess.PIPE else None
        if sink is not None:
            self.stdin_sinks.append(sink)
        return SimpleNamespace(pid=4242, stdin=sink)

    def _monitor(self, _live: Any) -> None:
        return None


def _runtime(workspace: Any) -> SimpleNamespace:
    runtime_dir = workspace.home / "task_mcp_worker_runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    ledger = runtime_dir / "audit_ledger.jsonl"
    key = runtime_dir / "audit_hmac.key"
    ledger.write_bytes(b"")
    key.write_bytes(b"k" * 32)
    config = runtime_dir / "claude_mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return SimpleNamespace(
        server_name=worker_ai_tools_mcp.SERVER_NAME,
        tool_names=(),
        env={},
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
        claude_mcp_config_path=config,
        copilot_mcp_config_path=runtime_dir / "copilot_mcp_config.json",
        codex_config_toml_path=workspace.home / ".codex" / "config.toml",
        kilo_config_path=workspace.home / ".config" / "kilo" / "kilo.json",
        package_import_root=worker_ai_tools_mcp.resolve_host_package_import_root(),
    )


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    child_argv: list[str],
    stdin_text: str | None,
    real_popen: bool = True,
) -> SimpleNamespace:
    base = tmp_path.resolve()
    authority = base / "authority"
    root = base / ".aiworkhub" / "runtime" / "worktrees" / _REQUEST_ID
    workspace = worker_workspace.WorkerWorkspace(
        request_id=_REQUEST_ID,
        repo=authority,
        path=root / "worktree",
        home=root / "home",
        allowed_writes=("src/a.py",),
        parent_baseline={},
        workspace_baseline={},
    )
    process_dir = base / "processes"
    for directory in (authority, workspace.path, workspace.home, process_dir):
        directory.mkdir(parents=True)
    runtime = _runtime(workspace)
    plan = runtime_adapters.RuntimeAdapterPlan(
        adapter_id="claude_cli",
        argv=list(child_argv),
        cwd=str(workspace.path),
        executable=child_argv[0],
        launchable=True,
        manual_only=False,
        validation_ok=True,
        validation_reason="",
        reasoning_decision=None,
        context_capacity=None,
        stdin_text=stdin_text,
    )
    manager = _Manager(authority, process_dir, plan, real_popen=real_popen)
    writes: list[tuple[Path, dict[str, Any]]] = []
    failed: list[str] = []

    class _TaskEngine:
        @staticmethod
        def claim_start_exact(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"ok": True, "card": {"claim_epoch": 1}}

        @staticmethod
        def mark_launch_failed(*_a: Any, reason: str, **_k: Any) -> dict[str, Any]:
            failed.append(reason)
            return {"ok": True}

    def _spy_write(path: Any, data: dict[str, Any]) -> None:
        writes.append((Path(path), data))
        _write_0600(Path(path), data)

    def _set(name: str, value: Any) -> None:
        monkeypatch.setattr(process_launcher, name, value)

    monkeypatch.setattr(worker_workspace, "chmod_path", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime_adapters, "probe_release", lambda _exe: {"ok": False})
    _set("launch_gates_open", lambda: True)
    _set("task_engine", _TaskEngine)
    _set("_validate_adapter_identity", lambda *_a: None)
    _set("validate_workforce_identity", lambda _r, _a, model, **_k: model)
    _set("_memory_launch_admission", lambda: {"admit": True})
    _set("_external_readonly_dirs", lambda *_a: [])
    _set("_task_authority_repo", lambda *_a: authority)
    _set("_launch_project_context", lambda *_a: None)
    _set("_launch_source_graph_request", lambda *_a: None)
    # Not windows_appcontainer: this exercises the plain-subprocess branch the
    # POSIX hosts in the report take.  The AppContainer branch is untouched and
    # stays covered by tests/test_worker_prompt_via_stdin.py.
    _set("_sandbox_backend_for_adapter", lambda _adapter_id: "landlock")
    _set("sandbox_argv", lambda _ws, _adapter, argv, **_k: list(argv))
    _set("create_workspace", lambda *_a: workspace)
    _set("build_residual_contract_manifest", lambda *_a: [])
    _set("_materialize_worker_rework_overlay", lambda *_a, **_k: (None, None))
    _set("_materialize_crash_retry_packet", lambda *_a, **_k: (None, None))
    _set("_provision_worker_mcp_runtime_for_authority", lambda *_a, **_k: runtime)
    _set("_worker_mcp_source_graph_targets", lambda _context: ("src/a.py",))
    _set("_worker_mcp_session_topic", lambda *_a: "topic")
    _set("build_worker_prompt", lambda **_k: stdin_text or "")
    _set("worker_launch_env", lambda *_a, **_k: dict(os.environ))
    _set("worker_temp_environment", lambda _repo, _request_id: {})
    _set("worker_validation_affordance_env", lambda *_a, **_k: {})
    _set("_worker_launch_cwd", lambda path: str(path))
    _set("_worker_supervisor_script", _supervisor_script)
    _set("_touch_0600", lambda path: Path(path).write_text("", encoding="utf-8"))
    _set("chmod_path", lambda *_a: None)
    _set("write_json_0600", _spy_write)
    _set("_write_terminal_authority_grant", lambda *_a, **_k: None)
    _set("_release_launch_request_resources", lambda **_k: [])
    _set("_pid_start_ticks", lambda _pid: 123)
    _set("process_group_launch_kwargs", lambda _name: {})
    _set(
        "_committed_claim_card",
        lambda _claim, **_k: {
            "request_id": _REQUEST_ID,
            "claim_epoch": 1,
            "allowed_writes": ["src/a.py"],
        },
    )
    return SimpleNamespace(
        manager=manager,
        workspace=workspace,
        process_dir=process_dir,
        writes=writes,
        failed=failed,
        plan=plan,
    )


def _launch(harness: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "task_id": _TASK_ID,
        "runner": "claude",
        "topic": "topic",
        "adapter_id": "claude_cli",
        "model": None,
        "timeout_seconds": 120,
        # Keyword-only and required on ``_launch_isolated``.  The harness stubs
        # ``build_worker_prompt``, so the prompt under test is the stub's return
        # value and this value is inert -- it only has to be passed.
        "owner_prompt": "",
    }
    kwargs.update(overrides)
    return process_launcher.ProcessManager._launch_isolated(harness.manager, **kwargs)


def _spec(harness: SimpleNamespace) -> dict[str, Any] | None:
    return next(
        (
            payload
            for path, payload in harness.writes
            if path.name == f"{_REQUEST_ID}.supervisor-spec.json"
        ),
        None,
    )


def _supervisor_status(spec: dict[str, Any]) -> str:
    path = Path(str(spec["status_path"]))
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"<no supervisor status at {path}: {exc}>"


def _reap(harness: SimpleNamespace) -> None:
    for live in list(harness.manager._live.values()):
        process = getattr(live, "process", None)
        if isinstance(process, subprocess.Popen):
            with contextlib.suppress(Exception):
                process.wait(timeout=30)
            with contextlib.suppress(Exception):
                process.kill()


def test_nf01159_claude_plan_carries_the_prompt_only_in_the_stdin_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one encoder every launcher routes through sees the whole prompt.

    ``claude -p`` has no positional prompt to fall back on, so these bytes are
    the only copy: if they are lost the worker has nothing to do.
    """
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        _PROMPT,
        repo,
        executable_overrides={"claude_cli": str(executable.resolve())},
    )

    assert plan.launchable is True, plan.validation_reason
    assert _PROMPT not in plan.argv
    assert runtime_adapters.plan_stdin_payload(plan) == _PROMPT_BYTES
    # An argv-prompt adapter, and a plan-shaped object with no attribute at
    # all, both mean "nothing to deliver" -- which is what keeps DEVNULL and
    # the supervisor's no-expectation branch exactly as they were.
    assert runtime_adapters.plan_stdin_payload(SimpleNamespace()) is None
    assert runtime_adapters.plan_stdin_payload(SimpleNamespace(stdin_text="")) is None


def test_nf01159_launcher_and_supervisor_name_the_prompt_byte_count_identically() -> None:
    """The one spec key that carries the expectation across the process boundary.

    ``worker_supervisor`` runs as a standalone script and cannot import
    ``runtime_adapters``, so the key is spelled out in both modules.  The
    launcher writes it and the supervisor reads it: if the two spellings ever
    drift, every declared prompt silently loses its expectation and the
    DEVNULL launch this whole fix removes comes straight back.
    """
    assert (
        runtime_adapters.WORKER_PROMPT_BYTES_SPEC_KEY
        == worker_supervisor.WORKER_PROMPT_BYTES_SPEC_KEY
    )


def test_nf01159_launch_isolated_delivers_the_exact_prompt_bytes_and_eof_to_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: real ``launch_isolated`` -> real supervisor -> fake child.

    The child records the bytes it read and, separately, that ``read()``
    returned -- which it can only do once the write end is closed.  Both files
    together are the proof the old fire-and-forget feeder never produced.
    """
    record = tmp_path / "child-stdin.bin"
    child = _script(tmp_path, "fake_child.py", _FAKE_CHILD)
    harness = _harness(
        monkeypatch,
        tmp_path,
        child_argv=[sys.executable, str(child), str(record)],
        stdin_text=_PROMPT,
    )

    result = _launch(harness)
    try:
        assert result["ok"] is True, result
        assert harness.failed == []
        spec = _spec(harness)
        assert spec is not None
        assert spec[runtime_adapters.WORKER_PROMPT_BYTES_SPEC_KEY] == len(_PROMPT_BYTES)

        assert _wait_for(record.with_suffix(".eof")), _supervisor_status(spec)
        assert record.read_bytes() == _PROMPT_BYTES
    finally:
        _reap(harness)


def test_nf01159_supervisor_refuses_a_zero_byte_delivery_and_never_spawns_the_child(
    tmp_path: Path,
) -> None:
    """A declared prompt that arrives empty is typed, not a DEVNULL launch.

    The reported failure, reproduced through the real supervisor entrypoint:
    ``stdin=DEVNULL`` is exactly what the supervisor saw once the prompt was
    lost upstream, and it used to answer by handing the child the same DEVNULL.
    """
    proof = tmp_path / "child-ran.txt"
    child = _script(tmp_path, "forbidden_child.py", _FORBIDDEN_CHILD)
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    status_path = process_dir / "status.json"
    spec_path = tmp_path / "spec.json"
    _write_0600(
        spec_path,
        {
            "argv": [sys.executable, str(child), str(proof)],
            "cwd": str(tmp_path),
            "timeout_seconds": 60,
            "status_path": str(status_path),
            "cancel_path": str(process_dir / "cancel.json"),
            "stdout_path": str(process_dir / "stdout.log"),
            "stderr_path": str(process_dir / "stderr.log"),
            worker_supervisor.WORKER_PROMPT_BYTES_SPEC_KEY: len(_PROMPT_BYTES),
        },
    )

    completed = subprocess.run(
        [sys.executable, str(_supervisor_script()), "--spec", str(spec_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 126, completed.stderr[-2000:]
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "spawn_failed"
    assert status["spawn_phase"] == "worker_prompt_delivery"
    assert status["error"].startswith(worker_supervisor.WORKER_PROMPT_NOT_DELIVERED)
    assert status["prompt_bytes_expected"] == len(_PROMPT_BYTES)
    assert status["prompt_bytes_delivered"] == 0
    assert "child_pid" not in status
    assert not proof.exists(), "the child must never be spawned without its prompt"


def test_nf01159_supervisor_refuses_a_truncated_delivery_and_never_spawns_the_child(
    tmp_path: Path,
) -> None:
    """A short delivery (0 < received < expected) is refused exactly like an empty one.

    Pins the exact-equality gate at ``supervise``'s prompt-delivery check: the
    all-or-nothing comparison must catch a partial write, not just the total
    loss the zero-byte case above already covers.
    """
    proof = tmp_path / "child-ran.txt"
    child = _script(tmp_path, "forbidden_child.py", _FORBIDDEN_CHILD)
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    status_path = process_dir / "status.json"
    truncated = _PROMPT[: len(_PROMPT) // 2]
    truncated_bytes = truncated.encode("utf-8")
    assert 0 < len(truncated_bytes) < len(_PROMPT_BYTES)
    spec = {
        "argv": [sys.executable, str(child), str(proof)],
        "cwd": str(tmp_path),
        "timeout_seconds": 60,
        "status_path": str(status_path),
        "cancel_path": str(process_dir / "cancel.json"),
        "stdout_path": str(process_dir / "stdout.log"),
        "stderr_path": str(process_dir / "stderr.log"),
        worker_supervisor.WORKER_PROMPT_BYTES_SPEC_KEY: len(_PROMPT_BYTES),
    }

    code = worker_supervisor.supervise(spec, stdin_text=truncated)

    assert code == 126
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "spawn_failed"
    assert status["exit_code"] == 126
    assert status["spawn_phase"] == "worker_prompt_delivery"
    assert status["error"] == (
        f"{worker_supervisor.WORKER_PROMPT_NOT_DELIVERED}:"
        f"expected_bytes={len(_PROMPT_BYTES)}:received_bytes={len(truncated_bytes)}"
    )
    assert status["prompt_bytes_expected"] == len(_PROMPT_BYTES)
    assert status["prompt_bytes_delivered"] == len(truncated_bytes)
    assert "child_pid" not in status
    assert not proof.exists(), "the child must never be spawned on a short delivery"


def test_nf01159_supervisor_receipt_carries_the_delivered_prompt_byte_count(
    tmp_path: Path,
) -> None:
    """The count the supervisor delivered is on the receipt; the prompt is not."""
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    status_path = process_dir / "status.json"
    spec = {
        "argv": [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))",
        ],
        "cwd": str(tmp_path),
        "timeout_seconds": 60,
        "status_path": str(status_path),
        "cancel_path": str(process_dir / "cancel.json"),
        "stdout_path": str(process_dir / "stdout.log"),
        "stderr_path": str(process_dir / "stderr.log"),
        worker_supervisor.WORKER_PROMPT_BYTES_SPEC_KEY: len(_PROMPT_BYTES),
    }

    code = worker_supervisor.supervise(spec, stdin_text=_PROMPT)

    assert code == 0
    stdout_text = Path(str(spec["stdout_path"])).read_text(encoding="utf-8")
    assert stdout_text == str(len(_PROMPT_BYTES))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "exited"
    assert status["prompt_bytes_delivered"] == len(_PROMPT_BYTES)
    assert _PROMPT not in json.dumps(status)


def test_nf01159_a_plan_without_stdin_text_keeps_devnull_and_declares_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unchanged branch: no prompt, no expectation, no pipe.

    An adapter that keeps its prompt in argv must see exactly the behaviour it
    saw before, or this fix would have moved the failure rather than removed it.
    """
    harness = _harness(
        monkeypatch,
        tmp_path,
        child_argv=[sys.executable, "-c", "pass"],
        stdin_text=None,
        real_popen=False,
    )

    result = _launch(harness)

    assert result["ok"] is True, result
    spec = _spec(harness)
    assert spec is not None
    assert runtime_adapters.WORKER_PROMPT_BYTES_SPEC_KEY not in spec
    ((_argv, spawn),) = harness.manager.popen_calls
    assert spawn["stdin"] is subprocess.DEVNULL
    assert harness.manager.stdin_sinks == []
    # And a supervisor handed that spec keeps its own unchanged behaviour.
    assert worker_supervisor._expected_prompt_bytes(spec) == 0


def test_nf01159_no_prompt_text_reaches_the_spec_the_argv_or_any_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a byte count crosses the process boundary in the clear."""
    record = tmp_path / "child-stdin.bin"
    child = _script(tmp_path, "fake_child.py", _FAKE_CHILD)
    harness = _harness(
        monkeypatch,
        tmp_path,
        child_argv=[sys.executable, str(child), str(record)],
        stdin_text=_PROMPT,
        real_popen=False,
    )

    result = _launch(harness)

    assert result["ok"] is True, result
    # The launcher wrote every byte and closed the pipe, so the supervisor's
    # read sees the whole prompt followed by EOF.
    (sink,) = harness.manager.stdin_sinks
    assert bytes(sink.written) == _PROMPT_BYTES
    assert sink.closed is True
    assert all(
        _PROMPT not in json.dumps(payload, default=str)
        for _path, payload in harness.writes
    )
    assert all(_PROMPT not in json.dumps(value, default=str) for value in result.values())
    ((argv, _spawn),) = harness.manager.popen_calls
    assert all(_PROMPT not in token for token in argv)
    spec = _spec(harness)
    assert spec is not None
    assert all(_PROMPT not in str(token) for token in spec["argv"])
