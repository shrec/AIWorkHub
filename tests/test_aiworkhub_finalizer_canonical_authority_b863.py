"""B863: one canonical task authority for worker claim/finalization.

Regression this closes (the reproduced B860/B861 failure): a successfully
completed isolated worker still legitimately owned its exact canonical claim,
but ``ProcessManager._exact_claim_state``/``_gc_finalized_workspace`` read
task state through ``core.show_task`` -> ``core.repo_root()`` -- an
independently, ambiently re-resolved repository (``AIWORKHUB_REPO`` env, or a
nested-repository walk-up that could escape into an outer checkout) that can
disagree with ``ProcessManager.repo``, the exact repository the worker's
isolated workspace was actually launched against. The disagreement produced a
false ``claim_ownership_lost``, and ``_finalize_isolated_request`` then
deleted the still-valid worktree immediately and unconditionally on that
error -- destroying the only evidence of a successful run.

Three independent fixes, each covered below:

1. ``ProcessManager._default_show_task`` binds every internal claim/
   finalization read to ``self.repo`` explicitly (via the new
   ``task_engine.show_task``), never to an ambiently re-resolved repo.
2. ``repository_state._find_upward``/``resolve_repository_root`` stop the
   manifest search at the nearest enclosing git repository, so a nested
   independent repository (its own ``.git``) is never treated as untracked
   content of whatever outer repository happens to contain it.
3. ``_finalize_isolated_request`` no longer unconditionally deletes the
   workspace on a ``claim_ownership_lost`` error -- every failure retains the
   workspace; only the canonical-status-gated sweep in
   ``_gc_finalized_workspace`` may delete it, and only once the task's
   canonical row is confirmed ``finished``/``archived``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    """See test_finalized_workspace_gc_b512_v1.py's identical helper: some
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
    """See test_finalized_workspace_gc_b512_v1.py's identical helper."""
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

