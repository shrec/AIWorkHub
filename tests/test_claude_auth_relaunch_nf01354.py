"""NF-2026-01354: the Claude auth relaunch replays the exact prompt, or spawns nothing.

The defect: when a ``claude_cli`` worker hit an HTTP 401 mid-run,
``ProcessManager._retry_claude_auth_refresh`` refreshed the credential and
relaunched the supervisor with a hand-built spec that carried no
``stdin_text_bytes`` and with ``stdin=DEVNULL`` -- so ``claude -p`` started with
no prompt and exited 1 -- after unlinking the first attempt's status, stdout,
stderr and cancel files, i.e. the 401's own evidence.

These tests drive the real launcher, the real retry and the real supervisor
with a fake ``claude`` and prove: the relaunched child reads the very bytes the
first one read, the replayed spec declares their count, a relaunch that cannot
prove it carries the same prompt spawns nothing, the first attempt's files are
rotated rather than deleted, and the refused relaunch is settled as a retained
spawn failure.  Nothing here is platform-gated.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher,
    runtime_adapters,
    worker_supervisor,
    worker_workspace,
)
from test_claude_cli_prompt_delivery_nf01159 import (  # noqa: E402
    _FAKE_CHILD,
    _FORBIDDEN_CHILD,
    _PROMPT,
    _PROMPT_BYTES,
    _REQUEST_ID,
    _harness,
    _launch,
    _reap,
    _script,
    _spec,
    _supervisor_script,
    _wait_for,
    _write_0600,
)

# The provider-owned JSONL shape ``claude -p --output-format stream-json`` emits
# for a dead credential; the real detector turns it into the retry's input.
_AUTH_401 = {
    "type": "result",
    "is_error": True,
    "terminal_reason": "api_error",
    "api_error_status": 401,
    "error": "authentication_failed",
    "session_id": "session-nf01354",
}

# A fake ``claude``: it records the sha256 of its whole stdin per attempt, and
# its first attempt fails with the 401 above; the second succeeds.
_FLAKY_CLAUDE = f"""\
import hashlib
import pathlib
import sys

record = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
first = not record.with_suffix(".1").exists()
if first:
    print({json.dumps(json.dumps(_AUTH_401))}, flush=True)
