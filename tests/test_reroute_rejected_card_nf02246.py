"""NF-2026-02246: a pending manager-rejected card can leave a failing route.

``reject_review --to pending`` pins the classified failure category of the
episode it judged (``validation_environment``, ``base_drift``, ...), not only
``candidate_code``.  ``_verified_manager_rejection_receipt`` accepted only
``candidate_code``, so ``reroute_launch_identity`` answered
``reroute_manager_rejection_identity_mismatch`` for every other manager
rejection and the card could never be moved off the route that kept failing.

The reroute is still refused when it contradicts an explicit runner pinned in
the rejection disposition, and every other identity check is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from aiworkhub import core, task_store, worker_workspace, workforce_catalog

_MISMATCH = "reroute_manager_rejection_identity_mismatch"
_FROM_RUNNER = "glm_5.3"
_TO_RUNNER = "deepseek_v4-pro"
_TO_ADAPTER = "deepseek_vscode_lm"
_TO_MODEL = "deepseek-v4-pro"


@pytest.fixture
def coordinator_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token_path = tmp_path / "coordinator.token"
    token_path.write_text("coordinator-token\n", encoding="utf-8")
    if hasattr(os, "geteuid"):
        # POSIX requires an owner-only token file; on Windows the ACL is the
        # boundary and mode bits are not meaningful.
        os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coordinator-token")
    monkeypatch.setattr(
        worker_workspace, "changed_paths", lambda _ws, **_kw: ["out/result.json"]
    )
    monkeypatch.setattr(
        workforce_catalog,
        "build_catalog",
        lambda _repo: {
            "workers": [{
                "execution_runner": _TO_RUNNER,
                "effective_adapter_id": _TO_ADAPTER,
                "model": _TO_MODEL,
                "enabled": True,
                "launch_eligible": True,
                "available": True,
                "max_risk": "critical",
            }]
        },
    )
    return repo


def _retained_predecessor(repo: Path, task_id: str) -> dict:
    request_id = "a" * 32
    root = worker_workspace.configured_worktree_root(repo) / request_id
    worktree, home = root / "worktree", root / "home"
    output = worktree / "out" / "result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    content = b"retained candidate\n"
    output.write_bytes(content)
    artifact_bytes = b"{}"
    artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_path = (
        worker_workspace.configured_runtime_root(repo)
        / "rework_deltas"
        / f"{artifact_digest}.json"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact_bytes)
    return {
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 1,
        "pinned_at": "2026-08-03T00:01:00+00:00",
        "changed_path_hashes": {
            "out/result.json": hashlib.sha256(content).hexdigest()
        },
        "workspace": {
            "request_id": request_id,
            "repo": str(repo),
            "path": str(worktree),
            "home": str(home),
            "allowed_writes": ["out/result.json"],
            "parent_baseline": {},
            "workspace_baseline": {},
            "base_oid": "b" * 40,
        },
        "rework_delta": {
            "schema_id": "aiworkhub.rework_delta_descriptor.v1",
            "sealed": True,
            "authority_repo": str(repo.resolve()),
            "task_id": task_id,
            "request_id": request_id,
            "claim_epoch": 1,
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_digest,
        },
        "delta_artifact": {"path": str(artifact_path), "digest": artifact_digest},
    }


def _insert_rejected_card(
    repo: Path, task_id: str, *, failure_category: str, pinned_runner: str = ""
) -> dict:
    predecessor = _retained_predecessor(repo, task_id)
    instruction = "repair the rejected candidate through another provider"
    rejection = {
        "schema_id": "aiworkhub.rejection_disposition.v1",
        "failure_category": failure_category,
        "request_id": predecessor["request_id"],
        "to": "pending",
        "pinned_at": predecessor["pinned_at"],
    }
    if pinned_runner:
        rejection["runner"] = pinned_runner
    card = {
        "task_id": task_id,
        "runner": _FROM_RUNNER,
        "topic": "nf02246",
        "objective": "reroute a manager-rejected card",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": None,
        "allowed_writes": ["out/result.json"],
        "required_outputs": ["out/result.json"],
        "forbidden": [],
        "validation": ["python -m pytest tests/test_process_launcher.py"],
        "risk_tier": "high",
        "claim_epoch": 1,
        "rework_predecessor": predecessor,
        "rejection_disposition": rejection,
        "review_feedback": {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": instruction,
            "reason_identity": {
                "bytes": len(instruction.encode("utf-8")),
                "sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
                "truncated": False,
            },
            "predecessor_request_id": predecessor["request_id"],
            "predecessor_changed_paths": sorted(predecessor["changed_path_hashes"]),
            "residual_identities": [],
        },
    }
    now = "2026-08-03T00:00:00+00:00"
    conn = sqlite3.connect(task_store.storage_readiness(repo).canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,"
            "priority,objective,card_json,created_at,updated_at,claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, _FROM_RUNNER, "nf02246", "solo", "pending", "unclaimed",
                "normal", card["objective"], json.dumps(card, sort_keys=True),
                now, now, None,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return card


def _runner(repo: Path, task_id: str) -> str:
    conn = sqlite3.connect(task_store.storage_readiness(repo).canonical_db)
    try:
        row = conn.execute(
            "SELECT runner FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _reroute(task_id: str, to_runner: str = _TO_RUNNER) -> dict:
    return core.reroute_launch_identity(
        task_id,
        from_runner=_FROM_RUNNER,
        to_runner=to_runner,
        to_adapter_id=_TO_ADAPTER,
        to_model=_TO_MODEL,
        reason="the rejected route keeps failing",
    )


@pytest.mark.parametrize(
    "category",
    ["validation_environment", "base_drift", "dependency_or_route", "candidate_code"],
)
def test_nf02246_reroute_pending_manager_rejected_card_succeeds(
    coordinator_repo: Path, category: str
) -> None:
    task_id = f"NF02246_REJECTED_{category.upper()}"
    card = _insert_rejected_card(coordinator_repo, task_id, failure_category=category)

    result = _reroute(task_id)

    assert result["ok"] is True, result
    assert _runner(coordinator_repo, task_id) == _TO_RUNNER
    authorization = result["manager_rejection_authorization"]
    assert authorization["request_id"] == card["rework_predecessor"]["request_id"]
    assert authorization["claim_epoch"] == 1


def test_nf02246_reroute_matching_pinned_runner_succeeds(
    coordinator_repo: Path,
) -> None:
    task_id = "NF02246_PINNED_MATCH"
    _insert_rejected_card(
        coordinator_repo, task_id,
        failure_category="validation_environment", pinned_runner=_TO_RUNNER,
    )

    result = _reroute(task_id)

    assert result["ok"] is True, result
    assert _runner(coordinator_repo, task_id) == _TO_RUNNER


def test_nf02246_reroute_contradicting_pinned_runner_is_refused(
    coordinator_repo: Path,
) -> None:
    task_id = "NF02246_PINNED_CONTRADICTION"
    _insert_rejected_card(
        coordinator_repo, task_id,
        failure_category="candidate_code", pinned_runner="claude_opus-5",
    )

    result = _reroute(task_id)

    assert result["ok"] is False, result
    assert _MISMATCH in result["stderr"]
    assert _runner(coordinator_repo, task_id) == _FROM_RUNNER


def test_nf02246_stale_rejection_identity_is_still_refused(
    coordinator_repo: Path,
) -> None:
    task_id = "NF02246_STALE"
    card = _insert_rejected_card(
        coordinator_repo, task_id, failure_category="validation_environment"
    )
    card["rejection_disposition"]["request_id"] = "c" * 32
    receipt, error = core._verified_manager_rejection_receipt(
        card, task_id=task_id, to_runner=_TO_RUNNER
    )

    assert receipt is None
    assert error == _MISMATCH


def test_nf02246_blocked_rejection_is_still_refused(coordinator_repo: Path) -> None:
    task_id = "NF02246_BLOCKED"
    card = _insert_rejected_card(
        coordinator_repo, task_id, failure_category="candidate_code"
    )
    card["rejection_disposition"]["to"] = "blocked"
    receipt, error = core._verified_manager_rejection_receipt(card, task_id=task_id)

    assert receipt is None
    assert error == _MISMATCH
