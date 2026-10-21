"""NF-2026-01634: a processing claim whose launch never produced a process record.

``ProcessManager.status`` answers ``not_found`` for a ``launch_request_id`` that
has no process record.  ``stale_recovery`` used to refuse that answer as missing
evidence, so the card stayed ``processing`` forever and neither the manager
repair nor the dashboard restart could release it.

Every test drives the real transition against a real temporary task store.  The
only stand-in is the process manager handed to the reconciler pass, which may
read the repository and process status and nothing else.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiworkhub import core, process_launcher, stale_recovery, task_reconciler, task_store

RUNNER = "worker_runner"
TOPIC = "terminal_retry"
EPOCH = 3
CREATED_AT = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
TASK = "NF01634_ORPHAN"


def _rid(n: int) -> str:
    return f"{n:032x}"


REQUEST_ID = _rid(1)


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


def _grace() -> float:
    return 3 * process_launcher.heartbeat_lease_seconds()


def _aged(seconds: float, *, now: datetime | None = None) -> str:
    return ((now or datetime.now(timezone.utc)) - timedelta(seconds=seconds)).isoformat()


def _insert(
    repo: Path,
    task_id: str,
    *,
    claimed_at: str | None,
    request_id: str = REQUEST_ID,
    epoch: int = EPOCH,
    status: str = "processing",
    worker_status: str = "claimed",
) -> None:
    claimed_by = RUNNER if worker_status == "claimed" else ""
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "objective": "release a claim whose launch never started",
        "status": status,
        "worker_status": worker_status,
        "claimed_by": claimed_by,
        "claim_epoch": epoch,
        "launch_request_id": request_id,
    }
    conn = _connect(repo)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by,claimed_at,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, RUNNER, TOPIC, "solo", status, worker_status, "normal",
                card["objective"], json.dumps(card, ensure_ascii=False, sort_keys=True),
                CREATED_AT, CREATED_AT, claimed_by or None, claimed_at, claimed_at, None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _retake(repo: Path, task_id: str, *, request_id: str) -> None:
    """A new claim episode lands on the card: next epoch, fresh claim time."""

    conn = _connect(repo)
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row["card_json"])
        card.update(claim_epoch=card["claim_epoch"] + 1, launch_request_id=request_id)
        conn.execute(
            "UPDATE tasks SET card_json=?, claimed_at=? WHERE task_id=?",
            (json.dumps(card, ensure_ascii=False, sort_keys=True), _aged(0), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot(repo: Path, task_id: str) -> tuple:
    conn = _connect(repo)
    try:
        row = conn.execute(
            "SELECT status, worker_status, claimed_at, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return tuple(row)


def _reconcile_actors(repo: Path, task_id: str) -> list[str]:
    conn = _connect(repo)
    try:
        rows = conn.execute(
            "SELECT runner FROM task_events WHERE task_id=? AND event='dead_process_reconciled'",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [str(row["runner"]) for row in rows]


def _not_found(request_id: str = REQUEST_ID) -> dict:
    return {"ok": False, "request_id": request_id, "state": "not_found"}


def _live(request_id: str, task_id: str) -> dict:
    return {
        "ok": True, "request_id": request_id, "task_id": task_id,
        "state": "running", "process_alive": True,
    }


def _dead(request_id: str, task_id: str) -> dict:
    event = {"request_id": request_id, "task_id": task_id, "state": "blocked", "error": "worker died"}
    return {
        "ok": True, "request_id": request_id, "task_id": task_id, "state": "blocked",
        "process_alive": False, "exit_code": 1, "error": "worker died", "latest_event": event,
    }


def _reconcile(
    repo: Path,
    *,
    claimed_at: str | None,
    now: datetime = NOW,
    task_id: str = TASK,
    request_id: str = REQUEST_ID,
    epoch: int = EPOCH,
    status: dict | None = None,
) -> tuple[bool, str]:
    return stale_recovery.reconcile_dead_processing_claim(
        root=str(repo), task_id=task_id, request_id=request_id, claim_epoch=epoch,
        process_status=_not_found(request_id) if status is None else status,
        actor="manager", claimed_at=claimed_at, now=now,
    )


class _Mgr:
    """What the reconciler pass may use: the repo, process status, and nothing else."""

    def __init__(self, repo: Path, statuses: dict[str, dict] | None = None) -> None:
        self.repo = repo
        self.statuses = statuses or {}
        self.status_calls: list[str] = []

    def status(self, request_id: str) -> dict:
        self.status_calls.append(request_id)
        return self.statuses.get(request_id) or _not_found(request_id)

    def reconcile(self, *, include_gc: bool = True) -> dict:
        return {"finalized": 3, "watched": 1, "gc_included": include_gc}


# ---------------------------------------------------------------------------
# The measured defect, through the manager repair entry point
# ---------------------------------------------------------------------------


def test_orphan_claim_released_after_grace(store: Path) -> None:
    _insert(store, TASK, claimed_at=_aged(_grace() + 60))

    result = core.reconcile_dead_processing_task(TASK, REQUEST_ID, EPOCH)

    assert result["ok"] is True, result
    assert result["reconciliation"] == "reconciled"
    card = task_store.get_task(store, TASK)
    assert card["status"] == "blocked"
    assert card["dead_process_reconciliation"]["evidence"]["reason"] == "launch_never_started"


def test_manager_repair_refuses_a_young_orphan_claim(store: Path) -> None:
    _insert(store, TASK, claimed_at=_aged(0))
    before = _snapshot(store, TASK)

    result = core.reconcile_dead_processing_task(TASK, REQUEST_ID, EPOCH)

    assert result["ok"] is False
    assert "reconcile_failed:orphan_claim_within_grace" in str(result["stderr"])
    assert _snapshot(store, TASK) == before


# ---------------------------------------------------------------------------
# stale_recovery: the not_found branch
# ---------------------------------------------------------------------------


def test_claim_one_grace_old_is_released_with_launch_never_started_evidence(store: Path) -> None:
    claimed_at = _aged(_grace(), now=NOW)
    _insert(store, TASK, claimed_at=claimed_at)

    assert _reconcile(store, claimed_at=claimed_at) == (True, "reconciled")

    card = task_store.get_task(store, TASK)
    assert card["status"] == "blocked"
    assert card["terminal_substatus"] == "dead_process_reconciled"
    assert not card.get("launch_request_id")
    recorded = card["dead_process_reconciliation"]
    assert recorded["request_id"] == REQUEST_ID
    assert recorded["claim_epoch"] == EPOCH
    assert recorded["evidence"] == {
        "state": "not_found",
        "reason": "launch_never_started",
        "request_id": REQUEST_ID,
        "task_id": TASK,
        "claimed_at": claimed_at,
        "grace_seconds": _grace(),
    }
    assert _reconcile_actors(store, TASK) == ["manager"]


@pytest.mark.parametrize(
    "claimed_at_factory",
    [
        pytest.param(lambda: _aged(0, now=NOW), id="just_claimed"),
        pytest.param(lambda: _aged(_grace() - 1, now=NOW), id="one_second_inside_grace"),
        pytest.param(lambda: _aged(-3600, now=NOW), id="claimed_in_the_future"),
        pytest.param(lambda: None, id="missing"),
        pytest.param(lambda: "", id="empty"),
        pytest.param(lambda: "not-a-timestamp", id="unparseable"),
    ],
)
def test_young_or_undated_orphan_claim_is_refused_unchanged(store: Path, claimed_at_factory) -> None:
    claimed_at = claimed_at_factory()
    _insert(store, TASK, claimed_at=claimed_at)
    before = _snapshot(store, TASK)

    assert _reconcile(store, claimed_at=claimed_at) == (False, "orphan_claim_within_grace")

    assert _snapshot(store, TASK) == before
    assert _reconcile_actors(store, TASK) == []


def test_claim_time_without_a_timezone_proves_no_age(store: Path) -> None:
    claimed_at = (NOW - timedelta(seconds=2 * _grace())).replace(tzinfo=None).isoformat(sep=" ")
    _insert(store, TASK, claimed_at=claimed_at)
    before = _snapshot(store, TASK)

    assert _reconcile(store, claimed_at=claimed_at) == (False, "orphan_claim_within_grace")

    assert _snapshot(store, TASK) == before
    assert _reconcile_actors(store, TASK) == []


def test_grace_is_three_heartbeat_leases_read_at_call_time(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_launcher, "heartbeat_lease_seconds", lambda: 100.0)
    young, old = _aged(299, now=NOW), _aged(300, now=NOW)
    _insert(store, "NF01634_YOUNG", claimed_at=young)
    _insert(store, "NF01634_OLD", claimed_at=old)

    assert _reconcile(store, task_id="NF01634_YOUNG", claimed_at=young) == (
        False, "orphan_claim_within_grace"
    )
    assert _reconcile(store, task_id="NF01634_OLD", claimed_at=old) == (True, "reconciled")
    evidence = task_store.get_task(store, "NF01634_OLD")["dead_process_reconciliation"]["evidence"]
    assert evidence["grace_seconds"] == 300.0


@pytest.mark.parametrize(
    ("status", "refusal"),
    [
        pytest.param({"ok": False}, "process_evidence_missing_or_unauthenticated", id="no_state"),
        pytest.param(
            {"ok": False, "request_id": REQUEST_ID, "state": "unknown"},
            "process_evidence_missing_or_unauthenticated",
            id="unknown_state",
        ),
        pytest.param(
            {"ok": False, "request_id": REQUEST_ID, "state": "Not_Found"},
            "process_evidence_missing_or_unauthenticated",
            id="not_found_is_matched_exactly",
        ),
        pytest.param(_live("other", TASK), "process_request_id_mismatch", id="other_request"),
        pytest.param(_live(REQUEST_ID, TASK), "process_not_proven_dead", id="live_process"),
        pytest.param(
            {**_live(REQUEST_ID, TASK), "process_alive": False},
            "process_not_terminal_blocked",
            id="dead_but_not_terminal",
        ),
    ],
)
def test_any_other_process_status_keeps_its_existing_refusal(
    store: Path, status: dict, refusal: str
) -> None:
    claimed_at = _aged(10 * _grace(), now=NOW)
    _insert(store, TASK, claimed_at=claimed_at)
    before = _snapshot(store, TASK)

    assert _reconcile(store, claimed_at=claimed_at, status=status) == (False, refusal)

    assert _snapshot(store, TASK) == before


@pytest.mark.parametrize(
    ("stored_request_id", "stored_epoch", "refusal"),
    [
        pytest.param(_rid(9), EPOCH, "request_id_mismatch", id="launch_request_id_changed"),
        pytest.param(REQUEST_ID, EPOCH + 1, "claim_epoch_mismatch", id="claim_epoch_changed"),
    ],
)
def test_changed_claim_identity_loses_the_compare_and_swap(
    store: Path, stored_request_id: str, stored_epoch: int, refusal: str
) -> None:
    claimed_at = _aged(_grace() + 60, now=NOW)
    _insert(store, TASK, claimed_at=claimed_at, request_id=stored_request_id, epoch=stored_epoch)
    before = _snapshot(store, TASK)

    assert _reconcile(store, claimed_at=claimed_at) == (False, refusal)

    assert _snapshot(store, TASK) == before


# ---------------------------------------------------------------------------
# task_reconciler: the orphan pass
# ---------------------------------------------------------------------------


def test_pass_releases_only_aged_cards_whose_request_has_no_process_record(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aged = _aged(_grace() + 600)
    _insert(store, "NF01634_ORPHAN", claimed_at=aged, request_id=_rid(1))
    _insert(store, "NF01634_FRESH", claimed_at=_aged(0), request_id=_rid(2))
    _insert(store, "NF01634_LIVE", claimed_at=aged, request_id=_rid(3))
    _insert(store, "NF01634_DEAD", claimed_at=aged, request_id=_rid(4))
    _insert(store, "NF01634_UNATTACHED", claimed_at=aged, request_id="")
    _insert(
        store, "NF01634_PENDING", claimed_at=None, request_id=_rid(5),
        status="pending", worker_status="unclaimed",
    )
    mgr = _Mgr(store, {
        _rid(3): _live(_rid(3), "NF01634_LIVE"),
        _rid(4): _dead(_rid(4), "NF01634_DEAD"),
    })
    untouched = (
        "NF01634_FRESH", "NF01634_LIVE", "NF01634_DEAD", "NF01634_UNATTACHED", "NF01634_PENDING",
    )
    before = {task_id: _snapshot(store, task_id) for task_id in untouched}
    offered: list[str] = []
    real = stale_recovery.reconcile_dead_processing_claim

    def spy(**kwargs):
        offered.append(kwargs["task_id"])
        return real(**kwargs)

    monkeypatch.setattr(stale_recovery, "reconcile_dead_processing_claim", spy)

    result = task_reconciler._scan_orphan_claims(mgr)

    assert result == {"released": ["NF01634_ORPHAN"], "within_grace": ["NF01634_FRESH"]}
    assert sorted(offered) == ["NF01634_FRESH", "NF01634_ORPHAN"]
    assert sorted(mgr.status_calls) == [_rid(1), _rid(2), _rid(3), _rid(4)]
    assert {task_id: _snapshot(store, task_id) for task_id in untouched} == before
    assert task_store.get_task(store, "NF01634_ORPHAN")["status"] == "blocked"
    assert _reconcile_actors(store, "NF01634_ORPHAN") == ["task_reconciler"]


def test_pass_is_idempotent(store: Path) -> None:
    _insert(store, "NF01634_ORPHAN", claimed_at=_aged(_grace() + 600), request_id=_rid(1))
    _insert(store, "NF01634_FRESH", claimed_at=_aged(0), request_id=_rid(2))
    mgr = _Mgr(store)

    first = task_reconciler._scan_orphan_claims(mgr)
    second = task_reconciler._scan_orphan_claims(mgr)

    assert first == {"released": ["NF01634_ORPHAN"], "within_grace": ["NF01634_FRESH"]}
    assert second == {"released": [], "within_grace": ["NF01634_FRESH"]}
    assert _reconcile_actors(store, "NF01634_ORPHAN") == ["task_reconciler"]


@pytest.mark.parametrize(
    "new_request_id",
    [
        pytest.param(_rid(9), id="launch_request_id_changed"),
        pytest.param(REQUEST_ID, id="claim_epoch_changed"),
    ],
)
def test_pass_loses_the_compare_and_swap_to_a_claim_retaken_after_the_scan(
    store: Path, new_request_id: str
) -> None:
    _insert(store, TASK, claimed_at=_aged(_grace() + 600))

    class _RetakingMgr(_Mgr):
        def status(self, request_id: str) -> dict:
            payload = super().status(request_id)
            _retake(store, TASK, request_id=new_request_id)
            return payload

    result = task_reconciler._scan_orphan_claims(_RetakingMgr(store))

    assert result == {"released": [], "within_grace": []}
    card = task_store.get_task(store, TASK)
    assert card["status"] == "processing"
    assert card["claim_epoch"] == EPOCH + 1
    assert card["launch_request_id"] == new_request_id
    assert _reconcile_actors(store, TASK) == []


def test_every_scan_runs_the_orphan_pass(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _insert(store, TASK, claimed_at=_aged(_grace() + 600))
    monkeypatch.setattr(
        task_reconciler.review_orchestrator, "canonical_review_db", lambda _mgr: None
    )
    mgr = _Mgr(store)

    first = task_reconciler.run_scan(mgr, include_gc=False)
    second = task_reconciler.run_scan(mgr, include_gc=False)

    assert first["orphan_claim_release"] == {"released": [TASK], "within_grace": []}
    assert second["orphan_claim_release"] == {"released": [], "within_grace": []}
    assert task_store.get_task(store, TASK)["status"] == "blocked"
    assert _reconcile_actors(store, TASK) == ["task_reconciler"]


def test_failing_orphan_pass_never_aborts_the_scan(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert(store, TASK, claimed_at=_aged(_grace() + 600))
    before = _snapshot(store, TASK)
    monkeypatch.setattr(
        task_reconciler.review_orchestrator, "canonical_review_db", lambda _mgr: None
    )

    class _BrokenMgr(_Mgr):
        def status(self, request_id: str) -> dict:
            raise OSError("process ledger unreadable")

    result = task_reconciler.run_scan(_BrokenMgr(store), include_gc=False)

    assert result["ok"] is True
    assert result["finalized"] == 3
    assert result["orphan_claim_release"] == {"ok": False, "state": "skipped", "reason": "OSError"}
    assert "transient_retry_relaunch" in result and "sdlc_sync" in result
    assert _snapshot(store, TASK) == before
