from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import platform_io, process_launcher, worker_ai_tools_mcp  # noqa: E402


def _card(task_id: str = "TASK_B1", state: str = "pending") -> dict:
    return {
        "task_id": task_id,
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "status": state,
        "worker_status": "review" if state == "review" else "unclaimed",
        "claimed_by": "claude_worker_b1" if state == "review" else "",
        "allowed_writes": ["out/result.json"],
        "priority": "high",
    }


def _show(card_fn):
    def show(task_id: str):
        card = card_fn()
        assert task_id == card["task_id"]
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _collision(**_):
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _plan(argv, repo):
    def build(**_):
        return SimpleNamespace(
            argv=list(argv),
            cwd=str(repo),
            launchable=True,
            reason="",
        )

    return build


def _manager(tmp_path: Path, *, show_task, argv) -> process_launcher.ProcessManager:
    repo = tmp_path / "repo"
    repo.mkdir()
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=show_task,
        collision_guard=_collision,
        adapter_builder=_plan(argv, repo),
        isolation_enabled=False,
    )


def _open_gates(monkeypatch):
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    # Generic launcher tests exercise process lifecycle with an injected
    # adapter command. Keep them independent of whether the CI host has a
    # first-party Claude subscription; auth failure/ready behavior has its own
    # focused tests in test_claude_vscode_lm_preference.py.
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "auth_status",
        lambda: {"launchable": True, "blocker_reason": ""},
    )


def test_request_events_cache_is_request_scoped_and_invalidates_on_append(
    tmp_path,
    monkeypatch,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    first_id = "1" * 32
    second_id = "2" * 32
    manager._append_event({"request_id": first_id, "state": "running"})
    manager._append_event({"request_id": second_id, "state": "running"})

    scans = 0
    original_events = manager._events

    def counted_events():
        nonlocal scans
        scans += 1
        return original_events()

    monkeypatch.setattr(manager, "_events", counted_events)

    assert manager._request_events(first_id)[-1]["state"] == "running"
    assert manager._request_events(first_id)[-1]["state"] == "running"
    assert scans == 1

    # A new request must never reuse another request's empty projection merely
    # because the durable ledger fingerprint is unchanged.
    assert manager._request_events(second_id)[-1]["state"] == "running"
    assert manager._request_events(second_id)[-1]["request_id"] == second_id
    assert manager._request_events(first_id)[-1]["request_id"] == first_id
    assert scans == 2

    manager._append_event({"request_id": first_id, "state": "review_ready"})
    assert manager._request_events(first_id)[-1]["state"] == "review_ready"
    assert scans == 3


def _wait_terminal(manager, request_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.collect(request_id)
        if result.get("terminal"):
            return result
        time.sleep(0.02)
    raise AssertionError("process did not become terminal")


def test_dual_gate_is_closed_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(process_launcher.ALLOW_LAUNCH_ENV, raising=False)
    monkeypatch.delenv(process_launcher.ALLOW_WRITES_ENV, raising=False)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
    )

    assert result["ok"] is False
    assert result["state"] == "blocked"
    assert "dual_gate_closed" in result["blocked_reason"]
    assert manager.list_processes()["active_in_memory"] == 0


def test_launch_contract_rejects_legacy_required_output_prose():
    card = _card()
    card["required_outputs"] = ["A concise report explaining the result"]

    with pytest.raises(
        process_launcher.LaunchRejected,
        match="required_output_not_allowed",
    ):
        process_launcher._validate_required_outputs_contract(card)


def test_quality_review_launch_requires_exact_packet_binding() -> None:
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="quality_review_binding_required",
    ):
        process_launcher._enforce_quality_review_launch_binding(
            "quality_review", None
        )

    with pytest.raises(
        process_launcher.LaunchRejected,
        match="quality_review_binding_topic_mismatch",
    ):
        process_launcher._enforce_quality_review_launch_binding(
            "task_mcp", {"packet": {}}
        )

    process_launcher._enforce_quality_review_launch_binding(
        "quality_review", {"packet": {}}
    )


def test_rework_overlay_materialization_uses_same_task_distinct_request(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "successor" / "worktree"
    home = tmp_path / "successor" / "home"
    repo.mkdir()
    worktree.mkdir(parents=True)
    home.mkdir(parents=True)
    candidate = worktree / "src" / "service.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"def repaired():\n    return True\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    workspace = process_launcher.WorkerWorkspace(
        request_id="5" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=("src/service.py",),
        parent_baseline={"src/service.py": None},
        workspace_baseline={"src/service.py": digest},
        inherited_rework_paths=("src/service.py",),
    )

    path, packet = process_launcher._materialize_worker_rework_overlay(
        workspace,
        task_id="TASK_SAME",
        card={
            "rework_predecessor": {
                "request_id": "6" * 32,
                "changed_path_hashes": {"src/service.py": digest},
            }
        },
    )

    assert path is not None and path.is_file()
    assert packet is not None
    assert packet["successor_task_id"] == "TASK_SAME"
    assert packet["predecessor_task_id"] == "TASK_SAME"
    assert packet["successor_request_id"] == "5" * 32
    assert packet["predecessor_request_id"] == "6" * 32
    assert json.loads(path.read_text(encoding="utf-8")) == packet


def test_crash_retry_packet_reuses_bounded_failure_evidence_without_stale_tree(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    process_dir = repo / ".aiworkhub" / "runtime" / "process_logs" / "processes"
    worktree = tmp_path / "successor" / "worktree"
    home = tmp_path / "successor" / "home"
    repo.mkdir()
    process_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    home.mkdir(parents=True)
    workspace = process_launcher.WorkerWorkspace(
        request_id="5" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=("src/service.py",),
        parent_baseline={"src/service.py": None},
        workspace_baseline={"src/service.py": "a" * 64},
        inherited_rework_paths=("src/service.py",),
    )
    predecessor = "6" * 32
    process_launcher.write_json_0600(
        process_dir / f"{predecessor}.request.json",
        {
            "request_id": predecessor,
            "task_id": "TASK_SAME",
            "workspace": {"repo": str(repo)},
        },
    )
    process_launcher.write_json_0600(
        process_dir / f"{predecessor}.supervisor.json",
        {"state": "supervisor_error", "exit_code": 126, "error": "bridge crashed"},
    )
    (process_dir / f"{predecessor}.stdout.log").write_text(
        "useful-step\n" + ("x" * 9000), encoding="utf-8"
    )
    (process_dir / f"{predecessor}.stderr.log").write_text(
        "exact-provider-error\n", encoding="utf-8"
    )
    process_launcher.attempt_artifacts.persist_json_bundle(
        process_dir / "attempt-artifacts" / predecessor,
        attempt_id=predecessor,
        payloads={
            "metadata": {"request_id": predecessor},
            "diff": {"changed_paths": ["src/service.py"]},
            "validation": {
                "checks": [{
                    "returncode": 1,
                    "argv": ["pytest", "tests/test_service.py"],
                    "stderr_tail": "AssertionError: expected true",
                }]
            },
            "usage": {"usage_observed": False},
            "review": {"target_state": "validation_failed"},
        },
    )
    overlay = {
        "predecessor_request_id": predecessor,
        "predecessor_task_id": "TASK_SAME",
        "canonical_digest": "b" * 64,
    }

    path, packet = process_launcher._materialize_crash_retry_packet(
        process_dir,
        workspace,
        task_id="TASK_SAME",
        card={"rework_predecessor": {"request_id": predecessor}},
        rework_overlay_packet=overlay,
    )

    assert path is not None and path.is_file()
    assert packet is not None
    assert packet["predecessor_state"] == "supervisor_error"
    assert packet["predecessor_exit_code"] == 126
    assert "exact-provider-error" in packet["stderr_tail"]
    assert len(packet["stdout_tail"].encode("utf-8")) <= 4096
    assert packet["rework_overlay_sha256"] == "b" * 64
    assert packet["validation_failure_delta"]["failure_count"] == 1
    assert packet["validation_failure_delta"]["receipts"][0][
        "failure_class"
    ] == "test_failure"
    assert len(packet["validation_manifest_sha256"]) == 64
    assert packet["stale_worktree_bytes_authoritative"] is False
    assert packet["canonical_reread_savings_claimed"] is False
    assert "path" not in packet and "home" not in packet
    prompt = process_launcher.build_worker_prompt(
        task_id="TASK_SAME",
        runner="claude_worker_b1",
        topic="task_mcp",
        card={"task_id": "TASK_SAME", "rework_predecessor": {}},
        crash_retry_packet=packet,
    )
    assert prompt.count("CRASH_RETRY_PACKET_JSON:") == 1
    # Later mutations of the old log cannot alter the request-private packet.
    (process_dir / f"{predecessor}.stderr.log").write_text(
        "stale-later-data", encoding="utf-8"
    )
    assert "stale-later-data" not in path.read_text(encoding="utf-8")


def test_crash_retry_packet_rejects_cross_task_predecessor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    process_dir = tmp_path / "processes"
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    for directory in (repo, process_dir, worktree, home):
        directory.mkdir(parents=True)
    workspace = process_launcher.WorkerWorkspace(
        request_id="5" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
        inherited_rework_paths=("src/service.py",),
    )

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="crash_retry_predecessor_identity_mismatch",
    ):
        process_launcher._materialize_crash_retry_packet(
            process_dir,
            workspace,
            task_id="TASK_SAME",
            card={
                "rework_predecessor": {
                    "request_id": "6" * 32,
                    "task_id": "OTHER_TASK",
                }
            },
            rework_overlay_packet={
                "predecessor_request_id": "6" * 32,
                "predecessor_task_id": "OTHER_TASK",
            },
        )


def test_finalize_after_process_exit_retries_transient_failure(monkeypatch, tmp_path):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "a" * 32
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "state": "running",
        "metadata_path": str(tmp_path / "metadata.json"),
    })
    attempts = []

    def flaky_finalize(request_id, supervisor_returncode=None, *, lock_blocking=True):
        attempts.append((request_id, supervisor_returncode))
        assert lock_blocking is True
        assert manager._request_events(request_id)[-1]["state"] == "running"
        if len(attempts) < 3:
            raise OSError("transient windows finalizer race")
        return {"request_id": request_id, "state": "review_ready"}

    monkeypatch.setattr(manager, "_finalize_isolated_request", flaky_finalize)
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _seconds: None)

    event = manager._finalize_after_process_exit(request_id, 0)

    assert event == {"request_id": request_id, "state": "review_ready"}
    assert len(attempts) == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows lock timeout regression")
def test_duplicate_finalizer_lock_contention_defers_for_owner(
    monkeypatch, tmp_path
):
    owner_manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    duplicate_manager = process_launcher.ProcessManager(
        repo=owner_manager.repo,
        process_log_path=owner_manager.process_log_path,
        process_dir=owner_manager.process_dir,
        isolation_enabled=False,
    )
    request_id = "c" * 32
    owner_manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "state": "running",
        "metadata_path": str(tmp_path / "metadata.json"),
    })
    owner_entered = threading.Event()
    release_owner = threading.Event()
    review_calls: list[str] = []
    owner_errors: list[BaseException] = []

    def run_owner():
        try:
            # Hold the real request lock until the duplicate has exceeded the
            # configured Windows contention timeout. This models validation
            # and evidence work that legitimately takes longer than 20s
            # without making the test wait 20 wall-clock seconds.
            with owner_manager._request_lock(request_id):
                owner_entered.set()
                assert release_owner.wait(timeout=5.0)
                review_calls.append(request_id)
                owner_manager._append_event({
                    "request_id": request_id,
                    "task_id": "TASK_B1",
                    "runner": "claude_worker_b1",
                    "topic": "task_mcp",
                    "state": "review_ready",
                })
        except BaseException as exc:  # surfaced on the main test thread below
            owner_errors.append(exc)

    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_POLL_SECONDS", 0.001)
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lock contention must not terminalize the task")
        ),
    )

    owner = threading.Thread(target=run_owner, name="finalizer-owner")
    owner.start()
    assert owner_entered.wait(timeout=5.0)
    try:
        duplicate = duplicate_manager._finalize_after_process_exit(request_id, 0)
        assert duplicate is not None
        assert duplicate["state"] == "running"
        assert duplicate["reconciliation_deferred"] == "request_lock_busy"
        assert duplicate["workspace_retained"] is True
        assert owner_manager._request_events(request_id)[-1]["state"] == "running"
    finally:
        release_owner.set()
        owner.join(timeout=5.0)

    assert not owner.is_alive()
    assert owner_errors == []
    assert review_calls == [request_id]
    states = [
        event["state"] for event in owner_manager._request_events(request_id)
    ]
    assert states.count("review_ready") == 1
    assert "finalize_failed" not in states


def test_finalize_after_process_exit_emits_terminal_callback_fallback(
    monkeypatch, tmp_path
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "b" * 32
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        # This test covers the generic terminal-callback fallback. Bridge
        # routes have a separate fail-closed cancellation publication gate.
        "adapter_id": "claude_cli",
        "state": "running",
    })
    monkeypatch.setattr(
        manager,
        "_finalize_isolated_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _seconds: None)
    transition_calls = []

    def terminal_failure(repo, task_id, runner, substatus, **kwargs):
        transition_calls.append((repo, task_id, runner, substatus, kwargs))
        return {"ok": True, "callback_enqueued": True, "stderr": ""}

    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        terminal_failure,
    )

    event = manager._finalize_after_process_exit(request_id, 0)

    assert event["state"] == "finalize_failed"
    assert event["finalization_duration_ms"] >= 0
    assert event["release_transition_ok"] is True
    assert event["callback_enqueued"] is True
    assert "finalizer_retries_exhausted" in event["error"]
    assert transition_calls[0][1:4] == (
        "TASK_B1",
        "claude_worker_b1",
        "finalize_failed",
    )


@pytest.mark.parametrize(
    ("terminal_state", "terminal_error"),
    [
        ("finalize_failed", ""),
        ("release_pending", "terminal_failure_transition_failed:sqlite_busy"),
        (
            "validation_failed",
            "validation_exec_scratch_unavailable:C:\\Temp:noexec",
        ),
    ],
)
def test_retry_finalization_reuses_retained_workspace_without_provider(
    monkeypatch, tmp_path, terminal_state, terminal_error
):
    _open_gates(monkeypatch)
    from aiworkhub import worker_workspace

    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="review")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "d" * 32
    worktree_root = tmp_path / "worktrees"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(worktree_root))
    workspace_path = worktree_root / request_id / "worktree"
    home_path = worktree_root / request_id / "home"
    workspace_path.parent.mkdir(parents=True)
    workspace_path.mkdir()
    home_path.mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=manager.repo,
        path=workspace_path,
        home=home_path,
        allowed_writes=("out/result.json",),
        parent_baseline={},
        workspace_baseline={},
    )
    status_path = manager.process_dir / f"{request_id}.supervisor.json"
    metadata_path = manager.process_dir / f"{request_id}.request.json"
    worker_workspace.write_json_0600(
        status_path, {"state": "exited", "exit_code": 0}
    )
    worker_workspace.write_json_0600(
        metadata_path,
        {
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "vscode_lm",
            "sandbox_backend": "vscode_lm_in_process",
            "supervisor_status_path": str(status_path),
            "workspace": workspace.as_metadata(),
        },
    )
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "vscode_lm",
        "state": terminal_state,
        "error": terminal_error,
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "workspace_retained": True,
    })
    transitions = []
    monkeypatch.setattr(
        process_launcher.task_engine,
        "retry_finalize_failed",
        lambda *args, **kwargs: transitions.append((args, kwargs)) or {
            "ok": True,
            "stderr": "",
        },
    )

    def finalize(request_id_arg, supervisor_returncode=None):
        latest = manager._request_events(request_id_arg)[-1]
        assert latest["state"] == "finalizing"
        assert latest["finalization_retry"] is True
        assert latest["finalization_retry_provider_launched"] is False
        assert supervisor_returncode == 0
        return {
            "request_id": request_id_arg,
            "task_id": "TASK_B1",
            "state": "review_ready",
            "workspace_retained": True,
            "error": "",
        }

    monkeypatch.setattr(manager, "_finalize_isolated_request", finalize)

    result = manager.retry_finalization(request_id, "TASK_B1")

    assert result["ok"] is True, result
    assert result["state"] == "review_ready"
    assert result["provider_relaunched"] is False
    if terminal_state == "release_pending":
        assert transitions == []
    else:
        assert transitions and transitions[0][0][1:4] == (
            "TASK_B1",
            "claude_worker_b1",
            request_id,
        )