record.with_suffix(".1" if first else ".2").write_text(digest, encoding="utf-8")
sys.exit(1 if first else 0)
"""

_FAILURE = {"http_status": 401, "error_code": "authentication_failed", "session_id": "s-1"}


def _refreshable(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int]]:
    """Make the credential refresh succeed and record every chmod the retry asks for."""
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "refresh_subscription_session_for_retry",
        lambda: {"launchable": True},
    )
    monkeypatch.setattr(
        worker_workspace,
        "refresh_claude_credential_projection",
        lambda _home: {"refreshed": True, "destination_sha256": "c" * 64},
    )
    chmods: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        process_launcher, "chmod_path", lambda path, mode: chmods.append((Path(path), mode))
    )
    return chmods


def _retry(harness, metadata: dict, failure: dict) -> dict | None:
    return process_launcher.ProcessManager._retry_claude_auth_refresh(
        harness.manager,
        request_id=_REQUEST_ID,
        metadata_path=harness.process_dir / f"{_REQUEST_ID}.request.json",
        metadata=metadata,
        workspace=harness.workspace,
        provider_launch_failure=failure,
    )


def _metadata(harness) -> dict:
    path = harness.process_dir / f"{_REQUEST_ID}.request.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _spec_writes(harness) -> list[dict]:
    return [
        payload
        for path, payload in harness.writes
        if path.name == f"{_REQUEST_ID}.supervisor-spec.json"
    ]


def _wait_exit(harness) -> None:
    harness.manager._live[_REQUEST_ID].process.wait(timeout=90)


def test_nf01354_auth_relaunch_replays_the_first_attempts_exact_prompt_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """401 once, then success: both children read byte-identical stdin.

    Also pins the replayed spec's byte declaration, the rotated first-attempt
    logs, and the ledger event that names them.
    """
    record = tmp_path / "claude-stdin"
    child = _script(tmp_path, "flaky_claude.py", _FLAKY_CLAUDE)
    harness = _harness(
        monkeypatch,
        tmp_path,
        child_argv=[sys.executable, str(child), str(record)],
        stdin_text=_PROMPT,
    )
    try:
        assert _launch(harness)["ok"] is True
        assert _wait_for(record.with_suffix(".1"))
        _wait_exit(harness)
        metadata = _metadata(harness)
        stdout_path = Path(metadata["stdout_path"])
        failure = process_launcher._provider_auth_failure_from_output(stdout_path)
        assert failure is not None and failure["http_status"] == 401
        chmods = _refreshable(monkeypatch)

        event = _retry(harness, metadata, failure)

        assert event is not None and event["state"] == "running", event
        assert _wait_for(record.with_suffix(".2"))
        _wait_exit(harness)
    finally:
        _reap(harness)

    expected = hashlib.sha256(_PROMPT_BYTES).hexdigest()
    first = record.with_suffix(".1").read_text(encoding="utf-8")
    assert first == expected
    assert record.with_suffix(".2").read_text(encoding="utf-8") == first

    # The relaunch replays the first launch's own spec, byte count included.
    first_spec, retry_spec = _spec_writes(harness)
    assert retry_spec[runtime_adapters.WORKER_PROMPT_BYTES_SPEC_KEY] == len(_PROMPT_BYTES)
    assert retry_spec == first_spec

    # The 401 evidence is rotated beside the request, never deleted.
    rotated = {
        label: Path(metadata[key]).with_name(
            Path(metadata[key]).name.replace(_REQUEST_ID, f"{_REQUEST_ID}.attempt1", 1)
        )
        for label, key in (
            ("stdout", "stdout_path"),
            ("stderr", "stderr_path"),
            ("status", "supervisor_status_path"),
        )
    }
    assert "authentication_failed" in rotated["stdout"].read_text(encoding="utf-8")
    assert json.loads(rotated["status"].read_text(encoding="utf-8"))["exit_code"] == 1
    assert rotated["stderr"].is_file()
    assert "authentication_failed" not in stdout_path.read_text(encoding="utf-8")
    assert {(path, 0o600) for path in rotated.values()} <= set(chmods)
    (ledger,) = [e for e in harness.manager.events if e.get("state") == "finalizing"]
    evidence = ledger["claude_auth_retry"]
    assert evidence["http_status"] == 401
    assert evidence["attempt1_paths"] == {label: str(path) for label, path in rotated.items()}


@pytest.mark.parametrize(
    ("tamper", "cause"),
    [
        ("missing_prompt", "relaunch_input_unreadable:FileNotFoundError"),
        ("altered_prompt", "prompt_sha256_mismatch"),
        ("undeclared_bytes", "relaunch_spec_mismatch"),
    ],
)
def test_nf01354_auth_relaunch_without_the_proven_prompt_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str, cause: str
) -> None:
    harness = _harness(
        monkeypatch,
        tmp_path,
        child_argv=[sys.executable, "-c", "pass"],
        stdin_text=_PROMPT,
        real_popen=False,
    )
    assert _launch(harness)["ok"] is True
    prompt_path = harness.process_dir / f"{_REQUEST_ID}.prompt"
    relaunch_spec_path = harness.process_dir / f"{_REQUEST_ID}.relaunch-spec.json"
    assert prompt_path.read_bytes() == _PROMPT_BYTES
    if tamper == "missing_prompt":
        prompt_path.unlink()
    elif tamper == "altered_prompt":
        prompt_path.write_bytes(_PROMPT_BYTES[:-1] + b"y")
    else:
        relaunch = json.loads(relaunch_spec_path.read_text(encoding="utf-8"))
        del relaunch[runtime_adapters.WORKER_PROMPT_BYTES_SPEC_KEY]
        _write_0600(relaunch_spec_path, relaunch)
    _refreshable(monkeypatch)
    metadata = _metadata(harness)

    refused = _retry(harness, metadata, _FAILURE)

    assert refused is not None
    assert refused["state"] == "spawn_failed"
    assert refused["exit_code"] == 126
    assert refused["error"] == f"worker_prompt_not_delivered:claude_auth_retry:{cause}"
    assert len(harness.manager.popen_calls) == 1, "the relaunch must spawn nothing"
    assert len(_spec_writes(harness)) == 1
    status = json.loads(
        Path(metadata["supervisor_status_path"]).read_text(encoding="utf-8")
    )
    assert status == refused
    retry = _metadata(harness)["claude_auth_retry"]
    assert retry["relaunch_refused"] == refused["error"]
    assert _metadata(harness)["claude_auth_retry_count"] == 1


@pytest.mark.parametrize("adapter_id", sorted(worker_supervisor.PROMPT_ON_STDIN_ADAPTERS))
def test_nf01354_supervisor_refuses_a_prompt_on_stdin_spec_that_declares_no_bytes(
    tmp_path: Path, adapter_id: str
) -> None:
    """The old relaunch spec shape: a prompt-on-stdin CLI with no byte count."""
    proof = tmp_path / "child-ran.txt"
    child = _script(tmp_path, "forbidden_child.py", _FORBIDDEN_CHILD)
    status_path = tmp_path / "status.json"
    spec_path = tmp_path / "spec.json"
    _write_0600(spec_path, {
        "argv": [sys.executable, str(child), str(proof)],
        "cwd": str(tmp_path),
        "timeout_seconds": 60,
        "status_path": str(status_path),
        "cancel_path": str(tmp_path / "cancel.json"),
        "stdout_path": str(tmp_path / "stdout.log"),
        "stderr_path": str(tmp_path / "stderr.log"),
        "adapter_id": adapter_id,
    })

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
    assert status["error"] == (
        f"{worker_supervisor.WORKER_PROMPT_NOT_DELIVERED}:"
        "expected_bytes=undeclared:received_bytes=0"
    )
    assert not proof.exists(), "the child must never be spawned without its prompt"


def test_nf01354_prompt_on_stdin_adapter_set_matches_the_plans_and_the_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The standalone supervisor's copy of the set, and the set itself, cannot drift."""
    assert worker_supervisor.PROMPT_ON_STDIN_ADAPTERS == runtime_adapters.PROMPT_ON_STDIN_ADAPTERS
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    carrying = set()
    for adapter_id in runtime_adapters.SUPPORTED_ADAPTERS:
        try:
            plan = runtime_adapters.build_runtime_command(
                adapter_id,
                "prompt",
                repo,
                model="deepseek/deepseek-chat" if adapter_id == "opencode_cli" else None,
                executable_overrides={adapter_id: str(executable.resolve())},
            )
        except Exception:  # an adapter that cannot build here carries nothing
            continue
        if plan.launchable and runtime_adapters.plan_stdin_payload(plan) is not None:
            carrying.add(adapter_id)
    assert carrying == set(runtime_adapters.PROMPT_ON_STDIN_ADAPTERS)


