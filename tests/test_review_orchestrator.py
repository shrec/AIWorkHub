from __future__ import annotations

import sys
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_lifecycle, review_orchestrator, task_store  # noqa: E402
import pytest  # noqa: E402


NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)
ROUTE = {
    "runner": "copilot_gpt-5.6-sol",
    "adapter_id": "vscode_lm",
    "model": "gpt-5.6-sol",
}


def _route(_repo: Path, _task_id: str, _lens: str) -> dict[str, str]:
    return dict(ROUTE)


class _Manager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.launches: list[dict] = []
        self.accepts: list[tuple[str, str]] = []
        self.status_result: dict = {"ok": True, "state": "starting"}
        self.status_results: dict[str, dict] = {}
        self.events: list[dict] = []
        self.target_status = _target_status()

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(kwargs)
        return {
            "ok": True,
            "request_id": "review-request-" + kwargs["lens"],
            "task_id": kwargs["reviewer_task_id"],
            "state": "starting",
        }

    def status(self, request_id):
        if request_id == "target-request":
            return dict(self.target_status)
        return {
            "request_id": request_id,
            **self.status_results.get(request_id, self.status_result),
        }

    def accept_review(self, request_id, task_id, **kwargs):
        self.accepts.append((request_id, task_id))
        return {"ok": True, "request_id": request_id, "task_id": task_id, **kwargs}

    def _append_event(self, event):
        self.events.append(event)


class _FailoverManager(_Manager):
    """ProcessManager-shaped launch reconciliation for route failover tests."""

    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.requests_by_task: dict[str, str] = {}
        self.provider_launches: list[dict] = []
        self.terminal_launch_results: dict[str, dict] = {}

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(kwargs)
        task_id = kwargs["reviewer_task_id"]
        terminal = self.terminal_launch_results.pop(task_id, None)
        if terminal is not None:
            return {
                "task_id": task_id,
                **{key: kwargs[key] for key in ROUTE},
                **terminal,
            }
        request_id = self.requests_by_task.get(task_id)
        if request_id is None:
            request_id = f"review-request-{len(self.requests_by_task) + 1}"
            self.requests_by_task[task_id] = request_id
            self.provider_launches.append(dict(kwargs))
        return {
            "ok": True,
            "request_id": request_id,
            "task_id": task_id,
            "state": "starting",
            "already_reserved": len([
                call for call in self.launches
                if call["reviewer_task_id"] == task_id
            ]) > 1,
        }


def _target_status(*, state: str = "review_ready", **card_overrides: object) -> dict:
    card = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "claim_epoch": "1",
        "packet_sha256": "a" * 64,
        "candidate_sha256": "b" * 64,
        "workspace_identity": "workspace-candidate-a",
        "evidence": {"source_graph_partition_readiness": {"target": True}},
    }
    card.update(card_overrides)
    return {"ok": True, "state": state, "task_card": card}


def test_driver_launches_one_action_and_persists_exact_identity(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request",
        claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256="b" * 64,
        now=NOW,
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert len(manager.launches) == 1
    assert {key: manager.launches[0][key] for key in ROUTE} == ROUTE
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert rows[0]["state"] == "completed"
    assert rows[0]["chain_id"] == chain.chain_id


def test_launch_waits_until_target_is_ready_then_launches_once(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(state="processing")
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    deferred = driver.drain(max_actions=2, now=NOW)

    assert deferred.attempted == 1
    assert deferred.pending == 1
    assert manager.launches == []
    assert manager.events[-1]["review_automation"]["reason"] == "target_not_review_ready"
    manager.target_status = _target_status()
    launched = driver.drain(max_actions=2, now=NOW)

    assert launched.completed == 1
    assert len(manager.launches) == 1


def test_round_rollover_duplicate_does_not_starve_later_chain(
    tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    manager.status_results["waiting-request"] = _target_status(
        state="processing",
        task_id="WAITING",
        request_id="waiting-request",
        candidate_sha256="b" * 64,
        workspace_identity="workspace-waiting",
    )
    manager.status_results["ready-request"] = _target_status(
        task_id="READY",
        request_id="ready-request",
        candidate_sha256="c" * 64,
        workspace_identity="workspace-ready",
    )
    db_path = tmp_path / "review.sqlite"
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="WAITING", target_request_id="waiting-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    first_action_id = review_lifecycle.rows_for_test(db_path)[0]["action_id"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO review_reservation_state "
            "(id, last_pending_action_id, round_high_watermark) VALUES (1, 0, 0)"
        )
        conn.execute(
            "UPDATE review_reservation_state SET last_pending_action_id=0, "
            "round_high_watermark=? WHERE id=1",
            (first_action_id,),
        )
        conn.commit()
    driver.ensure_chain(
        target_task_id="READY", target_request_id="ready-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="c" * 64, now=NOW,
    )

    result = driver.drain(max_actions=3, now=NOW)

    assert result.attempted == 2
    assert result.pending == 1
    assert result.completed == 1
    assert len(manager.launches) == 1


def test_initial_route_unavailable_defers_without_terminalizing_chain(
    tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    available = False

    def route(_repo: Path, _task_id: str, lens: str) -> dict[str, str]:
        if not available:
            raise RuntimeError("review_route_unavailable:" + lens)
        return dict(ROUTE)

    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    deferred = driver.drain(max_actions=1, now=NOW)
    assert deferred.pending == 1
    assert deferred.failed == 0
    assert review_lifecycle.lifecycle_counts(
        tmp_path / "review.sqlite"
    )["pending"] == 12

    available = True
    launched = driver.drain(max_actions=1, now=NOW)
    assert launched.completed == 1
    assert len(manager.launches) == 1


FIRST_ROUTE = {
    "runner": "copilot_claude-opus-5",
    "adapter_id": "vscode_lm",
    "model": "claude-opus-5",
}
SUCCESSOR_ROUTE = {
    "runner": "deepseek_native-v4-pro",
    "adapter_id": "deepseek_copilot_cli",
    "model": "deepseek-v4-pro",
}


def _terminal_status(request_id: str, task_id: str, *, error_code: str = "") -> dict:
    """A terminal reviewer status whose ROUTE failure the provider asserted.

    ``error_code`` is the launcher's own closed-vocabulary refusal field.
    Distinct-route failover is now gated on the typed disposition, so a status
    that asserts nothing is deliberately NOT enough to spend a second reviewer
    -- see ``test_unestablished_terminal_evidence_holds_instead_of_relaunching``.
    """
    status = {
        "ok": True,
        "request_id": request_id,
        "task_id": task_id,
        "state": "worker_failed",
        **FIRST_ROUTE,
        "error_code": error_code or "provider_unavailable",
        "latest_event": {
            "failure_kind": "worker_failed",
            "diagnostic": "worker_failed:provider_timeout:exit_code=1",
        },
        "task_card": {
            "terminal_substatus": "worker_failed",
            "worker_status": "worker_failed",
        },
    }
    return status


def _two_route_selector(first_task_id: str):
    def select(_repo: Path, task_id: str, _lens: str) -> dict[str, str]:
        return dict(FIRST_ROUTE if task_id == first_task_id else SUCCESSOR_ROUTE)

    return select


def test_terminal_reviewer_before_restart_launches_distinct_successor_and_converges(
    tmp_path: Path,
) -> None:
    manager = _FailoverManager(tmp_path)
    db_path = tmp_path / "terminal-restart.sqlite"
    seed = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=lambda *_args: dict(FIRST_ROUTE)
    )
    chain = seed.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    first_task = seed._reviewer_task_id(chain.chain_identity, "correctness")
    seed.route_selector = _two_route_selector(first_task)
    assert seed.drain(max_actions=1, now=NOW).completed == 1
    first_request = manager.requests_by_task[first_task]
    manager.status_results[first_request] = _terminal_status(first_request, first_task)

    restarted = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=_two_route_selector(first_task)
    )
    failed_over = restarted.drain(max_actions=1, now=NOW)

    assert failed_over.pending == 1
    assert len(manager.provider_launches) == 2
    successor = restarted._route_attempts(chain.chain_id, "correctness")[-1]
    assert successor["reviewer_task_id"] != first_task
    assert successor["state"] == "launched"
    assert {key: successor[key] for key in SUCCESSOR_ROUTE} == SUCCESSOR_ROUTE
    first = restarted._route_attempts(chain.chain_id, "correctness")[0]
    assert first["state"] == "retired"
    assert "provider_timeout" in first["failure_reason"]

    successor_request = str(successor["reviewer_request_id"])
    manager.status_results[successor_request] = _review_status(
        reviewer_request=successor_request,
        reviewer_task=str(successor["reviewer_task_id"]),
        provider=SUCCESSOR_ROUTE["adapter_id"],
        route=SUCCESSOR_ROUTE,
    )
    converged = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=_two_route_selector(first_task)
    ).drain(max_actions=1, now=NOW)

    assert converged.completed == 1
    assert len(manager.provider_launches) == 2
    assert manager.accepts == [
        (successor_request, str(successor["reviewer_task_id"]))
    ]


def test_launch_ack_before_attempt_request_persistence_reconciles_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FailoverManager(tmp_path)
    db_path = tmp_path / "ack-crash.sqlite"
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=lambda *_args: dict(FIRST_ROUTE)
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    first_task = driver._reviewer_task_id(chain.chain_identity, "correctness")
    driver.route_selector = _two_route_selector(first_task)
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    first_request = manager.requests_by_task[first_task]
    manager.status_results[first_request] = _terminal_status(first_request, first_task)
    original_bind = driver._bind_route_attempt_request
    failed_once = False

    def fail_successor_once(action, attempt, request_id):
        nonlocal failed_once
        if int(attempt["attempt_index"]) == 2 and not failed_once:
            failed_once = True
            return False
        return original_bind(action, attempt, request_id)

    monkeypatch.setattr(driver, "_bind_route_attempt_request", fail_successor_once)
    assert driver.drain(max_actions=1, now=NOW).pending == 1
    assert len(manager.provider_launches) == 2
    successor = driver._route_attempts(chain.chain_id, "correctness")[-1]
    assert successor["state"] == "planned"
    assert successor["reviewer_request_id"] == ""

    restarted = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=_two_route_selector(first_task)
    )
    assert restarted.drain(max_actions=1, now=NOW).pending == 1
    assert len(manager.provider_launches) == 2, "same task must bind the prior provider"
    recovered = restarted._route_attempts(chain.chain_id, "correctness")[-1]
    assert recovered["state"] == "launched"
    assert recovered["reviewer_request_id"] == manager.requests_by_task[
        str(recovered["reviewer_task_id"])
    ]


def _seed_terminal_launch(
    tmp_path: Path, name: str, terminal: dict,
) -> tuple[_FailoverManager, Path, object, str, review_orchestrator.ReviewOrchestrator]:
    """One chain whose first reviewer launch returns ``terminal`` synchronously."""
    manager = _FailoverManager(tmp_path)
    db_path = tmp_path / f"typed-{name}.sqlite"
    seed = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=lambda *_args: dict(FIRST_ROUTE)
    )
    chain = seed.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    first_task = seed._reviewer_task_id(chain.chain_identity, "correctness")
    seed.route_selector = _two_route_selector(first_task)
    manager.terminal_launch_results[first_task] = {
        "ok": False,
        "latest_event": {
            "diagnostic": "provider secret=sk-must-not-persist arbitrary prose",
        },
        **terminal,
    }
    return manager, db_path, chain, first_task, seed


def test_provider_asserted_transient_advances_distinct_route_and_converges(
    tmp_path: Path,
) -> None:
    """The ONE terminal receipt that still earns a second reviewer immediately.

    ``provider_unavailable`` is a refusal kind the provider boundary
    established, so the canonical disposition names ``provider_transient`` and
    selects ``route_retry`` with a single bounded distinct-route retry.
    """
    manager, db_path, chain, first_task, seed = _seed_terminal_launch(
        tmp_path, "transient",
        {"state": "launch_failed", "error_code": "provider_unavailable"},
    )

    launched = seed.drain(max_actions=1, now=NOW)

    assert launched.completed == 1
    assert len(manager.launches) == 2
    assert len(manager.provider_launches) == 1
    first, successor = seed._route_attempts(chain.chain_id, "correctness")
    assert first["state"] == "retired"
    assert first["failure_reason"] == "launch_failed:mechanical_terminal"
    assert "sk-must-not-persist" not in first["failure_reason"]
    assert review_orchestrator.route_attempt_hold(first["failure_reason"]) == ""
    assert successor["state"] == "launched"
    assert successor["reviewer_task_id"] != first_task
    assert {key: successor[key] for key in SUCCESSOR_ROUTE} == SUCCESSOR_ROUTE

    successor_request = str(successor["reviewer_request_id"])
    manager.status_results[successor_request] = _review_status(
        reviewer_request=successor_request,
        reviewer_task=str(successor["reviewer_task_id"]),
        provider=SUCCESSOR_ROUTE["adapter_id"],
        route=SUCCESSOR_ROUTE,
    )
    converged = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=_two_route_selector(first_task)
    ).drain(max_actions=1, now=NOW)

    assert converged.completed == 1
    assert len(manager.provider_launches) == 1
    assert manager.accepts == [
        (successor_request, str(successor["reviewer_task_id"]))
    ]


# Each row is a terminal launch receipt whose TYPED disposition forbids another
# reviewer. Before NF-2026-00847 every one of them bought a second provider on
# a distinct route that could not possibly have settled it.
@pytest.mark.parametrize(
    ("name", "terminal", "action"),
    [
        (
            "credential",
            {"state": "launch_failed", "error_code": "credential_rejected"},
            "credential_hold",
        ),
        (
            "dependency",
            {
                "state": "launch_failed",
                "provider_error": {
                    "owner": "provider", "sealed": True,
                    "code": "dependency_unavailable",
                },
            },
            "dependency_hold",
        ),
        (
            "callback",
            {"state": "finalize_failed", "error_code": ""},
            "callback_reconcile",
        ),
        (
            "cancellation",
            {"state": "cancelled", "error_code": ""},
            "cancellation_final",
        ),
        (
            "unestablished",
            {"state": "launch_failed", "error_code": "runtime_error"},
            "manager_judgment_unknown",
        ),
        (
            "candidate",
            {"state": "validation_failed", "error_code": ""},
            "candidate_rework",
        ),
    ],
)
def test_terminal_receipt_without_a_route_cause_holds_instead_of_relaunching(
    tmp_path: Path, name: str, terminal: dict, action: str,
) -> None:
    manager, _db_path, chain, _first_task, seed = _seed_terminal_launch(
        tmp_path, name, terminal,
    )

    launched = seed.drain(max_actions=1, now=NOW)

    # The action does not complete and, decisively, no second provider is spent.
    assert launched.completed == 0
    assert len(manager.provider_launches) == 0
    attempts = seed._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 1
    assert attempts[0]["state"] == "retired"
    assert review_orchestrator.route_attempt_hold(
        attempts[0]["failure_reason"]
    ) == action
    assert "sk-must-not-persist" not in attempts[0]["failure_reason"]

    # And the hold is DURABLE: a later reconcile pass, and a restarted driver,
    # both refuse to plan a fresh route rather than spending the launch a pass
    # later. That is the difference between a hold and a delay.
    assert seed.drain(max_actions=1, now=NOW).completed == 0
    assert len(seed._route_attempts(chain.chain_id, "correctness")) == 1
    assert len(manager.provider_launches) == 0