def test_retry_finalization_rejects_product_validation_failure(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="blocked")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "e" * 32
    manager._append_event(
        {
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "state": "validation_failed",
            "error": "validation_failed:python3 -m pytest:rc=1",
            "workspace_retained": True,
        }
    )

    result = manager.retry_finalization(request_id, "TASK_B1")

    assert result["ok"] is False
    assert result["error"] == (
        "request_not_retryable_finalization_failure:validation_failed"
    )


def test_environment_blocked_validation_is_never_reported_as_validation_failed():
    """NF-2026-00271: a recoverable environment/sandbox restriction must route
    to the retryable ``finalize_failed`` bucket, never to the acceptance-blocking
    ``validation_failed`` (the candidate did not fail its gate)."""
    from aiworkhub import worker_workspace

    env_blocked = worker_workspace.ValidationEnvironmentBlocked(
        "validation_environment_blocked:missing_package:pytest:"
        "restrictions=missing_package:stderr=",
        [],
        restriction="missing_package",
        restrictions=("missing_package",),
    )
    assert (
        process_launcher._terminal_state_for_workspace_error(env_blocked)
        == "finalize_failed"
    )

    genuine = worker_workspace.ValidationRunError(
        "validation_failed:pytest:rc=1:stdout=:stderr=", []
    )
    assert (
        process_launcher._terminal_state_for_workspace_error(genuine)
        == "validation_failed"
    )

    # Environment/sandbox restrictions must never become ``validation_failed``:
    # an absent executable, an unavailable pytest runtime, an unprovisionable
    # exec scratch, and a missing validator package are all recoverable.
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("validation_executable_unavailable:pytest")
    ) == "finalize_failed"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("validation_pytest_runtime_missing_pytest:/x")
    ) == "finalize_failed"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError(
            "validation_exec_scratch_unavailable:request-home:noexec"
        )
    ) == "finalize_failed"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("unsupported_sandbox_backend:bogus")
    ) == "finalize_failed"

    # Genuine candidate gate failures keep the acceptance-blocking state.
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("required_output_missing:out/result.json")
    ) == "validation_failed"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("quality_gate_failed:coverage")
    ) == "validation_failed"

    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("scope_violation:out/result.json")
    ) == "scope_rejected"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("promotion_scope:out")
    ) == "promotion_conflict"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("some_unexpected_error")
    ) == "finalize_failed"


def test_candidate_contract_validation_tokens_are_acceptance_blocking():
    """NF-2026-00271 (rework): deterministic candidate/card validation defects
    must stay acceptance-blocking ``validation_failed``. Only genuine
    environment/sandbox restrictions may reach the retryable ``finalize_failed``
    bucket; a catch-all there would let a provider-free ``retry_finalization``
    loop re-run a defect the candidate itself authored."""
    candidate_contract_tokens = (
        "validation_route_adapter_missing",
        "validation_commands_invalid",
        "validation_command_invalid",
        "validation_receipt_count_mismatch",
        "validation_failure_delta_too_large",
        "validation_command_limit_exceeded:9",
        "validation_cwd_not_directory:sub",
        "validation_pythonpath_not_directory:src",
        # Additional deterministic defects from the same declared-command
        # parsing and contract-shape surface.
        "validation_route_backend_mismatch:expected=landlock:recorded=bwrap",
        "validation_shell_syntax_forbidden:cd sub && pytest|tee",
        "validation_pythonpath_empty",
        "validation_cd_prefix_malformed",
        "invalid_validation_command",
    )
    for token in candidate_contract_tokens:
        assert (
            process_launcher._terminal_state_for_workspace_error(
                process_launcher.WorkspaceError(token)
            )
            == "validation_failed"
        ), token

    # The narrow environment/sandbox allowlist is unchanged: the same classifier
    # still routes a missing executable / runtime / scratch / backend to the
    # retryable bucket, never to ``validation_failed``.
    for token in (
        "validation_executable_unavailable:pytest",
        "validation_pytest_runtime_unavailable:/x",
        "validation_pytest_runtime_missing_pytest:/x",
        "validation_exec_scratch_unavailable:request-home:noexec",
        "unsupported_sandbox_backend:bogus",
        "validation_unsupported_in_sandbox:secure_sandbox_unavailable",
    ):
        assert (
            process_launcher._terminal_state_for_workspace_error(
                process_launcher.WorkspaceError(token)
            )
            == "finalize_failed"
        ), token


def test_validation_security_refusals_are_acceptance_blocking():
    """NF-2026-00271 (rework): security refusals and candidate validation
    defects must stay acceptance-blocking ``validation_failed``. The exact-token
    allowlist -- not a broad ``validation_executable_`` /
    ``validation_pytest_runtime_`` family prefix -- is the only route to the
    recoverable ``finalize_failed`` bucket, so a world-writable or
    untrusted-owner validator binary/runtime-root, a symlink-forbidden pytest
    runtime, or an unapproved/non-executable validator fails closed instead of
    failing open."""
    security_refusal_tokens = (
        "validation_executable_world_writable:/x",
        "validation_executable_untrusted_owner:/x",
        "validation_executable_runtime_root_world_writable:/x",
        "validation_executable_runtime_root_untrusted_owner:/x",
        "validation_executable_untrusted_runtime_root:/x",
        "validation_executable_not_approved:pylint",
        "validation_executable_not_executable:/x",
        "validation_pytest_runtime_world_writable:/x",
        "validation_pytest_runtime_untrusted_owner:/x",
        "validation_pytest_runtime_symlink_forbidden:/x",
    )
    for token in security_refusal_tokens:
        assert (
            process_launcher._terminal_state_for_workspace_error(
                process_launcher.WorkspaceError(token)
            )
            == "validation_failed"
        ), token


def test_validation_recoverable_environment_tokens_are_exact():
    """NF-2026-00271 (rework): only the six colon-terminated recoverable tokens
    route to the retryable ``finalize_failed`` bucket. A broad family prefix
    would fail-open and reclassify a security refusal as recoverable."""
    recoverable_tokens = (
        "validation_executable_unavailable:pytest",
        "validation_pytest_runtime_unavailable:/x",
        "validation_pytest_runtime_missing_pytest:/x",
        "validation_exec_scratch_unavailable:request-home:noexec",
        "unsupported_sandbox_backend:bogus",
        "validation_unsupported_in_sandbox:secure_sandbox_unavailable",
    )
    for token in recoverable_tokens:
        assert (
            process_launcher._terminal_state_for_workspace_error(
                process_launcher.WorkspaceError(token)
            )
            == "finalize_failed"
        ), token


def test_run_declared_validations_preserves_environment_blocked_subtype(monkeypatch):
    """The finalizer seam must keep ``ValidationEnvironmentBlocked`` (with its
    ``terminal_state``/``restriction``/``recoverable`` flags) so routing can
    tell an environment block apart from a genuine candidate failure."""
    from aiworkhub import worker_workspace

    raised = worker_workspace.ValidationEnvironmentBlocked(
        "validation_environment_blocked:missing_package:pytest:"
        "restrictions=missing_package:stderr=",
        [{
            "command": "pytest",
            "returncode": 1,
            "failure_receipt": {"failure_class": "absent_validator_module"},
        }],
        restriction="missing_package",
        restrictions=("missing_package",),
    )

    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "normalize_behavioral_contract",
        lambda work_kind, commands, roles: ("code", ["gate"]),
    )
    monkeypatch.setattr(process_launcher, "_validation_route_kwargs", lambda meta: {})

    def _raise(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(process_launcher, "run_validations", _raise)

    with pytest.raises(worker_workspace.ValidationEnvironmentBlocked) as caught:
        process_launcher._run_declared_validations(
            object(),
            {"validation": ["pytest"], "work_kind": "code"},
            {"adapter_id": "claude_cli"},
        )

    exc = caught.value
    assert isinstance(exc, worker_workspace.ValidationEnvironmentBlocked)
    assert exc.terminal_state == "validation_environment_blocked"
    assert exc.recoverable is True
    assert exc.requires_supersede is False
    assert exc.restriction == "missing_package"
    assert exc.restrictions == ("missing_package",)
    assert exc.results[0]["behavioral_role"] == "gate"
    assert exc.results[0]["failure_receipt"]["failure_class"] == "absent_validator_module"


def test_run_declared_validations_keeps_genuine_failure_as_validation_run_error(monkeypatch):
    """A real gate failure must stay ``ValidationRunError``/``validation_failed``
    through the finalizer seam -- it is never weakened into an environment
    block (NF-WAVE-SANDBOX-TRUTH)."""
    from aiworkhub import worker_workspace

    raised = worker_workspace.ValidationRunError(
        "validation_failed:pytest:rc=1:stdout=:stderr=",
        [{"command": "pytest", "returncode": 1}],
    )

    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "normalize_behavioral_contract",
        lambda work_kind, commands, roles: ("code", ["gate"]),
    )
    monkeypatch.setattr(process_launcher, "_validation_route_kwargs", lambda meta: {})

    def _raise(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(process_launcher, "run_validations", _raise)

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        process_launcher._run_declared_validations(
            object(),
            {"validation": ["pytest"], "work_kind": "code"},
            {"adapter_id": "claude_cli"},
        )

    exc = caught.value
    assert not isinstance(exc, worker_workspace.ValidationEnvironmentBlocked)
    assert exc.terminal_state == "validation_failed"
    assert exc.requires_supersede is True
    assert exc.results[0]["behavioral_role"] == "gate"


def test_reconcile_defers_live_windows_pid_without_start_ticks(monkeypatch, tmp_path):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "c" * 32
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "state": "running",
        "pid": 4242,
        "pid_start_ticks": None,
        "metadata_path": str(tmp_path / "metadata.json"),
    })
    watched = []
    monkeypatch.setattr(manager, "_watch_persisted_request", lambda *args: watched.append(args))
    monkeypatch.setattr(
        manager,
        "_finalize_after_process_exit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("live pid finalized")),
    )

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(process_launcher.threading, "Thread", ImmediateThread)

    result = manager._reconcile_persisted_requests()

    assert result == {"watched": 0, "finalized": 0}
    assert watched == []


@pytest.mark.parametrize(
    ("runner", "topic", "reason"),
    [
        ("wrong_runner_b1", "task_mcp", "runner_mismatch"),
        ("claude_worker_b1", "wrong_topic", "topic_mismatch"),
    ],
)
def test_exact_identity_is_required(monkeypatch, tmp_path, runner, topic, reason):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    result = manager.launch(
        task_id="TASK_B1",
        runner=runner,
        topic=topic,
        adapter_id="claude_cli",
    )
    assert result["ok"] is False
    assert reason in result["blocked_reason"]


def test_runner_adapter_family_must_match(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="codex_cli",
        timeout_seconds=30,
    )
    assert result["ok"] is False
    assert "runner_adapter_mismatch" in result["blocked_reason"]


def test_coordinator_runner_is_never_accepted_as_worker_identity() -> None:
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="coordinator_runner_cannot_launch_worker",
    ):
        process_launcher._validate_adapter_identity("codex", "glm_vscode_lm")


def test_real_shell_free_process_reaches_review_ready(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    marker = tmp_path / "review.marker"

    def current_card():
        return _card(state="review" if marker.exists() else "pending")

    manager = _manager(
        tmp_path,
        show_task=_show(current_card),
        argv=[
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ok'); print('worker complete')",
        ],
    )
    launched = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert launched["ok"] is True
    assert launched["shell"] is False

    result = _wait_terminal(manager, launched["request_id"])
    assert result["state"] == "review_ready"
    assert result["review_ready"] is True
    assert result["exit_code"] == 0
    assert "worker complete" in result["stdout_tail"]


def test_success_without_review_is_explicit_failure_state(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "print('claimed only')"],
    )
    launched = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    result = _wait_terminal(manager, launched["request_id"])
    assert result["state"] == "exited_without_review"
    assert result["review_ready"] is False


def test_spawn_failure_closes_the_same_audit_request(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    def fail_spawn(*_args, **_kwargs):
        raise OSError("fixture spawn failure")

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(lambda: _card()),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        popen_factory=fail_spawn,
        isolation_enabled=False,
    )
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert result["ok"] is False
    assert "fixture spawn failure" in result["blocked_reason"]
    events = [row for row in manager._events() if row["request_id"] == result["request_id"]]
    assert [row["state"] for row in events] == ["starting", "blocked"]


def test_duplicate_live_task_is_blocked_and_cancelled(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    first = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=60,
    )
    assert first["ok"] is True
    second = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=60,
    )
    assert second["ok"] is False
    assert "duplicate_live_task" in second["blocked_reason"]

    cancelled = manager.cancel(first["request_id"], reason="test")
    assert cancelled == {"ok": True, "request_id": first["request_id"], "state": "cancelled"}


