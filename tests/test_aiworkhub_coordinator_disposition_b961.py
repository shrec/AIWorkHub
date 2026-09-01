"""Coordinator lifecycle extensions (issues 3, 4, 7).

- reject_review supports an explicit disposition: pending | blocked | archived
  | superseded (issue 4).
- archive_task / supersede_task are coordinator-gated, atomic (archived_at +
  card_json + task_events), no direct SQLite patching (issue 3).
- terminal substatus -> callback transition map is exhaustive: dependency_blocked
  / liveness_lost never silently become review_ready (issue 7).
"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiworkhub  # noqa: E402
from aiworkhub import (  # noqa: E402
    callback_store,
    core,
    task_retention,
    task_store,
    worker_workspace,
)
from aiworkhub import process_launcher  # noqa: E402

NOW = "2026-07-20T00:00:00+00:00"


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _insert(root: Path, task_id: str, *, worker_status: str = "review",
            status: str = "review", topic: str = "coding", card: dict | None = None) -> None:
    r = task_store.storage_readiness(root)
    conn = sqlite3.connect(r.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", topic, "solo", status, worker_status, "normal",
             "obj", json.dumps(card or {}), NOW, NOW, "claude_coding"),
        )
        conn.commit()
    finally:
        conn.close()


def _row(root: Path, task_id: str) -> dict:
    r = task_store.storage_readiness(root)
    conn = sqlite3.connect(r.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


def _events(root: Path, task_id: str) -> list[str]:
    r = task_store.storage_readiness(root)
    conn = sqlite3.connect(r.canonical_db)
    try:
        rows = conn.execute("SELECT event FROM task_events WHERE task_id=? ORDER BY event_id", (task_id,)).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    tok = tmp_path / "coordinator.token"
    tok.write_text("coord-token\n", encoding="utf-8")
    os.chmod(tok, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(tok))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    return root


# --- issue 4: reject-review disposition ------------------------------------

def test_reject_to_pending_requeues_for_rework(coord):
    _insert(coord, "T_PEND")
    res = core.reject_review("T_PEND", "rework this", to="pending")
    assert res["ok"] is True, res
    row = _row(coord, "T_PEND")
    assert row["status"] == "pending" and row["worker_status"] == "unclaimed"
    feedback = json.loads(row["card_json"])["review_feedback"]
    assert feedback["schema_id"] == "aiworkhub.rework_feedback_delta.v1"
    assert feedback["instruction"] == "rework this"
    assert feedback["reason_identity"]["truncated"] is False


def test_reject_to_pending_never_repersists_decoded_card_json_envelope(coord):
    recursive = json.dumps({"card_json": json.dumps({"card_json": "x" * 200_000})})
    _insert(
        coord,
        "T_PEND_BOUNDED",
        card={"objective": "repair", "card_json": recursive},
    )

    res = core.reject_review("T_PEND_BOUNDED", "rework this", to="pending")

    assert res["ok"] is True, res
    persisted = json.loads(_row(coord, "T_PEND_BOUNDED")["card_json"])
    assert "card_json" not in persisted
    assert len(json.dumps(persisted)) < 3_000


def test_reject_to_pending_bounds_large_unicode_feedback(coord):
    _insert(coord, "T_PEND_UNICODE")
    reason = "ქართული მიზეზი " * 1000

    res = core.reject_review("T_PEND_UNICODE", reason, to="pending")

    assert res["ok"] is True, res
    feedback = json.loads(_row(coord, "T_PEND_UNICODE")["card_json"])[
        "review_feedback"
    ]
    assert len(feedback["instruction"].encode("utf-8")) <= 4 * 1024
    assert feedback["reason_identity"]["bytes"] == len(reason.encode("utf-8"))
    assert feedback["reason_identity"]["truncated"] is True
    assert len(feedback["reason_identity"]["sha256"]) == 64


def test_reject_transition_does_not_wait_for_workspace_gc(coord, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_gc(_manager):
        entered.set()
        release.wait(timeout=5)
        return {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0}

    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_gc_finalized_workspaces",
        slow_gc,
    )
    _insert(coord, "T_ASYNC_GC")

    try:
        res = core.reject_review("T_ASYNC_GC", "rework", to="pending")
        assert res["ok"] is True, res
        assert res["workspace_retention"]["queued"] is True
        assert entered.wait(timeout=1), "background GC did not start"
        assert not release.is_set(), "transition unexpectedly waited for GC"
    finally:
        release.set()


def test_reject_to_pending_pins_exact_review_workspace(coord):
    request_id = "review-request-1"
    workspace = {
        "request_id": request_id,
        "repo": str(coord),
        "path": f"/tmp/aiworkhub-worktrees/{request_id}/worktree",
        "home": f"/tmp/aiworkhub-worktrees/{request_id}/home",
        "allowed_writes": ["out/result.txt"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    _insert(
        coord,
        "T_PIN",
        card={
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": request_id},
                    "workspace": workspace,
                    "changed_path_hashes": {"out/result.txt": "a" * 64},
                },
            },
        },
    )

    res = core.reject_review(
        "T_PIN",
        "repair residual only",
        to="pending",
        residual_identities=[
            {"path": "out/result.txt", "pointer": "/rows/3"},
        ],
    )

    assert res["ok"] is True, res
    card = json.loads(_row(coord, "T_PIN")["card_json"])
    assert card["rework_predecessor"]["request_id"] == request_id
    assert card["rework_predecessor"]["changed_path_hashes"] == {
        "out/result.txt": "a" * 64
    }
    assert card["rework_predecessor"]["residual_identities"] == [
        {"path": "out/result.txt", "pointer": "/rows/3"}
    ]
    assert card["review_feedback"] == {
        "schema_id": "aiworkhub.rework_feedback_delta.v1",
        "instruction": "repair residual only",
        "reason_identity": {
            "bytes": len("repair residual only".encode("utf-8")),
            "sha256": hashlib.sha256(b"repair residual only").hexdigest(),
            "truncated": False,
        },
        "predecessor_request_id": request_id,
        "predecessor_changed_paths": ["out/result.txt"],
        "residual_identities": [
            {"path": "out/result.txt", "pointer": "/rows/3"}
        ],
    }
    assert "terminal_review" not in card


def _terminal_rework_delta_card(
    coord: Path,
    task_id: str,
    request_id: str,
    *,
    claim_epoch: int = 4,
) -> tuple[dict, dict]:
    artifact_root = coord / ".aiworkhub" / "runtime" / "rework_deltas"
    content = b"reviewed predecessor bytes\n"
    artifact = worker_workspace.seal_rework_delta_artifact(
        authority_repo=coord,
        task_id=task_id,
        request_id=request_id,
        claim_epoch=claim_epoch,
        file_entries=[("out/result.txt", content)],
        artifact_dir=artifact_root,
    )
    descriptor = {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(coord.resolve()),
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "artifact_path": artifact["path"],
        "artifact_sha256": artifact["digest"],
    }
    workspace = {
        "request_id": request_id,
        "repo": str(coord),
        "path": f"/tmp/aiworkhub-worktrees/{request_id}/worktree",
        "home": f"/tmp/aiworkhub-worktrees/{request_id}/home",
        "allowed_writes": ["out/result.txt"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    return {
        "claim_epoch": claim_epoch,
        "terminal_review": {
            "claim_epoch": claim_epoch,
            "substatus": "validation_failed",
            "evidence": {
                "request_identity": {
                    "request_id": request_id,
                    "task_id": task_id,
                },
                "workspace": workspace,
                "changed_path_hashes": {
                    "out/result.txt": hashlib.sha256(content).hexdigest()
                },
                "rework_delta": descriptor,
            },
        },
    }, descriptor


def test_reject_to_pending_persists_authenticated_rework_delta(coord):
    task_id = "T_DELTA_PIN"
    request_id = "d" * 32
    card, descriptor = _terminal_rework_delta_card(coord, task_id, request_id)
    _insert(coord, task_id, card=card)

    result = core.reject_review(task_id, "repair delta", to="pending")

    assert result["ok"] is True, result
    persisted = json.loads(_row(coord, task_id)["card_json"])
    predecessor = persisted["rework_predecessor"]
    assert predecessor["rework_delta"] == descriptor
    assert predecessor["request_id"] == request_id
    assert predecessor["task_id"] == task_id
    assert predecessor["claim_epoch"] == descriptor["claim_epoch"]
    assert predecessor["delta_artifact"] == {
        "path": descriptor["artifact_path"],
        "digest": descriptor["artifact_sha256"],
    }
    assert worker_workspace.has_verified_rework_delta(
        predecessor, authority_repo=coord
    )
    assert process_launcher.ProcessManager._gc_disposition(
        persisted, request_id, repo=coord
    ) == (True, "sealed_rework_delta")
    # The retained predecessor worktree may already be gone here.  The compact
    # fields persisted by reject_review must be directly consumable by the
    # delta materializer without any fallback to that deleted workspace.
    worktree = coord / "successor-worktree"
    worktree.mkdir()
    seeded = worker_workspace.materialize_rework_delta_artifact(
        artifact=predecessor["delta_artifact"],
        authority_repo=coord,
        request_id=predecessor["request_id"],
        task_id=predecessor["task_id"],
        claim_epoch=predecessor["claim_epoch"],
        worktree=worktree,
        expected_path_hashes=predecessor["changed_path_hashes"],
        allowed_writes=("out/result.txt",),
    )
    assert seeded == ["out/result.txt"]
    assert (worktree / "out/result.txt").read_bytes() == b"reviewed predecessor bytes\n"


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda row: row.update(claim_epoch=True), "identity_mismatch"),
        (lambda row: row.update(task_id="OTHER"), "identity_mismatch"),
        (lambda row: row.update(artifact_sha256="0" * 64), "tampered"),
    ],
)
def test_reject_to_pending_discards_spoofed_rework_delta(
    coord, mutation, expected
):
    task_id = "T_DELTA_BAD_" + expected
    request_id = "e" * 32
    card, descriptor = _terminal_rework_delta_card(coord, task_id, request_id)
    mutation(descriptor)
    _insert(coord, task_id, card=card)

    result = core.reject_review(task_id, "repair delta", to="pending")

    assert result["ok"] is True, result
    assert result["rework_delta_recovery"] == {
        "schema_id": "aiworkhub.rework_delta_recovery.v1",
        "state": "discarded_untrusted_delta",
        "reason": f"rework_delta_descriptor_{expected}",
        "predecessor_request_id": request_id,
    }
    row = _row(coord, task_id)
    assert row["status"] == "pending"
    persisted = json.loads(row["card_json"])
    assert "rework_predecessor" not in persisted
    assert "terminal_review" not in persisted
    assert persisted["review_feedback"]["predecessor_changed_paths"] == []


def _unsealed_rework_delta_card(coord: Path, task_id: str, request_id: str) -> dict:
    """Terminal review evidence whose retained ``rework_delta`` is present but
    unsealed -- the exact malformed evidence that once deadlocked blocked /
    archived / superseded dispositions behind a pending-only seal check."""
    path = coord / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"
    path.mkdir(parents=True, exist_ok=True)
    workspace = {
        "request_id": request_id,
        "task_id": task_id,
        "repo": str(coord),
        "path": str(path),
        "home": str(path.parent / "home"),
        "allowed_writes": ["out/result.txt"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    descriptor = {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": False,
        "authority_repo": str(coord.resolve()),
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 4,
        "artifact_path": str(
            coord / ".aiworkhub" / "runtime" / "rework_deltas" / "absent.json"
        ),
        "artifact_sha256": "0" * 64,
    }
    return {
        "claim_epoch": 4,
        "terminal_review": {
            "claim_epoch": 4,
            "substatus": "validation_failed",
            "evidence": {
                "request_identity": {"request_id": request_id, "task_id": task_id},
                "workspace": workspace,
                "changed_paths": ["out/result.txt"],
                "changed_path_hashes": {"out/result.txt": "c" * 64},
                "rework_delta": descriptor,
            },
        },
    }


def test_reject_pending_discards_unsealed_rework_delta(coord):
    """An unsealed delta is never reused, but cannot trap the task in review."""
    task_id = "T_UNSEALED_PENDING"
    request_id = "f" * 32
    _insert(coord, task_id, card=_unsealed_rework_delta_card(coord, task_id, request_id))

    result = core.reject_review(
        task_id, "repair delta", to="pending", predecessor_request_id=request_id
    )

    assert result["ok"] is True, result
    assert result["rework_delta_recovery"]["state"] == "discarded_untrusted_delta"
    assert "identity_mismatch" in result["rework_delta_recovery"]["reason"]
    row = _row(coord, task_id)
    assert row["status"] == "pending"
    assert row["worker_status"] == "unclaimed"
    persisted = json.loads(row["card_json"])
    assert "rework_predecessor" not in persisted
    assert "terminal_review" not in persisted


def test_reject_pending_discards_missing_validation_failed_delta(coord):
    task_id = "T_MISSING_PENDING"
    request_id = "9" * 32
    card = _unsealed_rework_delta_card(coord, task_id, request_id)
    del card["terminal_review"]["evidence"]["rework_delta"]
    _insert(coord, task_id, card=card)

    result = core.reject_review(task_id, "repair delta", to="pending")

    assert result["ok"] is True, result
    assert result["rework_delta_recovery"] == {
        "schema_id": "aiworkhub.rework_delta_recovery.v1",
        "state": "discarded_untrusted_delta",
        "reason": "rework_delta_descriptor_missing",
        "predecessor_request_id": request_id,
    }
    persisted = json.loads(_row(coord, task_id)["card_json"])
    assert "rework_predecessor" not in persisted
    assert persisted["review_feedback"]["predecessor_changed_paths"] == []


def test_reject_blocked_skips_unsealed_rework_delta(coord):
    """Blocked disposition must not validate or inherit the retained rework
    delta -- the exact unsealed fixture is parked without inheriting bytes."""
    task_id = "T_UNSEALED_BLOCKED"
    request_id = "a" * 32
    _insert(coord, task_id, card=_unsealed_rework_delta_card(coord, task_id, request_id))

    result = core.reject_review(
        task_id, "needs external input", to="blocked", predecessor_request_id=request_id
    )

    assert result["ok"] is True, result
    row = _row(coord, task_id)
    assert row["status"] == "blocked"
    assert row["worker_status"] == "blocked"
    card = json.loads(row["card_json"])
    predecessor = card["rework_predecessor"]
    assert predecessor["request_id"] == request_id
    assert predecessor["changed_path_hashes"] == {"out/result.txt": "c" * 64}
    assert "rework_delta" not in predecessor
    assert "delta_artifact" not in predecessor
    assert "reviewer_finalization" in result


@pytest.mark.parametrize("disposition", ["archived", "superseded"])
def test_reject_retire_skips_unsealed_rework_delta(coord, disposition):
    """archived / superseded retire the identical unsealed fixture out of the
    review queue without validating or inheriting the delta."""
    task_id = "T_UNSEALED_" + disposition.upper()
    request_id = "b" * 32
    _insert(coord, task_id, card=_unsealed_rework_delta_card(coord, task_id, request_id))

    result = core.reject_review(
        task_id, "obsolete", to=disposition, predecessor_request_id=request_id
    )

    assert result["ok"] is True, result
    row = _row(coord, task_id)
    assert str(row["archived_at"]).strip()
    assert disposition in _events(coord, task_id)
    assert "reviewer_finalization" in result


def test_invalid_residual_identity_returns_actionable_schema(coord):
    _insert(coord, "T_BAD_RESIDUAL")
    result = core.reject_review(
        "T_BAD_RESIDUAL",
        "repair residual",
        to="pending",
        residual_identities=[{"path": "out/result.txt", "pointer": "rows/3"}],
    )

    assert result["ok"] is False
    assert result["invalid_index"] == 0
    schema = result["residual_identities_schema"]
    assert schema["items"]["required"] == ["path", "pointer"]
    assert schema["example"] == [
        {"path": "data/residual.json", "pointer": "/rows/7"}
    ]


def test_reject_to_blocked_parks_as_blocked(coord):
    _insert(
        coord,
        "T_BLOCK",
        card={
            "claim_epoch": 3,
            "terminal_review": {"substatus": "review_ready"},
            "deterministic_verification": {"pass": True, "claim_epoch": 3},
        },
    )
    res = core.reject_review("T_BLOCK", "needs external input", to="blocked")
    assert res["ok"] is True, res
    row = _row(coord, "T_BLOCK")
    assert row["status"] == "blocked" and row["worker_status"] == "blocked"
    card = json.loads(row["card_json"])
    assert "deterministic_verification" not in card
    assert "terminal_review" not in card


def test_reject_to_archived_retires_atomically(coord):
    _insert(coord, "T_ARCH")
    res = core.reject_review("T_ARCH", "obsolete", to="archived")
    assert res["ok"] is True, res
    row = _row(coord, "T_ARCH")
    assert str(row["archived_at"]).strip()          # archived_at set
    assert "archived" in _events(coord, "T_ARCH")    # atomic task_events row


def test_reject_to_superseded_retires_as_superseded(coord):
    _insert(coord, "T_SUP")
    res = core.reject_review("T_SUP", "replaced", to="superseded")
    assert res["ok"] is True, res
    row = _row(coord, "T_SUP")
    assert str(row["archived_at"]).strip()
    assert "superseded" in _events(coord, "T_SUP")


def test_reject_invalid_disposition_is_rejected(coord):
    _insert(coord, "T_BAD")
    res = core.reject_review("T_BAD", "x", to="teleport")
    assert res["ok"] is False
    assert "invalid reject-review disposition" in (res.get("stderr") or "")


def test_reject_non_review_task_is_not_reviewable(coord):
    _insert(coord, "T_PENDING_SRC", worker_status="unclaimed", status="pending")
    res = core.reject_review("T_PENDING_SRC", "x", to="archived")
    assert res["ok"] is False
    assert "reject_not_reviewable" in (res.get("stderr") or "")


# --- issue 3: archive / supersede coordinator commands ---------------------

def test_archive_task_sets_archived_at_and_event(coord):
    _insert(coord, "T_A", worker_status="unclaimed", status="pending")
    res = core.archive_task("T_A", reason="stale pending")
    assert res["ok"] is True, res
    assert str(_row(coord, "T_A")["archived_at"]).strip()
    assert "archived" in _events(coord, "T_A")


def test_supersede_task_records_replacement(coord):
    _insert(coord, "T_NEW", worker_status="unclaimed", status="pending")
    _insert(coord, "T_S", worker_status="unclaimed", status="pending")
    res = core.supersede_task("T_S", reason="stale", by="T_NEW")
    assert res["ok"] is True, res
    row = _row(coord, "T_S")
    assert str(row["archived_at"]).strip()
    card = json.loads(row["card_json"])
    assert "T_NEW" in str(card.get("archive_reason") or "")
    assert "superseded" in _events(coord, "T_S")


def test_archive_requires_coordinator_capability(tmp_path, monkeypatch):
    # No coordinator token -> capability denied, no archive.
    root = _init_repo(tmp_path, "repo_noc")
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.delenv("BITNN_TASKCTL_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", raising=False)
    monkeypatch.setattr(aiworkhub, "_coordinator_token", "", raising=False)
    _insert(root, "T_NOC", worker_status="unclaimed", status="pending")
    res = core.archive_task("T_NOC", reason="x")
    assert res["ok"] is False
    assert not str(_row(root, "T_NOC")["archived_at"]).strip()


# --- issue 1: repo-local coordinator token ---------------------------------

import stat as _stat  # noqa: E402


def _clear_coordinator_env(monkeypatch) -> None:
    monkeypatch.delenv("BITNN_TASKCTL_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", raising=False)
    monkeypatch.setattr(aiworkhub, "_coordinator_token", "", raising=False)
    monkeypatch.setattr(aiworkhub, "_coordinator_token_file", "", raising=False)


def test_init_repo_creates_owner_only_coordinator_token(tmp_path):
    root = _init_repo(tmp_path, "repo_tok")
    token = root / ".aiworkhub" / "runtime" / "coordinator.token"
    assert token.is_file()
    assert token.read_text(encoding="utf-8").strip()
    if hasattr(os, "geteuid"):
        assert _stat.S_IMODE(token.stat().st_mode) == 0o600


def test_repo_local_token_grants_capability_without_env(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "repo_rl")
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    _clear_coordinator_env(monkeypatch)
    _insert(root, "T_RL", worker_status="unclaimed", status="pending")
    # No exported env token -- the repo-local owner-only token IS the capability.
    res = core.archive_task("T_RL", reason="via repo-local token")
    assert res["ok"] is True, res
    assert str(_row(root, "T_RL")["archived_at"]).strip()


def test_missing_token_denies_capability(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "repo_notok")
    (root / ".aiworkhub" / "runtime" / "coordinator.token").unlink()
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    _clear_coordinator_env(monkeypatch)
    _insert(root, "T_NT", worker_status="unclaimed", status="pending")
    res = core.archive_task("T_NT", reason="x")
    assert res["ok"] is False
    assert not str(_row(root, "T_NT")["archived_at"]).strip()


# --- issue 5: Source Graph refresh before launching dependents -------------

def test_mark_done_refreshes_source_graph_before_reconcile(coord):
    _insert(coord, "T_DONE")
    res = core.mark_done("T_DONE")
    assert res["ok"] is True, res
    # The just-promoted outputs are made visible to the Source Graph before any
    # depends_on dependent is launched by reconcile_after_accept.
    assert "source_graph_refresh" in res
    assert "dependency_autolaunch" in res


def test_mark_done_rejects_finalize_failed_terminal_review(coord):
    _insert(
        coord,
        "T_FINALIZE_FAILED",
        card={
            "terminal_review": {
                "substatus": "finalize_failed",
                "deterministic_verification": {
                    "applicable": True,
                    "pass": False,
                },
            }
        },
    )

    res = core.mark_done("T_FINALIZE_FAILED")

    assert res["ok"] is False
    assert "done_terminal_review_not_acceptable:finalize_failed" in res["stderr"]
    row = _row(coord, "T_FINALIZE_FAILED")
    assert row["status"] == "review"
    assert row["worker_status"] == "review"


def test_mark_done_requires_accept_review_for_isolated_candidate(coord):
    _insert(
        coord,
        "T_REVIEW_FIRST",
        card={
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": "request-123"},
                },
                "deterministic_verification": {
                    "applicable": True,
                    "pass": True,
                },
            }
        },
    )

    res = core.mark_done("T_REVIEW_FIRST")

    assert res["ok"] is False
    assert "agent_accept_review_required" in res["stderr"]
    assert _row(coord, "T_REVIEW_FIRST")["status"] == "review"


def test_mark_done_is_idempotent_after_accept_review_finished_candidate(coord):
    _insert(
        coord,
        "T_ALREADY_ACCEPTED",
        status="finished",
        worker_status="done",
        card={
            "accepted_request_id": "request-123",
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": "request-123"},
                },
            },
        },
    )

    res = core.mark_done("T_ALREADY_ACCEPTED")

    assert res["ok"] is True
    assert res["already_done"] is True
    assert res["reconciled"] is True
    assert _row(coord, "T_ALREADY_ACCEPTED")["status"] == "finished"


# --- issue 7: terminal substatus -> callback transition map ----------------

def test_callback_transition_map_is_exhaustive_for_blocked_substatuses():
    assert callback_store.resolve_callback_transition("dependency_blocked") == "blocked"
    assert callback_store.resolve_callback_transition("liveness_lost") == "blocked"
    assert callback_store.resolve_callback_transition("required_output_unchanged") == "validation_failed"
    # unchanged mappings still hold
    assert callback_store.resolve_callback_transition("validation_failed") == "validation_failed"
    assert callback_store.resolve_callback_transition("worker_failed") == "worker_failed"
    assert callback_store.resolve_callback_transition("review_ready") == "review_ready"
    assert callback_store.resolve_callback_transition("blocked") == "blocked"


# --- V2: predecessor_request_id selection ----------------------------------


def _strict_retained_workspace(
    coord: Path,
    task_id: str,
    request_id: str,
    allowed_writes: list[str],
) -> dict:
    path = coord / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"
    path.mkdir(parents=True, exist_ok=True)
    return {
        "request_id": request_id,
        "task_id": task_id,
        "repo": str(coord),
        "path": str(path),
        "home": str(path.parent / "home"),
        "allowed_writes": allowed_writes,
        "parent_baseline": {},
        "workspace_baseline": {},
    }


def _strict_delta_descriptor(
    coord: Path,
    task_id: str,
    request_id: str,
    path: str,
    content: bytes,
    *,
    claim_epoch: int = 1,
) -> tuple[dict, dict[str, str]]:
    artifact = worker_workspace.seal_rework_delta_artifact(
        authority_repo=coord,
        task_id=task_id,
        request_id=request_id,
        claim_epoch=claim_epoch,
        file_entries=[(path, content)],
        artifact_dir=coord / ".aiworkhub" / "runtime" / "rework_deltas",
    )
    return {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(coord.resolve()),
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "artifact_path": artifact["path"],
        "artifact_sha256": artifact["digest"],
    }, {path: hashlib.sha256(content).hexdigest()}


def test_reject_to_pending_explicit_predecessor_selects_retained_workspace(coord):
    """Explicit predecessor_request_id selects a durably-pinned retained
    workspace from a prior rework cycle rather than defaulting to the current
    terminal_review."""
    retained_request = "retained-request-A"
    task_id = "T_V2_EXPLICIT"
    retained_workspace = _strict_retained_workspace(
        coord, task_id, retained_request, ["src/a.py"]
    )
    retained_delta, retained_hashes = _strict_delta_descriptor(
        coord, task_id, retained_request, "src/a.py", b"retained A\n"
    )
    # Card has a durably-pinned rework_predecessor from cycle A plus a current
    # terminal_review from cycle B (validation_failed).
    _insert(
        coord,
        task_id,
        card={
            "claim_epoch": 1,
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": retained_request,
                "task_id": task_id,
                "claim_epoch": 1,
                "workspace": retained_workspace,
                "changed_path_hashes": retained_hashes,
                "rework_delta": retained_delta,
                "residual_identities": [],
                "pinned_at": "2026-07-19T00:00:00+00:00",
            },
            "terminal_review": {
                "substatus": "validation_failed",
                "evidence": {
                    "request_identity": {"request_id": "newer-request-B"},
                    "workspace": {
                        "request_id": "newer-request-B",
                        "repo": str(coord),
                        "path": "/tmp/aiworkhub-worktrees/newer-request-B/worktree",
                        "home": "/tmp/aiworkhub-worktrees/newer-request-B/home",
                        "allowed_writes": ["src/b.py"],
                        "parent_baseline": {},
                        "workspace_baseline": {},
                    },
                    "changed_path_hashes": {"src/b.py": "b" * 64},
                },
            },
        },
    )

    res = core.reject_review(
        task_id,
        "rework from retained A",
        to="pending",
        predecessor_request_id=retained_request,
    )

    assert res["ok"] is True, res
    card = json.loads(_row(coord, task_id)["card_json"])
    assert card["rework_predecessor"]["request_id"] == retained_request
    assert card["rework_predecessor"]["changed_path_hashes"] == retained_hashes
    assert card["rework_predecessor"]["workspace"]["request_id"] == retained_request
    assert card["review_feedback"]["predecessor_request_id"] == retained_request
    assert card["review_feedback"]["predecessor_changed_paths"] == ["src/a.py"]
    assert "terminal_review" not in card


def test_reject_to_blocked_explicit_predecessor_pins_selection(coord):
    """Blocked disposition with an explicit predecessor_request_id must pin
    the rework_predecessor identically to the pending case."""
    retained_request = "retained-blocked-A"
    task_id = "T_V2_BLOCKED_PIN"
    retained_workspace = _strict_retained_workspace(
        coord, task_id, retained_request, ["src/x.py"]
    )
    retained_hashes = {"src/x.py": "c" * 64}
    _insert(
        coord,
        task_id,
        card={
            "claim_epoch": 1,
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": retained_request,
                "task_id": task_id,
                "claim_epoch": 1,
                "workspace": retained_workspace,
                "changed_path_hashes": retained_hashes,
                "residual_identities": [],
                "pinned_at": "2026-07-19T00:00:00+00:00",
            },
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": "blocked-current-B"},
                    "workspace": {
                        "request_id": "blocked-current-B",
                        "repo": str(coord),
                        "path": "/tmp/aiworkhub-worktrees/blocked-current-B/worktree",
                        "home": "/tmp/aiworkhub-worktrees/blocked-current-B/home",
                        "allowed_writes": ["src/y.py"],
                        "parent_baseline": {},
                        "workspace_baseline": {},
                    },
                    "changed_path_hashes": {"src/y.py": "y" * 64},
                },
            },
        },
    )

    res = core.reject_review(
        task_id,
        "blocked with explicit A",
        to="blocked",
        predecessor_request_id=retained_request,
    )

    assert res["ok"] is True, res
    row = _row(coord, task_id)
    assert row["status"] == "blocked"
    assert row["worker_status"] == "blocked"
    card = json.loads(row["card_json"])
    assert card["rework_predecessor"]["request_id"] == retained_request
    assert card["rework_predecessor"]["changed_path_hashes"] == retained_hashes
    assert "terminal_review" not in card


def test_reject_explicit_current_zero_diff_predecessor_matches_automatic_resolution(
    coord,
):
    request_id = "zero-diff-current-request"
    task_id = "T_V2_ZERO_DIFF_EXPLICIT"
    workspace = _strict_retained_workspace(coord, task_id, request_id, [])
    _insert(
        coord,
        task_id,
        card={
            "claim_epoch": 1,
            "terminal_review": {
                "claim_epoch": 1,
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {
                        "request_id": request_id,
                        "task_id": task_id,
                    },
                    "workspace": workspace,
                    "changed_paths": [],
                    "changed_path_hashes": {},
                },
            },
        },
    )

    result = core.reject_review(
        task_id,
        "repeat the read-only audit",
        to="pending",
        predecessor_request_id=request_id,
    )

    assert result["ok"] is True, result
    card = json.loads(_row(coord, task_id)["card_json"])
    assert card["review_feedback"]["predecessor_request_id"] == request_id
    assert card["review_feedback"]["predecessor_changed_paths"] == []
    assert "terminal_review" not in card


_ZERO_DIFF_GITIGNORE = "__pycache__/\n.pytest_cache/\n.ruff_cache/\n.coverage\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _sealed_zero_diff_workspace(
    coord: Path,
    task_id: str,
    request_id: str,
    writable: dict[str, bytes],
    allowed_writes: list[str] | None = None,
) -> dict:
    """Materialize a *realistic* retained worktree and seal its baseline.

    Mirrors ``worker_workspace.WorkerWorkspace.as_metadata()`` for an attempt
    that wrote nothing: every declared writable path exists on disk and its
    ``workspace_baseline`` digest is computed from those exact bytes, so an
    empty diff is re-derivable from the filesystem instead of merely declared.
    ``tree_baseline`` is sealed the same way over the *complete* worktree, so a
    path no per-path baseline entry covers -- such as a file only a glob
    ``allowed_writes`` entry could match -- is still mechanically comparable.

    The worktree is a real git repository with a real ``.gitignore`` because
    the declared ``changed_paths`` on terminal evidence come from git; a
    fixture that is only a bare directory cannot show whether the zero-diff
    authority agrees with git about what is ignored.  ``core.excludesFile`` is
    pointed at a path that does not exist so the developer's global ignore
    rules cannot change what this test proves.
    """
    workspace = _strict_retained_workspace(
        coord,
        task_id,
        request_id,
        sorted(writable) if allowed_writes is None else allowed_writes,
    )
    root = Path(workspace["path"])
    _git(root, "init", "--quiet")
    _git(root, "config", "core.excludesFile", str(root / ".git" / "no-global-excludes"))
    (root / ".gitignore").write_text(_ZERO_DIFF_GITIGNORE, encoding="utf-8")
    tracked = [".gitignore"]
    baseline: dict[str, str | None] = {}
    for relative, content in writable.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        tracked.append(relative)
    # Staged, not committed: ``git check-ignore`` consults the index, which is
    # what keeps a tracked path that also matches an ignore pattern reported as
    # *not* ignored.
    _git(root, "add", "--", *tracked)
    for relative in writable:
        baseline[relative] = worker_workspace._hash_path(root / relative)
    workspace["workspace_baseline"] = baseline
    workspace["tree_baseline"] = worker_workspace._worktree_manifest(root)
    return workspace


def _terminal_failure_evidence(
    task_id: str,
    request_id: str,
    workspace: dict,
    changed_paths: list[str],
) -> dict:
    """The exact evidence a validation_failed terminal review carries.

    ``ProcessManager._terminal_failure_exact`` seals request identity, the
    changed/promoted path lists and workspace metadata -- and, unlike the
    review_ready path, no ``changed_path_hashes`` map at all.  A read-only
    (zero diff) failure therefore reaches ``core.reject_review`` with an
    absent hash map and an empty declared diff, which is the shape observed on
    Windows terminal cards.
    """
    return {
        "request_id": request_id,
        "error": "declared validation command failed",
        "changed_paths": list(changed_paths),
        "promoted_paths": [],
        "workspace": workspace,
        "request_identity": {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "claude_coding",
            "topic": "coding",
        },
    }


def _insert_validation_failed_card(
    coord: Path,
    task_id: str,
    request_id: str,
    workspace: dict,
    changed_paths: list[str],
    *,
    review_claim_epoch: int = 1,
) -> None:
    _insert(
        coord,
        task_id,
        card={
            "claim_epoch": 1,
            "terminal_review": {
                "claim_epoch": review_claim_epoch,
                "substatus": "validation_failed",
                "evidence": _terminal_failure_evidence(
                    task_id, request_id, workspace, changed_paths
                ),
            },
        },
    )


def _reject_zero_diff(coord: Path, task_id: str, request_id: str) -> dict:
    return core.reject_review(
        task_id,
        "rerun the read-only audit",
        to="pending",
        predecessor_request_id=request_id,
    )


def test_reject_explicit_validation_failed_zero_diff_predecessor_is_selectable(
    coord,
):
    """The Windows regression: an explicit predecessor_request_id naming a
    retained validation_failed review that changed nothing must bind, even
    though that terminal evidence shape carries no changed_path_hashes map."""
    request_id = "validation-failed-zero-diff"
    task_id = "T_V2_VF_ZERO_DIFF"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    # On the canonical preimage this is predecessor_request_id_missing_hashes.
    assert result["ok"] is True, result.get("stderr")
    row = _row(coord, task_id)
    assert row["status"] == "pending" and row["worker_status"] == "unclaimed"
    card = json.loads(row["card_json"])
    assert card["review_feedback"]["predecessor_request_id"] == request_id
    assert card["review_feedback"]["predecessor_changed_paths"] == []
    assert "terminal_review" not in card


def test_reject_explicit_validation_failed_zero_diff_ignores_post_seal_artifacts(
    coord,
):
    """Running the declared validation leaves git-ignored artifacts behind.

    ``__pycache__/``, ``.pytest_cache/``, ``.ruff_cache/`` and ``.coverage``
    appear in a raw tree manifest but never in the git-derived declared
    ``changed_paths``.  The zero-diff authority must agree with git about that,
    or every real read-only rejection recreates the original failure.
    """
    request_id = "validation-failed-zero-diff-artifacts"
    task_id = "T_V2_VF_ZERO_DIFF_ARTIFACTS"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    root = Path(workspace["path"])
    for relative, content in {
        "src/__pycache__/audited.cpython-312.pyc": b"\x00compiled\n",
        ".pytest_cache/CACHEDIR.TAG": b"Signature: 8a477f597d28d172\n",
        ".ruff_cache/0.6.9/entry": b"cached\n",
        ".coverage": b"coverage-db\n",
    }.items():
        artifact = root / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(content)
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is True, result.get("stderr")
    card = json.loads(_row(coord, task_id)["card_json"])
    assert card["review_feedback"]["predecessor_request_id"] == request_id
    assert card["review_feedback"]["predecessor_changed_paths"] == []


def test_reject_explicit_validation_failed_zero_diff_rejects_drifted_workspace(
    coord,
):
    """A declared empty diff is never authority on its own: if a declared
    writable path no longer matches the digest sealed with it, the selection
    fails closed and says so."""
    request_id = "validation-failed-zero-diff-drift"
    task_id = "T_V2_VF_ZERO_DIFF_DRIFT"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    (Path(workspace["path"]) / "src" / "audited.py").write_bytes(b"drifted\n")
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_baseline_drift"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_requires_sealed_baseline(
    coord,
):
    """A declared writable path left out of workspace_baseline leaves nothing
    to re-derive, so the empty hash map must not be synthesized."""
    request_id = "validation-failed-zero-diff-unsealed"
    task_id = "T_V2_VF_ZERO_DIFF_UNSEALED"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    workspace["workspace_baseline"] = {}
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_unsealed_baseline"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_requires_sealed_tree(coord):
    """Evidence that never carried a tree manifest cannot prove a zero diff,
    and must not be confused with a tree that merely drifted."""
    request_id = "validation-failed-zero-diff-no-tree"
    task_id = "T_V2_VF_ZERO_DIFF_NO_TREE"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    workspace.pop("tree_baseline")
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_unsealed_tree"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_rejects_new_glob_scope_file(
    coord,
):
    """A glob ``allowed_writes`` entry cannot be enumerated by the per-path
    ``workspace_baseline``, so a file created under it after provisioning is
    invisible there.  It is untracked and *not* git-ignored, so the complete
    tree comparison must still report drift."""
    request_id = "validation-failed-zero-diff-glob-new-file"
    task_id = "T_V2_VF_ZERO_DIFF_GLOB_NEW"
    workspace = _sealed_zero_diff_workspace(
        coord,
        task_id,
        request_id,
        {"src/audited.py": b"unchanged\n"},
        allowed_writes=["src/audited.py", "out/*.txt"],
    )
    # Created after the baseline was sealed: matches the glob scope, is absent
    # from workspace_baseline, and leaves every declared exact path untouched.
    created = Path(workspace["path"]) / "out" / "leaked.txt"
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_bytes(b"created after provisioning\n")
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_tree_drift"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_rejects_out_of_root_workspace(
    coord,
):
    """A card-declared path outside the repository-scoped runtime worktree
    root is never scanned, however plausible its sealed manifests look."""
    request_id = "validation-failed-zero-diff-stray"
    task_id = "T_V2_VF_ZERO_DIFF_STRAY"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    stray = coord / ".aiworkhub" / "runtime" / "stray" / "worktree"
    stray.mkdir(parents=True, exist_ok=True)
    workspace["path"] = str(stray)
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_workspace_out_of_root"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_rejects_foreign_request_worktree(
    coord,
):
    """Inside the runtime worktree root is not enough: the worktree scanned
    must be the one this exact request owns."""
    request_id = "validation-failed-zero-diff-foreign"
    task_id = "T_V2_VF_ZERO_DIFF_FOREIGN"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    foreign = _sealed_zero_diff_workspace(
        coord,
        task_id,
        "validation-failed-zero-diff-foreign-other",
        {"src/audited.py": b"unchanged\n"},
    )
    workspace["path"] = foreign["path"]
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_identity_mismatch"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_rejects_stale_claim_epoch(
    coord,
):
    """A retained worktree from a superseded claim episode is not this
    episode's evidence, and is refused before anything is hashed."""
    request_id = "validation-failed-zero-diff-epoch"
    task_id = "T_V2_VF_ZERO_DIFF_EPOCH"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    _insert_validation_failed_card(
        coord, task_id, request_id, workspace, [], review_claim_epoch=2
    )

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_identity_mismatch"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_bounds_the_tree_walk(
    coord, monkeypatch
):
    """The complete tree walk is capped; an oversized worktree fails closed
    with its own reason rather than being silently truncated into a pass."""
    request_id = "validation-failed-zero-diff-budget"
    task_id = "T_V2_VF_ZERO_DIFF_BUDGET"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    monkeypatch.setattr(core, "_MAX_ZERO_DIFF_TREE_ENTRIES", 1)
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_budget_exceeded"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_reports_scan_failure(
    coord, monkeypatch
):
    """An unreadable retained worktree proves nothing either way, and must not
    be reported as drift."""
    request_id = "validation-failed-zero-diff-scan"
    task_id = "T_V2_VF_ZERO_DIFF_SCAN"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )

    def _unreadable(_root):
        raise OSError("scandir denied")

    monkeypatch.setattr(core, "_bounded_worktree_manifest", _unreadable)
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_scan_failed"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_reports_ignore_check_failure(
    coord, monkeypatch
):
    """If git cannot classify the candidate delta, the artifact allowance must
    fail closed with its own reason -- never default to "nothing is ignored"
    (spurious drift) or to "everything is" (a forged pass)."""
    request_id = "validation-failed-zero-diff-ignore"
    task_id = "T_V2_VF_ZERO_DIFF_IGNORE"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    (Path(workspace["path"]) / ".coverage").write_bytes(b"coverage-db\n")

    def _no_git(_root, _candidates):
        raise OSError("git unavailable")

    monkeypatch.setattr(core, "_git_ignored_subset", _no_git)
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_ignore_check_failed"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_zero_diff_requires_coordinator_capability(
    coord, monkeypatch
):
    """The card names the directory this walks.  Without the coordinator
    capability nothing on that path may be scanned or hashed, and no
    filesystem-derived verdict may be returned."""
    request_id = "validation-failed-zero-diff-capability"
    task_id = "T_V2_VF_ZERO_DIFF_CAPABILITY"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    scanned: list[Path] = []

    def _record(root):
        scanned.append(root)
        raise AssertionError("scanned a card-declared path without capability")

    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda _runner: (False, "denied")
    )
    monkeypatch.setattr(core, "_bounded_worktree_manifest", _record)
    _insert_validation_failed_card(coord, task_id, request_id, workspace, [])

    result = _reject_zero_diff(coord, task_id, request_id)

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_zero_diff_capability_denied"
    assert scanned == []
    assert _row(coord, task_id)["status"] == "review"