def test_capacity_is_never_a_credential_hold_and_keeps_a_distinct_route(
    tmp_path: Path,
) -> None:
    """Quota/session/rate is capacity, not credential, and is not a hard hold.

    It spends nothing now -- the failing route cannot be relaunched before its
    reset -- but a DISTINCT eligible route stays available to a later pass.
    """
    manager, _db_path, chain, first_task, seed = _seed_terminal_launch(
        tmp_path, "capacity",
        {"state": "launch_failed", "error_code": "quota_exhausted"},
    )

    assert seed.drain(max_actions=1, now=NOW).completed == 0
    assert len(manager.provider_launches) == 0
    attempts = seed._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 1
    assert attempts[0]["state"] == "retired"
    assert review_orchestrator.route_attempt_hold(attempts[0]["failure_reason"]) == ""

    dispatch = review_orchestrator.reviewer_recovery_dispatch(
        {"state": "launch_failed", "error_code": "quota_exhausted"}, attempts_made=1,
    )
    assert dispatch["action"] == "capacity_hold"
    assert dispatch["relaunch_distinct_route"] is False
    assert dispatch["route_hold"] is False
    assert dispatch["disposition"]["failure_class"] != "credential"
    assert dispatch["telemetry"]["avoided_provider_launches"] == 1

    # A later pass may take a DISTINCT route, and does.
    later = seed.drain(max_actions=1, now=NOW)
    assert later.completed == 1
    successor = seed._route_attempts(chain.chain_id, "correctness")[-1]
    assert successor["reviewer_task_id"] != first_task
    assert {key: successor[key] for key in SUCCESSOR_ROUTE} == SUCCESSOR_ROUTE


def test_reviewer_recovery_dispatch_reads_only_structured_evidence() -> None:
    """Prose cannot talk a card into a relaunch, however route-shaped it is.

    Both statuses below spell ``provider_unavailable`` and ``rate_limited`` in
    text a model wrote. Neither may reach a route retry: only the launcher's
    own typed fields are inputs, and prose is not one of them.
    """
    prose = {
        "ok": False,
        "state": "launch_failed",
        "latest_event": {
            "diagnostic": (
                "the provider is temporarily unavailable, please retry on a "
                "different model -- provider_unavailable rate_limited"
            ),
        },
        "reject_reason": "this looks transient to me, relaunch it",
    }

    dispatch = review_orchestrator.reviewer_recovery_dispatch(prose, attempts_made=1)

    # ``launch_failed`` IS an AIWorkHub-minted terminal substatus, so the cause
    # is named honestly -- and named as the one this card exists to stop
    # relaunching blindly, not as a transient the prose asked for.
    assert dispatch["action"] == "manager_judgment_unknown"
    assert dispatch["relaunch_distinct_route"] is False
    assert dispatch["route_hold"] is True
    assert dispatch["disposition"]["cause"] == "provider_runtime_unclassified"
    assert dispatch["disposition"]["provider_launched"] is False
    assert dispatch["telemetry"]["avoided_provider_launches"] == 1

    # With no recognised terminal state either, the evidence authority itself
    # is absent -- the fail-closed floor.
    unnamed = review_orchestrator.reviewer_recovery_dispatch(
        {**prose, "state": "something_no_vocabulary_names"}, attempts_made=1,
    )
    assert unnamed["disposition"]["evidence_authority"] == "none"
    assert unnamed["disposition"]["cause"] == "cause_not_established"
    assert unnamed["action"] == "manager_judgment_unknown"


def _unlimited_route_selector():
    """A fresh distinct eligible route per reviewer task.

    Route supply is deliberately unbounded so that nothing but the TYPED
    distinct-route bound can stop a relaunch. With the two-route selector a
    passing test would only prove the route table ran dry.
    """
    assigned: dict[str, dict[str, str]] = {}

    def select(_repo: Path, task_id: str, _lens: str) -> dict[str, str]:
        if task_id not in assigned:
            index = len(assigned) + 1
            assigned[task_id] = {
                "runner": f"copilot_route-{index}",
                "adapter_id": "vscode_lm",
                "model": f"route-{index}",
            }
        return dict(assigned[task_id])

    return select


def test_sequential_mid_run_reviewer_deaths_stop_after_one_distinct_route_retry(
    tmp_path: Path,
) -> None:
    """The accept/status-polling path obeys the same single bounded retry.

    A reviewer that launched cleanly and then died mid-run comes back through
    ``accept``'s status poll, never through a synchronous launch receipt.
    ``route_retry`` is not a route hold, so no retirement marker stops that
    branch: before this it retired the dead attempt and launched a successor on
    EVERY drain pass, spending a provider per pass for ever.
    """
    manager = _FailoverManager(tmp_path)
    db_path = tmp_path / "mid-run-death.sqlite"
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=_unlimited_route_selector()
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    assert len(manager.provider_launches) == 1

    def kill_running_attempt() -> None:
        """The live reviewer dies mid-run on its own exact route."""
        attempt = driver._route_attempts(chain.chain_id, "correctness")[-1]
        request_id = str(attempt["reviewer_request_id"])
        manager.status_results[request_id] = {
            "ok": True,
            "request_id": request_id,
            "task_id": str(attempt["reviewer_task_id"]),
            "state": "worker_failed",
            "error_code": "provider_unavailable",
            **{key: str(attempt[key]) for key in ROUTE},
            "latest_event": {
                "failure_kind": "worker_failed",
                "diagnostic": "worker_failed:provider_timeout:exit_code=1",
            },
            "task_card": {
                "terminal_substatus": "worker_failed",
                "worker_status": "worker_failed",
            },
        }

    # First mid-run death spends the ONE distinct-route retry the typed
    # disposition allows.
    kill_running_attempt()
    assert driver.drain(max_actions=1, now=NOW).completed == 0
    assert len(manager.provider_launches) == 2
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 2
    assert attempts[0]["state"] == "retired"
    assert attempts[1]["state"] == "launched"
    assert attempts[1]["runner"] != attempts[0]["runner"]

    # The successor dies the same way. The retry is spent, so every later pass
    # must hold on the durable retirement -- no third reviewer, ever.
    kill_running_attempt()
    for _ in range(3):
        assert driver.drain(max_actions=1, now=NOW).completed == 0
        assert len(manager.provider_launches) == 2
        held = driver._route_attempts(chain.chain_id, "correctness")
        assert len(held) == 2
        assert held[-1]["state"] == "retired"

    # And the bound is durable rather than in-process: a restarted driver reads
    # the same two retired attempts and still refuses to plan a third route.
    restarted = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=_unlimited_route_selector()
    )
    assert restarted.drain(max_actions=1, now=NOW).completed == 0
    assert len(manager.provider_launches) == 2
    assert len(restarted._route_attempts(chain.chain_id, "correctness")) == 2

    dispatch = review_orchestrator.reviewer_recovery_dispatch(
        {"state": "worker_failed", "error_code": "provider_unavailable"},
        attempts_made=2,
    )
    assert dispatch["action"] == "route_retry"
    assert dispatch["route_hold"] is False
    assert dispatch["relaunch_distinct_route"] is False
    assert dispatch["telemetry"]["avoided_provider_launches"] == 1


@pytest.mark.parametrize(
    ("state", "detail", "failure_class"),
    [
        ("worker_failed", "monthly_credit_limit_reached", "provider_credit"),
        ("launch_failed", "provider_refused:quota_exhausted", "provider_quota"),
        ("launch_failed", "provider_refused:request_refused", "provider_refusal"),
        ("timed_out", "vscode_lm_response_timeout", "provider_timeout"),
    ],
)
def test_provider_terminal_classes_are_mechanical_route_failures(
    state: str, detail: str, failure_class: str,
) -> None:
    reason = review_orchestrator.mechanical_reviewer_failure_reason({
        "state": state,
        "latest_event": {"diagnostic": detail},
        "task_card": {},
    })

    assert reason == f"{state}:{failure_class}"
    assert review_orchestrator.mechanical_reviewer_failure_reason({
        "state": "review_ready", "latest_event": {"diagnostic": detail},
    }) == ""


def test_mechanical_route_failure_never_retains_provider_prose_or_secrets() -> None:
    secret = "sk-provider-secret-material"
    reason = review_orchestrator.mechanical_reviewer_failure_reason({
        "ok": False,
        "state": "launch_failed",
        "latest_event": {
            "diagnostic": f"monthly_credit_limit token={secret} arbitrary prose",
        },
        "task_card": {},
    })

    assert reason == "launch_failed:provider_credit"
    assert secret not in reason
    assert "arbitrary prose" not in reason