def test_nf01354_refused_relaunch_finalizes_as_a_retained_spawn_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real finalizer, real retry: the refusal is retained, not a second worker run."""
    request_id = "5" * 32
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    path = tmp_path / "wtroot" / request_id / "worktree"
    home = tmp_path / "wtroot" / request_id / "home"
    path.mkdir(parents=True)
    home.mkdir(parents=True)
    (path / "evidence.txt").write_text("worker output", encoding="utf-8")
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    files = {
        key: process_dir / f"{request_id}.{suffix}"
        for key, suffix in (
            ("metadata_path", "request.json"),
            ("stdout_path", "stdout.log"),
            ("stderr_path", "stderr.log"),
            ("supervisor_status_path", "supervisor.json"),
            ("cancel_path", "cancel.json"),
        )
    }
    files["stdout_path"].write_text(json.dumps(_AUTH_401) + "\n", encoding="utf-8")
    files["stderr_path"].write_text("", encoding="utf-8")
    worker_workspace.write_json_0600(files["supervisor_status_path"], {
        "state": "exited", "exit_code": 1,
    })
    # Relaunch inputs whose prompt no longer matches the recorded hash.
    (process_dir / f"{request_id}.prompt").write_bytes(b"altered")
    worker_workspace.write_json_0600(
        process_dir / f"{request_id}.relaunch-spec.json", {"argv": ["claude", "-p"]}
    )
    task_id, runner, topic = "TASK_NF01354", "claude_worker_nf01354", "task_mcp"
    worker_workspace.write_json_0600(files["metadata_path"], {
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "claude_cli",
        **{key: str(value) for key, value in files.items()},
        "worker_argv": ["claude", "-p"],
        "worker_cwd": str(path),
        "stdin_payload_sha256": hashlib.sha256(b"original").hexdigest(),
        "validation": [],
        "required_outputs": [],
        "sandbox_backend": "landlock",
        "workspace": {
            "request_id": request_id,
            "repo": str(repo),
            "path": str(path),
            "home": str(home),
            "allowed_writes": ["out/result.txt"],
            "parent_baseline": {},
            "workspace_baseline": {},
        },
    })
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        show_task=lambda _tid: {"returncode": 0, "stdout": json.dumps({"task_id": task_id})},
        collision_guard=lambda **_: {
            "returncode": 0, "stdout": '{"collision_free":true}', "stderr": "",
        },
        isolation_enabled=True,
    )
    manager._append_event({
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": 2_147_483_070,
        "pid_start_ticks": 999_999_930,
        **{key: str(value) for key, value in files.items()},
    })
    _refreshable(monkeypatch)
    spawned: list[object] = []
    auth_failures: list[object] = []
    monkeypatch.setattr(manager, "_popen", lambda *a, **k: spawned.append(a) or 1 / 0)
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "record_runtime_auth_failure",
        lambda **k: auth_failures.append(k) or True,
    )
    monkeypatch.setattr(process_launcher, "_requires_bridge_cancellation", lambda *a, **k: False)
    monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_record_usage", lambda *a, **k: ({}, False, ""))
    released: list[str] = []
    for exit_path in ("_review_terminal_exact", "_terminal_failure_exact"):
        monkeypatch.setattr(
            manager, exit_path,
            lambda _metadata, state, **_k: released.append(state) or {"ok": True},
        )
    monkeypatch.setattr(
        process_launcher, "cleanup_workspace",
        lambda *a, **k: pytest.fail("a refused relaunch must retain its workspace"),
    )

    result = manager._finalize_isolated_request(request_id)

    assert spawned == []
    assert auth_failures == []
    assert released == ["worker_failed"]
    assert result is not None
    assert result["state"] == "worker_failed", result
    assert result["terminal_reason"]["code"] == "sandbox_spawn_failed"
    assert result["exit_code"] == 126
    assert result["workspace_retained"] is True
    assert result["workspace_disposition"] == "retained_in_place"
    assert (path / "evidence.txt").exists()
    status = json.loads(files["supervisor_status_path"].read_text(encoding="utf-8"))
    assert status["error"] == (
        "worker_prompt_not_delivered:claude_auth_retry:prompt_sha256_mismatch"
    )
    assert (process_dir / f"{request_id}.attempt1.stdout.log").is_file()
    assert not (process_dir / f"{request_id}.prompt").exists()
    assert not (process_dir / f"{request_id}.relaunch-spec.json").exists()


def test_nf01354_direct_launch_delivers_the_prompt_on_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The non-isolated spawn site used to hand a prompt-on-stdin plan DEVNULL."""
    record = tmp_path / "child-stdin.bin"
    child = _script(tmp_path, "fake_child.py", _FAKE_CHILD)
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = runtime_adapters.RuntimeAdapterPlan(
        adapter_id="claude_cli",
        argv=[sys.executable, str(child), str(record)],
        cwd=str(repo),
        executable=sys.executable,
        launchable=True,
        manual_only=False,
        validation_ok=True,
        validation_reason="",
        reasoning_decision=None,
        context_capacity=None,
        stdin_text=_PROMPT,
    )
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=False,
    )
    for name, value in {
        "launch_gates_open": lambda: True,
        "_validate_adapter_identity": lambda *_a: None,
        "validate_workforce_identity": lambda _r, _a, model, **_k: model,
        "_memory_launch_admission": lambda: {"admit": True},
        "_external_readonly_dirs": lambda *_a: [],
        "build_worker_prompt": lambda **_k: _PROMPT,
        "worker_launch_env": lambda *_a, **_k: dict(os.environ),
    }.items():
        monkeypatch.setattr(process_launcher, name, value)
    monkeypatch.setattr(process_launcher.project_context, "collect_project_context", lambda *_a: None)
    monkeypatch.setattr(manager, "_preflight_card", lambda *_a: {})
    monkeypatch.setattr(manager, "_resolve_provider_env", lambda _a, model: (None, model))
    monkeypatch.setattr(manager, "_build_adapter", lambda **_k: plan)
    monkeypatch.setattr(manager, "_monitor", lambda _live: None)

    result = manager._launch_direct_for_tests(
        task_id="T-nf01354", runner="claude", topic="topic", adapter_id="claude_cli",
    )
    try:
        assert result["ok"] is True, result
        assert _wait_for(record.with_suffix(".eof"))
        assert record.read_bytes() == _PROMPT_BYTES
    finally:
        for live in manager._live.values():
            live.process.wait(timeout=30)