def test_reject_explicit_validation_failed_nonempty_diff_without_hashes_fails_closed(
    coord,
):
    """A non-empty declared diff with no hash map stays unbindable -- the
    zero-diff allowance must not weaken changed-path hash binding."""
    request_id = "validation-failed-nonempty-diff"
    task_id = "T_V2_VF_NONEMPTY_DIFF"
    workspace = _sealed_zero_diff_workspace(
        coord, task_id, request_id, {"src/audited.py": b"unchanged\n"}
    )
    _insert_validation_failed_card(
        coord, task_id, request_id, workspace, ["src/audited.py"]
    )

    result = core.reject_review(
        task_id,
        "rerun the audit",
        to="pending",
        predecessor_request_id=request_id,
    )

    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_missing_hashes"
    assert _row(coord, task_id)["status"] == "review"


def test_reject_default_predecessor_is_current_review(coord):
    """When predecessor_request_id is omitted (None), the current terminal_review
    request is the safe default -- identical to V1 behaviour."""
    current_request = "current-review-default"
    current_workspace = {
        "request_id": current_request,
        "repo": str(coord),
        "path": f"/tmp/aiworkhub-worktrees/{current_request}/worktree",
        "home": f"/tmp/aiworkhub-worktrees/{current_request}/home",
        "allowed_writes": ["out/default.txt"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    current_hashes = {"out/default.txt": "d" * 64}
    _insert(
        coord,
        "T_V2_DEFAULT",
        card={
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "older-stale-request",
                "workspace": {
                    "request_id": "older-stale-request",
                    "repo": str(coord),
                    "path": "/tmp/aiworkhub-worktrees/older-stale-request/worktree",
                    "home": "/tmp/aiworkhub-worktrees/older-stale-request/home",
                    "allowed_writes": ["src/stale.py"],
                    "parent_baseline": {},
                    "workspace_baseline": {},
                },
                "changed_path_hashes": {"src/stale.py": "s" * 64},
                "residual_identities": [],
                "pinned_at": "2026-07-18T00:00:00+00:00",
            },
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": current_request},
                    "workspace": current_workspace,
                    "changed_path_hashes": current_hashes,
                },
            },
        },
    )

    # No explicit predecessor_request_id -- defaults to current review
    res = core.reject_review("T_V2_DEFAULT", "rework", to="pending")

    assert res["ok"] is True, res
    card = json.loads(_row(coord, "T_V2_DEFAULT")["card_json"])
    # The freshly pinned rework_predecessor must be the CURRENT request,
    # not the stale older one.
    assert card["rework_predecessor"]["request_id"] == current_request
    assert card["rework_predecessor"]["changed_path_hashes"] == current_hashes
    assert card["review_feedback"]["predecessor_request_id"] == current_request


