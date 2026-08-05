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
from aiworkhub import callback_store, core, task_store  # noqa: E402
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


# --- issue 7: terminal substatus -> callback transition map ----------------

def test_callback_transition_map_is_exhaustive_for_blocked_substatuses():
    assert callback_store.resolve_callback_transition("dependency_blocked") == "blocked"
    assert callback_store.resolve_callback_transition("liveness_lost") == "blocked"
    assert callback_store.resolve_callback_transition("required_output_unchanged") == "validation_failed"
    # unchanged mappings still hold
    assert callback_store.resolve_callback_transition("validation_failed") == "validation_failed"
    assert callback_store.resolve_callback_transition("review_ready") == "review_ready"
    assert callback_store.resolve_callback_transition("blocked") == "blocked"
