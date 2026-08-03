"""B894: narrowly scoped, one-task terminal-transition authority for the
detached supervisor's reconciliation finalize path.

Regression: ``_finalize_isolated_request``'s successful-exit branch required
``core.writes_allowed()`` -- an ambient, process-wide flag -- even just to
reach Codex review. That flag is only ever set in the process actively
handling an MCP request; it is gone by the time a later reconciliation scan
(a different, detached process) observes a clean exit after the launcher has
already exited. Every successful worker outcome then stalled forever at
``review_pending`` with error ``write_gate_closed_during_reconciliation`` --
never reaching review, never falling back to pending either, just stuck.

The fix: a launch that WAS authorized (both ``AIWORKHUB_ALLOW_LAUNCH`` and
``AIWORKHUB_ALLOW_WRITES`` were "1" at that exact moment) mints a narrowly
scoped, single-use, HMAC-signed grant bound to the exact
``(repo, task_id, runner, topic, request_id)`` tuple. A later finalize -- in
this same process or a brand-new one -- consumes it in place of the ambient
flag. The grant never widens to any other task, repo, or runner, and never
survives a second use.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    """See test_process_launcher_security.py's identical helper: some
    isolated Task MCP worktrees are missing ``deepseek_credentials.py``
    entirely (an uncommitted file on the trusted host). Only installs a
    stub when the real module is genuinely unimportable."""
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    def adapter_readiness(repo=None):  # noqa: ANN001, ARG001
        return {"ok": True, "readonly": True, "adapters": []}

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    stub.adapter_readiness = adapter_readiness
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import process_launcher, worker_workspace  # noqa: E402


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_process_launcher_security.py's identical fixture docstring:
    neutralizes ``os.chmod``/``os.fchmod`` ONLY when this exact sandbox
    genuinely rejects the bare syscall (probed once); a no-op elsewhere."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _card(task_id: str = "TASK_B894", runner: str = "claude_worker_b894") -> dict:
    return {
        "task_id": task_id,
        "runner": runner,
        "topic": "coding",
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": runner,
        "review_requested_by": "",
        "allowed_writes": ["out/result.txt"],
    }


def _show(card: dict):
    def show(task_id: str) -> dict:
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _collision(**_kwargs) -> dict:
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)


def _build_manager(tmp_path: Path, card: dict) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=lambda **_k: SimpleNamespace(argv=[], cwd=str(tmp_path), launchable=True, reason=""),
        isolation_enabled=True,
    )


def _seed_request(
    manager: process_launcher.ProcessManager,
    tmp_path: Path,
    card: dict,
    *,
    request_id: str,
    supervisor_pid: int,
    supervisor_ticks,
    supervisor_status: dict,
) -> Path:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status_path = process_dir / f"{request_id}.supervisor.json"
    metadata_path = process_dir / f"{request_id}.request.json"
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    cancel_path = process_dir / f"{request_id}.cancel.json"
    for p in (stdout_path, stderr_path):
        os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
    _write_status(status_path, supervisor_status)

    workspace_metadata = {
        "request_id": request_id,
        "repo": str(tmp_path / "repo"),
        "path": str(tmp_path / "workspace" / request_id),
        "home": str(tmp_path / "home" / request_id),
        "allowed_writes": list(card["allowed_writes"]),
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "model": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(cancel_path),
        "metadata_path": str(metadata_path),
        "validation": [],
        "sandbox_backend": "landlock",
        "workspace": workspace_metadata,
    })
    manager._append_event({
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": supervisor_pid,
        "pid_start_ticks": supervisor_ticks,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })
    return metadata_path


def _seed_exited_request(manager, tmp_path, card, *, request_id: str) -> Path:
    dead_pid = 2_147_483_100
    dead_ticks = 999_999_900
    return _seed_request(
        manager, tmp_path, card,
        request_id=request_id,
        supervisor_pid=dead_pid,
        supervisor_ticks=dead_ticks,
        supervisor_status={
            "state": "exited",
            "exit_code": 0,
            "child_pid": dead_pid,
            "child_pid_start_ticks": dead_ticks,
            "started_at_epoch": time.time() - 60,
            "finished_at_epoch": time.time() - 10,
            "heartbeat_seq": 2,
            "heartbeat_at_epoch": time.time() - 10,
        },
    )


def _grant_authority(manager, *, repo, task_id, runner, topic, request_id) -> Path:
    path = manager._terminal_authority_grant_path(request_id)
    process_launcher._write_terminal_authority_grant(
        path,
        manager._terminal_authority_key(),
        repo=repo,
        task_id=task_id,
        runner=runner,
        topic=topic,
        request_id=request_id,
    )
    return path


def _patch_success_pipeline(monkeypatch, card, calls: dict | None = None) -> None:
    calls = calls if calls is not None else {}
    calls.setdefault("promote", 0)
    calls.setdefault("mark_review", 0)
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda workspace: ["out/result.txt"])
    monkeypatch.setattr(
        process_launcher, "run_validations",
        lambda workspace, commands, **_kw: [{"command": c, "returncode": 0} for c in commands],
    )

    def fake_promote(workspace, changed):
        calls["promote"] += 1
        return list(changed)

    def fake_mark_review(task_id, runner=None, topic=None):
        calls["mark_review"] += 1
        card.update({"status": "review", "worker_status": "review", "review_requested_by": runner})
        return {"ok": True}

    def fake_mark_terminal_review(repo, task_id, runner, substatus, *, evidence=None):
        # B895: when ambient writes are closed, the success path transitions
        # through this repository-bound task_engine.mark_terminal_review
        # instead of the ambient-gated core.mark_review -- both fakes feed
        # the same counter since either may fire depending on which of
        # ambient-writes/the one-shot grant authorized this finalize.
        calls["mark_review"] += 1
        card.update({"status": "review", "worker_status": "review", "review_requested_by": runner})
        return {"ok": True, "substatus": substatus, "evidence": evidence}

    monkeypatch.setattr(process_launcher, "promote", fake_promote)
    monkeypatch.setattr(process_launcher.core, "mark_review", fake_mark_review)
    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_review", fake_mark_terminal_review)


# --- positive: exact one-task grant reaches review with no ambient writes ---


def test_valid_one_task_grant_reaches_review_without_ambient_writes(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)
    calls: dict = {}
    _patch_success_pipeline(monkeypatch, card, calls)

    request_id = "req-authority-ok"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    grant_path = _grant_authority(
        manager, repo=manager.repo, task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_ready"
    # Canonical promotion now happens only in the coordinator's
    # accept_review path, not during finalize.
    assert calls == {"promote": 0, "mark_review": 1}
    assert card["status"] == "review"
    # Single-use: the grant is consumed (deleted) on first use.
    assert not grant_path.is_file()


def test_grant_survives_across_a_fresh_process_manager_instance(tmp_path, monkeypatch):
    """Simulates the exact regression: the launcher mints the grant, then
    exits (a brand-new ``ProcessManager`` instance stands in for a fresh
    process), and only that later instance performs the finalize."""
    card = _card()
    launcher = _build_manager(tmp_path, card)
    request_id = "req-cross-process"
    _seed_exited_request(launcher, tmp_path, card, request_id=request_id)
    _grant_authority(
        launcher, repo=launcher.repo, task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )

    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)
    _patch_success_pipeline(monkeypatch, card)
    # ProcessManager construction immediately reconciles persisted requests;
    # install the terminal-review fake before creating the fresh instance.
    reconciler = _build_manager(tmp_path, card)

    result = reconciler._finalize_isolated_request(request_id)

    assert result["state"] == "review_ready"
    assert card["status"] == "review"


def test_second_finalize_scan_after_authority_consumption_is_a_pure_noop(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)
    calls: dict = {}
    _patch_success_pipeline(monkeypatch, card, calls)

    request_id = "req-replay-noop"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    _grant_authority(
        manager, repo=manager.repo, task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )

    first = manager._finalize_isolated_request(request_id)
    assert first["state"] == "review_ready"
    assert calls == {"promote": 0, "mark_review": 1}

    second = manager._finalize_isolated_request(request_id)
    assert second["state"] == "review_ready"
    assert calls == {"promote": 0, "mark_review": 1}


# --- hostile replay: wrong task / wrong repo / tampered signature ----------


def test_grant_scoped_to_a_different_task_id_is_rejected(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)

    request_id = "req-wrong-task"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    grant_path = _grant_authority(
        manager, repo=manager.repo, task_id="TASK_OTHER", runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_pending"
    assert "write_gate_closed_during_reconciliation" in result["error"]
    assert not grant_path.is_file()
    assert card["status"] != "pending"


def test_grant_scoped_to_a_different_repo_is_rejected(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)

    request_id = "req-wrong-repo"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    _grant_authority(
        manager, repo=tmp_path / "some-other-repo", task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_pending"
    assert "write_gate_closed_during_reconciliation" in result["error"]
    assert card["status"] != "pending"


def test_grant_scoped_to_a_different_runner_is_rejected(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)

    request_id = "req-wrong-runner"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    _grant_authority(
        manager, repo=manager.repo, task_id=card["task_id"], runner="claude_worker_impostor",
        topic=card["topic"], request_id=request_id,
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_pending"
    assert card["status"] != "pending"


def test_grant_with_tampered_signature_is_rejected(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)

    request_id = "req-tampered"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    grant_path = _grant_authority(
        manager, repo=manager.repo, task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )
    payload = json.loads(grant_path.read_text(encoding="utf-8"))
    payload["signature"] = "0" * len(payload["signature"])
    worker_workspace.write_json_0600(grant_path, payload)

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_pending"
    assert not grant_path.is_file()
    assert card["status"] != "pending"


def test_grant_for_a_different_request_id_is_rejected(tmp_path, monkeypatch):
    """A grant minted for one launch episode must not authorize a
    differently-identified episode of the same task, even by the same
    runner/repo -- the request_id itself is part of the signed scope."""
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)

    request_id = "req-scope-mismatch"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    # Mint a grant that is internally self-consistent (valid signature) but
    # scoped to a DIFFERENT request_id -- exercises signature validity
    # combined with an identity mismatch, not just a corrupted signature.
    other_grant_path = manager._terminal_authority_grant_path("req-someone-elses-episode")
    process_launcher._write_terminal_authority_grant(
        other_grant_path,
        manager._terminal_authority_key(),
        repo=manager.repo,
        task_id=card["task_id"],
        runner=card["runner"],
        topic=card["topic"],
        request_id="req-someone-elses-episode",
    )
    # Present it under this request's own grant path (a forged/misplaced copy).
    grant_path = manager._terminal_authority_grant_path(request_id)
    worker_workspace.write_json_0600(
        grant_path, json.loads(other_grant_path.read_text(encoding="utf-8"))
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_pending"
    assert card["status"] != "pending"


# --- no grant + ambient closed: fails closed, never returns to pending -----


def test_missing_grant_and_closed_ambient_gate_stalls_at_review_pending_never_pending(
    tmp_path, monkeypatch,
):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)

    request_id = "req-no-authority"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_pending"
    assert "write_gate_closed_during_reconciliation" in result["error"]
    assert card["status"] != "pending"
    assert card["worker_status"] != "unclaimed"


# --- failure outcomes never need the grant or ambient writes ---------------


def test_failure_outcome_reaches_blocked_without_ambient_writes_or_any_grant(tmp_path, monkeypatch):
    """A no-candidate failure is terminal/blocked, never review work."""
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)
    failure_calls = []

    def fake_mark_terminal_failure(
        repo, task_id, runner, substatus, *, evidence=None, request_id=""
    ):
        failure_calls.append((task_id, runner, substatus, request_id, evidence))
        card.update({"status": "blocked", "worker_status": substatus, "claimed_by": runner})
        return {"ok": True}

    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_failure", fake_mark_terminal_failure
    )

    request_id = "req-timeout-no-grant"
    dead_pid = 2_147_483_050
    _seed_request(
        manager, tmp_path, card,
        request_id=request_id,
        supervisor_pid=dead_pid,
        supervisor_ticks=999_999_950,
        supervisor_status={
            "state": "timed_out",
            "exit_code": 124,
            "started_at_epoch": time.time() - 30,
            "finished_at_epoch": time.time() - 5,
        },
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "timed_out"
    assert [(call[0], call[1], call[2], call[3]) for call in failure_calls] == [
        (card["task_id"], card["runner"], "timed_out", request_id)
    ]
    assert failure_calls[0][4]["required_outputs"] == []
    assert card["status"] == "blocked"
    assert card["worker_status"] == "timed_out"


@pytest.mark.parametrize("operation", ["archived", "superseded"])
def test_failure_outcome_preserves_already_finalized_task(
    tmp_path, monkeypatch, operation,
):
    """A late child exit is process evidence, never authority to resurrect
    a task that the manager already archived or superseded."""
    card = _card()
    card.update({
        "archived_at": "2026-07-30T00:00:00+00:00",
        "archive_operation": operation,
    })
    manager = _build_manager(tmp_path, card)
    failure_calls = []
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *args, **kwargs: failure_calls.append((args, kwargs)) or {"ok": True},
    )

    request_id = f"req-already-{operation}"
    dead_pid = 2_147_483_051
    _seed_request(
        manager,
        tmp_path,
        card,
        request_id=request_id,
        supervisor_pid=dead_pid,
        supervisor_ticks=999_999_949,
        supervisor_status={
            "state": "timed_out",
            "exit_code": 124,
            "started_at_epoch": time.time() - 30,
            "finished_at_epoch": time.time() - 5,
        },
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "timed_out"
    assert result["error"] == ""
    assert result["release_transition_ok"] is True
    assert result["canonical_lifecycle"] == "archived"
    assert result["terminal_review_disposition"] == (
        f"terminal_skipped_already_finalized:{operation}"
    )
    assert failure_calls == []
    assert card["archived_at"]
    assert card["archive_operation"] == operation


def test_ambient_writes_alone_still_authorizes_and_consumes_any_stray_grant(tmp_path, monkeypatch):
    """The ambient flag remains a valid path on its own (e.g. a reconciler
    daemon process that legitimately keeps AIWORKHUB_ALLOW_WRITES=1 for its
    whole lifetime) -- and a grant is consumed regardless, so it is never
    left as a stale, replayable artifact just because ambient writes already
    authorized this finalize."""
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)
    calls: dict = {}
    _patch_success_pipeline(monkeypatch, card, calls)

    request_id = "req-ambient-only"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    grant_path = _grant_authority(
        manager, repo=manager.repo, task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_ready"
    assert calls == {"promote": 0, "mark_review": 1}
    assert not grant_path.is_file()
