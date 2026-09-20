"""Tests for aiworkhub.attempt_trajectory_export."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import attempt_artifacts, attempt_trajectory_export as export_mod, task_engine, task_store

TASK_ID = "TASK_ATE_1"
REQUEST_ID = "req-ate-1"
RUNNER = "codex_worker_ate"
TOPIC = "task_mcp"
BASE_OID = "base-oid-ate"


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _receipt(*, task_id: str = TASK_ID, request_id: str = REQUEST_ID) -> dict:
    body = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 1,
        "base_oid": BASE_OID,
        "promoted_paths": ["src/foo.py"],
        "changed_path_hashes": {"src/foo.py": "a" * 64},
        "attempt_artifact_manifest_id": "b" * 64,
        "repository_revision": "sha256:" + "c" * 64,
    }
    body["receipt_id"] = "sha256:" + _digest(body)
    return body


def _accepted_card(**overrides) -> dict:
    card = {
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "finished",
        "accepted_request_id": REQUEST_ID,
        "accept_evidence": {"accepted_outcome_receipt": _receipt()},
    }
    card.update(overrides)
    return card


# --------------------------------------------------------------------------
# Pure builder: schema shape, accepted / rejected / failed / unknown outcomes
# --------------------------------------------------------------------------

_ALL_OUTCOME_TOP_KEYS = {
    "schema_id", "repository_id", "task_id", "request_id", "runner", "topic",
    "task_status", "outcome", "events", "events_bounds", "artifacts",
    "validations", "reviews", "manager_decision", "usage",
}


def test_unknown_outcome_with_no_evidence() -> None:
    result = export_mod.build_attempt_trajectory(task_id=TASK_ID, request_id=REQUEST_ID)
    assert set(result.keys()) == _ALL_OUTCOME_TOP_KEYS
    assert result["outcome"]["state"] == "unknown"
    assert result["outcome"]["accepted_outcome_receipt"] is None
    assert result["runner"] == export_mod.UNKNOWN
    assert result["artifacts"]["state"] == "absent"
    assert result["validations"]["state"] == export_mod.UNKNOWN
    assert result["reviews"] == []
    assert result["manager_decision"]["decision"] == export_mod.UNKNOWN
    assert result["usage"]["state"] == export_mod.UNKNOWN
    assert result["usage"]["total_tokens"] == export_mod.UNKNOWN
    assert result["usage"]["cost_usd"] == export_mod.UNKNOWN


def _accepting_authority(card, task_id, request_id, receipt):
    """Stand-in for a repository-bound task_engine._validate_accepted_outcome_receipt
    call: confirms the receipt is bound to canonical sealed evidence."""
    return dict(receipt), ""


def _refusing_authority(card, task_id, request_id, receipt):
    return None, "accepted_outcome_receipt_canonical_hash_mismatch"


def test_receipt_without_canonical_authority_never_grants_accepted() -> None:
    """A self-consistent (self-digested) receipt is trivial to forge -- a
    caller can recompute a valid digest over fabricated content. Absent a
    bound canonical authority to check it against sealed evidence, the
    outcome must stay non-accepted rather than trust the receipt's own
    self-digest."""
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, card=_accepted_card(),
    )
    assert result["outcome"]["state"] != "accepted"
    assert result["outcome"]["accepted_outcome_receipt"] is None


def test_accepted_outcome_granted_when_canonical_authority_confirms() -> None:
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        card=_accepted_card(),
        accepted_outcome_authority=_accepting_authority,
    )
    assert result["outcome"]["state"] == "accepted"
    assert result["outcome"]["accepted_outcome_receipt"]["receipt_id"] == _receipt()["receipt_id"]


def test_accepted_outcome_refused_when_canonical_authority_rejects() -> None:
    with pytest.raises(export_mod.IdentityMismatchError):
        export_mod.build_attempt_trajectory(
            task_id=TASK_ID,
            request_id=REQUEST_ID,
            card=_accepted_card(),
            accepted_outcome_authority=_refusing_authority,
        )


def test_accepted_outcome_receipt_tamper_detected() -> None:
    receipt = _receipt()
    receipt["base_oid"] = "tampered-oid"
    card = _accepted_card(accept_evidence={"accepted_outcome_receipt": receipt})
    with pytest.raises(export_mod.IdentityMismatchError):
        export_mod.build_attempt_trajectory(task_id=TASK_ID, request_id=REQUEST_ID, card=card)


def test_accepted_request_id_conflicts_with_receipt_identity() -> None:
    other_receipt = _receipt(request_id="some-other-request")
    card = _accepted_card(accept_evidence={"accepted_outcome_receipt": other_receipt})
    with pytest.raises(export_mod.IdentityMismatchError):
        export_mod.build_attempt_trajectory(task_id=TASK_ID, request_id=REQUEST_ID, card=card)


def test_receipt_bound_to_this_request_but_task_card_disagrees() -> None:
    card = _accepted_card(accepted_request_id="another-request")
    with pytest.raises(export_mod.IdentityMismatchError):
        export_mod.build_attempt_trajectory(task_id=TASK_ID, request_id=REQUEST_ID, card=card)


def test_receipt_schema_id_mismatch_rejected() -> None:
    receipt = _receipt()
    receipt["schema_id"] = "not.the.right.schema"
    receipt["receipt_id"] = "sha256:" + _digest(
        {k: v for k, v in receipt.items() if k != "receipt_id"}
    )
    card = _accepted_card(accept_evidence={"accepted_outcome_receipt": receipt})
    with pytest.raises(export_mod.IdentityMismatchError):
        export_mod.build_attempt_trajectory(task_id=TASK_ID, request_id=REQUEST_ID, card=card)


def _task_event(event: str, *, request_id: str = REQUEST_ID) -> dict:
    return {
        "event": event,
        "runner": RUNNER,
        "created_at": "2026-09-01T00:00:00+00:00",
        "payload": json.dumps({"request_id": request_id}),
    }


def test_rejected_outcome_from_reject_review_event() -> None:
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        task_events=[_task_event("reject_review")],
    )
    assert result["outcome"]["state"] == "rejected"
    assert result["outcome"]["reason"] == "reject_review_event"


def test_accept_review_event_alone_does_not_imply_accepted() -> None:
    """accept_review is a manager audit event, not terminal authority: it
    must never itself project outcome.state == "accepted" absent a receipt
    validated by the bound canonical authority."""
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        task_events=[_task_event("accept_review")],
    )
    assert result["outcome"]["state"] == "unknown"
    assert result["outcome"]["accepted_outcome_receipt"] is None


def test_accept_review_and_reject_review_without_receipt_is_rejected_not_contradictory() -> None:
    """Since accept_review carries no acceptance authority by itself, pairing
    it with reject_review is not a genuine contradiction -- reject_review is
    the only evidenced terminal signal here."""
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        task_events=[_task_event("accept_review"), _task_event("reject_review")],
    )
    assert result["outcome"]["state"] == "rejected"


def test_contradictory_authenticated_accept_and_reject_events_refuse() -> None:
    with pytest.raises(export_mod.ContradictoryTerminalDecisionError):
        export_mod.build_attempt_trajectory(
            task_id=TASK_ID,
            request_id=REQUEST_ID,
            card=_accepted_card(),
            task_events=[_task_event("reject_review")],
            accepted_outcome_authority=_accepting_authority,
        )


def test_contradictory_authenticated_accept_and_ledger_failure_refuse() -> None:
    failure_event = {
        "request_id": REQUEST_ID,
        "state": "validation_failed",
        "timestamp": "2026-09-01T00:00:00+00:00",
        "terminal_reason": {"code": "validation_failed", "message": "boom"},
    }
    with pytest.raises(export_mod.ContradictoryTerminalDecisionError):
        export_mod.build_attempt_trajectory(
            task_id=TASK_ID,
            request_id=REQUEST_ID,
            card=_accepted_card(),
            ledger_events=[failure_event],
            accepted_outcome_authority=_accepting_authority,
        )


def test_failed_outcome_from_ledger_terminal_reason() -> None:
    failure_event = {
        "request_id": REQUEST_ID,
        "state": "worker_failed",
        "timestamp": "2026-09-01T00:00:00+00:00",
        "terminal_reason": {"code": "worker_failed", "message": "boom"},
    }
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, ledger_events=[failure_event],
    )
    assert result["outcome"]["state"] == "failed"
    assert result["events"][0]["source"] == "process_ledger"


def test_ledger_events_scoped_to_request_id_only() -> None:
    mine = {"request_id": REQUEST_ID, "state": "running", "timestamp": "t1"}
    other = {"request_id": "other-request", "state": "running", "timestamp": "t2"}
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, ledger_events=[mine, other],
    )
    assert len(result["events"]) == 1
    assert result["events"][0]["request_id"] == REQUEST_ID


def test_duplicate_sequence_refuses() -> None:
    events = [
        {"request_id": REQUEST_ID, "seq": 1, "state": "running", "timestamp": "t1"},
        {"request_id": REQUEST_ID, "seq": 1, "state": "running", "timestamp": "t2"},
    ]
    with pytest.raises(export_mod.DuplicateSequenceError):
        export_mod.build_attempt_trajectory(
            task_id=TASK_ID, request_id=REQUEST_ID, ledger_events=events,
        )


def test_distinct_sequences_are_fine() -> None:
    events = [
        {"request_id": REQUEST_ID, "seq": 1, "state": "running", "timestamp": "t1"},
        {"request_id": REQUEST_ID, "seq": 2, "state": "running", "timestamp": "t2"},
    ]
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, ledger_events=events,
    )
    assert len(result["events"]) == 2


def test_artifact_bundle_identity_mismatch_refuses() -> None:
    artifact_bundle = {
        "verification": {"attempt_id": "not-this-request"},
        "payloads": {},
    }
    with pytest.raises(export_mod.IdentityMismatchError):
        export_mod.build_attempt_trajectory(
            task_id=TASK_ID,
            request_id=REQUEST_ID,
            artifact_bundle=artifact_bundle,
        )


def test_artifact_bundle_verified_populates_roles_and_validations() -> None:
    artifact_bundle = {
        "verification": {"attempt_id": REQUEST_ID, "verified": True},
        "payloads": {
            "validation": {"checks": [{"name": "pytest", "passed": True}], "passed": True},
            "review": {"target_state": "review_ready"},
        },
    }
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        artifact_bundle=artifact_bundle,
    )
    assert result["artifacts"]["state"] == "verified"
    assert result["validations"]["state"] == "recorded"
    assert result["validations"]["passed"] is True
    assert result["reviews"] == [
        {"source": "attempt_artifact_bundle", "payload": {"target_state": "review_ready"}}
    ]


# --------------------------------------------------------------------------
# Usage / cost: explicit UNKNOWN, never a silent zero
# --------------------------------------------------------------------------

def _usage_row(**overrides) -> dict:
    row = {
        "request_id": REQUEST_ID,
        "usage_observed": True,
        "cost_known": True,
        "total_tokens": 100,
        "cost_usd": 1.5,
    }
    row.update(overrides)
    return row


def test_usage_measured_when_fully_observed() -> None:
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, usage_rows=[_usage_row()],
    )
    assert result["usage"]["state"] == "measured"
    assert result["usage"]["total_tokens"] == 100
    assert result["usage"]["cost_usd"] == 1.5


def test_usage_unknown_tokens_when_not_observed() -> None:
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        usage_rows=[_usage_row(usage_observed=False)],
    )
    assert result["usage"]["state"] == "partially_unknown"
    assert result["usage"]["total_tokens"] == export_mod.UNKNOWN
    assert result["usage"]["matched_records"] == 1


def test_usage_unknown_cost_when_not_known() -> None:
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        usage_rows=[_usage_row(cost_known=False)],
    )
    assert result["usage"]["cost_usd"] == export_mod.UNKNOWN
    assert result["usage"]["total_tokens"] == 100


def test_usage_observed_true_but_total_tokens_absent_stays_unknown() -> None:
    """usage_observed=True is a claim of measurement, not proof of it. A row
    missing the actual total_tokens number must never be trusted as a
    measured zero -- the aggregate must downgrade to UNKNOWN."""
    row = _usage_row()
    del row["total_tokens"]
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, usage_rows=[row],
    )
    assert result["usage"]["total_tokens"] == export_mod.UNKNOWN
    assert result["usage"]["state"] == "partially_unknown"


def test_usage_cost_known_true_but_cost_usd_absent_stays_unknown() -> None:
    """cost_known=True is a claim of measurement, not proof of it. A row
    missing the actual cost_usd number must never be trusted as a measured
    zero -- the aggregate must downgrade to UNKNOWN."""
    row = _usage_row()
    del row["cost_usd"]
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, usage_rows=[row],
    )
    assert result["usage"]["cost_usd"] == export_mod.UNKNOWN
    assert result["usage"]["state"] == "partially_unknown"


def test_usage_rows_scoped_to_request_id() -> None:
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        usage_rows=[_usage_row(), _usage_row(request_id="other")],
    )
    assert result["usage"]["matched_records"] == 1


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

def test_redacts_known_secret_field_in_task_event_payload() -> None:
    event = {
        "event": "accept_review",
        "runner": RUNNER,
        "created_at": "2026-09-01T00:00:00+00:00",
        "payload": json.dumps({"request_id": REQUEST_ID, "api_key": "shh-secret"}),
    }
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, task_events=[event],
    )
    canonical = export_mod.to_canonical_json(result)
    assert "shh-secret" not in canonical
    payload = result["events"][0]["payload"]
    assert payload["api_key"]["redacted"] is True
    assert "sha256" in payload["api_key"]


def test_redaction_is_deterministic_by_field_identity() -> None:
    assert export_mod.redact({"token": "x"}) == export_mod.redact({"token": "x"})
    assert export_mod.redact({"token": "x"}) != export_mod.redact({"token": "y"})


# --------------------------------------------------------------------------
# Bounded output
# --------------------------------------------------------------------------

def test_bounded_ledger_events_report_omitted_count_and_digest() -> None:
    events = [
        {"request_id": REQUEST_ID, "state": "running", "timestamp": f"t{i}"}
        for i in range(export_mod._MAX_LEDGER_EVENTS + 5)
    ]
    result = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, ledger_events=events,
    )
    bounds = result["events_bounds"]
    assert bounds["ledger_events_omitted_count"] == 5
    assert bounds["ledger_events_omitted_sha256"] is not None
    assert len([e for e in result["events"] if e["source"] == "process_ledger"]) == (
        export_mod._MAX_LEDGER_EVENTS
    )


def test_no_omission_reports_none_digest() -> None:
    result = export_mod.build_attempt_trajectory(task_id=TASK_ID, request_id=REQUEST_ID)
    bounds = result["events_bounds"]
    assert bounds["ledger_events_omitted_count"] == 0
    assert bounds["ledger_events_omitted_sha256"] is None


# --------------------------------------------------------------------------
# Deterministic serialization
# --------------------------------------------------------------------------

def test_canonical_json_is_order_independent_and_deterministic() -> None:
    result1 = export_mod.build_attempt_trajectory(
        task_id=TASK_ID, request_id=REQUEST_ID, card=_accepted_card(),
    )
    result2 = export_mod.build_attempt_trajectory(
        card=_accepted_card(), request_id=REQUEST_ID, task_id=TASK_ID,
    )
    assert export_mod.to_canonical_json(result1) == export_mod.to_canonical_json(result2)


def test_canonical_json_has_no_indentation_whitespace() -> None:
    result = export_mod.build_attempt_trajectory(task_id=TASK_ID, request_id=REQUEST_ID)
    canonical = export_mod.to_canonical_json(result)
    assert "\n" not in canonical
    assert ": " not in canonical


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------

def test_rejects_empty_task_id() -> None:
    with pytest.raises(export_mod.AttemptTrajectoryExportError):
        export_mod.build_attempt_trajectory(task_id="", request_id=REQUEST_ID)


def test_rejects_empty_request_id() -> None:
    with pytest.raises(export_mod.AttemptTrajectoryExportError):
        export_mod.build_attempt_trajectory(task_id=TASK_ID, request_id="")


# --------------------------------------------------------------------------
# Orchestrator: real repository + real ledger + real artifact bundle
# --------------------------------------------------------------------------

def _seed_task(repo: Path, *, task_id: str, card: dict) -> None:
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-09-01T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, "
            "origin_thread_id) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, RUNNER, TOPIC, card.get("status", "review"),
                card.get("status", "review"), json.dumps(card), now, now, RUNNER, now, now,
                "thread-ate",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_task_event(repo: Path, *, task_id: str, event: str, payload: dict) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, event, RUNNER, json.dumps(payload), "2026-09-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_genuine_accepted_evidence(repo: Path, *, task_id: str, request_id: str) -> dict:
    """Build a card + receipt that the real, repository-bound
    ``task_engine._validate_accepted_outcome_receipt`` canonically accepts:
    a real promoted file on disk whose hash matches the card's sealed
    terminal_review evidence, which the receipt in turn binds to."""
    relative = "src/foo.py"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / relative).write_text("print('ate')\n", encoding="utf-8")
    changed_path_hashes = {relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest()}
    manifest = {"artifacts": ["metadata.json"]}
    claim_epoch = 1
    receipt = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "base_oid": BASE_OID,
        "promoted_paths": [relative],
        "changed_path_hashes": changed_path_hashes,
        "attempt_artifact_manifest_id": _digest(manifest),
        "repository_revision": "sha256:"
        + _digest({"base_oid": BASE_OID, "changed_path_hashes": changed_path_hashes}),
    }
    receipt["receipt_id"] = "sha256:" + _digest(receipt)
    return {
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "finished",
        "claim_epoch": claim_epoch,
        "accepted_request_id": request_id,
        "accept_evidence": {"accepted_outcome_receipt": receipt},
        "terminal_review": {
            "evidence": {
                "request_identity": {"request_id": request_id},
                "changed_paths": [relative],
                "changed_path_hashes": changed_path_hashes,
                "attempt_artifact_manifest": manifest,
                "workspace": {"base_oid": BASE_OID},
            }
        },
    }


def test_export_attempt_trajectory_accepted_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    card = _seed_genuine_accepted_evidence(repo, task_id=TASK_ID, request_id=REQUEST_ID)
    _seed_task(repo, task_id=TASK_ID, card=card)

    result = export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)

    receipt = card["accept_evidence"]["accepted_outcome_receipt"]
    assert result["outcome"]["state"] == "accepted"
    assert result["outcome"]["accepted_outcome_receipt"]["receipt_id"] == receipt["receipt_id"]
    assert result["task_status"] == "finished"
    assert result["repository_id"] == str(repo.resolve())


def test_export_attempt_trajectory_canonical_hash_mismatch_refuses(tmp_path: Path) -> None:
    """A receipt that is internally self-consistent and structurally bound to
    the right task/request but no longer matches the promoted file's current
    bytes must be refused by the real canonical authority, not accepted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    card = _seed_genuine_accepted_evidence(repo, task_id=TASK_ID, request_id=REQUEST_ID)
    _seed_task(repo, task_id=TASK_ID, card=card)
    (repo / "src" / "foo.py").write_text("print('tampered after promotion')\n", encoding="utf-8")

    with pytest.raises(export_mod.IdentityMismatchError):
        export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)


