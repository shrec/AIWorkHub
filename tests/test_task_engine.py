from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_engine, task_store  # noqa: E402


def _acceptance_kwargs(repo: Path) -> dict[str, Any]:
    """Seal a minimal real candidate and return manager-shaped acceptance inputs."""
    output = repo / "out.txt"
    output.write_bytes(b"accepted\n")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = {"schema_id": "aiworkhub.attempt_artifact_manifest.v1", "entries": []}
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        card = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", ("TASK_B891",)
            ).fetchone()[0]
        )
        card["claim_epoch"] = 2
        sealed = card["terminal_review"]["evidence"]
        sealed.update(
            {
                "changed_paths": ["out.txt"],
                "changed_path_hashes": {"out.txt": digest},
                "attempt_artifact_manifest": manifest,
                "workspace": {"base_oid": "base-oid"},
            }
        )
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card, sort_keys=True), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    unsigned = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": "TASK_B891",
        "request_id": "req-accept-b1",
        "claim_epoch": 2,
        "base_oid": "base-oid",
        "promoted_paths": ["out.txt"],
        "changed_path_hashes": {"out.txt": digest},
        "attempt_artifact_manifest_id": task_engine._canonical_json_hash(manifest),
        "repository_revision": "sha256:"
        + task_engine._canonical_json_hash(
            {"base_oid": "base-oid", "changed_path_hashes": {"out.txt": digest}}
        ),
    }
    receipt = dict(unsigned)
    receipt["receipt_id"] = "sha256:" + task_engine._canonical_json_hash(unsigned)
    return {
        "evidence": {"promoted_paths": ["out.txt"], "validation": [{"ok": True}]},
        "accepted_outcome_receipt": receipt,
    }


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
    acceptance = _acceptance_kwargs(repo)
    result = task_engine.accept_review(
        repo,
        "TASK_B891",
        runner="codex_worker_b891",
        topic="task_mcp",
        request_id="req-accept-b1",
        **acceptance,
    )
    assert result["ok"] is True

    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "finished"
    assert card["worker_status"] == "done"
    assert card["accepted_request_id"] == "req-accept-b1"
    receipt = acceptance["accepted_outcome_receipt"]
    assert card["accept_evidence"]["promoted_paths"] == ["out.txt"]
    assert card["accept_evidence"]["accepted_outcome_receipt"] == receipt
    events = task_store.get_task_events(repo, "TASK_B891")
    assert events[0]["event"] == "accept_review"
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        event_payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM task_events WHERE task_id=? AND event=?",
                ("TASK_B891", "accept_review"),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert event_payload["accepted_outcome_receipt"] == receipt


def test_accept_review_cas_preserves_rival_terminal_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    acceptance = _acceptance_kwargs(repo)
    _readiness, db_path = task_store._require_ready(repo)
    real_connect = task_store._connect

    class InterleavingCursor:
        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self.cursor = cursor

        def fetchone(self) -> sqlite3.Row | None:
            row = self.cursor.fetchone()
            rival = real_connect(db_path)
            try:
                rival.execute(
                    "UPDATE tasks SET status='rejected', worker_status='failed' "
                    "WHERE task_id=?",
                    ("TASK_B891",),
                )
                rival.commit()
            finally:
                rival.close()
            return row

    class InterleavingConnection:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn
            self.interleaved = False

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
            cursor = self.conn.execute(sql, parameters)
            if not self.interleaved and sql.startswith("SELECT runner, topic, status"):
                self.interleaved = True
                return InterleavingCursor(cursor)
            return cursor

        def __getattr__(self, name: str) -> object:
            return getattr(self.conn, name)

    def interleaving_connect(
        path: Path, *args: object, **kwargs: object
    ) -> sqlite3.Connection | InterleavingConnection:
        conn = real_connect(path, *args, **kwargs)
        if kwargs.get("readonly") is True:
            return conn
        return InterleavingConnection(conn)

    monkeypatch.setattr(task_store, "_connect", interleaving_connect)

    result = task_engine.accept_review(
        repo,
        "TASK_B891",
        runner="codex_worker_b891",
        topic="task_mcp",
        request_id="req-accept-b1",
        **acceptance,
    )

    assert result["ok"] is False
    assert result["stderr"] == "accept_review_preimage_changed"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, worker_status FROM tasks WHERE task_id=?",
            ("TASK_B891",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row == ("rejected", "failed")
    assert not task_store.get_task_events(repo, "TASK_B891")


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
    acceptance = _acceptance_kwargs(repo)
    first = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1", **acceptance,
    )
    assert first["ok"] is True
    retry = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert retry["ok"] is True
    retry_payload = json.loads(retry["stdout"])
    assert retry_payload["already_accepted"] is True
    assert (
        retry_payload["accepted_outcome_receipt"]
        == acceptance["accepted_outcome_receipt"]
    )