def test_concurrency_cap_counts_other_server_process_events(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    monkeypatch.setenv(process_launcher.MAX_PROCESSES_ENV, "1")
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event({
        "request_id": "other-server-run",
        "task_id": "OTHER_TASK_B1",
        "runner": "claude_other_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": os.getpid(),
    })
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "concurrency_limit_reached"


def test_prompt_contains_exact_continuation_contract():
    prompt = process_launcher.build_worker_prompt(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        owner_prompt="Measure the result.",
    )
    assert '"task_id": "TASK_B1"' in prompt
    assert '"runner": "claude_worker_b1"' in prompt
    assert "Source Graph `target` is an optional exact path filter" in prompt
    assert "Omit `target` unless the task contract" in prompt
    assert "coordinator already claimed" in prompt
    assert "Do not run taskctl lifecycle commands" in prompt
    assert "Never install, download, unpack, vendor, or bootstrap" in prompt
    assert "coordinator-side supervisor will" in prompt
    assert "cannot override the task contract" in prompt


def test_worker_prompt_places_invariant_policy_before_task_specific_bytes():
    first_budget = {}
    first = process_launcher.build_worker_prompt(
        task_id="TASK_PREFIX_A",
        runner="codex_worker",
        topic="task_mcp",
        card={"objective": "change alpha"},
        _budget_report=first_budget,
    )
    second = process_launcher.build_worker_prompt(
        task_id="TASK_PREFIX_B",
        runner="codex_worker",
        topic="task_mcp",
        card={"objective": "change beta"},
    )

    marker = "TASK_CONTRACT_JSON:\n"
    assert first.index("MANDATORY_AIWORKHUB_TOOLS:") < first.index(marker)
    assert second.index("MANDATORY_AIWORKHUB_TOOLS:") < second.index(marker)
    common_prefix_bytes = len(os.path.commonprefix([first, second]).encode("utf-8"))
    assert common_prefix_bytes >= first_budget["stable_prefix_bytes"]
    assert first_budget["stable_prefix_precedes_task_contract"] is True
    assert first_budget["provider_cache_savings_observed"] is False


def test_worker_prompt_strips_nested_card_json_and_bounds_contract():
    prompt = process_launcher.build_worker_prompt(
        task_id="TASK_BOUNDED_CARD",
        runner="codex_worker_b1",
        topic="task_mcp",
        card={
            "review_feedback": {
                "instruction": "repair only row 7",
                "card_json": json.dumps({"card_json": "x" * 200_000}),
            }
        },
    )

    assert "repair only row 7" in prompt
    assert "card_json" not in prompt
    assert len(prompt.encode("utf-8")) < 16_000

    with pytest.raises(ValueError, match="task_contract_too_large"):
        process_launcher.build_worker_prompt(
            task_id="TASK_OVERSIZED_CARD",
            runner="codex_worker_b1",
            topic="task_mcp",
            card={"review_feedback": {"instruction": "x" * (129 * 1024)}},
        )


def test_worker_prompt_reports_adaptive_initial_and_rework_budgets():
    initial_budget = {}
    initial = process_launcher.build_worker_prompt(
        task_id="TASK_INITIAL",
        runner="codex_worker_b1",
        topic="task_mcp",
        card={"objective": "bounded implementation"},
        project_context_bundle="PROJECT_CONTEXT_BUNDLE:\n{}",
        _budget_report=initial_budget,
    )
    assert initial_budget["mode"] == "initial"
    assert initial_budget["total_bytes"] == len(initial.encode("utf-8"))
    assert initial_budget["max_bytes"] == process_launcher.MAX_WORKER_PROMPT_BYTES
    assert initial_budget["sections"]["project_context_bytes"] > 0
    assert initial_budget["byte_labels_are_token_truth"] is False

    rework_budget = {}
    rework = process_launcher.build_worker_prompt(
        task_id="TASK_REWORK",
        runner="codex_worker_b1",
        topic="task_mcp",
        card={
            "objective": "repair residual",
            "review_feedback": {
                "schema_id": "aiworkhub.rework_feedback_delta.v1",
                "instruction": "repair row 7 only",
                "residual_identities": [{"path": "out.json", "pointer": "/rows/7"}],
            },
        },
        _budget_report=rework_budget,
    )
    assert "repair row 7 only" in rework
    assert rework_budget["mode"] == "rework_delta"
    assert rework_budget["delta_rework"] is True
    assert rework_budget["max_bytes"] == process_launcher.MAX_REWORK_WORKER_PROMPT_BYTES


def test_external_readonly_sources_are_bounded_and_collapsed(monkeypatch, tmp_path):
    root = tmp_path / "external"
    release = root / "release"
    buckets = release / "buckets"
    buckets.mkdir(parents=True)
    report = release / "report.json"
    manifest = release / "source_manifest.jsonl"
    report.write_text("{}", encoding="utf-8")
    manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(process_launcher, "EXTERNAL_READONLY_ROOTS", (root,))

    card = {
        "external_readonly_sources": [str(report), str(manifest), str(buckets)]
    }
    assert process_launcher._external_readonly_dirs(
        card, "deepseek_copilot_cli"
    ) == [str(release.resolve())]


def test_external_readonly_sources_fail_closed_on_escape(monkeypatch, tmp_path):
    root = tmp_path / "external"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(process_launcher, "EXTERNAL_READONLY_ROOTS", (root,))

    with pytest.raises(process_launcher.LaunchRejected, match="outside_roots"):
        process_launcher._external_readonly_dirs(
            {"external_readonly_sources": [str(escape)]},
            "deepseek_copilot_cli",
        )
    with pytest.raises(process_launcher.LaunchRejected, match="requires_deepseek"):
        process_launcher._external_readonly_dirs(
            {"external_readonly_sources": [str(root)]}, "claude_cli"
        )


def test_deepseek_adapter_adds_only_declared_read_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        process_launcher.runtime_adapters, "_is_windows_host", lambda: False
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    executable = tmp_path / "copilot"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        process_launcher.runtime_adapters.shutil,
        "which",
        lambda _name: str(executable),
    )

    plan = process_launcher.runtime_adapters.build_runtime_command(
        "deepseek_copilot_cli",
        "work",
        repo,
        additional_readonly_dirs=[external],
    )
    assert plan.launchable is True
    assert plan.argv[plan.argv.index("--add-dir") + 1] == str(external.resolve())
    assert "--allow-all-paths" not in plan.argv
    assert "--allow-all" not in plan.argv


def test_direct_launch_child_env_excludes_write_gate_launch_and_coordinator_secrets(
    monkeypatch, tmp_path
):
    """B314_F001/F003 regression: the non-isolated (isolation_enabled=False)
    launch path used to build the child env with plain os.environ.copy(),
    which inherited every parent secret including AIWORKHUB_ALLOW_WRITES
    (a write-gate bypass) and the taskctl coordinator token/token-file.
    sanitized_env() now builds an explicit minimal allowlist instead, so none
    of these leak into the spawned process regardless of what happens to be
    set in the MCP server's own environment.
    """
    _open_gates(monkeypatch)
    monkeypatch.setenv(process_launcher.MAX_PROCESSES_ENV, "4")
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "super-secret-capability")
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", "/tmp/does-not-matter")
    monkeypatch.setenv("SOME_UNRELATED_SECRET_TOKEN", "leak-me-if-buggy")

    dump_path = tmp_path / "child_env.json"
    script = (
        "import json, os; "
        f"json.dump(dict(os.environ), open({str(dump_path)!r}, 'w'))"
    )
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", script],
    )
    launched = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert launched["ok"] is True
    _wait_terminal(manager, launched["request_id"])

    child_env = json.loads(dump_path.read_text(encoding="utf-8"))
    for leaked_key in (
        process_launcher.ALLOW_LAUNCH_ENV,
        process_launcher.ALLOW_WRITES_ENV,
        process_launcher.MAX_PROCESSES_ENV,
        "BITNN_TASKCTL_COORDINATOR_TOKEN",
        "BITNN_TASKCTL_COORDINATOR_TOKEN_FILE",
        "SOME_UNRELATED_SECRET_TOKEN",
    ):
        assert leaked_key not in child_env, f"{leaked_key} leaked into child env"
    # The happy path still works: the launcher-owned override is present.
    assert child_env["AIWORKHUB_REPO"] == str((tmp_path / "repo").resolve())


def test_direct_launch_duplicate_check_uses_pid_start_ticks_not_bare_liveness(
    monkeypatch, tmp_path
):
    """B314_F009 regression: the persisted-event duplicate-task check on the
    direct (non-isolated) launch path used _pid_alive() alone, so a PID
    recycled by an unrelated but genuinely-alive process would falsely block
    a legitimate launch. It must use _pid_matches() (PID + /proc start-tick),
    exactly like every other liveness check in this module.
    """
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    real_ticks = process_launcher._pid_start_ticks(os.getpid())
    assert real_ticks is not None

    # A stale record: this PID is alive (it's the test process itself) but
    # the recorded start-tick does not match it -- the process that owned
    # this request_id has actually exited and the PID was recycled.
    manager._append_event({
        "request_id": "stale-recycled-pid",
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": os.getpid(),
        "pid_start_ticks": real_ticks + 999_999,
    })
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert result["ok"] is True, result
    manager.cancel(result["request_id"], reason="test-cleanup")

    # Sanity check the other direction: when the start-tick genuinely
    # matches the live PID, the duplicate guard still fires. Uses a separate
    # tmp subdir so its repo/process_log/process_dir don't collide with the
    # first manager created above in this same test.
    second = tmp_path / "second"
    second.mkdir()
    manager2 = _manager(
        second,
        show_task=_show(lambda: _card(task_id="TASK_B2")),
        argv=[sys.executable, "-c", "pass"],
    )
    manager2._append_event({
        "request_id": "genuinely-still-running",
        "task_id": "TASK_B2",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": os.getpid(),
        "pid_start_ticks": real_ticks,
    })
    blocked = manager2.launch(
        task_id="TASK_B2",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert blocked["ok"] is False
    assert "duplicate_persisted_task" in blocked["blocked_reason"]


def test_safe_tail_refuses_to_follow_a_symlinked_log_path(tmp_path):
    """B314_F008 regression: _safe_tail must not dereference a symlink that
    has replaced the expected log path -- open with O_NOFOLLOW and return an
    empty tail rather than the linked-to file's content."""
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("do-not-leak-this-content", encoding="utf-8")
    link = tmp_path / "request.stdout.log"
    link.symlink_to(sensitive)

    assert process_launcher._safe_tail(link) == ""

    regular = tmp_path / "regular.stdout.log"
    regular.write_text("normal worker output\n", encoding="utf-8")
    assert process_launcher._safe_tail(regular) == regular.read_bytes().decode("utf-8")


def test_successful_isolated_reconcile_enters_review_without_promoting(
    monkeypatch, tmp_path
):
    """Phase 1 review-first reconcile regression: a successful worker exit
    must never call ``promote()`` or ``core.mark_review`` directly. It must
    retain the isolated workspace, leave the canonical repo byte-unchanged,
    and hand the coordinator's review ledger every check's evidence
    (validation, required outputs, the worker-MCP gate, changed paths + their
    hashes, and the exact workspace/request identity) via
    ``_review_terminal_exact`` with substatus ``review_ready``.
    """
    if os.name == "nt":
        pytest.skip("review finalization requires the POSIX secure sandbox backend")
    _open_gates(monkeypatch)
    from aiworkhub import worker_workspace, task_engine

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out").mkdir(parents=True)
    canonical_file = repo / "out" / "result.json"
    canonical_file.write_text("canonical-v1", encoding="utf-8")
    canonical_before = canonical_file.read_bytes()

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "out").mkdir(parents=True)
    worked_file = workspace_dir / "out" / "result.json"
    worked_file.write_text("canonical-v1", encoding="utf-8")

    import subprocess

    def _git(*args):
        return subprocess.run(
            ["git", *args], cwd=workspace_dir, text=True, capture_output=True, check=True
        )

    _git("init", "-q")
    _git("config", "user.email", "tests@example.invalid")
    _git("config", "user.name", "Task MCP Tests")
    _git("add", "out/result.json")
    _git("commit", "-qm", "baseline")
    worked_file.write_text("worker-output-v2", encoding="utf-8")  # uncommitted change

    home_dir = tmp_path / "home"
    home_dir.mkdir()

    workspace = worker_workspace.WorkerWorkspace(
        request_id="req-review-first-1",
        repo=repo,
        path=workspace_dir,
        home=home_dir,
        allowed_writes=("out/result.json",),
        parent_baseline={},
        workspace_baseline={},
    )

    def _processing_card():
        card = _card()
        card["status"] = "processing"
        card["worker_status"] = "claimed"
        card["claimed_by"] = "claude_worker_b1"
        return card

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(_processing_card),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        isolation_enabled=False,
    )

    stdout_path = tmp_path / "req-review-first-1.stdout.log"
    stderr_path = tmp_path / "req-review-first-1.stderr.log"
    stdout_path.write_text("worker complete\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    status_path = tmp_path / "req-review-first-1.supervisor.json"
    metadata_path = tmp_path / "req-review-first-1.request.json"

    worker_workspace.write_json_0600(
        status_path,
        {"state": "exited", "exit_code": 0},
    )
    metadata = {
        "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
        "request_id": "req-review-first-1",
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(tmp_path / "req-review-first-1.cancel.json"),
        "prompt_sha256": "0" * 64,
        "project_context": None,
        "project_context_delivery": {"injected": False},
        "sandbox_backend": "landlock",
        "validation": [],
        "required_outputs": [],
        "allow_empty_required_outputs": [],
        "allow_unchanged_required_outputs": [],
        "external_readonly_dirs": [],
        "workspace": workspace.as_metadata(),
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    review_calls = []

    def fake_review_terminal_exact(metadata_arg, substatus, *, request_id, error="", evidence=None):
        review_calls.append(
            {
                "repo": repo,
                "task_id": metadata_arg["task_id"],
                "runner": metadata_arg["runner"],
                "substatus": substatus,
                "evidence": evidence or {},
            }
        )
        return {"ok": True, "returncode": 0, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(manager, "_review_terminal_exact", fake_review_terminal_exact)

    promote_calls = []
    monkeypatch.setattr(
        process_launcher,
        "promote",
        lambda *a, **k: promote_calls.append((a, k)) or [],
        raising=False,
    )
    mark_review_calls = []
    monkeypatch.setattr(
        process_launcher.core,
        "mark_review",
        lambda *a, **k: mark_review_calls.append((a, k)) or {"ok": True},
    )
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty validation must not resolve a sandbox route")
        ),
    )

    manager._append_event({
        "request_id": "req-review-first-1",
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "state": "running",
        "pid": 999_999_999,
        "pid_start_ticks": 1,
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    })

    event = manager._finalize_isolated_request("req-review-first-1", supervisor_returncode=0)

    assert event["state"] == "review_ready"
    assert event["workspace_retained"] is True
    assert event["promoted_paths"] == []
    assert event["finalization_duration_ms"] >= 0
    phase_durations = event["finalization_phase_durations_ms"]
    assert 0.0 <= phase_durations["validation"] < 5.0
    assert phase_durations["workspace_scope"] >= 0.0
    assert phase_durations["evidence_and_transition"] >= 0.0
    assert sum(phase_durations.values()) <= event["finalization_duration_ms"] + 1.0
    assert "out/result.json" in event["changed_paths"]

    # No promotion or direct mark_review call ever happened.
    assert promote_calls == []
    assert mark_review_calls == []

    # The canonical repo is byte-unchanged; the isolated workspace is intact.
    assert canonical_file.read_bytes() == canonical_before
    assert worked_file.read_text(encoding="utf-8") == "worker-output-v2"
    assert workspace_dir.is_dir()

    # The coordinator's review ledger received review_ready plus full evidence.
    assert len(review_calls) == 1
    call = review_calls[0]
    assert call["substatus"] == "review_ready"
    assert call["task_id"] == "TASK_B1"
    assert call["runner"] == "claude_worker_b1"
    evidence = call["evidence"]
    assert "out/result.json" in evidence["changed_paths"]
    assert evidence["changed_path_hashes"]["out/result.json"] == hashlib.sha256(
        b"worker-output-v2"
    ).hexdigest()
    assert evidence["validation"] == []
    assert evidence["worker_mcp_gate"]["gated"] is False
    assert evidence["request_identity"] == {
        "request_id": "req-review-first-1",
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
    }
    assert evidence["workspace"]["request_id"] == "req-review-first-1"
    assert evidence["workspace"]["path"] == str(workspace_dir)
    artifact_receipt = evidence["attempt_artifact_manifest"]
    assert artifact_receipt["verified"] is True
    assert event["attempt_artifact_manifest"] == artifact_receipt
    verification = process_launcher.attempt_artifacts.verify_json_bundle(
        Path(artifact_receipt["manifest_path"]).parent
    )
    assert verification["attempt_id"] == "req-review-first-1"
    assert verification["roles"] == [
        "diff",
        "metadata",
        "review",
        "usage",
        "validation",
    ]
    evidence_record = process_launcher.evidence_levels.validate_evidence_record(
        evidence["evidence_record"]
    )
    assert (
        evidence_record.evidence_level
        == process_launcher.evidence_levels.EvidenceLevel.STATIC_EVIDENCE
    )


def test_quality_reviewer_finalization_seals_attempt_bundle(
    monkeypatch, tmp_path
):
    if os.name == "nt":
        pytest.skip("review finalization requires the POSIX secure sandbox backend")
    _open_gates(monkeypatch)
    from aiworkhub import worker_workspace

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_dir = tmp_path / "review-workspace"
    workspace_dir.mkdir()
    marker = workspace_dir / "README.md"
    marker.write_text("review target\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=workspace_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=workspace_dir,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Task MCP Tests"],
        cwd=workspace_dir,
        check=True,
    )
    subprocess.run(["git", "add", "README.md"], cwd=workspace_dir, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "baseline"], cwd=workspace_dir, check=True
    )
    home = tmp_path / "review-home"
    home.mkdir()
    request_id = "req-quality-artifacts-1"
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=workspace_dir,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )

    def processing_card():
        card = _card()
        card.update({
            "status": "processing",
            "worker_status": "claimed",
            "claimed_by": "claude_worker_b1",
            "topic": "task_mcp",
        })
        return card

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(processing_card),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        isolation_enabled=False,
    )
    stdout_path = tmp_path / f"{request_id}.stdout.log"
    stderr_path = tmp_path / f"{request_id}.stderr.log"
    status_path = tmp_path / f"{request_id}.supervisor.json"
    metadata_path = tmp_path / f"{request_id}.request.json"
    stdout_path.write_text("review complete\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    worker_workspace.write_json_0600(
        status_path, {"state": "exited", "exit_code": 0}
    )
    metadata = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude-sonnet",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "sandbox_backend": "landlock",
        "quality_review": {
            "target_request_id": "target-request",
            "target_task_id": "TARGET_TASK",
            "lens": "correctness",
        },
        "workspace": workspace.as_metadata(),
    }
    worker_workspace.write_json_0600(metadata_path, metadata)
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": 999_999_999,
        "pid_start_ticks": 1,
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })
    verified_receipt = {
        "schema_id": "aiworkhub.quality_reviewer_receipt.v1",
        "report": {"lens": "correctness", "findings": []},
    }
    monkeypatch.setattr(
        process_launcher,
        "_verified_quality_review_receipt",
        lambda *_args: verified_receipt,
    )
    review_calls = []
    monkeypatch.setattr(
        manager,
        "_review_terminal_exact",
        lambda _metadata, substatus, **kwargs: (
            review_calls.append((substatus, kwargs["evidence"]))
            or {"ok": True}
        ),
    )

    event = manager._finalize_isolated_request(request_id, supervisor_returncode=0)

    assert event["state"] == "review_ready"
    assert len(review_calls) == 1
    receipt = review_calls[0][1]["attempt_artifact_manifest"]
    assert receipt["verified"] is True
    assert event["attempt_artifact_manifest"] == receipt
    assert process_launcher.attempt_artifacts.verify_json_bundle(
        Path(receipt["manifest_path"]).parent
    )["verified"] is True
    assert review_calls[0][1]["evidence_record"]["evidence_level"] == (
        "static_evidence"
    )