def test_reject_empty_predecessor_fails_closed(coord):
    """An empty predecessor_request_id string must fail closed before any
    card state change."""
    _insert(
        coord,
        "T_V2_EMPTY",
        card={
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": "some-request"},
                },
            },
        },
    )

    res = core.reject_review(
        "T_V2_EMPTY", "x", to="pending", predecessor_request_id=""
    )

    assert res["ok"] is False
    assert "predecessor_request_id" in (res.get("stderr") or "")
    # Card must be unchanged -- still in review
    row = _row(coord, "T_V2_EMPTY")
    assert row["status"] == "review"


def test_reject_foreign_predecessor_fails_closed(coord):
    """A predecessor_request_id not in any durable evidence must fail closed."""
    _insert(
        coord,
        "T_V2_FOREIGN",
        card={
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "known-request",
                "workspace": {
                    "request_id": "known-request",
                    "repo": str(coord),
                    "path": "/tmp/aiworkhub-worktrees/known-request/worktree",
                    "home": "/tmp/aiworkhub-worktrees/known-request/home",
                    "allowed_writes": ["src/z.py"],
                    "parent_baseline": {},
                    "workspace_baseline": {},
                },
                "changed_path_hashes": {"src/z.py": "z" * 64},
                "residual_identities": [],
                "pinned_at": "2026-07-19T00:00:00+00:00",
            },
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": "current-review"},
                },
            },
        },
    )

    res = core.reject_review(
        "T_V2_FOREIGN",
        "x",
        to="pending",
        predecessor_request_id="unknown-foreign-request",
    )

    assert res["ok"] is False
    assert res["stderr"] == "predecessor_request_id_stale:unknown-foreign-request"
    row = _row(coord, "T_V2_FOREIGN")
    assert row["status"] == "review"


