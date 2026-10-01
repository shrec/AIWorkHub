"""NF-2026-01160: a zero-change terminal launch failure whose worktree is still
present is recoverable through the canonical clean-root recovery, keeps its
task id, request lineage and failure evidence, and can then be retried on the
same provider or on an alternate provider through the launch-identity reroute.

Every git probe here runs only inside ``tmp_path`` repositories.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import (
    core,
    launch_replay_guard,
    process_launcher,
    task_store,
    worker_workspace,
)

TASK_ID = "NF01160_ZERO_CHANGE_LAUNCH_FAILURE"
REQUEST_ID = "c" * 32
TOPIC = "aiworkhub_blocked_rework_recovery"
RUNNER = "claude_sonnet-5"
ADAPTER = "claude_cli"
ALT_RUNNER = "codex_gpt-5.5"
ALT_ADAPTER = "codex_cli"
ALT_MODEL = "gpt-5.5"
FEEDBACK = "Retry the zero-change launch failure from the canonical root"
REFUSED = "clean_root_no_candidate_workspace_still_available"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c", "user.name=nf01160",
            "-c", "user.email=nf01160@example.invalid",
            "-c", "core.autocrlf=false",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    repo = path.resolve(strict=True)
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("__pycache__/\n.aiworkhub/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_bytes(b"VALUE = 1\n")
    _git(repo, "add", ".gitignore", "src/app.py")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _workspace(repo: Path, request_id: str = REQUEST_ID) -> Path:
    return repo / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"


def _provision_worktree(repo: Path, request_id: str = REQUEST_ID) -> Path:
    workspace = _workspace(repo, request_id)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(workspace), "HEAD")
    return workspace


def _insert_claimed_task(repo: Path, request_id: str = REQUEST_ID) -> None:
    """The launcher's claim: processing, pinned runner, launch request id."""
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-10-01T00:00:00+00:00"
    card = {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "allowed_writes": ["src/app.py"],
        "required_outputs": ["src/app.py"],
        "risk_tier": "medium",
        "claim_epoch": 1,
        "launch_request_id": request_id,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at) VALUES (?, ?, ?, 'processing', 'in_progress', '', '', ?, ?, ?, ?, ?, ?)",
            (TASK_ID, RUNNER, TOPIC, json.dumps(card), now, now, RUNNER, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _workspace_metadata(
    repo: Path,
    workspace: Path,
    workspace_baseline: dict[str, str | None] | None = None,
    *,
    base_oid: str | None = None,
    request_id: str = REQUEST_ID,
) -> dict[str, Any]:
    """The launcher's ``WorkerWorkspace.as_metadata()`` for this request."""
    if base_oid is None:
        base_oid = _git(workspace, "rev-parse", "HEAD").stdout.strip()
    return {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(workspace),
        "home": str(workspace.parent / "home"),
        "allowed_writes": ["src/app.py"],
        "parent_baseline": {"src/app.py": None},
        "workspace_baseline": dict(workspace_baseline or {}),
        "tree_baseline": {},
        "provisioning_timings_ms": {},
        "inherited_rework_paths": [],
        "base_oid": base_oid,
    }


def _fake_provider_exits_before_work(
    repo: Path,
    request_id: str = REQUEST_ID,
    *,
    workspace_metadata: dict[str, Any] | None = None,
) -> None:
    """A claude_cli provider that exits 1 before touching its worktree."""
    evidence: dict[str, Any] = {
        "adapter_id": ADAPTER,
        "error": "worker_failed:runtime_error:exit_code=1",
        "exit_code": 1,
        "failure_class": "unknown",
        "request_id": request_id,
        "required_outputs": [
            {
                "bytes": None,
                "missing": True,
                "path": "src/app.py",
                "reason": "worker_terminal_before_output_validation",
                "sha256": "",
            }
        ],
    }
    if workspace_metadata is not None:
        evidence["workspace"] = workspace_metadata
    assert task_store.mark_terminal_failure(
        repo,
        TASK_ID,
        runner=RUNNER,
        substatus="worker_failed",
        evidence=evidence,
        request_id=request_id,
        claim_epoch=1,
    ) == (True, "blocked")


def _failed_launch(tmp_path: Path) -> tuple[Path, Path]:
    repo = _init_git_repo(tmp_path / "repo")
    task_store.initialize_repository(repo)
    _insert_claimed_task(repo)
    workspace = _provision_worktree(repo)
    _fake_provider_exits_before_work(
        repo, workspace_metadata=_workspace_metadata(repo, workspace)
    )
    return repo, workspace


def _card(repo: Path) -> dict[str, Any]:
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None
    return card


def _recover(repo: Path) -> tuple[bool, str]:
    return task_store.recover_blocked_rework(
        repo,
        TASK_ID,
        actor=core.CODEX_RUNNER,
        feedback_reason=FEEDBACK,
        clean_root_if_predecessor_missing=True,
    )


def _terminal_failure_events(repo: Path) -> list[str]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT payload_json FROM task_events "
                "WHERE task_id=? AND event='terminal_failure' ORDER BY rowid",
                (TASK_ID,),
            ).fetchall()
        ]
    finally:
        conn.close()