def test_empty_declared_validation_skips_route_and_scratch(monkeypatch, tmp_path):
    workspace = SimpleNamespace(path=tmp_path, home=tmp_path)
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty validation must not resolve a route")
        ),
    )
    monkeypatch.setattr(
        process_launcher,
        "run_validations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty validation must not enter the executor")
        ),
    )

    assert process_launcher._run_declared_validations(
        workspace,
        {"validation": []},
        {"adapter_id": "vscode_lm", "sandbox_backend": "deterministic_validation"},
    ) == []


def test_validation_only_replay_skips_bridge_cancellation_only_without_provider():
    assert process_launcher._requires_bridge_cancellation(
        {
            "execution_mode": "validation_only_replay",
            "provider_launched": False,
            "adapter_id": "deepseek_vscode_lm",
        }
    ) is False
    assert process_launcher._requires_bridge_cancellation(
        {
            "execution_mode": "validation_only_replay",
            "provider_launched": True,
            "adapter_id": "deepseek_vscode_lm",
        }
    ) is True
    assert process_launcher._requires_bridge_cancellation(
        {
            "execution_mode": "provider_worker",
            "provider_launched": False,
            "adapter_id": "deepseek_vscode_lm",
        }
    ) is True


def test_finalize_isolated_request_validation_only_replay_authorization(
    monkeypatch, tmp_path
):
    """NF50 Phase B regression at the real worker finalization callsite
    (``_finalize_isolated_request``'s ``validate_required_outputs`` call):
    an unchanged *inherited* predecessor required output only reaches
    review when the immutable ``metadata`` snapshot carries a Phase A
    ``validation_only_replay_authorization`` whose exact task, coordinator
    actor, rework predecessor request, claim epoch, and pinned raw SHA-256
    all match this exact episode. Without it, the ordinary
    ``required_output_unchanged`` failure still applies.
    """
    if os.name == "nt":
        pytest.skip("review finalization requires the POSIX secure sandbox backend")
    _open_gates(monkeypatch)
    from aiworkhub import worker_workspace

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out").mkdir(parents=True)
    canonical_file = repo / "out" / "result.json"
    canonical_file.write_text("canonical-v1", encoding="utf-8")

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "out").mkdir(parents=True)
    worked_file = workspace_dir / "out" / "result.json"
    # Byte-identical to canonical: nothing changed within this episode, and
    # the inherited predecessor content itself matches canonical too, so
    # this is genuinely a validation-only replay, not a real delta.
    worked_file.write_text("canonical-v1", encoding="utf-8")
    baseline_hash = worker_workspace._hash_path(worked_file)
    raw_sha256 = hashlib.sha256(worked_file.read_bytes()).hexdigest()

    import subprocess

    def _git(*args):
        return subprocess.run(
            ["git", *args], cwd=workspace_dir, text=True, capture_output=True, check=True
        )

    _git("init", "-q")
    _git("config", "user.email", "tests@example.invalid")
    _git("config", "user.name", "Task MCP Tests")
    _git("add", "out/result.json")
    _git("commit", "-qm", "baseline")

    home_dir = tmp_path / "home"
    home_dir.mkdir()

    workspace = worker_workspace.WorkerWorkspace(
        request_id="req-replay-1",
        repo=repo,
        path=workspace_dir,
        home=home_dir,
        allowed_writes=("out/result.json",),
        parent_baseline={"out/result.json": baseline_hash},
        workspace_baseline={"out/result.json": baseline_hash},
        inherited_rework_paths=("out/result.json",),
    )

    def _processing_card():
        card = _card()
        card["status"] = "processing"
        card["worker_status"] = "claimed"
        card["claimed_by"] = "claude_worker_b1"
        return card

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(_processing_card),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        isolation_enabled=False,
    )
    monkeypatch.setattr(
        process_launcher, "promote", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(
        process_launcher.core, "mark_review", lambda *a, **k: {"ok": True}
    )

    review_calls = []

    def fake_review_terminal_exact(metadata_arg, substatus, *, request_id, error="", evidence=None):
        review_calls.append(
            {
                "request_id": request_id,
                "task_id": metadata_arg["task_id"],
                "substatus": substatus,
                "evidence": evidence or {},
            }
        )
        return {"ok": True, "returncode": 0, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(manager, "_review_terminal_exact", fake_review_terminal_exact)

    def _run(request_id: str, authorization: dict | None):
        stdout_path = tmp_path / f"{request_id}.stdout.log"
        stderr_path = tmp_path / f"{request_id}.stderr.log"
        stdout_path.write_text("worker complete\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        status_path = tmp_path / f"{request_id}.supervisor.json"
        metadata_path = tmp_path / f"{request_id}.request.json"
        worker_workspace.write_json_0600(
            status_path, {"state": "exited", "exit_code": 0}
        )
        metadata = {
            "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "claude_cli",
            "model": "claude_cli",
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "supervisor_status_path": str(status_path),
            "cancel_path": str(tmp_path / f"{request_id}.cancel.json"),
            "prompt_sha256": "0" * 64,
            "project_context": None,
            "project_context_delivery": {"injected": False},
            "sandbox_backend": "landlock",
            "validation": [],
            "required_outputs": ["out/result.json"],
            "allow_empty_required_outputs": [],
            "allow_unchanged_required_outputs": [],
            "external_readonly_dirs": [],
            "workspace": workspace.as_metadata(),
            "claim_epoch": 3,
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "predecessor-1",
                "changed_path_hashes": {"out/result.json": raw_sha256},
            },
            "validation_only_replay_authorization": authorization,
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        manager._append_event({
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "claude_cli",
            "model": "claude_cli",
            "state": "running",
            "pid": 999_999_999,
            "pid_start_ticks": 1,
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        })
        return manager._finalize_isolated_request(request_id, supervisor_returncode=0)

    # Missing authorization: the ordinary required_output_unchanged failure
    # still applies (this must never silently pass).
    unauthorized_event = _run("req-replay-unauthorized", None)
    assert unauthorized_event["state"] == "validation_failed"
    assert "required_output_unchanged:out/result.json" in unauthorized_event["error"]
    assert unauthorized_event["attempt_artifact_manifest"]["verified"] is True
    assert process_launcher.attempt_artifacts.verify_json_bundle(
        Path(unauthorized_event["attempt_artifact_manifest"]["manifest_path"]).parent
    )["attempt_id"] == "req-replay-unauthorized"

    # A wrong claim epoch (stale/replayed episode) fails closed the same way.
    stale_authorization = {
        "task_id": "TASK_B1",
        "actor": process_launcher.core.CODEX_RUNNER,
        "predecessor_request_id": "predecessor-1",
        "changed_path_hashes": {"out/result.json": raw_sha256},
        "authorized_at": "2026-08-07T00:00:00+00:00",
        "next_claim_epoch": 99,
        "one_episode_binding": True,
    }
    stale_event = _run("req-replay-stale-epoch", stale_authorization)
    assert stale_event["state"] == "validation_failed"
    assert "required_output_unchanged:out/result.json" in stale_event["error"]

    # Exact matching authorization: reaches review with structured replay
    # evidence attached, and manager acceptance/promotion gates untouched.
    authorization = {**stale_authorization, "next_claim_epoch": 3}
    authorized_event = _run("req-replay-authorized", authorization)
    assert authorized_event["state"] == "review_ready"
    assert authorized_event["changed_paths"] == []

    call = next(c for c in review_calls if c["request_id"] == "req-replay-authorized")
    assert call["substatus"] == "review_ready"
    evidence = call["evidence"]
    record = evidence["required_outputs"][0]
    assert record["unchanged_allowed"] is True
    assert record["replay_evidence"]["sha256"] == raw_sha256
    assert record["replay_evidence"]["claim_epoch"] == 3
    assert evidence["validation_only_replay"] == [record["replay_evidence"]]


def test_validation_only_replay_authorization_fails_closed_before_launch():
    digest = "a" * 64
    card = {
        "task_id": "TASK_REPLAY",
        "claim_epoch": 4,
        "required_outputs": ["out/result.json"],
        "validation": ["python3 -m py_compile out/result.json"],
        "rework_predecessor": {
            "request_id": "predecessor-4",
            "changed_path_hashes": {"out/result.json": digest},
        },
        "validation_only_replay_authorization": {
            "task_id": "TASK_REPLAY",
            "actor": process_launcher.core.CODEX_RUNNER,
            "predecessor_request_id": "predecessor-4",
            "changed_path_hashes": {"out/result.json": digest},
            "next_claim_epoch": 4,
            "one_episode_binding": True,
        },
    }
    exact = process_launcher._validation_only_replay_authorization(
        card, "TASK_REPLAY"
    )
    assert exact is not card["validation_only_replay_authorization"]
    assert exact["changed_path_hashes"] == {"out/result.json": digest}

    no_validation = json.loads(json.dumps(card))
    no_validation["validation"] = []
    exact_no_validation = process_launcher._validation_only_replay_authorization(
        no_validation, "TASK_REPLAY"
    )
    assert exact_no_validation["changed_path_hashes"] == {
        "out/result.json": digest
    }

    stale = json.loads(json.dumps(card))
    stale["validation_only_replay_authorization"]["next_claim_epoch"] = 3
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="validation_only_replay_claim_epoch_mismatch",
    ):
        process_launcher._validation_only_replay_authorization(
            stale, "TASK_REPLAY"
        )

    forged = json.loads(json.dumps(card))
    forged["validation_only_replay_authorization"]["actor"] = "worker"
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="validation_only_replay_actor_mismatch",
    ):
        process_launcher._validation_only_replay_authorization(
            forged, "TASK_REPLAY"
        )


def test_isolated_validation_only_replay_never_resolves_or_starts_provider(
    monkeypatch, tmp_path
):
    _open_gates(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    workspace_dir.mkdir()
    home_dir.mkdir()
    digest = "b" * 64
    card = {
        **_card(task_id="TASK_REPLAY"),
        "claim_epoch": 7,
        "validation": ["python3 -m py_compile out/result.py"],
        "required_outputs": ["out/result.py"],
        "rework_predecessor": {
            "request_id": "predecessor-7",
            "changed_path_hashes": {"out/result.py": digest},
        },
        "validation_only_replay_authorization": {
            "task_id": "TASK_REPLAY",
            "actor": process_launcher.core.CODEX_RUNNER,
            "predecessor_request_id": "predecessor-7",
            "changed_path_hashes": {"out/result.py": digest},
            "next_claim_epoch": 7,
            "one_episode_binding": True,
        },
    }
    workspace = process_launcher.WorkerWorkspace(
        request_id="placeholder",
        repo=repo,
        path=workspace_dir,
        home=home_dir,
        allowed_writes=("out/result.py",),
        parent_baseline={"out/result.py": None},
        workspace_baseline={"out/result.py": digest},
        inherited_rework_paths=("out/result.py",),
    )
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(lambda: card),
        collision_guard=_collision,
        adapter_builder=lambda **_: (_ for _ in ()).throw(
            AssertionError("provider adapter plan must not be built")
        ),
    )
    monkeypatch.setattr(manager, "_preflight_card", lambda *a, **k: dict(card))
    monkeypatch.setattr(
        manager,
        "_resolve_provider_env",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("provider credentials must not be resolved")
        ),
    )
    monkeypatch.setattr(
        manager,
        "_popen",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("provider/supervisor process must not start")
        ),
    )
    monkeypatch.setattr(
        process_launcher,
        "create_workspace",
        lambda repo_arg, request_id, card_arg, adapter_id: process_launcher.replace(
            workspace, request_id=request_id
        ),
    )
    monkeypatch.setattr(
        process_launcher,
        "build_residual_contract_manifest",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "claim_start_exact",
        lambda *a, **k: {"ok": True},
    )
    finalized = []
    monkeypatch.setattr(
        manager,
        "_finalize_isolated_request",
        lambda request_id, supervisor_returncode=None: finalized.append(
            (request_id, supervisor_returncode)
        ),
    )

    result = manager.launch(
        task_id="TASK_REPLAY",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        model="claude-sonnet-5",
        timeout_seconds=30,
    )

    assert result["ok"] is True
    assert result["state"] == "running"
    assert result["terminal"] is False
    assert result["execution_mode"] == "validation_only_replay"
    assert result["provider_launched"] is False
    assert result["pid"] is None
    assert not list((tmp_path / "processes").glob("*.supervisor-spec.json"))
    deadline = time.monotonic() + 2
    while not finalized and time.monotonic() < deadline:
        time.sleep(0.01)
    assert finalized == [(result["request_id"], 0)]