def test_reject_adversarial_ab_preserves_green_a_when_b_rejected(coord):
    """Adversarial A/B: A is a green (review_ready) predecessor, B is the
    current validation_failed review.  Explicitly selecting A as predecessor
    must preserve A's workspace while B is rejected."""
    green_request = "green-candidate-A"
    task_id = "T_V2_AB"
    green_workspace = _strict_retained_workspace(
        coord, task_id, green_request, ["src/green.py"]
    )
    green_delta, green_hashes = _strict_delta_descriptor(
        coord, task_id, green_request, "src/green.py", b"green A\n"
    )
    # Previously green review cycle produced a rework_predecessor for A.
    # The current (B) terminal_review is validation_failed with its own
    # workspace evidence.
    _insert(
        coord,
        task_id,
        card={
            "claim_epoch": 1,
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": green_request,
                "task_id": task_id,
                "claim_epoch": 1,
                "workspace": green_workspace,
                "changed_path_hashes": green_hashes,
                "rework_delta": green_delta,
                "residual_identities": [],
                "pinned_at": "2026-07-19T00:00:00+00:00",
            },
            "terminal_review": {
                "substatus": "validation_failed",
                "evidence": {
                    "request_identity": {"request_id": "validation-failed-B"},
                    "workspace": {
                        "request_id": "validation-failed-B",
                        "repo": str(coord),
                        "path": "/tmp/aiworkhub-worktrees/validation-failed-B/worktree",
                        "home": "/tmp/aiworkhub-worktrees/validation-failed-B/home",
                        "allowed_writes": ["src/failed.py"],
                        "parent_baseline": {},
                        "workspace_baseline": {},
                    },
                    "changed_path_hashes": {"src/failed.py": "f" * 64},
                },
            },
        },
    )

    res = core.reject_review(
        task_id,
        "rework from green A",
        to="pending",
        predecessor_request_id=green_request,
    )

    assert res["ok"] is True, res
    card = json.loads(_row(coord, task_id)["card_json"])
    # Green A's workspace must be durably pinned, not B's
    assert card["rework_predecessor"]["request_id"] == green_request
    assert card["rework_predecessor"]["changed_path_hashes"] == green_hashes
    assert card["review_feedback"]["predecessor_request_id"] == green_request
    assert card["review_feedback"]["predecessor_changed_paths"] == ["src/green.py"]
    assert "terminal_review" not in card


