"""Automatic reroute off an open quota/auth route circuit (NF-2026-01674).

A pending card bound to a route whose provider refused for balance/quota or
auth sat until a manager called ``aiworkhub_task_reroute_launch_identity`` by
hand.  The dependency reconciler now moves it through
``core.reroute_launch_identity`` -- every gate of a manual reroute applies --
at most once per circuit episode, and records ``no_capable_route`` once when
nothing capable can take it.

Route evidence is injected where production reads it
(``task_store.route_terminal_observations``), so derivation, the circuit, the
ranking, the reroute gates and the reconciler all run for real.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    core,
    dependency_autolaunch,
    process_launcher,
    storage_retention,
    task_store,
    workforce_catalog,
    workforce_router,
)


PARENT = "QUOTA_PARENT"
CHILD = "QUOTA_CHILD"
TOPIC = "quota_reroute"
FROM_RUNNER = "claude_opus-5"
FROM_ADAPTER = "claude_cli"
FROM_MODEL = str(process_launcher._CANONICAL_WORKFORCE[(FROM_RUNNER, FROM_ADAPTER)]["model"])
TO_RUNNER = "codex_gpt-5.5"
TO_ADAPTER = "codex_cli"
TO_MODEL = str(process_launcher._CANONICAL_WORKFORCE[(TO_RUNNER, TO_ADAPTER)]["model"])
SONNET_RUNNER = "claude_sonnet-5"
SONNET_MODEL = str(
    process_launcher._CANONICAL_WORKFORCE[(SONNET_RUNNER, FROM_ADAPTER)]["model"]
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token = tmp_path / "coordinator.token"
    token.write_text("coordinator-token\n", encoding="utf-8")
    if hasattr(os, "geteuid"):
        # The POSIX gate requires an owner-only token file; on Windows the
        # file's ACL is the boundary and mode bits are not meaningful.
        os.chmod(token, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coordinator-token")
    # The post-commit janitor hint is not under test; keep it off tmp_path.
    monkeypatch.setattr(
        storage_retention, "schedule_repository_cleanup", lambda _root: False
    )
    return root


def _db(root: Path) -> Path:
    return Path(task_store.storage_readiness(root).canonical_db)


def _insert(root: Path, card: dict, status: str, worker: str) -> None:
    now = "2026-10-26T00:00:00+00:00"
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, card_json, "
            "created_at, updated_at, origin_thread_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                card["task_id"],
                card["runner"],
                card["topic"],
                status,
                worker,
                json.dumps(card, sort_keys=True),
                now,
                now,
                "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed(root: Path, *, task_type: str = "code") -> None:
    parent = {
        "task_id": PARENT,
        "runner": "codex",
        "topic": TOPIC,
        "status": "finished",
        "worker_status": "done",
    }
    _insert(root, parent, "finished", "done")
    child = {
        "schema_id": "aiworkhub.task_card.v1",
        "task_id": CHILD,
        "runner": FROM_RUNNER,
        "topic": TOPIC,
        "status": "pending",
        "worker_status": "unclaimed",
        "depends_on": [PARENT],
        "risk_tier": "medium",
        "allowed_writes": ["src/app.py"],
        "project_context": {
            "required": True,
            "task_type": task_type,
            "source_graph": {"required": True},
        },
    }
    _insert(root, child, "pending", "unclaimed")


def _sealed(code: str, *, ago: timedelta, reset_in: timedelta | None = None) -> dict:
    """One provider-owned, transport-sealed terminal refusal on a route."""
    now = datetime.now(timezone.utc)
    error: dict = {"owner": "provider", "sealed": True, "code": code}
    if reset_in is not None:
        error["reset_at"] = (now + reset_in).replace(microsecond=0).isoformat()
    return {
        "state": "worker_failed",
        "finished_at": (now - ago).isoformat(),
        "provider_error": error,
    }


def _unsealed(state: str, *, ago: timedelta) -> dict:
    return {"state": state, "finished_at": (datetime.now(timezone.utc) - ago).isoformat()}


def _route_evidence(monkeypatch: pytest.MonkeyPatch, rows_by_route: dict) -> None:
    def observations(_root, *, adapter_id, model, since_iso):
        return [dict(row) for row in rows_by_route.get((adapter_id, model), ())]

    monkeypatch.setattr(task_store, "route_terminal_observations", observations)


def _row(
    worker_id: str, adapter_id: str, model: str, provider: str, *, available: bool = True
) -> dict:
    return {
        "worker_id": worker_id,
        "adapter_id": adapter_id,
        "effective_adapter_id": adapter_id,
        "execution_runner": workforce_catalog.execution_runner(worker_id, adapter_id),
        "model": model,
        "provider": provider,
        "supports": ["code", "review"],
        "tools": [],
        "max_context_tokens": 400_000,
        "max_risk": "critical",
        "quality_ceiling": 1.0,
        "enabled": True,
        "available": available,
        "launch_eligible": available,
        "implementation_worker": True,
        "availability_reason": "" if available else "route_circuit_open",
        "outcomes": {},
    }


TRIPPED_ROW = _row("claude-opus-5", FROM_ADAPTER, FROM_MODEL, "anthropic", available=False)
CODEX_ROW = _row("gpt-5.5", TO_ADAPTER, TO_MODEL, "openai")
SONNET_ROW = _row("claude-sonnet-5", FROM_ADAPTER, SONNET_MODEL, "anthropic")


def _catalog(monkeypatch: pytest.MonkeyPatch, *rows: dict) -> dict:
    snapshot = {"schema_id": workforce_catalog.SCHEMA_ID, "workers": [dict(row) for row in rows]}
    monkeypatch.setattr(workforce_catalog, "build_catalog", lambda *_a, **_k: snapshot)
    monkeypatch.setattr(workforce_catalog, "build_routing_catalog", lambda *_a, **_k: snapshot)
    return snapshot


def _launcher(root: Path, calls: list, *, claim: bool = True):
    """A stand-in for ``claim_start_exact``: claims only the card's exact runner."""

    def launch(task_id: str, runner: str, topic: str, request_id: str) -> dict:
        calls.append((task_id, runner))
        if not claim:
            return {"ok": False, "stderr": "route_failure_circuit_open:runner=" + runner}
        conn = sqlite3.connect(_db(root))
        try:
            cur = conn.execute(
                "UPDATE tasks SET status='processing', worker_status='claimed', claimed_by=? "
                "WHERE task_id=? AND runner=? AND topic=? "
                "AND status='pending' AND worker_status='unclaimed'",
                (runner, task_id, runner, topic),
            )
            conn.commit()
        finally:
            conn.close()
        claimed = cur.rowcount == 1
        return {"ok": claimed, "stderr": "" if claimed else "identity_mismatch"}

    return launch


