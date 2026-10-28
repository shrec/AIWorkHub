"""NF-2026-01777: a never-started autolaunch claim is retryable, never "launched".

Measured 2026-10-28: dependency autolaunch at startup moved needfix-NF-2026-01775
to processing and reported it launched, but production wires the bare
``core.claim_start_exact`` claim in as ``launch`` and no worker ever spawned.
After the grace the dead-process reconciler blocked the card
``dead_process_reconciled`` with ``launch_never_started`` evidence, which
``retry_terminal`` refused and ``stale_recovery_recommend`` told the manager to
keep.  The only way out lost the card id and its dependents' links.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiworkhub import core, process_launcher, stale_recovery, task_store

_B905_PATH = Path(__file__).resolve().parent / "test_aiworkhub_dependency_autolaunch_b905_v7.py"
_spec = importlib.util.spec_from_file_location("_b905_nf01777_helpers", _B905_PATH)
assert _spec is not None and _spec.loader is not None
b905 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b905)
autolaunch = b905.autolaunch

RUNNER = "worker_runner"
TOPIC = "terminal_retry"
EPOCH = 3
TASK = "NF01777_NEVER_STARTED"
REQUEST_ID = "dependency-autolaunch:startup:" + TASK
NOW = datetime(2026, 10, 28, 3, 11, 20, tzinfo=timezone.utc)
SUBSTATUS = "dead_process_reconciled"


def _write_coordinator_token(path: Path, token: str) -> None:
    """Create the token file already at 0600; chmod is denied in the sandbox."""

    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token_path = tmp_path / "coordinator.token"
    _write_coordinator_token(token_path, "coordinator-token")
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coordinator-token")
    monkeypatch.delenv(process_launcher.HEARTBEAT_LEASE_ENV, raising=False)
    return repo


def _connect(repo: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(task_store.storage_readiness(repo).canonical_db))
    conn.row_factory = sqlite3.Row
    return conn


def _claimed_at() -> str:
    grace = 3 * process_launcher.heartbeat_lease_seconds()
    return (NOW - timedelta(seconds=grace + 28)).isoformat()


def _insert_claim(repo: Path, task_id: str = TASK) -> str:
    claimed_at = _claimed_at()
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "objective": "a dependency-autolaunched claim no worker ever ran",
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": RUNNER,
        "claim_epoch": EPOCH,
        "launch_request_id": REQUEST_ID,
    }
    conn = _connect(repo)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by,claimed_at,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, RUNNER, TOPIC, "solo", "processing", "claimed", "normal",
                card["objective"], json.dumps(card, ensure_ascii=False, sort_keys=True),
                claimed_at, claimed_at, RUNNER, claimed_at, claimed_at, None,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return claimed_at


def _block(repo: Path, process_status: dict, task_id: str = TASK) -> None:
    claimed_at = _insert_claim(repo, task_id)
    assert stale_recovery.reconcile_dead_processing_claim(
        root=str(repo), task_id=task_id, request_id=REQUEST_ID, claim_epoch=EPOCH,
        process_status=process_status, actor="task_reconciler",
        claimed_at=claimed_at, now=NOW,
    ) == (True, "reconciled")


def _never_started(repo: Path, task_id: str = TASK) -> None:
    _block(repo, {"ok": False, "request_id": REQUEST_ID, "state": "not_found"}, task_id)


def _worker_died(repo: Path, task_id: str = TASK) -> None:
    event = {
        "request_id": REQUEST_ID, "task_id": task_id, "state": "blocked", "error": "worker died",
    }
    _block(repo, {
        "ok": True, "request_id": REQUEST_ID, "task_id": task_id, "state": "blocked",
        "process_alive": False, "exit_code": 1, "error": "worker died", "latest_event": event,
    }, task_id)


def _row(repo: Path, task_id: str = TASK) -> tuple:
    conn = _connect(repo)
    try:
        row = conn.execute(
            "SELECT status, worker_status, claimed_by, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return tuple(row)


# ---------------------------------------------------------------------------
# retry_terminal: the never-started episode is requeued under the same id
# ---------------------------------------------------------------------------


def test_never_started_claim_is_retryable_under_the_same_task_id(store: Path) -> None:
    _never_started(store)
    blocked = task_store.get_task(store, TASK)
    assert blocked["terminal_substatus"] == SUBSTATUS
    assert blocked["dead_process_reconciliation"]["evidence"]["reason"] == "launch_never_started"
    assert stale_recovery.never_started_request_id(blocked) == REQUEST_ID

    result = core.retry_terminal_task(TASK, REQUEST_ID, SUBSTATUS, "launch never started")

    assert result["ok"] is True, result
    assert result["retried_request_id"] == REQUEST_ID
    status, worker_status, claimed_by, card_json = _row(store)
    assert (status, worker_status, claimed_by) == ("pending", "unclaimed", None)
    card = json.loads(card_json)
    assert card["task_id"] == TASK
    assert card["terminal_retry"]["request_id"] == REQUEST_ID
    assert card["terminal_retry"]["terminal_substatus"] == SUBSTATUS
    # The next episode must be reconcilable on its own: a retained record of
    # this one would refuse it as reconcile_identity_mismatch.
    assert "dead_process_reconciliation" not in card
    assert "terminal_substatus" not in card

    repeated = core.retry_terminal_task(TASK, REQUEST_ID, SUBSTATUS, "launch never started")
    assert repeated["ok"] is True
    assert repeated["idempotent"] is True


def test_never_started_retry_still_requires_the_exact_request(store: Path) -> None:
    _never_started(store)
    before = _row(store)

    result = core.retry_terminal_task(TASK, "f" * 32, SUBSTATUS)

    assert result["ok"] is False
    assert "terminal_retry_request_mismatch" in str(result["stderr"])
    assert _row(store) == before


def test_a_worker_that_ran_and_died_stays_refused(store: Path) -> None:
    _worker_died(store)
    before = _row(store)

    result = core.retry_terminal_task(TASK, REQUEST_ID, SUBSTATUS)

    assert result["ok"] is False
    assert f"terminal_retry_substatus_not_operational:{SUBSTATUS}" in str(result["stderr"])
    assert _row(store) == before


def _evidence(**changes):
    return lambda card: card["dead_process_reconciliation"]["evidence"].update(changes)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_evidence(reason="other"), id="other_reason"),
        pytest.param(_evidence(state="blocked"), id="process_was_found"),
        pytest.param(_evidence(request_id="x"), id="evidence_names_another_request"),
        pytest.param(_evidence(task_id="OTHER"), id="evidence_names_another_task"),
        pytest.param(
            lambda card: card["dead_process_reconciliation"].update(request_id=""),
            id="no_request_id",
        ),
        pytest.param(
            lambda card: card["dead_process_reconciliation"].pop("evidence"), id="no_evidence"
        ),
        pytest.param(lambda card: card.pop("dead_process_reconciliation"), id="no_record"),
        pytest.param(
            lambda card: card.update(terminal_substatus="worker_failed"), id="other_substatus"
        ),
    ],
)
def test_only_the_exact_never_started_evidence_qualifies(store: Path, mutate) -> None:
    _never_started(store)
    card = task_store.get_task(store, TASK)
    assert stale_recovery.never_started_request_id(card) == REQUEST_ID

    mutate(card)

    assert stale_recovery.never_started_request_id(card) == ""


# ---------------------------------------------------------------------------
# stale_recovery_recommend(scope=blocked)
# ---------------------------------------------------------------------------


def _recommendations(repo: Path) -> dict[str, str]:
    triage = stale_recovery.build_blocked_triage(root=repo, needfix_status=lambda _id: None)
    assert triage.get("ok", True) is not False, triage
    return {
        row["task_id"]: row["recommendation"]
        for rows in triage["groups"].values()
        for row in rows
    }


def test_recommend_relaunches_a_never_started_episode_but_keeps_a_dead_worker(
    store: Path,
) -> None:
    _never_started(store, "NF01777_A_NEVER_STARTED")
    _worker_died(store, "NF01777_B_WORKER_DIED")

    assert _recommendations(store) == {
        "NF01777_A_NEVER_STARTED": "relaunch",
        "NF01777_B_WORKER_DIED": "keep",
    }


# ---------------------------------------------------------------------------
# dependency autolaunch: nothing is "launched" without a started process
# ---------------------------------------------------------------------------


def _ready_child(tmp_path: Path) -> Path:
    repo = b905._init_repo(tmp_path)
    b905._add(repo, "PARENT", status="finished", worker="done")
    b905._add(repo, "CHILD", deps=["PARENT"])
    return repo


def _claim_like_claim_start(repo: Path, task_id: str, runner: str, request_id: str) -> None:
    """Exactly what ``core.claim_start_exact`` leaves on the row: a claim, no process."""
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    try:
        card = json.loads(conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()[0])
        card.update(
            claim_epoch=1, launch_request_id=request_id,
            status="processing", worker_status="claimed", claimed_by=runner,
        )
        conn.execute(
            "UPDATE tasks SET card_json=?, status='processing', worker_status='claimed', "
            "claimed_by=?, claimed_at='t', started_at='t' WHERE task_id=?",
            (json.dumps(card), runner, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _events(repo: Path, task_id: str, event: str) -> list[dict]:
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event=?", (task_id, event)
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def test_startup_never_calls_the_bare_claim_and_leaves_the_card_pending(tmp_path: Path) -> None:
    repo = _ready_child(tmp_path)

    outcome = autolaunch.reconcile_startup(repo, core.claim_start_exact)

    assert outcome["launched"] == []
    assert {
        "task_id": "CHILD",
        "reason": autolaunch.LAUNCH_NOT_STARTED,
        "detail": "launch_callable_claims_without_spawning_a_worker",
    } in outcome["delayed"]
    assert b905._state(repo, "CHILD")[:2] == ("pending", "unclaimed")
    assert _events(repo, "CHILD", "claim_start") == []


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param(
            {"ok": True, "returncode": 0, "command": ["claim-start", "CHILD"]},
            id="claim_start_receipt",
        ),
        pytest.param({"ok": True, "process_started": False}, id="process_started_false"),
    ],
)
def test_ok_without_a_started_process_is_released_not_launched(tmp_path: Path, receipt) -> None:
    repo = _ready_child(tmp_path)
    requests: list[str] = []

    def claim_only(task_id, runner, topic, request_id):
        requests.append(request_id)
        _claim_like_claim_start(repo, task_id, runner, request_id)
        return dict(receipt)

    outcome = autolaunch.reconcile_startup(repo, claim_only)

    assert requests == ["dependency-autolaunch:startup:CHILD"]
    assert outcome["launched"] == []
    [entry] = [row for row in outcome["delayed"] if row["task_id"] == "CHILD"]
    assert entry["reason"] == autolaunch.LAUNCH_NOT_STARTED
    assert entry["detail"] == "claim_without_worker"
    assert entry["claim_released"] is True
    assert entry["denial_kind"] == "transient"
    status, worker_status, card = b905._state(repo, "CHILD")
    assert (status, worker_status) == ("pending", "unclaimed")
    assert "launch_request_id" not in card
    assert card["status"] == "pending" and card["worker_status"] == "unclaimed"
    released = _events(repo, "CHILD", autolaunch.LAUNCH_NOT_STARTED)
    assert [event["request_id"] for event in released] == ["dependency-autolaunch:startup:CHILD"]


def test_a_claim_held_by_another_request_is_never_released(tmp_path: Path) -> None:
    repo = _ready_child(tmp_path)

    def foreign_claim(task_id, runner, topic, request_id):
        _claim_like_claim_start(repo, task_id, runner, "someone-else")
        return {"ok": True, "process_started": False}

    outcome = autolaunch.reconcile_startup(repo, foreign_claim)

    assert outcome["launched"] == []
    [entry] = [row for row in outcome["delayed"] if row["task_id"] == "CHILD"]
    assert entry["claim_released"] is False
    assert b905._state(repo, "CHILD")[:2] == ("processing", "claimed")


def test_a_launch_that_started_a_process_is_still_launched(tmp_path: Path) -> None:
    repo = _ready_child(tmp_path)
    calls: list[str] = []

    outcome = autolaunch.reconcile_startup(repo, b905._canonical_claim_start(repo, calls))

    assert calls == ["CHILD"]
    assert [row["task_id"] for row in outcome["launched"]] == ["CHILD"]
    assert b905._state(repo, "CHILD")[:2] == ("processing", "claimed")
