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
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiworkhub  # noqa: E402
from aiworkhub import callback_store, core, task_store, worker_workspace  # noqa: E402
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
def test_reject_to_pending_fails_closed_on_spoofed_rework_delta(
    coord, mutation, expected
):
    task_id = "T_DELTA_BAD_" + expected
    request_id = "e" * 32
    card, descriptor = _terminal_rework_delta_card(coord, task_id, request_id)
    mutation(descriptor)
    _insert(coord, task_id, card=card)

    result = core.reject_review(task_id, "repair delta", to="pending")

    assert result["ok"] is False
    assert expected in result["stderr"]
    row = _row(coord, task_id)
    assert row["status"] == "review"


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


def test_reject_to_pending_explicit_predecessor_selects_retained_workspace(coord):
    """Explicit predecessor_request_id selects a durably-pinned retained
    workspace from a prior rework cycle rather than defaulting to the current
    terminal_review."""
    retained_request = "retained-request-A"
    retained_workspace = {
        "request_id": retained_request,
        "repo": str(coord),
        "path": f"/tmp/aiworkhub-worktrees/{retained_request}/worktree",
        "home": f"/tmp/aiworkhub-worktrees/{retained_request}/home",
        "allowed_writes": ["src/a.py"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    retained_hashes = {"src/a.py": "a" * 64}
    # Card has a durably-pinned rework_predecessor from cycle A plus a current
    # terminal_review from cycle B (validation_failed).
    _insert(
        coord,
        "T_V2_EXPLICIT",
        card={
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": retained_request,
                "workspace": retained_workspace,
                "changed_path_hashes": retained_hashes,
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
        "T_V2_EXPLICIT",
        "rework from retained A",
        to="pending",
        predecessor_request_id=retained_request,
    )

    assert res["ok"] is True, res
    card = json.loads(_row(coord, "T_V2_EXPLICIT")["card_json"])
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
    retained_workspace = {
        "request_id": retained_request,
        "repo": str(coord),
        "path": f"/tmp/aiworkhub-worktrees/{retained_request}/worktree",
        "home": f"/tmp/aiworkhub-worktrees/{retained_request}/home",
        "allowed_writes": ["src/x.py"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    retained_hashes = {"src/x.py": "x" * 64}
    _insert(
        coord,
        "T_V2_BLOCKED_PIN",
        card={
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": retained_request,
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
        "T_V2_BLOCKED_PIN",
        "blocked with explicit A",
        to="blocked",
        predecessor_request_id=retained_request,
    )

    assert res["ok"] is True, res
    row = _row(coord, "T_V2_BLOCKED_PIN")
    assert row["status"] == "blocked"
    assert row["worker_status"] == "blocked"
    card = json.loads(row["card_json"])
    assert card["rework_predecessor"]["request_id"] == retained_request
    assert card["rework_predecessor"]["changed_path_hashes"] == retained_hashes
    assert "terminal_review" not in card


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
    assert "not found" in (res.get("stderr") or "")
    row = _row(coord, "T_V2_FOREIGN")
    assert row["status"] == "review"


def test_reject_adversarial_ab_preserves_green_a_when_b_rejected(coord):
    """Adversarial A/B: A is a green (review_ready) predecessor, B is the
    current validation_failed review.  Explicitly selecting A as predecessor
    must preserve A's workspace while B is rejected."""
    green_request = "green-candidate-A"
    green_workspace = {
        "request_id": green_request,
        "repo": str(coord),
        "path": f"/tmp/aiworkhub-worktrees/{green_request}/worktree",
        "home": f"/tmp/aiworkhub-worktrees/{green_request}/home",
        "allowed_writes": ["src/green.py"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    green_hashes = {"src/green.py": "g" * 64}
    # Previously green review cycle produced a rework_predecessor for A.
    # The current (B) terminal_review is validation_failed with its own
    # workspace evidence.
    _insert(
        coord,
        "T_V2_AB",
        card={
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": green_request,
                "workspace": green_workspace,
                "changed_path_hashes": green_hashes,
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
        "T_V2_AB",
        "rework from green A",
        to="pending",
        predecessor_request_id=green_request,
    )

    assert res["ok"] is True, res
    card = json.loads(_row(coord, "T_V2_AB")["card_json"])
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
    _insert(
        coord,
        "T_V2_HASH_MISMATCH",
        card={
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "hash-mismatch-req",
                "workspace": {
                    "request_id": "different-workspace-req",
                    "repo": str(coord),
                    "path": "/tmp/aiworkhub-worktrees/different-workspace-req/worktree",
                    "home": "/tmp/aiworkhub-worktrees/different-workspace-req/home",
                    "allowed_writes": ["src/m.py"],
                    "parent_baseline": {},
                    "workspace_baseline": {},
                },
                "changed_path_hashes": {},
                "residual_identities": [],
                "pinned_at": "2026-07-19T00:00:00+00:00",
            },
        },
    )

    res = core.reject_review(
        "T_V2_HASH_MISMATCH",
        "x",
        to="pending",
        predecessor_request_id="hash-mismatch-req",
    )

    assert res["ok"] is False
    assert "not found" in (res.get("stderr") or "")


def test_reject_workspace_missing_changed_hashes_not_selected(coord):
    """A predecessor with empty changed_path_hashes is not selectable --
    the coordinator must not guess at workspace identity."""
    _insert(
        coord,
        "T_V2_EMPTY_HASHES",
        card={
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "empty-hash-req",
                "workspace": {
                    "request_id": "empty-hash-req",
                    "repo": str(coord),
                    "path": "/tmp/aiworkhub-worktrees/empty-hash-req/worktree",
                    "home": "/tmp/aiworkhub-worktrees/empty-hash-req/home",
                    "allowed_writes": [],
                    "parent_baseline": {},
                    "workspace_baseline": {},
                },
                "changed_path_hashes": {},
                "residual_identities": [],
                "pinned_at": "2026-07-19T00:00:00+00:00",
            },
        },
    )

    res = core.reject_review(
        "T_V2_EMPTY_HASHES",
        "x",
        to="pending",
        predecessor_request_id="empty-hash-req",
    )

    assert res["ok"] is False
    assert "not found" in (res.get("stderr") or "")


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
):
    """Insert one blocked task card plus its append-only terminal_review event.

    Single correct local helper for validation_only_replay tests: creates the
    task row (status=blocked, with rework_predecessor and workspace) and
    appends the matching terminal_review task_event row.
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
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_review', 'codex', ?, ?)",
            (
                task_id,
                _json.dumps(terminal_review_payload, ensure_ascii=False, sort_keys=True),
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


def test_recover_blocked_rework_validation_only_replay_missing_evidence_rejected(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(
        root, include_predecessor_identity=False
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
    card = task_store.get_task(root, task_id)
    assert "validation_only_replay_authorization" not in card
    assert card.get("claim_epoch") == 1


def test_recover_blocked_rework_validation_only_replay_persists_authorization(tmp_path):
    root = tmp_path
    task_store.initialize_repository(root)
    task_id = _make_blocked_rework_task_with_terminal_review(
        root, request_id="req-xyz789", changed_path_hashes={"a.py": "aaaa"}
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
