"""NF-2026-01747: a card whose latest run died operationally before changing
any file reroutes in place even though an OLDER reject-to-pending disposition
is still pinned on it.  That rejection adjudicated an earlier request; the
zero-change death that followed it produced no candidate a review-rejection
receipt could describe.

Fixture helpers are loaded by-path from the sibling nf01160 test module so the
card insertion, clean-root recovery and coordinator gate patches stay single-
sourced.
"""

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiworkhub import core, task_store

_SIBLING = Path(__file__).with_name(
    "test_zero_change_launch_failure_recovery_nf01160.py"
)
_SPEC = importlib.util.spec_from_file_location("nf01160_fixtures", _SIBLING)
assert _SPEC is not None and _SPEC.loader is not None
_NF01160 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_NF01160)

TASK_ID = _NF01160.TASK_ID
TOPIC = _NF01160.TOPIC
RUNNER = _NF01160.RUNNER
ALT_RUNNER = _NF01160.ALT_RUNNER
ALT_ADAPTER = _NF01160.ALT_ADAPTER
ALT_MODEL = _NF01160.ALT_MODEL
coordinator = _NF01160.coordinator
REASON = "nf01747 zero-change operational death: reroute in place"
REJECTED_REQUEST_ID = "0123456789abcdef0123456789abcdef"
TERMINAL_REQUEST_ID = "6929f727aaaabbbbccccddddeeeeffff"
PINNED_AT = "2026-10-20T00:00:00+00:00"
RETRIED_AT = "2026-10-21T00:00:00+00:00"


def _disposition(**overrides):
    disposition = {
        "schema_id": "aiworkhub.rejection_disposition.v1",
        "to": "pending",
        "failure_category_source": "manager_explicit",
        "failure_category": "candidate_code",
        "request_id": REJECTED_REQUEST_ID,
        "pinned_at": PINNED_AT,
    }
    disposition.update(overrides)
    return disposition


def _terminal_retry(**overrides):
    retry = {
        "schema_id": "aiworkhub.terminal_retry.v1",
        "task_id": TASK_ID,
        "request_id": TERMINAL_REQUEST_ID,
        "runner": RUNNER,
        "claim_epoch": 2,
        "terminal_substatus": "launch_failed",
        "reason": "runner_mismatch:card_scoped_identity_mismatch",
        "retried_at": RETRIED_AT,
    }
    retry.update(overrides)
    return retry


def _pending_card(**overrides):
    card = {
        "task_id": TASK_ID,
        "topic": TOPIC,
        "status": "pending",
        "worker_status": "unclaimed",
        "runner": RUNNER,
        "claim_epoch": 2,
        "updated_at": "2026-10-21T00:00:00+00:00",
        "rejection_disposition": _disposition(),
        "terminal_retry": _terminal_retry(),
    }
    card.update(overrides)
    return card


def _reroute():
    return core.reroute_launch_identity(
        TASK_ID,
        from_runner=RUNNER,
        to_runner=ALT_RUNNER,
        to_adapter_id=ALT_ADAPTER,
        to_model=ALT_MODEL,
        reason=REASON,
        topic=TOPIC,
    )


def _refused_for_missing_rejection_receipt(monkeypatch, card):
    monkeypatch.setattr(core, "_live_card", lambda _task_id: (card, None))
    monkeypatch.setattr(task_store, "is_pristine_pending_card", lambda _card: False)
    result = _reroute()
    return result, "reroute_manager_rejection_provenance_missing" in json.dumps(
        result
    )


def test_nf01747_zero_change_operational_death_reroutes_in_place(
    tmp_path, coordinator
):
    repo = _NF01160._init_git_repo(tmp_path / "repo")
    task_store.initialize_repository(repo)
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
        "launch_request_id": _NF01160.REQUEST_ID,
        "terminal_review": {
            "substatus": "validation_failed",
            "request_id": _NF01160.REQUEST_ID,
        },
        "deterministic_verification": {"pass": False, "claim_epoch": 1},
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at) VALUES (?, ?, ?, 'review', 'review', '', '', ?, ?, ?, ?, ?, ?)",
            (TASK_ID, RUNNER, TOPIC, json.dumps(card), now, now, RUNNER, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    coordinator(repo)

    # The OLDER episode: a manager reject-to-pending of the first request.
    rejected = core.reject_review(TASK_ID, reason=REASON, topic=TOPIC, to="pending")
    assert rejected.get("ok"), rejected
    rejected_card = _NF01160._card(repo)
    disposition = rejected_card["rejection_disposition"]
    assert disposition["to"] == "pending"
    assert disposition["failure_category_source"] != "manager_explicit_infrastructure"
    assert not core._has_retained_candidate_delta(rejected_card)
    # Without the fix this older rejection still classifies the card.
    assert core._operational_zero_change_after_rejection(rejected_card) is None
    assert core._is_review_rejection_reroute(rejected_card) is True

    # The LATER episode: the next run died operationally before changing a
    # file and the manager retried that exact terminal (retry_terminal shape).
    retried_at = (
        datetime.fromisoformat(disposition["pinned_at"]) + timedelta(minutes=5)
    ).astimezone(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (TASK_ID,)
        ).fetchone()
        later = json.loads(row[0])
        later["terminal_retry"] = _terminal_retry(
            claim_epoch=later.get("claim_epoch"), retried_at=retried_at
        )
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(later), TASK_ID),
        )
        conn.commit()
    finally:
        conn.close()

    live = _NF01160._card(repo)
    assert core._is_review_rejection_reroute(live) is False

    result = _reroute()
    assert result["ok"] is True, result
    card = _NF01160._card(repo)
    assert card["task_id"] == TASK_ID
    assert card["runner"] == ALT_RUNNER
    authorization = card["identity_reroute"]["operational_provider_authorization"]
    assert authorization["reroute_basis"] == "operational_zero_change"
    assert authorization["terminal_request_id"] == TERMINAL_REQUEST_ID
    assert authorization["terminal_substatus"] == "launch_failed"
    assert authorization["superseded_rejection_request_id"] == str(
        disposition.get("request_id") or ""
    )
    assert card["rejection_disposition"] == disposition