def test_provider_free_replay_usage_is_labeled_without_fabricated_observation(
    monkeypatch, tmp_path
):
    output = tmp_path / "empty-provider-output.jsonl"
    output.write_text("", encoding="utf-8")
    card = _card()
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: card),
        argv=[sys.executable, "-c", "pass"],
    )
    calls = []

    def fake_run_taskctl(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(process_launcher.core, "run_taskctl", fake_run_taskctl)
    usage, recorded, error = manager._record_usage(
        "request-replay",
        card["task_id"],
        card["runner"],
        "claude_cli",
        "claude-sonnet-5",
        output,
        topic=card["topic"],
        execution_mode="validation_only_replay",
    )

    assert recorded is True
    assert error == ""
    assert usage["provider_launched"] is False
    assert usage["usage_observed"] is False
    assert usage["telemetry_reason"] == "provider_not_invoked_deterministic_replay"
    argv = calls[0][0]
    assert argv[argv.index("--provider") + 1] == "deterministic_validation_replay"
    assert argv[argv.index("--model") + 1] == "deterministic_validation_replay"
    assert argv[argv.index("--requested-model") + 1] == "claude-sonnet-5"
    assert "--usage-observed" not in argv

def test_usage_parser_reads_claude_result_json(tmp_path):
    output = tmp_path / "claude.json"
    output.write_text(json.dumps({
        "type": "result",
        "total_cost_usd": 0.125,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 45,
            "cache_read_input_tokens": 80,
        },
    }), encoding="utf-8")
    assert process_launcher._usage_from_output(output) == {
        "input_tokens": 120,
        "output_tokens": 45,
        "cached_input_tokens": 80,
        "cache_creation_input_tokens": 0,
        "usage_observed": True,
        "cache_metrics_observed": True,
        "cost_usd": 0.125,
        "cost_observed": True,
    }


def test_usage_parser_keeps_unreported_cost_unknown(tmp_path):
    output = tmp_path / "usage-without-cost.json"
    output.write_text(
        json.dumps({
            "type": "result",
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }),
        encoding="utf-8",
    )

    usage = process_launcher._usage_from_output(output)

    assert usage["usage_observed"] is True
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 3
    assert usage["cost_observed"] is False
    assert usage["cost_usd"] is None


def test_vscode_lm_usage_records_explicit_provider_api_unavailability(
    tmp_path, monkeypatch,
):
    output = tmp_path / "vscode-lm-result.jsonl"
    output.write_text(
        json.dumps({
            "type": "result",
            "subtype": "success",
            "model": {"id": "glm-5.2"},
            "result": "completed without provider usage metadata",
        }) + "\n",
        encoding="utf-8",
    )
    captured: list[str] = []

    def record(args, **_kwargs):
        captured.extend(args)
        return process_launcher.core.TaskCtlResult(args, 0, "ok", "")

    monkeypatch.setattr(process_launcher.core, "run_taskctl", record)
    manager = process_launcher.ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=False,
    )

    usage, recorded, error = manager._record_usage(
        "a" * 32,
        "TASK_USAGE",
        "glm_worker",
        "glm_vscode_lm",
        "glm-5.2",
        output,
        topic="code",
    )

    assert recorded is True
    assert error == ""
    assert usage["usage_observed"] is False
    assert usage["telemetry_reason"] == "provider_api_usage_unavailable"
    reason_index = captured.index("--telemetry-reason")
    assert captured[reason_index + 1] == "provider_api_usage_unavailable"


def test_usage_parser_preserves_nested_per_turn_cache_and_model_evidence(tmp_path):
    output = tmp_path / "provider-stream.jsonl"
    output.write_text(
        "\n".join([
            json.dumps({
                "type": "message_start",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 40,
                        "cache_creation_input_tokens": 10,
                    },
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "message_delta",
                    "usage": {"output_tokens": 25},
                },
                "total_cost_usd": 0.02,
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    usage = process_launcher._usage_from_output(output, include_samples=True)

    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 25
    assert usage["cached_input_tokens"] == 40
    assert usage["cache_creation_input_tokens"] == 10
    assert usage["observed_model"] == "claude-sonnet-5"
    assert usage["model_observed"] is True
    assert usage["usage_sample_count"] == 2
    assert [sample["event_type"] for sample in usage["usage_samples"]] == [
        "message_start",
        "message_delta",
    ]
    assert usage["cost_usd"] == 0.02
    assert usage["cost_observed"] is True


def test_usage_parser_counts_codex_reasoning_and_cache_write_tokens(tmp_path):
    output = tmp_path / "codex-stream.jsonl"
    output.write_text(
        json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 129_189,
                "cached_input_tokens": 111_232,
                "cache_write_input_tokens": 12,
                "output_tokens": 1_113,
                "reasoning_output_tokens": 285,
            },
        }) + "\n",
        encoding="utf-8",
    )

    usage = process_launcher._usage_from_output(output, include_samples=True)

    assert usage["reasoning_output_tokens"] == 285
    assert usage["cache_write_input_tokens"] == 12
    assert usage["cache_creation_input_tokens"] == 12
    assert usage["cache_metrics_observed"] is True
    assert process_launcher.provider_usage.cumulative_total_tokens(
        usage, "codex_cli"
    ) == 130_587
    assert process_launcher._ledger_output_tokens(usage) == 1_398


def test_termination_refuses_a_pid_without_recorded_start_ticks():
    """A bare pid is not an identity, so it must never authorise a kill.

    ``_pid_matches`` answers "yes" for any live pid when no start ticks were
    recorded, which is fine for liveness reporting.  Termination goes through
    ``_identity_verified_pid`` instead: on Windows the terminator is
    ``taskkill /PID <pid> /T``, which also kills every descendant, so a
    recycled pid would take out an unrelated process tree.
    """

    live_pid = os.getpid()

    # The permissive helper still reports a match -- that is its contract.
    assert process_launcher._pid_matches(live_pid, None) is True
    assert process_launcher._pid_matches(live_pid, "") is True

    # The termination gate refuses all of them.
    assert process_launcher._identity_verified_pid(live_pid, None) == 0
    assert process_launcher._identity_verified_pid(live_pid, "") == 0


def test_termination_accepts_only_a_matching_creation_timestamp():
    live_pid = os.getpid()
    ticks = process_launcher._pid_start_ticks(live_pid)
    if ticks is None:
        pytest.skip("process creation timestamps are unavailable on this host")

    assert process_launcher._identity_verified_pid(live_pid, ticks) == live_pid
    assert process_launcher._identity_verified_pid(live_pid, str(ticks)) == live_pid
    # A recycled pid presents a different creation timestamp.
    assert process_launcher._identity_verified_pid(live_pid, ticks + 1) == 0


@pytest.mark.parametrize("pid", [0, -1, None, "", "not-a-pid"])
def test_termination_refuses_a_malformed_pid(pid):
    assert process_launcher._identity_verified_pid(pid, 12345) == 0


def test_collect_returns_bounded_projection_without_recursive_card(tmp_path, monkeypatch):
    manager = _manager(tmp_path, show_task=_show(lambda: _card()), argv=[])
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("x" * 10_000, encoding="utf-8")
    stderr.write_text("y" * 10_000, encoding="utf-8")
    huge_card = {
        **_card(state="review"),
        "claimed_by": "claude_worker_b1",
        "terminal_review": {"evidence": {"nested": "z" * 100_000}},
        "card_json": "q" * 100_000,
    }
    huge_event = {
        "request_id": "req-bounded",
        "task_id": "TASK_B1",
        "state": "review_ready",
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
        "terminal_review": huge_card["terminal_review"],
        "changed_paths": [f"out/{index}.json" for index in range(100)],
    }
    monkeypatch.setattr(manager, "status", lambda _request_id: {
        "ok": True,
        "request_id": "req-bounded",
        "task_id": "TASK_B1",
        "state": "review_ready",
        "process_alive": False,
        "exit_code": 0,
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "sonnet",
        "task_state": "review",
        "task_card": huge_card,
        "event_count": 3,
        "latest_event": huge_event,
        "liveness": {},
    })

    result = manager.collect("req-bounded", max_log_bytes=1024)

    assert result["log_bytes_returned"] <= 1024
    assert "terminal_review" not in result["task_card"]
    assert result["task_card"]["claimed_by"] == "claude_worker_b1"
    assert "terminal_review" not in result["latest_event"]
    assert result["latest_event"]["changed_path_count"] == 100
    assert len(result["latest_event"]["changed_paths"]) == 64
    assert result["truncated_fields"] == ["task_card", "latest_event"]
    assert len(json.dumps(result)) < 12_000


def test_worker_context_section_count_supports_v1_and_v2_bundles():
    assert process_launcher._worker_context_section_count(
        {"sections": [{"name": "source_graph"}, {"name": "session"}]}
    ) == 2
    assert process_launcher._worker_context_section_count(
        {"evidence": {"source_graph": {}, "session_current_state": {}}}
    ) == 2
    assert process_launcher._worker_context_section_count({}) == 0


def test_quality_review_card_is_readonly_and_quality_review_topic():
    quality_card = {
        "topic": "quality_review",
        "project_context": {"task_type": "research"},
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
    }
    assert process_launcher._card_is_readonly_quality_review(quality_card) is True

    impl_card = {
        "topic": "task_mcp",
        "project_context": {"task_type": "code"},
        "read_only": False,
        "allowed_writes": ["out.txt"],
        "required_outputs": ["out.txt"],
    }
    assert process_launcher._card_is_readonly_quality_review(impl_card) is False


def test_quality_review_card_identification_rejects_mutation():
    card = {
        "topic": "quality_review",
        "project_context": {"task_type": "research"},
        "read_only": False,
        "allowed_writes": ["some_file"],
        "required_outputs": [],
    }
    assert process_launcher._card_is_readonly_quality_review(card) is False


def _w1_pid_evidence(
    verdict: process_launcher.PidIdentityVerdict,
) -> process_launcher.PidIdentityEvidence:
    return process_launcher.PidIdentityEvidence(
        verdict=verdict,
        pid=123,
        expected_start_ticks=456,
        observed_start_ticks=(
            456 if verdict is process_launcher.PidIdentityVerdict.MATCH else None
        ),
        attempts=1,
        operation="test",
    )


def test_status_pid_identity_unknown_defers_without_mutation_then_mismatch_finalizes(
    monkeypatch,
    tmp_path,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "status-pid-identity"
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": 123,
        "pid_start_ticks": 456,
        "metadata_path": str(tmp_path / "request.json"),
    })
    verdict = {"value": process_launcher.PidIdentityVerdict.UNKNOWN}
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict["value"]),
    )
    monkeypatch.setattr(process_launcher, "_pid_matches", lambda *_args: False)
    finalizer_calls = []

    def finalize(request_id_arg, *, lock_blocking=True):
        finalizer_calls.append((request_id_arg, lock_blocking))
        manager._append_event({
            "request_id": request_id_arg,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "state": "worker_failed",
        })

    monkeypatch.setattr(manager, "_finalize_after_process_exit", finalize)
    before = manager._request_events(request_id)

    unknown = manager.status(request_id)

    assert unknown["state"] == "running"
    assert unknown["latest_event"]["reconciliation_deferred"] == "pid_identity_unknown"
    assert manager._request_events(request_id) == before
    assert finalizer_calls == []

    verdict["value"] = process_launcher.PidIdentityVerdict.MATCH
    assert manager.status(request_id)["state"] == "running"
    assert finalizer_calls == []

    verdict["value"] = process_launcher.PidIdentityVerdict.MISMATCH
    assert manager.status(request_id)["state"] == "worker_failed"
    assert finalizer_calls == [(request_id, False)]
    assert manager.status(request_id)["state"] == "worker_failed"
    assert finalizer_calls == [(request_id, False)]


