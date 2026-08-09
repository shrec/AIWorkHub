"""B512: fail-closed GC for retained-for-review isolated worker workspaces
that outlive coordinator finalization.

Regression this closes: ``ProcessManager._finalize_isolated_request`` sets
``cleanup=False`` on purpose for every non-success terminal state (see
``process_launcher.py``'s "Coordinator review-first" comment) so a human/
Codex reviewer can inspect exactly what a failed worker did before its git
worktree + isolated HOME are deleted. Nothing ever revisited those retained
directories afterward: once Codex ran ``taskctl done``/``archive`` on the
task, the workspace stayed on disk forever -- a disk leak, one retained
worktree per validation_failed/cancelled/scope_rejected/worker_failed/
timed_out/promotion_conflict/finalize_failed run.

This file exercises ``ProcessManager._gc_finalized_workspace(s)`` (wired into
``ProcessManager.reconcile()``, which ``task_reconciler.py``'s scan loop
already drives on every interval) and
``worker_workspace.assert_gc_safe_workspace_shape``: GC fires only once ALL
of (a) the process event is a genuine terminal, non-blocked state with
``workspace_retained`` still true, (b) the exact PID/start-tick identity is
PROVEN dead (never inferred from "alive with unknown ticks"), (c) the
canonical task has disposed the exact attempt or a review card names a newer
request id (never the current review request and never processing), and (d)
the workspace path/home are exactly
``<configured_root>/<request_id>/{worktree,home}``. Every other case is
skipped and reported, never deleted.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
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


def _ensure_project_context_stub() -> None:
    """Some isolated Task MCP worktrees lack ``project_context.py`` entirely
    -- it is an uncommitted sibling file in the parent repo that this task's
    ``read_first``/``allowed_writes`` never declared, so worktree seeding
    never copies it in. ``process_launcher.py`` imports it unconditionally
    at module scope; only install a stub when the real module is genuinely
    unimportable, and only enough surface for that import to succeed (these
    GC tests never exercise project-context collection)."""
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.project_context")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.project_context")

    class ProjectContextError(Exception):
        pass

    class ProjectContextResult:
        def __init__(self, metadata=None, prompt_bundle: str = "") -> None:
            self.metadata = metadata or {}
            self.prompt_bundle = prompt_bundle

    def collect_project_context(repo, card):  # noqa: ANN001, ARG001
        return None

    stub.ProjectContextError = ProjectContextError
    stub.ProjectContextResult = ProjectContextResult
    stub.collect_project_context = collect_project_context
    stub.RECEIPT_SCHEMA_ID = "aiworkhub.task_mcp.project_context_receipt.v1"
    sys.modules["aiworkhub.project_context"] = stub


_ensure_deepseek_credentials_stub()
_ensure_project_context_stub()

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


def _spawn_sleeper(seconds: float = 30.0) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _kill_if_alive(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _card(task_id: str = "TASK_B512", runner: str = "claude_worker_b512") -> dict:
    return {
        "task_id": task_id,
        "runner": runner,
        "topic": "task_mcp",
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": runner,
        "archived_at": "",
        "allowed_writes": ["out/result.txt"],
    }


def _show(card: dict):
    def show(task_id: str) -> dict:
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _collision(**_kwargs) -> dict:
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


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


def _seed_gc_candidate(
    manager: process_launcher.ProcessManager,
    tmp_path: Path,
    card: dict,
    *,
    request_id: str,
    pid: int,
    pid_start_ticks,
    state: str,
    workspace_retained: bool = True,
    create_dirs: bool = True,
    root: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Seed one persisted request whose retained workspace physically exists
    at the exact ``<root>/<request_id>/{worktree,home}`` shape GC expects."""
    root = root or (tmp_path / "wtroot")
    path = root / request_id / "worktree"
    home = root / request_id / "home"
    if create_dirs:
        path.mkdir(parents=True, exist_ok=True)
        (path / "evidence.txt").write_text("retained worker output")
        home.mkdir(parents=True, exist_ok=True)
        (home / "evidence.txt").write_text("retained home artifact")

    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = process_dir / f"{request_id}.request.json"
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "model": None,
        "stdout_path": str(process_dir / f"{request_id}.stdout.log"),
        "stderr_path": str(process_dir / f"{request_id}.stderr.log"),
        "supervisor_status_path": str(process_dir / f"{request_id}.supervisor.json"),
        "cancel_path": str(process_dir / f"{request_id}.cancel.json"),
        "metadata_path": str(metadata_path),
        "validation": [],
        "sandbox_backend": "landlock",
        "workspace": {
            "request_id": request_id,
            "repo": str(repo),
            "path": str(path),
            "home": str(home),
            "allowed_writes": list(card["allowed_writes"]),
            "parent_baseline": {},
            "workspace_baseline": {},
        },
    })
    manager._append_event({
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": state,
        "pid": pid,
        "pid_start_ticks": pid_start_ticks,
        "metadata_path": str(metadata_path),
        "workspace_retained": workspace_retained,
    })
    return path, home, metadata_path


