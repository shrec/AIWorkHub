"""B895: repair the one exact defect the coordinator reproduced in the B894
terminal-authority foundation.

Regression: a valid one-shot terminal grant (see B894) correctly authorized
``_finalize_isolated_request``'s success branch to promote worker output --
but the branch then called ``core.mark_review``, which re-checks the ambient,
process-wide ``AIWORKHUB_ALLOW_WRITES`` flag through
``core._canonical_write_gate``. That flag is gone by the time a detached
reconciler (a different, later process than the one that launched the
request) performs the finalize, so ``core.mark_review`` always rejected and
the request stalled at ``review_pending`` forever with
``review_transition_failed`` -- even though the exact-scoped grant had
already authorized this precise ``(repo, task_id, runner, topic,
request_id)`` tuple.

The fix: route the successful terminal transition through the same
repository-bound, non-ambient ``task_engine.mark_terminal_review`` the
failure branch already used (via ``_review_terminal_exact``), carrying full
changed/promoted/validation/required-output evidence -- never setting
``AIWORKHUB_ALLOW_WRITES``, never widening to a general write bypass, and
never weakening the one-shot/replay/cross-repo grant protections B894 already
established.
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
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _card(task_id: str = "TASK_B895", runner: str = "claude_worker_b895") -> dict:
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


def _seed_exited_request(
    manager: process_launcher.ProcessManager,
    tmp_path: Path,
    card: dict,
    *,
    request_id: str,
) -> Path:
    dead_pid = 2_147_483_100
    dead_ticks = 999_999_900
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status_path = process_dir / f"{request_id}.supervisor.json"
    metadata_path = process_dir / f"{request_id}.request.json"
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    cancel_path = process_dir / f"{request_id}.cancel.json"
    for p in (stdout_path, stderr_path):
        os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
    _write_status(status_path, {
        "state": "exited",
        "exit_code": 0,
        "child_pid": dead_pid,
        "child_pid_start_ticks": dead_ticks,
        "started_at_epoch": time.time() - 60,
        "finished_at_epoch": time.time() - 10,
        "heartbeat_seq": 2,
        "heartbeat_at_epoch": time.time() - 10,
    })

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
        "pid": dead_pid,
        "pid_start_ticks": dead_ticks,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })
    return metadata_path


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


def _forbid_ambient_mark_review(monkeypatch) -> None:
    """Fail the test hard if the ambient-gated path is ever reached again --
    the whole point of B895 is that success no longer depends on it."""

    def _boom(*_a, **_k):
        raise AssertionError("core.mark_review must not be called by the success path (B895)")

    monkeypatch.setattr(process_launcher.core, "mark_review", _boom)


# --- acceptance: valid exact grant reaches review_ready with no ambient writes


def test_valid_exact_grant_reaches_review_ready_via_task_engine_not_core_mark_review(
    tmp_path, monkeypatch,
):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.delenv(process_launcher.ALLOW_WRITES_ENV, raising=False)
    monkeypatch.delenv(process_launcher.ALLOW_LAUNCH_ENV, raising=False)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)
    _forbid_ambient_mark_review(monkeypatch)

    monkeypatch.setattr(process_launcher, "enforce_scope", lambda workspace: ["out/result.txt"])
    monkeypatch.setattr(
        process_launcher, "run_validations",
        lambda workspace, commands, **_kw: [{"command": c, "returncode": 0} for c in commands],
    )
    promote_calls: list[list[str]] = []
    monkeypatch.setattr(
        process_launcher, "promote",
        lambda workspace, changed: (promote_calls.append(list(changed)) or list(changed)),
    )

    terminal_calls: list[dict] = []

    def fake_mark_terminal_review(repo, task_id, runner, substatus, *, evidence=None):
        terminal_calls.append({
            "repo": repo, "task_id": task_id, "runner": runner,
            "substatus": substatus, "evidence": evidence,
        })
        card.update({"status": "review", "worker_status": "review", "claimed_by": runner})
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_review", fake_mark_terminal_review)

    request_id = "req-b895-ok"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    grant_path = _grant_authority(
        manager, repo=manager.repo, task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_ready"
    assert card["status"] == "review"
    # Review-before-promotion: a successful worker exit reaches review_ready
    # purely on evidence collected from the isolated workspace -- it never
    # promotes into the canonical repo itself, so promote() is never called.
    assert promote_calls == []
    # Exactly one non-ambient terminal-review transition, scoped to this task.
    assert len(terminal_calls) == 1
    call = terminal_calls[0]
    assert call["repo"] == manager.repo
    assert call["task_id"] == card["task_id"]
    assert call["runner"] == card["runner"]
    assert call["substatus"] == "review_ready"
    # Successful review evidence includes changed-path and validation records.
    evidence = call["evidence"]
    assert evidence["changed_paths"] == ["out/result.txt"]
    assert evidence["validation"] == []
    assert evidence["required_outputs"] == []
    assert evidence["request_identity"] == {
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
    }
    # No environment write-flag mutation: the ambient gate is still closed.
    assert os.environ.get(process_launcher.ALLOW_WRITES_ENV) is None
    assert not grant_path.is_file()


# --- acceptance: invalid tampered/replayed grant cannot promote or review --


def test_tampered_grant_cannot_promote_or_reach_review(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.delenv(process_launcher.ALLOW_WRITES_ENV, raising=False)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)
    _forbid_ambient_mark_review(monkeypatch)

    promote_calls: list[list[str]] = []
    monkeypatch.setattr(
        process_launcher, "promote",
        lambda workspace, changed: (promote_calls.append(list(changed)) or list(changed)),
    )
    terminal_calls: list[dict] = []
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_review",
        lambda *a, **k: (terminal_calls.append((a, k)) or {"ok": True}),
    )

    request_id = "req-b895-tampered"
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
    assert "write_gate_closed_during_reconciliation" in result["error"]
    assert card["status"] != "review"
    assert card["status"] != "pending"
    assert promote_calls == []
    assert terminal_calls == []
    assert not grant_path.is_file()

    # A second scan cannot replay the already-consumed (deleted) grant either.
    second = manager._finalize_isolated_request(request_id)
    assert second["state"] == "review_pending"
    assert promote_calls == []
    assert terminal_calls == []


def test_replayed_grant_after_successful_consumption_is_rejected(tmp_path, monkeypatch):
    """A grant already consumed by one successful finalize must never
    authorize a second, separately-seeded request even if an attacker
    resurrects an identical-looking grant file for it."""
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.delenv(process_launcher.ALLOW_WRITES_ENV, raising=False)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)
    _forbid_ambient_mark_review(monkeypatch)
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda workspace: ["out/result.txt"])
    monkeypatch.setattr(
        process_launcher, "run_validations",
        lambda workspace, commands, **_kw: [{"command": c, "returncode": 0} for c in commands],
    )
    monkeypatch.setattr(process_launcher, "promote", lambda workspace, changed: list(changed))
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_review",
        lambda repo, task_id, runner, substatus, *, evidence=None: {"ok": True},
    )

    request_id = "req-b895-first"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    original_grant = _grant_authority(
        manager, repo=manager.repo, task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )
    first = manager._finalize_isolated_request(request_id)
    assert first["state"] == "review_ready"
    assert not original_grant.is_file()

    # Replay attempt: write a grant scoped to a NEW request_id but reusing
    # the exact same (repo, task_id, runner, topic) tuple. The signature is
    # only valid for the original request_id, so re-presenting it under a
    # different request_id's path must fail closed.
    replay_card = _card()
    replay_request_id = "req-b895-replay"
    _seed_exited_request(manager, tmp_path, replay_card, request_id=replay_request_id)
    replay_grant_path = manager._terminal_authority_grant_path(replay_request_id)
    # original_grant no longer exists on disk (consumed by the first
    # finalize) -- forge one at the NEW request's path that is internally
    # well-signed but scoped to the OLD request_id, using the same key.
    process_launcher._write_terminal_authority_grant(
        replay_grant_path,
        manager._terminal_authority_key(),
        repo=manager.repo,
        task_id=card["task_id"],
        runner=card["runner"],
        topic=card["topic"],
        request_id=request_id,  # wrong scope: belongs to the first episode
    )

    result = manager._finalize_isolated_request(replay_request_id)

    assert result["state"] == "review_pending"
    assert not replay_grant_path.is_file()


# --- acceptance: no environment write flag mutation or general bypass -----


def test_finalize_never_sets_ambient_allow_writes_env(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.delenv(process_launcher.ALLOW_WRITES_ENV, raising=False)
    monkeypatch.delenv(process_launcher.ALLOW_LAUNCH_ENV, raising=False)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: False)
    _forbid_ambient_mark_review(monkeypatch)
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda workspace: ["out/result.txt"])
    monkeypatch.setattr(
        process_launcher, "run_validations",
        lambda workspace, commands, **_kw: [{"command": c, "returncode": 0} for c in commands],
    )
    monkeypatch.setattr(process_launcher, "promote", lambda workspace, changed: list(changed))
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_review",
        lambda repo, task_id, runner, substatus, *, evidence=None: {"ok": True},
    )

    request_id = "req-b895-no-env-mutation"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    _grant_authority(
        manager, repo=manager.repo, task_id=card["task_id"], runner=card["runner"],
        topic=card["topic"], request_id=request_id,
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "review_ready"
    # The success path must never mutate the ambient gate itself to
    # authorize its own review transition -- it stays exactly as it was.
    assert process_launcher.ALLOW_WRITES_ENV not in os.environ
    assert process_launcher.ALLOW_LAUNCH_ENV not in os.environ
    assert process_launcher.core.writes_allowed() is False