def test_cancel_pid_identity_tri_state_and_bridge_completion_order(
    monkeypatch,
    tmp_path,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    verdicts = {}
    ordering = []
    bridge_results = {}
    signals = []
    finalizer_calls = []

    def seed(request_id):
        metadata_path = tmp_path / f"{request_id}.json"
        cancel_path = tmp_path / f"{request_id}.cancel.json"
        metadata_path.write_text(
            json.dumps({"cancel_path": str(cancel_path)}), encoding="utf-8"
        )
        manager._append_event({
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "claude_cli",
            "state": "running",
            "pid": 123,
            "pid_start_ticks": 456,
            "metadata_path": str(metadata_path),
        })
        return cancel_path

    def bridge(request_id, _live):
        ordering.append((request_id, "bridge"))
        return bridge_results.get(request_id, "")

    def identity(_pid, _ticks):
        request_id = ordering[-1][0]
        ordering.append((request_id, "identity"))
        return _w1_pid_evidence(verdicts[request_id])

    def finalize(request_id):
        ordering.append((request_id, "finalize"))
        finalizer_calls.append(request_id)
        manager._append_event({
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "state": "worker_failed",
        })

    monkeypatch.setattr(
        manager, "_publish_bridge_cancellation_before_finalization", bridge
    )
    monkeypatch.setattr(process_launcher, "_pid_identity_evidence", identity)
    monkeypatch.setattr(manager, "_finalize_after_process_exit", finalize)
    monkeypatch.setattr(
        process_launcher.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    unknown_id = "cancel-unknown"
    unknown_cancel_path = seed(unknown_id)
    verdicts[unknown_id] = process_launcher.PidIdentityVerdict.UNKNOWN
    before = manager._request_events(unknown_id)
    unknown = manager.cancel(unknown_id)
    assert unknown["blocked_reason"] == "pid_identity_unknown"
    assert manager._request_events(unknown_id) == before
    assert not unknown_cancel_path.exists()
    assert signals == []
    assert finalizer_calls == []
    assert ordering == [(unknown_id, "bridge"), (unknown_id, "identity")]

    completed_id = "cancel-completed"
    completed_path = seed(completed_id)
    bridge_results[completed_id] = "completed"
    completed = manager.cancel(completed_id)
    assert completed["completion_won"] is True
    assert not completed_path.exists()
    assert (completed_id, "identity") not in ordering

    match_id = "cancel-match"
    match_path = seed(match_id)
    verdicts[match_id] = process_launcher.PidIdentityVerdict.MATCH
    matched = manager.cancel(match_id)
    assert matched["state"] == "cancel_requested"
    assert json.loads(match_path.read_text(encoding="utf-8"))["request_id"] == match_id
    assert signals == [(123, signal.SIGTERM)]
    assert ordering[-2:] == [(match_id, "bridge"), (match_id, "identity")]

    mismatch_id = "cancel-mismatch"
    mismatch_path = seed(mismatch_id)
    verdicts[mismatch_id] = process_launcher.PidIdentityVerdict.MISMATCH
    mismatched = manager.cancel(mismatch_id)
    assert mismatched["state"] == "worker_failed"
    assert not mismatch_path.exists()
    assert finalizer_calls == [mismatch_id]
    assert ordering[-3:] == [
        (mismatch_id, "bridge"),
        (mismatch_id, "identity"),
        (mismatch_id, "finalize"),
    ]


_W2_ADMISSION_CASES = [
    pytest.param(process_launcher.PidIdentityVerdict.MATCH, 456, True, id="match"),
    pytest.param(process_launcher.PidIdentityVerdict.UNKNOWN, 456, True, id="unknown"),
    pytest.param(process_launcher.PidIdentityVerdict.MISMATCH, 456, False, id="mismatch"),
    pytest.param(process_launcher.PidIdentityVerdict.UNKNOWN, None, True, id="missing-ticks"),
    pytest.param(
        process_launcher.PidIdentityVerdict.UNKNOWN,
        "malformed-ticks",
        True,
        id="malformed-ticks",
    ),
]


def _seed_w2_persisted_request(
    manager,
    *,
    request_id,
    task_id,
    ticks,
    state="running",
    pid=123,
):
    manager._append_event({
        "request_id": request_id,
        "task_id": task_id,
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": state,
        "pid": pid,
        "pid_start_ticks": ticks,
        "metadata_path": "persisted-request.json",
    })


def _forbid_w2_admission_side_effects(monkeypatch, manager):
    calls = []

    def forbidden(name):
        def record(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"admission triggered forbidden side effect: {name}")

        return record

    monkeypatch.setattr(
        manager,
        "_finalize_after_process_exit",
        forbidden("terminal_event"),
    )
    monkeypatch.setattr(manager, "_release_exact", forbidden("release"))
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        forbidden("callback"),
    )
    monkeypatch.setattr(
        process_launcher,
        "cleanup_workspace",
        forbidden("gc"),
    )
    return calls


@pytest.mark.parametrize("verdict,ticks,expected_active", _W2_ADMISSION_CASES)
def test_active_request_ids_pid_identity_admission_matrix(
    monkeypatch,
    tmp_path,
    verdict,
    ticks,
    expected_active,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-capacity-persisted"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="OTHER_TASK_B1",
        ticks=ticks,
    )
    before = manager._request_events(request_id)
    identity_calls = []

    def identity(pid, expected_ticks):
        identity_calls.append((pid, expected_ticks))
        return _w1_pid_evidence(verdict)

    monkeypatch.setattr(process_launcher, "_pid_identity_evidence", identity)
    monkeypatch.setattr(
        process_launcher,
        "_pid_matches",
        lambda *_args: pytest.fail("admission must not use reporting-only _pid_matches"),
    )
    side_effects = _forbid_w2_admission_side_effects(monkeypatch, manager)

    active = manager._active_request_ids()

    assert (request_id in active) is expected_active
    assert manager._active_count() == int(expected_active)
    assert identity_calls == [(123, ticks), (123, ticks)]
    assert manager._request_events(request_id) == before
    assert side_effects == []


@pytest.mark.parametrize("verdict,ticks,expected_blocked", _W2_ADMISSION_CASES)
def test_assert_no_duplicate_task_pid_identity_admission_matrix(
    monkeypatch,
    tmp_path,
    verdict,
    ticks,
    expected_blocked,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-assert-duplicate"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="TASK_B1",
        ticks=ticks,
    )
    before = manager._request_events(request_id)
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict),
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_matches",
        lambda *_args: pytest.fail("admission must not use reporting-only _pid_matches"),
    )
    side_effects = _forbid_w2_admission_side_effects(monkeypatch, manager)

    if expected_blocked:
        with pytest.raises(
            process_launcher.LaunchRejected,
            match=f"duplicate_persisted_task:{request_id}",
        ):
            manager._assert_no_duplicate_task("TASK_B1")
    else:
        manager._assert_no_duplicate_task("TASK_B1")

    assert manager._request_events(request_id) == before
    assert side_effects == []


@pytest.mark.parametrize("verdict,ticks,expected_blocked", _W2_ADMISSION_CASES)
def test_direct_launch_duplicate_pid_identity_admission_matrix(
    monkeypatch,
    tmp_path,
    verdict,
    ticks,
    expected_blocked,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-direct-duplicate"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="TASK_B1",
        ticks=ticks,
    )
    before = manager._request_events(request_id)
    monkeypatch.setattr(manager, "_active_count", lambda: 0)
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict),
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_matches",
        lambda *_args: pytest.fail("admission must not use reporting-only _pid_matches"),
    )
    side_effects = (
        _forbid_w2_admission_side_effects(monkeypatch, manager)
        if expected_blocked
        else []
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert result["ok"] is not expected_blocked
    if expected_blocked:
        assert result["blocked_reason"] == f"duplicate_persisted_task:{request_id}"
    else:
        _wait_terminal(manager, result["request_id"])
    assert manager._request_events(request_id) == before
    assert side_effects == []


@pytest.mark.parametrize(
    "verdict,expected_blocked",
    [
        pytest.param(process_launcher.PidIdentityVerdict.MATCH, True, id="match"),
        pytest.param(
            process_launcher.PidIdentityVerdict.UNKNOWN,
            True,
            id="unknown",
        ),
        pytest.param(
            process_launcher.PidIdentityVerdict.MISMATCH,
            False,
            id="mismatch",
        ),
    ],
)
def test_direct_launch_cancel_requested_pid_identity_admission_matrix(
    monkeypatch,
    tmp_path,
    verdict,
    expected_blocked,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-direct-cancel-requested"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="TASK_B1",
        ticks=456,
        state="cancel_requested",
    )
    before = manager._request_events(request_id)
    monkeypatch.setattr(manager, "_active_count", lambda: 0)
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict),
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_matches",
        lambda *_args: pytest.fail("admission must not use reporting-only _pid_matches"),
    )
    side_effects = (
        _forbid_w2_admission_side_effects(monkeypatch, manager)
        if expected_blocked
        else []
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert result["ok"] is not expected_blocked
    if expected_blocked:
        assert result["blocked_reason"] == f"duplicate_persisted_task:{request_id}"
    else:
        _wait_terminal(manager, result["request_id"])
    assert manager._request_events(request_id) == before
    assert side_effects == []


def test_missing_pid_is_mismatch_inactive_and_allows_direct_launch(
    monkeypatch,
    tmp_path,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-direct-missing-pid"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="TASK_B1",
        ticks=None,
        state="cancel_requested",
        pid=0,
    )
    before = manager._request_events(request_id)

    identity = process_launcher._pid_identity_evidence(0, None)
    assert identity.verdict is process_launcher.PidIdentityVerdict.MISMATCH
    assert request_id not in manager._active_request_ids()
    manager._assert_no_duplicate_task("TASK_B1")

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert result["ok"] is True
    _wait_terminal(manager, result["request_id"])
    assert manager._request_events(request_id) == before


def test_unknown_duplicate_recovery_allows_direct_launch_after_mismatch(
    monkeypatch,
    tmp_path,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    persisted_id = "w2-direct-recovery"
    _seed_w2_persisted_request(
        manager,
        request_id=persisted_id,
        task_id="TASK_B1",
        ticks=None,
    )
    before = manager._request_events(persisted_id)
    verdict = {"value": process_launcher.PidIdentityVerdict.UNKNOWN}
    monkeypatch.setattr(manager, "_active_count", lambda: 0)
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict["value"]),
    )

    blocked = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert blocked["blocked_reason"] == f"duplicate_persisted_task:{persisted_id}"
    assert manager._request_events(persisted_id) == before

    verdict["value"] = process_launcher.PidIdentityVerdict.MISMATCH
    recovered = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert recovered["ok"] is True
    _wait_terminal(manager, recovered["request_id"])
    assert manager._request_events(persisted_id) == before


# ---------------------------------------------------------------------------
# NF129: rework_overlay / request_scoped_predecessor live Source Graph gate
# ---------------------------------------------------------------------------

import hmac as _hmac_mod  # noqa: E402


def _sign_entry(entry: dict, key: bytes) -> str:
    """HMAC-sign a ledger entry dict, returning the complete JSON line."""
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    digest = _hmac_mod.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    signed = {**entry, "hmac_sha256": digest}
    return json.dumps(signed, ensure_ascii=False, sort_keys=True) + "\n"


_ENTRY_SCHEMA = worker_ai_tools_mcp.AUDIT_ENTRY_SCHEMA_ID


def _rework_entry(**overrides: object) -> dict:
    defaults: dict[str, object] = {
        "schema_id": _ENTRY_SCHEMA,
        "timestamp": "2026-08-11T01:00:00+00:00",
        "task_id": "TASK_NF129",
        "runner": "test_runner",
        "topic": "nf129_topic",
        "request_id": "req-nf129",
        "tool": "source_graph",
        "ok": True,
        "cache_hit": False,
        "hit_count": 3,
        "bytes_returned": 500,
        "violation": "",
        "authority_source": "rework_overlay",
        "authority_state": "request_scoped_predecessor",
        "authority_repo": "/test/repo",
    }
    return {**defaults, **overrides}


def test_rework_overlay_counts_as_live_source_graph_call(tmp_path: Path) -> None:
    """authority_source=rework_overlay + authority_state=request_scoped_predecessor
    must be counted as an authoritative live Source Graph call after HMAC +
    identity checks pass."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry()
    line = _sign_entry(entry, key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["entries_verified"] == 1
    assert result["entries_tampered"] == 0
    assert result["live_source_graph_calls"] == 1
    assert result["fresh_source_graph_calls"] == 1
    assert (
        "source_graph:rework_overlay:request_scoped_predecessor:/test/repo"
        in result["authority_index_identity"]
    )


def test_rework_overlay_rejected_with_wrong_hmac(tmp_path: Path) -> None:
    """A rework_overlay entry with a tampered/forged HMAC must be dropped,
    not counted as live."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry()
    line = _sign_entry(entry, key)

    # Forge: sign with a different key
    wrong_key = os.urandom(32)
    forged_line = _sign_entry(entry, wrong_key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(forged_line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["entries_verified"] == 0
    assert result["entries_tampered"] == 1
    assert result["live_source_graph_calls"] == 0


def test_rework_overlay_rejected_with_wrong_identity(tmp_path: Path) -> None:
    """A rework_overlay entry with a mismatched task_id/runner/topic/request_id
    must not count as live."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry(task_id="TASK_OTHER")
    line = _sign_entry(entry, key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["entries_verified"] == 0
    assert result["live_source_graph_calls"] == 0


def test_rework_overlay_not_live_when_cache_hit(tmp_path: Path) -> None:
    """A cached rework_overlay source_graph call is fresh telemetry but not
    live -- the completion gate requires a non-cached hit."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry(cache_hit=True)
    line = _sign_entry(entry, key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["live_source_graph_calls"] == 0
    assert result["fresh_source_graph_calls"] == 0


def test_rework_overlay_zero_hit_is_still_a_live_invocation(tmp_path: Path) -> None:
    """A fresh authenticated zero-hit overlay call is live invocation truth;
    evidence usefulness remains visible in the zero-hit counters."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry(hit_count=0)
    line = _sign_entry(entry, key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["live_source_graph_calls"] == 1
    assert result["fresh_source_graph_calls"] == 1
    assert result["source_graph_zero_hit_calls"] == 1


def test_rework_overlay_authority_label_remains_distinct(tmp_path: Path) -> None:
    """rework_overlay must appear under its own authority label in the index,
    never conflated with canonical or candidate_overlay."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    rework = _rework_entry()
    canonical = _rework_entry(
        authority_source="canonical",
        authority_state="sole_authority",
    )
    candidate = _rework_entry(
        authority_source="candidate_overlay",
        authority_state="quality_review_readonly",
    )

    lines = "".join(_sign_entry(e, key) for e in (rework, canonical, candidate))
    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(lines, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["live_source_graph_calls"] == 3
    authority = set(result["authority_index_identity"])
    assert "source_graph:rework_overlay:request_scoped_predecessor:/test/repo" in authority
    assert "source_graph:canonical:sole_authority:/test/repo" in authority
    assert "source_graph:candidate_overlay:quality_review_readonly:/test/repo" in authority
    # Three distinct labels, no conflation
    assert len(authority) == 3


# ---------------------------------------------------------------------------
# NF-2026-00118 / NF-2026-00131: quality-review lifecycle bootstrap regressions
# ---------------------------------------------------------------------------


def _sealed_reviewer_receipt(
    packet_sha256: str | None = None,
    reviewer_request_id: str = "rev-req-001",
    reviewer_task_id: str = "rev-task-001",
    target_request_id: str = "tgt-req-001",
    target_task_id: str = "tgt-task-001",
    provider: str = "deepseek_vscode_lm",
    claim_epoch: int = 1,
) -> dict:
    if packet_sha256 is None:
        packet_sha256 = hashlib.sha256(b"packet-body").hexdigest()
    from aiworkhub import quality_reviewer as _qr

    return {
        "schema_id": _qr.RECEIPT_SCHEMA_ID,
        "packet_sha256": packet_sha256,
        "target": {
            "request_id": target_request_id,
            "task_id": target_task_id,
            "claim_epoch": claim_epoch,
        },
        "reviewer": {
            "request_id": reviewer_request_id,
            "task_id": reviewer_task_id,
            "provider": provider,
        },
        "report": {
            "lens": "correctness",
            "provider": provider,
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": hashlib.sha256(b"sealed-submission").hexdigest(),
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }


def _accepted_latest_event(
    receipt: dict, reviewer_task_id: str = "rev-task-001", adapter_id: str = "deepseek_vscode_lm",
) -> dict:
    return {
        "state": "accepted",
        "accepted": True,
        "task_id": reviewer_task_id,
        "adapter_id": adapter_id,
        "quality_review_receipt": receipt,
    }


def _quality_review_workspace_metadata() -> dict:
    return {
        "request_id": "rev-req-001",
        "repo": "/tmp/quality-review-repo",
        "path": "/tmp/quality-review-workspace",
        "home": "/tmp/quality-review-home",
        "allowed_writes": [],
        "parent_baseline": {},
        "workspace_baseline": {},
        "inherited_rework_paths": [],
    }


def _accepted_card(
    receipt: dict,
    reviewer_request_id: str = "rev-req-001",
    reviewer_task_id: str = "rev-task-001",
    topic: str = "quality_review",
    claim_epoch: int = 1,
) -> dict:
    return {
        "task_id": reviewer_task_id,
        "accepted_request_id": reviewer_request_id,
        "topic": topic,
        "status": "finished",
        "allowed_writes": [],
        "terminal_review": {
            "evidence": {
                "quality_review_receipt": receipt,
                "quality_review": {
                    "target_claim_epoch": claim_epoch,
                    "adapter_id": "deepseek_vscode_lm",
                },
                "changed_paths": [],
                "changed_path_hashes": {},
                "workspace": _quality_review_workspace_metadata(),
            },
        },
        "accept_evidence": {"quality_review_receipt": receipt},
    }


def test_sealed_reviewer_receipt_survives_empty_workspace_hashes() -> None:
    """A sealed read-only reviewer receipt remains consumable after workspace
    cleanup when changed_paths=[] and canonical_delta_paths=[] because the
    receipt identity is self-contained in the event/card/receipt triple and
    does not depend on workspace-side hash files."""
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    result = process_launcher._verified_accepted_quality_review_receipt(
        latest=latest,
        card=card,
        reviewer_request_id="rev-req-001",
        target_request_id="tgt-req-001",
        target_task_id="tgt-task-001",
    )

    assert result["schema_id"] == receipt["schema_id"]
    assert result["packet_sha256"] == receipt["packet_sha256"]
    assert result["report"]["read_only"] is True
    assert result["report"]["can_mutate_repo"] is False
    assert result["authority"]["process_identity_verified"] is True
    # The receipt is valid with no workspace hashes — the canonical_delta_paths
    # and changed_paths are [] for a read-only sealed receipt.
    assert result["target"]["request_id"] == "tgt-req-001"
    assert result["target"]["task_id"] == "tgt-task-001"


def test_sealed_reviewer_receipt_rejects_event_not_accepted() -> None:
    """A reviewer event that was never accepted must be rejected even when the
    receipt payload is otherwise well-formed."""
    receipt = _sealed_reviewer_receipt()
    latest = {
        "state": "review_ready",
        "accepted": False,
        "task_id": "rev-task-001",
        "quality_review_receipt": receipt,
    }
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_accepted_event_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_identity_mismatch() -> None:
    """A receipt whose task/request identity doesn't match the card must be
    rejected — sealed does not mean unverified."""
    receipt = _sealed_reviewer_receipt(
        reviewer_request_id="rev-req-001",
        target_request_id="tgt-req-001",
    )
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)
    # Card has a different request_id than what we pass as target
    card["accepted_request_id"] = "rev-req-001"

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_accepted_target_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="wrong-target-req",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_wrong_topic() -> None:
    """A card marked with a non-quality_review topic cannot satisfy the sealed
    receipt acceptance check."""
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt, topic="task_mcp")

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_accepted_topic_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_readonly_no_paths_no_hashes_with_receipt() -> None:
    """A retained read-only reviewer receipt with typed-empty changed_paths
    (list), changed_path_hashes (dict) and empty allowed_writes remains
    consumable after reload because its identity is self-contained."""
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    result = process_launcher._verified_accepted_quality_review_receipt(
        latest=latest,
        card=card,
        reviewer_request_id="rev-req-001",
        target_request_id="tgt-req-001",
        target_task_id="tgt-task-001",
    )

    assert result["schema_id"] == receipt["schema_id"]
    assert result["packet_sha256"] == receipt["packet_sha256"]
    assert result["submission_id"] == receipt["submission_id"]
    assert result["target"]["claim_epoch"] == 1
    assert result["physical_submission_count"] == 1
    assert result["logical_submission_count"] == 1


def test_sealed_reviewer_receipt_rejects_nonempty_changed_paths() -> None:
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)
    card["terminal_review"]["evidence"]["changed_paths"] = ["src/x.py"]

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_changed_paths_not_empty",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_nonempty_changed_path_hashes() -> None:
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)
    card["terminal_review"]["evidence"]["changed_path_hashes"] = {
        "src/x.py": hashlib.sha256(b"x").hexdigest()
    }

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_changed_path_hashes_not_empty",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_nonempty_workspace_allowed_writes() -> None:
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)
    card["terminal_review"]["evidence"]["workspace"]["allowed_writes"] = ["out/x.py"]

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_workspace_allowed_writes_not_empty",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_bool_claim_epoch() -> None:
    receipt = _sealed_reviewer_receipt(claim_epoch=True)  # type: ignore[arg-type]
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_claim_epoch_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_bool_submission_count() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt["physical_submission_count"] = True
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_physical_submission_count_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_uppercase_packet_sha256() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt["packet_sha256"] = receipt["packet_sha256"].upper()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_packet_sha256_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_provider_mismatch() -> None:
    receipt = _sealed_reviewer_receipt(provider="other_provider")
    latest = _accepted_latest_event(receipt, adapter_id="deepseek_vscode_lm")
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_reviewer_provider_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_claim_epoch_binding_mismatch() -> None:
    receipt = _sealed_reviewer_receipt(claim_epoch=7)
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt, claim_epoch=8)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_claim_epoch_binding_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_adapter_binding_mismatch() -> None:
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt, adapter_id="deepseek_vscode_lm")
    card = _accepted_card(receipt)
    card["terminal_review"]["evidence"]["quality_review"]["adapter_id"] = "other_adapter"

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_adapter_binding_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_missing_submission_counts() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt.pop("submission_id")
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_receipt_top_level_keys_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_quality_review_receipt_schema_rejects_string_claim_epoch() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt["target"]["claim_epoch"] = "1"

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_claim_epoch_invalid",
    ):
        process_launcher._enforce_quality_review_receipt_schema(
            receipt, "deepseek_vscode_lm"
        )