def test_launch_accepts_canonical_review_ready_card_when_process_state_is_absent(
    tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(
        status="review",
        worker_status="review",
        terminal_substatus="review_ready",
    )
    manager.target_status["state"] = None
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    launched = driver.drain(max_actions=1, now=NOW)

    assert launched.completed == 1
    assert len(manager.launches) == 1


def test_launch_rejects_identity_mismatch_but_prewarm_is_launch_owned(
    tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(candidate_sha256="c" * 64)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    invalid = driver.drain(max_actions=2, now=NOW)

    assert invalid.failed == 1
    assert manager.launches == []
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert "target_candidate_identity_invalid" in rows[0]["failure_reason"]

    manager = _Manager(tmp_path)
    manager.target_status = _target_status(
        evidence={"source_graph_partition_readiness": {}}
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "empty.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    launch_owned = driver.drain(max_actions=2, now=NOW)

    assert launch_owned.completed == 1
    assert len(manager.launches) == 1
    readiness = json.loads(
        review_lifecycle.rows_for_test(tmp_path / "empty.sqlite")[0]["receipt_json"]
    )["target_readiness_receipt"]
    assert readiness["outcome"] == "ready"
    assert readiness["partition_readiness"] == {}


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("task_id", "target_task_identity_invalid"),
        ("packet_sha256", "target_packet_identity_invalid"),
        ("candidate_sha256", "target_candidate_identity_invalid"),
    ],
)
def test_launch_identity_mismatches_are_terminal_before_not_ready(
    tmp_path: Path, field: str, reason: str
) -> None:
    manager = _Manager(tmp_path)
    replacement = "wrong" if field != "candidate_sha256" else "c" * 64
    manager.target_status = _target_status(state="processing", **{field: replacement})
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "identity.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    assert manager.launches == []
    assert reason in review_lifecycle.rows_for_test(tmp_path / "identity.sqlite")[0]["failure_reason"]


@pytest.mark.parametrize(
    ("field", "replacement", "reason"),
    [
        ("request_id", "newer-request", "target_request_superseded:newer-request"),
        ("claim_epoch", "9", "target_claim_epoch_superseded:9"),
    ],
)
def test_superseded_target_is_retired_as_obsolete_not_failed(
    tmp_path: Path, field: str, replacement: str, reason: str
) -> None:
    """A newer request/claim on the same task ends the chain, it does not park it.

    Measured over 627 real chains: 337 were bound to a request the card had
    already replaced, and every one of them FAILED its launch action, which
    parked its whole chain permanently. Nothing is left to review on those
    bytes -- that is a completed chain, not a broken one.
    """
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(**{field: replacement})
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "superseded.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 0
    assert result.completed == 1
    assert manager.launches == []
    row = review_lifecycle.rows_for_test(tmp_path / "superseded.sqlite")[0]
    assert row["state"] == "completed"
    assert json.loads(row["receipt_json"])["obsolete_reason"] == reason


def test_launch_binds_workspace_when_canonical_card_becomes_available(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    manager.target_status = {"ok": True, "state": "processing"}
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "late-card.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    manager.target_status = _target_status()

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert len(manager.launches) == 1


def test_launch_rejects_different_nonempty_workspace_identity(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "workspace.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    manager.target_status = _target_status(workspace_identity="workspace-candidate-b")

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    assert manager.launches == []
    rows = review_lifecycle.rows_for_test(tmp_path / "workspace.sqlite")
    assert "target_workspace_identity_invalid" in rows[0]["failure_reason"]


def test_accept_waits_for_supervisor_terminal_receipt_without_busy_loop(
    tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request",
        claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256="b" * 64,
        now=NOW,
    )
    driver.drain(max_actions=1, now=NOW)

    result = driver.drain(max_actions=1, now=NOW)

    assert result.attempted == 1
    assert result.pending == 1
    assert result.completed == 0
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert rows[1]["state"] == "pending"
    assert rows[1]["lease_token"] == ""


def _review_packet_sha256(lens: str) -> str:
    """The REVIEW-PACKET digest, which is never the attempt-manifest digest.

    The chain identity's ``packet_sha256`` ("a" * 64 throughout this module) is
    the digest of the attempt-artifact MANIFEST; a reviewer receipt carries the
    digest of the review PACKET, recomputed by
    ``quality_reviewer.verify_reviewer_receipt`` over the packet body -- and the
    packet is per lens, so the three lenses do not even share one. Putting the
    same constant on both sides hid a comparison that could never hold in
    production: measured over the live store, 0 of 627 chain packet digests
    appear among the 958 distinct receipt digests. Two different realistic
    digests are what catches that.
    """
    return hashlib.sha256(f"review-packet:target-request:{lens}".encode()).hexdigest()


def _review_status(
    lens: str = "correctness", *, findings: list[dict] | None = None,
    binding_packet_sha256: str | None = None,
    candidate_sha256: str = "b" * 64,
    reviewer_request: str | None = None,
    reviewer_task: str | None = None,
    provider: str | None = None,
    route: dict[str, str] | None = None,
) -> dict:
    route = dict(route or ROUTE)
    provider = provider or route["adapter_id"]
    reviewer_request = reviewer_request or "review-request-" + lens
    reviewer_task = reviewer_task or review_orchestrator.ReviewOrchestrator._reviewer_task_id(
        {
            "schema_id": "aiworkhub.review_lifecycle.v1",
            "target_task_id": "TARGET", "target_request_id": "target-request",
            "claim_epoch": "1", "packet_sha256": "a" * 64,
            "candidate_sha256": candidate_sha256,
        },
        lens,
    )
    packet_sha256 = _review_packet_sha256(lens)
    receipt = {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": packet_sha256,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 1},
        "reviewer": {
            "request_id": reviewer_request,
            "task_id": reviewer_task,
            "provider": provider,
        },
        "report": {
            "lens": lens, "provider": provider, "read_only": True,
            "can_mutate_repo": False, "findings": list(findings or []),
        },
        "authority": {
            "process_identity_verified": True, "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": hashlib.sha256(b"submission").hexdigest(),
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }
    # The reviewer card's own record of the packet THIS repository sealed for
    # THIS reviewer -- the receipt digest's real counterpart. Measured on the
    # live store: it equals the receipt digest in 2,219 of 2,219 stored reviewer
    # cards, with the lens matching in all 2,219.
    binding = {
        "lens": lens,
        "packet_sha256": (
            packet_sha256 if binding_packet_sha256 is None else binding_packet_sha256
        ),
        "target_request_id": "target-request",
        "target_task_id": "TARGET",
        "target_claim_epoch": 1,
    }
    return {
        "ok": True, "state": "review_ready",
        "request_id": reviewer_request, "task_id": reviewer_task,
        "runner": route["runner"], "adapter_id": route["adapter_id"],
        "model": route["model"],
        "latest_event": {"quality_review_receipt": receipt},
        "task_card": {
            "terminal_review": {
                "evidence": {
                    "quality_review": binding,
                    "quality_review_receipt": receipt,
                }
            }
        },
    }


def test_actionable_finding_completes_reviewer_accept_for_manager_decision(
    tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    manager.status_result = _review_status(
        findings=[{"disposition": "defect", "actionable": True}]
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert result.failed == 0
    assert manager.accepts == [
        (
            "review-request-correctness",
            review_orchestrator.ReviewOrchestrator._reviewer_task_id(
                {
                    "schema_id": "aiworkhub.review_lifecycle.v1",
                    "target_task_id": "TARGET",
                    "target_request_id": "target-request",
                    "claim_epoch": "1",
                    "packet_sha256": "a" * 64,
                    "candidate_sha256": "b" * 64,
                },
                "correctness",
            ),
        )
    ]
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert rows[1]["state"] == "completed"
    assert json.loads(rows[1]["receipt_json"])["actionable_findings"] is True


def test_terminal_receipt_card_event_mismatch_fails_closed(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    status = _review_status()
    status["task_card"]["terminal_review"]["evidence"]["quality_review_receipt"] = {}
    with pytest.raises(RuntimeError, match="reviewer_terminal_receipt_mismatch"):
        driver._review_receipt(
            chain.actions[1], status, "review-request-correctness",
            review_orchestrator.ReviewOrchestrator._reviewer_task_id(
                chain.chain_identity, "correctness"
            ),
        )


def test_terminal_receipt_uses_durable_card_when_process_event_omits_copy(
    tmp_path: Path,
) -> None:
    driver, chain = _receipt_chain(tmp_path)
    status = _review_status()
    expected = status["task_card"]["terminal_review"]["evidence"][
        "quality_review_receipt"
    ]
    status["latest_event"].pop("quality_review_receipt")

    assert _verify_receipt(driver, chain, status) == expected


def _receipt_chain(tmp_path: Path) -> tuple[object, object]:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return driver, chain


def _verify_receipt(driver, chain, status: dict) -> dict:
    return driver._review_receipt(
        chain.actions[1], status, "review-request-correctness",
        review_orchestrator.ReviewOrchestrator._reviewer_task_id(
            chain.chain_identity, "correctness"
        ),
    )


def test_reviewer_receipt_binds_to_the_review_packet_not_the_attempt_manifest(
    tmp_path: Path,
) -> None:
    """The two digests are of two different objects and must not be conflated.

    ``chain_identity["packet_sha256"]`` is the attempt-artifact MANIFEST digest;
    the receipt carries the review PACKET digest. Comparing them refused every
    real receipt -- measured over the live store, 0 of 627 chain digests appear
    among the 958 distinct receipt digests, and of the 103 receipts whose target
    request owns a chain, 0 matched and 103 differed. The check now compares the
    receipt against the reviewer card's own record of the packet this repository
    sealed for it, which is the digest it is actually a receipt for.
    """
    driver, chain = _receipt_chain(tmp_path)
    status = _review_status()
    packet_sha256 = _review_packet_sha256("correctness")

    assert packet_sha256 != chain.chain_identity["packet_sha256"]
    assert _verify_receipt(driver, chain, status)["packet_sha256"] == packet_sha256

    # The exact shape the old comparison demanded: a receipt claiming the
    # attempt-manifest digest, which no reviewer can ever produce.
    manifest_digest = _review_status()
    for holder in (
        manifest_digest["latest_event"]["quality_review_receipt"],
        manifest_digest["task_card"]["terminal_review"]["evidence"][
            "quality_review_receipt"
        ],
    ):
        holder["packet_sha256"] = chain.chain_identity["packet_sha256"]
    with pytest.raises(RuntimeError, match="reviewer_receipt_binding_invalid"):
        _verify_receipt(driver, chain, manifest_digest)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({}, "reviewer_packet_binding_missing"),
        ({"packet_sha256": "c" * 64}, "reviewer_receipt_binding_invalid"),
        ({"packet_sha256": "not-a-digest"}, "reviewer_packet_binding_invalid"),
        ({"lens": "security"}, "reviewer_packet_binding_invalid"),
        ({"target_request_id": "other-request"}, "reviewer_packet_binding_invalid"),
        ({"target_claim_epoch": 9}, "reviewer_packet_binding_invalid"),
    ],
)
def test_reviewer_packet_binding_must_name_this_chain_and_this_lens(
    tmp_path: Path, mutation: dict, reason: str
) -> None:
    """A receipt is evidence only for the packet it was written against."""
    driver, chain = _receipt_chain(tmp_path)
    status = _review_status()
    evidence = status["task_card"]["terminal_review"]["evidence"]
    if mutation:
        evidence["quality_review"].update(mutation)
    else:
        evidence.pop("quality_review")
    with pytest.raises(RuntimeError, match=reason):
        _verify_receipt(driver, chain, status)


def test_automatic_chain_stops_at_acceptance_and_never_accepts_the_target(
    monkeypatch, tmp_path: Path
) -> None:
    """Fixing the launch check must not hand acceptance to the orchestrator.

    The nine reviewer actions complete automatically, then one authenticated
    manager-ready receipt is sealed.  Archival waits while the target remains
    in review and no target acceptance call is made by the orchestrator.
    """
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda _repo, task_id, **_kwargs: {"ok": True, "task_id": task_id},
    )
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "gated.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    for lens in review_orchestrator.LENSES:
        manager.status_results["review-request-" + lens] = _review_status(lens)

    assert review_orchestrator.AUTOMATIC_TARGET_ACCEPT_ENABLED is False
    for _ in range(9):
        assert driver.drain(max_actions=1, now=NOW).completed == 1
    manager_ready = driver.drain(max_actions=1, now=NOW)
    cleanup = driver.drain(max_actions=2, now=NOW)

    assert manager_ready.completed == 1
    assert manager_ready.failed == 0
    assert cleanup.completed == 2
    assert cleanup.pending == 0
    assert manager.accepts == [
        ("review-request-" + lens, task_id)
        for lens, task_id in zip(
            review_orchestrator.LENSES,
            [row["reviewer_task_id"] for row in manager.launches],
        )
    ]
    assert ("target-request", "TARGET") not in manager.accepts
    rows = review_lifecycle.rows_for_test(tmp_path / "gated.sqlite")
    assert rows[9]["action_type"] == "target_accept"
    assert rows[9]["state"] == "completed"
    aggregate = json.loads(rows[9]["receipt_json"])["manager_ready"]
    assert aggregate["schema_id"] == review_lifecycle.MANAGER_READY_SCHEMA_ID
    assert aggregate["lenses"] == list(review_orchestrator.LENSES)
    assert len(aggregate["reviews"]) == 3


@pytest.mark.parametrize(
    ("required_reviewer_lenses", "effective_tier", "expected_lenses"),
    [
        (None, "", list(review_orchestrator.LENSES)),
        ([], "low", []),
    ],
    ids=("unplanned-fail-closed", "low-zero-reviewers"),
)
def test_canonical_chain_publishes_one_manager_callback_after_all_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    required_reviewer_lenses: list[str] | None,
    effective_tier: str,
    expected_lenses: list[str],
) -> None:
    """The manager wake is a projection of the completed quality chain only."""

    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda _repo, task_id, **_kwargs: {"ok": True, "task_id": task_id},
    )
    task_store.initialize_repository(tmp_path)
    _readiness, db_path = task_store._require_ready(tmp_path)
    now = NOW.isoformat()
    card = {
        "task_id": "TARGET",
        "runner": "glm53_worker",
        "topic": "task_mcp",
        "status": "review",
        "claim_epoch": 1,
        "coordinator_provider": "codex",
        "origin_thread_id": "thread-manager-ready",
        "terminal_substatus": "review_ready",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": "target-request",
                    "task_id": "TARGET",
                    "runner": "glm53_worker",
                }
            },
        },
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?, ?, 'task_mcp', 'review', 'review', '', '', ?, ?, ?, ?, ?)",
            (
                "TARGET",
                "glm53_worker",
                json.dumps(card, sort_keys=True),
                now,
                now,
                "glm53_worker",
                "thread-manager-ready",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request",
        claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256="b" * 64,
        now=NOW,
        required_reviewer_lenses=required_reviewer_lenses,
        effective_tier=effective_tier,
    )
    for lens in expected_lenses:
        manager.status_results["review-request-" + lens] = _review_status(lens)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM callback_outbox").fetchone()[0] == 0
    finally:
        conn.close()
    for _ in range(10):
        assert driver.drain(max_actions=1, now=NOW).completed == 1

    stored = task_store.get_task(tmp_path, "TARGET")
    marker = task_store.manager_ready_marker(stored or {})
    assert marker is not None
    assert marker["manager_ready"]["lenses"] == expected_lenses
    assert [launch["lens"] for launch in manager.launches] == expected_lenses
    conn = sqlite3.connect(db_path)
    try:
        callback = conn.execute(
            "SELECT transition, request_id, episode_id FROM callback_outbox"
        ).fetchall()
        manager_events = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id='TARGET' AND event='manager_ready'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert callback == [("review_ready", "target-request", "1")]
    assert manager_events == 1

    cleanup = driver.drain(max_actions=12, now=NOW)
    assert cleanup.completed == 2
    assert cleanup.pending == 0
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM callback_outbox").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id='TARGET' AND event='manager_ready'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_stale_manager_ready_receipt_cannot_starve_current_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded completed chain is quarantined without widening failures."""

    task_store.initialize_repository(tmp_path)
    _readiness, db_path = task_store._require_ready(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        _Manager(tmp_path), db_path=db_path, route_selector=_route,
    )
    receipts = [
        {
            "manager_ready": {
                "chain_id": 41,
                "target_task_id": "TARGET",
                "target_request_id": "stale-request",
                "claim_epoch": "1",
            },
        },
        {
            "manager_ready": {
                "chain_id": 42,
                "target_task_id": "TARGET",
                "target_request_id": "current-request",
                "claim_epoch": "2",
            },
        },
    ]
    monkeypatch.setattr(
        review_orchestrator.review_lifecycle,
        "completed_manager_ready_receipts",
        lambda _db_path: receipts,
    )
    calls: list[str] = []

    def publish(_repo, *, task_id, request_id, claim_epoch):
        assert task_id == "TARGET"
        assert claim_epoch in {"1", "2"}
        calls.append(request_id)
        if request_id == "stale-request":
            return False, "manager_ready_target_identity_mismatch", False
        return True, "manager_ready", True

    monkeypatch.setattr(review_orchestrator.task_store, "publish_manager_ready", publish)

    assert driver._publish_completed_manager_ready() == 1
    assert calls == ["stale-request", "current-request"]
    assert driver._publish_completed_manager_ready() == 0
    assert calls == ["stale-request", "current-request"]


def test_happy_path_is_exactly_ordered_and_hands_cleanup_to_manager(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _Manager(tmp_path)
    archived: list[str] = []
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda _repo, task_id, **_kwargs: archived.append(task_id) or {"ok": True},
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    for lens in review_orchestrator.LENSES:
        manager.status_results["review-request-" + lens] = _review_status(lens)

    for _ in range(10):
        result = driver.drain(max_actions=1, now=NOW)
        rows_now = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
        failed = [row["failure_reason"] for row in rows_now if row["state"] == "failed"]
        assert result.completed == 1, failed
        assert result.failed == 0
    for _ in range(2):
        result = driver.drain(max_actions=1, now=NOW)
        assert result.completed == 1
        assert result.failed == 0
    exhausted = driver.drain(max_actions=1, now=NOW)

    assert exhausted.attempted == 0
    assert [row["lens"] for row in manager.launches] == list(review_orchestrator.LENSES)
    assert [row["runner"] for row in manager.launches] == [ROUTE["runner"]] * 3
    assert ("target-request", "TARGET") not in manager.accepts
    assert "TARGET" not in archived
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert [row["state"] for row in rows] == ["completed"] * 12
    assert rows[11]["action_type"] == "needfix_close"
    assert rows[11]["state"] == "completed"


def test_needfix_close_is_a_manager_owned_handoff(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    monkeypatch.setattr(
        driver,
        "_receipts",
        lambda _chain_id: [
            {
                "lens": lens,
                "action_type": "accept",
                "reviewer_request_id": "review-request-" + lens,
            }
            for lens in review_orchestrator.LENSES
        ],
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    action = chain.actions[11]
    receipt = driver._execute(action)

    assert receipt is not None
    assert receipt["result"] == {
        "ok": True,
        "state": "manager_owned",
        "task_id": "TARGET",
        "request_id": "target-request",
    }


_CATALOG = {"schema_id": "aiworkhub.workforce_catalog.v1", "workers": [{"worker_id": "gpt-5.5"}]}


def _ready_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "storage_readiness",
        lambda _repo: SimpleNamespace(
            ready=True, reason="ready", repo_id="repo-test", canonical_db="queue.sqlite"
        ),
    )


def _capture_rank(monkeypatch, captured: list) -> None:
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "rank_task",
        lambda _repo, task, *, catalog=None: (
            captured.append((task, catalog)) or {"launch_contract": dict(ROUTE)}
        ),
    )


def test_default_route_uses_canonical_workforce_contract(monkeypatch, tmp_path: Path) -> None:
    captured: list = []
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, captured)
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: dict(_CATALOG),
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_EXACT", "security"
    )

    assert route == ROUTE
    task, catalog = captured[0]
    assert task.kinds == frozenset({"review"})
    assert task.risk == "high"
    assert "session-manager" in task.tool_needs
    # The point of this wiring: ranking sees the evidenced catalog, and no
    # longer falls through to build_catalog's empty process/usage defaults.
    assert catalog == _CATALOG


def test_default_route_real_rank_falls_back_from_unavailable_critical_route(
    monkeypatch, tmp_path: Path,
) -> None:
    """The production ranker must admit a high-capability read-only reviewer."""
    tools = ["source-graph", "session-manager", "ai-memory", "kb"]

    def worker(
        worker_id: str, adapter_id: str, model: str, provider: str,
        max_risk: str, *, available: bool,
    ) -> dict:
        return {
            "worker_id": worker_id,
            "adapter_id": adapter_id,
            "model": model,
            "provider": provider,
            "supports": ["code", "research", "review"],
            "tools": tools,
            "max_context_tokens": 1_000_000,
            "max_risk": max_risk,
            "quality_ceiling": 0.97,
            "manager_score_adjustment": 0.0,
            "available": available,
            "outcomes": {"sample_count": 0},
        }

    catalog = {
        "schema_id": "aiworkhub.workforce_catalog.v1",
        "workers": [
            worker(
                "claude-opus-5", "vscode_lm", "claude-opus-5", "anthropic",
                "critical", available=False,
            ),
            worker(
                "grok-4.6", "grok_kilo_cli", "xai/grok-4.6", "xai",
                "high", available=True,
            ),
            worker(
                "glm-5.2", "glm_vscode_lm", "glm-5.2", "zhipu",
                "high", available=True,
            ),
            worker(
                "deepseek-v4-pro", "deepseek_vscode_lm", "deepseek-v4-pro",
                "deepseek", "high", available=True,
            ),
        ],
    }
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    monkeypatch.setattr(
        review_orchestrator, "routing_catalog", lambda _repo: catalog,
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_HIGH_FAILOVER", "correctness",
        excluded_routes=frozenset({
            ("copilot_claude-opus-5", "vscode_lm", "claude-opus-5"),
        }),
    )

    assert route["adapter_id"] in {
        "grok_kilo_cli", "glm_vscode_lm", "deepseek_vscode_lm",
    }
    assert route["model"] in {"xai/grok-4.6", "glm-5.2", "deepseek-v4-pro"}

    critical_task = review_orchestrator.workforce_router.TaskRequirements.build(
        task_id="QUALITY_REVIEW_CRITICAL",
        repo_id="repo-test",
        kinds=("review",),
        risk="critical",
        tool_needs=tools,
    )
    critical_rank = review_orchestrator.workforce_catalog.rank_task(
        tmp_path, critical_task, catalog=catalog,
    )
    assert critical_rank["launch_contract"] is None
    assert all(
        "risk_exceeds_worker_limit" in candidate["exclusion_reasons"]
        for candidate in critical_rank["candidates"]
        if candidate["model"] != "claude-opus-5"
    )