def test_nf01747_predicate_names_the_terminal_identity():
    card = _pending_card()
    assert core._operational_zero_change_after_rejection(card) == {
        "request_id": TERMINAL_REQUEST_ID,
        "terminal_substatus": "launch_failed",
    }
    assert core._is_review_rejection_reroute(card) is False
    receipt = core._operational_provider_reroute_authorization(
        card,
        from_runner=RUNNER,
        to_runner=ALT_RUNNER,
        to_adapter_id=ALT_ADAPTER,
        to_model=ALT_MODEL,
        reason=REASON,
    )
    assert receipt is not None
    assert receipt["authority"] == "provider_runtime_or_cancellation"
    assert receipt["reroute_basis"] == "operational_zero_change"
    assert receipt["terminal_request_id"] == TERMINAL_REQUEST_ID
    assert receipt["terminal_substatus"] == "launch_failed"


def test_nf01747_retained_candidate_delta_still_requires_rejection_receipt(
    monkeypatch,
):
    card = _pending_card(
        rework_predecessor={
            "request_id": TERMINAL_REQUEST_ID,
            "changed_path_hashes": {"src/aiworkhub/core.py": "ab" * 32},
        }
    )
    assert core._operational_zero_change_after_rejection(card) is None
    assert core._is_review_rejection_reroute(card) is True
    result, refused = _refused_for_missing_rejection_receipt(monkeypatch, card)
    assert result["ok"] is False
    assert refused is True


def test_nf01747_semantic_latest_terminal_is_not_operational_zero_change(
    monkeypatch,
):
    cards = (
        # A validation_failed terminal is never an operational retry receipt.
        _pending_card(terminal_retry=_terminal_retry(terminal_substatus="validation_failed")),
        # The latest recorded terminal is semantic even beside a stale retry.
        _pending_card(
            terminal_review={
                "substatus": "validation_failed",
                "request_id": TERMINAL_REQUEST_ID,
            }
        ),
        _pending_card(terminal_substatus="validation_failed"),
        # The classifier recorded a finding about the work itself.
        _pending_card(
            terminal_failure={
                "evidence": {
                    "failure_class": (
                        core.terminal_failure_classification.FAILURE_CLASS_DEFECT
                    ),
                    "request_id": TERMINAL_REQUEST_ID,
                }
            }
        ),
    )
    for card in cards:
        assert core._operational_zero_change_after_rejection(card) is None
        assert core._is_review_rejection_reroute(card) is True
        result, refused = _refused_for_missing_rejection_receipt(monkeypatch, card)
        assert result["ok"] is False
        assert refused is True


def test_nf01747_retry_not_newer_than_rejection_keeps_review_path():
    cards = (
        # The retry names the very request the rejection adjudicated.
        _pending_card(terminal_retry=_terminal_retry(request_id=REJECTED_REQUEST_ID)),
        # The retry predates the rejection.
        _pending_card(terminal_retry=_terminal_retry(retried_at="2026-10-19T00:00:00+00:00")),
        # Episode order cannot be proven.
        _pending_card(terminal_retry=_terminal_retry(retried_at="")),
        _pending_card(rejection_disposition=_disposition(pinned_at=None)),
        _pending_card(terminal_retry=_terminal_retry(retried_at="2026-10-21T00:00:00")),
        # The retry belongs to an earlier claim than the card's current one.
        _pending_card(claim_epoch=3),
        _pending_card(terminal_retry=_terminal_retry(claim_epoch=None)),
    )
    for card in cards:
        assert core._operational_zero_change_after_rejection(card) is None
        assert core._is_review_rejection_reroute(card) is True