def test_export_attempt_trajectory_honors_opt_in_authority(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    card = _seed_genuine_accepted_evidence(repo, task_id=TASK_ID, request_id=REQUEST_ID)
    _seed_task(repo, task_id=TASK_ID, card=card)

    def _refusing(_card, _task_id, _request_id, _receipt):
        return None, "opt_in_authority_refused"

    with pytest.raises(export_mod.IdentityMismatchError, match="opt_in_authority_refused"):
        export_mod.export_attempt_trajectory(
            repo, task_id=TASK_ID, request_id=REQUEST_ID,
            accepted_outcome_authority=_refusing,
        )

    def _accepting(_card, _task_id, _request_id, receipt):
        return dict(receipt), ""

    (repo / "src" / "foo.py").write_text("print('later edit')\n", encoding="utf-8")
    result = export_mod.export_attempt_trajectory(
        repo, task_id=TASK_ID, request_id=REQUEST_ID,
        accepted_outcome_authority=_accepting,
    )
    assert result["outcome"]["state"] == "accepted"

def test_export_attempt_trajectory_accept_review_event_without_receipt_stays_unknown(
    tmp_path: Path,
) -> None:
    """A recorded accept_review manager event with no accepted_outcome_receipt
    on the task card must never surface as an accepted outcome end-to-end."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_task(repo, task_id=TASK_ID, card={"runner": RUNNER, "topic": TOPIC})
    _insert_task_event(
        repo, task_id=TASK_ID, event="accept_review", payload={"request_id": REQUEST_ID},
    )

    result = export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)

    assert result["outcome"]["state"] == "unknown"
    assert result["outcome"]["accepted_outcome_receipt"] is None


def test_export_attempt_trajectory_rejected_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_task(repo, task_id=TASK_ID, card={"runner": RUNNER, "topic": TOPIC})
    _insert_task_event(
        repo, task_id=TASK_ID, event="reject_review", payload={"request_id": REQUEST_ID},
    )

    result = export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)

    assert result["outcome"]["state"] == "rejected"


def test_export_attempt_trajectory_failed_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_task(repo, task_id=TASK_ID, card={"runner": RUNNER, "topic": TOPIC})
    ledger_path = tmp_path / "process_events.jsonl"
    from aiworkhub import process_event_ledger
    process_event_ledger.append_event(
        ledger_path, {"request_id": REQUEST_ID, "state": "worker_failed"}
    )

    result = export_mod.export_attempt_trajectory(
        repo, task_id=TASK_ID, request_id=REQUEST_ID, process_events_path=ledger_path,
    )

    assert result["outcome"]["state"] == "failed"


def test_export_attempt_trajectory_bounds_raw_ledger_read_before_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw process-event ledger read must itself be bounded (via
    itertools.islice) before the full ledger is scoped/materialized, the same
    way task_events and usage_rows reads are bounded at their source. Proof:
    with the raw-read bound forced tiny, a request's own event appended after
    that many unrelated rows is never observed at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_task(repo, task_id=TASK_ID, card={"runner": RUNNER, "topic": TOPIC})
    ledger_path = tmp_path / "process_events.jsonl"
    from aiworkhub import process_event_ledger

    monkeypatch.setattr(export_mod, "_MAX_RAW_LEDGER_EVENTS_READ", 2)
    for i in range(4):
        process_event_ledger.append_event(
            ledger_path, {"request_id": "other-request", "state": f"s{i}"}
        )
    process_event_ledger.append_event(
        ledger_path,
        {
            "request_id": REQUEST_ID,
            "state": "worker_failed",
            "terminal_reason": {"code": "worker_failed", "message": "boom"},
        },
    )

    result = export_mod.export_attempt_trajectory(
        repo, task_id=TASK_ID, request_id=REQUEST_ID, process_events_path=ledger_path,
    )

    assert result["outcome"]["state"] == "unknown"
    assert result["events"] == []


def test_export_attempt_trajectory_missing_task_is_unknown_not_a_crash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)

    result = export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)

    assert result["outcome"]["state"] == "unknown"
    assert result["artifacts"]["state"] == "absent"


def test_export_attempt_trajectory_artifact_digest_mismatch_refuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    bundle_dir = (
        repo / ".aiworkhub" / "runtime" / "process_logs" / "processes"
        / "attempt-artifacts" / REQUEST_ID
    )
    attempt_artifacts.persist_json_bundle(
        bundle_dir,
        attempt_id=REQUEST_ID,
        payloads={
            "metadata": {"request_id": REQUEST_ID},
            "diff": {"changed_paths": []},
            "validation": {"checks": [], "passed": True},
            "usage": {"usage_observed": False},
            "review": {"target_state": "review_ready"},
        },
    )
    (bundle_dir / "usage.json").write_text('{"usage_observed":true}\n', encoding="utf-8")

    with pytest.raises(export_mod.ArtifactDigestMismatchError):
        export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)


def test_export_attempt_trajectory_artifact_identity_mismatch_refuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    bundle_dir = (
        repo / ".aiworkhub" / "runtime" / "process_logs" / "processes"
        / "attempt-artifacts" / REQUEST_ID
    )
    attempt_artifacts.persist_json_bundle(
        bundle_dir,
        attempt_id="a-different-attempt-id",
        payloads={
            "metadata": {"request_id": REQUEST_ID},
            "diff": {"changed_paths": []},
            "validation": {"checks": [], "passed": True},
            "usage": {"usage_observed": False},
            "review": {"target_state": "review_ready"},
        },
    )

    with pytest.raises(export_mod.IdentityMismatchError):
        export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)


def test_export_attempt_trajectory_verified_artifact_bundle_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    bundle_dir = (
        repo / ".aiworkhub" / "runtime" / "process_logs" / "processes"
        / "attempt-artifacts" / REQUEST_ID
    )
    attempt_artifacts.persist_json_bundle(
        bundle_dir,
        attempt_id=REQUEST_ID,
        payloads={
            "metadata": {"request_id": REQUEST_ID},
            "diff": {"changed_paths": ["src/example.py"]},
            "validation": {"checks": [{"name": "pytest"}], "passed": True},
            "usage": {"usage_observed": False},
            "review": {"target_state": "review_ready"},
        },
    )

    result = export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)

    assert result["artifacts"]["state"] == "verified"
    assert result["artifacts"]["verification"]["attempt_id"] == REQUEST_ID
    assert result["validations"]["passed"] is True


# --------------------------------------------------------------------------
# Public contract: the exact symbols scripts/build_accepted_task_eval.py
# depends on must stay importable, or the accepted-task eval corpus builder
# silently breaks.
# --------------------------------------------------------------------------

def test_public_contract_symbols_used_by_accepted_task_eval_builder_are_stable() -> None:
    assert export_mod.__all__ == [
        "SCHEMA_ID",
        "UNKNOWN",
        "AttemptTrajectoryExportError",
        "IdentityMismatchError",
        "DuplicateSequenceError",
        "ArtifactDigestMismatchError",
        "ContradictoryTerminalDecisionError",
        "redact",
        "to_canonical_json",
        "build_attempt_trajectory",
        "export_attempt_trajectory",
    ]
    assert export_mod.UNKNOWN == "UNKNOWN"
    assert callable(export_mod.export_attempt_trajectory)


# --------------------------------------------------------------------------
# Batched manager_decisions / usage_rows: a caller exporting many
# trajectories from one store snapshot (the accepted-task eval corpus
# builder) must be able to fetch each whole-store query once and reuse it,
# rather than have every export call re-run its own whole-table scan --
# that per-call re-fetch is what turns an N-card rebuild into O(N) whole-
# store scans.
# --------------------------------------------------------------------------

def test_export_attempt_trajectory_reuses_prefetched_manager_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    card = _seed_genuine_accepted_evidence(repo, task_id=TASK_ID, request_id=REQUEST_ID)
    _seed_task(repo, task_id=TASK_ID, card=card)

    def _boom(*_args, **_kwargs):
        raise AssertionError("latest_manager_decisions must not be re-queried when prefetched")

    monkeypatch.setattr(task_store, "latest_manager_decisions", _boom)

    result = export_mod.export_attempt_trajectory(
        repo, task_id=TASK_ID, request_id=REQUEST_ID,
        manager_decisions={
            TASK_ID: {
                "decision": "accepted", "event": "accept_review",
                "created_at": "2026-09-01T00:00:00+00:00",
            },
        },
    )

    assert result["manager_decision"]["decision"] == "accepted"


def test_export_attempt_trajectory_reuses_prefetched_usage_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    card = _seed_genuine_accepted_evidence(repo, task_id=TASK_ID, request_id=REQUEST_ID)
    _seed_task(repo, task_id=TASK_ID, card=card)

    def _boom(*_args, **_kwargs):
        raise AssertionError("list_usage_events must not be re-queried when prefetched")

    monkeypatch.setattr(task_store, "list_usage_events", _boom)

    usage_row = {
        "request_id": REQUEST_ID, "usage_observed": True, "cost_known": True,
        "total_tokens": 42, "cost_usd": 0.5,
    }
    result = export_mod.export_attempt_trajectory(
        repo, task_id=TASK_ID, request_id=REQUEST_ID, usage_rows=[usage_row],
    )

    assert result["usage"]["state"] == "measured"
    assert result["usage"]["total_tokens"] == 42


def test_export_attempt_trajectory_still_fetches_when_not_prefetched(tmp_path: Path) -> None:
    """Omitting both arguments preserves the original single-call behavior."""
    repo = tmp_path / "repo"
    repo.mkdir()
    card = _seed_genuine_accepted_evidence(repo, task_id=TASK_ID, request_id=REQUEST_ID)
    _seed_task(repo, task_id=TASK_ID, card=card)

    result = export_mod.export_attempt_trajectory(repo, task_id=TASK_ID, request_id=REQUEST_ID)

    assert result["outcome"]["state"] == "accepted"
    assert result["manager_decision"]["decision"] == export_mod.UNKNOWN
    assert result["usage"]["state"] == export_mod.UNKNOWN
    assert issubclass(export_mod.AttemptTrajectoryExportError, ValueError)
