"""NF-2026-01192: a launch that dies in preflight stays recoverable.

Measured on 0.12.13/0.12.14.  A launch rejected BEFORE any worker process
exists has nothing to attach a launch request to yet, and that single missing
linkage produced two dead ends on the same card:

* VARIANT 1 -- the claim already existed but carried no ``launch_request_id``,
  so ``task_store.mark_launch_failed`` refused its own terminal transition with
  ``launch_request_mismatch`` and the card stayed ``processing`` with a dead
  launch.  ``reconcile_dead_processing_claim`` then refused the same card with
  ``request_id_mismatch``.
* VARIANT 2 -- the blocker was recorded without a request id at all, so the
  resulting ``blocked``/``launch_failed`` card (``launch_request_id`` empty, no
  ``terminal_failure``) had nothing for ``retry-terminal`` to authenticate
  against and was refused ``terminal_retry_request_mismatch:expected=:got=<id>``
  forever.

Every test here drives the real transition functions against a real temporary
task store.  Nothing launches a process, spawns node, constructs a
ToolchainAuthority or touches the network: a preflight failure is exactly the
case where no process was ever created, so the reproduction needs none.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import core, task_engine, task_store

RUNNER = "worker_runner"
TOPIC = "terminal_retry"
ADAPTER = "claude-code"
ACTOR = "codex"

# Distinct, non-empty request ids.  "attached" is what a card already carries,
# "supplied" is what the failing launcher/manager names.
SUPPLIED = "a" * 32
ATTACHED = "b" * 32
STALE = "c" * 32
EVENT_ONLY = "d" * 32


def _write_coordinator_token(path: Path, token: str) -> None:
    """Create the coordinator token file already at 0600.

    ``_verify_coordinator_capability`` enforces 0600 on POSIX.  The mode is
    passed to ``os.open`` rather than applied afterwards because ``chmod`` is
    denied in the validation sandbox, and a fixture that cannot run proves
    nothing about the code under test.
    """

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
    return repo


def _db(repo: Path) -> Path:
    return Path(task_store.storage_readiness(repo).canonical_db)


def _connect(repo: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_db(repo))
    conn.row_factory = sqlite3.Row
    return conn


def _insert(
    repo: Path,
    task_id: str,
    *,
    status: str,
    worker_status: str,
    claimed_by: str = "",
    extra: dict | None = None,
    created_at: str = "2026-09-30T00:00:00+00:00",
) -> None:
    """Insert one exact card in the lifecycle state a field report measured."""

    card: dict = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "objective": "recover a launch that never started a worker",
        "status": status,
        "worker_status": worker_status,
        "claimed_by": claimed_by,
        "claim_epoch": 1,
    }
    card.update(extra or {})
    conn = _connect(repo)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by,claimed_at,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                RUNNER,
                TOPIC,
                "solo",
                status,
                worker_status,
                "normal",
                card["objective"],
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                created_at,
                created_at,
                claimed_by or None,
                created_at if claimed_by else None,
                created_at if claimed_by else None,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_event(
    repo: Path, task_id: str, *, event: str, payload: dict, created_at: str
) -> None:
    conn = _connect(repo)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, event, RUNNER, json.dumps(payload, sort_keys=True), created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _raw_card_json(repo: Path, task_id: str) -> str:
    conn = _connect(repo)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row["card_json"])


def _card(repo: Path, task_id: str) -> dict:
    """The persisted semantic card, with no read-projection overlay."""

    decoded = json.loads(_raw_card_json(repo, task_id))
    assert isinstance(decoded, dict)
    return decoded


def _lifecycle(repo: Path, task_id: str) -> tuple[str, str]:
    conn = _connect(repo)
    try:
        row = conn.execute(
            "SELECT status, worker_status FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row["status"] or ""), str(row["worker_status"] or "")


def _events(repo: Path, task_id: str) -> list[tuple[str, dict]]:
    conn = _connect(repo)
    try:
        rows = conn.execute(
            "SELECT event, payload_json FROM task_events WHERE task_id=? ORDER BY event_id",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    decoded: list[tuple[str, dict]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        decoded.append((str(row["event"]), payload if isinstance(payload, dict) else {}))
    return decoded


def _task_count(repo: Path) -> int:
    conn = _connect(repo)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    finally:
        conn.close()


def _legacy_blocked_card(blocked_at: str) -> dict:
    """The exact 0.12.13/0.12.14 field shape: blocked with no request linkage.

    ``launch_request_id`` is the empty string the field card actually carried --
    not a missing key -- and there is no ``terminal_failure`` projection at all,
    so neither source ``retry-terminal`` reads today names a request.
    """

    return {
        "launch_request_id": "",
        "terminal_substatus": "launch_failed",
        "launch_failed": True,
        "blocker_reason": "launch_failed:preflight refused",
        "launch_error": "launch_failed:preflight refused",
        "blocked_at": blocked_at,
        "blocked_by": RUNNER,
    }


# ---------------------------------------------------------------------------
# VARIANT 1 -- the stuck processing/claimed card
# ---------------------------------------------------------------------------


def test_mark_launch_failed_attaches_a_supplied_request_id_to_an_unattached_claim(
    store: Path,
) -> None:
    _insert(
        store,
        "NF01192_V1_ATTACH",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        extra={"launch_request_id": ""},
    )

    ok, state = task_store.mark_launch_failed(
        store,
        "NF01192_V1_ATTACH",
        runner=RUNNER,
        reason="launch_preflight_refused",
        request_id=SUPPLIED,
    )

    assert (ok, state) == (True, "blocked")
    assert _lifecycle(store, "NF01192_V1_ATTACH") == ("blocked", "launch_failed")
    card = _card(store, "NF01192_V1_ATTACH")
    assert card["terminal_substatus"] == "launch_failed"
    # The transition that terminalises the claim is the only place this linkage
    # can still be recorded: recovery has nothing to authenticate without it.
    assert card["launch_request_id"] == SUPPLIED
    named = [
        payload for event, payload in _events(store, "NF01192_V1_ATTACH")
        if event == "launch_failed"
    ]
    assert named and named[-1]["request_id"] == SUPPLIED


def test_mark_launch_failed_still_refuses_a_contradicting_or_missing_request_id(
    store: Path,
) -> None:
    _insert(
        store,
        "NF01192_V1_REFUSE",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        extra={"launch_request_id": ATTACHED},
    )
    before = _raw_card_json(store, "NF01192_V1_REFUSE")

    # A DIFFERENT non-empty attached id is a contradiction, never a card to
    # adopt: the losing launcher must not terminalise the winner's claim.
    assert task_store.mark_launch_failed(
        store,
        "NF01192_V1_REFUSE",
        runner=RUNNER,
        reason="launch_preflight_refused",
        request_id=SUPPLIED,
    ) == (False, "launch_request_mismatch")
    assert _raw_card_json(store, "NF01192_V1_REFUSE") == before

    assert task_store.mark_launch_failed(
        store,
        "NF01192_V1_REFUSE",
        runner=RUNNER,
        reason="launch_preflight_refused",
        request_id="",
    ) == (False, "launch_request_id_required")
    assert _raw_card_json(store, "NF01192_V1_REFUSE") == before
    assert _lifecycle(store, "NF01192_V1_REFUSE") == ("processing", "claimed")

    # The unattached claim is the one case that now resolves, and it resolves
    # to the supplied id rather than to a refusal.
    _insert(
        store,
        "NF01192_V1_UNATTACHED",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        extra={"launch_request_id": ""},
    )
    assert task_store.mark_launch_failed(
        store,
        "NF01192_V1_UNATTACHED",
        runner=RUNNER,
        reason="launch_preflight_refused",
        request_id=SUPPLIED,
    ) == (True, "blocked")


# ---------------------------------------------------------------------------
# VARIANT 2 -- the blocker recorded with no request linkage
# ---------------------------------------------------------------------------


def test_record_launch_blocker_names_the_request_id_on_a_pending_card(
    store: Path,
) -> None:
    _insert(store, "NF01192_V2_PENDING", status="pending", worker_status="unclaimed")

    result = task_engine.record_launch_blocker(
        store,
        "NF01192_V2_PENDING",
        RUNNER,
        TOPIC,
        adapter_id=ADAPTER,
        reason="launch_preflight_refused",
        request_id=SUPPLIED,
    )

    assert result["ok"] is True, result
    blocker = _card(store, "NF01192_V2_PENDING")["operational_blocker"]
    assert blocker["kind"] == "launch_blocked"
    assert blocker["request_id"] == SUPPLIED
    named = [
        payload for event, payload in _events(store, "NF01192_V2_PENDING")
        if event == "launch_blocked"
    ]
    assert named and named[-1]["request_id"] == SUPPLIED
    # A pre-claim blocker still never fabricates a claim.
    assert _lifecycle(store, "NF01192_V2_PENDING") == ("pending", "unclaimed")


def test_record_launch_blocker_terminalises_an_owned_claim_with_no_attached_id(
    store: Path,
) -> None:
    _insert(
        store,
        "NF01192_V2_OWNED",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        extra={"launch_request_id": ""},
    )

    result = task_engine.record_launch_blocker(
        store,
        "NF01192_V2_OWNED",
        RUNNER,
        TOPIC,
        adapter_id=ADAPTER,
        reason="launch_preflight_refused",
        request_id=SUPPLIED,
    )

    assert result["ok"] is True, result
    assert _lifecycle(store, "NF01192_V2_OWNED") == ("blocked", "launch_failed")
    assert _card(store, "NF01192_V2_OWNED")["launch_request_id"] == SUPPLIED


# ---------------------------------------------------------------------------
# Retry-terminal: the legacy card nothing could retry
# ---------------------------------------------------------------------------


def test_retry_terminal_recovers_a_legacy_unlinked_launch_failure(store: Path) -> None:
    blocked_at = "2026-09-30T12:00:00+00:00"
    _insert(
        store,
        "NF01192_LEGACY",
        status="blocked",
        worker_status="launch_failed",
        claimed_by=RUNNER,
        extra=_legacy_blocked_card(blocked_at),
    )
    # A PREVIOUS episode's launch_failed event does name a request.  It must not
    # authenticate this episode: linkage is per blocked episode, and borrowing an
    # older one would let any historical id unlock the current card.
    _insert_event(
        store,
        "NF01192_LEGACY",
        event="launch_failed",
        payload={"request_id": STALE, "recorded_at": "2026-09-01T00:00:00+00:00"},
        created_at="2026-09-01T00:00:00+00:00",
    )
    _insert_event(
        store,
        "NF01192_LEGACY",
        event="launch_failed",
        payload={"reason": "launch_preflight_refused", "recorded_at": blocked_at},
        created_at=blocked_at,
    )

    result = core.retry_terminal_task(
        "NF01192_LEGACY", SUPPLIED, "launch_failed", "route repaired", TOPIC
    )

    assert result["ok"] is True, result
    assert _lifecycle(store, "NF01192_LEGACY") == ("pending", "unclaimed")
    assert _task_count(store) == 1
    retry = _card(store, "NF01192_LEGACY")["terminal_retry"]
    assert retry["request_id"] == SUPPLIED
    assert retry["terminal_substatus"] == "launch_failed"
    # The accepted linkage is recorded as exactly what it was: nothing on the
    # card or in its current episode named a request.
    assert retry["request_linkage"] == "legacy_unlinked"

    # An empty supplied id is still not an identity.
    refused = core.retry_terminal_task("NF01192_LEGACY", "", "launch_failed", "", TOPIC)
    assert refused["ok"] is False
    assert "terminal_retry_request_id_invalid" in str(refused["stderr"])


def test_retry_terminal_authenticates_against_the_current_episode_event(
    store: Path,
) -> None:
    blocked_at = "2026-09-30T13:00:00+00:00"
    _insert(
        store,
        "NF01192_EVENT_LINKED",
        status="blocked",
        worker_status="launch_failed",
        claimed_by=RUNNER,
        extra=_legacy_blocked_card(blocked_at),
    )
    # This episode's own terminal event DOES name a request, so that id is the
    # authority and a different supplied id stays refused.
    _insert_event(
        store,
        "NF01192_EVENT_LINKED",
        event="launch_failed",
        payload={"request_id": EVENT_ONLY, "recorded_at": blocked_at},
        created_at=blocked_at,
    )

    refused = core.retry_terminal_task(
        "NF01192_EVENT_LINKED", SUPPLIED, "launch_failed", "route repaired", TOPIC
    )
    assert refused["ok"] is False
    assert (
        f"terminal_retry_request_mismatch:expected={EVENT_ONLY}:got={SUPPLIED}"
        in str(refused["stderr"])
    )
    assert _lifecycle(store, "NF01192_EVENT_LINKED") == ("blocked", "launch_failed")

    accepted = core.retry_terminal_task(
        "NF01192_EVENT_LINKED", EVENT_ONLY, "launch_failed", "route repaired", TOPIC
    )
    assert accepted["ok"] is True, accepted
    retry = _card(store, "NF01192_EVENT_LINKED")["terminal_retry"]
    assert retry["request_id"] == EVENT_ONLY
    # An authenticated episode is not a legacy unlinked one.
    assert "request_linkage" not in retry


def test_retry_terminal_is_unchanged_for_a_card_carrying_its_own_request_id(
    store: Path,
) -> None:
    blocked_at = "2026-09-30T14:00:00+00:00"
    extra = _legacy_blocked_card(blocked_at)
    extra["launch_request_id"] = ATTACHED
    _insert(
        store,
        "NF01192_CARDED",
        status="blocked",
        worker_status="launch_failed",
        claimed_by=RUNNER,
        extra=extra,
    )
    # A launch_failed event naming a different request must not widen what this
    # card accepts: the card's own id still decides.
    _insert_event(
        store,
        "NF01192_CARDED",
        event="launch_failed",
        payload={"request_id": EVENT_ONLY, "recorded_at": blocked_at},
        created_at=blocked_at,
    )

    refused = core.retry_terminal_task(
        "NF01192_CARDED", EVENT_ONLY, "launch_failed", "route repaired", TOPIC
    )
    assert refused["ok"] is False
    assert (
        f"terminal_retry_request_mismatch:expected={ATTACHED}:got={EVENT_ONLY}"
        in str(refused["stderr"])
    )

    accepted = core.retry_terminal_task(
        "NF01192_CARDED", ATTACHED, "launch_failed", "route repaired", TOPIC
    )
    assert accepted["ok"] is True, accepted
    retry = _card(store, "NF01192_CARDED")["terminal_retry"]
    assert retry["request_id"] == ATTACHED
    assert "request_linkage" not in retry


# ---------------------------------------------------------------------------
# Reconcile: the dead processing claim with no attached request
# ---------------------------------------------------------------------------


def test_reconcile_dead_processing_claim_accepts_an_unattached_claim(
    store: Path,
) -> None:
    _insert(
        store,
        "NF01192_RECONCILE",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        extra={"launch_request_id": ""},
    )
    evidence = {"state": "process_lost", "error": "supervisor pid never existed"}

    # The claim epoch is still the whole identity check for an unattached claim.
    assert task_store.reconcile_dead_processing_claim(
        store,
        "NF01192_RECONCILE",
        request_id=SUPPLIED,
        claim_epoch=9,
        terminal_evidence=evidence,
        actor=ACTOR,
    ) == (False, "claim_epoch_mismatch")

    assert task_store.reconcile_dead_processing_claim(
        store,
        "NF01192_RECONCILE",
        request_id=SUPPLIED,
        claim_epoch=1,
        terminal_evidence=evidence,
        actor=ACTOR,
    ) == (True, "reconciled")
    recorded = _card(store, "NF01192_RECONCILE")["dead_process_reconciliation"]
    assert recorded["request_id"] == SUPPLIED
    assert recorded["claim_epoch"] == 1

    assert task_store.reconcile_dead_processing_claim(
        store,
        "NF01192_RECONCILE",
        request_id=SUPPLIED,
        claim_epoch=1,
        terminal_evidence=evidence,
        actor=ACTOR,
    ) == (True, "already_reconciled")


def test_reconcile_dead_processing_claim_still_refuses_a_contradicting_id(
    store: Path,
) -> None:
    _insert(
        store,
        "NF01192_RECONCILE_REFUSE",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        extra={"launch_request_id": ATTACHED},
    )
    before = _raw_card_json(store, "NF01192_RECONCILE_REFUSE")

    assert task_store.reconcile_dead_processing_claim(
        store,
        "NF01192_RECONCILE_REFUSE",
        request_id=SUPPLIED,
        claim_epoch=1,
        terminal_evidence={"state": "process_lost"},
        actor=ACTOR,
    ) == (False, "request_id_mismatch")
    assert _raw_card_json(store, "NF01192_RECONCILE_REFUSE") == before

    # The same call against an unattached claim is the case that now resolves.
    _insert(
        store,
        "NF01192_RECONCILE_OPEN",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        extra={"launch_request_id": ""},
    )
    assert task_store.reconcile_dead_processing_claim(
        store,
        "NF01192_RECONCILE_OPEN",
        request_id=SUPPLIED,
        claim_epoch=1,
        terminal_evidence={"state": "process_lost"},
        actor=ACTOR,
    ) == (True, "reconciled")


# ---------------------------------------------------------------------------
# A new claim never inherits a previous episode's request id
# ---------------------------------------------------------------------------


def test_claim_start_exact_never_inherits_a_previous_episode_request_id(
    store: Path,
) -> None:
    """Both claim implementations, pinned against the same stale card.

    ``task_engine.claim_start_exact`` is what auto-pickup and the launcher call;
    ``core.claim_start_exact`` is the CLI claim-start route.  A card requeued
    while still carrying an old ``launch_request_id`` must not hand that id to
    the next episode through EITHER door, or the next launch's own request is
    refused by every check that compares the two.
    """

    _insert(
        store,
        "NF01192_CLAIM_ENGINE",
        status="pending",
        worker_status="unclaimed",
        extra={"launch_request_id": STALE},
    )
    engine_claim = task_engine.claim_start_exact(
        store, "NF01192_CLAIM_ENGINE", RUNNER, TOPIC, request_id=SUPPLIED
    )
    assert engine_claim["ok"] is True, engine_claim
    assert _card(store, "NF01192_CLAIM_ENGINE")["launch_request_id"] == SUPPLIED

    _insert(
        store,
        "NF01192_CLAIM_CORE",
        status="pending",
        worker_status="unclaimed",
        extra={"launch_request_id": STALE},
    )
    core_claim = core.claim_start_exact(
        "NF01192_CLAIM_CORE", RUNNER, TOPIC, request_id=SUPPLIED
    )
    assert core_claim["ok"] is True, core_claim
    assert _card(store, "NF01192_CLAIM_CORE")["launch_request_id"] == SUPPLIED

    # An auto-pickup claim names no request, so it must leave none behind.
    _insert(
        store,
        "NF01192_CLAIM_PICKUP",
        status="pending",
        worker_status="unclaimed",
        extra={"launch_request_id": STALE},
    )
    pickup = task_engine.claim_start_exact(
        store, "NF01192_CLAIM_PICKUP", RUNNER, TOPIC
    )
    assert pickup["ok"] is True, pickup
    assert _card(store, "NF01192_CLAIM_PICKUP").get("launch_request_id", "") == ""


def test_recovered_legacy_card_is_launchable_again_on_the_same_task_id(
    store: Path,
) -> None:
    blocked_at = "2026-09-30T15:00:00+00:00"
    _insert(
        store,
        "NF01192_RELAUNCH",
        status="blocked",
        worker_status="launch_failed",
        claimed_by=RUNNER,
        extra=_legacy_blocked_card(blocked_at),
    )
    _insert_event(
        store,
        "NF01192_RELAUNCH",
        event="launch_failed",
        payload={"reason": "launch_preflight_refused", "recorded_at": blocked_at},
        created_at=blocked_at,
    )
    history_before = _events(store, "NF01192_RELAUNCH")

    recovered = core.retry_terminal_task(
        "NF01192_RELAUNCH", SUPPLIED, "launch_failed", "route repaired", TOPIC
    )
    assert recovered["ok"] is True, recovered

    relaunch = task_engine.claim_start_exact(
        store, "NF01192_RELAUNCH", RUNNER, TOPIC, request_id=ATTACHED
    )
    assert relaunch["ok"] is True, relaunch
    assert _lifecycle(store, "NF01192_RELAUNCH") == ("processing", "claimed")
    card = _card(store, "NF01192_RELAUNCH")
    assert card["launch_request_id"] == ATTACHED
    assert card["claim_epoch"] == 2

    # One card, same task id, and the failed episode's history is append-only.
    assert _task_count(store) == 1
    history_after = _events(store, "NF01192_RELAUNCH")
    assert history_after[: len(history_before)] == history_before
    assert [event for event, _ in history_after].count("launch_failed") == 1
