from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_engine, task_store  # noqa: E402


def _repo_with_task(tmp_path: Path, *, status: str = "processing") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-07-22T00:00:00+00:00"
    card = {
        "task_id": "TASK_B891",
        "runner": "codex_worker_b891",
        "topic": "task_mcp",
        "allowed_writes": ["out.txt"],
        "callback_required": True,
        "coordinator_provider": "codex",
        "origin_thread_id": "thread-b891",
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, origin_thread_id) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
            (
                "TASK_B891",
                "codex_worker_b891",
                "task_mcp",
                status,
                "claimed" if status == "processing" else "unclaimed",
                json.dumps(card),
                now,
                now,
                "codex_worker_b891" if status == "processing" else "",
                now if status == "processing" else "",
                now if status == "processing" else "",
                "thread-b891",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


@pytest.mark.parametrize(
    "substatus",
    [
        "worker_failed",
        "validation_failed",
        "required_output_unchanged",
        "blocked",
        "cancelled",
        "launch_failed",
        "liveness_lost",
    ],
)
def test_terminal_outcomes_route_to_review_with_exact_substatus(tmp_path: Path, substatus: str) -> None:
    repo = _repo_with_task(tmp_path)
    result = task_engine.mark_terminal_review(
        repo,
        "TASK_B891",
        "codex_worker_b891",
        substatus,
        evidence={"error": substatus, "request_id": "req-b891"},
    )
    assert result["ok"] is True
    assert result["callback_enqueued"] is True

    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"
    assert card["worker_status"] == "review"
    assert card["terminal_substatus"] == substatus
    assert card["terminal_review"]["evidence"]["error"] == substatus
    events = task_store.get_task_events(repo, "TASK_B891")
    assert [event["event"] for event in events] == ["callback_enqueued", "terminal_review"]


def _repo_with_review_ready_task(tmp_path: Path, *, request_id: str = "req-accept-b1") -> Path:
    repo = _repo_with_task(tmp_path, status="review")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by='codex_worker_b891' WHERE task_id=?",
            ("TASK_B891",),
        )
        card = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", ("TASK_B891",)
            ).fetchone()[0]
        )
        card["terminal_review"] = {
            "substatus": "review_ready",
            "evidence": {
                "changed_paths": ["out.txt"],
                "changed_path_hashes": {"out.txt": "deadbeef"},
                "request_identity": {
                    "request_id": request_id,
                    "task_id": "TASK_B891",
                    "runner": "codex_worker_b891",
                    "topic": "task_mcp",
                },
            },
        }
        conn.execute(
            "UPDATE tasks SET status='review', worker_status='review', card_json=? "
            "WHERE task_id=?",
            (json.dumps(card), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def test_accept_review_promotes_and_finishes(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    result = task_engine.accept_review(
        repo,
        "TASK_B891",
        runner="codex_worker_b891",
        topic="task_mcp",
        request_id="req-accept-b1",
        evidence={"promoted_paths": ["out.txt"]},
    )
    assert result["ok"] is True

    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "finished"
    assert card["worker_status"] == "done"
    assert card["accepted_request_id"] == "req-accept-b1"
    events = task_store.get_task_events(repo, "TASK_B891")
    assert events[0]["event"] == "accept_review"


def test_missing_review_workspace_enqueues_finalize_failed_callback(tmp_path: Path) -> None:
    request_id = "req-review-workspace-gone"
    repo = _repo_with_review_ready_task(tmp_path, request_id=request_id)

    result = task_engine.mark_review_workspace_missing(
        repo,
        "TASK_B891",
        "codex_worker_b891",
        request_id,
        reason="review_workspace_missing",
    )

    assert result["ok"] is True
    assert result["callback_enqueued"] is True
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "blocked"
    assert card["worker_status"] == "finalize_failed"
    assert card["claimed_by"] is None
    events = [row["event"] for row in task_store.get_task_events(repo, "TASK_B891")]
    assert "review_workspace_missing" in events
    assert "callback_enqueued" in events


def test_accept_review_idempotent_retry_returns_already_accepted(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    first = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert first["ok"] is True
    retry = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert retry["ok"] is True
    assert json.loads(retry["stdout"])["already_accepted"] is True


def test_accept_review_rejects_different_request_after_finish(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    first = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert first["ok"] is True
    other = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-other",
    )
    assert other["ok"] is False
    assert "already_finished" in other["stderr"]


def test_accept_review_rejects_non_review_ready_substatus(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        card = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", ("TASK_B891",)
            ).fetchone()[0]
        )
        card["terminal_review"]["substatus"] = "worker_failed"
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    result = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert result["ok"] is False
    assert "terminal_substatus_not_review_ready" in result["stderr"]
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"


def _repo_with_reviewer_children(
    tmp_path: Path, *, parent_task_id: str = "PARENT_T1",
    child_verified: str = "REVIEWER_V1", child_sibling: str = "REVIEWER_S1",
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-08T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        # Parent task in review_ready
        parent_card = {
            "task_id": parent_task_id,
            "runner": "worker_p1",
            "topic": "task_mcp",
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {
                        "request_id": "req-parent-1",
                        "task_id": parent_task_id,
                        "runner": "worker_p1",
                        "topic": "task_mcp",
                    },
                },
            },
        }
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by, claimed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (parent_task_id, "worker_p1", "task_mcp", "review", "review",
             "", "", json.dumps(parent_card), now, now, "worker_p1", now),
        )
        # Verified reviewer child
        verified_card = {
            "task_id": child_verified,
            "runner": "reviewer_v1",
            "topic": "quality_review",
            "quality_review": {
                "target_task_id": parent_task_id,
                "target_request_id": "req-parent-1",
            },
        }
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by, claimed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_verified, "reviewer_v1", "quality_review", "review", "review",
             "", "", json.dumps(verified_card), now, now, "reviewer_v1", now),
        )
        # Sibling (redundant) reviewer child
        sibling_card = {
            "task_id": child_sibling,
            "runner": "reviewer_s1",
            "topic": "quality_review",
            "quality_review": {
                "target_task_id": parent_task_id,
                "target_request_id": "req-parent-1",
            },
        }
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by, claimed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_sibling, "reviewer_s1", "quality_review", "review", "review",
             "", "", json.dumps(sibling_card), now, now, "reviewer_s1", now),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def test_disposition_reviewer_children_finalizes_verified_and_supersedes_siblings(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    result = task_engine.disposition_reviewer_children(
        repo,
        "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert result["ok"] is True

    verified = task_store.get_task(repo, "REVIEWER_V1")
    assert verified is not None
    assert verified["status"] == "finished"
    assert verified["worker_status"] == "done"

    sibling = task_store.get_task(repo, "REVIEWER_S1")
    assert sibling is not None
    assert sibling["status"] == "superseded"
    assert sibling["worker_status"] == "superseded"


def test_disposition_reviewer_children_reads_terminal_review_evidence_binding(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", ("REVIEWER_V1",)
        ).fetchone()
        card = json.loads(row[0])
        binding = card.pop("quality_review")
        card["terminal_review"] = {"evidence": {"quality_review": binding}}
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "REVIEWER_V1"),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.disposition_reviewer_children(
        repo,
        "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    payload = json.loads(result["stdout"])
    assert payload["finalized"] == ["REVIEWER_V1"]
    verified = task_store.get_task(repo, "REVIEWER_V1")
    assert verified is not None
    assert verified["status"] == "finished"
    assert verified["worker_status"] == "done"


def test_disposition_reviewer_children_rejects_conflicting_durable_bindings(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", ("REVIEWER_V1",)
        ).fetchone()
        card = json.loads(row[0])
        card["terminal_review"] = {
            "evidence": {
                "quality_review": {
                    "target_task_id": "OTHER_PARENT",
                    "target_request_id": "req-other",
                }
            }
        }
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "REVIEWER_V1"),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.disposition_reviewer_children(
        repo,
        "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    payload = json.loads(result["stdout"])
    assert payload["finalized"] == []
    assert payload["errors"] == ["REVIEWER_V1:reviewer_binding_conflict"]
    verified = task_store.get_task(repo, "REVIEWER_V1")
    assert verified is not None
    assert verified["status"] == "review"


def test_disposition_reviewer_children_idempotent_retry(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    first = task_engine.disposition_reviewer_children(
        repo, "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert first["ok"] is True
    second = task_engine.disposition_reviewer_children(
        repo, "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert second["ok"] is True
    payload = json.loads(second["stdout"])
    assert "REVIEWER_V1" in payload["skipped"]
    assert "REVIEWER_S1" in payload["skipped"]


def test_disposition_reviewer_children_preserves_receipt_evidence(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    task_engine.disposition_reviewer_children(
        repo, "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    verified = task_store.get_task(repo, "REVIEWER_V1")
    assert verified is not None
    assert verified.get("topic") == "quality_review"
    qr = verified.get("quality_review") or {}
    assert qr.get("target_task_id") == "PARENT_T1"
    disposition_meta = verified.get("reviewer_disposition") or {}
    assert disposition_meta.get("parent_task_id") == "PARENT_T1"
    sibling = task_store.get_task(repo, "REVIEWER_S1")
    assert sibling is not None
    qr_sib = sibling.get("quality_review") or {}
    assert qr_sib.get("target_task_id") == "PARENT_T1"


def test_disposition_reviewer_children_ignores_non_quality_review_tasks(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    # Add a task with topic task_mcp that is NOT quality_review
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-08T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("IMPL_T1", "worker_i1", "task_mcp", "review", "review",
             "", "", json.dumps({"task_id": "IMPL_T1"}), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.disposition_reviewer_children(
        repo, "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert result["ok"] is True
    impl = task_store.get_task(repo, "IMPL_T1")
    assert impl is not None
    assert impl["status"] == "review"  # unchanged


def test_disposition_reviewer_children_ignores_request_mismatched_sibling(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", ("REVIEWER_S1",)
        ).fetchone()
        card = json.loads(row[0])
        card["quality_review"]["target_request_id"] = "req-other-candidate"
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "REVIEWER_S1"),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.disposition_reviewer_children(
        repo,
        "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert result["ok"] is True
    sibling = task_store.get_task(repo, "REVIEWER_S1")
    assert sibling is not None
    assert sibling["status"] == "review"
    assert sibling["worker_status"] == "review"


_VALID_HASH = "a" * 64
_REQ_OLD = "req-validation-old"
_REQ_NEW = "req-validation-new"


def _validation_failed_card(
    tmp_path: Path,
    *,
    request_id: str = _REQ_OLD,
    task_id: str = "TASK_B891",
    claim_epoch: int = 3,
    changed_paths: list[str] | None = None,
    hashes: dict[str, str] | None = None,
    workspace: dict[str, object] | None = None,
    repo_path: Path | None = None,
    retain_workspace: bool = True,
) -> dict[str, object]:
    authority = (repo_path or tmp_path).resolve()
    if workspace is None:
        ws_dir = tmp_path / f"ws-{request_id}"
        if retain_workspace:
            ws_dir.mkdir(parents=True, exist_ok=True)
        workspace = {
            "path": str(ws_dir),
            "repo": str(authority),
            "request_id": request_id,
            "task_id": task_id,
        }
    if changed_paths is None:
        changed_paths = []
    if hashes is None:
        hashes = {path: _VALID_HASH for path in changed_paths}
    return {
        "task_id": task_id,
        "claim_epoch": claim_epoch,
        "terminal_review": {
            "substatus": "validation_failed",
            "claim_epoch": claim_epoch,
            "evidence": {
                "changed_paths": list(changed_paths),
                "changed_path_hashes": dict(hashes),
                "request_id": request_id,
                "request_identity": {
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": "codex_worker_b891",
                    "topic": "task_mcp",
                },
                "workspace": workspace,
            },
        },
    }


def _resolve(card: dict[str, object], predecessor: str | None, tmp_path: Path):
    return core._resolve_reject_review_predecessor(
        card,
        task_id="TASK_B891",
        authority_repo=tmp_path.resolve(),
        predecessor_request_id=predecessor,
    )


def test_reject_review_predecessor_omitted_keeps_implicit_selection(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=["out.txt"])
    resolved, error = _resolve(card, None, tmp_path)
    assert (resolved, error) == (None, None)


def test_reject_review_predecessor_validation_failed_empty_changed_paths(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[], hashes={})
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert error is None
    assert resolved is not None
    assert resolved["request_id"] == _REQ_OLD
    assert resolved["changed_path_hashes"] == {}


def test_reject_review_predecessor_validation_failed_nonempty_hashes(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=["out.txt"])
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert error is None
    assert resolved is not None
    assert resolved["request_id"] == _REQ_OLD
    assert resolved["changed_path_hashes"] == {"out.txt": _VALID_HASH}


def test_reject_review_predecessor_wrong_task_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, task_id="OTHER_TASK")
    card["task_id"] = "TASK_B891"
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_task_mismatch:expected=TASK_B891:got=OTHER_TASK"


def test_reject_review_predecessor_wrong_repo_fails_closed(tmp_path: Path) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    card = _validation_failed_card(tmp_path, repo_path=other)
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_repo_mismatch"


def test_reject_review_predecessor_claim_epoch_mismatch_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, claim_epoch=2)
    card["claim_epoch"] = 4
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_claim_epoch_mismatch:expected=4:got=2"


def test_reject_review_predecessor_missing_hashes_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=["out.txt"], hashes={})
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_missing_hashes"


@pytest.mark.parametrize(
    "changed_paths",
    [None, {}, ("out.txt",), [1], ["out.txt", "out.txt"], ["/etc/passwd"], ["../secret"]],
)
def test_reject_review_predecessor_malformed_changed_paths_fail_closed(
    tmp_path: Path, changed_paths: object
) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[], hashes={})
    card["terminal_review"]["evidence"]["changed_paths"] = changed_paths
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_missing_hashes"


def test_reject_review_predecessor_unretained_workspace_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(
        tmp_path,
        changed_paths=["out.txt"],
        retain_workspace=False,
    )
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_workspace_unretained"


def test_reject_review_predecessor_stale_request_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, request_id=_REQ_NEW)
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == f"predecessor_request_id_stale:{_REQ_OLD}"


def test_reject_review_predecessor_malformed_id_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path)
    resolved, error = _resolve(card, "not a valid id", tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_malformed:not a valid id"


def test_reject_review_predecessor_empty_id_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path)
    resolved, error = _resolve(card, "", tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id must be a non-empty request id or omitted"


def test_reject_review_predecessor_never_falls_back_to_newer_request(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, request_id=_REQ_NEW, changed_paths=["out.txt"])
    card["rework_predecessor"] = {
        "request_id": _REQ_OLD,
        "task_id": "TASK_B891",
        "claim_epoch": 3,
        "workspace": {
            "path": str(tmp_path / "missing-old"),
            "repo": str(tmp_path.resolve()),
            "request_id": _REQ_OLD,
            "task_id": "TASK_B891",
        },
        "changed_path_hashes": {"out.txt": _VALID_HASH},
    }
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_workspace_unretained"


def test_reject_review_predecessor_request_id_missing_task_id_optional_succeeds(
    tmp_path: Path,
) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=["out.txt"])
    del card["terminal_review"]["evidence"]["request_identity"]["task_id"]
    del card["terminal_review"]["evidence"]["workspace"]["task_id"]
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert error is None
    assert resolved is not None
    assert resolved["request_id"] == _REQ_OLD
    assert resolved["changed_path_hashes"] == {"out.txt": _VALID_HASH}


def test_reject_review_predecessor_explicit_implicit_seal_identical_lineage(
    tmp_path: Path,
) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=["out.txt"])
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert error is None
    assert resolved is not None
    evidence = card["terminal_review"]["evidence"]
    assert resolved["request_id"] == evidence["request_identity"]["request_id"]
    assert resolved["workspace"] == evidence["workspace"]
    assert resolved["changed_path_hashes"] == evidence["changed_path_hashes"]
    assert resolved["claim_epoch"] == card["terminal_review"]["claim_epoch"]


def test_reject_review_predecessor_missing_identity_request_id_fails_closed(
    tmp_path: Path,
) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[], hashes={})
    del card["terminal_review"]["evidence"]["request_identity"]["request_id"]
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == f"predecessor_request_id {_REQ_OLD} not found in retained review evidence"


def test_reject_review_predecessor_mismatched_identity_request_id_fails_closed(
    tmp_path: Path,
) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[], hashes={})
    card["terminal_review"]["evidence"]["request_identity"]["request_id"] = _REQ_NEW
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == f"predecessor_request_id_stale:{_REQ_OLD}"


def test_reject_review_predecessor_missing_workspace_repo_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[])
    del card["terminal_review"]["evidence"]["workspace"]["repo"]
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_repo_mismatch"


