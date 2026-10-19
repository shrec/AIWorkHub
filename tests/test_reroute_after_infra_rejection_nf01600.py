"""NF-2026-01600: a manager reject-to-pending pinned to an infrastructure
failure category with zero changed paths reroutes in place through the
operational provider authorization instead of demanding the review-rejection
receipt that a zero-change candidate can never satisfy.

Fixture helpers are loaded by-path from the sibling nf01160 test module so the
card insertion, clean-root recovery and coordinator gate patches stay single-
sourced.
"""

import importlib.util
import json
from pathlib import Path

import pytest

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
coordinator = _NF01160.coordinator
ALT_MODEL = _NF01160.ALT_MODEL
REASON = "nf01600 infrastructure rejection: reroute in place"


def _disposition(**overrides):
    disposition = {
        "schema_id": "aiworkhub.rejection_disposition.v1",
        "to": "pending",
        "failure_category_source": "manager_explicit_infrastructure",
        "failure_category": "provider_runtime",
        "request_id": "0123456789abcdef0123456789abcdef",
    }
    disposition.update(overrides)
    return disposition


def _pending_card(rejection_disposition):
    return {
        "task_id": TASK_ID,
        "topic": TOPIC,
        "status": "pending",
        "worker_status": "unclaimed",
        "runner": RUNNER,
        "updated_at": "2026-10-19T00:00:00+00:00",
        "rejection_disposition": rejection_disposition,
    }


def _reroute(card, from_runner=RUNNER):
    return core.reroute_launch_identity(
        TASK_ID,
        from_runner=from_runner,
        to_runner=ALT_RUNNER,
        to_adapter_id=ALT_ADAPTER,
        to_model=ALT_MODEL,
        reason=REASON,
        topic=TOPIC,
    )


def test_nf01600_provider_runtime_rejection_reroutes_in_place(
    tmp_path, coordinator
):
    repo = _NF01160._init_git_repo(tmp_path / "repo")
    task_store.initialize_repository(repo)

    import sqlite3
    import json

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

    rejected = core.reject_review(
        TASK_ID,
        reason=REASON,
        topic=TOPIC,
        to="pending",
        failure_category="provider_runtime",
    )
    assert rejected.get("ok"), rejected

    result = _reroute(None)
    assert result["ok"] is True, result
    card = _NF01160._card(repo)
    assert card["task_id"] == TASK_ID
    assert card["runner"] == ALT_RUNNER
    identity_reroute = card["identity_reroute"]
    assert isinstance(identity_reroute, dict)
    authorization = identity_reroute["operational_provider_authorization"]
    assert authorization["authority"] == "manager_infrastructure_rejection"
    assert authorization["failure_category"] == "provider_runtime"
    assert (
        authorization["rejection_request_id"]
        == card["rejection_disposition"]["request_id"]
    )


def test_nf01600_candidate_code_still_requires_review_rejection_receipt(
    monkeypatch,
):
    dispositions = (
        _disposition(failure_category="candidate_code"),
        _disposition(failure_category=None, failure_category_source=None),
    )
    for rejection_disposition in dispositions:
        card = _pending_card(rejection_disposition)
        assert core._infrastructure_rejection_without_candidate(card) is False
        assert core._is_review_rejection_reroute(card) is True
        monkeypatch.setattr(core, "_live_card", lambda _task_id: (card, None))
        monkeypatch.setattr(
            task_store, "is_pristine_pending_card", lambda _card: False
        )
        result = _reroute(card)
        assert result["ok"] is False
        assert "reroute_manager_rejection_provenance_missing" in json.dumps(
            result
        )


def test_nf01600_retained_delta_pending_rejection_stays_review_reroute():
    card = _pending_card(_disposition())
    card["rework_predecessor"] = {
        "request_id": "cededcededcededcededcededcededcededcede",
        "changed_path_hashes": {"src/aiworkhub/core.py": "ab" * 32},
    }
    assert core._infrastructure_rejection_without_candidate(card) is False
    assert core._is_review_rejection_reroute(card) is True


def test_nf01600_infrastructure_rejection_predicate_unit_checks():
    assert core._infrastructure_rejection_without_candidate(
        _pending_card(_disposition())
    )
    assert not core._infrastructure_rejection_without_candidate(
        _pending_card(_disposition(to="blocked"))
    )
    assert not core._infrastructure_rejection_without_candidate(
        _pending_card(_disposition(failure_category_source="worker_reported"))
    )
    assert not core._infrastructure_rejection_without_candidate(
        _pending_card(_disposition(failure_category="candidate_code"))
    )