def _card(root: Path) -> dict:
    card = task_store.get_task(root, CHILD)
    assert card is not None
    return card


def _updated_at(root: Path) -> str:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT updated_at FROM tasks WHERE task_id=?", (CHILD,)).fetchone()
    finally:
        conn.close()
    return str(row[0])


def _events(root: Path, event: str) -> list[dict]:
    conn = sqlite3.connect(_db(root))
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event=? ORDER BY rowid",
            (CHILD, event),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def _entry(outcome: dict) -> dict:
    [entry] = [row for row in outcome["delayed"] if row["task_id"] == CHILD]
    return entry


def _within_a_second(left: str, right: str) -> bool:
    delta = datetime.fromisoformat(left) - datetime.fromisoformat(right)
    return abs(delta.total_seconds()) <= 1.0


@pytest.mark.parametrize(
    ("code", "kind", "reset_in"),
    [
        ("insufficient_balance", "quota", timedelta(days=5)),
        ("invalid_api_key", "auth", None),
    ],
    ids=["quota_with_named_reset", "auth"],
)
def test_open_quota_or_auth_circuit_reroutes_to_the_ranked_alternative_and_launches(
    repo, monkeypatch, code, kind, reset_in
):
    refusal = _sealed(code, ago=timedelta(minutes=1), reset_in=reset_in)
    reset_at = refusal["provider_error"].get("reset_at", "")
    _route_evidence(monkeypatch, {(FROM_ADAPTER, FROM_MODEL): [refusal]})
    _catalog(monkeypatch, TRIPPED_ROW, CODEX_ROW)
    _seed(repo)
    calls: list = []

    outcome = dependency_autolaunch.reconcile(
        repo, trigger_task_id=PARENT, launch=_launcher(repo, calls)
    )

    # The normal launch path ran exactly once, on the new runner.
    assert calls == [(CHILD, TO_RUNNER)]
    assert outcome["launched"] == [{"task_id": CHILD, "runner": TO_RUNNER, "topic": TOPIC}]
    [receipt] = outcome["rerouted"]
    assert receipt["authority"] == "quota_circuit"
    assert (receipt["from_runner"], receipt["from_adapter_id"], receipt["from_model"]) == (
        FROM_RUNNER, FROM_ADAPTER, FROM_MODEL,
    )
    assert (receipt["to_runner"], receipt["to_adapter_id"], receipt["to_model"]) == (
        TO_RUNNER, TO_ADAPTER, TO_MODEL,
    )
    assert receipt["failure_kind"] == kind
    assert receipt["reset_at"] == reset_at

    card = _card(repo)
    assert card["runner"] == TO_RUNNER
    authorization = card["identity_reroute"]["operational_provider_authorization"]
    assert authorization["schema_id"] == "aiworkhub.operational_provider_reroute.v1"
    assert authorization["authority"] == "quota_circuit"
    assert authorization["failure_kind"] == kind
    assert authorization["reset_at"] == reset_at
    assert (authorization["from_adapter_id"], authorization["from_model"]) == (
        FROM_ADAPTER, FROM_MODEL,
    )
    assert (
        authorization["to_runner"], authorization["to_adapter_id"], authorization["to_model"]
    ) == (TO_RUNNER, TO_ADAPTER, TO_MODEL)
    assert authorization["circuit"]["state"] == "open"
    assert authorization["reason"].startswith("operational_provider_reroute:quota_circuit:")
    assert f"failure_kind={kind}" in authorization["reason"]

    record = card["quota_circuit_reroute"]
    assert record["schema_id"] == workforce_catalog.QUOTA_CIRCUIT_REROUTE_SCHEMA_ID
    assert (record["from_runner"], record["to_runner"]) == (FROM_RUNNER, TO_RUNNER)
    if reset_at:
        # A provider-named reset bounds the episode, not the 600 s cooldown.
        assert _within_a_second(record["open_until"], reset_at)
    else:
        opened = datetime.fromisoformat(record["opened_at"])
        until = datetime.fromisoformat(record["open_until"])
        span = (until - opened).total_seconds()
        assert abs(span - workforce_catalog.ROUTE_CIRCUIT_COOLDOWN_SECONDS) <= 1.0

    [event] = _events(repo, "reroute_launch_identity")
    assert event["from_runner"] == FROM_RUNNER
    assert event["to_runner"] == TO_RUNNER
    assert event["operational_provider_authorization"]["authority"] == "quota_circuit"
    assert event["operational_provider_authorization"]["circuit"]["failure_kind"] == kind