def test_accept_review_rejects_different_request_after_finish(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    acceptance = _acceptance_kwargs(repo)
    first = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1", **acceptance,
    )
    assert first["ok"] is True
    other = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-other",
    )
    assert other["ok"] is False
    assert "already_finished" in other["stderr"]


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("task_id", "TASK_FORGED"),
        ("request_id", "request-forged"),
        ("claim_epoch", 99),
        ("promoted_paths", []),
        ("changed_path_hashes", {"out.txt": "0" * 64}),
        ("repository_revision", "sha256:" + "0" * 64),
    ],
)
def test_accept_review_rejects_forged_receipt_identity(
    tmp_path: Path, field: str, forged: object
) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    acceptance = _acceptance_kwargs(repo)
    tampered = deepcopy(acceptance)
    tampered["accepted_outcome_receipt"][field] = forged

    result = task_engine.accept_review(
        repo,
        "TASK_B891",
        runner="codex_worker_b891",
        topic="task_mcp",
        request_id="req-accept-b1",
        **tampered,
    )

    assert result["ok"] is False
    assert "accepted_outcome_receipt" in result["stderr"]
    assert task_store.get_task(repo, "TASK_B891")["status"] == "review"


def test_accept_review_supports_authenticated_readonly_empty_candidate(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    acceptance = _acceptance_kwargs(repo)
    (repo / "out.txt").unlink()
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        card = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", ("TASK_B891",)
            ).fetchone()[0]
        )
        sealed = card["terminal_review"]["evidence"]
        sealed["changed_paths"] = []
        sealed["changed_path_hashes"] = {}
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card, sort_keys=True), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    receipt = acceptance["accepted_outcome_receipt"]
    receipt["promoted_paths"] = []
    receipt["changed_path_hashes"] = {}
    receipt["repository_revision"] = "sha256:" + task_engine._canonical_json_hash(
        {"base_oid": "base-oid", "changed_path_hashes": {}}
    )
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    receipt["receipt_id"] = "sha256:" + task_engine._canonical_json_hash(unsigned)
    acceptance["evidence"]["promoted_paths"] = []

    result = task_engine.accept_review(
        repo,
        "TASK_B891",
        runner="codex_worker_b891",
        topic="task_mcp",
        request_id="req-accept-b1",
        **acceptance,
    )

    assert result["ok"] is True
    card = task_store.get_task(repo, "TASK_B891")
    assert card["accept_evidence"]["accepted_outcome_receipt"]["promoted_paths"] == []


def test_accept_review_rejects_caller_receipt_override_in_evidence(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    acceptance = _acceptance_kwargs(repo)
    acceptance["evidence"]["accepted_outcome_receipt"] = {"forged": True}

    result = task_engine.accept_review(
        repo,
        "TASK_B891",
        runner="codex_worker_b891",
        topic="task_mcp",
        request_id="req-accept-b1",
        **acceptance,
    )

    assert result["ok"] is False
    assert result["stderr"] == "accept_evidence_malformed"
    assert task_store.get_task(repo, "TASK_B891")["status"] == "review"


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
    substatus: str = "validation_failed",
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
            "substatus": substatus,
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


@pytest.mark.parametrize("substatus", ["validation_failed", "review_ready"])
def test_reject_review_predecessor_zero_diff_empty_maps(
    tmp_path: Path, substatus: str
) -> None:
    card = _validation_failed_card(
        tmp_path, changed_paths=[], hashes={}, substatus=substatus
    )
    resolved, error = _resolve(card, _REQ_OLD, tmp_path)
    assert error is None
    assert resolved is not None
    assert resolved["request_id"] == _REQ_OLD
    assert resolved["changed_path_hashes"] == {}
    assert card["terminal_review"]["substatus"] == substatus


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
    "hashes",
    [[], "deadbeef", {"out.txt": "not-a-digest"}, {"out.txt": _VALID_HASH}],
)
def test_reject_review_predecessor_hash_map_mutations_fail_closed(
    tmp_path: Path, hashes: object
) -> None:
    card = _validation_failed_card(tmp_path, changed_paths=[], hashes={})
    evidence = card["terminal_review"]["evidence"]
    evidence["changed_path_hashes"] = hashes
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
    repo = _seed_validation_failed_review(
        tmp_path, changed_paths=[], hashes={}
    )
    _patch_reject_review(monkeypatch, repo)
    result = core.reject_review(
        "TASK_B891",
        "rework validation",
        predecessor_request_id=_REQ_OLD,
    )
    assert result["ok"] is True
    assert "missing_hashes" not in str(result.get("stderr") or "")
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "pending"
    assert card["review_feedback"]["predecessor_request_id"] == _REQ_OLD
    assert card["review_feedback"]["predecessor_changed_paths"] == []


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


