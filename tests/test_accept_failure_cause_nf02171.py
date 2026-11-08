"""NF-2026-02171: a reject after a failed accept reads that accept's structured record.

The reject tests seed the durable ``accept_review_failed`` row directly and never
import the new module, so on a tree without it they fail on the recorded category
(``candidate_code``) rather than on an import error.
"""
from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    core,
    launch_replay_guard,
    process_launcher_accept_review,
    task_store,
    validation_base_triage,
)
from test_base_drift_reject_nf01832 import (  # noqa: E402,F401  (fixtures are used by name)
    _CANDIDATE_PATH,
    _TOPIC,
    _commit,
    _db,
    _insert_task,
    _reject_rows,
    _to_review,
    category,
    drifted,
    repo,
)

_REQUEST = "b" * 32
_OTHER_REQUEST = "c" * 32
_EVENT = "accept_review_failed"
_CONFLICT = "promotion_merge_conflict:src/existing.py:conflicts=1"
_VALIDATION_ERROR = "revalidation_failed:validation_failed:pytest -q:rc=1:stdout=:stderr="
_RED_ROW = {
    "command": "pytest -q tests/existing.py",
    "declared_command": "pytest -q tests/existing.py",
    "returncode": 1,
}
_GREEN_ROW = {"command": "ruff check", "returncode": 0}
_BASE_FAILED_TRIAGE = {"schema_id": "triage.v1", "outcome": "base_failed", "reason": "", "commands": []}


def _cause():
    return importlib.import_module("aiworkhub.accept_failure_cause")


def _seed_failure(root: Path, task_id: str, **payload: object) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, _EVENT, "accept_failure_cause", json.dumps(payload), "2026-11-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _failure_rows(root: Path, task_id: str) -> list[dict]:
    conn = sqlite3.connect(_db(root))
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event=? ORDER BY event_id",
            (task_id, _EVENT),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def _review(root: Path, task_id: str, base: str | None = None) -> None:
    _insert_task(root, task_id)
    _to_review(root, task_id, _REQUEST, base)


def _base_failed(root: Path, task_id: str, *, request_id: str = _REQUEST, error: str = _VALIDATION_ERROR) -> None:
    _seed_failure(root, task_id, request_id=request_id, error=error, triage=_BASE_FAILED_TRIAGE)


def _reject(task_id: str, **kwargs) -> dict:
    result = core.reject_review(task_id, "not acceptable", topic=_TOPIC, **kwargs)
    assert result["ok"] is True, result
    return result


def _assert_unchanged(root: Path, task_id: str) -> None:
    (row,) = _reject_rows(root, task_id)
    assert row["failure_category"] == "candidate_code"
    assert row["failure_category_source"] == "terminal_evidence"
    assert "base_drift_proof" not in row


def test_base_failed_accept_makes_the_reject_environmental_and_uncounted(repo, category):
    task_id = "NF02171_BASE_FAILED"
    _review(repo, task_id)
    _base_failed(repo, task_id)

    _reject(task_id)

    (row,) = _reject_rows(repo, task_id)
    assert row["failure_category"] == "validation_environment"
    assert row["terminal_disposition"] == "validation_environment"
    assert row["failure_category_source"] == "accept_failure_record"
    assert launch_replay_guard.counted_candidate_code_rejects([row], row["contract_identity"]) == []
    pinned = task_store.get_task(repo, task_id)["rejection_disposition"]
    assert pinned["failure_category"] == "validation_environment"
    assert pinned["failure_category_source"] == "accept_failure_record"


def test_promotion_conflict_with_git_proof_is_base_drift_and_stores_the_proof(repo, drifted, category):
    base, head = drifted
    task_id = "NF02171_CONFLICT_PROVEN"
    _review(repo, task_id, base)
    _seed_failure(repo, task_id, request_id=_REQUEST, error=_CONFLICT)

    _reject(task_id)

    (row,) = _reject_rows(repo, task_id)
    assert row["failure_category"] == "base_drift"
    assert row["failure_category_source"] == "accept_failure_record"
    assert row["base_drift_proof"]["base_oid"] == base
    assert row["base_drift_proof"]["head_oid"] == head
    assert row["base_drift_proof"]["drifted_paths"] == [_CANDIDATE_PATH]
    pinned = task_store.get_task(repo, task_id)["rejection_disposition"]
    assert pinned["base_drift_proof"] == row["base_drift_proof"]
    assert launch_replay_guard.counted_candidate_code_rejects([row], row["contract_identity"]) == []