def test_default_route_skips_manager_codex_cli_but_keeps_copilot_gpt(
    monkeypatch, tmp_path: Path,
) -> None:
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "rank_task",
        lambda *_args, **_kwargs: {
            "launch_contract": {
                "runner": "codex_gpt-5.6-sol",
                "adapter_id": "codex_cli",
                "model": "gpt-5.6-sol",
            },
            "candidates": [
                {
                    "execution_runner": "codex_gpt-5.6-sol",
                    "adapter_id": "codex_cli",
                    "model": "gpt-5.6-sol",
                    "excluded": False,
                },
                {
                    "execution_runner": "copilot_gpt-5.6-sol",
                    "adapter_id": "vscode_lm",
                    "model": "gpt-5.6-sol",
                    "excluded": False,
                },
            ],
        },
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: dict(_CATALOG),
    )

    assert review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_EXACT", "correctness"
    ) == {
        "runner": "copilot_gpt-5.6-sol",
        "adapter_id": "vscode_lm",
        "model": "gpt-5.6-sol",
    }


def test_default_route_skips_the_exact_retired_route_identity(
    monkeypatch, tmp_path: Path,
) -> None:
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "rank_task",
        lambda *_args, **_kwargs: {
            "launch_contract": dict(FIRST_ROUTE),
            "candidates": [
                {
                    **FIRST_ROUTE,
                    "execution_runner": FIRST_ROUTE["runner"],
                    "excluded": False,
                },
                {
                    **SUCCESSOR_ROUTE,
                    "execution_runner": SUCCESSOR_ROUTE["runner"],
                    "excluded": False,
                },
            ],
        },
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: dict(_CATALOG),
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_SUCCESSOR", "correctness",
        excluded_routes=frozenset({
            review_orchestrator._review_route_identity(FIRST_ROUTE)
        }),
    )

    assert route == SUCCESSOR_ROUTE


def test_reviewer_route_still_selects_when_the_catalog_build_raises(
    monkeypatch, tmp_path: Path
) -> None:
    """A reviewer chosen on the prior beats a reviewer that never launches."""
    captured: list = []
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, captured)

    def _explode(_repo):
        raise OSError("process ledger unreadable")

    monkeypatch.setattr(
        review_orchestrator.workforce_catalog, "build_routing_catalog", _explode
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_EXACT", "security"
    )

    assert route == ROUTE
    # catalog=None makes rank_task rebuild the bare catalog itself, which is
    # exactly the conservative-prior ranking used before this was wired.
    assert captured[0][1] is None


def test_a_catalog_build_that_raises_anything_never_stops_the_review(
    monkeypatch, tmp_path: Path
) -> None:
    """Every failure mode of the ledger chain degrades to the prior, not to no review."""
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    for failure in (
        OSError("io"),
        sqlite3.Error("db"),
        ValueError("malformed row"),
        ImportError("dashboard mid-edit"),
        SyntaxError("module being edited"),
        RuntimeError("anything at all"),
    ):
        captured: list = []
        _capture_rank(monkeypatch, captured)
        review_orchestrator.reset_routing_catalog_cache()

        def _explode(_repo, _exc=failure):
            raise _exc

        monkeypatch.setattr(
            review_orchestrator.workforce_catalog, "build_routing_catalog", _explode
        )

        route = review_orchestrator.select_reviewer_route(
            tmp_path, "QUALITY_REVIEW_EXACT", "security"
        )

        assert route == ROUTE, failure
        assert captured[0][1] is None, failure


@pytest.mark.parametrize(
    "unusable",
    [None, {}, [], "catalog", {"workers": []}, {"workers": None}, {"no_workers": 1}],
)
def test_an_unusable_catalog_never_reaches_rank_task(
    monkeypatch, tmp_path: Path, unusable
) -> None:
    """A truthy catalog with no workers ranks zero candidates and kills the launch."""
    captured: list = []
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, captured)
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: unusable,
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_EXACT", "security"
    )

    assert route == ROUTE
    assert captured[0][1] is None


def test_an_unusable_catalog_is_never_cached_as_if_it_were_good(
    monkeypatch, tmp_path: Path
) -> None:
    """A failed build must not poison the memo for the rest of the pass."""
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, [])
    builds: list[int] = []

    def _fail_then_succeed(_repo):
        builds.append(1)
        if len(builds) == 1:
            raise OSError("transient")
        return dict(_CATALOG)

    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        _fail_then_succeed,
    )

    assert review_orchestrator.routing_catalog(tmp_path) is None
    assert review_orchestrator.routing_catalog(tmp_path) == _CATALOG
    assert len(builds) == 2


def test_the_routing_catalog_is_built_once_per_pass_not_once_per_action(
    monkeypatch, tmp_path: Path
) -> None:
    """12 actions in a drain pass must not pay the measured +1.49s twelve times."""
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, [])
    builds: list[int] = []
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: builds.append(1) or dict(_CATALOG),
    )

    for _ in range(12):
        review_orchestrator.select_reviewer_route(tmp_path, "QUALITY_REVIEW", "security")

    assert len(builds) == 1


def test_each_drain_pass_starts_from_a_fresh_routing_catalog(tmp_path: Path) -> None:
    """Evidence may go stale within a pass, never across one."""
    review_orchestrator.reset_routing_catalog_cache()
    review_orchestrator._ROUTING_CATALOG_CACHE["primed"] = (0.0, dict(_CATALOG))
    driver = review_orchestrator.ReviewOrchestrator(
        _Manager(tmp_path), db_path=tmp_path / "review.sqlite", route_selector=_route
    )

    driver.drain(max_actions=1, now=NOW)

    assert review_orchestrator._ROUTING_CATALOG_CACHE == {}


def test_workspace_binding_commits_and_closes_its_connection(
    monkeypatch, tmp_path: Path
) -> None:
    """The two workspace-binding sites must close their connection at block exit
    while still committing the binding (the write site relied on ``__exit__``
    to commit, so the transaction boundary has to be preserved)."""
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "bindings.sqlite", route_selector=_route
    )

    real_connect = sqlite3.connect
    open_conns: list[sqlite3.Connection] = []

    class _Tracked(sqlite3.Connection):
        def close(self) -> None:
            if self in open_conns:
                open_conns.remove(self)
            super().close()

    def _tracking_connect(*args, **kwargs):
        kwargs["factory"] = _Tracked
        conn = real_connect(*args, **kwargs)
        open_conns.append(conn)
        return conn

    monkeypatch.setattr(review_orchestrator.sqlite3, "connect", _tracking_connect)

    driver._repair_expected_workspace(7, "workspace-xyz")
    assert open_conns == [], "write site left a sqlite connection open"

    # Commit was preserved: the just-written binding is durably readable.
    assert driver._expected_workspace_identity(7) == "workspace-xyz"
    assert open_conns == [], "read site left a sqlite connection open"


def test_archive_status_uses_canonical_task_envelope(monkeypatch, tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "show_task",
        lambda _repo, task_id: {
            "returncode": 0,
            "stdout": '{"task_id":"' + task_id + '","status":"finished",'
            '"archived_at":"2026-08-29T00:00:00Z"}',
        },
    )

    assert driver._is_archived("REVIEWER") is True


def _show(status_by_task: dict) -> object:
    def show(_repo: Path, task_id: str) -> dict:
        if task_id not in status_by_task:
            return {"returncode": 1, "stdout": ""}
        return {"returncode": 0, "stdout": json.dumps(status_by_task[task_id])}

    return show


@pytest.mark.parametrize(
    "card",
    [
        {"task_id": "TARGET", "status": "finished", "archived_at": "2026-08-29T00:00:00Z"},
        {"task_id": "TARGET", "status": "superseded", "worker_status": "superseded"},
        {"task_id": "TARGET", "status": "blocked"},
        {"task_id": "TARGET", "status": "finished"},
    ],
)
def test_an_action_whose_target_left_review_retires_instead_of_failing(
    monkeypatch, tmp_path: Path, card: dict
) -> None:
    """A decided target cannot be driven through review -- that is not a failure.

    Failing it was: a failed action parks every later action in its chain, so
    one moot launch stranded eleven more. Measured on this repository: 129
    chains and 1,389 actions permanently unreservable, and every one of the 39
    chains still live targeted an already-decided card.
    """
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    monkeypatch.setattr(review_orchestrator.task_engine, "show_task", _show({"TARGET": card}))

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert result.failed == 0
    assert manager.launches == [], "a decided target must not spawn a reviewer"
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert rows[0]["state"] == "completed"
    receipt = json.loads(rows[0]["receipt_json"])
    assert receipt["obsolete_reason"].startswith("target_left_review:")
    assert receipt["result"]["state"] == "obsolete"