# --- finalized cleanup -------------------------------------------------------


def test_finalized_task_with_dead_process_is_gc_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-finalized-dead",
        pid=2_147_483_050, pid_start_ticks=999_999_900,
        state="validation_failed",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not path.exists()
    assert not home.exists()
    events = manager._request_events("req-finalized-dead")
    latest = events[-1]
    assert latest["state"] == "validation_failed", "terminal outcome must stay unchanged"
    assert latest["workspace_gc"] is True
    assert latest["workspace_retained"] is False

    # A second scan must be a total no-op: the request no longer looks like
    # a retained candidate (workspace_retained is now False).
    result2 = manager._gc_finalized_workspaces()
    assert result2 == {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0}
    assert len(manager._request_events("req-finalized-dead")) == len(events)


def test_archived_task_with_dead_process_is_gc_cleaned(tmp_path, monkeypatch):
    """``core._lifecycle_state`` never checks ``archived_at`` -- the GC must
    use the fixed local ``_canonical_task_status`` helper instead, or an
    archived task's retained workspace would never become eligible."""
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({
        "status": "finished", "worker_status": "done", "claimed_by": "",
        "archived_at": "2026-07-16T00:00:00Z",
    })
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-archived-dead",
        pid=2_147_483_051, pid_start_ticks=999_999_901,
        state="cancelled",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not path.exists() and not home.exists()
    assert manager._request_events("req-archived-dead")[-1]["workspace_gc_reason"] == (
        "disposed_task_status:archived"
    )


@pytest.mark.parametrize(
    "state", ["worker_failed", "scope_rejected", "promotion_conflict", "finalize_failed", "timed_out"]
)
def test_every_retained_terminal_state_is_gc_eligible_once_finished(tmp_path, monkeypatch, state):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id=f"req-{state}",
        pid=2_147_483_060, pid_start_ticks=999_999_910,
        state=state,
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not path.exists() and not home.exists()


# --- review / blocked / active preservation ---------------------------------


@pytest.mark.parametrize(
    "status,worker_status",
    [("processing", "in_progress")],
)
def test_active_task_status_preserves_the_workspace(tmp_path, monkeypatch, status, worker_status):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": status, "worker_status": worker_status})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id=f"req-preserve-{status}",
        pid=2_147_483_061, pid_start_ticks=999_999_911,
        state="worker_failed",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists()
    # No audit event is appended for a routine "not yet eligible" skip -- only
    # a genuine GC action gets a persisted event (see module docstring).
    latest = manager._request_events(f"req-preserve-{status}")[-1]
    assert "workspace_gc" not in latest


@pytest.mark.parametrize(
    "status,worker_status",
    [("pending", "unclaimed"), ("blocked", "blocked")],
)
def test_coordinator_disposed_attempt_is_gc_cleaned(tmp_path, monkeypatch, status, worker_status):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": status, "worker_status": worker_status})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id=f"req-disposed-{status}",
        pid=2_147_483_071, pid_start_ticks=999_999_921,
        state="worker_failed",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not path.exists() and not home.exists()
    latest = manager._request_events(f"req-disposed-{status}")[-1]
    assert latest["workspace_gc_reason"] == f"disposed_task_status:{status}"