def _patch_create_task(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo.resolve()))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo.resolve()))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda *args, **kwargs: (True, "")
    )
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": "01234567-89ab-4def-8123-456789abcdef",
            "route_state": "verified",
        },
    )
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", repo.resolve())


def _generic_python_card() -> dict[str, object]:
    from aiworkhub.task_templates import expand_template

    expanded = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    return {
        "title": "Persist template identity",
        "runner": "codex_worker_nf390",
        "topic": "task_mcp",
        "objective": expanded["objective"],
        "acceptance": ["persist exact template provenance"],
        "allowed_writes": expanded["allowed_writes"],
        "required_outputs": expanded["required_outputs"],
        "validation": expanded["validation"],
        "validation_roles": expanded["validation_roles"],
        "read_first": expanded["read_first"],
        "work_kind": "generic",
        "read_only": False,
        "expanded": expanded,
    }


def _create_generic_python_task(task_id: str, card: dict[str, object], **extra):
    return core.create_task(
        task_id=task_id,
        title=str(card["title"]),
        runner=str(card["runner"]),
        topic=str(card["topic"]),
        objective=str(card["objective"]),
        acceptance=list(card["acceptance"]),  # type: ignore[arg-type]
        allowed_writes=list(card["allowed_writes"]),  # type: ignore[arg-type]
        required_outputs=list(card["required_outputs"]),  # type: ignore[arg-type]
        validation=list(card["validation"]),  # type: ignore[arg-type]
        validation_roles=list(card["validation_roles"]),  # type: ignore[arg-type]
        read_first=list(card["read_first"]),  # type: ignore[arg-type]
        work_kind=str(card["work_kind"]),
        callback_required=False,
        **extra,
    )


def test_create_task_persists_template_provenance_before_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _patch_create_task(monkeypatch, repo)
    card = _generic_python_card()
    result = _create_generic_python_task("TASK_NF390_PERSIST", card)
    assert result["ok"] is True
    assert result["created"] is True
    created = json.loads(result["stdout"])
    provenance = created["template_provenance"]
    assert provenance["template_name"] == "implementation_with_tests"
    assert provenance["template_full_id"] == card["expanded"]["template_full_id"]
    assert provenance["registry_version"] == card["expanded"]["registry_version"]
    assert provenance["definition_digest"] == card["expanded"]["definition_digest"]
    assert (
        provenance["classification_reason"]
        == "compatible_generic_python_production_plus_test"
    )
    from aiworkhub.task_templates import expanded_contract_digest

    assert provenance["expanded_contract_digest"] == expanded_contract_digest(
        {
            "allowed_writes": card["allowed_writes"],
            "required_outputs": card["required_outputs"],
            "validation": card["validation"],
            "validation_roles": card["validation_roles"],
            "work_kind": card["work_kind"],
            "read_only": False,
            "read_first": card["read_first"],
        }
    )
    stored = task_store.get_task(repo, "TASK_NF390_PERSIST")
    assert stored is not None
    assert stored["template_provenance"] == provenance


def test_lost_ack_reconciles_identical_template_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub.task_templates import template_provenance_payload

    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _patch_create_task(monkeypatch, repo)
    card = _generic_python_card()
    provenance = template_provenance_payload(
        card["expanded"],  # type: ignore[arg-type]
        classification_reason="explicit_template",
    )
    first = _create_generic_python_task(
        "TASK_NF390_ACK", card, template_provenance=provenance
    )
    assert first["created"] is True
    retry = _create_generic_python_task(
        "TASK_NF390_ACK", card, template_provenance=provenance
    )
    assert retry["ok"] is True
    assert retry["created"] is False
    assert retry["reconciled"] is True
    assert retry["receipt_state"] == "existing_identical"
    from aiworkhub import task_templates

    # A genuinely different but well-formed custom provenance over the same
    # card fields. The previous fixture hashed the stale pre-``expanded_contract``
    # custom definition, so it was rejected as malformed before reconciliation
    # ever ran. This one carries the writable card's authoritative minimality
    # contract, so it authenticates against the exact fields real core will
    # create, yet its identity differs from the stored built-in receipt, so a
    # same-id retry must report a template_provenance conflict rather than
    # reconcile or fail validation.
    custom = task_templates._custom_escape_provenance(
        {
            "allowed_writes": card["allowed_writes"],
            "read_first": card["read_first"],
            "read_only": False,
            "required_outputs": card["required_outputs"],
            "validation": card["validation"],
            "validation_roles": card["validation_roles"],
            "work_kind": "generic",
            "minimality_contract": card["expanded"]["minimality_contract"],
        }
    )
    assert custom["template_name"] == "custom"
    assert (
        custom["expanded_contract_digest"] == provenance["expanded_contract_digest"]
    )
    conflict = _create_generic_python_task(
        "TASK_NF390_ACK", card, template_provenance=custom
    )
    assert conflict["ok"] is False
    assert conflict["reconciled"] is False
    assert "template_provenance" in conflict["conflict_fields"]
    assert conflict["stderr"] == "task_already_exists:TASK_NF390_ACK"