def test_a_reviewable_target_is_still_driven(monkeypatch, tmp_path: Path) -> None:
    """Retirement must not swallow live work."""
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    monkeypatch.setattr(
        review_orchestrator.task_engine, "show_task",
        _show({"TARGET": {"task_id": "TARGET", "status": "review"}}),
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert len(manager.launches) == 1, "a target still in review must be launched"


def test_an_unreadable_card_is_never_treated_as_decided(monkeypatch, tmp_path: Path) -> None:
    """Fail closed: not knowing is not the same as knowing it is over."""
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    monkeypatch.setattr(
        review_orchestrator.task_engine, "show_task",
        lambda _repo, _task_id: {"returncode": 1, "stdout": ""},
    )
    assert driver._target_left_review("TARGET") == ""

    monkeypatch.setattr(
        review_orchestrator.task_engine, "show_task",
        lambda _repo, _task_id: {"returncode": 0, "stdout": "not json"},
    )
    assert driver._target_left_review("TARGET") == ""


def test_needfix_close_is_not_retired_by_a_decided_target() -> None:
    """Bookkeeping outlives the review it followed.

    needfix_close resolves NeedFix rows linked to the target. That stays
    meaningful once the target is accepted or archived, so it is deliberately
    absent from the driving set.
    """
    assert "needfix_close" not in review_orchestrator.REVIEW_DRIVING_ACTIONS
    assert review_orchestrator.REVIEW_DRIVING_ACTIONS == {
        "launch", "accept", "archive", "manager_ready", "target_accept",
    }


def test_needfix_close_survives_a_chain_whose_reviewers_were_retired(
    monkeypatch, tmp_path: Path
) -> None:
    """Bookkeeping must not die of a dependency it never had.

    reviewer_request_ids were hoisted above the action branch, so needfix_close
    -- which never reads them -- raised KeyError as soon as the reviewer
    actions ahead of it were retired as obsolete. Measured live the moment
    retirement landed.
    """
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    monkeypatch.setattr(
        review_orchestrator.task_engine, "show_task",
        _show({"TARGET": {"task_id": "TARGET", "status": "finished",
                          "archived_at": "2026-08-29T00:00:00Z"}}),
    )
    monkeypatch.setattr(
        review_orchestrator.needfix_store, "list_needfix",
        lambda _repo, **_kwargs: [],
    )

    # eleven retirements, then the bookkeeping action itself
    result = driver.drain(max_actions=12, now=NOW)

    assert result.failed == 0, "no action in a retired chain may fail"
    assert result.completed == 12
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert [r["state"] for r in rows] == ["completed"] * 12
    last = json.loads(rows[-1]["receipt_json"])
    assert last["action_type"] == "needfix_close"
    assert "obsolete_reason" not in last, "bookkeeping ran, it was not retired"


# --- mechanical short-circuit ------------------------------------------------
#
# 92.3% of this repository's 2,470 reject_review events were mechanically
# decidable, yet every one spent a quality-reviewer launch (avg 453,650 input
# tokens) to discover. The deterministic verdict that decides them is already
# computed and already on the card; these tests pin that the queue reads it,
# and -- far more important -- that it reads it ONE-DIRECTIONALLY. A wrong
# short-circuit lets defective code through unreviewed, which is much worse
# than the token waste being fixed, so every ambiguous verdict must still
# launch the reviewer.


def _verdict(
    *,
    applicable: object = True,
    passed: object = False,
    claim_epoch: object = "1",
    nothing_measured: object = False,
    failed: object = 2,
    missing: object = 0,
) -> dict:
    """Build a deterministic_verification exactly as task_fsm emits one."""
    return {
        "applicable": applicable,
        "pass": passed,
        "substatus": "review_ready",
        "reason": "evidence_verdict_failed",
        "claim_epoch": claim_epoch,
        "evidence_verdict": {
            "passed": False,
            "nothing_measured": nothing_measured,
            "validation_count": 3,
            "failed_validation_count": failed,
            "required_output_count": 1,
            "missing_required_output_count": missing,
        },
    }


def _drive(tmp_path: Path, db: str, **card_overrides: object):
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(**card_overrides)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / db, route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return manager, driver, driver.drain(max_actions=1, now=NOW)


def test_mechanically_failing_candidate_never_spends_a_reviewer_launch(
    tmp_path: Path,
) -> None:
    manager, _driver, result = _drive(
        tmp_path, "mech.sqlite", deterministic_verification=_verdict()
    )

    assert manager.launches == [], "a measured red candidate must not be reviewed"
    assert result.failed == 1
    row = review_lifecycle.rows_for_test(tmp_path / "mech.sqlite")[0]
    assert "mechanically_failing_candidate" in row["failure_reason"]
    # The reason is a measurement, not a label: it names the counts it read.
    assert "failed_validation_count=2" in row["failure_reason"]

    # The target's own event stream explains the transition too.
    event = manager.events[-1]
    assert event["event_type"] == "review_orchestrator_mechanical_rework"
    assert event["task_id"] == "TARGET"
    automation = event["review_automation"]
    assert automation["state"] == "mechanical_rework"
    assert automation["reviewer_launched"] is False
    assert automation["disposition"] == "return_for_rework"
    assert "mechanically_failing_candidate" in automation["reason"]


def test_missing_required_output_alone_short_circuits(tmp_path: Path) -> None:
    """The 8.4% unwired/required-output-unchanged class is mechanical too."""
    manager, _driver, result = _drive(
        tmp_path, "missing.sqlite",
        deterministic_verification=_verdict(failed=0, missing=3),
    )

    assert manager.launches == []
    assert result.failed == 1
    reason = review_lifecycle.rows_for_test(tmp_path / "missing.sqlite")[0]["failure_reason"]
    assert "missing_required_output_count=3" in reason


def test_verdict_on_terminal_review_is_read_like_core_reads_it(tmp_path: Path) -> None:
    """core.py prefers terminal_review's copy; so must the queue."""
    manager, _driver, result = _drive(
        tmp_path, "nested.sqlite",
        terminal_review={"deterministic_verification": _verdict()},
    )

    assert manager.launches == []
    assert result.failed == 1


def test_absent_verdict_still_launches_the_reviewer(tmp_path: Path) -> None:
    """No verdict is not a failing verdict. It is no measurement at all."""
    manager, _driver, result = _drive(tmp_path, "absent.sqlite")

    assert len(manager.launches) == 1, "an unmeasured candidate must be reviewed"
    assert result.completed == 1


def test_nothing_measured_verdict_still_launches_the_reviewer(tmp_path: Path) -> None:
    """The exact inversion the fail-closed doctrine forbids.

    ``nothing_measured`` means no validation ran and no required output was
    checked. Short-circuiting on it would silently convert "we did not
    measure" into "it passed" -- and it is precisely the candidate that most
    needs a human-grade review.
    """
    manager, _driver, result = _drive(
        tmp_path, "vacuum.sqlite",
        deterministic_verification=_verdict(
            nothing_measured=True, failed=0, missing=0
        ),
    )

    assert len(manager.launches) == 1
    assert result.completed == 1


def test_nothing_measured_still_launches_even_with_a_nonzero_count(
    tmp_path: Path,
) -> None:
    """A self-contradicting verdict is unreadable, so it gets a reviewer."""
    manager, _driver, result = _drive(
        tmp_path, "contradiction.sqlite",
        deterministic_verification=_verdict(nothing_measured=True, failed=5),
    )

    assert len(manager.launches) == 1
    assert result.completed == 1


def test_stale_verdict_from_another_claim_still_launches_the_reviewer(
    tmp_path: Path,
) -> None:
    """A verdict about a PREVIOUS attempt must never decide this one.

    The chain is bound to claim_epoch 1; this verdict was recorded against
    claim_epoch 0, so it is evidence about code that has since been reworked.
    """
    manager, _driver, result = _drive(
        tmp_path, "stale.sqlite",
        deterministic_verification=_verdict(claim_epoch="0"),
    )

    assert len(manager.launches) == 1
    assert result.completed == 1


@pytest.mark.parametrize(
    ("label", "verdict"),
    [
        ("not_a_mapping", "evidence_verdict_failed"),
        ("empty", {}),
        ("applicable_missing", {"pass": False, "claim_epoch": "1"}),
        ("applicable_false", _verdict(applicable=False)),
        ("applicable_truthy_not_true", _verdict(applicable=1)),
        ("pass_missing", {"applicable": True, "claim_epoch": "1"}),
        ("pass_none", _verdict(passed=None)),
        ("pass_true", _verdict(passed=True)),
        ("evidence_verdict_missing", {
            "applicable": True, "pass": False, "claim_epoch": "1",
        }),
        ("evidence_verdict_not_a_mapping", {
            "applicable": True, "pass": False, "claim_epoch": "1",
            "evidence_verdict": [1, 2, 3],
        }),
        ("nothing_measured_missing", {
            "applicable": True, "pass": False, "claim_epoch": "1",
            "evidence_verdict": {"failed_validation_count": 4},
        }),
        ("counts_all_zero", _verdict(failed=0, missing=0)),
        ("count_is_a_bool", _verdict(failed=True, missing=False)),
        ("count_is_a_string", _verdict(failed="7", missing="0")),
        ("count_is_negative", _verdict(failed=-3, missing=0)),
        ("count_is_a_float", _verdict(failed=2.0, missing=0)),
    ],
)
def test_unreadable_or_unmeasured_verdicts_always_launch(
    tmp_path: Path, label: str, verdict: object
) -> None:
    """Every ambiguous shape falls through to a real review.

    None of these is a positive, explicit, measured mechanical failure, so
    none of them may spend a card's review. When in doubt, launch.
    """
    manager, _driver, result = _drive(
        tmp_path, f"fallthrough-{label}.sqlite",
        deterministic_verification=verdict,
    )

    assert len(manager.launches) == 1, f"{label} must still be reviewed"
    assert result.completed == 1


def test_mechanical_verdict_never_overrides_an_identity_mismatch(
    tmp_path: Path,
) -> None:
    """Identity is checked first; a verdict cannot mask a tampered card."""
    manager, _driver, result = _drive(
        tmp_path, "identity-first.sqlite",
        candidate_sha256="c" * 64,
        deterministic_verification=_verdict(),
    )

    assert manager.launches == []
    assert result.failed == 1
    reason = review_lifecycle.rows_for_test(
        tmp_path / "identity-first.sqlite"
    )[0]["failure_reason"]
    assert "target_candidate_identity_invalid" in reason
    assert "mechanically_failing_candidate" not in reason


def test_mechanical_verdict_is_not_read_before_the_card_is_review_ready(
    tmp_path: Path,
) -> None:
    """A still-running card's verdict describes an unfinished attempt."""
    manager, _driver, result = _drive(
        tmp_path, "not-ready.sqlite",
        deterministic_verification=_verdict(),
    )
    assert result.failed == 1  # sanity: review_ready by default

    manager2 = _Manager(tmp_path)
    manager2.target_status = _target_status(
        state="processing", deterministic_verification=_verdict()
    )
    driver2 = review_orchestrator.ReviewOrchestrator(
        manager2, db_path=tmp_path / "processing.sqlite", route_selector=_route
    )
    driver2.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    pending = driver2.drain(max_actions=1, now=NOW)

    assert pending.pending == 1, "not-ready must defer, not mechanically reject"
    assert manager2.launches == []
    assert manager2.events[-1]["review_automation"]["reason"] == "target_not_review_ready"


def test_mechanical_failure_reason_is_pure_and_total() -> None:
    """The predicate never raises and never mutates, on any input."""
    reason = review_orchestrator.mechanical_failure_reason
    for hostile in (None, "", 0, [], (), object(), {"terminal_review": 5}):
        assert reason(hostile, "1") == ""
    card = {"deterministic_verification": _verdict()}
    snapshot = json.dumps(card, sort_keys=True)
    assert reason(card, "1").startswith("mechanically_failing_candidate:")
    assert json.dumps(card, sort_keys=True) == snapshot, "input was mutated"
    # An empty bound epoch must never match a recorded one.
    assert reason(card, "") == ""


def test_unknown_readiness_outcome_never_falls_through_to_a_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed on a readiness vocabulary the launch branch cannot read."""
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "unknown.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    monkeypatch.setattr(
        review_orchestrator.ReviewOrchestrator,
        "_launch_readiness",
        lambda self, action: {"outcome": "a_new_idea", "reason": "whatever"},
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert manager.launches == []
    assert result.failed == 1
    reason = review_lifecycle.rows_for_test(tmp_path / "unknown.sqlite")[0]["failure_reason"]
    assert "launch_readiness_outcome_unknown:a_new_idea" in reason


# --- returning the mechanically failing card for rework -----------------------
#
# Refusing to spend a reviewer is only half the fix: the card still has to
# LEAVE review, or the manager has to dispose of every one of them by hand.
# The orchestrator has no canonical write surface of its own -- importing core
# here would be circular -- so it asks the manager, which delegates to
# core.reject_review. Every test below is about the half that can go wrong: a
# rejection that is refused, that raises, or that the manager cannot perform at
# all. The one outcome none of them may produce is a card that left review with
# nothing saying why, or a card believed returned that was not.


class _RejectingManager(_Manager):
    """A manager whose ``reject_review`` really returns the card to pending."""

    def __init__(
        self, repo: Path, *, result: object = None, raises: bool = False
    ) -> None:
        super().__init__(repo)
        self.rejects: list[tuple] = []
        self.card = {"task_id": "TARGET", "status": "review", "worker_status": "review"}
        self._result = result
        self._raises = raises

    def reject_review(self, task_id, reason, *, to="pending"):
        self.rejects.append((task_id, reason, to))
        if self._raises:
            raise RuntimeError("canonical store is locked")
        if self._result is not None:
            return self._result
        self.card = {"task_id": task_id, "status": to, "worker_status": "unclaimed"}
        return {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps(self.card),
            "task_id": task_id,
            "to": to,
            "learning_commit_owed": {"outcome": "rejected"},
        }


def _drive_rejecting(tmp_path: Path, db: str, manager, monkeypatch, **card_overrides):
    """Drive one mechanically failing chain against a live-ish card reader."""
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "show_task",
        lambda _repo, task_id: (
            {"returncode": 0, "stdout": json.dumps(manager.card)}
            if task_id == "TARGET"
            else {"returncode": 1, "stdout": ""}
        ),
    )
    manager.target_status = _target_status(
        deterministic_verification=_verdict(), **card_overrides
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / db, route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return driver


def _failure_reason(db_path: Path) -> str:
    return str(review_lifecycle.rows_for_test(db_path)[0]["failure_reason"] or "")


def test_a_mechanically_failing_candidate_is_returned_to_pending_for_rework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card leaves review by itself, with the measurement as its reason."""
    manager = _RejectingManager(tmp_path)
    driver = _drive_rejecting(tmp_path, "returned.sqlite", manager, monkeypatch)

    result = driver.drain(max_actions=1, now=NOW)

    assert manager.launches == [], "a measured red candidate must not be reviewed"
    assert len(manager.rejects) == 1, "exactly one rejection, never a retry storm"
    task_id, reason, to = manager.rejects[0]
    assert task_id == "TARGET"
    assert to == "pending", "rework, not blocked -- blocked is not a quality signal"
    # The reason handed to the canonical store is the measurement itself.
    assert reason.startswith("mechanically_failing_candidate:")
    assert "failed_validation_count=2" in reason
    assert manager.card["status"] == "pending"

    # Both durable surfaces still explain the transition.
    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "returned.sqlite")
    assert "failed_validation_count=2" in outbox
    assert "returned_for_rework=returned" in outbox
    automation = manager.events[-1]["review_automation"]
    assert automation["rework_return"] == {"state": "returned", "detail": "pending"}
    assert automation["disposition"] == "return_for_rework"


def test_a_refused_rejection_still_records_the_reason_and_keeps_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejection that did not happen must never be reported as one.

    core refuses any move whose row is not still ``worker_status='review'``.
    When it does, the card stays exactly where it was and the manager disposes
    of it -- which costs one reviewer launch. Believing a refused rejection
    instead would leave the card in review with the chain saying it left, and
    a reader would have no way to tell. The refusal detail is carried verbatim.
    """
    manager = _RejectingManager(
        tmp_path,
        result={
            "ok": False,
            "returncode": 1,
            "stderr": "reject_not_reviewable:task_id=TARGET",
            "error": "reject_not_reviewable:task_id=TARGET",
        },
    )
    driver = _drive_rejecting(tmp_path, "refused.sqlite", manager, monkeypatch)

    result = driver.drain(max_actions=1, now=NOW)

    assert len(manager.rejects) == 1
    assert manager.card["status"] == "review", "a refused rejection moves nothing"
    assert manager.launches == []
    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "refused.sqlite")
    assert "failed_validation_count=2" in outbox, "the measurement is never lost"
    assert "returned_for_rework=refused" in outbox
    assert "reject_not_reviewable:task_id=TARGET" in outbox
    assert manager.events[-1]["review_automation"]["rework_return"]["state"] == "refused"


def test_a_raising_rejection_is_a_refusal_and_never_a_lost_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store fault must not replace the mechanical reason with a traceback."""
    manager = _RejectingManager(tmp_path, raises=True)
    driver = _drive_rejecting(tmp_path, "raising.sqlite", manager, monkeypatch)

    result = driver.drain(max_actions=1, now=NOW)

    assert manager.card["status"] == "review"
    assert manager.launches == []
    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "raising.sqlite")
    assert "failed_validation_count=2" in outbox
    assert "returned_for_rework=error" in outbox
    assert "RuntimeError" in outbox
    assert manager.events[-1]["review_automation"]["rework_return"]["state"] == "error"


def test_a_manager_with_no_reject_surface_still_records_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short-circuit predates the reject surface and must outlive its absence."""
    manager = _Manager(tmp_path)
    manager.card = {"task_id": "TARGET", "status": "review"}
    driver = _drive_rejecting(tmp_path, "no-surface.sqlite", manager, monkeypatch)
    assert not hasattr(manager, "reject_review")

    result = driver.drain(max_actions=1, now=NOW)

    assert manager.launches == []
    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "no-surface.sqlite")
    assert "failed_validation_count=2" in outbox
    assert "returned_for_rework=unavailable" in outbox


def test_a_reviewable_candidate_is_never_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-directional in both halves: no verdict means review, never reject.

    The rejection is reachable ONLY from a positive, measured, in-epoch
    mechanical failure. Every other card gets its reviewer, and none of them
    may be moved out of review by this queue.
    """
    manager = _RejectingManager(tmp_path)
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "show_task",
        lambda _repo, _task_id: {"returncode": 0, "stdout": json.dumps(manager.card)},
    )
    manager.target_status = _target_status()  # no deterministic verdict at all
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "healthy.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert len(manager.launches) == 1
    assert manager.rejects == [], "an unmeasured candidate is reviewed, not rejected"
    assert manager.card["status"] == "review"


def test_a_raising_event_surface_never_replaces_the_mechanical_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outbox row must carry the measurement even if the event write fails.

    _record_mechanical_rework is documented best-effort, but it only handled a
    manager with NO event surface. One that raises let the append error escape
    and become the outbox failure_reason, erasing the very explanation the
    method exists to record.
    """
    manager = _RejectingManager(tmp_path)

    def _boom(_event):
        raise OSError("event log is full")

    monkeypatch.setattr(manager, "_append_event", _boom)
    driver = _drive_rejecting(tmp_path, "noisy-events.sqlite", manager, monkeypatch)

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "noisy-events.sqlite")
    assert "failed_validation_count=2" in outbox
    assert "returned_for_rework=returned" in outbox
    assert "event log is full" not in outbox
    assert manager.card["status"] == "pending", "the rejection still happened"


def test_returning_a_target_to_pending_does_not_retire_the_rest_of_its_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured, not assumed: a returned card does NOT drain its own chain.

    ``reject_review(to="pending")`` moves the target out of ``review``, but
    ``pending`` is deliberately inside REVIEWABLE_TARGET_STATUSES -- it is a
    status a target can still be driven through review FROM -- so
    ``_target_left_review`` reads "" and cannot retire the queued actions. The
    launch action fails with the mechanical reason and the remaining eleven
    stay parked, exactly as they did before any rejection existed.

    Pinned here rather than "fixed", because forcing the retirement would
    carry the chain on to ``needfix_close``, which resolves every linked
    NeedFix row with "automatic review lifecycle accepted and archived task
    <id>". That sentence is false about a card that was REJECTED. Trading a
    parked chain for a fabricated acceptance record is not an improvement,
    and this repository's worst defect class is exactly the state transition
    whose record does not match what happened.
    """
    assert "pending" in review_orchestrator.REVIEWABLE_TARGET_STATUSES
    manager = _RejectingManager(tmp_path)
    driver = _drive_rejecting(tmp_path, "chain.sqlite", manager, monkeypatch)

    first = driver.drain(max_actions=12, now=NOW)

    assert manager.card["status"] == "pending", "the target did leave review"
    assert driver._target_left_review("TARGET") == "", (
        "pending is still a drivable status, so nothing is retired"
    )
    assert first.attempted == 1 and first.failed == 1
    rows = review_lifecycle.rows_for_test(tmp_path / "chain.sqlite")
    assert [row["state"] for row in rows] == ["failed"] + ["pending"] * 11

    # And the chain is genuinely parked: a second pass reserves nothing.
    assert driver.drain(max_actions=12, now=NOW).attempted == 0


# ---------------------------------------------------------------------------
# The identity read (audit 2026-09-08, problem 1).
#
# Every fixture above uses TOP-LEVEL card keys, which nothing in production
# writes: measured over the live store's 627 chains, 0 target cards carry a
# top-level ``request_id`` and 522 of the 581 failed launch actions died with
# ``target_request_identity_invalid``. These fixtures use the shape
# ``_finalize_isolated_request`` actually seals.
# ---------------------------------------------------------------------------