def test_the_alternative_is_the_top_of_the_managers_own_ranking(repo, monkeypatch):
    refusal = _sealed("insufficient_balance", ago=timedelta(minutes=1), reset_in=timedelta(days=5))
    _route_evidence(monkeypatch, {(FROM_ADAPTER, FROM_MODEL): [refusal]})
    snapshot = _catalog(monkeypatch, TRIPPED_ROW, CODEX_ROW, SONNET_ROW)
    _seed(repo)
    task = workforce_router.TaskRequirements.build(
        task_id=CHILD,
        repo_id=task_store.storage_readiness(repo).repo_id or "unknown",
        kinds=("code",),
        risk="medium",
    )
    expected = workforce_catalog.rank_task(repo, task, catalog=snapshot)["launch_contract"]
    assert expected["runner"] in {TO_RUNNER, SONNET_RUNNER}
    calls: list = []

    outcome = dependency_autolaunch.reconcile(
        repo, trigger_task_id=PARENT, launch=_launcher(repo, calls)
    )

    [receipt] = outcome["rerouted"]
    assert (receipt["to_runner"], receipt["to_adapter_id"], receipt["to_model"]) == (
        expected["runner"], expected["adapter_id"], expected["model"],
    )
    assert calls == [(CHILD, expected["runner"])]
    assert _card(repo)["runner"] == expected["runner"]