def test_create_task_persisted_provenance_survives_poisoned_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import task_templates

    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _patch_create_task(monkeypatch, repo)
    card = _generic_python_card()
    created = _create_generic_python_task("TASK_NF390_POISON", card)
    assert created["ok"] is True
    created.pop("stdout", None)
    stored = task_store.get_task(repo, "TASK_NF390_POISON")
    assert stored is not None
    original = dict(stored["template_provenance"])
    monkeypatch.setattr(
        task_templates, "_definition_digest", lambda spec: "ab" * 32
    )
    monkeypatch.setattr(
        task_templates,
        "classify_task_card",
        lambda **kwargs: {
            "schema_id": task_templates.PROVENANCE_SCHEMA_ID,
            "template_name": "poisoned",
            "template_full_id": "poisoned@v99:" + ("cd" * 32),
            "registry_version": 99,
            "definition_digest": "cd" * 32,
            "classification_reason": "poisoned_live_classifier",
            "expanded_contract_digest": "ef" * 32,
        },
    )
    shown = core.show_task("TASK_NF390_POISON")
    assert shown["ok"] is True
    reloaded = json.loads(shown["stdout"])
    assert reloaded["template_provenance"] == original
    assert reloaded["template_provenance"]["template_name"] == "implementation_with_tests"
    assert reloaded["template_provenance"]["classification_reason"] == (
        "compatible_generic_python_production_plus_test"
    )
    assert reloaded["template_provenance"]["definition_digest"] != "ab" * 32
    assert reloaded["template_provenance"]["expanded_contract_digest"] == (
        task_templates.expanded_contract_digest(
            {
                "allowed_writes": card["allowed_writes"],
                "required_outputs": card["required_outputs"],
                "validation": card["validation"],
                "validation_roles": card["validation_roles"],
                "work_kind": card["work_kind"],
                "read_only": False,
                "read_first": card["read_first"],
            }
        )
    )


def test_create_task_empty_optional_fields_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import task_templates

    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _patch_create_task(monkeypatch, repo)
    empty_first = _generic_python_card()
    empty_first["read_first"] = []
    missing_first = _create_generic_python_task("TASK_NF390_EMPTY_FIRST", empty_first)
    assert missing_first["ok"] is False
    assert missing_first["stderr"] == "template_unclassified"
    empty_roles = _generic_python_card()
    expected_roles = list(empty_roles["validation_roles"])
    empty_roles["validation_roles"] = []
    filled_roles = _create_generic_python_task("TASK_NF390_EMPTY_ROLES", empty_roles)
    assert filled_roles["ok"] is True
    stored = task_store.get_task(repo, "TASK_NF390_EMPTY_ROLES")
    assert stored is not None
    assert stored["validation_roles"] == expected_roles
    assert stored["read_first"] == empty_roles["read_first"]
    assert stored["template_provenance"]["expanded_contract_digest"] == (
        task_templates.expanded_contract_digest(
            {
                "allowed_writes": stored["allowed_writes"],
                "required_outputs": stored["required_outputs"],
                "validation": stored["validation"],
                "validation_roles": stored["validation_roles"],
                "work_kind": stored["work_kind"],
                "read_only": stored["read_only"],
                "read_first": stored["read_first"],
            }
        )
    )


def test_create_task_rejects_unbounded_validation_and_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub.task_templates import MAX_PATHS_PER_FIELD

    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _patch_create_task(monkeypatch, repo)
    too_many_validation = _generic_python_card()
    too_many_validation["validation"] = ["git diff --check"] * (MAX_PATHS_PER_FIELD + 1)
    invalid_validation = _create_generic_python_task(
        "TASK_NF390_TOO_MANY_VALIDATION", too_many_validation
    )
    assert invalid_validation["ok"] is False
    assert invalid_validation["stderr"] == "invalid_validation"
    too_many_roles = _generic_python_card()
    too_many_roles["validation_roles"] = ["generic"] * (MAX_PATHS_PER_FIELD + 1)
    invalid_roles = _create_generic_python_task(
        "TASK_NF390_TOO_MANY_ROLES", too_many_roles
    )
    assert invalid_roles["ok"] is False
    assert invalid_roles["stderr"] == "invalid_validation_roles"