def _sealed_changed_path_hashes() -> dict[str, str]:
    return {"src/aiworkhub/service.py": "c" * 64}


_SEALED_CANDIDATE_SHA256 = review_orchestrator.candidate_digest(
    {"src/aiworkhub/service.py": "c" * 64}
)


def _stub_archive(monkeypatch) -> None:
    """Archiving a finished reviewer is a store write, not what is under test."""
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda _repo, task_id, **_kwargs: {"ok": True, "task_id": task_id},
    )


def _sealed_target_status(
    *, state: str = "review_ready", evidence_overrides: dict | None = None,
    claim_epoch: str = "1",
) -> dict:
    """A card in the shape ``_finalize_isolated_request`` writes: no top-level
    ``request_id``/``packet_sha256``/``candidate_sha256``/``workspace_identity``
    anywhere, everything under ``terminal_review.evidence``."""
    workspace = {
        "request_id": "target-request",
        "path": "/candidate/worktree",
        "base_oid": "base-oid",
    }
    evidence = {
        "request_identity": {
            "request_id": "target-request",
            "task_id": "TARGET",
            "claim_epoch": claim_epoch,
        },
        "attempt_artifact_manifest": {"manifest_sha256": "a" * 64},
        "changed_path_hashes": _sealed_changed_path_hashes(),
        "workspace": workspace,
        "source_graph_partition_readiness": {"target": True},
    }
    evidence.update(evidence_overrides or {})
    return {
        "ok": True,
        "state": state,
        "task_card": {
            "task_id": "TARGET",
            "claim_epoch": claim_epoch,
            "terminal_review": {"substatus": "review_ready", "evidence": evidence},
            "evidence": {"source_graph_partition_readiness": {"target": True}},
        },
    }


def _sealed_chain(driver) -> None:
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256=review_orchestrator.candidate_digest(
            _sealed_changed_path_hashes()
        ),
        now=NOW,
    )


def test_identity_resolves_from_terminal_evidence_where_the_finalizer_writes_it(
    tmp_path: Path,
) -> None:
    """The read that made 504 of 563 outbox launch actions terminal-fail.

    Verified read-only against the live store: the old top-level read resolved
    0 of 627 chains; this read resolves 93 exactly, retires 332 as superseded
    and leaves 202 unresolvable (199 of whose targets have already left review
    and retire one check earlier).
    """
    manager = _Manager(tmp_path)
    manager.target_status = _sealed_target_status()
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "sealed.sqlite", route_selector=_route
    )
    _sealed_chain(driver)

    # The old read, on the same card, resolves nothing.
    card = manager.target_status["task_card"]
    assert review_orchestrator._legacy_card_identity(card) == {}

    resolved = review_orchestrator.resolve_target_identity(manager.target_status)
    assert resolved["identity_source"] == "terminal_review_evidence"
    assert resolved["identity"]["target_request_id"] == "target-request"
    assert resolved["identity"]["packet_sha256"] == "a" * 64
    assert resolved["workspace_identity"]

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1 and result.failed == 0
    assert len(manager.launches) == 1
    receipt = json.loads(
        review_lifecycle.rows_for_test(tmp_path / "sealed.sqlite")[0]["receipt_json"]
    )
    assert receipt["target_readiness_receipt"]["identity_source"] == (
        "terminal_review_evidence"
    )


def test_registration_payload_answers_when_the_card_evidence_is_gone(
    tmp_path: Path,
) -> None:
    """The finalizer records the same five fields on the terminal event as
    ``review_automation.registration``; it is the only remaining statement of
    what a chain was bound to once the card's terminal evidence is replaced."""
    status = {
        "ok": True,
        "state": "review_ready",
        "task_card": {"task_id": "TARGET"},
        "latest_event": {
            "review_automation": {
                "registration": {
                    "target_task_id": "TARGET",
                    "target_request_id": "target-request",
                    "claim_epoch": "1",
                    "packet_sha256": "a" * 64,
                    "candidate_sha256": "b" * 64,
                }
            }
        },
    }
    resolved = review_orchestrator.resolve_target_identity(status)

    assert resolved["identity_source"] == "review_automation_registration"
    assert resolved["identity"]["candidate_sha256"] == "b" * 64
    assert resolved["conflict"] == ""


def test_two_durable_identities_that_disagree_fail_closed(tmp_path: Path) -> None:
    """A packet digest that differs between the card and the registration is
    two statements about which bytes are under review. Picking one silently is
    exactly what must not happen."""
    status = _sealed_target_status()
    status["latest_event"] = {
        "review_automation": {
            "registration": {
                "target_task_id": "TARGET",
                "target_request_id": "target-request",
                "claim_epoch": "1",
                "packet_sha256": "f" * 64,
                "candidate_sha256": review_orchestrator.candidate_digest(
                    _sealed_changed_path_hashes()
                ),
            }
        }
    }
    resolved = review_orchestrator.resolve_target_identity(status)
    assert resolved["conflict"] == "packet_sha256"

    manager = _Manager(tmp_path)
    manager.target_status = status
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "conflict.sqlite", route_selector=_route
    )
    _sealed_chain(driver)

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1 and manager.launches == []
    assert "target_identity_conflict:packet_sha256" in (
        review_lifecycle.rows_for_test(tmp_path / "conflict.sqlite")[0]["failure_reason"]
    )


# ---------------------------------------------------------------------------
# The lens plan (audit 2026-09-08, problem 2).
#
# ``_RISK_PROFILES`` requires low () / medium (correctness) / high (correctness,
# security) / critical (all three), but the plan launched all three for every
# chain: 194 of 893 launches on accepted targets (21.7%) were of a lens the
# tier never required, at ~889K input tokens each.
# ---------------------------------------------------------------------------


def test_only_the_tier_required_lenses_are_launched(
    monkeypatch, tmp_path: Path
) -> None:
    _stub_archive(monkeypatch)
    manager = _Manager(tmp_path)
    manager.target_status = _sealed_target_status()
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "tier.sqlite", route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256=review_orchestrator.candidate_digest(
            _sealed_changed_path_hashes()
        ),
        now=NOW, required_reviewer_lenses=["correctness"], effective_tier="medium",
    )
    manager.status_results["review-request-correctness"] = _review_status(
        "correctness", candidate_sha256=_SEALED_CANDIDATE_SHA256
    )

    assert review_orchestrator.required_lenses(
        tmp_path / "tier.sqlite", chain.chain_id
    ) == ("correctness",)

    # Nine reviewer actions: three do work, six retire without a reviewer.
    for _ in range(9):
        assert driver.drain(max_actions=1, now=NOW).completed == 1

    assert [row["lens"] for row in manager.launches] == ["correctness"]
    rows = review_lifecycle.rows_for_test(tmp_path / "tier.sqlite")
    obsolete = [
        json.loads(row["receipt_json"])["obsolete_reason"]
        for row in rows[:9]
        if row["state"] == "completed"
        and json.loads(row["receipt_json"]).get("obsolete_reason")
    ]
    assert obsolete == ["lens_not_required_by_tier:medium"] * 6


def test_an_unplanned_chain_still_gets_every_lens(tmp_path: Path) -> None:
    """Fail OPEN on the plan: a chain with no recorded tier reviews everything,
    which is what happened before the plan existed."""
    assert review_orchestrator.required_lenses(
        tmp_path / "absent.sqlite", 999
    ) == review_orchestrator.LENSES
    record = review_orchestrator.lens_plan_record(tmp_path / "absent.sqlite", 999)
    assert record["planned"] is False
    assert record["lenses"] == list(review_orchestrator.LENSES)


def test_manager_override_adds_a_lens_and_can_never_remove_one(tmp_path: Path) -> None:
    """The tier is a floor. A manager may ask for more review than it demands;
    asking for less would silently lower a bar ``accept_review`` still enforces.
    """
    db_path = tmp_path / "override.sqlite"
    review_orchestrator.bind_lens_plan(
        db_path, chain_id=7, lenses=["correctness"], effective_tier="medium"
    )
    assert review_orchestrator.add_required_lens(
        db_path, chain_id=7, lens="security"
    ) == ("correctness", "security")
    # Re-binding cannot shrink a bound plan.
    assert review_orchestrator.bind_lens_plan(
        db_path, chain_id=7, lenses=[], effective_tier="low"
    ) == ("correctness", "security")
    assert review_orchestrator.lens_plan_record(db_path, 7)["source"] == (
        "manager_override"
    )
    with pytest.raises(ValueError, match="unknown_review_lens"):
        review_orchestrator.add_required_lens(db_path, chain_id=7, lens="vibes")


def test_registration_carries_the_tier_from_the_finalizers_own_gate_record() -> None:
    registration = review_orchestrator.candidate_registration(
        metadata={"task_id": "TARGET", "request_id": "target-request", "claim_epoch": 1},
        artifact_receipt={"manifest_sha256": "a" * 64},
        changed_path_hashes=_sealed_changed_path_hashes(),
        quality_gate={
            "review_risk_profile": {
                "effective_tier": "high",
                "required_reviewer_lenses": ["correctness", "security"],
                "error": "",
            }
        },
    )
    assert registration["effective_tier"] == "high"
    assert registration["required_reviewer_lenses"] == ["correctness", "security"]

    low = review_orchestrator.candidate_registration(
        metadata={"task_id": "TARGET", "request_id": "target-request", "claim_epoch": 1},
        artifact_receipt={"manifest_sha256": "a" * 64},
        changed_path_hashes={},
        quality_gate={
            "review_risk_profile": {
                "effective_tier": "low",
                "required_reviewer_lenses": [],
                "error": "",
            }
        },
    )
    assert low["effective_tier"] == "low"
    assert low["required_reviewer_lenses"] == []

    # A gate whose observation failed plans nothing, so every lens runs.
    degraded = review_orchestrator.candidate_registration(
        metadata={"task_id": "TARGET", "request_id": "target-request", "claim_epoch": 1},
        artifact_receipt={"manifest_sha256": "a" * 64},
        changed_path_hashes=_sealed_changed_path_hashes(),
        quality_gate={"review_risk_profile": {"error": "ValueError:boom"}},
    )
    assert "required_reviewer_lenses" not in degraded


def test_drain_defaults_to_the_whole_pass_and_keeps_its_hard_bound(
    monkeypatch, tmp_path: Path
) -> None:
    """One action per reconcile pass could not work off 627 chains: measured, 30
    launches completed automatically while 570 were typed by hand. The BOUND is
    unchanged -- drain still clamps to DEFAULT_DRAIN_MAX_ACTIONS -- only the
    default request changed."""
    import inspect

    _stub_archive(monkeypatch)
    assert review_orchestrator.DEFAULT_DRAIN_MAX_ACTIONS == 12
    assert inspect.signature(
        review_orchestrator.ReviewOrchestrator.drain
    ).parameters["max_actions"].default == 12

    manager = _Manager(tmp_path)
    manager.target_status = _sealed_target_status()
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "drain.sqlite", route_selector=_route
    )
    _sealed_chain(driver)
    for lens in review_orchestrator.LENSES:
        manager.status_results["review-request-" + lens] = _review_status(
            lens, candidate_sha256=_SEALED_CANDIDATE_SHA256
        )

    result = driver.drain(now=NOW)

    # Nine reviewer actions, manager-ready, and two manager-owned compatibility
    # handoffs complete in one bounded pass.
    assert result.attempted <= review_orchestrator.DEFAULT_DRAIN_MAX_ACTIONS
    assert result.completed == 12
    assert result.pending == 0
    assert len(manager.launches) == 3
    # A caller asking for more than the bound still gets the bound.
    assert driver.drain(max_actions=10_000, now=NOW).attempted <= (
        review_orchestrator.DEFAULT_DRAIN_MAX_ACTIONS
    )


def _high_tier_gate() -> dict:
    return {
        "review_risk_profile": {
            "effective_tier": "high",
            "required_reviewer_lenses": ["correctness", "security"],
        }
    }


def _nf517_queue_card(**overrides: object) -> dict:
    status = _sealed_target_status(
        evidence_overrides={"quality_gate": _high_tier_gate()}
    )
    card = dict(status["task_card"])
    row = {
        "task_id": "TARGET",
        "topic": "task_mcp",
        "runner": "grok_4.6",
        "status": "review",
        "terminal_substatus": "review_ready",
        "terminal_review": card["terminal_review"],
        "claim_epoch": "1",
    }
    row.update(overrides)
    return row


def _patch_queue(monkeypatch, cards) -> None:
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "list_review_queue_cards",
        lambda _repo, limit=500: list(cards),
    )
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "get_task",
        lambda _repo, _task_id: None,
    )


def test_nf517_zero_child_review_ready_scan_launches_required_lenses_once(
    monkeypatch, tmp_path: Path,
) -> None:
    """Live NF-517 shape: review_ready, zero children, two required lenses."""
    _stub_archive(monkeypatch)
    _patch_queue(monkeypatch, [_nf517_queue_card()])
    manager = _Manager(tmp_path)
    manager.target_status = _sealed_target_status(
        evidence_overrides={"quality_gate": _high_tier_gate()}
    )
    db = tmp_path / "nf517.sqlite"
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db, route_selector=_route,
    )

    first = review_orchestrator.recover_review_ready_targets(manager, db_path=db)
    assert first["review_recovery_scanned"] == 1
    assert first["review_recovery_ensured"] == 1
    assert first["review_recovery_failed"] == 0
    for lens in ("correctness", "security"):
        manager.status_results["review-request-" + lens] = _review_status(
            lens, candidate_sha256=_SEALED_CANDIDATE_SHA256
        )
    for _ in range(12):
        assert driver.drain(max_actions=1, now=NOW).failed == 0
        if [row["lens"] for row in manager.launches] == ["correctness", "security"]:
            break
    assert [row["lens"] for row in manager.launches] == ["correctness", "security"]
    assert ("target-request", "TARGET") not in manager.accepts
    assert review_orchestrator.AUTOMATIC_TARGET_ACCEPT_ENABLED is False
    assert review_orchestrator._manager_reserved_codex_route(
        {"runner": "codex", "adapter_id": "codex_cli", "model": "gpt-5"}
    )
    for row in manager.launches:
        assert not review_orchestrator._manager_reserved_codex_route({
            "runner": row.get("runner") or "",
            "adapter_id": row.get("adapter_id") or "",
            "model": row.get("model") or "",
        })

    second = review_orchestrator.recover_review_ready_targets(manager, db_path=db)
    driver.drain(now=NOW)
    assert second["review_recovery_failed"] == 0
    assert [row["lens"] for row in manager.launches] == ["correctness", "security"]
    assert ("target-request", "TARGET") not in manager.accepts