def test_reject_review_predecessor_missing_workspace_request_id_fails_closed(
    tmp_path: Path,
) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[])
    del card["terminal_review"]["evidence"]["workspace"]["request_id"]
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == f"predecessor_request_id {_REQ_OLD} not found in retained review evidence"


@pytest.mark.parametrize("container", ["request_identity", "workspace"])
@pytest.mark.parametrize(
    "bad_task_id",
    ["OTHER_TASK", "", True, 5, None, 3.14, ["TASK_B891"]],
)
def test_reject_review_predecessor_invalid_optional_task_id_fails_closed(
    tmp_path: Path, container: str, bad_task_id: object
) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[])
    card["terminal_review"]["evidence"][container]["task_id"] = bad_task_id
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == (
        f"predecessor_request_id_task_mismatch:expected=TASK_B891:got={bad_task_id!s}"
    )


def test_reject_review_predecessor_bool_claim_epoch_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[])
    card["terminal_review"]["claim_epoch"] = True
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert resolved is None
    assert error == "predecessor_request_id_claim_epoch_mismatch:expected=3:got=True"


def test_reject_review_predecessor_mismatched_file_fails_closed(tmp_path: Path) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=["out.txt"])
    workspace = Path(str(card["terminal_review"]["evidence"]["workspace"]["path"]))
    (workspace / "out.txt").write_text("not-the-declared-digest", encoding="utf-8")
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert error is None
    assert resolved is not None
    assert resolved["changed_path_hashes"] == {"out.txt": _VALID_HASH}


