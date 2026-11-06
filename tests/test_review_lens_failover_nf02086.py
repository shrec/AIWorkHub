"""NF-2026-02086: review lenses fail over off adapters this sandbox cannot start.

Field report NF-22 on 0.12.88: quality-review lenses were routed to CLI
adapters that cannot start under the Windows AppContainer (``grok_kilo_cli``,
``opencode_cli``) and the chain never failed over. A launch-class START
failure -- spawn denied, executable not found, AppContainer / access denied,
WinError 5 / 2 -- now retires the attempt with a named skip reason, the lens
fails over to the next ranked adapter in the SAME chain, and the route circuit
keeps that route skipped for later plans until a start of it succeeds. A
candidate or review-content failure is not launch-class and changes nothing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pytest  # noqa: E402

import test_review_orchestrator as base  # noqa: E402
from aiworkhub import (  # noqa: E402
    review_orchestrator,
    reviewer_launch_refusal,
    workforce_catalog,
)

ACCESS_DENIED = "worker_launch_failed:PermissionError:[WinError 5] Access is denied"
DENIED_ROUTE = {
    "runner": "grok_kilo_cli_grok-code-fast-1",
    "adapter_id": "grok_kilo_cli",
    "model": "grok-code-fast-1",
}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _denied_launch(**extra: object) -> dict:
    """A synchronous reviewer start the AppContainer refused (no provider ran)."""
    return {
        "ok": False,
        "state": "launch_failed",
        "request_id": "reserved-denied-1",
        "latest_event": {"error": ACCESS_DENIED},
        **extra,
    }


# --- (2) in-chain failover --------------------------------------------------


def test_access_denied_start_fails_over_to_next_adapter_and_lens_is_reviewed(
    tmp_path: Path,
) -> None:
    manager, db_path, chain, first_task, seed = base._seed_terminal_launch(
        tmp_path, "access-denied", _denied_launch(),
    )

    launched = seed.drain(max_actions=1, now=base.NOW)

    # The failover happened inside this one pass: no hold, no manager.
    assert launched.completed == 1
    assert len(manager.launches) == 2
    assert len(manager.provider_launches) == 1
    first, successor = seed._route_attempts(chain.chain_id, "correctness")
    assert first["state"] == "retired"
    assert first["adapter_id"] == base.FIRST_ROUTE["adapter_id"]
    # The skip reason is recorded on the lens: class, adapter, reviewer request.
    assert first["failure_reason"] == (
        "launch_failed:launch_denied"
        f":adapter={base.FIRST_ROUTE['adapter_id']}"
        ":reviewer_request=reserved-denied-1"
    )
    assert reviewer_launch_refusal.launch_class_retirement(first["failure_reason"])
    assert review_orchestrator.route_attempt_hold(first["failure_reason"]) == ""
    assert successor["state"] == "launched"
    assert successor["reviewer_task_id"] != first_task
    assert {key: successor[key] for key in base.SUCCESSOR_ROUTE} == (
        base.SUCCESSOR_ROUTE
    )

    successor_request = str(successor["reviewer_request_id"])
    manager.status_results[successor_request] = base._review_status(
        reviewer_request=successor_request,
        reviewer_task=str(successor["reviewer_task_id"]),
        provider=base.SUCCESSOR_ROUTE["adapter_id"],
        route=base.SUCCESSOR_ROUTE,
    )
    converged = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path,
        route_selector=base._two_route_selector(first_task),
    ).drain(max_actions=1, now=base.NOW)

    # The required lens is reviewed by the adapter that could start.
    assert converged.completed == 1
    assert manager.accepts == [
        (successor_request, str(successor["reviewer_task_id"]))
    ]


def test_launch_class_failure_excludes_the_whole_adapter_from_the_next_plan(
    tmp_path: Path,
) -> None:
    _manager, _db_path, chain, _first_task, seed = base._seed_terminal_launch(
        tmp_path, "excluded", _denied_launch(),
    )
    seed.drain(max_actions=1, now=base.NOW)
    attempts = seed._route_attempts(chain.chain_id, "correctness")

    excluded, excluded_adapters = review_orchestrator._route_attempt_exclusions(
        attempts
    )

    assert base.FIRST_ROUTE["adapter_id"] in excluded_adapters
    assert (
        base.FIRST_ROUTE["runner"], base.FIRST_ROUTE["adapter_id"],
        base.FIRST_ROUTE["model"],
    ) in excluded


def test_launched_reviewer_whose_start_was_denied_fails_over_on_reconcile(
    tmp_path: Path,
) -> None:
    manager = base._FailoverManager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "async-denied.sqlite",
        route_selector=lambda *_args: dict(base.FIRST_ROUTE),
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=base.NOW,
    )
    first_task = driver._reviewer_task_id(chain.chain_identity, "correctness")
    driver.route_selector = base._two_route_selector(first_task)
    assert driver.drain(max_actions=1, now=base.NOW).completed == 1
    first_request = manager.requests_by_task[first_task]
    manager.status_results[first_request] = {
        "ok": True,
        "request_id": first_request,
        "task_id": first_task,
        "state": "launch_failed",
        **base.FIRST_ROUTE,
        "latest_event": {"error": "AppContainer spawn denied: [WinError 5]"},
    }

    driver.drain(max_actions=1, now=base.NOW)

    first, successor = driver._route_attempts(chain.chain_id, "correctness")
    assert first["state"] == "retired"
    assert first["reviewer_request_id"] == first_request
    assert reviewer_launch_refusal.launch_class_retirement(first["failure_reason"])
    assert successor["state"] == "launched"
    assert successor["adapter_id"] == base.SUCCESSOR_ROUTE["adapter_id"]
    assert len(manager.provider_launches) == 2


# --- non-launch failures never trigger the failover -------------------------


def test_candidate_failure_with_denied_text_does_not_fail_over(
    tmp_path: Path,
) -> None:
    manager, _db_path, chain, _first_task, seed = base._seed_terminal_launch(
        tmp_path, "candidate",
        {
            "state": "validation_failed",
            "error_code": "",
            "latest_event": {"error": ACCESS_DENIED},
        },
    )

    launched = seed.drain(max_actions=1, now=base.NOW)

    assert launched.completed == 0
    assert len(manager.provider_launches) == 0
    attempts = seed._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 1
    assert not reviewer_launch_refusal.launch_class_retirement(
        attempts[0]["failure_reason"]
    )
    assert review_orchestrator.route_attempt_hold(
        attempts[0]["failure_reason"]
    ) == "candidate_rework"
    _excluded, excluded_adapters = review_orchestrator._route_attempt_exclusions(
        attempts
    )
    assert base.FIRST_ROUTE["adapter_id"] not in excluded_adapters


@pytest.mark.parametrize("state", ["worker_failed", "validation_failed", "timed_out"])
def test_a_reviewer_that_ran_is_never_launch_class(state: str) -> None:
    status = {
        "ok": True,
        "state": state,
        "latest_event": {"error": ACCESS_DENIED},
        "task_card": {"terminal_substatus": state},
    }

    reason = review_orchestrator.mechanical_reviewer_failure_reason(status)

    assert reviewer_launch_refusal.LAUNCH_FAILURE_CLASS not in reason
    assert not reviewer_launch_refusal.launch_class_retirement(reason)
    assert workforce_catalog._route_failure_kind(
        {"state": state, "error": ACCESS_DENIED}
    ) != reviewer_launch_refusal.LAUNCH_FAILURE_CLASS


@pytest.mark.parametrize(
    "error",
    [
        ACCESS_DENIED,
        "[WinError 2] The system cannot find the file specified",
        "spawn_denied:appcontainer",
        "executable_not_found:opencode",
        "[Errno 2] No such file or directory: 'opencode'",
        "[Errno 13] Permission denied: 'grok'",
    ],
)
def test_launch_class_vocabulary_is_shared_by_classifier_and_circuit(
    error: str,
) -> None:
    status = {"ok": False, "state": "launch_failed", "latest_event": {"error": error}}

    assert review_orchestrator.mechanical_reviewer_failure_reason(status) == (
        "launch_failed:launch_denied"
    )
    assert workforce_catalog._route_failure_kind(
        {"state": "launch_failed", "error": error}
    ) == reviewer_launch_refusal.LAUNCH_FAILURE_CLASS


def test_unrelated_launch_failure_keeps_its_previous_classification() -> None:
    status = {
        "ok": False, "state": "launch_failed",
        "latest_event": {"error": "runtime_error: provider exited 1"},
    }

    assert review_orchestrator.mechanical_reviewer_failure_reason(status) == (
        "launch_failed:mechanical_terminal"
    )
    assert workforce_catalog._route_failure_kind(
        {"state": "launch_failed", "error": "runtime_error"}
    ) == "transient"


# --- (1) routing skips the route until a later start succeeds ---------------


def _denied_row(epoch: float) -> dict:
    return {
        **DENIED_ROUTE,
        "request_id": "denied-start",
        "state": "launch_failed",
        "error": ACCESS_DENIED,
        "finished_at": _iso(epoch),
    }


def test_one_denied_start_opens_the_circuit_until_a_later_start_succeeds(
    tmp_path: Path,
) -> None:
    now = 2_000_000_000.0
    denied = [_denied_row(now - 60)]

    opened = workforce_catalog.route_circuit_for(
        tmp_path, DENIED_ROUTE["adapter_id"], DENIED_ROUTE["model"],
        now_epoch=now, observations=denied,
    )
    # Long after the transient cooldown it is still skipped: nothing changed.
    still_open = workforce_catalog.route_circuit_for(
        tmp_path, DENIED_ROUTE["adapter_id"], DENIED_ROUTE["model"],
        now_epoch=now + workforce_catalog.ROUTE_CIRCUIT_COOLDOWN_SECONDS + 60,
        observations=denied,
    )
    recovered = workforce_catalog.route_circuit_for(
        tmp_path, DENIED_ROUTE["adapter_id"], DENIED_ROUTE["model"],
        now_epoch=now,
        observations=denied + [{
            **DENIED_ROUTE, "state": "accepted", "error": "",
            "finished_at": _iso(now - 30),
        }],
    )

    assert opened["state"] == "open"
    assert opened["failure_kind"] == reviewer_launch_refusal.LAUNCH_FAILURE_CLASS
    assert opened["threshold"] == 1
    assert still_open["state"] == "open"
    assert recovered["state"] == "closed"


def test_reviewer_route_selection_skips_a_route_whose_start_was_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    rows: list[dict] = [_denied_row((now - timedelta(seconds=60)).timestamp())]
    monkeypatch.setattr(
        workforce_catalog.task_store, "route_terminal_observations",
        lambda _root, **_kw: list(rows),
    )
    monkeypatch.setattr(
        review_orchestrator, "_review_route_policy_enabled", lambda *_a: True,
    )
    identity = review_orchestrator._review_route_identity(DENIED_ROUTE)

    def refusal() -> str:
        return review_orchestrator._review_route_refusal(
            tmp_path, DENIED_ROUTE, identity, None, frozenset(), frozenset(),
        )

    assert refusal() == "circuit_open"
    rows.insert(0, {
        **DENIED_ROUTE, "state": "accepted", "error": "",
        "finished_at": (now - timedelta(seconds=10)).isoformat(),
    })
    assert refusal() == ""