def test_recover_review_ready_fails_closed_on_untrusted_targets(
    monkeypatch, tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    db = tmp_path / "closed.sqlite"

    _patch_queue(monkeypatch, [_nf517_queue_card(terminal_substatus="blocked")])
    skipped = review_orchestrator.recover_review_ready_targets(manager, db_path=db)
    assert skipped["review_recovery_scanned"] == 0

    missing = dict(_nf517_queue_card())
    missing["terminal_review"] = {"substatus": "review_ready", "evidence": {}}
    _patch_queue(monkeypatch, [missing])
    result = review_orchestrator.recover_review_ready_targets(manager, db_path=db)
    assert result["review_recovery_reasons"].get("missing_registration") == 1

    _patch_queue(monkeypatch, [_nf517_queue_card()])
    manager.target_status = _sealed_target_status(
        evidence_overrides={"changed_path_hashes": {}, "quality_gate": _high_tier_gate()}
    )
    result = review_orchestrator.recover_review_ready_targets(manager, db_path=db)
    assert result["review_recovery_reasons"].get("empty_delta") == 1

    manager.target_status = _sealed_target_status(
        evidence_overrides={"quality_gate": _high_tier_gate()}
    )
    manager.target_status["latest_event"] = {
        "review_automation": {
            "state": "seeded",
            "registration": {
                "target_task_id": "TARGET",
                "target_request_id": "target-request",
                "claim_epoch": "1",
                "packet_sha256": "a" * 64,
                "candidate_sha256": "d" * 64,
            },
        }
    }
    result = review_orchestrator.recover_review_ready_targets(manager, db_path=db)
    assert result["review_recovery_reasons"].get("changed_candidate") == 1

    manager.target_status = _sealed_target_status(state="processing")
    result = review_orchestrator.recover_review_ready_targets(manager, db_path=db)
    assert result["review_recovery_reasons"].get("non_review") == 1

    manager.target_status = _sealed_target_status(
        evidence_overrides={"quality_gate": _high_tier_gate()}
    )
    _patch_queue(monkeypatch, [_nf517_queue_card(task_id="OTHER")])
    result = review_orchestrator.recover_review_ready_targets(manager, db_path=db)
    assert result["review_recovery_reasons"].get("mismatched_identity") == 1
    assert result["review_recovery_ensured"] == 0


# ---------------------------------------------------------------------------
# NF-2026-00887: a reviewer task ROW is not a review.
#
# Recovery counted any child keyed to the lens as coverage. On the live queue
# that produced this card the scan reported 5 review_ready targets scanned, 4
# skipped as ``already_present`` and 0 reviewers running -- four parents whose
# only children had died were told they owed nothing, forever.
# ---------------------------------------------------------------------------


def _nf887_identity(task_id: str, request_id: str) -> dict[str, str]:
    return review_lifecycle._chain_identity(
        target_task_id=task_id,
        target_request_id=request_id,
        claim_epoch="1",
        packet_sha256="a" * 64,
        candidate_sha256=_SEALED_CANDIDATE_SHA256,
    )


def _nf887_reviewer_task(
    task_id: str, request_id: str, lens: str, *, attempt: int = 1
) -> str:
    return review_orchestrator.ReviewOrchestrator._reviewer_task_id(
        _nf887_identity(task_id, request_id), lens, attempt_index=attempt
    )


def _nf887_status(task_id: str, request_id: str, gate: dict) -> dict:
    """One sealed review_ready target, in the shape the finalizer writes."""
    return {
        "ok": True,
        "state": "review_ready",
        "task_card": {
            "task_id": task_id,
            "claim_epoch": "1",
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {
                        "request_id": request_id,
                        "task_id": task_id,
                        "claim_epoch": "1",
                    },
                    "attempt_artifact_manifest": {"manifest_sha256": "a" * 64},
                    "changed_path_hashes": _sealed_changed_path_hashes(),
                    "workspace": {
                        "request_id": request_id,
                        "path": "/candidate/worktree",
                        "base_oid": "base-oid",
                    },
                    "quality_gate": gate,
                    "source_graph_partition_readiness": {"target": True},
                },
            },
            "evidence": {"source_graph_partition_readiness": {"target": True}},
        },
    }


def _nf887_queue_card(task_id: str, request_id: str, gate: dict) -> dict:
    status = _nf887_status(task_id, request_id, gate)
    return {
        "task_id": task_id,
        "topic": "task_mcp",
        "runner": "grok_4.6",
        "status": "review",
        "terminal_substatus": "review_ready",
        "terminal_review": status["task_card"]["terminal_review"],
        "claim_epoch": "1",
    }


def _dead_reviewer_row(task_id: str, request_id: str, lens: str, state: str) -> dict:
    """A reviewer child that exists and can never file the report it owes."""
    return {
        "task_id": _nf887_reviewer_task(task_id, request_id, lens),
        "topic": "quality_review",
        "status": state,
        "worker_status": "",
    }


def _live_reviewer_row(
    task_id: str, request_id: str, lens: str, *, attempt: int = 1
) -> dict:
    return {
        "task_id": _nf887_reviewer_task(task_id, request_id, lens, attempt=attempt),
        "topic": "quality_review",
        "status": "processing",
        "worker_status": "claimed",
    }


def _reported_reviewer_row(
    task_id: str, request_id: str, lens: str, *, state: str,
    claim_epoch: str = "1",
) -> dict:
    """A stopped reviewer carrying the sealed report for ``claim_epoch``."""
    reviewer_task = _nf887_reviewer_task(task_id, request_id, lens)
    packet = hashlib.sha256(f"review-packet:{request_id}:{lens}".encode()).hexdigest()
    receipt = {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": packet,
        "target": {
            "request_id": request_id, "task_id": task_id, "claim_epoch": claim_epoch,
        },
        "reviewer": {
            "request_id": "review-request-" + lens,
            "task_id": reviewer_task,
            "provider": ROUTE["adapter_id"],
        },
        "report": {
            "lens": lens, "provider": ROUTE["adapter_id"], "read_only": True,
            "can_mutate_repo": False, "findings": [],
        },
        "authority": {
            "process_identity_verified": True, "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": hashlib.sha256(
            f"submission:{request_id}:{lens}".encode()
        ).hexdigest(),
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }
    return {
        "task_id": reviewer_task,
        "topic": "quality_review",
        "status": state,
        "archived_at": "2026-09-15T00:00:00+00:00" if state == "archived" else "",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "quality_review": {
                    "lens": lens,
                    "packet_sha256": packet,
                    "target_request_id": request_id,
                    "target_task_id": task_id,
                    "target_claim_epoch": claim_epoch,
                },
                "quality_review_receipt": receipt,
            },
        },
    }


def _patch_nf887(monkeypatch, cards: list[dict], rows: dict[str, dict]) -> None:
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "list_review_queue_cards",
        lambda _repo, limit=500: list(cards),
    )
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "get_task",
        lambda _repo, task_id: rows.get(task_id),
    )


def test_nf887_dead_reviewer_children_are_not_coverage_for_a_required_lens(
    monkeypatch, tmp_path: Path,
) -> None:
    """The measured live state, reproduced: 5 scanned, 4 "already_present", 0
    reviewers running. Four of those five parents had children in exactly the
    states that can never produce a report, and the fifth had none at all. All
    five are owed both required lenses, so all five must be ensured."""
    _stub_archive(monkeypatch)
    gate = _high_tier_gate()
    cards = [
        _nf887_queue_card(f"TARGET-{index}", f"request-{index}", gate)
        for index in range(1, 6)
    ]
    rows: dict[str, dict] = {}
    for index, state in enumerate(
        ("blocked", "worker_failed", "cancelled", "timed_out"), start=1
    ):
        for lens in ("correctness", "security"):
            row = _dead_reviewer_row(f"TARGET-{index}", f"request-{index}", lens, state)
            rows[row["task_id"]] = row
    _patch_nf887(monkeypatch, cards, rows)
    manager = _Manager(tmp_path)
    for index in range(1, 6):
        manager.status_results[f"request-{index}"] = _nf887_status(
            f"TARGET-{index}", f"request-{index}", gate
        )

    result = review_orchestrator.recover_review_ready_targets(
        manager, db_path=tmp_path / "nf887.sqlite"
    )

    assert result["review_recovery_scanned"] == 5
    assert result["review_recovery_failed"] == 0
    # The defect, pinned: not one of these five is "already present" any more.
    assert result["review_recovery_reasons"].get("already_present") is None
    assert result["review_recovery_reasons"]["unusable_reviewer"] == 4
    assert result["review_recovery_ensured"] == 5


def test_nf887_a_dead_lens_is_replaced_once_and_a_live_one_is_never_duplicated(
    monkeypatch, tmp_path: Path,
) -> None:
    """A reviewer still running on this claim owes its report and is left
    alone; a dead sibling is replaced, and the pass after the replacement sees
    the successor rather than buying a second one."""
    _stub_archive(monkeypatch)
    gate = _high_tier_gate()
    live = _live_reviewer_row("TARGET", "request-1", "correctness")
    dead = _dead_reviewer_row("TARGET", "request-1", "security", "worker_failed")
    rows = {live["task_id"]: live, dead["task_id"]: dead}
    _patch_nf887(monkeypatch, [_nf887_queue_card("TARGET", "request-1", gate)], rows)
    manager = _Manager(tmp_path)
    manager.status_results["request-1"] = _nf887_status("TARGET", "request-1", gate)
    db = tmp_path / "nf887-once.sqlite"

    first = review_orchestrator.recover_review_ready_targets(manager, db_path=db)

    assert first["review_recovery_ensured"] == 1
    assert first["review_recovery_reasons"]["unusable_reviewer"] == 1

    # The replacement that ensure bought: a distinct-route successor with its
    # own canonical task id. The dead first attempt is still on disk.
    successor = _live_reviewer_row("TARGET", "request-1", "security", attempt=2)
    rows[successor["task_id"]] = successor

    second = review_orchestrator.recover_review_ready_targets(manager, db_path=db)

    assert second["review_recovery_ensured"] == 0
    assert second["review_recovery_reasons"]["already_present"] == 1
    assert second["review_recovery_reasons"].get("unusable_reviewer") is None


def test_nf887_only_a_current_claim_report_keeps_a_stopped_reviewer_present(
    monkeypatch, tmp_path: Path,
) -> None:
    """An archived or superseded reviewer that filed THIS claim's sealed report
    is coverage. One whose only report was written against another claim epoch
    is not, however finished its row says it is."""
    _stub_archive(monkeypatch)
    gate = {
        "review_risk_profile": {
            "effective_tier": "high",
            "required_reviewer_lenses": list(review_orchestrator.LENSES),
        }
    }
    reported = [
        _reported_reviewer_row("TARGET", "request-1", "correctness", state="archived"),
        _reported_reviewer_row("TARGET", "request-1", "security", state="superseded"),
        _reported_reviewer_row(
            "TARGET", "request-1", "code_quality", state="finished", claim_epoch="2",
        ),
    ]
    rows = {row["task_id"]: row for row in reported}
    _patch_nf887(monkeypatch, [_nf887_queue_card("TARGET", "request-1", gate)], rows)
    manager = _Manager(tmp_path)
    manager.status_results["request-1"] = _nf887_status("TARGET", "request-1", gate)

    result = review_orchestrator.recover_review_ready_targets(
        manager, db_path=tmp_path / "nf887-stale.sqlite"
    )

    assert result["review_recovery_ensured"] == 1
    assert result["review_recovery_reasons"]["unusable_reviewer"] == 1
    assert review_orchestrator._reviewer_lens_coverage(
        manager.repo,
        _nf887_identity("TARGET", "request-1"),
        review_orchestrator.LENSES,
    ) == {
        "correctness": review_orchestrator._COVERAGE_COVERED,
        "security": review_orchestrator._COVERAGE_COVERED,
        "code_quality": review_orchestrator._COVERAGE_UNUSABLE,
    }


# ---------------------------------------------------------------------------
# NF-2026-00888: a RETIRED attempt is not a reviewer either.
#
# NF887 stopped a dead reviewer CARD from being read as coverage. This is the
# other half, one layer down, on the durable route attempt. Two shapes parked
# a CURRENT candidate on one lens for ever, both with no successor and no
# recorded reason: an attempt some earlier pass already retired, which every
# later accept pass re-polled and returned nothing for; and an attempt still
# recorded as ``launched`` whose process was already terminal, which every
# later launch pass handed back unchanged and relaunched.
# ---------------------------------------------------------------------------


def _nf888_chain(
    tmp_path: Path, name: str,
) -> tuple[_FailoverManager, review_orchestrator.ReviewOrchestrator, object]:
    """One real chain on a route table deep enough to never run dry."""
    manager = _FailoverManager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager,
        db_path=tmp_path / f"nf888-{name}.sqlite",
        route_selector=_unlimited_route_selector(),
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return manager, driver, chain


def _nf888_kill(
    manager: _FailoverManager,
    driver: review_orchestrator.ReviewOrchestrator,
    chain,
    *,
    state: str,
    error_code: str,
) -> dict:
    """The latest reviewer dies on its own exact route, as a poll sees it."""
    attempt = driver._route_attempts(chain.chain_id, "correctness")[-1]
    request_id = str(attempt["reviewer_request_id"])
    manager.status_results[request_id] = {
        "ok": True,
        "request_id": request_id,
        "task_id": str(attempt["reviewer_task_id"]),
        "state": state,
        "error_code": error_code,
        **{key: str(attempt[key]) for key in ROUTE},
        "task_card": {"terminal_substatus": state, "worker_status": state},
    }
    return dict(attempt)


def test_nf888_a_retired_attempt_on_accept_reaches_its_distinct_successor(
    tmp_path: Path,
) -> None:
    """Capacity retires an attempt now and leaves a distinct route for later.

    The launch path already honoured that. The accept path did not: it re-read
    the SAME retired attempt, polled the SAME dead reviewer and returned None
    on every pass afterwards, so a current candidate owed a lens no reviewer
    was alive to file and nothing ever moved it.
    """
    manager, driver, chain = _nf888_chain(tmp_path, "accept-retired")
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    assert len(manager.provider_launches) == 1

    _nf888_kill(
        manager, driver, chain, state="launch_failed", error_code="quota_exhausted",
    )
    # Capacity spends nothing NOW: retired, and no second provider this pass.
    assert driver.drain(max_actions=1, now=NOW).pending == 1
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 1
    assert attempts[0]["state"] == "retired"
    assert review_orchestrator.route_attempt_hold(attempts[0]["failure_reason"]) == ""
    assert len(manager.provider_launches) == 1

    # THE DEFECT, pinned: a later pass takes the distinct eligible route.
    assert driver.drain(max_actions=1, now=NOW).pending == 1
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 2
    assert attempts[1]["state"] == "launched"
    assert attempts[1]["runner"] != attempts[0]["runner"]
    assert len(manager.provider_launches) == 2

    successor_request = str(attempts[1]["reviewer_request_id"])
    manager.status_results[successor_request] = _review_status(
        reviewer_request=successor_request,
        reviewer_task=str(attempts[1]["reviewer_task_id"]),
        provider=str(attempts[1]["adapter_id"]),
        route={key: str(attempts[1][key]) for key in ROUTE},
    )

    converged = driver.drain(max_actions=1, now=NOW)

    assert converged.completed == 1
    assert manager.accepts == [
        (successor_request, str(attempts[1]["reviewer_task_id"]))
    ]
    assert len(manager.provider_launches) == 2