def _seed_validation_failed_review(
    tmp_path: Path,
    *,
    changed_paths: list[str],
    request_id: str = _REQ_OLD,
    hashes: dict[str, str] | None = None,
) -> Path:
    repo = _repo_with_task(tmp_path, status="review")
    _readiness, db_path = task_store._require_ready(repo)
    card = _validation_failed_card(
        tmp_path,
        request_id=request_id,
        changed_paths=changed_paths,
        hashes=hashes,
        repo_path=repo,
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status='review', worker_status='review', card_json=? "
            "WHERE task_id=?",
            (json.dumps(card), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def _patch_reject_review(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo.resolve()))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo.resolve()))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "codex")
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", repo.resolve())


def test_reject_review_exact_predecessor_completes_validation_failed_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _seed_validation_failed_review(tmp_path, changed_paths=[])
    _patch_reject_review(monkeypatch, repo)
    result = core.reject_review(
        "TASK_B891",
        "rework validation",
        predecessor_request_id=_REQ_OLD,
    )
    assert result["ok"] is True
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "pending"
    assert card["review_feedback"]["predecessor_request_id"] == _REQ_OLD


def test_reject_review_omitted_duplicate_task_id_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_task(tmp_path, status="review")
    _readiness, db_path = task_store._require_ready(repo)
    card = _validation_failed_card(tmp_path, changed_paths=[], repo_path=repo)
    del card["terminal_review"]["evidence"]["request_identity"]["task_id"]
    del card["terminal_review"]["evidence"]["workspace"]["task_id"]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status='review', worker_status='review', card_json=? "
            "WHERE task_id=?",
            (json.dumps(card), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    _patch_reject_review(monkeypatch, repo)
    result = core.reject_review(
        "TASK_B891", "rework validation", predecessor_request_id=_REQ_OLD
    )
    assert result["ok"] is True
    card2 = task_store.get_task(repo, "TASK_B891")
    assert card2 is not None
    assert card2["status"] == "pending"
    assert card2["review_feedback"]["predecessor_request_id"] == _REQ_OLD


def test_reject_review_exact_predecessor_completes_validation_failed_nonempty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _seed_validation_failed_review(tmp_path, changed_paths=["out.txt"])
    _patch_reject_review(monkeypatch, repo)
    result = core.reject_review(
        "TASK_B891",
        "rework validation",
        predecessor_request_id=_REQ_OLD,
    )
    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_missing_delta"
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"
    assert "rework_predecessor" not in card


def test_reject_review_exact_predecessor_missing_delta_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    payload = b"retained-candidate"
    digest = hashlib.sha256(payload).hexdigest()
    repo = _seed_validation_failed_review(
        tmp_path,
        changed_paths=["out.txt"],
        hashes={"out.txt": digest},
    )
    (tmp_path / f"ws-{_REQ_OLD}" / "out.txt").write_bytes(payload)
    _patch_reject_review(monkeypatch, repo)
    result = core.reject_review(
        "TASK_B891",
        "rework validation",
        predecessor_request_id=_REQ_OLD,
    )
    assert result["ok"] is False
    assert result["stderr"] == "predecessor_request_id_missing_delta"
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"
    assert "rework_predecessor" not in card


def test_reject_review_exact_predecessor_does_not_select_newer_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _seed_validation_failed_review(
        tmp_path, changed_paths=["out.txt"], request_id=_REQ_NEW
    )
    _patch_reject_review(monkeypatch, repo)
    result = core.reject_review(
        "TASK_B891",
        "rework validation",
        predecessor_request_id=_REQ_OLD,
    )
    assert result["ok"] is False
    assert result["stderr"] == f"predecessor_request_id_stale:{_REQ_OLD}"
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"