from aiworkhub import process_launcher, repository_state, task_store, worker_workspace  # noqa: E402


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
    """See test_finalized_workspace_gc_b512_v1.py's identical fixture docstring."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_task_row(repo: Path, *, task_id: str, runner: str, topic: str, status: str,
                    worker_status: str, claimed_by: str, card_json_extra: dict | None = None,
                    archived_at: str = "") -> None:
    """Bootstrap ``repo``'s canonical task_store (if needed) and insert/replace
    one row directly -- the same durable authority ``task_store.get_task``
    reads, bypassing any card-shaped subprocess/CLI layer entirely."""
    readiness = task_store.storage_readiness(repo)
    if not readiness.ready:
        task_store.initialize_repository(repo)
        readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    card_json = {"task_id": task_id, **(card_json_extra or {})}
    now = _utcnow()
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, card_json, "
            "created_at, updated_at, claimed_by, claimed_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET runner=excluded.runner, topic=excluded.topic, "
            "status=excluded.status, worker_status=excluded.worker_status, "
            "card_json=excluded.card_json, updated_at=excluded.updated_at, "
            "claimed_by=excluded.claimed_by, archived_at=excluded.archived_at",
            (task_id, runner, topic, status, worker_status, json.dumps(card_json),
             now, now, claimed_by, now, archived_at),
        )
        conn.commit()
    finally:
        conn.close()


# --- 1. task_store canonical-row authority (guard rail) ---------------------


def test_task_store_row_overrides_stale_card_json_claim_field(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_task_row(
        repo, task_id="TASK_B863_ROW", runner="claude_worker_b863", topic="task_mcp",
        status="processing", worker_status="in_progress", claimed_by="claude_worker_b863",
        card_json_extra={"claimed_by": "some_stale_legacy_runner", "status": "review"},
    )
    card = task_store.get_task(repo, "TASK_B863_ROW")
    assert card is not None
    # The canonical SQLite row -- not the stale card_json copy -- decides.
    assert card["claimed_by"] == "claude_worker_b863"
    assert card["status"] == "processing"


# --- 2. nested independent repository binding --------------------------------


def test_nested_independent_repository_binds_to_its_own_root_not_outer(tmp_path):
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    outer_manifest_dir = outer / repository_state.HUB_DIRNAME
    outer_manifest_dir.mkdir()
    outer_manifest = repository_state.RepositoryManifest(
        repo_id=repository_state.new_repo_id(), repo_name="outer", created_at=_utcnow(),
    )
    (outer_manifest_dir / "project.json").write_text(
        json.dumps(outer_manifest.to_json()), encoding="utf-8",
    )

    nested = outer / "tools" / "geoai-task-mcp"
    (nested / ".git").mkdir(parents=True)

    # No manifest under `nested` yet: with the pre-fix unbounded walk-up,
    # this would keep climbing past `nested`'s own `.git` and resolve to the
    # outer repo's manifest instead -- treating the nested independent repo
    # as mere untracked content of the outer one.
    resolved = repository_state.resolve_repository_root(cwd=nested, require_manifest=False)
    assert resolved == nested.resolve()
    assert resolved != outer.resolve()


def test_non_nested_repository_resolution_is_unchanged(tmp_path):
    """The common (no nesting) case must resolve exactly as before: the
    manifest at the enclosing git root is found normally."""
    repo = tmp_path / "solo"
    (repo / ".git").mkdir(parents=True)
    hub = repo / repository_state.HUB_DIRNAME
    hub.mkdir()
    manifest = repository_state.RepositoryManifest(
        repo_id=repository_state.new_repo_id(), repo_name="solo", created_at=_utcnow(),
    )
    (hub / "project.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")

    sub = repo / "some" / "nested" / "cwd"
    sub.mkdir(parents=True)
    resolved = repository_state.resolve_repository_root(cwd=sub, require_manifest=False)
    assert resolved == repo.resolve()


# --- 3 & 4. canonical claim authority + retain-on-failure --------------------


def _metadata_and_events(manager, tmp_path, *, request_id, task_id, runner, topic,
                          exit_code: int = 0, supervisor_state: str = "exited"):
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = process_dir / f"{request_id}.request.json"
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    status_path = process_dir / f"{request_id}.supervisor.json"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    worktree_root = tmp_path / "wtroot"
    os.environ[worker_workspace.WORKTREE_ROOT_ENV] = str(worktree_root)
    path = worktree_root / request_id / "worktree"
    home = worktree_root / request_id / "home"
    path.mkdir(parents=True, exist_ok=True)
    (path / "evidence.txt").write_text("still-valid worker output", encoding="utf-8")
    home.mkdir(parents=True, exist_ok=True)

    worker_workspace.write_json_0600(status_path, {
        "state": supervisor_state,
        "exit_code": exit_code,
        "supervisor_pid": 2_147_483_070,
        "supervisor_pid_start_ticks": 999_999_930,
    })
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "claude_cli",
        "model": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(process_dir / f"{request_id}.cancel.json"),
        "metadata_path": str(metadata_path),
        "validation": [],
        "required_outputs": [],
        "sandbox_backend": "landlock",
        "workspace": {
            "request_id": request_id,
            "repo": str(manager.repo),
            "path": str(path),
            "home": str(home),
            "allowed_writes": ["out/result.txt"],
            "parent_baseline": {},
            "workspace_baseline": {},
        },
    })
    manager._append_event({
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": 2_147_483_070,
        "pid_start_ticks": 999_999_930,
        "metadata_path": str(metadata_path),
    })
    return path, home, metadata_path


def test_default_show_task_reads_the_bound_repo_not_an_ambient_one(tmp_path, monkeypatch):
    """Reproduces the B860/B861 root cause directly: a worker's canonical
    claim genuinely still lives in ``self.repo``, but the ambient repo an
    old-style lookup would independently resolve (``AIWORKHUB_REPO``) points
    somewhere else entirely (here: nonexistent). Without the fix,
    ``core.show_task`` would consult the ambient repo and see no such task ->
    false ``claim_ownership_lost``.
    """
    bound_repo = tmp_path / "bound_repo"
    bound_repo.mkdir()
    ambient_repo = tmp_path / "ambient_repo_never_initialized"
    monkeypatch.setenv("AIWORKHUB_REPO", str(ambient_repo))

    _seed_task_row(
        bound_repo, task_id="TASK_B863_BOUND", runner="claude_worker_b863", topic="task_mcp",
        status="processing", worker_status="in_progress", claimed_by="claude_worker_b863",
    )

    manager = process_launcher.ProcessManager(
        repo=bound_repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        collision_guard=lambda **_: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        isolation_enabled=False,
    )
    # No show_task override: exercises the real default, bound to self.repo.
    state = manager._exact_claim_state({
        "task_id": "TASK_B863_BOUND", "runner": "claude_worker_b863", "topic": "task_mcp",
    })
    assert state == "processing"


def test_finalize_retains_workspace_on_claim_ownership_lost_instead_of_deleting_it(tmp_path, monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    # Canonical row exists but is claimed by a DIFFERENT runner than the one
    # in this request's metadata -- this is exactly what makes
    # `_exact_claim_state` raise `claim_ownership_lost`, whether that
    # disagreement is a genuine reassignment or a false-positive read race.
    _seed_task_row(
        repo, task_id="TASK_B863_RETAIN", runner="claude_worker_b863", topic="task_mcp",
        status="processing", worker_status="in_progress", claimed_by="a_different_runner",
    )

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        collision_guard=lambda **_: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        isolation_enabled=True,
    )
    path, home, _ = _metadata_and_events(
        manager, tmp_path,
        request_id="req-b863-retain", task_id="TASK_B863_RETAIN",
        runner="claude_worker_b863", topic="task_mcp",
    )

    event = manager._finalize_isolated_request("req-b863-retain")

    assert event is not None
    assert event["error"].startswith("claim_ownership_lost")
    assert event["workspace_retained"] is True, "a claim_ownership_lost read must never delete the workspace outright"
    assert path.exists() and (path / "evidence.txt").exists(), "valid worker output must survive a false/ambiguous ownership read"
    assert home.exists()

    # A second finalize call must not re-delete anything either -- the event
    # is already terminal.
    again = manager._finalize_isolated_request("req-b863-retain")
    assert again["state"] == event["state"]
    assert path.exists()


def test_gc_still_waits_for_confirmed_canonical_terminal_status_after_retain(tmp_path, monkeypatch):
    """Chains directly off the retain behavior above: even after finalize
    retains the workspace, the dedicated GC sweep must not clean it up until
    the canonical row is independently confirmed finished/archived."""
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_task_row(
        repo, task_id="TASK_B863_GC", runner="claude_worker_b863", topic="task_mcp",
        status="processing", worker_status="in_progress", claimed_by="a_different_runner",
    )

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        collision_guard=lambda **_: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        isolation_enabled=True,
    )
    path, home, _ = _metadata_and_events(
        manager, tmp_path,
        request_id="req-b863-gc", task_id="TASK_B863_GC",
        runner="claude_worker_b863", topic="task_mcp",
    )
    manager._finalize_isolated_request("req-b863-gc")
    assert path.exists() and home.exists()

    # Canonical status is still "processing" (owned by someone else, or a
    # stale read) -- GC must skip it, not delete it.
    result = manager._gc_finalized_workspaces()
    assert result["gc_cleaned"] == 0
    assert path.exists() and home.exists()

    # Only once the canonical row is independently confirmed finished/
    # archived may GC ever remove it -- and the proven-dead pid used by
    # _metadata_and_events (2_147_483_070) makes the liveness check pass.
    _seed_task_row(
        repo, task_id="TASK_B863_GC", runner="claude_worker_b863", topic="task_mcp",
        status="finished", worker_status="done", claimed_by="",
    )
    result2 = manager._gc_finalized_workspaces()
    assert result2["gc_cleaned"] == 1
    assert not path.exists() and not home.exists()