def test_a_route_core_refuses_falls_through_to_the_next_ranked_route(repo, monkeypatch):
    refusal = _sealed("insufficient_balance", ago=timedelta(minutes=1), reset_in=timedelta(days=5))
    _route_evidence(monkeypatch, {(FROM_ADAPTER, FROM_MODEL): [refusal]})
    snapshot = _catalog(monkeypatch, TRIPPED_ROW, CODEX_ROW, SONNET_ROW)
    _seed(repo)
    task = workforce_router.TaskRequirements.build(
        task_id=CHILD,
        repo_id=task_store.storage_readiness(repo).repo_id or "unknown",
        kinds=("code",),
        risk="medium",
    )
    top = workforce_catalog.rank_task(repo, task, catalog=snapshot)["launch_contract"]["runner"]
    real_reroute = core.reroute_launch_identity
    refused: list = []

    def reroute(task_id, **kwargs):
        # The ranking and core's target gate read different catalogs; here they disagree.
        if kwargs["to_runner"] == top:
            refused.append(top)
            return {"ok": False, "stderr": "reroute_target_not_launch_eligible"}
        return real_reroute(task_id, **kwargs)

    monkeypatch.setattr(core, "reroute_launch_identity", reroute)
    calls: list = []

    outcome = dependency_autolaunch.reconcile(
        repo, trigger_task_id=PARENT, launch=_launcher(repo, calls)
    )

    assert refused == [top]
    [receipt] = outcome["rerouted"]
    assert receipt["to_runner"] in {TO_RUNNER, SONNET_RUNNER} - {top}
    assert calls == [(CHILD, receipt["to_runner"])]
    assert _card(repo)["runner"] == receipt["to_runner"]


@pytest.mark.parametrize(
    "rows",
    [
        # Factories, so every age is measured from when the test runs.
        lambda: [
            _unsealed("launch_failed", ago=timedelta(seconds=90)),
            _unsealed("launch_failed", ago=timedelta(seconds=30)),
        ],
        lambda: [
            _unsealed("timed_out", ago=timedelta(seconds=90)),
            _unsealed("timed_out", ago=timedelta(seconds=30)),
        ],
        lambda: [_sealed("model_not_found", ago=timedelta(minutes=1))],
        # Past the 600 s cooldown with no named reset: tripped, but half_open.
        lambda: [_sealed("insufficient_balance", ago=timedelta(seconds=700))],
        # The provider answered; a downstream gate failed.  Never a route failure.
        lambda: [
            _unsealed("validation_failed", ago=timedelta(seconds=90)),
            _unsealed("finalize_failed", ago=timedelta(seconds=30)),
        ],
    ],
    ids=[
        "transient",
        "network_timeout",
        "model_not_found",
        "quota_half_open",
        "validation_or_finalization_failure",
    ],
)
def test_transient_network_model_or_half_open_circuits_never_reroute(repo, monkeypatch, rows):
    _route_evidence(monkeypatch, {(FROM_ADAPTER, FROM_MODEL): rows()})
    _catalog(monkeypatch, TRIPPED_ROW, CODEX_ROW)
    _seed(repo)
    calls: list = []

    outcome = dependency_autolaunch.reconcile(
        repo, trigger_task_id=PARENT, launch=_launcher(repo, calls, claim=False)
    )

    assert outcome["rerouted"] == []
    # The ordinary launch path still ran, unchanged, on the card's own runner.
    assert calls == [(CHILD, FROM_RUNNER)]
    assert _entry(outcome)["reason"] == "launch_not_claimed"
    card = _card(repo)
    assert card["runner"] == FROM_RUNNER
    assert "identity_reroute" not in card
    assert "quota_circuit_reroute" not in card
    assert _events(repo, "reroute_launch_identity") == []