def test_reject_blocked_after_validation_failed_defaults_to_current(coord):
    """When the current (B) review is valid but gets blocked for an unrelated
    reason, and no explicit predecessor is selected, the current review (B)
    is the safe default."""
    current_request = "current-valid-B"
    current_workspace = {
        "request_id": current_request,
        "repo": str(coord),
        "path": f"/tmp/aiworkhub-worktrees/{current_request}/worktree",
        "home": f"/tmp/aiworkhub-worktrees/{current_request}/home",
        "allowed_writes": ["src/valid.py"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    current_hashes = {"src/valid.py": "v" * 64}
    _insert(
        coord,
        "T_V2_BLOCKED_CURRENT",
        card={
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "older-predecessor-X",
                "workspace": {
                    "request_id": "older-predecessor-X",
                    "repo": str(coord),
                    "path": "/tmp/aiworkhub-worktrees/older-predecessor-X/worktree",
                    "home": "/tmp/aiworkhub-worktrees/older-predecessor-X/home",
                    "allowed_writes": ["src/old.py"],
                    "parent_baseline": {},
                    "workspace_baseline": {},
                },
                "changed_path_hashes": {"src/old.py": "o" * 64},
                "residual_identities": [],
                "pinned_at": "2026-07-18T00:00:00+00:00",
            },
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": current_request},
                    "workspace": current_workspace,
                    "changed_path_hashes": current_hashes,
                },
            },
        },
    )

    # Blocked with no explicit predecessor -- defaults to current review (B)
    res = core.reject_review(
        "T_V2_BLOCKED_CURRENT", "needs external input", to="blocked"
    )

    assert res["ok"] is True, res
    row = _row(coord, "T_V2_BLOCKED_CURRENT")
    assert row["status"] == "blocked"
    card = json.loads(row["card_json"])
    # The pinned rework_predecessor must be the current (B), not the stale older one
    assert card["rework_predecessor"]["request_id"] == current_request
    assert card["rework_predecessor"]["changed_path_hashes"] == current_hashes
    assert "terminal_review" not in card


