"""NF421: a quarantine is bookkeeping; it must not rename the adjudicated
outcome or claim a quarantined workspace is gone.

Reproduces the live 0.10.80 defect (request 254cf14ae10d4f9cb16631dfe03fc6b3):
a finalized request had already recorded ``validation_failed`` -- the true,
useful outcome naming which required outputs never changed.  Seconds later the
retention sweep in ``ProcessManager._gc_finalized_workspace`` found the review
workspace's sealed hashes absent, quarantined the bytes (correct fail-closed
behaviour), and then appended a terminal ledger event that

  1. renamed the run's ``state`` to ``finalize_failed`` -- so the ledger and the
     card's ``terminal_substatus`` disagreed and the reason a manager needs was
     no longer terminal; and
  2. set ``workspace_retained`` false while the very same event recorded a
     quarantine path holding the whole worktree -- so a reader checking
     retention concluded the evidence was gone when it was one lookup away.

The fix keeps the adjudicated outcome as the terminal state, carries the
quarantine as bookkeeping on the same event, and adds an explicit
``workspace_disposition`` distinguishing ``retained_in_place`` / ``quarantined``
/ ``removed`` so retention is truthful.  A finalization that fails for a reason
OTHER than an already-adjudicated validation result still reports
``finalize_failed``.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    """Some isolated Task MCP worktrees are missing ``deepseek_credentials.py``
    (an uncommitted file on the trusted host).  Only stubs when genuinely
    unimportable -- see test_aiworkhub_finalizer_canonical_authority_b863.py."""
    import importlib

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
    import importlib

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

# A proven-dead identity: a PID far above any live process, paired with an
# equally implausible start-tick, so ``_process_proven_dead`` passes.
_DEAD_PID = 2_147_483_070
_DEAD_TICKS = 999_999_930


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
    """chmod/chown are denied under the worker sandbox; the retention helpers
    only tighten permissions defensively, so bridging them keeps the behaviour
    under test reachable without weakening any assertion."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _build(tmp_path: Path, *, request_id: str, card: dict) -> tuple:
    """Build a ProcessManager whose ledger already holds one retained,
    proven-dead ``validation_failed`` finalization for ``request_id`` and whose
    canonical card is exactly ``card``.  Returns (manager, path, home, event)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)

    worktree_root = tmp_path / "wtroot"
    os.environ[worker_workspace.WORKTREE_ROOT_ENV] = str(worktree_root)
    path = worktree_root / request_id / "worktree"
    home = worktree_root / request_id / "home"
    path.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    (path / "evidence.txt").write_text("adjudicated validation output", encoding="utf-8")

    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = process_dir / f"{request_id}.request.json"

    task_id = str(card["task_id"])
    runner = "claude_worker_nf421"
    topic = "task_mcp"

    workspace_meta = {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(path),
        "home": str(home),
        # A non-empty allowed_writes keeps this off the read-only special case
        # so an absent hash map is honestly ``review_workspace_hashes_missing``.
        "allowed_writes": ["out/result.txt"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "claude_cli",
        "metadata_path": str(metadata_path),
        "validation": [],
        "required_outputs": [],
        "sandbox_backend": "landlock",
        "workspace": workspace_meta,
    })

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        show_task=lambda _tid: {"returncode": 0, "stdout": json.dumps(card)},
        collision_guard=lambda **_: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        isolation_enabled=True,
    )

    event = {
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "claude_cli",
        "state": "validation_failed",
        "workspace_retained": True,
        "pid": _DEAD_PID,
        "pid_start_ticks": _DEAD_TICKS,
        "metadata_path": str(metadata_path),
    }
    manager._append_event(event)
    return manager, path, home, event


def _review_card(task_id: str, request_id: str, path: Path, home: Path, repo: Path) -> dict:
    """A card still in review, adjudicated ``validation_failed``, whose current
    terminal_review names ``request_id`` -- exactly the state that drives the
    retention sweep into the quarantine branch."""
    return {
        "task_id": task_id,
        "status": "review",
        "worker_status": "review",
        "terminal_substatus": "validation_failed",
        "allowed_writes": ["out/result.txt"],
        "terminal_review": {
            "evidence": {
                "request_identity": {"request_id": request_id},
                "workspace": {
                    "request_id": request_id,
                    "repo": str(repo),
                    "path": str(path),
                    "home": str(home),
                    "allowed_writes": ["out/result.txt"],
                    "parent_baseline": {},
                    "workspace_baseline": {},
                },
            },
        },
    }


def test_quarantine_keeps_validation_failed_outcome_and_reports_bytes_present(
    tmp_path, monkeypatch,
):
    request_id = "254cf14ae10d4f9cb16631dfe03fc6b3"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    path = tmp_path / "wtroot" / request_id / "worktree"
    home = tmp_path / "wtroot" / request_id / "home"
    card = _review_card("TASK_NF421_KEEP", request_id, path, home, repo)

    manager, path, home, event = _build(tmp_path, request_id=request_id, card=card)
    # Blocking the unusable review is a separate module's concern; stub it so
    # the exact card the manager reads keeps its adjudicated terminal_substatus.
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_review_workspace_missing",
        lambda *a, **k: {"ok": True, "callback_enqueued": False},
    )

    result = manager._gc_finalized_workspace(request_id, event)

    # The quarantine still happened (no fail-closed behaviour lost).
    assert result is not None and result.get("quarantined") is True
    assert not path.exists(), "bytes must leave the live tree when quarantined"

    latest = manager._latest_by_request()[request_id]

    # 1. The adjudicated outcome stays terminal -- NOT renamed to finalize_failed.
    assert latest["state"] == "validation_failed"
    assert latest["error"].startswith("retained_workspace_quarantined")

    # 2. Retention is truthful: disposition says the bytes were quarantined, and
    #    the recorded path still holds the whole worktree -- never "removed" and
    #    never a bare unretained claim.
    assert latest["workspace_disposition"] == "quarantined"
    assert latest.get("workspace_disposition") != "removed"
    # workspace_retained is truthfully True (not merely "not False"): every
    # existing reader tests this key by truthiness, so a bare drop would read
    # as unretained to the GC path and to _finalization_retry's response.
    assert latest["workspace_retained"] is True
    assert latest["workspace_quarantined"] is True
    quarantine_dir = Path(latest["workspace_quarantine_path"])
    assert (quarantine_dir / "worktree" / "evidence.txt").exists(), (
        "a quarantined workspace's bytes are one lookup away, not gone"
    )

    # 3. The card's terminal_substatus and the ledger's terminal event agree.
    card_after = json.loads(manager._show_task("TASK_NF421_KEEP")["stdout"])
    assert card_after["terminal_substatus"] == latest["state"] == "validation_failed"


def test_genuine_finalization_failure_still_reports_finalize_failed(tmp_path, monkeypatch):
    request_id = "0123456789abcdef0123456789abcdef"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    path = tmp_path / "wtroot" / request_id / "worktree"
    home = tmp_path / "wtroot" / request_id / "home"
    card = _review_card("TASK_NF421_FINFAIL", request_id, path, home, repo)

    manager, path, home, event = _build(tmp_path, request_id=request_id, card=card)
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_review_workspace_missing",
        lambda *a, **k: {"ok": True, "callback_enqueued": False},
    )

    def _raise(*_a, **_k):
        raise process_launcher.WorkspaceError("quarantine_move_failed")

    monkeypatch.setattr(process_launcher, "quarantine_review_workspace", _raise)

    result = manager._gc_finalized_workspace(request_id, event)

    assert result is not None and result.get("gc") is False
    assert "quarantine_failed" in result["reason"]

    latest = manager._latest_by_request()[request_id]
    # A finalization that fails for a reason OTHER than an already-adjudicated
    # validation result keeps reporting finalize_failed: the change does not
    # hide genuine finalization defects.
    assert latest["state"] == "finalize_failed"
    # The bytes never moved, so retention truthfully reports them in place.
    assert latest["workspace_disposition"] == "retained_in_place"
    assert latest["workspace_retained"] is True
    assert path.exists()


def test_purged_workspace_reports_removed_disposition(tmp_path):
    request_id = "fedcba9876543210fedcba9876543210"
    card = {
        "task_id": "TASK_NF421_PURGE",
        "status": "finished",
        "worker_status": "done",
    }

    manager, path, home, event = _build(tmp_path, request_id=request_id, card=card)

    result = manager._gc_finalized_workspace(request_id, event)

    assert result is not None and result.get("gc") is True
    assert not path.exists() and not home.exists()

    latest = manager._latest_by_request()[request_id]
    # Removal is the third, distinct disposition -- and the adjudicated outcome
    # is preserved on the purge event too.
    assert latest["workspace_disposition"] == "removed"
    assert latest["state"] == "validation_failed"


def test_gc_cleanup_failure_reports_retained_in_place(tmp_path, monkeypatch):
    """The eligible purge branch whose ``cleanup_workspace`` raises must append a
    terminal retention event saying the bytes are still here -- disposition
    ``retained_in_place`` with ``workspace_retained`` True -- exactly as the
    review-retained cleanup-failure branch already does.  A branch that returns
    ``cleanup_failed`` with no event leaves a reader unable to tell from the
    ledger alone that the workspace survived: the same gap the disposition
    vocabulary exists to close."""
    request_id = "99998888777766665555444433332222"
    card = {
        "task_id": "TASK_NF421_CLEANUPFAIL",
        "status": "finished",
        "worker_status": "done",
    }

    manager, path, home, event = _build(tmp_path, request_id=request_id, card=card)

    def _raise(*_a, **_k):
        raise process_launcher.WorkspaceError("cleanup_move_failed")

    monkeypatch.setattr(process_launcher, "cleanup_workspace", _raise)

    result = manager._gc_finalized_workspace(request_id, event)

    assert result is not None and result.get("gc") is False
    assert "cleanup_failed" in result["reason"]
    # The bytes never left the live tree, so retention must say so, not stay silent.
    assert path.exists()

    latest = manager._latest_by_request()[request_id]
    assert latest["workspace_disposition"] == "retained_in_place"
    assert latest["workspace_retained"] is True
    assert latest["error"].startswith("cleanup_failed")
    # The adjudicated outcome survives a cleanup failure, never renamed.
    assert latest["state"] == "validation_failed"


def test_reclaimed_missing_workspace_reports_removed_and_keeps_outcome(
    tmp_path, monkeypatch,
):
    """The fourth terminal retention event -- the reclaim branch for a workspace
    whose bytes have already vanished -- must also carry the disposition
    vocabulary (``removed``) and preserve the adjudicated outcome, exactly as the
    quarantine and purge branches do.  An incomplete vocabulary one branch over
    reads as "not taught yet" and is the same defect this card exists to remove."""
    request_id = "aaaabbbbccccddddeeeeffff00001111"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    path = tmp_path / "wtroot" / request_id / "worktree"
    home = tmp_path / "wtroot" / request_id / "home"
    card = _review_card("TASK_NF421_RECLAIM", request_id, path, home, repo)

    manager, path, home, event = _build(tmp_path, request_id=request_id, card=card)
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_review_workspace_missing",
        lambda *a, **k: {"ok": True, "callback_enqueued": False},
    )

    # The bytes have already gone, so integrity reports review_workspace_missing
    # and the sweep takes the reclaim branch instead of the quarantine branch.
    shutil.rmtree(path)

    result = manager._gc_finalized_workspace(request_id, event)

    assert result is not None and result.get("gc") is True
    assert result["reason"] == "review_workspace_missing"
    assert not path.exists() and not home.exists()

    latest = manager._latest_by_request()[request_id]
    # Every terminal retention event carries exactly one disposition; a reclaim
    # is a removal, not an untagged None a reader cannot classify.
    assert latest["workspace_disposition"] == "removed"
    assert latest["workspace_retained"] is False
    assert latest["workspace_gc"] is True
    assert latest["error"].startswith("retained_workspace_missing_reclaimed")
    # The adjudicated outcome survives the reclaim, just as it survives the
    # quarantine: a vanished workspace is bookkeeping, never a new verdict.
    assert latest["state"] == "validation_failed"
    card_after = json.loads(manager._show_task("TASK_NF421_RECLAIM")["stdout"])
    assert card_after["terminal_substatus"] == latest["state"] == "validation_failed"


def test_quarantined_workspace_is_not_collected_by_a_later_sweep(
    tmp_path, monkeypatch,
):
    """The quarantine event now carries workspace_retained True and an
    adjudicated ``validation_failed`` state -- both GC-candidate signals.  A
    following retention sweep must still refuse to collect it: the quarantine
    flag is the guard, so the moved bytes are never a second time reclaimed."""
    request_id = "abcdef0123456789abcdef0123456789"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    path = tmp_path / "wtroot" / request_id / "worktree"
    home = tmp_path / "wtroot" / request_id / "home"
    card = _review_card("TASK_NF421_NOGC", request_id, path, home, repo)

    manager, path, home, event = _build(tmp_path, request_id=request_id, card=card)
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_review_workspace_missing",
        lambda *a, **k: {"ok": True, "callback_enqueued": False},
    )

    first = manager._gc_finalized_workspace(request_id, event)
    assert first is not None and first.get("quarantined") is True

    quarantine_event = manager._latest_by_request()[request_id]
    assert quarantine_event["workspace_retained"] is True
    assert quarantine_event["workspace_quarantined"] is True
    assert quarantine_event["state"] in process_launcher.GC_CANDIDATE_PROCESS_STATES
    quarantine_dir = Path(quarantine_event["workspace_quarantine_path"])
    assert (quarantine_dir / "worktree" / "evidence.txt").exists()

    # A second sweep is fed the quarantine event verbatim: the guard skips it.
    second = manager._gc_finalized_workspace(request_id, quarantine_event)
    assert second is None
    assert (quarantine_dir / "worktree" / "evidence.txt").exists(), (
        "the quarantined bytes must survive a subsequent retention sweep"
    )


def test_main_finalization_cleanup_failure_reports_retained_not_removed(
    tmp_path, monkeypatch,
):
    """The main finalization path -- ``_finalize_isolated_request`` -- that
    REQUESTS cleanup and whose ``cleanup_workspace`` raises must record ONE
    terminal event whose disposition is truthful: ``retained_in_place`` with
    ``workspace_retained`` True.  The defect this closes labelled the workspace
    ``removed`` and set ``workspace_retained`` False a full delete-attempt early,
    then, when the delete failed, appended a second bare event with no retention
    field -- so the ledger ended with a false "removed" while the bytes remained.
    ``launch_failed`` is the sole terminal outcome that still requests cleanup."""
    request_id = "1234abcd1234abcd1234abcd1234abcd"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)

    worktree_root = tmp_path / "wtroot"
    os.environ[worker_workspace.WORKTREE_ROOT_ENV] = str(worktree_root)
    path = worktree_root / request_id / "worktree"
    home = worktree_root / request_id / "home"
    path.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    (path / "evidence.txt").write_text("worker output", encoding="utf-8")

    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = process_dir / f"{request_id}.request.json"
    stdout_path = process_dir / f"{request_id}.stdout"
    stderr_path = process_dir / f"{request_id}.stderr"
    status_path = process_dir / f"{request_id}.status.json"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    # A clean supervisor record that classifies to worker_failed (a nonzero
    # exit); the launch-failure reclassification below turns that into the
    # cleanup=True launch_failed terminal outcome.
    worker_workspace.write_json_0600(status_path, {
        "state": "exited", "exit_code": 1, "error": "provider exited nonzero",
    })

    task_id = "TASK_NF421_MAINPATH"
    runner = "codex_worker_nf421"
    topic = "task_mcp"
    workspace_meta = {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(path),
        "home": str(home),
        "allowed_writes": ["out/result.txt"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "codex_cli",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "metadata_path": str(metadata_path),
        "validation": [],
        "required_outputs": [],
        "sandbox_backend": "landlock",
        "workspace": workspace_meta,
    })

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        show_task=lambda _tid: {"returncode": 0, "stdout": json.dumps({"task_id": task_id})},
        collision_guard=lambda **_: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        isolation_enabled=True,
    )
    # A non-terminal running row so finalization runs its full body.
    manager._append_event({
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "codex_cli",
        "state": "running",
        "pid": _DEAD_PID,
        "pid_start_ticks": _DEAD_TICKS,
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    })

    # Reclassify the worker_failed exit as a launch failure -- the sole terminal
    # outcome that still requests cleanup -- and stub the surrounding I/O so the
    # test isolates the retention/disposition decision this card owns.
    monkeypatch.setattr(
        process_launcher, "_requires_bridge_cancellation", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        process_launcher, "_provider_auth_failure_from_output",
        lambda *_a, **_k: {
            "http_status": 500, "error_code": "provider_down",
            "session_id": "s", "reason": "provider_launch_refused",
        },
    )
    monkeypatch.setattr(manager, "_retry_claude_auth_refresh", lambda **_k: None)
    monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_record_usage", lambda *a, **k: ({}, False, ""))
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_launch_failed",
        lambda *a, **k: {"ok": True},
    )

    def _raise(*_a, **_k):
        raise process_launcher.WorkspaceError("cleanup_move_failed")

    monkeypatch.setattr(process_launcher, "cleanup_workspace", _raise)

    result = manager._finalize_isolated_request(request_id)

    assert result is not None
    assert result["state"] == "launch_failed"
    # The delete was requested and raised, so the bytes are still on disk:
    # retention must say so, and the disposition must never be "removed".
    assert result["workspace_disposition"] == "retained_in_place"
    assert result["workspace_retained"] is True
    assert result["cleanup_error"].startswith("cleanup_failed")
    assert path.exists() and (path / "evidence.txt").exists()

    # Exactly ONE terminal launch_failed event, and NO event anywhere claims the
    # workspace was removed while the bytes remain -- no false "removed", no bare
    # unretained follow-up row.
    rows = manager._request_events(request_id)
    terminal_rows = [r for r in rows if r.get("state") == "launch_failed"]
    assert len(terminal_rows) == 1
    assert all(r.get("workspace_disposition") != "removed" for r in rows)


# The retention fields a terminal event may carry: any one of them makes an
# event a retention event that the disposition vocabulary must govern.
_RETENTION_FIELDS = {
    "workspace_retained",
    "workspace_gc",
    "workspace_quarantined",
    "workspace_disposition",
    "workspace_quarantine_path",
}


def test_every_retention_event_is_single_sourced_through_the_constructor():
    """Structural guarantee, not a remembered one: the disposition vocabulary
    lives in ONE constructor, and every ``_append_event`` carrying a retention
    field routes through it.  Three prior rounds each found one more unlabelled
    branch; this walks the module so branch nine cannot be added later without
    ``_retention_event`` -- which is the point of this round."""
    source = Path(process_launcher.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    ctors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_retention_event"
    ]
    assert len(ctors) == 1, "exactly one retention-event constructor must exist"
    ctor = ctors[0]

    # ``disposition`` is keyword-only with no default, so a site that forgets it
    # fails to construct rather than silently omitting the field.
    kwonly = {arg.arg for arg in ctor.args.kwonlyargs}
    assert "disposition" in kwonly, "disposition must be keyword-only"
    kw_defaults = dict(zip(
        [arg.arg for arg in ctor.args.kwonlyargs], ctor.args.kw_defaults
    ))
    assert kw_defaults["disposition"] is None, "disposition must have no default"

    def _inside_ctor(node: ast.AST) -> bool:
        return ctor.lineno <= node.lineno <= ctor.end_lineno

    offenders: list[int] = []
    retention_event_calls = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "_retention_event":
            retention_event_calls += 1
            continue
        if node.func.attr != "_append_event":
            continue
        if not (node.args and isinstance(node.args[0], ast.Dict)):
            continue
        literal_keys = {
            key.value for key in node.args[0].keys if isinstance(key, ast.Constant)
        }
        if literal_keys & _RETENTION_FIELDS and not _inside_ctor(node):
            offenders.append(node.lineno)

    assert offenders == [], (
        "these _append_event calls carry a retention field but bypass "
        f"_retention_event (line numbers): {offenders}"
    )
    # Every historical terminal retention branch now routes through the door.
    assert retention_event_calls >= 12


def test_retention_event_derives_workspace_retained_from_disposition(tmp_path):
    """The constructor DERIVES workspace_retained from the disposition so the
    two can never disagree, and refuses an unknown disposition outright."""
    manager, _path, _home, _event = _build(
        tmp_path,
        request_id="1111222233334444aaaabbbbccccdddd",
        card={"task_id": "TASK_NF421_DERIVE", "status": "finished"},
    )
    for disposition, retained in (
        ("retained_in_place", True),
        ("quarantined", True),
        ("removed", False),
    ):
        row = manager._retention_event(
            {"request_id": "derive", "state": "validation_failed"},
            disposition=disposition,
        )
        assert row["workspace_disposition"] == disposition
        assert row["workspace_retained"] is retained

    # An unknown disposition fails to construct -- no silently-omitted field.
    with pytest.raises(KeyError):
        manager._retention_event({"request_id": "derive"}, disposition="mystery")