@pytest.mark.parametrize(
    ("other_routes", "rows", "task_type", "detail"),
    [
        (dict, (TRIPPED_ROW,), "code", "no_compatible_available_worker"),
        # The catalog still lists the alternative as available, but the launch
        # gate's own evidence shows its circuit open too.
        (
            lambda: {
                (TO_ADAPTER, TO_MODEL): [_sealed("insufficient_quota", ago=timedelta(minutes=2))]
            },
            (TRIPPED_ROW, CODEX_ROW),
            "code",
            "no_compatible_available_worker",
        ),
        # A task type the ranking has no kind for is never guessed onto a route.
        (dict, (TRIPPED_ROW, CODEX_ROW), "data_classification", "task_type_not_rankable"),
    ],
    ids=["no_alternative", "alternative_circuit_open", "task_type_not_rankable"],
)
def test_no_capable_route_keeps_the_card_pending_and_records_the_blocker_once(
    repo, monkeypatch, other_routes, rows, task_type, detail
):
    refusal = _sealed("insufficient_balance", ago=timedelta(minutes=1), reset_in=timedelta(days=5))
    _route_evidence(monkeypatch, {(FROM_ADAPTER, FROM_MODEL): [refusal], **other_routes()})
    _catalog(monkeypatch, *rows)
    _seed(repo, task_type=task_type)
    calls: list = []
    launch = _launcher(repo, calls)

    first = dependency_autolaunch.reconcile(repo, trigger_task_id=PARENT, launch=launch)
    recorded_at = _updated_at(repo)
    second = dependency_autolaunch.reconcile(repo, trigger_task_id=PARENT, launch=launch)

    # Launching into an open circuit with nowhere to go is never attempted.
    assert calls == []
    for outcome in (first, second):
        assert outcome["rerouted"] == []
        assert outcome["launched"] == []
        entry = _entry(outcome)
        assert entry["reason"] == dependency_autolaunch.NO_CAPABLE_ROUTE
        assert entry["detail"].startswith(detail)
        assert entry["failure_kind"] == "quota"
    assert _entry(first)["blocker_recorded"] is True
    assert _entry(second)["blocker_recorded"] is False
    # Recorded once: the second tick left the card row untouched.
    assert _updated_at(repo) == recorded_at
    card = _card(repo)
    assert card["runner"] == FROM_RUNNER
    assert card["status"] == "pending"
    blocker = card["operational_blocker"]
    assert blocker["kind"] == dependency_autolaunch.QUOTA_CIRCUIT_BLOCKER_KIND
    assert blocker["reason"] == "no_capable_route"
    assert (blocker["adapter_id"], blocker["model"]) == (FROM_ADAPTER, FROM_MODEL)
    assert blocker["reset_at"] == refusal["provider_error"]["reset_at"]
    assert len(_events(repo, "quota_circuit_reroute_blocked")) == 1
    assert _events(repo, "reroute_launch_identity") == []


def test_a_card_already_rerouted_in_this_episode_is_not_rerouted_again(repo, monkeypatch):
    quota = _sealed("insufficient_balance", ago=timedelta(minutes=1), reset_in=timedelta(days=5))
    evidence = {(FROM_ADAPTER, FROM_MODEL): [quota]}
    _route_evidence(monkeypatch, evidence)
    _catalog(monkeypatch, TRIPPED_ROW, CODEX_ROW)
    _seed(repo)
    calls: list = []
    launch = _launcher(repo, calls, claim=False)

    first = dependency_autolaunch.reconcile(repo, trigger_task_id=PARENT, launch=launch)
    assert [row["to_runner"] for row in first["rerouted"]] == [TO_RUNNER]
    assert calls == [(CHILD, TO_RUNNER)]

    # The new route trips too, while the first episode is still open, and a
    # capable third route is on offer: no ping-pong, the card stays put.
    evidence[(TO_ADAPTER, TO_MODEL)] = [_sealed("invalid_api_key", ago=timedelta(seconds=10))]
    _catalog(
        monkeypatch,
        TRIPPED_ROW,
        _row("gpt-5.5", TO_ADAPTER, TO_MODEL, "openai", available=False),
        SONNET_ROW,
    )
    second = dependency_autolaunch.reconcile(repo, trigger_task_id=PARENT, launch=launch)
    third = dependency_autolaunch.reconcile(repo, trigger_task_id=PARENT, launch=launch)

    assert second["rerouted"] == [] and third["rerouted"] == []
    assert calls == [(CHILD, TO_RUNNER)]
    for outcome in (second, third):
        assert _entry(outcome)["reason"] == dependency_autolaunch.QUOTA_CIRCUIT_REROUTE_EXHAUSTED
    assert _entry(second)["blocker_recorded"] is True
    assert _entry(third)["blocker_recorded"] is False
    card = _card(repo)
    assert card["runner"] == TO_RUNNER
    assert card["operational_blocker"]["reason"] == "quota_circuit_reroute_exhausted"
    assert card["operational_blocker"]["failure_kind"] == "auth"
    assert len(_events(repo, "reroute_launch_identity")) == 1

    # The bound is core's, not only the reconciler's.
    refused = core.reroute_launch_identity(
        CHILD,
        from_runner=TO_RUNNER,
        to_runner=SONNET_RUNNER,
        to_adapter_id=FROM_ADAPTER,
        to_model=SONNET_MODEL,
        reason="second automatic move",
        topic=TOPIC,
        quota_circuit=True,
    )
    assert refused["ok"] is False
    assert refused["stderr"] == "reroute_quota_circuit_unverified:already_rerouted_this_episode"
    assert _card(repo)["runner"] == TO_RUNNER