def test_pending_rework_predecessor_is_pinned_until_successor_claim(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    request_id = "req-pinned-rework"
    card = _card()
    card.update({
        "status": "pending",
        "worker_status": "unclaimed",
        "rework_predecessor": {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "request_id": request_id,
        },
    })
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id=request_id,
        pid=2_147_483_075, pid_start_ticks=999_999_925,
        state="validation_failed",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists()
    assert "workspace_gc" not in manager._request_events(request_id)[-1]


def test_blocked_rework_predecessor_survives_failed_successor(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    request_id = "req-pinned-rework-after-successor-failure"
    card = _card()
    card.update({
        "status": "blocked",
        "worker_status": "timed_out",
        "rework_predecessor": {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "request_id": request_id,
        },
    })
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id=request_id,
        pid=2_147_483_076, pid_start_ticks=999_999_926,
        state="validation_failed",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists()
    assert "workspace_gc" not in manager._request_events(request_id)[-1]


def test_current_review_request_is_preserved_but_older_attempt_is_gc_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    current_request = "req-current-review"
    older_request = "req-older-review"
    card = _card()
    card.update({
        "status": "review",
        "worker_status": "review",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"request_identity": {"request_id": current_request}},
        },
    })
    manager = _build_manager(tmp_path, card)
    current_path, current_home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id=current_request,
        pid=2_147_483_072, pid_start_ticks=999_999_922,
        state="review_ready",
    )
    older_path, older_home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id=older_request,
        pid=2_147_483_073, pid_start_ticks=999_999_923,
        state="validation_failed",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 2, "gc_cleaned": 1, "gc_skipped": 1}
    assert current_path.exists() and current_home.exists()
    assert not older_path.exists() and not older_home.exists()
    assert manager._request_events(older_request)[-1]["workspace_gc_reason"] == (
        f"superseded_review_request:{current_request}"
    )


def test_review_without_exact_request_identity_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "review", "worker_status": "review", "terminal_review": {}})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-review-missing-identity",
        pid=2_147_483_074, pid_start_ticks=999_999_924,
        state="validation_failed",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists()


def test_blocked_process_event_is_never_a_gc_candidate_even_if_task_finished(tmp_path, monkeypatch):
    """Defense in depth: "blocked" is excluded from GC_CANDIDATE_PROCESS_STATES
    even though it is a member of TERMINAL_PROCESS_STATES, because a launch
    rejection never has a retained workspace worth reconsidering."""
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-blocked-never",
        pid=2_147_483_062, pid_start_ticks=999_999_912,
        state="blocked",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0}
    assert path.exists() and home.exists()


@pytest.mark.parametrize("state", ["review_pending", "release_pending", "reconcile_pending"])
def test_finalization_pending_states_are_never_gc_candidates(tmp_path, monkeypatch, state):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id=f"req-{state}",
        pid=2_147_483_063, pid_start_ticks=999_999_913,
        state=state,
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0}
    assert path.exists() and home.exists()


def test_request_still_tracked_live_in_memory_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-live-in-memory",
        pid=2_147_483_064, pid_start_ticks=999_999_914,
        state="worker_failed",
    )
    fake_process = SimpleNamespace(poll=lambda: None)
    manager._live["req-live-in-memory"] = process_launcher._LiveProcess(
        request_id="req-live-in-memory",
        task_id=card["task_id"], runner=card["runner"], topic=card["topic"],
        adapter_id="claude_cli", model=None,
        process=fake_process,
        stdout_path=tmp_path / "o.log", stderr_path=tmp_path / "e.log",
        started_at="now", timeout_seconds=10, isolated=True,
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0}
    assert path.exists() and home.exists()


# --- PID reuse / start-tick protection ---------------------------------------


def test_live_process_with_matching_start_ticks_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    proc = _spawn_sleeper()
    try:
        ticks = process_launcher._pid_start_ticks(proc.pid)
        path, home, _ = _seed_gc_candidate(
            manager, tmp_path, card,
            request_id="req-live-pid",
            pid=proc.pid, pid_start_ticks=ticks,
            state="worker_failed",
        )

        result = manager._gc_finalized_workspaces()

        assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
        assert path.exists() and home.exists()
    finally:
        _kill_if_alive(proc)


def test_live_process_with_unknown_start_ticks_is_not_proven_dead(tmp_path, monkeypatch):
    """Unlike ``_pid_matches`` (used for cancel/liveness, biased toward
    "still the same process"), GC uses the opposite fail-closed bias: an
    alive PID with NO recorded start ticks is not proof of death either."""
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    proc = _spawn_sleeper()
    try:
        path, home, _ = _seed_gc_candidate(
            manager, tmp_path, card,
            request_id="req-live-pid-no-ticks",
            pid=proc.pid, pid_start_ticks=None,
            state="worker_failed",
        )

        result = manager._gc_finalized_workspaces()

        assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
        assert path.exists() and home.exists()
    finally:
        _kill_if_alive(proc)


def test_pid_reused_with_mismatched_start_ticks_is_proven_dead_and_gc_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    proc = _spawn_sleeper()
    try:
        real_ticks = process_launcher._pid_start_ticks(proc.pid)
        path, home, _ = _seed_gc_candidate(
            manager, tmp_path, card,
            request_id="req-pid-reused",
            pid=proc.pid, pid_start_ticks=real_ticks + 1,
            state="worker_failed",
        )

        result = manager._gc_finalized_workspaces()

        assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
        assert not path.exists() and not home.exists()
    finally:
        _kill_if_alive(proc)