def test_reject_explicit_nonmatching_hash_fails_closed(coord):
    """A predecessor_request_id that matches a stored request_id but whose
    workspace request_id does not agree fails closed."""
    task_id = "T_V2_HASH_MISMATCH"
    mismatched_workspace = _strict_retained_workspace(
        coord, task_id, "different-workspace-req", ["src/m.py"]
    )
    _insert(
        coord,
        task_id,
        card={
            "claim_epoch": 1,
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "hash-mismatch-req",
                "task_id": task_id,
                "claim_epoch": 1,
                "workspace": mismatched_workspace,
                "changed_path_hashes": {},
                "residual_identities": [],
                "pinned_at": "2026-07-19T00:00:00+00:00",
            },
        },
    )

    res = core.reject_review(
        task_id,
        "x",
        to="pending",
        predecessor_request_id="hash-mismatch-req",
    )

    assert res["ok"] is False
    assert "not found" in (res.get("stderr") or "")


def test_reject_workspace_missing_changed_hashes_not_selected(coord):
    """A predecessor with malformed changed_path_hashes is not selectable."""
    task_id = "T_V2_EMPTY_HASHES"
    request_id = "empty-hash-req"
    workspace = _strict_retained_workspace(coord, task_id, request_id, [])
    _insert(
        coord,
        task_id,
        card={
            "claim_epoch": 1,
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": request_id,
                "task_id": task_id,
                "claim_epoch": 1,
                "workspace": workspace,
                "changed_path_hashes": None,
                "residual_identities": [],
                "pinned_at": "2026-07-19T00:00:00+00:00",
            },
        },
    )

    res = core.reject_review(
        task_id,
        "x",
        to="pending",
        predecessor_request_id=request_id,
    )

    assert res["ok"] is False
    assert res["stderr"] == "predecessor_request_id_missing_hashes"


def test_reject_none_predecessor_omission_is_not_empty_selection(coord):
    """None (omitted) is distinct from "" (empty).  Omission defaults to
    the current review; empty fails closed."""
    current_request = "default-from-none"
    _insert(
        coord,
        "T_V2_NONE_VS_EMPTY",
        card={
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": current_request},
                    "workspace": {
                        "request_id": current_request,
                        "repo": str(coord),
                        "path": f"/tmp/aiworkhub-worktrees/{current_request}/worktree",
                        "home": f"/tmp/aiworkhub-worktrees/{current_request}/home",
                        "allowed_writes": ["out/result.txt"],
                        "parent_baseline": {},
                        "workspace_baseline": {},
                    },
                    "changed_path_hashes": {"out/result.txt": "r" * 64},
                },
            },
        },
    )

    # None (omitted) must succeed
    res_none = core.reject_review("T_V2_NONE_VS_EMPTY", "rework", to="pending")
    assert res_none["ok"] is True, res_none
    card_none = json.loads(_row(coord, "T_V2_NONE_VS_EMPTY")["card_json"])
    assert card_none["rework_predecessor"]["request_id"] == current_request

    # Now reset and try empty -- must fail
    _insert(
        coord,
        "T_V2_NONE_VS_EMPTY_2",
        card={
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": current_request},
                },
            },
        },
    )
    res_empty = core.reject_review(
        "T_V2_NONE_VS_EMPTY_2", "x", to="pending", predecessor_request_id=""
    )
    assert res_empty["ok"] is False
    assert "predecessor_request_id" in (res_empty.get("stderr") or "")