def _task_ids(repo: Path) -> list[str]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        return [str(row[0]) for row in conn.execute("SELECT task_id FROM tasks")]
    finally:
        conn.close()


def _assert_refused_and_kept(repo: Path, workspace: Path) -> None:
    before = _card(repo)
    assert _recover(repo) == (False, REFUSED)
    after = _card(repo)
    assert after["status"] == "blocked"
    assert after["terminal_failure"] == before["terminal_failure"]
    assert workspace.exists()


@pytest.fixture
def coordinator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Bind core to the tmp repo with fakes for the gate, actor and routes."""

    def bind(repo: Path) -> None:
        monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
        monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
        monkeypatch.setattr(core, "_canonical_write_gate", lambda *_a, **_k: None)
        monkeypatch.setattr(core, "_verified_manager_actor", lambda: core.CODEX_RUNNER)
        monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)
        monkeypatch.setattr(
            process_launcher,
            "_CANONICAL_WORKFORCE",
            {(ALT_RUNNER, ALT_ADAPTER): {"model": ALT_MODEL}},
        )
        monkeypatch.setattr(
            process_launcher,
            "validate_workforce_identity",
            lambda runner, adapter_id, model, risk_tier=None: model,
        )

    return bind


def _fake_launch(repo: Path, *, runner: str, adapter_id: str) -> dict[str, Any]:
    """The canonical launch preflight plus exact claim, with the provider faked."""
    card = _card(repo)
    refusal = launch_replay_guard.identical_relaunch_refusal(
        card, runner=runner, adapter_id=adapter_id, repo=repo
    )
    assert refusal == "", refusal
    result = core.claim_start_exact(TASK_ID, runner, TOPIC, uuid.uuid4().hex)
    assert result["ok"] is True, result
    return _card(repo)


def _reroute_to_alternate(repo: Path) -> dict[str, Any]:
    result = core.reroute_launch_identity(
        TASK_ID,
        from_runner=RUNNER,
        to_runner=ALT_RUNNER,
        to_adapter_id=ALT_ADAPTER,
        to_model=ALT_MODEL,
        reason="nf01160 zero-change provider failure: retry on another provider",
        topic=TOPIC,
    )
    assert result["ok"] is True, result
    return result


# ---------------------------------------------------------------------------
# Recovery of an untouched worktree
# ---------------------------------------------------------------------------


def test_nf01160_zero_change_worktree_present_recovers_with_lineage(
    tmp_path: Path,
) -> None:
    repo, workspace = _failed_launch(tmp_path)
    failure_before = _card(repo)["terminal_failure"]
    events_before = _terminal_failure_events(repo)
    head = _git(workspace, "rev-parse", "HEAD").stdout.strip()

    assert _recover(repo) == (True, "recovered")

    card = _card(repo)
    assert _task_ids(repo) == [TASK_ID]
    assert card["task_id"] == TASK_ID
    assert card["status"] == "pending"
    assert card["recovery_mode"] == "clean_root_no_candidate_terminal_failure"
    assert card["terminal_failure"] == failure_before
    assert card["terminal_failure"]["request_id"] == REQUEST_ID
    assert _terminal_failure_events(repo) == events_before
    assert card["recovery_predecessor"]["request_id"] == REQUEST_ID
    authorization = card["clean_root_recovery_authorization"]
    assert authorization["predecessor_request_id"] == REQUEST_ID
    assert authorization["changed_path_hashes"] == {}
    assert authorization["untouched_workspace"] == str(workspace)
    # Schema v1 keeps its key set: the worktree is present, so nothing is missing.
    assert authorization["schema_id"] == "aiworkhub.clean_root_no_candidate_authority.v1"
    assert "missing_workspace" in authorization
    assert authorization["missing_workspace"] is None
    proof = authorization["untouched_worktree_proof"]
    assert proof["head_oid"] == head
    assert proof["workspace"] == str(workspace)
    assert isinstance(proof["tree_fingerprint"], str)
    assert len(proof["tree_fingerprint"]) == 64
    # Recovery never deletes the worktree it proved untouched.
    assert (workspace / "src" / "app.py").read_bytes() == b"VALUE = 1\n"
    assert _recover(repo) == (True, "already_recovered")


@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_nf01160_worktree_with_changes_is_refused(tmp_path: Path, change: str) -> None:
    repo, workspace = _failed_launch(tmp_path)
    if change == "tracked":
        (workspace / "src" / "app.py").write_bytes(b"VALUE = 2\n")
        kept = workspace / "src" / "app.py"
    else:
        kept = workspace / "src" / "new_module.py"
        kept.write_bytes(b"NEW = True\n")

    _assert_refused_and_kept(repo, workspace)
    assert kept.exists()


def test_nf01160_stat_dirty_content_identical_worktree_recovers(tmp_path: Path) -> None:
    repo, workspace = _failed_launch(tmp_path)
    tracked = workspace / "src" / "app.py"
    tracked.write_bytes(b"VALUE = 2\n")
    _git(workspace, "add", "src/app.py")
    tracked.write_bytes(b"VALUE = 1\n")
    # The premise: porcelain reports the file while promotion's diff does not.
    assert _git(workspace, "status", "--porcelain").stdout.strip()
    assert _git(workspace, "diff", "--name-only", "HEAD").stdout == ""

    assert _recover(repo) == (True, "recovered")


def test_nf01160_foreign_repository_worktree_is_refused(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    task_store.initialize_repository(repo)
    _insert_claimed_task(repo)
    foreign = _init_git_repo(tmp_path / "foreign")
    workspace = _provision_worktree_from(foreign, _workspace(repo))
    _fake_provider_exits_before_work(
        repo, workspace_metadata=_workspace_metadata(repo, workspace)
    )
    assert _git(workspace, "status", "--porcelain").stdout == ""

    _assert_refused_and_kept(repo, workspace)


def _provision_worktree_from(source_repo: Path, workspace: Path) -> Path:
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _git(source_repo, "worktree", "add", "-q", "--detach", str(workspace), "HEAD")
    return workspace


def test_nf01160_hand_written_git_pointer_is_refused(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    task_store.initialize_repository(repo)
    _insert_claimed_task(repo)
    workspace = _workspace(repo)
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_bytes(b"VALUE = 1\n")
    fake_admin = tmp_path / "fake_admin"
    fake_admin.mkdir()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (fake_admin / "HEAD").write_text(head + "\n", encoding="utf-8")
    (fake_admin / "gitdir").write_text(str(workspace / ".git") + "\n", encoding="utf-8")
    (fake_admin / "commondir").write_text(str(repo / ".git") + "\n", encoding="utf-8")
    (workspace / ".git").write_text(f"gitdir: {fake_admin}\n", encoding="utf-8")
    _fake_provider_exits_before_work(
        repo, workspace_metadata=_workspace_metadata(repo, workspace, base_oid=head)
    )

    _assert_refused_and_kept(repo, workspace)


def test_nf01160_ignored_pycache_does_not_block_recovery(tmp_path: Path) -> None:
    repo, workspace = _failed_launch(tmp_path)
    pycache = workspace / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_bytes(b"\x00compiled")

    assert _recover(repo) == (True, "recovered")


def test_nf01160_proof_head_differing_from_admin_head_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, workspace = _failed_launch(tmp_path)
    real_proof = task_store._untouched_no_candidate_worktree_proof

    def stale_proof(
        repo_arg: Path, workspace_arg: Path, metadata: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        proof = real_proof(repo_arg, workspace_arg, metadata)
        assert proof is not None
        return {**proof, "head_oid": "0" * 40}

    monkeypatch.setattr(task_store, "_untouched_no_candidate_worktree_proof", stale_proof)

    _assert_refused_and_kept(repo, workspace)


def test_nf01160_moved_worktree_head_is_refused(tmp_path: Path) -> None:
    repo, workspace = _failed_launch(tmp_path)
    (workspace / "src" / "app.py").write_bytes(b"VALUE = 3\n")
    _git(workspace, "commit", "-q", "-am", "worker commit")
    assert _git(workspace, "diff", "--name-only", "HEAD").stdout == ""

    _assert_refused_and_kept(repo, workspace)


def test_nf01160_candidate_evidence_never_runs_the_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, workspace = _failed_launch(tmp_path)
    card = _card(repo)
    failure = card["terminal_failure"]
    failure["evidence"]["changed_paths"] = ["src/app.py"]
    card["terminal_failure"] = failure
    _readiness, db_path = task_store._require_ready(repo)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), TASK_ID)
        )
        conn.execute(
            "UPDATE task_events SET payload_json=? "
            "WHERE task_id=? AND event='terminal_failure'",
            (json.dumps(failure), TASK_ID),
        )
    calls: list[tuple[Any, ...]] = []

    def spy(*args: Any) -> dict[str, Any] | None:
        calls.append(args)
        return None

    monkeypatch.setattr(task_store, "_untouched_no_candidate_worktree_proof", spy)

    ok, _state = _recover(repo)

    assert ok is False
    assert calls == []
    assert _card(repo)["status"] == "blocked"
    assert workspace.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_nf01160_junction_component_is_refused_win32(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    task_store.initialize_repository(repo)
    _insert_claimed_task(repo)
    real_parent = tmp_path / "elsewhere"
    real_parent.mkdir()
    junction = _workspace(repo).parent
    junction.parent.mkdir(parents=True, exist_ok=True)
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real_parent)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"mklink /J unavailable: {made.stderr.strip()}")
    workspace = _provision_worktree(repo)
    metadata = _workspace_metadata(repo, workspace)
    _fake_provider_exits_before_work(repo, workspace_metadata=metadata)

    assert task_store._untouched_no_candidate_worktree_proof(
        repo, workspace, metadata
    ) is None
    _assert_refused_and_kept(repo, workspace)


# ---------------------------------------------------------------------------
# Retries after recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["same_provider", "alternate_provider"])
def test_nf01160_recovered_card_retries_on_same_and_alternate_provider(
    tmp_path: Path, coordinator: Any, route: str,
) -> None:
    repo, _workspace_path = _failed_launch(tmp_path)
    coordinator(repo)
    assert _recover(repo) == (True, "recovered")

    if route == "same_provider":
        launched = _fake_launch(repo, runner=RUNNER, adapter_id=ADAPTER)
        assert launched["claimed_by"] == RUNNER
    else:
        reroute = _reroute_to_alternate(repo)
        assert reroute["operational_provider_authorization"]["authority"] == (
            "manager_recovery"
        )
        launched = _fake_launch(repo, runner=ALT_RUNNER, adapter_id=ALT_ADAPTER)
        assert launched["claimed_by"] == ALT_RUNNER
    assert launched["task_id"] == TASK_ID
    assert launched["status"] == "processing"
    assert launched["recovery_predecessor"]["request_id"] == REQUEST_ID
    assert _task_ids(repo) == [TASK_ID]


def test_nf01160_end_to_end_failed_launch_recovery_alternate_retry(
    tmp_path: Path, coordinator: Any,
) -> None:
    # 1. The initial launch: claimed, worktree provisioned, provider exits
    #    before doing any work.
    repo, workspace = _failed_launch(tmp_path)
    coordinator(repo)
    blocked = _card(repo)
    assert blocked["status"] == "blocked"
    assert _git(workspace, "diff", "--name-only", "HEAD").stdout == ""

    # 2. Canonical recovery through core, with the worktree still present.
    recovered = core.recover_blocked_rework(
        TASK_ID,
        feedback_reason=FEEDBACK,
        topic=TOPIC,
        clean_root_if_predecessor_missing=True,
    )
    assert recovered["ok"] is True, recovered
    card = _card(repo)
    assert card["status"] == "pending"
    assert card["terminal_failure"] == blocked["terminal_failure"]
    assert card["clean_root_recovery_authorization"]["untouched_workspace"] == (
        str(workspace)
    )

    # 3. Alternate provider through the launch-identity reroute, then launch.
    _reroute_to_alternate(repo)
    launched = _fake_launch(repo, runner=ALT_RUNNER, adapter_id=ALT_ADAPTER)
    assert launched["task_id"] == TASK_ID
    assert launched["runner"] == ALT_RUNNER
    assert launched["claimed_by"] == ALT_RUNNER
    assert launched["recovery_predecessor"]["request_id"] == REQUEST_ID
    assert launched["terminal_failure"] == blocked["terminal_failure"]
    assert _task_ids(repo) == [TASK_ID]


# ---------------------------------------------------------------------------
# Production worktree shape, seeded paths and under-lease content re-proof
# ---------------------------------------------------------------------------


def _production_failed_launch(
    tmp_path: Path, *, edit_seeded_after_baseline: bool = False
) -> tuple[Path, Path]:
    """Provision like the launcher: no-checkout, sparse, checkout, then seed."""
    repo = _init_git_repo(tmp_path / "repo")
    (repo / "docs").mkdir()
    (repo / "docs" / "outside.md").write_bytes(b"outside the sparse set\n")
    _git(repo, "add", "docs/outside.md")
    _git(repo, "commit", "-q", "-m", "docs")
    task_store.initialize_repository(repo)
    _insert_claimed_task(repo)
    workspace = _workspace(repo)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--no-checkout", "--detach", str(workspace), "HEAD")
    _git(workspace, "sparse-checkout", "set", "--no-cone", "/src/", "/.gitignore")
    _git(workspace, "checkout", "-q", "--detach", "HEAD")
    assert (workspace / "src" / "app.py").read_bytes() == b"VALUE = 1\n"
    assert not (workspace / "docs" / "outside.md").exists()
    # Provisioning seeds a declared untracked input and a live .gitignore
    # whose bytes differ from HEAD; both are recorded in workspace_baseline.
    seeded_ignore = workspace / ".gitignore"
    seeded_ignore.write_text("__pycache__/\n.aiworkhub/\n*.log\n", encoding="utf-8")
    declared = workspace / "inputs" / "brief.md"
    declared.parent.mkdir()
    declared.write_bytes(b"declared input\n")
    baseline = {
        ".gitignore": worker_workspace._hash_path(seeded_ignore),
        "inputs/brief.md": worker_workspace._hash_path(declared),
    }
    if edit_seeded_after_baseline:
        declared.write_bytes(b"edited after the baseline\n")
    assert _git(workspace, "status", "--porcelain").stdout.strip()
    _fake_provider_exits_before_work(
        repo, workspace_metadata=_workspace_metadata(repo, workspace, baseline)
    )
    return repo, workspace


def test_nf01160_production_shape_worktree_recovers(tmp_path: Path) -> None:
    repo, workspace = _production_failed_launch(tmp_path)

    assert _recover(repo) == (True, "recovered")
    authorization = _card(repo)["clean_root_recovery_authorization"]
    assert authorization["untouched_workspace"] == str(workspace)
    assert (workspace / "inputs" / "brief.md").read_bytes() == b"declared input\n"


def test_nf01160_production_shape_seeded_file_edited_is_refused(tmp_path: Path) -> None:
    repo, workspace = _production_failed_launch(
        tmp_path, edit_seeded_after_baseline=True
    )

    _assert_refused_and_kept(repo, workspace)
    assert (workspace / "inputs" / "brief.md").read_bytes() == (
        b"edited after the baseline\n"
    )


def test_nf01160_nested_untracked_gitignore_hiding_itself_is_refused(
    tmp_path: Path,
) -> None:
    repo, workspace = _failed_launch(tmp_path)
    scratch = workspace / "src" / "scratch"
    scratch.mkdir()
    (scratch / ".gitignore").write_text("*\n", encoding="utf-8")
    (scratch / "leftover.py").write_bytes(b"LEFT = 1\n")
    # The premise: the self-ignoring file hides both from --exclude-standard.
    assert _git(workspace, "ls-files", "--others", "--exclude-standard").stdout == ""

    _assert_refused_and_kept(repo, workspace)
    assert (scratch / "leftover.py").exists()


def test_nf01160_write_between_proof_and_lease_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, workspace = _failed_launch(tmp_path)
    real_pre_lease = task_store._pre_lease_untouched_no_candidate_proof

    def leaky(root: Any, db_path: Path, task_id: str) -> dict[str, Any] | None:
        proof = real_pre_lease(root, db_path, task_id)
        assert proof is not None
        (workspace / "src" / "leaked.py").write_bytes(b"LEAKED = True\n")
        return proof

    monkeypatch.setattr(task_store, "_pre_lease_untouched_no_candidate_proof", leaky)

    _assert_refused_and_kept(repo, workspace)
    assert (workspace / "src" / "leaked.py").exists()


def test_nf01160_tracked_change_outside_allowed_and_baseline_is_refused(
    tmp_path: Path,
) -> None:
    repo, workspace = _failed_launch(tmp_path)
    tracked = workspace / ".gitignore"
    # Neither declared in allowed_writes nor seeded in workspace_baseline.
    original = tracked.read_bytes()
    tracked.write_bytes(original + b"*.log\n")
    assert _git(workspace, "diff", "--name-only", "HEAD").stdout.strip() == ".gitignore"

    _assert_refused_and_kept(repo, workspace)
    assert tracked.read_bytes() == original + b"*.log\n"


def test_nf01160_unresolved_metadata_path_still_recovers(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    task_store.initialize_repository(repo)
    _insert_claimed_task(repo)
    workspace = _provision_worktree(repo)
    metadata = _workspace_metadata(repo, workspace)
    # An equivalent, unresolved spelling of the same worktree path.
    unresolved = workspace.parent / ".." / workspace.parent.name / workspace.name
    assert str(unresolved) != str(workspace)
    assert unresolved.resolve() == workspace.resolve()
    metadata["path"] = str(unresolved)
    _fake_provider_exits_before_work(repo, workspace_metadata=metadata)

    assert _recover(repo) == (True, "recovered")
    assert workspace.exists()