def test_quality_review_receipt_schema_rejects_missing_findings() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt["report"].pop("findings")

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_report_keys_invalid",
    ):
        process_launcher._enforce_quality_review_receipt_schema(
            receipt, "deepseek_vscode_lm"
        )


def test_native_cli_large_packet_uses_file_transport_avoiding_argv_e2big(
    tmp_path: Path,
) -> None:
    """Large quality-review packets (≥ 150 KB serialised) must be routed
    through file/stdin transport, never through argv, to avoid E2BIG on
    native CLI adapters."""
    from aiworkhub import quality_reviewer as _qr

    # Bounded path count (≤ MAX_PACKET_PATHS) with large mechanical_checks
    # provenance strings to push the serialised packet above 150 KB without
    # exceeding any production packet limit.
    large_changed_path_hashes = {
        f"src/large_module_{i:04d}.py": hashlib.sha256(
            f"content-{i}".encode("utf-8")
        ).hexdigest()
        for i in range(150)
    }
    packet = _qr.build_review_packet(
        request_id="req-e2big-001",
        task_id="task-e2big-001",
        claim_epoch=1,
        worker_provider="deepseek_vscode_lm",
        changed_path_hashes=large_changed_path_hashes,
        acceptance=["Packets >= 150 KB must avoid argv."],
        mechanical_checks=[
            {
                "check_id": f"ck-{j:04d}",
                "kind": "lint",
                "status": "ok",
                "provenance": "X" * 1900,
            }
            for j in range(70)
        ],
    )
    encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert len(encoded.encode("utf-8")) > 150_000, (
        f"Packet must be > 150 KB to trigger E2BIG gate; got {len(encoded.encode('utf-8'))} bytes"
    )

    packet_file = tmp_path / "large_packet.json"
    packet_file.write_text(encoded, encoding="utf-8")

    prompt = _qr.build_review_prompt(
        packet,
        lens="correctness",
        submit_tool_name="aiworkhub_worker_quality_review_submit",
        packet_file=str(packet_file),
        max_inline_bytes=96 * 1024,
    )
    assert "QUALITY_REVIEW_PACKET_FILE:" in prompt
    assert "QUALITY_REVIEW_PACKET:" not in prompt
    assert "Read the packet file first" in prompt


def test_bounded_review_submit_fails_closed_without_receipt_schema() -> None:
    """A receipt payload missing the schema_id or with a wrong schema must be
    rejected — the protocol fails closed rather than fabricating findings."""
    from aiworkhub import quality_reviewer as _qr

    packet = _qr.build_review_packet(
        request_id="req-bounded-001",
        task_id="task-bounded-001",
        claim_epoch=1,
        worker_provider="deepseek_vscode_lm",
        changed_path_hashes={"src/module.py": "a" * 64},
    )
    # No schema_id at all
    with pytest.raises(_qr.ReviewerEvidenceError):
        _qr.verify_reviewer_receipt(
            receipt={"report": {"lens": "correctness", "findings": []}},
            packet=packet,
            expected_reviewer_request_id="rev-req-001",
            expected_reviewer_task_id="rev-task-001",
            observed_provider="deepseek_vscode_lm",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )

    # Wrong schema
    with pytest.raises(_qr.ReviewerEvidenceError):
        _qr.verify_reviewer_receipt(
            receipt={"schema_id": "aiworkhub.wrong_schema.v1", "report": {}},
            packet=packet,
            expected_reviewer_request_id="rev-req-001",
            expected_reviewer_task_id="rev-task-001",
            observed_provider="deepseek_vscode_lm",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )


def test_bounded_review_submit_rejects_provider_spoof_in_receipt() -> None:
    """A receipt's self-reported provider must match the process-observed
    provider. A provider string in JSON proves nothing — the launcher
    independently records the actual adapter_id."""
    from aiworkhub import quality_reviewer as _qr

    receipt = _sealed_reviewer_receipt(provider="attacker_provider")
    packet = _qr.build_review_packet(
        request_id="req-provider-001",
        task_id="task-provider-001",
        claim_epoch=1,
        worker_provider="deepseek_vscode_lm",
        changed_path_hashes={"src/module.py": "a" * 64},
    )
    # Build a receipt with the right shape but spoofed provider
    shaped_receipt = {
        "schema_id": _qr.RECEIPT_SCHEMA_ID,
        "packet_sha256": packet["packet_sha256"],
        "target": {
            "request_id": "req-provider-001",
            "task_id": "task-provider-001",
            "claim_epoch": 1,
        },
        "reviewer": {
            "request_id": "rev-req-001",
            "task_id": "rev-task-001",
            "provider": "attacker_provider",
        },
        "report": {
            "lens": "correctness",
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
    }
    with pytest.raises(
        _qr.ReviewerEvidenceError,
        match="reviewer_provider_spoofed",
    ):
        _qr.verify_reviewer_receipt(
            receipt=shaped_receipt,
            packet=packet,
            expected_reviewer_request_id="rev-req-001",
            expected_reviewer_task_id="rev-task-001",
            observed_provider="deepseek_vscode_lm",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )


def _reserve_starting(
    manager,
    request_id: str,
    phase: str | None = None,
    *,
    expires_at_epoch: float | None = None,
) -> None:
    event = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
        "reservation_expires_at_epoch": (
            time.time() + 600
            if expires_at_epoch is None
            else expires_at_epoch
        ),
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
    }
    if phase is not None:
        event["preparation_phase"] = phase
    manager._append_event(event)


def test_quality_review_prewarm_liveness_tracks_started_phase(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "req-prewarm-live"
    _reserve_starting(
        manager, request_id, "reviewer_source_graph_prewarm_started"
    )

    assert manager._reviewer_source_graph_prewarm_live(request_id) is True

    manager._publish_reviewer_progress(
        request_id, "reviewer_source_graph_prewarm_complete"
    )
    assert manager._reviewer_source_graph_prewarm_live(request_id) is False


def test_quality_review_launch_owner_join_keeps_live_prewarm(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "req-prewarm-live"
    _reserve_starting(
        manager, request_id, "reviewer_source_graph_prewarm_started"
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS",
        0.05,
    )

    def owner() -> None:
        time.sleep(0.25)

    launcher = threading.Thread(target=owner)
    launcher.start()

    outcome = manager._reviewer_launch_owner_join(launcher, request_id)
    launcher.join(timeout=5)

    assert outcome == "completed"


def test_quality_review_launch_owner_join_timeouts_without_live_prewarm(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "req-prewarm-stale"
    _reserve_starting(manager, request_id, "packet_prepared")
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS",
        0.05,
    )

    stop = threading.Event()

    def owner() -> None:
        while not stop.is_set():
            time.sleep(0.01)

    launcher = threading.Thread(target=owner, daemon=True)
    launcher.start()

    outcome = manager._reviewer_launch_owner_join(launcher, request_id)
    stop.set()
    launcher.join(timeout=5)

    assert outcome == "timeout"


def test_quality_review_prewarm_reconciliation_defers_live_owned_prewarm(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    live_request = "req-live-prewarm-expired"
    _reserve_starting(
        manager,
        live_request,
        "reviewer_source_graph_prewarm_started",
        expires_at_epoch=time.time() - 60,
    )
    unrelated_request = "req-unrelated-stale"
    manager._append_event({
        "request_id": unrelated_request,
        "task_id": "TASK_UNRELATED",
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
        "reservation_expires_at_epoch": time.time() - 60,
    })

    reconciled = manager._reconcile_expired_starting_reservations()

    assert reconciled == 1
    live_latest = manager._latest_by_request()[live_request]
    assert live_latest.get("state") == "starting"
    assert (
        live_latest.get("preparation_phase")
        == "reviewer_source_graph_prewarm_started"
    )
    unrelated_latest = manager._latest_by_request()[unrelated_request]
    assert unrelated_latest.get("state") == "blocked"
    assert unrelated_latest.get("blocked_reason") == "reservation_expired"


def test_quality_review_prewarm_reconciliation_fails_closed_without_live_owner(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    cases = {
        "req-dead-owner": {
            "owner_pid": 2**22 + 12345,
            "owner_pid_start_ticks": 1,
        },
        "req-mismatched-owner": {
            "owner_pid": os.getpid(),
            "owner_pid_start_ticks": 1,
        },
        "req-missing-owner": {},
    }
    for request_id, extra in cases.items():
        event = {
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "quality_review",
            "adapter_id": "claude_cli",
            "state": "starting",
            "reservation_expires_at_epoch": time.time() - 60,
            "preparation_phase": "reviewer_source_graph_prewarm_started",
        }
        event.update(extra)
        manager._append_event(event)

    reconciled = manager._reconcile_expired_starting_reservations()

    assert reconciled == 3
    for request_id in cases:
        latest = manager._latest_by_request()[request_id]
        assert latest.get("state") == "blocked"
        assert latest.get("blocked_reason") == "reservation_expired"


def test_quality_review_prewarm_live_fails_closed_on_unknown_identity(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "req-prewarm-unknown"
    # A positive owner pid whose start-ticks are missing yields UNKNOWN
    # identity evidence.  Live prewarm ownership requires an exact MATCH, so
    # UNKNOWN must fail closed rather than be treated as a live owned build.
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
        "reservation_expires_at_epoch": time.time() - 60,
        "preparation_phase": "reviewer_source_graph_prewarm_started",
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": None,
    })

    assert manager._reviewer_source_graph_prewarm_live(request_id) is False

    reconciled = manager._reconcile_expired_starting_reservations()

    assert reconciled == 1
    latest = manager._latest_by_request()[request_id]
    assert latest.get("state") == "blocked"
    assert latest.get("blocked_reason") == "reservation_expired"


def _quality_review_card(task_id: str = "TASK_REVIEW_1") -> dict:
    return {
        "task_id": task_id,
        "runner": "claude_worker_reviewer",
        "topic": "quality_review",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "allowed_writes": [],
        "read_only": True,
        "priority": "high",
    }


def _reviewer_launch_setup(tmp_path: Path, monkeypatch):
    """Shared scaffolding for the real-``_launch_isolated`` reviewer ordering
    tests below.  Mocks only what a synthetic reviewer launch cannot
    reasonably exercise in a unit test -- the task-engine claim, and the
    git-backed candidate-overlay worktree diffing inside
    ``create_quality_review_workspace`` -- while calling the actual
    ``ProcessManager._launch_isolated`` method, so the ordering it enforces
    (workspace+packet creation, then authority verification, then prewarm,
    then runtime/provider registration) is exercised for real rather than
    reimplemented in the test.
    """

    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _quality_review_card()),
        argv=[sys.executable, "-c", "pass"],
    )
    monkeypatch.setattr(
        process_launcher.task_engine, "claim_start_exact",
        lambda *a, **k: {"ok": True},
    )
    monkeypatch.setattr(
        process_launcher, "_task_authority_repo", lambda repo, card: repo.resolve()
    )
    # The reviewer ordering under test is workspace+packet creation, then
    # authority, then prewarm, then runtime/provider registration -- it is not
    # host-sandbox selection.  ``_launch_isolated`` resolves the OS sandbox
    # backend before that ordering, and ``select_sandbox_backend`` legitimately
    # raises on a host with no bubblewrap/landlock (e.g. macOS CI), aborting the
    # launch before the ordering runs and leaving ``order`` empty.  Pin a fixed
    # backend -- an orthogonal dependency like the mocks above -- so the ordering
    # is exercised identically on every platform, exactly as it already is on
    # Linux where a real sandbox is present.
    monkeypatch.setattr(
        process_launcher, "_sandbox_backend_for_adapter", lambda adapter_id: "bubblewrap"
    )
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    candidate_home = tmp_path / "candidate_home"
    (candidate_home / "task_mcp_worker_runtime").mkdir(parents=True)
    fake_workspace = process_launcher.WorkerWorkspace(
        request_id="c" * 32,
        repo=candidate_dir,
        path=candidate_dir,
        home=candidate_home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    monkeypatch.setattr(
        process_launcher, "create_quality_review_workspace",
        lambda *a, **k: (fake_workspace, {"schema_id": "fake.v1"}),
    )
    binding = {
        "target_request_id": "target-request-1",
        "target_task_id": "TARGET_TASK_1",
        "target_claim_epoch": 1,
        "adapter_id": "claude_cli",
        "source_workspace": fake_workspace.as_metadata(),
        "candidate_paths": ["module.py"],
        "packet": {"packet_sha256": "a" * 64},
        "lens": "correctness",
    }
    return manager, binding


def test_quality_review_launch_isolated_orders_authority_prewarm_registration(
    monkeypatch, tmp_path,
):
    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    order: list[str] = []

    def fake_authority(authority_repo):
        order.append("authority")
        return worker_ai_tools_mcp.AuthorityBinding(
            db_path=tmp_path / "canonical.sqlite",
            authority_source="canonical",
            authority_state="sole_authority",
            authority_repo=authority_repo,
        )

    def fake_prewarm(*_args, **_kwargs):
        order.append("prewarm")
        return {"ok": True, "built": True}

    def fake_registration(*_args, **_kwargs):
        order.append("registration")
        raise RuntimeError(
            "stop-after-registration: real subprocess spawn is out of scope for this ordering test"
        )

    monkeypatch.setattr(
        worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", fake_authority
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp, "prewarm_quality_review_source_graph", fake_prewarm
    )
    monkeypatch.setattr(
        process_launcher, "_provision_worker_mcp_runtime_for_authority", fake_registration
    )

    manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
    )

    assert order == ["authority", "prewarm", "registration"]


def test_quality_review_launch_isolated_fails_closed_before_prewarm_and_registration(
    monkeypatch, tmp_path,
):
    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    order: list[str] = []

    def fail_authority(authority_repo):
        order.append("authority")
        raise worker_ai_tools_mcp.WorkerToolError(
            "authority_component_not_canonical_active:source_graph.source_graph:shadow"
        )

    def fake_prewarm(*_args, **_kwargs):
        order.append("prewarm")
        return {"ok": True, "built": True}

    def fake_registration(*_args, **_kwargs):
        order.append("registration")
        raise RuntimeError("registration must never run after a failed authority check")

    monkeypatch.setattr(
        worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", fail_authority
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp, "prewarm_quality_review_source_graph", fake_prewarm
    )
    monkeypatch.setattr(
        process_launcher, "_provision_worker_mcp_runtime_for_authority", fake_registration
    )

    result = manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
    )

    assert order == ["authority"]
    assert result.get("ok") is False
    assert "quality_review_source_graph_authority_unverified" in str(
        result.get("blocked_reason") or ""
    )


