"""NF-2026-00638: a hard ceiling on reject -> rework -> reject loops."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, launch_replay_guard, task_store  # noqa: E402
from aiworkhub.learning_commit import FailureCategory  # noqa: E402

_NOW = "2026-10-27T00:00:00+00:00"
_RUNNER = "claude_task_mcp_runtime_wiring"
_ADAPTER = "claude_cli"
_TOPIC = "task_mcp"
_MANAGER = "nf00638-manager"
_CAPPED = "reject_depth_cap_reached:2:2:decompose_or_supersede_required"
_CONTRACT = {
    "objective": "objective",
    "acceptance": ["the cap holds"],
    "allowed_writes": ["src/existing.py"],
    "validation": ["pytest -q tests/existing.py"],
    "required_outputs": ["src/existing.py"],
}
_NEVER_COUNTED = (
    FailureCategory.VALIDATION_ENVIRONMENT,
    FailureCategory.PROVIDER_RUNTIME,
    FailureCategory.DEPENDENCY_OR_ROUTE,
    FailureCategory.CANCELLATION_OR_TIMEOUT,
    FailureCategory.INCONCLUSIVE,
)


def _db(root: Path) -> str:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    return readiness.canonical_db


def _insert_task(root: Path, task_id: str, extra_card: dict | None = None) -> None:
    card = {"origin_thread_id": "nf00638-thread", **_CONTRACT, **(extra_card or {})}
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, _RUNNER, _TOPIC, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps(card), _NOW, _NOW, None, "nf00638-thread"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_review_request_id(root: Path, task_id: str, request_id: str) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row[0])
        terminal = card.get("terminal_review")
        terminal = terminal if isinstance(terminal, dict) else {}
        evidence = terminal.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        evidence["request_identity"] = {"request_id": request_id, "task_id": task_id}
        terminal["evidence"] = evidence
        card["terminal_review"] = terminal
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def _to_review(root: Path, task_id: str, request_id: str) -> None:
    assert core.claim_start_exact(task_id, _RUNNER, _TOPIC)["ok"] is True
    assert core.mark_review(task_id, runner=_RUNNER, topic=_TOPIC)["ok"] is True
    _seed_review_request_id(root, task_id, request_id)


def _launch_card(task_id: str) -> dict:
    # The launcher reads the same rendered card the reject path judged.
    return json.loads(core.show_task(task_id)["stdout"])


def _refusal(root: Path, task_id: str) -> str:
    return launch_replay_guard.identical_relaunch_refusal(
        _launch_card(task_id), runner=_RUNNER, adapter_id=_ADAPTER, repo=root
    )


def _reject_rows(root: Path, task_id: str) -> list[dict]:
    conn = sqlite3.connect(_db(root))
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event='reject_review' "
            "ORDER BY event_id",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def _store_snapshot(root: Path, task_id: str) -> tuple:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute(
            "SELECT status, worker_status, card_json, updated_at FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT event_id, event, payload_json FROM task_events WHERE task_id=? "
            "ORDER BY event_id",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return tuple(row), tuple(events)


@pytest.fixture
def repo(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: _MANAGER)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    return root


@pytest.fixture
def category(monkeypatch) -> dict:
    """The structured classification each rejection derives from the card."""

    chosen = {"value": FailureCategory.CANDIDATE_CODE}
    monkeypatch.setattr(core, "classify_terminal_disposition", lambda _card: chosen["value"])
    return chosen


def _reject(task_id: str, **kwargs) -> dict:
    result = core.reject_review(task_id, "still wrong", topic=_TOPIC, **kwargs)
    assert result["ok"] is True, result
    return result


def test_two_candidate_code_rejects_refuse_the_third_launch(repo, category):
    task_id = "NF00638_CAP"
    _insert_task(repo, task_id)
    _to_review(repo, task_id, "a" * 32)
    judged = launch_replay_guard.task_contract_identity(_launch_card(task_id))

    first = _reject(task_id)

    assert "rejection_depth" not in first
    assert _refusal(repo, task_id) == ""

    _to_review(repo, task_id, "b" * 32)
    second = _reject(task_id)

    depth = second["rejection_depth"]
    assert depth["schema_id"] == launch_replay_guard.REJECT_DEPTH_SCHEMA_ID
    assert (depth["depth"], depth["cap"]) == (2, 2)
    assert depth["next_action"] == "decompose_or_supersede_required"
    assert depth["contract_identity"] == judged
    assert depth["rejected_request_ids"] == ["a" * 32, "b" * 32]
    assert depth["actor"] == _MANAGER
    assert task_store.get_task(repo, task_id)["rejection_depth"] == depth
    assert _refusal(repo, task_id) == _CAPPED


def test_every_reject_row_pins_its_classification_and_contract(repo, category):
    task_id = "NF00638_PIN"
    _insert_task(repo, task_id)
    _to_review(repo, task_id, "c" * 32)
    judged = launch_replay_guard.task_contract_identity(_launch_card(task_id))

    _reject(task_id)

    (row,) = _reject_rows(repo, task_id)
    assert row["failure_category"] == "candidate_code"
    assert row["terminal_disposition"] == "candidate_code"
    assert row["failure_category_source"] == "terminal_evidence"
    assert row["contract_identity"] == judged
    assert row["request_id"] == "c" * 32
    assert row["to"] == "pending"
    assert "rejection_depth" not in row


def test_the_capping_row_carries_the_record_durably(repo, category):
    task_id = "NF00638_DURABLE"
    _insert_task(repo, task_id)
    for request_id in ("d" * 32, "e" * 32):
        _to_review(repo, task_id, request_id)
        result = _reject(task_id)

    rows = _reject_rows(repo, task_id)
    assert rows[-1]["rejection_depth"] == result["rejection_depth"]


@pytest.mark.parametrize("never", _NEVER_COUNTED, ids=lambda c: c.value)
def test_non_candidate_code_rejects_never_count(repo, category, never):
    task_id = f"NF00638_NEVER_{never.value.upper()}"
    _insert_task(repo, task_id)
    category["value"] = never
    for request_id in ("f" * 32, "1" * 32, "2" * 32):
        _to_review(repo, task_id, request_id)
        result = _reject(task_id)
        assert "rejection_depth" not in result

    assert _refusal(repo, task_id) == ""


def test_an_infrastructure_reject_neither_counts_nor_resets_the_depth(repo, category):
    task_id = "NF00638_MIXED"
    _insert_task(repo, task_id)
    for request_id, verdict in (
        ("3" * 32, FailureCategory.CANDIDATE_CODE),
        ("4" * 32, FailureCategory.PROVIDER_RUNTIME),
    ):
        category["value"] = verdict
        _to_review(repo, task_id, request_id)
        _reject(task_id)

    assert _refusal(repo, task_id) == ""
    # The next candidate-code reject after the infra one is depth 2: the cap.
    category["value"] = FailureCategory.CANDIDATE_CODE
    _to_review(repo, task_id, "ac" * 16)
    capped = _reject(task_id)
    assert capped["rejection_depth"]["rejected_request_ids"] == ["3" * 32, "ac" * 16]
    assert _refusal(repo, task_id) == _CAPPED


def test_a_non_counted_reject_after_the_cap_carries_no_record(repo, category):
    task_id = "NF00638_AFTER_CAP"
    _insert_task(repo, task_id)
    for request_id in ("ba" * 16, "dc" * 16):
        _to_review(repo, task_id, request_id)
        _reject(task_id)
    _to_review(repo, task_id, "fe" * 16)

    blocked = _reject(task_id, to="blocked")

    assert "rejection_depth" not in blocked
    assert "rejection_depth" not in _reject_rows(repo, task_id)[-1]


def test_blocked_rejects_are_not_laps_of_the_rework_loop(repo, category):
    task_id = "NF00638_BLOCKED"
    _insert_task(repo, task_id)
    _to_review(repo, task_id, "5" * 32)
    _reject(task_id)
    _to_review(repo, task_id, "6" * 32)

    result = _reject(task_id, to="blocked")

    assert "rejection_depth" not in result
    assert _reject_rows(repo, task_id)[-1]["to"] == "blocked"


def _legacy_row(root: Path, task_id: str, payload: dict) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, "reject_review", _MANAGER, json.dumps(payload), _NOW),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "drop",
    ["failure_category", "failure_category_source", "contract_identity"],
)
def test_reject_rows_missing_classification_or_contract_never_count(repo, drop):
    task_id = f"NF00638_LEGACY_{drop.upper()}"
    _insert_task(repo, task_id)
    identity = launch_replay_guard.task_contract_identity(_launch_card(task_id))
    complete = {
        "to": "pending",
        "failure_category": "candidate_code",
        "failure_category_source": "terminal_evidence",
        "contract_identity": identity,
        "request_id": "7" * 32,
    }
    partial = {key: value for key, value in complete.items() if key != drop}
    for _ in range(3):
        _legacy_row(repo, task_id, partial)

    assert _refusal(repo, task_id) == ""
    # The same rows WITH every pinned field do count, so the omission is
    # what kept them out -- not an unrelated mismatch.
    for _ in range(2):
        _legacy_row(repo, task_id, complete)
    assert _refusal(repo, task_id) == _CAPPED


def test_pre_change_reject_rows_never_count(repo):
    task_id = "NF00638_PRE_CHANGE"
    _insert_task(repo, task_id)
    for _ in range(3):
        _legacy_row(repo, task_id, {
            "to": "pending",
            "terminal_disposition": "candidate_code",
            "failure_category_source": "terminal_evidence",
        })
    _legacy_row(repo, task_id, {"not": "a reject"})

    assert _refusal(repo, task_id) == ""


@pytest.mark.parametrize(
    "amendment",
    [
        {"validation_amendment": ["pytest -q tests/test_new_nf00638.py"]},
        {"allowed_writes_amendment": ["tests/test_new_nf00638.py"]},
    ],
    ids=["validation", "allowed_writes"],
)
def test_a_contract_amendment_starts_a_fresh_counter(repo, category, amendment):
    task_id = "NF00638_AMEND_" + next(iter(amendment)).upper()
    _insert_task(repo, task_id)
    _to_review(repo, task_id, "8" * 32)
    before = launch_replay_guard.task_contract_identity(_launch_card(task_id))
    _reject(task_id)
    _to_review(repo, task_id, "9" * 32)

    amended = _reject(task_id, **amendment)

    assert "rejection_depth" not in amended
    assert launch_replay_guard.task_contract_identity(_launch_card(task_id)) != before
    assert _refusal(repo, task_id) == ""
    # One more candidate-code rejection of the NEW contract is depth 1, not 3.
    _to_review(repo, task_id, "0" * 32)
    third = _reject(task_id)
    assert "rejection_depth" not in third
    assert _refusal(repo, task_id) == ""
    # ...and the second one reaches the cap again, on the new identity.
    _to_review(repo, task_id, "ab" * 16)
    fourth = _reject(task_id)
    assert fourth["rejection_depth"]["rejected_request_ids"] == ["0" * 32, "ab" * 16]
    assert _refusal(repo, task_id) == _CAPPED


def test_the_refusal_leaves_workspace_candidate_and_evidence_untouched(
    repo, category, tmp_path
):
    task_id = "NF00638_UNTOUCHED"
    _insert_task(repo, task_id)
    workspace = tmp_path / "retained_workspace" / task_id
    candidate = workspace / "src" / "existing.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"candidate bytes\n")
    for request_id in ("cd" * 16, "ef" * 16):
        _to_review(repo, task_id, request_id)
        _reject(task_id)
    before = _store_snapshot(repo, task_id)

    assert _refusal(repo, task_id) == _CAPPED
    assert _refusal(repo, task_id) == _CAPPED

    assert _store_snapshot(repo, task_id) == before
    assert candidate.read_bytes() == b"candidate bytes\n"
    evidence = launch_replay_guard.recorded_reject_events(repo, task_id)
    assert [row["request_id"] for row in evidence] == ["cd" * 16, "ef" * 16]


def test_a_superseding_successor_starts_at_depth_zero(repo, category):
    parent = "NF00638_PARENT"
    _insert_task(repo, parent)
    for request_id in ("12" * 16, "34" * 16):
        _to_review(repo, parent, request_id)
        _reject(parent)
    assert _refusal(repo, parent) == _CAPPED

    successor = "NF00638_SUCCESSOR"
    _insert_task(repo, successor, {"supersedes": parent})

    assert _refusal(repo, successor) == ""


def test_the_cap_is_counted_from_durable_events_not_the_card(repo, category):
    task_id = "NF00638_EVENTS_ONLY"
    _insert_task(repo, task_id)
    for request_id in ("56" * 16, "78" * 16):
        _to_review(repo, task_id, request_id)
        _reject(task_id)
    card = _launch_card(task_id)
    card.pop("rejection_depth", None)

    assert launch_replay_guard.identical_relaunch_refusal(
        card, runner=_RUNNER, adapter_id=_ADAPTER, repo=repo
    ) == _CAPPED
    assert launch_replay_guard.identical_relaunch_refusal(
        card, runner=_RUNNER, adapter_id=_ADAPTER, repo=None
    ) == ""


def test_unreadable_store_and_missing_card_fail_open():
    assert launch_replay_guard.reject_depth_refusal(None, repo="/nonexistent") == ""
    assert launch_replay_guard.reject_depth_refusal(
        {"task_id": "T"}, repo=None
    ) == ""
    assert launch_replay_guard.recorded_reject_events("/nonexistent/repo", "T") == []


def test_counting_is_a_positive_allowlist():
    identity = "f" * 64
    base = {
        "to": "pending",
        "failure_category": "candidate_code",
        "failure_category_source": "terminal_evidence",
        "contract_identity": identity,
    }
    rows = [
        dict(base, request_id="r1"),
        dict(base, request_id="r2", to="blocked"),
        dict(base, request_id="r3", failure_category="policy_or_scope"),
        dict(base, request_id="r4", failure_category="unknown"),
        dict(base, request_id="r5", contract_identity="0" * 64),
        "not a mapping",
        dict(base, request_id="r6"),
    ]

    assert launch_replay_guard.counted_candidate_code_rejects(rows, identity) == ["r1", "r6"]
    assert launch_replay_guard.counted_candidate_code_rejects(rows, "") == []
    assert launch_replay_guard.rejection_depth_record(
        contract_identity=identity, rejected_request_ids=["r1"], recorded_at=_NOW, actor="m"
    ) is None
