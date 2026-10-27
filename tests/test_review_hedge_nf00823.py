"""NF-2026-00823: one bounded hedged successor for a quiet automatic reviewer."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_hedge, review_orchestrator, worker_supervisor  # noqa: E402


def _load_orchestrator_test_doubles():
    """The orchestrator suite's own fake manager and sealed-report builders."""
    path = Path(__file__).with_name("test_review_orchestrator.py")
    spec = importlib.util.spec_from_file_location("_nf00823_orchestrator_doubles", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = _load_orchestrator_test_doubles()
NOW = base.NOW
LENS = "correctness"
INCUMBENT_ROUTE = {
    "runner": "deepseek_reviewer", "adapter_id": "deepseek_vscode_lm",
    "model": "deepseek-v4",
}
HEDGE_ROUTE = {
    "runner": "copilot_gpt-5.6-sol", "adapter_id": "vscode_lm", "model": "gpt-5.6-sol",
}
QUIET = review_hedge.HEDGE_QUIET_SECONDS


# --- pure decision ---------------------------------------------------------


def test_hedge_quiet_seconds_is_conservative_and_below_the_reviewer_wall() -> None:
    assert 600 <= review_hedge.HEDGE_QUIET_SECONDS < 1800


def _decide(**overrides):
    values = {
        "launched_at": 0.0, "last_actionable_progress_at": None,
        "now": QUIET + 1.0, "hedge_state": review_hedge.HEDGE_STATE_NONE,
        "kind": review_hedge.KIND_READ_ONLY_REVIEWER,
    }
    values.update(overrides)
    return review_hedge.decide_hedge(**values)


def test_reasoning_only_activity_past_quiet_threshold_hedges() -> None:
    # Reasoning deltas and heartbeats never reach last_actionable_progress, so
    # a reviewer that only thought for longer than the threshold is quiet.
    decision = _decide()

    assert decision.hedge is True
    assert decision.reason == "no_actionable_progress"
    assert decision.quiet_seconds == pytest.approx(QUIET + 1.0)


def test_actionable_progress_resets_the_quiet_clock() -> None:
    decision = _decide(launched_at=0.0, last_actionable_progress_at=500.0, now=700.0)

    assert decision.hedge is False
    assert decision.reason == "actionable_progress_recent"
    assert decision.quiet_seconds == pytest.approx(200.0)


def test_mutating_worker_never_hedges() -> None:
    decision = _decide(kind=review_hedge.KIND_MUTATING, now=10_000.0)

    assert decision.hedge is False
    assert decision.reason == "mutating_worker_never_hedged"
    assert _decide(kind="something").reason == "attempt_kind_unknown"


@pytest.mark.parametrize(
    "state",
    [
        review_hedge.HEDGE_STATE_RACING,
        review_hedge.HEDGE_STATE_DECIDED,
        review_hedge.HEDGE_STATE_SKIPPED,
    ],
)
def test_second_hedge_for_the_same_lens_is_refused(state: str) -> None:
    decision = _decide(hedge_state=state, now=10_000.0)

    assert decision.hedge is False
    assert decision.reason == "hedge_already_spent"


@pytest.mark.parametrize("launched_at", [None, True, float("nan"), "0"])
def test_unmeasurable_quiet_clock_never_hedges(launched_at) -> None:
    assert _decide(launched_at=launched_at).reason == "quiet_clock_unknown"


def test_attempt_kind_reads_process_and_card_read_only_flags() -> None:
    assert review_hedge.attempt_kind({}) == review_hedge.KIND_READ_ONLY_REVIEWER
    assert review_hedge.attempt_kind({"read_only": False}) == review_hedge.KIND_MUTATING
    assert review_hedge.attempt_kind(
        {"task_card": {"read_only": False}}
    ) == review_hedge.KIND_MUTATING


def test_quiet_clock_reads_runtime_and_actionable_progress() -> None:
    status = {"liveness": {
        "runtime_seconds": 300.0, "last_actionable_progress_epoch": 950.0,
        "last_meaningful_progress_epoch": 999.0,
    }}

    assert review_hedge.quiet_clock(status, 1000.0) == (700.0, 950.0)
    assert review_hedge.quiet_clock({}, 1000.0) == (None, None)
    assert review_hedge.quiet_clock(
        {"liveness": {"runtime_seconds": -1}}, 1000.0
    ) == (None, None)


@pytest.mark.parametrize(
    ("incumbent", "hedge", "winner"),
    [
        ("submitted", "submitted", "incumbent"),
        ("submitted", "running", "incumbent"),
        ("running", "submitted", "hedge"),
        ("failed", "submitted", "hedge"),
        ("running", "running", None),
        ("running", "failed", None),
        ("failed", "running", None),
        ("failed", "unsubmitted", ""),
        ("unsubmitted", "failed", ""),
        ("mystery", "submitted", None),
    ],
)
def test_race_winner_table(incumbent: str, hedge: str, winner) -> None:
    assert review_hedge.race_winner(incumbent, hedge) == winner


# --- through ReviewOrchestrator --------------------------------------------


class _HedgeManager(base._Manager):
    """Distinct request per reviewer task, a running default and exact cancels."""

    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.requests_by_task: dict[str, str] = {}
        self.cancels: list[tuple[str, str]] = []
        self.status_result = {"ok": True, "state": "running"}

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(kwargs)
        task_id = kwargs["reviewer_task_id"]
        request_id = self.requests_by_task.setdefault(
            task_id, f"review-request-{len(self.requests_by_task) + 1}"
        )
        return {"ok": True, "request_id": request_id, "task_id": task_id, "state": "starting"}

    def cancel(self, request_id, reason="owner_cancelled"):
        self.cancels.append((request_id, reason))
        return {"ok": True, "request_id": request_id, "state": "cancelled"}


def _running(adapter_id: str, *, runtime: float, actionable: float | None = None) -> dict:
    return {
        "ok": True, "state": "running", "adapter_id": adapter_id,
        "liveness": {
            "runtime_seconds": runtime,
            "last_actionable_progress_epoch": actionable,
        },
    }


def _selector(routes: list[dict[str, str]]):
    assigned: dict[str, dict[str, str]] = {}

    def select(_repo: Path, task_id: str, _lens: str) -> dict[str, str]:
        if task_id not in assigned:
            assigned[task_id] = dict(routes[min(len(assigned), len(routes) - 1)])
        return dict(assigned[task_id])

    return select


def _incumbent_running(tmp_path: Path, routes=(INCUMBENT_ROUTE, HEDGE_ROUTE)):
    manager = _HedgeManager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "hedge.sqlite", route_selector=_selector(list(routes)),
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    incumbent = driver._route_attempts(chain.chain_id, LENS)[0]
    assert incumbent["state"] == "launched"
    return manager, driver, chain, incumbent


def _quiet_incumbent(manager, incumbent, adapter_id="deepseek_vscode_lm") -> None:
    manager.status_results[incumbent["reviewer_request_id"]] = _running(
        adapter_id, runtime=QUIET + 60.0,
    )


def _hedged(tmp_path: Path):
    manager, driver, chain, incumbent = _incumbent_running(tmp_path)
    _quiet_incumbent(manager, incumbent)
    assert driver.drain(max_actions=1, now=NOW).completed == 0
    attempts = driver._route_attempts(chain.chain_id, LENS)
    assert len(attempts) == 2
    return manager, driver, chain, attempts[0], attempts[1]


def _sealed(attempt) -> dict:
    return base._review_status(
        LENS,
        reviewer_request=str(attempt["reviewer_request_id"]),
        reviewer_task=str(attempt["reviewer_task_id"]),
        route={key: str(attempt[key]) for key in ("runner", "adapter_id", "model")},
    )


def _events(driver, chain) -> list[str]:
    return [row["event"] for row in review_hedge.hedge_events(driver.db_path, chain.chain_id, LENS)]


def test_quiet_incumbent_gets_exactly_one_hedge_on_a_distinct_route(tmp_path: Path) -> None:
    manager, driver, chain, incumbent, hedge = _hedged(tmp_path)

    assert len(manager.launches) == 2
    first, second = manager.launches
    assert second["hedge_incumbent_request_id"] == incumbent["reviewer_request_id"]
    assert "hedge_incumbent_request_id" not in first
    for key in ("lens", "target_request_id", "target_task_id", "target_claim_epoch"):
        assert second[key] == first[key]
    assert {key: second[key] for key in HEDGE_ROUTE} == HEDGE_ROUTE
    assert second["reviewer_task_id"] != first["reviewer_task_id"]
    assert hedge["attempt_index"] == 2 and hedge["state"] == "launched"
    # The incumbent keeps running: nothing cancelled or retired it.
    assert manager.cancels == []
    assert driver._route_attempts(chain.chain_id, LENS)[0]["state"] == "launched"
    record = review_hedge.hedge_record(driver.db_path, chain.chain_id, LENS)
    assert record["state"] == review_hedge.HEDGE_STATE_RACING
    assert record["incumbent_request_id"] == incumbent["reviewer_request_id"]
    assert record["hedge_request_id"] == hedge["reviewer_request_id"]
    events = review_hedge.hedge_events(driver.db_path, chain.chain_id, LENS)
    assert [row["event"] for row in events] == [review_hedge.EVENT_LAUNCHED]
    assert events[0]["incumbent_request_id"] == incumbent["reviewer_request_id"]
    assert events[0]["hedge_request_id"] == hedge["reviewer_request_id"]
    assert events[0]["quiet_seconds"] >= QUIET

    # Further passes while both run: no second hedge, no third route.
    manager.status_results[hedge["reviewer_request_id"]] = _running(
        "vscode_lm", runtime=QUIET + 60.0,
    )
    for _ in range(3):
        assert driver.drain(max_actions=1, now=NOW).completed == 0
    assert len(manager.launches) == 2
    assert len(driver._route_attempts(chain.chain_id, LENS)) == 2
    assert manager.cancels == []


def test_recent_actionable_progress_buys_no_hedge(tmp_path: Path) -> None:
    manager, driver, chain, incumbent = _incumbent_running(tmp_path)
    manager.status_results[incumbent["reviewer_request_id"]] = _running(
        "deepseek_vscode_lm", runtime=QUIET + 600.0, actionable=time.time() - 30.0,
    )

    assert driver.drain(max_actions=1, now=NOW).completed == 0
    assert len(manager.launches) == 1
    assert review_hedge.hedge_record(driver.db_path, chain.chain_id, LENS) is None


def test_hedge_submits_first_cancels_incumbent_and_reads_hedge(tmp_path: Path) -> None:
    manager, driver, chain, incumbent, hedge = _hedged(tmp_path)
    manager.status_results[hedge["reviewer_request_id"]] = _sealed(hedge)

    assert driver.drain(max_actions=1, now=NOW).completed == 1

    assert manager.cancels == [
        (incumbent["reviewer_request_id"], review_hedge.LOSER_RETIREMENT)
    ]
    assert manager.accepts == [
        (hedge["reviewer_request_id"], hedge["reviewer_task_id"])
    ]
    attempts = driver._route_attempts(chain.chain_id, LENS)
    assert attempts[0]["state"] == "retired"
    assert attempts[0]["failure_reason"] == review_hedge.LOSER_RETIREMENT
    assert attempts[1]["state"] == "launched"
    assert _events(driver, chain) == [
        review_hedge.EVENT_LAUNCHED, review_hedge.EVENT_WON,
        review_hedge.EVENT_LOSER_CANCELLED,
    ]
    record = review_hedge.hedge_record(driver.db_path, chain.chain_id, LENS)
    assert record["state"] == review_hedge.HEDGE_STATE_DECIDED
    assert record["winner_attempt_index"] == 2

    # A late submission from the cancelled incumbent is never counted.
    manager.status_results[incumbent["reviewer_request_id"]] = _sealed(incumbent)
    action = review_hedge_action(driver, chain)
    assert review_hedge.arbitrate(driver, action, attempts[1])["attempt_index"] == 2
    assert len(manager.accepts) == 1


def test_incumbent_submits_first_cancels_hedge(tmp_path: Path) -> None:
    manager, driver, chain, incumbent, hedge = _hedged(tmp_path)
    manager.status_results[hedge["reviewer_request_id"]] = _running(
        "vscode_lm", runtime=60.0,
    )
    manager.status_results[incumbent["reviewer_request_id"]] = _sealed(incumbent)

    assert driver.drain(max_actions=1, now=NOW).completed == 1

    assert manager.cancels == [
        (hedge["reviewer_request_id"], review_hedge.LOSER_RETIREMENT)
    ]
    assert manager.accepts == [
        (incumbent["reviewer_request_id"], incumbent["reviewer_task_id"])
    ]
    attempts = driver._route_attempts(chain.chain_id, LENS)
    assert attempts[0]["state"] == "launched"
    assert attempts[1]["state"] == "retired"
    assert _events(driver, chain) == [
        review_hedge.EVENT_LAUNCHED, review_hedge.EVENT_LOSER_CANCELLED,
    ]

    # The cancelled hedge's late report is not read: the decided race names
    # the incumbent for ever.
    manager.status_results[hedge["reviewer_request_id"]] = _sealed(hedge)
    action = review_hedge_action(driver, chain)
    assert review_hedge.arbitrate(driver, action, attempts[1])["attempt_index"] == 1
    assert len(manager.accepts) == 1


def test_no_distinct_route_records_skip_and_leaves_incumbent(tmp_path: Path) -> None:
    manager, driver, chain, incumbent = _incumbent_running(
        tmp_path, routes=(INCUMBENT_ROUTE,),
    )
    _quiet_incumbent(manager, incumbent)

    for _ in range(2):
        assert driver.drain(max_actions=1, now=NOW).completed == 0

    assert len(manager.launches) == 1
    assert manager.cancels == []
    attempts = driver._route_attempts(chain.chain_id, LENS)
    assert len(attempts) == 1 and attempts[0]["state"] == "launched"
    events = review_hedge.hedge_events(driver.db_path, chain.chain_id, LENS)
    assert [row["event"] for row in events] == [review_hedge.EVENT_SKIPPED]
    assert events[0]["detail"] == review_hedge.SKIP_NO_DISTINCT_ROUTE
    assert events[0]["incumbent_request_id"] == incumbent["reviewer_request_id"]
    assert events[0]["quiet_seconds"] >= QUIET
    record = review_hedge.hedge_record(driver.db_path, chain.chain_id, LENS)
    assert record["state"] == review_hedge.HEDGE_STATE_SKIPPED
    assert record["outcome"] == review_hedge.SKIP_NO_DISTINCT_ROUTE


def test_untrusted_progress_adapter_is_never_hedged(tmp_path: Path) -> None:
    untrusted = {"runner": "gemini", "adapter_id": "gemini_cli", "model": "gemini-3"}
    assert untrusted["adapter_id"] not in worker_supervisor.TRUSTED_PROGRESS_ADAPTERS
    manager, driver, chain, incumbent = _incumbent_running(
        tmp_path, routes=(untrusted, HEDGE_ROUTE),
    )
    _quiet_incumbent(manager, incumbent, adapter_id="gemini_cli")

    assert driver.drain(max_actions=1, now=NOW).completed == 0

    assert len(manager.launches) == 1
    assert manager.cancels == []
    events = review_hedge.hedge_events(driver.db_path, chain.chain_id, LENS)
    assert [row["detail"] for row in events] == [
        review_hedge.SKIP_PROGRESS_SIGNAL_UNAVAILABLE
    ]


def test_mutating_status_is_never_hedged_through_the_orchestrator(tmp_path: Path) -> None:
    manager, driver, chain, incumbent = _incumbent_running(tmp_path)
    manager.status_results[incumbent["reviewer_request_id"]] = {
        **_running("deepseek_vscode_lm", runtime=QUIET + 60.0), "read_only": False,
    }

    assert driver.drain(max_actions=1, now=NOW).completed == 0
    assert len(manager.launches) == 1
    assert review_hedge.hedge_record(driver.db_path, chain.chain_id, LENS) is None


def test_hedge_is_the_one_distinct_route_retry(tmp_path: Path) -> None:
    """Both sides die: the lens holds on the spent ceiling, never a third route."""
    manager, driver, chain, incumbent, hedge = _hedged(tmp_path)
    for attempt in (incumbent, hedge):
        request_id = str(attempt["reviewer_request_id"])
        manager.status_results[request_id] = {
            "ok": True, "request_id": request_id,
            "task_id": str(attempt["reviewer_task_id"]),
            "state": "worker_failed", "error_code": "provider_unavailable",
            **{key: str(attempt[key]) for key in ("runner", "adapter_id", "model")},
            "latest_event": {
                "failure_kind": "worker_failed",
                "diagnostic": "worker_failed:provider_timeout:exit_code=1",
            },
            "task_card": {
                "terminal_substatus": "worker_failed",
                "worker_status": "worker_failed",
            },
        }

    for _ in range(3):
        assert driver.drain(max_actions=1, now=NOW).completed == 0
        assert len(manager.launches) == 2
        assert len(driver._route_attempts(chain.chain_id, LENS)) == 2
    record = review_hedge.hedge_record(driver.db_path, chain.chain_id, LENS)
    assert record["state"] == review_hedge.HEDGE_STATE_DECIDED
    assert record["outcome"] == "no_valid_submission"
    assert manager.cancels == []


def review_hedge_action(driver, chain):
    """A ReviewAction-shaped view of this chain's lens for direct arbitration."""
    identity = {
        "schema_id": "aiworkhub.review_lifecycle.v1",
        "target_task_id": "TARGET", "target_request_id": "target-request",
        "claim_epoch": "1", "packet_sha256": "a" * 64, "candidate_sha256": "b" * 64,
    }
    return SimpleNamespace(
        chain_id=chain.chain_id, lens=LENS,
        descriptor={"chain_identity": identity},
    )