def test_promotion_conflict_without_a_proof_stays_candidate_code(repo, category):
    # Canonical has not moved: the base IS head, so there is no drift to prove.
    base = _commit(repo, _CANDIDATE_PATH, "value = 1\n")
    task_id = "NF02171_CONFLICT_UNPROVEN"
    _review(repo, task_id, base)
    _seed_failure(repo, task_id, request_id=_REQUEST, error=_CONFLICT)

    _reject(task_id)

    _assert_unchanged(repo, task_id)


def test_confirmed_finding_keeps_candidate_code_despite_a_base_failure(repo, category, monkeypatch):
    task_id = "NF02171_CONFIRMED"
    _review(repo, task_id)
    _base_failed(repo, task_id)
    seen: list[tuple[str, str]] = []

    def dispositions(_manager, task, request):
        seen.append((task, request))
        return [{"disposition": "confirmed", "finding_id": "F-1"}]

    monkeypatch.setattr(process_launcher_accept_review, "review_finding_dispositions", dispositions)

    _reject(task_id)

    _assert_unchanged(repo, task_id)
    assert seen == [(task_id, _REQUEST)]


def test_a_refuted_finding_does_not_block_the_classification(repo, category, monkeypatch):
    task_id = "NF02171_REFUTED"
    _review(repo, task_id)
    _base_failed(repo, task_id)
    monkeypatch.setattr(
        process_launcher_accept_review,
        "review_finding_dispositions",
        lambda *_a, **_k: [{"disposition": "refuted"}],
    )

    _reject(task_id)

    (row,) = _reject_rows(repo, task_id)
    assert row["failure_category"] == "validation_environment"


def test_unreadable_dispositions_excuse_nothing(repo, category, monkeypatch):
    task_id = "NF02171_DISPOSITIONS_RAISE"
    _review(repo, task_id)
    _base_failed(repo, task_id)

    def boom(*_a, **_k):
        raise OSError("review store unreadable")

    monkeypatch.setattr(process_launcher_accept_review, "review_finding_dispositions", boom)

    _reject(task_id)

    _assert_unchanged(repo, task_id)


def test_a_row_for_another_request_is_ignored(repo, category):
    task_id = "NF02171_STALE"
    _review(repo, task_id)
    _base_failed(repo, task_id, request_id=_OTHER_REQUEST)

    _reject(task_id)

    _assert_unchanged(repo, task_id)


@pytest.mark.parametrize(
    "error",
    [
        'revalidation_failed:neighbour_tests_failed:{"tests": ["tests/test_x.py"]}',
        'revalidation_failed:combined_tree_quality_failed:{"blockers": ["ruff"]}',
        'neighbour_tests_failed:{"tests": ["tests/test_x.py"]}',
    ],
)
def test_neighbour_and_combined_tree_refusals_stay_candidate_code(repo, category, error):
    task_id = "NF02171_NEIGHBOUR"
    _review(repo, task_id)
    _base_failed(repo, task_id, error=error)

    _reject(task_id)

    _assert_unchanged(repo, task_id)


@pytest.mark.parametrize("outcome", ["base_passed", "base_unrunnable", ""])
def test_a_triage_that_did_not_fail_on_base_changes_nothing(repo, category, outcome):
    task_id = "NF02171_TRIAGE_OTHER"
    _review(repo, task_id)
    _seed_failure(repo, task_id, request_id=_REQUEST, error=_VALIDATION_ERROR, triage={"outcome": outcome})

    _reject(task_id)

    _assert_unchanged(repo, task_id)