@pytest.mark.parametrize(
    ("state", "error_code", "hold", "attempts_after"),
    [
        ("launch_failed", "provider_unavailable", "", 2),
        ("finalize_failed", "", "callback_reconcile", 1),
    ],
)
def test_nf888_a_launched_attempt_whose_process_died_is_reconciled_not_relaunched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    state: str, error_code: str, hold: str, attempts_after: int,
) -> None:
    """A row saying ``launched`` is a claim about a process, not the process.

    The launch is acknowledged and the attempt is durably ``launched``, then
    the pass dies before completing its outbox action -- the state a restarted
    driver finds. When that bound process is already terminal, relaunching it
    reconciles nothing: it re-invokes a reviewer that can never report. Each
    row is retired to its own TYPED disposition instead, and the chain then
    advances exactly as far as the bounded policy allows: one distinct-route
    successor for a provider-asserted transient, a durable hold for the
    finalizer race that no second reviewer can settle.
    """
    manager, driver, chain = _nf888_chain(tmp_path, f"stale-{state}")
    original_bind = driver._bind_route_attempt_request

    def bind_then_lose_the_pass(action, attempt, request_id):
        original_bind(action, attempt, request_id)
        return False

    monkeypatch.setattr(
        driver, "_bind_route_attempt_request", bind_then_lose_the_pass
    )
    assert driver.drain(max_actions=1, now=NOW).pending == 1
    monkeypatch.undo()
    stale = driver._route_attempts(chain.chain_id, "correctness")[-1]
    assert stale["state"] == "launched"
    assert str(stale["reviewer_request_id"])
    _nf888_kill(manager, driver, chain, state=state, error_code=error_code)

    driver.drain(max_actions=1, now=NOW)

    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert attempts[0]["state"] == "retired"
    assert attempts[0]["failure_reason"].startswith(state + ":")
    assert review_orchestrator.route_attempt_hold(
        attempts[0]["failure_reason"]
    ) == hold
    # THE DEFECT: the dead reviewer task was invoked again, every pass.
    assert [
        call["reviewer_task_id"] for call in manager.launches
    ].count(str(stale["reviewer_task_id"])) == 1
    assert len(attempts) == attempts_after
    assert len(manager.provider_launches) == attempts_after
    if attempts_after == 2:
        assert attempts[1]["state"] == "launched"
        assert attempts[1]["runner"] != attempts[0]["runner"]


# The live NF888 recovery failure these tests pin. A durable attempt keeps the
# model ALIAS its route selection named ("opus"); the process the launcher
# actually started reports the canonical model ("claude-opus-5") for that
# identical request, task, runner and adapter. Measured on current chains 906
# and 916: actions 10862 and 10982 failed with
# ``reviewer_terminal_route_binding_invalid`` -- the orchestrator refusing its
# OWN reviewer as evidence about somebody else -- instead of reconciling the
# launch_failed and finalize_failed processes it had itself launched.
_NF888_ALIAS_ROUTES = (
    {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "opus"},
    {"runner": "claude_sonnet-5", "adapter_id": "claude_cli", "model": "sonnet"},
)
_NF888_LAUNCHER_MODEL = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5"}


def _nf888_alias_route_selector():
    """Canonical workforce routes, recorded by the alias the plan named."""
    assigned: dict[str, dict[str, str]] = {}

    def select(_repo: Path, task_id: str, _lens: str) -> dict[str, str]:
        if task_id not in assigned:
            assigned[task_id] = dict(
                _NF888_ALIAS_ROUTES[len(assigned) % len(_NF888_ALIAS_ROUTES)]
            )
        return dict(assigned[task_id])

    return select


def _nf888_alias_chain(tmp_path: Path, name: str):
    manager = _FailoverManager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager,
        db_path=tmp_path / f"nf888-alias-{name}.sqlite",
        route_selector=_nf888_alias_route_selector(),
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return manager, driver, chain


def _nf888_stale_launched(driver, chain, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Leave the newest attempt durably ``launched`` with its pass lost."""
    original_bind = driver._bind_route_attempt_request

    def bind_then_lose_the_pass(action, attempt, request_id):
        original_bind(action, attempt, request_id)
        return False

    monkeypatch.setattr(
        driver, "_bind_route_attempt_request", bind_then_lose_the_pass
    )
    assert driver.drain(max_actions=1, now=NOW).pending == 1
    monkeypatch.undo()
    stale = driver._route_attempts(chain.chain_id, "correctness")[-1]
    assert stale["state"] == "launched"
    assert str(stale["reviewer_request_id"])
    return dict(stale)


def _nf888_kill_reporting_model(
    manager: _FailoverManager,
    driver: review_orchestrator.ReviewOrchestrator,
    chain,
    *,
    state: str,
    error_code: str,
    model: str | None = None,
) -> dict:
    """The bound reviewer dies, naming its model the way the launcher does."""
    attempt = driver._route_attempts(chain.chain_id, "correctness")[-1]
    request_id = str(attempt["reviewer_request_id"])
    reported = model or _NF888_LAUNCHER_MODEL[str(attempt["model"])]
    assert reported != str(attempt["model"]), "the two spellings must differ"
    manager.status_results[request_id] = {
        "ok": True,
        "request_id": request_id,
        "task_id": str(attempt["reviewer_task_id"]),
        "state": state,
        "error_code": error_code,
        "runner": str(attempt["runner"]),
        "adapter_id": str(attempt["adapter_id"]),
        "model": reported,
        "task_card": {"terminal_substatus": state, "worker_status": state},
    }
    return dict(attempt)


@pytest.mark.parametrize(
    ("state", "error_code", "hold", "attempts_after"),
    [
        ("launch_failed", "provider_unavailable", "", 2),
        ("finalize_failed", "", "callback_reconcile", 1),
    ],
)
def test_nf888_a_planned_model_alias_and_its_launched_canonical_name_are_one_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    state: str, error_code: str, hold: str, attempts_after: int,
) -> None:
    """The recorded alias and the reported canonical model are ONE identity.

    Everything else about the binding is unchanged and exact -- same request,
    same reviewer task, same runner, same adapter. Only the model was compared
    byte-for-byte across two vocabularies, and that alone made a chain refuse
    its own terminal reviewer on every pass: no retirement, no successor, no
    hold, just the same failed action again. Reconciliation must reach the
    SAME typed dispositions it reaches when both sides spell the model alike --
    one distinct-route successor for the provider-asserted transient, a durable
    hold for the finalizer race no second reviewer can settle.
    """
    manager, driver, chain = _nf888_alias_chain(tmp_path, state)
    stale = _nf888_stale_launched(driver, chain, monkeypatch)
    assert stale["model"] == "opus"
    killed = _nf888_kill_reporting_model(
        manager, driver, chain, state=state, error_code=error_code,
    )
    reported = manager.status_results[str(killed["reviewer_request_id"])]
    assert reported["model"] == "claude-opus-5"

    result = driver.drain(max_actions=1, now=NOW)

    # THE DEFECT: reviewer_terminal_route_binding_invalid, every pass.
    assert result.failed == 0
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert attempts[0]["state"] == "retired"
    assert attempts[0]["failure_reason"].startswith(state + ":")
    assert review_orchestrator.route_attempt_hold(
        attempts[0]["failure_reason"]
    ) == hold
    assert [
        call["reviewer_task_id"] for call in manager.launches
    ].count(str(stale["reviewer_task_id"])) == 1
    assert len(attempts) == attempts_after
    assert len(manager.provider_launches) == attempts_after
    if attempts_after == 2:
        assert attempts[1]["state"] == "launched"
        assert attempts[1]["runner"] != attempts[0]["runner"]
        assert attempts[1]["model"] == "sonnet"


def test_nf888_historical_binding_failure_recovers_after_alias_fix(
    tmp_path: Path,
) -> None:
    """A chain failed by the old comparison resumes without losing its launch."""
    manager, driver, chain = _nf888_alias_chain(tmp_path, "historical")
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    killed = _nf888_kill_reporting_model(
        manager,
        driver,
        chain,
        state="launch_failed",
        error_code="provider_unavailable",
    )
    assert killed["model"] == "opus"
    accept = review_lifecycle.reserve_next_action(
        driver.db_path,
        owner="old-driver",
        lease_token="old-lease",
        now=NOW,
    )
    assert accept is not None
    assert accept.action_type == "accept"
    review_lifecycle.fail_action(
        driver.db_path,
        action_id=accept.action_id,
        owner="old-driver",
        lease_token="old-lease",
        reason=review_lifecycle.TERMINAL_ROUTE_BINDING_FAILURE,
        now=NOW,
    )
    review_lifecycle.reconcile_dead_chains(driver.db_path, now=NOW)
    assert review_lifecycle.lifecycle_counts(driver.db_path)["retired"] == 10

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 0
    assert result.pending == 1
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 2
    assert attempts[0]["state"] == "retired"
    assert attempts[1]["state"] == "launched"
    counts = review_lifecycle.lifecycle_counts(driver.db_path)
    assert counts["failed"] == 0
    assert counts["retired"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "claude-sonnet-5"),
        ("runner", "claude_sonnet-5"),
        ("adapter_id", "vscode_lm"),
        ("task_id", "SOME_OTHER_REVIEWER"),
        ("request_id", "some-other-request"),
    ],
)
def test_nf888_a_terminal_status_naming_another_process_is_still_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str,
) -> None:
    """Canonicalizing a spelling may not canonicalize away the identity.

    ``claude-sonnet-5`` is a canonical model name too, and it is not this
    attempt's. Every field of the binding stays load-bearing: the attempt is
    left exactly as it was, no reviewer is retired and no successor is bought
    on somebody else's evidence.
    """
    manager, driver, chain = _nf888_alias_chain(tmp_path, f"foreign-{field}")
    stale = _nf888_stale_launched(driver, chain, monkeypatch)
    killed = _nf888_kill_reporting_model(
        manager, driver, chain,
        state="launch_failed", error_code="provider_unavailable",
    )
    request_id = str(killed["reviewer_request_id"])
    manager.status_results[request_id][field] = value
    assert not driver._attempt_route_binding_matches(
        manager.status_results[request_id], killed
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 1
    assert attempts[0]["state"] == "launched"
    assert attempts[0]["reviewer_task_id"] == stale["reviewer_task_id"]
    assert len(manager.provider_launches) == 1


def test_nf888_a_spent_retry_ceiling_waits_on_a_named_reason(
    tmp_path: Path,
) -> None:
    """Advancing a retired attempt may never become a third reviewer.

    The NF847 ceiling is one first attempt plus one distinct-route retry. Once
    both are dead the lens waits, and it waits on a NAMED durable deferral --
    the difference between a bound that is recorded and one that is silence.
    """
    manager, driver, chain = _nf888_chain(tmp_path, "exhausted")
    assert driver.drain(max_actions=1, now=NOW).completed == 1

    _nf888_kill(
        manager, driver, chain,
        state="worker_failed", error_code="provider_unavailable",
    )
    assert driver.drain(max_actions=1, now=NOW).pending == 1
    assert len(manager.provider_launches) == 2, "the one retry, spent"

    _nf888_kill(
        manager, driver, chain,
        state="worker_failed", error_code="provider_unavailable",
    )
    assert driver.drain(max_actions=1, now=NOW).pending == 1
    assert [
        row["state"]
        for row in driver._route_attempts(chain.chain_id, "correctness")
    ] == ["retired", "retired"]

    for _ in range(3):
        assert driver.drain(max_actions=1, now=NOW).pending == 1
        assert len(manager.provider_launches) == 2
        assert len(driver._route_attempts(chain.chain_id, "correctness")) == 2

    wait = manager.events[-1]["review_automation"]
    assert wait["state"] == "deferred"
    assert wait["reason"] == "review_route_retries_exhausted:correctness"


# The NF780 binding, kept executable. ``manager_ready_marker`` authenticates a
# card against ITS OWN terminal_review and claim epoch; that seal is
# task_store's and is not re-derived here. What recovery adds on top is the
# binding under test: a duplicate request can hold task, request and claim
# identical while sealing a brand-new candidate, so the skip must also name the
# candidate digest the CURRENT episode resolved.
_NF888_CURRENT_MARKER = {
    "target_task_id": "TARGET",
    "target_request_id": "request-1",
    "claim_epoch": "1",
    "candidate_sha256": _SEALED_CANDIDATE_SHA256,
}


def _nf888_recover_with_marker(
    monkeypatch, tmp_path: Path, aggregate: dict,
) -> dict:
    _stub_archive(monkeypatch)
    gate = _high_tier_gate()
    card = _nf887_queue_card("TARGET", "request-1", gate)
    _patch_nf887(monkeypatch, [card], {})
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "manager_ready_marker",
        lambda _card: {"manager_ready": dict(aggregate)},
    )
    manager = _Manager(tmp_path)
    manager.status_results["request-1"] = _nf887_status("TARGET", "request-1", gate)
    return review_orchestrator.recover_review_ready_targets(
        manager, db_path=tmp_path / "nf888-marker.sqlite"
    )


def test_nf888_manager_ready_skips_exactly_the_current_candidate(
    monkeypatch, tmp_path: Path,
) -> None:
    result = _nf888_recover_with_marker(monkeypatch, tmp_path, _NF888_CURRENT_MARKER)

    assert result["review_recovery_scanned"] == 1
    assert result["review_recovery_skipped"] == 1
    assert result["review_recovery_reasons"]["manager_ready"] == 1
    assert result["review_recovery_ensured"] == 0


@pytest.mark.parametrize("field", sorted(_NF888_CURRENT_MARKER))
def test_nf888_a_manager_ready_marker_off_by_one_field_never_skips(
    monkeypatch, tmp_path: Path, field: str,
) -> None:
    """Every one of the four fields is load-bearing, candidate digest included.

    A marker that matches three of them and misses the fourth belongs to some
    other episode, and reading it as this one's would strand the current
    candidate on an aggregate that was never about it.
    """
    result = _nf888_recover_with_marker(
        monkeypatch, tmp_path, {**_NF888_CURRENT_MARKER, field: "d" * 64},
    )

    assert result["review_recovery_reasons"].get("manager_ready") is None
    assert result["review_recovery_ensured"] == 1


def test_nf888_a_lens_whose_whole_retry_ceiling_died_is_named_not_re_ensured(
    monkeypatch, tmp_path: Path,
) -> None:
    """Recovery may not report an ensure that cannot buy a reviewer.

    Both slots inside the ceiling exist and both are dead, so the orchestrator
    refuses another route for this lens. Ensuring the chain again would spend
    nothing and count itself as recovery; the scan names the state instead.
    """
    _stub_archive(monkeypatch)
    gate = _high_tier_gate()
    rows: dict[str, dict] = {}
    for lens in ("correctness", "security"):
        for attempt in (1, 2):
            row = {
                "task_id": _nf887_reviewer_task(
                    "TARGET", "request-1", lens, attempt=attempt
                ),
                "topic": "quality_review",
                "status": "worker_failed",
                "worker_status": "",
            }
            rows[row["task_id"]] = row
    _patch_nf887(monkeypatch, [_nf887_queue_card("TARGET", "request-1", gate)], rows)
    manager = _Manager(tmp_path)
    manager.status_results["request-1"] = _nf887_status("TARGET", "request-1", gate)

    result = review_orchestrator.recover_review_ready_targets(
        manager, db_path=tmp_path / "nf888-exhausted-scan.sqlite"
    )

    assert result["review_recovery_scanned"] == 1
    assert result["review_recovery_skipped"] == 1
    assert result["review_recovery_ensured"] == 0
    assert result["review_recovery_reasons"]["retry_exhausted"] == 1
    assert result["review_recovery_reasons"].get("unusable_reviewer") is None

    # A still-replaceable lens is NOT swept up in that: one dead first attempt
    # with its retry unspent is the NF887 case, and it must still be ensured.
    del rows[_nf887_reviewer_task("TARGET", "request-1", "security", attempt=2)]

    partial = review_orchestrator.recover_review_ready_targets(
        manager, db_path=tmp_path / "nf888-exhausted-scan.sqlite"
    )

    assert partial["review_recovery_ensured"] == 1
    assert partial["review_recovery_reasons"].get("retry_exhausted") is None
    assert partial["review_recovery_reasons"]["unusable_reviewer"] == 1
