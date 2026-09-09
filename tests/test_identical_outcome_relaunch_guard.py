"""The second refusal axis: an outcome that has already repeated.

The first axis (``identical_relaunch_blocked``) compares the CARD, and cannot
fire on the rework path at all: every rejection writes new ``review_feedback``,
and ``reject_review`` runs ``begin_claim_episode``, which erases the card's own
``terminal_review``. This axis reads the durable ``task_events`` terminal rows
and compares OUTCOMES -- the error identity AND the candidate bytes -- so it
sees exactly the loop the first axis is blind to.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from aiworkhub import core, launch_replay_guard, task_store


RUNNER = "claude_sonnet-4.6"
ADAPTER = "claude_cli"
REQUEST_IDS = ("a" * 32, "b" * 32, "c" * 32)
ERROR_TEXT = "validation_failed:python -m pytest -q tests/test_x.py:rc=1"
CANDIDATE = {
    "out/result.json": "0" * 64,
    "src/aiworkhub/thing.py": "1" * 64,
}
OTHER_CANDIDATE = {"out/result.json": "2" * 64}


@pytest.fixture
def coordinator_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token_path = tmp_path / "coordinator.token"
    token_path.write_text("coordinator-token\n", encoding="utf-8")
    os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coordinator-token")
    return repo


def _card(task_id: str = "OUTCOME_LOOP", *, runner: str = RUNNER) -> dict:
    return {
        "task_id": task_id,
        "runner": runner,
        "topic": "terminal_retry",
        "mode": "solo",
        "objective": "stop the identical-outcome loop",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": None,
        "allowed_writes": ["out/result.json"],
        "required_outputs": ["out/result.json"],
        "validation": ["python -m pytest tests/test_process_launcher.py"],
        # A fresh rework feedback every time: this is exactly what makes the
        # first axis unable to fire, and this axis must not depend on it.
        "review_feedback": {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": "attempt 4",
        },
    }


def _insert_card(repo: Path, card: dict) -> None:
    readiness = task_store.storage_readiness(repo)
    now = "2026-09-06T00:00:00+00:00"
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                card["task_id"],
                card["runner"],
                card["topic"],
                "solo",
                "pending",
                "unclaimed",
                "normal",
                card["objective"],
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                now,
                now,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _terminal(
    card: dict,
    *,
    request_id: str,
    recorded_at: str,
    runner: str = RUNNER,
    adapter_id: str = ADAPTER,
    error: str = ERROR_TEXT,
    candidate: dict | None = None,
    substatus: str = "validation_failed",
    pin_card: bool = True,
) -> dict:
    record = {
        "substatus": substatus,
        "runner": runner,
        "adapter_id": adapter_id,
        "request_id": request_id,
        "error_hash": task_store.bounded_error_hash(error),
        "recorded_at": recorded_at,
        "evidence": {
            "request_id": request_id,
            "adapter_id": adapter_id,
            "error": error,
            "changed_path_hashes": dict(CANDIDATE if candidate is None else candidate),
        },
        # Deliberately a DIFFERENT feedback identity on every attempt, the way
        # a real rejection writes one.
        "review_feedback_identity": f"feedback-{request_id[:4]}",
    }
    if pin_card:
        record["card_content_sha256"] = task_store.card_content_identity(card)
    return record


def _record_terminals(repo: Path, card: dict, records: list[dict]) -> None:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        for record in records:
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
                (
                    card["task_id"],
                    "terminal_review",
                    str(record.get("runner") or ""),
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                    str(record.get("recorded_at") or ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _looping_task(repo: Path, *, repeats: int = 3, **card_kwargs) -> dict:
    """A card whose last ``repeats`` terminals are one identical outcome."""

    card = _card(**card_kwargs)
    _insert_card(repo, card)
    _record_terminals(
        repo,
        card,
        [
            _terminal(
                card,
                request_id=REQUEST_IDS[index],
                recorded_at=f"2026-09-06T0{index}:00:00+00:00",
            )
            for index in range(repeats)
        ],
    )
    return card


def _refusal(card: dict, repo: Path, *, runner: str = RUNNER, adapter_id: str = ADAPTER) -> str:
    return launch_replay_guard.identical_relaunch_refusal(
        card, runner=runner, adapter_id=adapter_id, repo=repo
    )


def test_identical_error_and_identical_candidate_on_the_same_runner_refuses(
    coordinator_repo: Path,
) -> None:
    card = _looping_task(coordinator_repo)

    refusal = _refusal(card, coordinator_repo)

    assert refusal.startswith(
        launch_replay_guard.IDENTICAL_OUTCOME_RELAUNCH_BLOCKED_REASON + ":"
    )
    fields = refusal.split(":")
    assert fields[1] == REQUEST_IDS[2]
    assert fields[2] == task_store.bounded_error_hash(ERROR_TEXT)
    assert "repeats=3" in refusal


def test_the_refusal_names_a_legal_next_move(coordinator_repo: Path) -> None:
    card = _looping_task(coordinator_repo)

    refusal = _refusal(card, coordinator_repo)

    assert "next=reroute_launch_identity|identical_outcome_override" in refusal
    for move in launch_replay_guard.IDENTICAL_OUTCOME_NEXT_MOVES:
        assert move in refusal


def test_two_identical_outcomes_are_not_yet_a_loop(coordinator_repo: Path) -> None:
    """Measured: refusing on the second repeat blocks attempts that succeed."""

    card = _looping_task(coordinator_repo, repeats=2)

    assert _refusal(card, coordinator_repo) == ""


def test_identical_error_with_a_different_candidate_launches(
    coordinator_repo: Path,
) -> None:
    card = _card()
    _insert_card(coordinator_repo, card)
    _record_terminals(
        coordinator_repo,
        card,
        [
            _terminal(card, request_id=REQUEST_IDS[0], recorded_at="2026-09-06T00:00:00+00:00"),
            _terminal(card, request_id=REQUEST_IDS[1], recorded_at="2026-09-06T01:00:00+00:00"),
            # Same failure, different bytes: a real new attempt.
            _terminal(
                card,
                request_id=REQUEST_IDS[2],
                recorded_at="2026-09-06T02:00:00+00:00",
                candidate=OTHER_CANDIDATE,
            ),
        ],
    )

    assert _refusal(card, coordinator_repo) == ""


def test_identical_candidate_with_a_different_runner_launches(
    coordinator_repo: Path,
) -> None:
    card = _looping_task(coordinator_repo)

    assert _refusal(card, coordinator_repo, runner="claude_sonnet-5") == ""
    assert _refusal(card, coordinator_repo, adapter_id="codex_cli") == ""


def test_a_different_error_ends_the_run(coordinator_repo: Path) -> None:
    card = _card()
    _insert_card(coordinator_repo, card)
    _record_terminals(
        coordinator_repo,
        card,
        [
            _terminal(card, request_id=REQUEST_IDS[0], recorded_at="2026-09-06T00:00:00+00:00"),
            _terminal(card, request_id=REQUEST_IDS[1], recorded_at="2026-09-06T01:00:00+00:00"),
            _terminal(
                card,
                request_id=REQUEST_IDS[2],
                recorded_at="2026-09-06T02:00:00+00:00",
                error="a different failure entirely",
            ),
        ],
    )

    assert _refusal(card, coordinator_repo) == ""


def test_a_changed_contract_launches(coordinator_repo: Path) -> None:
    card = _looping_task(coordinator_repo)
    card["objective"] = "a materially different objective"

    assert _refusal(card, coordinator_repo) == ""


def test_an_unmeasurable_candidate_fails_open(coordinator_repo: Path) -> None:
    card = _card()
    _insert_card(coordinator_repo, card)
    _record_terminals(
        coordinator_repo,
        card,
        [
            _terminal(
                card,
                request_id=REQUEST_IDS[index],
                recorded_at=f"2026-09-06T0{index}:00:00+00:00",
                candidate={},
            )
            for index in range(3)
        ],
    )

    assert _refusal(card, coordinator_repo) == ""


def test_a_repo_without_a_store_fails_open(tmp_path: Path) -> None:
    assert (
        launch_replay_guard.identical_relaunch_refusal(
            _card(), runner=RUNNER, adapter_id=ADAPTER, repo=tmp_path / "absent"
        )
        == ""
    )


def test_a_coordinator_recovery_supersedes_the_refusal(coordinator_repo: Path) -> None:
    card = _looping_task(coordinator_repo)
    card["recovered_from_blocked_at"] = "2026-09-07T00:00:00+00:00"

    assert _refusal(card, coordinator_repo) == ""


def test_the_recorded_override_launches(coordinator_repo: Path) -> None:
    card = _looping_task(coordinator_repo)
    card["identical_outcome_override"] = {
        "schema_id": launch_replay_guard.IDENTICAL_OUTCOME_OVERRIDE_SCHEMA_ID,
        "request_id": REQUEST_IDS[2],
        "error_hash": task_store.bounded_error_hash(ERROR_TEXT),
        "reason": "measured: the fix landed outside the candidate",
    }

    assert _refusal(card, coordinator_repo) == ""


def test_an_override_naming_another_outcome_still_refuses(
    coordinator_repo: Path,
) -> None:
    card = _looping_task(coordinator_repo)
    card["identical_outcome_override"] = {
        "schema_id": launch_replay_guard.IDENTICAL_OUTCOME_OVERRIDE_SCHEMA_ID,
        "request_id": REQUEST_IDS[0],
        "error_hash": task_store.bounded_error_hash(ERROR_TEXT),
        "reason": "stale override for an earlier episode",
    }

    assert _refusal(card, coordinator_repo).startswith(
        launch_replay_guard.IDENTICAL_OUTCOME_RELAUNCH_BLOCKED_REASON
    )


def test_the_manager_can_authorize_the_relaunch_in_place(
    coordinator_repo: Path,
) -> None:
    card = _looping_task(coordinator_repo)
    error_hash = task_store.bounded_error_hash(ERROR_TEXT)

    result = core.authorize_identical_outcome_relaunch(
        card["task_id"],
        REQUEST_IDS[2],
        error_hash,
        reason="the failure is environmental, not in the candidate",
    )

    assert result["ok"] is True, result
    stored = task_store.get_task(coordinator_repo, card["task_id"])
    assert stored is not None
    override = stored["identical_outcome_override"]
    assert override["schema_id"] == launch_replay_guard.IDENTICAL_OUTCOME_OVERRIDE_SCHEMA_ID
    assert override["request_id"] == REQUEST_IDS[2]
    assert override["error_hash"] == error_hash
    assert override["reason"]
    # The card is launchable again, on the same identity, and the permission
    # is on the card where the next reader can see it.
    assert _refusal(stored, coordinator_repo) == ""


def test_an_override_without_a_live_refusal_is_refused(coordinator_repo: Path) -> None:
    card = _looping_task(coordinator_repo, repeats=2)

    result = core.authorize_identical_outcome_relaunch(
        card["task_id"],
        REQUEST_IDS[1],
        task_store.bounded_error_hash(ERROR_TEXT),
        reason="pre-arming a future repeat",
    )

    assert result["ok"] is False
    assert "identical_outcome_override_no_live_refusal" in result["stderr"]


def test_an_override_must_name_the_exact_outcome(coordinator_repo: Path) -> None:
    card = _looping_task(coordinator_repo)

    result = core.authorize_identical_outcome_relaunch(
        card["task_id"],
        REQUEST_IDS[0],
        task_store.bounded_error_hash(ERROR_TEXT),
        reason="wrong predecessor",
    )

    assert result["ok"] is False
    assert "identical_outcome_override_request_mismatch" in result["stderr"]


def test_reroute_accepts_a_validation_failed_predecessor_carrying_the_refusal(
    coordinator_repo: Path,
) -> None:
    """The manager moves a refused card in place -- same task id, same history."""

    card = _looping_task(coordinator_repo)
    assert "terminal_retry" not in card

    result = core.reroute_launch_identity(
        card["task_id"],
        from_runner=RUNNER,
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
        reason="three identical outcomes on this provider",
    )

    assert result["ok"] is True, result
    stored = task_store.get_task(coordinator_repo, card["task_id"])
    assert stored is not None
    assert stored["task_id"] == card["task_id"]
    assert stored["runner"] == "claude_sonnet-5"
    assert stored["status"] == "pending" and stored["worker_status"] == "unclaimed"
    receipt = stored["identity_reroute"]["identical_outcome_refusal"]
    assert receipt["schema_id"] == launch_replay_guard.IDENTICAL_OUTCOME_REFUSAL_SCHEMA_ID
    assert receipt["request_id"] == REQUEST_IDS[2]
    assert receipt["terminal_substatus"] == "validation_failed"
    assert receipt["repeats"] == launch_replay_guard.IDENTICAL_OUTCOME_REPEAT_THRESHOLD
    # And the reroute is what makes the card launchable again: the refusal is
    # pinned to the runner it was measured on.
    assert (
        launch_replay_guard.identical_relaunch_refusal(
            stored, runner="claude_sonnet-5", adapter_id="claude_cli", repo=coordinator_repo
        )
        == ""
    )


def test_reroute_without_a_live_refusal_still_requires_retry_provenance(
    coordinator_repo: Path,
) -> None:
    card = _looping_task(coordinator_repo, repeats=2)

    result = core.reroute_launch_identity(
        card["task_id"],
        from_runner=RUNNER,
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert "reroute_requires_terminal_retry_provenance" in result["stderr"]