def test_only_the_newest_row_counts(repo, category):
    task_id = "NF02171_NEWEST"
    _review(repo, task_id)
    _base_failed(repo, task_id)
    _seed_failure(repo, task_id, request_id=_REQUEST, error="stale_input:src/existing.py")

    _reject(task_id)

    _assert_unchanged(repo, task_id)


def test_no_row_and_a_spoofing_reason_change_nothing(repo, category):
    task_id = "NF02171_NO_ROW"
    _review(repo, task_id)

    core.reject_review(task_id, f"{_CONFLICT} base_failed validation_environment", topic=_TOPIC)

    _assert_unchanged(repo, task_id)


def test_explicit_manager_category_is_never_overridden(repo, drifted, category):
    base, _head = drifted
    task_id = "NF02171_EXPLICIT"
    _review(repo, task_id, base)
    _base_failed(repo, task_id)

    _reject(task_id, failure_category="base_drift")

    (row,) = _reject_rows(repo, task_id)
    assert row["failure_category"] == "base_drift"
    assert row["failure_category_source"] == "manager_explicit_infrastructure"


def _failed_reply(**extra: object) -> dict:
    return {"ok": False, "error": _VALIDATION_ERROR, "request_id": _REQUEST, "task_id": "T", **extra}


def _wrapped(reply: dict):
    return _cause().record_accept_failures(lambda self, request_id, task_id, **_kw: dict(reply))


def _manager(root: Path):
    return SimpleNamespace(repo=root, _request_events=lambda _rid: [{"adapter_id": "claude_cli"}])


def _private(rows: list[dict] | None) -> dict:
    return {_cause().PRIVATE_RESULTS_KEY: rows}


def test_reply_is_byte_identical_and_the_row_carries_the_triage(repo, category, monkeypatch):
    task_id = "NF02171_WRAP"
    _review(repo, task_id)
    captured: dict = {}

    def triage(_repo, **kwargs):
        captured.update(kwargs)
        return dict(_BASE_FAILED_TRIAGE)

    monkeypatch.setattr(validation_base_triage, "triage", triage)

    reply = _wrapped(_failed_reply(task_id=task_id, **_private([_RED_ROW, _GREEN_ROW])))(
        _manager(repo), _REQUEST, task_id
    )

    assert json.dumps(reply, sort_keys=True) == json.dumps(_failed_reply(task_id=task_id), sort_keys=True)
    assert captured["commands"] == [_RED_ROW["command"]]
    assert captured["adapter_id"] == "claude_cli"
    assert captured["candidate_rows"][_RED_ROW["command"]]["returncode"] == 1
    (row,) = _failure_rows(repo, task_id)
    assert row["request_id"] == _REQUEST
    assert row["error"] == _VALIDATION_ERROR.partition(":stdout=")[0]
    assert row["commands"] == [_RED_ROW["command"]]
    assert row["triage"]["outcome"] == "base_failed"

    _reject(task_id)

    (rejected,) = _reject_rows(repo, task_id)
    assert rejected["failure_category"] == "validation_environment"


def test_a_failure_without_rows_is_recorded_without_triage(repo, category, monkeypatch):
    task_id = "NF02171_NO_RESULTS"
    _review(repo, task_id)

    def triage(*_a, **_k):
        raise AssertionError("no validation rows, nothing to triage")

    monkeypatch.setattr(validation_base_triage, "triage", triage)

    _wrapped(_failed_reply(error=_CONFLICT, task_id=task_id, **_private(None)))(_manager(repo), _REQUEST, task_id)

    (row,) = _failure_rows(repo, task_id)
    assert row["error"] == _CONFLICT
    assert row["commands"] == []
    assert "triage" not in row


def test_neighbour_rows_are_never_triaged(repo, category, monkeypatch):
    task_id = "NF02171_NEIGHBOUR_ROWS"
    _review(repo, task_id)

    def triage(*_a, **_k):
        raise AssertionError("a neighbour refusal is not a base-commit question")

    monkeypatch.setattr(validation_base_triage, "triage", triage)
    error = 'revalidation_failed:neighbour_tests_failed:{"tests": []}'

    _wrapped(_failed_reply(error=error, task_id=task_id, **_private([_RED_ROW])))(_manager(repo), _REQUEST, task_id)

    (row,) = _failure_rows(repo, task_id)
    assert row["commands"] == [_RED_ROW["command"]]
    assert "triage" not in row