def test_quality_review_launch_isolated_classifies_prewarm_data_failure_truthfully(
    monkeypatch, tmp_path,
):
    """A Source Graph contract/data failure surfaced from the prewarm call
    (as ``worker_ai_tools_mcp.prewarm_quality_review_source_graph`` now always
    raises ``WorkerToolError`` for its own clone/backup/index/schema
    failures) must be classified as a truthful, expected
    ``quality_review_source_graph_prewarm_failed`` launch block -- never
    folded into the generic ``unexpected_launch_error`` bucket reserved for
    real provider-launch anomalies -- and must never reach runtime/provider
    registration.
    """

    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    order: list[str] = []

    def fake_authority(authority_repo):
        order.append("authority")
        return SimpleNamespace(
            authority_source="canonical", authority_state="sole_authority",
        )

    def fail_prewarm(*_args, **_kwargs):
        order.append("prewarm")
        raise worker_ai_tools_mcp.WorkerToolError(
            "quality_review_candidate_source_graph_prewarm_error:"
            "OperationalError:disk I/O error"
        )

    def fake_registration(*_args, **_kwargs):
        order.append("registration")
        raise RuntimeError("registration must never run after a failed prewarm")

    monkeypatch.setattr(
        worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", fake_authority
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp, "prewarm_quality_review_source_graph", fail_prewarm
    )
    monkeypatch.setattr(
        process_launcher, "_provision_worker_mcp_runtime_for_authority", fake_registration
    )

    result = manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
    )

    assert order == ["authority", "prewarm"]
    assert result.get("ok") is False
    reason = str(result.get("blocked_reason") or "")
    assert reason.startswith(
        "quality_review_source_graph_prewarm_failed:"
        "quality_review_candidate_source_graph_prewarm_error:"
    )
    assert "unexpected_launch_error" not in reason
    assert "diagnostic" not in result


def test_crash_retry_packet_carries_unsanitized_diagnostics_and_hashes_delivered(
    tmp_path: Path,
) -> None:
    """fix #4: the predecessor diagnostics are embedded in a JSON packet, so
    JSON encoding already neutralises every metacharacter. The HTML-oriented
    live-output sanitiser must not run: it escaped and redacted bytes the
    successor needs verbatim, and the tail hashes were computed over the
    unsanitised bytes, so the corruption was undetectable. Carry the bytes
    unescaped/unredacted and hash exactly the bytes delivered."""

    repo = tmp_path / "repo"
    process_dir = tmp_path / "processes"
    worktree = tmp_path / "succ" / "worktree"
    home = tmp_path / "succ" / "home"
    for directory in (repo, process_dir, worktree, home):
        directory.mkdir(parents=True)
    workspace = process_launcher.WorkerWorkspace(
        request_id="7" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    predecessor = "8" * 32
    process_launcher.write_json_0600(
        process_dir / f"{predecessor}.request.json",
        {"request_id": predecessor, "task_id": "TASK_SAME", "workspace": {"repo": str(repo)}},
    )
    process_launcher.write_json_0600(
        process_dir / f"{predecessor}.supervisor.json",
        {"state": "supervisor_error", "exit_code": 1, "error": "boom"},
    )
    # HTML metacharacters plus a long opaque token the live-output sanitiser
    # would escape/redact. Kept short so the whole stream is the delivered tail.
    long_token = "SECRET" + "x" * 80
    stdout_text = f"<step> a & b {long_token}\n"
    stderr_text = f'trace <b>"boom"</b> {long_token}\n'
    (process_dir / f"{predecessor}.stdout.log").write_text(stdout_text, encoding="utf-8")
    (process_dir / f"{predecessor}.stderr.log").write_text(stderr_text, encoding="utf-8")
    overlay = {
        "predecessor_request_id": predecessor,
        "predecessor_task_id": "TASK_SAME",
        "canonical_digest": "c" * 64,
    }

    _path, packet = process_launcher._materialize_crash_retry_packet(
        process_dir,
        workspace,
        task_id="TASK_SAME",
        card={"rework_predecessor": {"request_id": predecessor}},
        rework_overlay_packet=overlay,
    )

    assert packet is not None
    # Verbatim: no HTML escaping and no long-token redaction.
    assert packet["stdout_tail"] == stdout_text
    assert packet["stderr_tail"] == stderr_text
    assert long_token in packet["stdout_tail"]
    assert "&amp;" not in packet["stdout_tail"]
    assert "&quot;" not in packet["stderr_tail"]
    # The tail hashes cover exactly the bytes delivered in the packet.
    assert (
        packet["stdout_tail_sha256"]
        == hashlib.sha256(packet["stdout_tail"].encode("utf-8")).hexdigest()
    )
    assert (
        packet["stderr_tail_sha256"]
        == hashlib.sha256(packet["stderr_tail"].encode("utf-8")).hexdigest()
    )


def _finalize_retry_manager(tmp_path: Path):
    from aiworkhub import worker_workspace

    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="review")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "f" * 32
    worktree = tmp_path / "ws"
    home = tmp_path / "ws-home"
    worktree.mkdir()
    home.mkdir()
    workspace = process_launcher.WorkerWorkspace(
        request_id=request_id,
        repo=manager.repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    stdout_path = tmp_path / f"{request_id}.stdout.log"
    stderr_path = tmp_path / f"{request_id}.stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    status_path = manager.process_dir / f"{request_id}.supervisor.json"
    metadata_path = manager.process_dir / f"{request_id}.request.json"
    # A non-exited terminal state takes the light failure finalization path,
    # which still runs the shared usage-recording block being exercised here.
    worker_workspace.write_json_0600(status_path, {"state": "cancelled"})
    metadata = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(tmp_path / f"{request_id}.cancel.json"),
        "workspace": workspace.as_metadata(),
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return manager, request_id, metadata_path, status_path


def test_release_pending_retry_records_provider_token_spend(monkeypatch, tmp_path):
    """fix #8: a release_pending predecessor is a finalization-pending state
    that never recorded provider spend. On the retry the spend must be recorded,
    not lost by reusing an empty prior-usage record."""

    manager, request_id, metadata_path, status_path = _finalize_retry_manager(tmp_path)
    # A release_pending predecessor carried NO recorded usage, then a retry
    # appended its ``finalizing`` event (as retry_finalization does).
    common = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "pid": 999_999_999,
        "pid_start_ticks": 1,
    }
    manager._append_event({**common, "state": "release_pending"})
    manager._append_event(
        {**common, "state": "finalizing", "finalization_retry": True}
    )

    recorded: list[str] = []

    def fake_record_usage(request_id_arg, *_args, **_kwargs):
        recorded.append(request_id_arg)
        return {"input_tokens": 123, "output_tokens": 45}, True, ""

    monkeypatch.setattr(manager, "_record_usage", fake_record_usage)
    monkeypatch.setattr(
        manager, "_terminal_failure_exact", lambda *a, **k: {"ok": True, "stderr": ""}
    )
    monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *a, **k: None)

    event = manager._finalize_isolated_request(request_id, 0)

    assert recorded == [request_id]
    assert event["usage_recorded"] is True
    assert event["usage"]["input_tokens"] == 123


def test_finalization_retry_reuses_prior_recorded_usage(monkeypatch, tmp_path):
    """A retry whose predecessor already recorded usage must reuse it (no
    double count), never re-record."""

    manager, request_id, metadata_path, status_path = _finalize_retry_manager(tmp_path)
    common = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "pid": 999_999_999,
        "pid_start_ticks": 1,
    }
    manager._append_event(
        {
            **common,
            "state": "finalize_failed",
            "usage": {"input_tokens": 7},
            "usage_recorded": True,
        }
    )
    manager._append_event(
        {**common, "state": "finalizing", "finalization_retry": True}
    )

    monkeypatch.setattr(
        manager,
        "_record_usage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not re-record already-recorded usage")
        ),
    )
    monkeypatch.setattr(
        manager, "_terminal_failure_exact", lambda *a, **k: {"ok": True, "stderr": ""}
    )
    monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *a, **k: None)

    event = manager._finalize_isolated_request(request_id, 0)

    assert event["usage_recorded"] is True
    assert event["usage"]["input_tokens"] == 7


def test_terminal_rework_delta_evidence_seals_changed_and_deleted_paths(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "changed.py").write_bytes(b"changed\n")
    workspace = SimpleNamespace(repo=repo, path=worktree)
    captured = {}

    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "configured_runtime_root",
        lambda authority_repo: authority_repo / ".aiworkhub" / "runtime",
    )

    def seal(authority_repo, task_id, request_id, claim_epoch, entries, artifact_dir):
        captured.update(
            authority_repo=authority_repo,
            task_id=task_id,
            request_id=request_id,
            claim_epoch=claim_epoch,
            entries=list(entries),
            artifact_dir=artifact_dir,
        )
        return {"path": str(artifact_dir / "packet.json"), "digest": "a" * 64}

    monkeypatch.setattr(
        process_launcher._worker_workspace, "seal_rework_delta_artifact", seal
    )

    evidence = process_launcher._terminal_rework_delta_evidence(
        workspace,
        {"task_id": "TASK-DELTA", "claim_epoch": 3},
        "a" * 32,
        ["src/changed.py", "src/deleted.py"],
    )

    assert evidence == {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(repo.resolve()),
        "task_id": "TASK-DELTA",
        "request_id": "a" * 32,
        "claim_epoch": 3,
        "artifact_path": str(repo / ".aiworkhub/runtime/rework_deltas/packet.json"),
        "artifact_sha256": "a" * 64,
    }
    assert captured["authority_repo"] == repo.resolve()
    assert captured["entries"] == [
        ("src/changed.py", b"changed\n"),
        ("src/deleted.py", None),
    ]
    assert captured["artifact_dir"] == repo / ".aiworkhub/runtime/rework_deltas"


@pytest.mark.parametrize("claim_epoch", [True, 0, "1", None])
def test_terminal_rework_delta_evidence_rejects_invalid_claim_epoch(
    monkeypatch, tmp_path, claim_epoch
):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    (worktree / "changed.py").write_bytes(b"changed")
    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "seal_rework_delta_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid identity must not seal")
        ),
    )

    evidence = process_launcher._terminal_rework_delta_evidence(
        SimpleNamespace(repo=repo, path=worktree),
        {"task_id": "TASK-DELTA", "claim_epoch": claim_epoch},
        "b" * 32,
        ["changed.py"],
    )

    assert evidence == {
        "schema_id": "aiworkhub.rework_delta_seal.v1",
        "sealed": False,
        "reason": "rework_delta_identity_invalid",
    }


def test_terminal_rework_delta_evidence_reports_seal_failure_without_descriptor(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    (worktree / "changed.py").write_bytes(b"changed")
    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "configured_runtime_root",
        lambda authority_repo: authority_repo / ".aiworkhub" / "runtime",
    )
    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "seal_rework_delta_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            process_launcher.WorkspaceError("synthetic_failure")
        ),
    )

    evidence = process_launcher._terminal_rework_delta_evidence(
        SimpleNamespace(repo=repo, path=worktree),
        {"task_id": "TASK-DELTA", "claim_epoch": 2},
        "c" * 32,
        ["changed.py"],
    )

    assert evidence["sealed"] is False
    assert evidence["reason"] == "rework_delta_seal_failed:synthetic_failure"
    assert "artifact_path" not in evidence