def test_core_never_grants_quota_circuit_authority_without_the_evidence(repo, monkeypatch):
    _route_evidence(monkeypatch, {})
    _catalog(monkeypatch, TRIPPED_ROW, CODEX_ROW)
    _seed(repo)

    refused = core.reroute_launch_identity(
        CHILD,
        from_runner=FROM_RUNNER,
        to_runner=TO_RUNNER,
        to_adapter_id=TO_ADAPTER,
        to_model=TO_MODEL,
        reason="forged circuit",
        topic=TOPIC,
        quota_circuit=True,
    )

    assert refused["ok"] is False
    assert refused["stderr"].startswith("reroute_quota_circuit_unverified:circuit_not_open")
    card = _card(repo)
    assert card["runner"] == FROM_RUNNER
    assert "quota_circuit_reroute" not in card


def test_core_refuses_a_quota_circuit_move_onto_an_open_route(repo, monkeypatch):
    quota = _sealed("insufficient_balance", ago=timedelta(minutes=1), reset_in=timedelta(days=5))
    _route_evidence(
        monkeypatch,
        {
            (FROM_ADAPTER, FROM_MODEL): [quota],
            (TO_ADAPTER, TO_MODEL): [_sealed("insufficient_quota", ago=timedelta(minutes=2))],
        },
    )
    _catalog(monkeypatch, TRIPPED_ROW, CODEX_ROW)
    _seed(repo)

    refused = core.reroute_launch_identity(
        CHILD,
        from_runner=FROM_RUNNER,
        to_runner=TO_RUNNER,
        to_adapter_id=TO_ADAPTER,
        to_model=TO_MODEL,
        reason="onto another dead route",
        topic=TOPIC,
        quota_circuit=True,
    )

    assert refused["ok"] is False
    assert refused["stderr"].startswith("reroute_quota_circuit_target_open:")
    assert _card(repo)["runner"] == FROM_RUNNER


def test_a_card_that_is_not_pending_is_never_rerouted(repo, monkeypatch):
    quota = _sealed("insufficient_balance", ago=timedelta(minutes=1), reset_in=timedelta(days=5))
    _route_evidence(monkeypatch, {(FROM_ADAPTER, FROM_MODEL): [quota]})
    _catalog(monkeypatch, TRIPPED_ROW, CODEX_ROW)
    _seed(repo)
    conn = sqlite3.connect(_db(repo))
    try:
        conn.execute(
            "UPDATE tasks SET status='processing', worker_status='claimed', claimed_by=? "
            "WHERE task_id=?",
            (FROM_RUNNER, CHILD),
        )
        conn.commit()
    finally:
        conn.close()
    calls: list = []

    outcome = dependency_autolaunch.reconcile(
        repo, trigger_task_id=PARENT, launch=_launcher(repo, calls)
    )

    assert outcome["rerouted"] == []
    assert calls == []
    refused = core.reroute_launch_identity(
        CHILD,
        from_runner=FROM_RUNNER,
        to_runner=TO_RUNNER,
        to_adapter_id=TO_ADAPTER,
        to_model=TO_MODEL,
        reason="claimed card",
        topic=TOPIC,
        quota_circuit=True,
    )
    assert refused["ok"] is False
    assert refused["stderr"] == "reroute_not_pending_unclaimed"
    assert _card(repo)["runner"] == FROM_RUNNER


def test_reroutable_kinds_are_a_named_subset_of_the_single_failure_kinds():
    kinds = workforce_catalog.QUOTA_REROUTE_FAILURE_KINDS
    assert kinds == frozenset({"auth", "quota"})
    assert kinds < workforce_catalog._ROUTE_SINGLE_FAILURE_KINDS
    assert "model_not_found" not in kinds