def test_success_writes_nothing_and_still_drops_the_private_key(repo):
    task_id = "NF02171_OK"
    _review(repo, task_id)

    reply = _wrapped({"ok": True, **_private(None)})(_manager(repo), _REQUEST, task_id)

    assert reply == {"ok": True}
    assert _failure_rows(repo, task_id) == []


def test_a_failing_event_write_changes_nothing(repo, monkeypatch, caplog):
    task_id = "NF02171_WRITE_FAILS"
    _review(repo, task_id)
    real_connect = task_store._connect

    def connect(path, *args, **kwargs):
        if kwargs.get("readonly"):
            return real_connect(path, *args, **kwargs)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(validation_base_triage, "triage", lambda *_a, **_k: dict(_BASE_FAILED_TRIAGE))
    monkeypatch.setattr(task_store, "_connect", connect)

    with caplog.at_level(logging.WARNING, logger="aiworkhub.accept_failure_cause"):
        reply = _wrapped(_failed_reply(task_id=task_id, **_private([_RED_ROW])))(
            _manager(repo), _REQUEST, task_id
        )

    assert reply == _failed_reply(task_id=task_id)
    assert "OperationalError" in caplog.text
    monkeypatch.undo()
    assert _failure_rows(repo, task_id) == []


def test_a_raising_triage_still_records_the_failure_and_excuses_nothing(repo, category, monkeypatch):
    task_id = "NF02171_TRIAGE_RAISES"
    _review(repo, task_id)

    def triage(*_a, **_k):
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(validation_base_triage, "triage", triage)

    reply = _wrapped(_failed_reply(task_id=task_id, **_private([_RED_ROW])))(_manager(repo), _REQUEST, task_id)

    assert reply == _failed_reply(task_id=task_id)
    (row,) = _failure_rows(repo, task_id)
    assert "triage" not in row

    _reject(task_id)

    _assert_unchanged(repo, task_id)


def test_an_unready_store_is_only_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="aiworkhub.accept_failure_cause"):
        reply = _wrapped(_failed_reply(**_private([_RED_ROW])))(_manager(tmp_path), _REQUEST, "T")

    assert reply == _failed_reply()
    assert "StorageNotReadyError" in caplog.text


def test_an_exception_from_accept_propagates_unchanged_and_writes_nothing(repo):
    task_id = "NF02171_RAISES"
    _review(repo, task_id)
    failure = ValueError("accept broke")

    def accept(self, request_id, task_id, **_kw):
        raise failure

    with pytest.raises(ValueError) as raised:
        _cause().record_accept_failures(accept)(_manager(repo), _REQUEST, task_id)

    assert raised.value is failure
    assert _failure_rows(repo, task_id) == []


def test_an_oversized_triage_keeps_the_verdict(repo, monkeypatch):
    task_id = "NF02171_BOUNDED"
    _review(repo, task_id)
    monkeypatch.setattr(
        validation_base_triage,
        "triage",
        lambda *_a, **_k: {**_BASE_FAILED_TRIAGE, "commands": [{"base_tail": "x" * 200_000}]},
    )

    _wrapped(_failed_reply(task_id=task_id, **_private([_RED_ROW])))(_manager(repo), _REQUEST, task_id)

    (row,) = _failure_rows(repo, task_id)
    assert "base_tail" not in json.dumps(row)
    assert row["triage"]["outcome"] == "base_failed"
    assert row["request_id"] == _REQUEST


def test_accept_review_is_wrapped_at_module_level_with_its_signature():
    wrapped = process_launcher_accept_review.accept_review

    assert wrapped.__wrapped__.__name__ == "accept_review"
    assert wrapped.__module__ == process_launcher_accept_review.__name__