# --- unsafe path rejection ----------------------------------------------------


def test_workspace_outside_configured_root_is_rejected_and_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    _, _, metadata_path = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-unsafe-path",
        pid=2_147_483_054, pid_start_ticks=999_999_915,
        state="validation_failed",
    )

    # Tamper the metadata to point outside the configured worktree root --
    # e.g. a stale/edited record, or an attempt to smuggle an arbitrary path
    # through the request.json.
    rogue_root = tmp_path / "definitely_not_the_worktree_root" / "req-unsafe-path"
    rogue_path = rogue_root / "worktree"
    rogue_home = rogue_root / "home"
    rogue_path.mkdir(parents=True)
    rogue_home.mkdir(parents=True)
    (rogue_path / "sensitive.txt").write_text("must-survive")
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["workspace"]["path"] = str(rogue_path)
    payload["workspace"]["home"] = str(rogue_home)
    worker_workspace.write_json_0600(metadata_path, payload)

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert rogue_path.exists() and rogue_home.exists()
    assert (rogue_path / "sensitive.txt").exists()


def test_assert_gc_safe_workspace_shape_rejects_a_home_worktree_swap(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    root = worker_workspace.configured_worktree_root()
    request_id = "req-swap-shape"
    path = root / request_id / "worktree"
    home = root / request_id / "home"
    with pytest.raises(worker_workspace.WorkspaceError):
        # home/worktree swapped -- an exact-shape mismatch even though both
        # paths are under the correct root and request_id.
        worker_workspace.assert_gc_safe_workspace_shape(request_id, home, path)


# --- missing directory idempotence -------------------------------------------


def test_missing_workspace_directories_are_idempotent_gc_success(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-already-gone",
        pid=2_147_483_055, pid_start_ticks=999_999_916,
        state="scope_rejected",
        create_dirs=False,
    )
    assert not path.exists() and not home.exists()

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    latest = manager._request_events("req-already-gone")[-1]
    assert latest["workspace_gc"] is True

    # Calling it again must remain a pure no-op (workspace_retained is False now).
    result2 = manager._gc_finalized_workspaces()
    assert result2 == {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0}


# --- fail-closed on ambiguity -------------------------------------------------


def test_missing_metadata_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, metadata_path = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-no-metadata",
        pid=2_147_483_065, pid_start_ticks=999_999_917,
        state="worker_failed",
    )
    metadata_path.unlink()

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists()


def test_task_lookup_failure_fails_closed_and_preserves_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-lookup-fails",
        pid=2_147_483_066, pid_start_ticks=999_999_918,
        state="worker_failed",
    )
    manager._show_task = lambda task_id: {"returncode": 1, "stdout": "", "stderr": "boom"}

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists()


# --- unchanged callback / review lifecycle -----------------------------------


def test_gc_never_invokes_review_or_release_callbacks_and_leaves_task_card_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    calls: list[tuple] = []
    monkeypatch.setattr(
        process_launcher.core, "mark_review",
        lambda *a, **k: calls.append(("mark_review", a, k)),
    )
    monkeypatch.setattr(
        process_launcher.core, "release_launch",
        lambda *a, **k: calls.append(("release_launch", a, k)),
    )
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-no-callback",
        pid=2_147_483_056, pid_start_ticks=999_999_919,
        state="promotion_conflict",
    )
    before = dict(card)

    result = manager._gc_finalized_workspaces()

    assert result["gc_cleaned"] == 1
    assert not path.exists() and not home.exists()
    assert calls == []
    assert card == before


def test_reconcile_wires_gc_into_the_existing_reconciliation_entry_point(tmp_path, monkeypatch):
    """The one required integration point: ``task_reconciler.py``'s scan
    loop calls ``ProcessManager.reconcile()`` on an interval -- GC must ride
    that SAME call, not a separate cron/daemon."""
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    manager = _build_manager(tmp_path, card)
    path, home, _ = _seed_gc_candidate(
        manager, tmp_path, card,
        request_id="req-via-reconcile",
        pid=2_147_483_057, pid_start_ticks=999_999_920,
        state="timed_out",
    )

    result = manager.reconcile()

    assert result["ok"] is True
    assert result["gc_cleaned"] == 1
    assert not path.exists() and not home.exists()