def _make_blocked_rework_task_with_terminal_review(
    root,
    task_id="nf50-validation-only-replay",
    *,
    request_id="req-abc123",
    changed_path_hashes=None,
    feedback_reason="fix the flaky assertion",
    include_predecessor_identity=True,
    terminal_failure=False,
    include_terminal_event=True,
):
    """Insert one blocked task card plus its append-only terminal event.

    Single correct local helper for validation_only_replay tests: creates the
    task row (status=blocked, with rework_predecessor and workspace) and
    appends the matching terminal_review or terminal_failure task_event row.
    """
    import json as _json
    from datetime import datetime, timezone

    if changed_path_hashes is None:
        changed_path_hashes = {"src/example.py": "deadbeef"}

    now = datetime.now(timezone.utc).isoformat()
    rework_predecessor = {"workspace": "/tmp/nf50-workspace"}
    if include_predecessor_identity:
        rework_predecessor["request_id"] = request_id
        rework_predecessor["changed_path_hashes"] = changed_path_hashes

    card = {
        "topic": "nf50",
        "claim_epoch": 1,
        "rework_predecessor": rework_predecessor,
        "reject_review": {"reason": feedback_reason},
    }
    card_json = _json.dumps(card, ensure_ascii=False, sort_keys=True)

    _readiness, db_path = task_store._require_ready(root)
    conn = task_store._connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, status, worker_status, "
            "claimed_by, claimed_at, card_json, created_at, updated_at) "
            "VALUES (?, 'codex', 'blocked', 'blocked', NULL, NULL, ?, ?, ?)",
            (task_id, card_json, now, now),
        )
        terminal_review_payload = {
            "substatus": "validation_failed",
            "runner": "codex",
            "recorded_at": now,
            "claim_epoch": 1,
            "evidence": {"changed_path_hashes": changed_path_hashes},
        }
        if include_terminal_event:
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, ?, 'codex', ?, ?)",
                (
                    task_id,
                    "terminal_failure" if terminal_failure else "terminal_review",
                    _json.dumps(
                        terminal_review_payload, ensure_ascii=False, sort_keys=True
                    ),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return task_id


def test_recover_blocked_rework_validation_only_replay_default_false_unchanged(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(root)
    ok, state = task_store.recover_blocked_rework(
        root, task_id, actor="coordinator", feedback_reason="fix flaky test"
    )
    assert ok is True
    assert state == "recovered"
    card = task_store.get_task(root, task_id)
    assert "validation_only_replay_authorization" not in card


def test_reviewer_transport_recovery_rejects_workspace_repository_mismatch(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(root)
    card = task_store.get_task(root, task_id)
    assert card is not None
    card["rework_predecessor"] = {
        "request_id": "a" * 32,
        "task_id": task_id,
        "changed_path_hashes": {"result.py": "b" * 64},
        "workspace": {
            "request_id": "a" * 32,
            "repo": str(tmp_path / "different-repository"),
            "path": str(tmp_path / "workspace"),
        },
    }
    _readiness, db_path = task_store._require_ready(root)
    conn = task_store._connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (
                json.dumps(task_store.persistable_card_payload(card), sort_keys=True),
                task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        feedback_reason="rerun validation",
        validation_only_replay=True,
    )
    assert (ok, state) == (False, "validation_only_replay_workspace_invalid")
    assert task_store.get_task(root, task_id)["status"] == "blocked"


def test_recover_blocked_rework_validation_only_replay_no_terminal_failure_rejected(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(
        root, include_predecessor_identity=False, include_terminal_event=False
    )
    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor="coordinator",
        feedback_reason="fix flaky test",
        validation_only_replay=True,
    )
    assert ok is False
    assert state == "no_retained_predecessor_evidence"
    card = task_store.get_task(root, task_id)
    assert "validation_only_replay_authorization" not in card
    assert card.get("claim_epoch") == 1


def test_recover_blocked_rework_validation_only_replay_missing_evidence_rejected(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(
        root, include_predecessor_identity=False, terminal_failure=True
    )
    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor="coordinator",
        feedback_reason="fix flaky test",
        validation_only_replay=True,
    )
    assert ok is False
    assert state == "validation_only_replay_missing_evidence"


def test_recover_blocked_rework_validation_only_replay_persists_authorization(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(
        root,
        request_id="req-xyz789",
        changed_path_hashes={"a.py": "aaaa"},
        terminal_failure=True,
    )
    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor="coordinator",
        feedback_reason="fix flaky test",
        validation_only_replay=True,
    )
    assert ok is True
    assert state == "recovered"
    card = task_store.get_task(root, task_id)
    auth = card.get("validation_only_replay_authorization")
    assert auth is not None
    assert auth["task_id"] == task_id
    assert auth["actor"] == "coordinator"
    assert auth["predecessor_request_id"] == "req-xyz789"
    assert auth["changed_path_hashes"] == {"a.py": "aaaa"}
    assert auth["next_claim_epoch"] == card["claim_epoch"] == 2
    assert "authorized_at" in auth


def test_pending_rework_rebinds_validation_only_replay_to_latest_episode(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    first_request = "a" * 32
    second_request = "b" * 32
    task_id = _make_blocked_rework_task_with_terminal_review(
        root,
        request_id=first_request,
        changed_path_hashes={"a.py": "1" * 64},
        terminal_failure=True,
    )
    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor="coordinator",
        feedback_reason="run validation",
        validation_only_replay=True,
    )
    assert (ok, state) == (True, "recovered")

    # Model the completed replay being rejected back to pending: the current
    # retained predecessor and claim epoch advance, while the old one-episode
    # authorization remains on the decoded card until recovery refreshes it.
    card = task_store.get_task(root, task_id)
    assert card is not None
    card["claim_epoch"] = 3
    card["rework_predecessor"] = {
        "request_id": second_request,
        "changed_path_hashes": {"b.py": "2" * 64},
    }
    card["operational_blocker"] = {
        "kind": "launch_blocked",
        "reason": "validation_only_replay_predecessor_mismatch",
    }
    _readiness, db_path = task_store._require_ready(root)
    conn = task_store._connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (
                json.dumps(
                    task_store.persistable_card_payload(card),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor="coordinator",
        feedback_reason="replay current retained candidate",
        validation_only_replay=True,
    )

    assert (ok, state) == (True, "recovered_validation_only_replay")
    rebound = task_store.get_task(root, task_id)
    assert rebound is not None
    auth = rebound["validation_only_replay_authorization"]
    assert auth["predecessor_request_id"] == second_request
    assert auth["changed_path_hashes"] == {"b.py": "2" * 64}
    assert auth["next_claim_epoch"] == rebound["claim_epoch"] == 3
    assert "operational_blocker" not in rebound
    events = task_store.get_task_events(root, task_id)
    assert sum(
        event["event"] == "blocked_rework_validation_replay_reauthorized"
        for event in events
    ) == 1


def test_recover_blocked_rework_ordinary_recovery_regression(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(root)
    ok, state = task_store.recover_blocked_rework(
        root, task_id, actor="coordinator", feedback_reason="fix flaky test"
    )
    assert ok is True
    assert state == "recovered"
    card = task_store.get_task(root, task_id)
    assert card.get("claim_epoch") == 2
    assert card.get("recovered_by") == "coordinator"
    assert "validation_only_replay_authorization" not in card


def _persist_required_outputs(root, task_id, outputs):
    card = task_store.get_task(root, task_id)
    assert card is not None
    card["required_outputs"] = list(outputs)
    _readiness, db_path = task_store._require_ready(root)
    conn = task_store._connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (
                json.dumps(
                    task_store.persistable_card_payload(card),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return task_store.get_task(root, task_id)


def _consume_event_count(root, task_id):
    return sum(
        event["event"] == "blocked_rework_validation_replay_authorization_consumed"
        for event in task_store.get_task_events(root, task_id)
    )


def _probe_process_manager_launch_lane(root, task_id, tmp_path, monkeypatch):
    selected = []

    def _preflight(self, probed_task_id, runner, topic, adapter_id):
        assert probed_task_id == task_id
        current = task_store.get_task(root, probed_task_id)
        assert current is not None
        return task_store.persistable_card_payload(current)

    def _replay(self, **kwargs):
        selected.append("validation_only_replay")
        return {
            "ok": True,
            "execution_mode": "validation_only_replay",
            "provider_launched": False,
        }

    def _provider_dirs(card, adapter_id):
        selected.append("provider")
        raise process_launcher.LaunchRejected("provider_lane_selected")

    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(
        process_launcher, "_validate_adapter_identity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        process_launcher,
        "_enforce_quality_review_launch_binding",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager, "_preflight_card", _preflight
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_launch_validation_only_replay",
        _replay,
    )
    monkeypatch.setattr(
        process_launcher, "_external_readonly_dirs", _provider_dirs
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "record_launch_blocker",
        lambda *a, **k: {"ok": True},
    )
    manager = process_launcher.ProcessManager(
        repo=Path(root),
        isolation_enabled=False,
        process_dir=tmp_path / "nf393-process",
        process_log_path=tmp_path / "nf393-process.log",
        collision_guard=lambda **kwargs: {"returncode": 0},
    )
    result = manager._launch_isolated(
        task_id=task_id,
        runner="codex_coding",
        topic="nf50",
        adapter_id="codex_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
    )
    return selected, result


def test_pending_ordinary_recovery_consumes_stale_validation_only_replay_authorization(
    tmp_path,
):
    root = tmp_path
    task_store.initialize_repository(root)
    request_id = "a" * 32
    path_hash = "d" * 64
    task_id = _make_blocked_rework_task_with_terminal_review(
        root,
        task_id="nf393-ordinary-consume-token",
        request_id=request_id,
        changed_path_hashes={"src/example.py": path_hash},
        terminal_failure=True,
    )
    actor = core.CODEX_RUNNER
    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="replay validation",
        validation_only_replay=True,
    )
    assert (ok, state) == (True, "recovered")
    first = task_store.get_task(root, task_id)
    assert first is not None
    first_auth = first["validation_only_replay_authorization"]
    claim_epoch = first["claim_epoch"]
    assert claim_epoch == first_auth["next_claim_epoch"] == 2

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="switch to ordinary rework",
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
    )
    assert (ok, state) == (
        True,
        "consumed_stale_validation_only_replay_authorization",
    )
    consumed = task_store.get_task(root, task_id)
    assert consumed is not None
    assert "validation_only_replay_authorization" not in consumed
    assert consumed["claim_epoch"] == claim_epoch
    assert _consume_event_count(root, task_id) == 1
    consume_events = [
        event
        for event in task_store.get_task_events(root, task_id)
        if event["event"]
        == "blocked_rework_validation_replay_authorization_consumed"
    ]
    payload = json.loads(consume_events[0]["payload"])
    assert payload["claim_epoch"] == claim_epoch
    assert payload["consumed_authorization"]["next_claim_epoch"] == 2
    assert payload["consumed_authorization"]["predecessor_request_id"] == request_id

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="switch to ordinary rework",
        validation_only_replay=False,
    )
    assert (ok, state) == (True, "already_recovered")
    assert "validation_only_replay_authorization" not in task_store.get_task(
        root, task_id
    )
    assert _consume_event_count(root, task_id) == 1


def test_manager_bootstrap_hygiene_is_interval_throttled_and_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = _init_repo(tmp_path, "hygiene-bootstrap-a")
    root_b = _init_repo(tmp_path, "hygiene-bootstrap-b")
    current_root = [root_a]
    monkeypatch.setattr(core, "repo_root", lambda: current_root[0])
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_TASK_HYGIENE_LAST_RUNS", {})
    calls: list[Path] = []

    def hygiene(repo: Path) -> dict[str, object]:
        calls.append(repo)
        return {
            "ok": True,
            "scanned": 9,
            "eligible": 2,
            "archived": 1,
            "skipped": 1,
            "reasons": {"callback_live": 1},
        }

    monkeypatch.setattr(task_retention, "run_automatic_hygiene", hygiene)
    first_a = core.manager_bootstrap()
    current_root[0] = root_b
    first_b = core.manager_bootstrap()
    current_root[0] = root_a
    second_a = core.manager_bootstrap()

    assert calls == [root_a.resolve(), root_b.resolve()]
    expected_completed = {
        "scanned": 9,
        "eligible": 2,
        "archived": 1,
        "skipped": 1,
        "reasons": {"callback_live": 1},
        "state": "completed",
    }
    assert first_a["task_hygiene"] == expected_completed
    assert first_b["task_hygiene"] == expected_completed
    assert second_a["task_hygiene"] == {"state": "throttled"}

    monkeypatch.setattr(core, "_TASK_HYGIENE_LAST_RUNS", {})
    monkeypatch.setattr(
        task_retention,
        "run_automatic_hygiene",
        lambda _repo: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    failed = core.manager_bootstrap()
    assert failed["ok"] is True
    assert failed["task_hygiene"]["state"] == "skipped"


def test_pending_ordinary_recovery_absent_authorization_already_recovered(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(
        root, task_id="nf393-absent-authorization"
    )
    ok, state = task_store.recover_blocked_rework(
        root, task_id, actor="coordinator", feedback_reason="ordinary recover"
    )
    assert (ok, state) == (True, "recovered")
    assert "validation_only_replay_authorization" not in task_store.get_task(
        root, task_id
    )
    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor="coordinator",
        feedback_reason="ordinary recover",
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
    )
    assert (ok, state) == (True, "already_recovered")
    assert _consume_event_count(root, task_id) == 0


def test_ordinary_consume_selects_process_manager_provider_launch_lane(
    tmp_path, monkeypatch
):
    root = tmp_path
    task_store.initialize_repository(root)
    request_id = "a" * 32
    path_hash = "d" * 64
    task_id = _make_blocked_rework_task_with_terminal_review(
        root,
        task_id="nf393-process-manager-lane",
        request_id=request_id,
        changed_path_hashes={"src/example.py": path_hash},
        terminal_failure=True,
    )
    actor = core.CODEX_RUNNER
    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="replay validation",
        validation_only_replay=True,
    )
    assert (ok, state) == (True, "recovered")
    _persist_required_outputs(root, task_id, ["src/example.py"])
    before, replay_result = _probe_process_manager_launch_lane(
        root, task_id, tmp_path, monkeypatch
    )
    assert before == ["validation_only_replay"]
    assert replay_result["execution_mode"] == "validation_only_replay"
    assert replay_result["provider_launched"] is False

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="switch to ordinary rework",
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
    )
    assert (ok, state) == (
        True,
        "consumed_stale_validation_only_replay_authorization",
    )
    after, provider_result = _probe_process_manager_launch_lane(
        root, task_id, tmp_path, monkeypatch
    )
    assert after == ["provider"]
    assert provider_result["ok"] is False
    assert "provider_lane_selected" in str(provider_result.get("blocked_reason") or "")
    assert process_launcher._validation_only_replay_authorization(
        task_store.get_task(root, task_id), task_id
    ) is None


def test_second_block_ordinary_recovery_fences_claim_epoch_and_launch(
    tmp_path, monkeypatch
):
    root = tmp_path
    task_store.initialize_repository(root)
    request_id = "a" * 32
    path_hash = "d" * 64
    task_id = _make_blocked_rework_task_with_terminal_review(
        root,
        task_id="nf393-second-block-ordinary",
        request_id=request_id,
        changed_path_hashes={"src/example.py": path_hash},
        terminal_failure=True,
    )
    actor = core.CODEX_RUNNER
    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="replay validation",
        validation_only_replay=True,
    )
    assert (ok, state) == (True, "recovered")
    first = _persist_required_outputs(root, task_id, ["src/example.py"])
    first_auth = dict(first["validation_only_replay_authorization"])
    assert first_auth["next_claim_epoch"] == 2
    first_lane, _ = _probe_process_manager_launch_lane(
        root, task_id, tmp_path, monkeypatch
    )
    assert first_lane == ["validation_only_replay"]

    _readiness, db_path = task_store._require_ready(root)
    conn = task_store._connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status='blocked', worker_status='blocked' "
            "WHERE task_id=?",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="second block",
        validation_only_replay=True,
    )
    assert (ok, state) == (True, "recovered")
    second = _persist_required_outputs(root, task_id, ["src/example.py"])
    assert second["claim_epoch"] == 3
    assert second["validation_only_replay_authorization"]["next_claim_epoch"] == 3
    assert second["claim_epoch"] != first_auth["next_claim_epoch"]

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="ordinary after second block",
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
    )
    assert (ok, state) == (
        True,
        "consumed_stale_validation_only_replay_authorization",
    )
    consumed = task_store.get_task(root, task_id)
    assert consumed is not None
    assert "validation_only_replay_authorization" not in consumed
    assert consumed["claim_epoch"] == 3
    assert _consume_event_count(root, task_id) == 1
    after, _ = _probe_process_manager_launch_lane(
        root, task_id, tmp_path, monkeypatch
    )
    assert after == ["provider"]

    copied = task_store.persistable_card_payload(consumed)
    copied["validation_only_replay_authorization"] = first_auth
    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        process_launcher._validation_only_replay_authorization(copied, task_id)
    assert "claim_epoch_mismatch" in str(excinfo.value)

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="rebind current episode",
        validation_only_replay=True,
    )
    assert (ok, state) == (True, "recovered_validation_only_replay")
    rebound = task_store.get_task(root, task_id)
    auth = rebound["validation_only_replay_authorization"]
    assert auth["predecessor_request_id"] == request_id
    assert auth["changed_path_hashes"] == {"src/example.py": path_hash}
    assert auth["next_claim_epoch"] == rebound["claim_epoch"] == 3
    assert auth["next_claim_epoch"] != first_auth["next_claim_epoch"]

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        actor=actor,
        feedback_reason="clean root unchanged",
        clean_root_if_predecessor_missing=True,
    )
    assert ok is False
    assert state in {
        "clean_root_rework_predecessor_invalid",
        "clean_root_rework_workspace_invalid",
        "clean_root_rework_identity_mismatch",
        "clean_root_rework_workspace_still_available",
    }
    after_clean_root = task_store.get_task(root, task_id)
    assert after_clean_root is not None
    assert after_clean_root["claim_epoch"] == 3
    assert _consume_event_count(root, task_id) == 1
