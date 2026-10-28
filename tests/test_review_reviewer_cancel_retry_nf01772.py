"""A reviewer that dies on a transport cancel without submitting retries once (NF-2026-01772).

Measured 2026-10-28: the orchestrator auto-launched a correctness reviewer on a
vscode_lm route. It ended ``blocked`` with ``worker_failed:vscode_lm_request_
cancelled:exit_code=1`` and no ``quality_review_submit`` receipt, and nothing
relaunched it -- the target sat on ``required_reviewer_missing:correctness``
until a manager launched a reviewer by hand.

ROOT CAUSE. Both reviewer-terminal paths (the accept pass and
``_reconcile_launched_attempt``) ask ``reviewer_recovery_dispatch`` ->
``reviewer_terminal_disposition`` what to do. That disposition places a
reviewer's own vscode_lm code only through ``reviewer_route_behaviour_code``,
whose vocabulary is the turn-BEHAVIOUR codes plus the ``vscode_lm_request_failed``
wrapper; ``vscode_lm_request_cancelled`` lives in the separate, deliberately
unplaced ``_VSCODE_LM_TRANSPORT_CODES`` tuple. So the reason fell through to
``failure_disposition_from_substatus(worker_failed)`` ->
``provider_runtime_unclassified`` -> ``manager_judgment_unknown``, a member of
``ROUTE_HOLD_ACTIONS``. The attempt was retired with ``:hold=manager_judgment_
unknown`` and ``_plan_route_attempt``'s ``route_attempt_hold`` gate then refused
every successor with ``review_route_hold:manager_judgment_unknown``. The
planner, the exclusions and the ``_MAX_ROUTE_ATTEMPTS`` ceiling were never
reached.

FIX. On the REVIEWER path only, a transport code now takes the same
``route_retry`` disposition a behaviour code does -- unless the reviewer was
cancelled (manager/operator cancel is final) or its card already carries a
``quality_review_submit`` receipt. The implementation-worker classification
(``_REASON_CAUSE`` / ``failure_disposition_from_substatus``) is untouched.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_orchestrator, terminal_failure_classification  # noqa: E402
import pytest  # noqa: E402


NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
_VSCODE_ROUTE = {
    "runner": "copilot_claude-opus-5",
    "adapter_id": "vscode_lm",
    "model": "claude-opus-5",
}
_CLAUDE_ROUTE = {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "opus-5"}
_DEEPSEEK_ROUTE = {
    "runner": "deepseek_native-v4-pro",
    "adapter_id": "deepseek_copilot_cli",
    "model": "deepseek-v4-pro",
}
_CATALOG = {"schema_id": "aiworkhub.workforce_catalog.v1", "workers": [{"worker_id": "gpt-5.5"}]}
_TARGET_CARD = {
    "task_id": "TARGET",
    "request_id": "target-request",
    "claim_epoch": "1",
    "packet_sha256": "a" * 64,
    "candidate_sha256": "b" * 64,
    "workspace_identity": "workspace-candidate-a",
    "evidence": {"source_graph_partition_readiness": {"target": True}},
}
# The measured blocker reason, verbatim.
TRANSPORT_CANCEL = "worker_failed:vscode_lm_request_cancelled:exit_code=1"


class _Manager:
    """ProcessManager-shaped fake: one request per reviewer task, scripted statuses."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.launches: list[dict] = []
        self.accepts: list[tuple[str, str]] = []
        self.statuses: dict[str, dict] = {}
        self.requests_by_task: dict[str, str] = {}
        self.events: list[dict] = []

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(dict(kwargs))
        task_id = kwargs["reviewer_task_id"]
        request_id = self.requests_by_task.setdefault(
            task_id, f"review-request-{len(self.requests_by_task) + 1}"
        )
        return {"ok": True, "request_id": request_id, "task_id": task_id, "state": "starting"}

    def status(self, request_id):
        if request_id == "target-request":
            return {"ok": True, "state": "review_ready", "task_card": dict(_TARGET_CARD)}
        return {
            "request_id": request_id,
            **self.statuses.get(request_id, {"ok": True, "state": "starting"}),
        }

    def accept_review(self, request_id, task_id, **kwargs):
        self.accepts.append((request_id, task_id))
        return {"ok": True, "request_id": request_id, "task_id": task_id}

    def _append_event(self, event):
        self.events.append(event)


@pytest.fixture(autouse=True)
def _fresh_routing_cache():
    review_orchestrator.reset_routing_catalog_cache()
    yield
    review_orchestrator.reset_routing_catalog_cache()


def _driver(monkeypatch, tmp_path: Path, *routes: dict):
    """A real orchestrator over a fake manager and a fixed ranking, best first."""
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "storage_readiness",
        lambda _repo: SimpleNamespace(
            ready=True, reason="ready", repo_id="repo-test",
            canonical_db=str(tmp_path / "queue.sqlite"),
        ),
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "rank_task",
        lambda *_args, **_kwargs: {
            "launch_contract": dict(routes[0]),
            "candidates": [
                {**route, "execution_runner": route["runner"], "excluded": False}
                for route in routes
            ],
        },
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: dict(_CATALOG),
    )
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(manager, db_path=tmp_path / "review.sqlite")
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return manager, driver, chain


def _correctness(chain, action_type: str):
    return next(
        action for action in chain.actions
        if action.lens == "correctness" and action.action_type == action_type
    )


def _terminal_status(
    attempt: dict | None = None,
    *,
    substatus: str = "worker_failed",
    diagnostic: str = TRANSPORT_CANCEL,
    submitted: bool = False,
) -> dict:
    """A ``blocked`` reviewer status shaped like the measured one."""
    attempt = attempt or {
        "reviewer_request_id": "review-request", "reviewer_task_id": "QUALITY_REVIEW",
        **_VSCODE_ROUTE,
    }
    card: dict = {
        "terminal_substatus": substatus,
        "worker_status": substatus,
        "blocker_reason": diagnostic,
    }
    if submitted:
        card["terminal_review"] = {
            "evidence": {
                "quality_review": {"lens": "correctness", "packet_sha256": "c" * 64},
                "quality_review_receipt": {
                    "schema_id": "aiworkhub.quality_review_receipt.v1",
                    "physical_submission_count": 1,
                    "logical_submission_count": 1,
                },
            }
        }
    return {
        "ok": True,
        "state": "blocked",
        "request_id": attempt["reviewer_request_id"],
        "task_id": attempt["reviewer_task_id"],
        "runner": attempt["runner"],
        "adapter_id": attempt["adapter_id"],
        "model": attempt["model"],
        "latest_event": {"failure_kind": substatus, "diagnostic": diagnostic},
        "task_card": card,
    }


def _first_attempt_ends(monkeypatch, tmp_path: Path, **status_kwargs):
    """Launch the correctness reviewer on vscode_lm and end it as described."""
    manager, driver, chain = _driver(
        monkeypatch, tmp_path, _VSCODE_ROUTE, _CLAUDE_ROUTE, _DEEPSEEK_ROUTE
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    (first,) = driver._route_attempts(chain.chain_id, "correctness")
    assert first["adapter_id"] == "vscode_lm"
    manager.statuses[first["reviewer_request_id"]] = _terminal_status(
        first, **status_kwargs
    )
    return manager, driver, chain


# -- the typed vocabulary ----------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (TRANSPORT_CANCEL, "vscode_lm_request_cancelled"),
        ("vscode_lm_mcp_unavailable", "vscode_lm_mcp_unavailable"),
        ("worker_failed:vscode_lm_request_expired:exit_code=1", "vscode_lm_request_expired"),
        ("", ""),
        ("worker_failed:provider_timeout:exit_code=1", ""),
        ("worker_failed:vscode_lm_agent_turn_limit:exit_code=1", ""),
        ("worker_failed:vscode_lm_request_cancelled_typo", ""),
    ],
)
def test_reviewer_route_transport_code_selects_only_the_closed_vocabulary(
    reason: str, expected: str,
) -> None:
    assert terminal_failure_classification.reviewer_route_transport_code(reason) == expected


def test_a_reviewer_transport_cancel_without_submit_is_route_retry() -> None:
    dispatch = review_orchestrator.reviewer_recovery_dispatch(
        _terminal_status(), attempts_made=1,
    )

    assert dispatch["action"] == terminal_failure_classification.ACTION_ROUTE_RETRY
    assert dispatch["route_hold"] is False
    assert dispatch["relaunch_distinct_route"] is True
    assert (
        dispatch["disposition"]["cause"]
        == terminal_failure_classification.CAUSE_PROVIDER_TRANSIENT
    )


def test_a_manager_cancelled_reviewer_is_never_route_retry() -> None:
    dispatch = review_orchestrator.reviewer_recovery_dispatch(
        _terminal_status(substatus="cancelled"), attempts_made=1,
    )

    assert dispatch["action"] == terminal_failure_classification.ACTION_CANCELLATION_FINAL
    assert dispatch["route_hold"] is True
    assert dispatch["relaunch_distinct_route"] is False


def test_a_reviewer_that_submitted_is_never_route_retry() -> None:
    dispatch = review_orchestrator.reviewer_recovery_dispatch(
        _terminal_status(submitted=True), attempts_made=1,
    )

    assert dispatch["action"] != terminal_failure_classification.ACTION_ROUTE_RETRY
    assert dispatch["relaunch_distinct_route"] is False


def test_the_implementation_worker_classification_is_unchanged() -> None:
    disposition = terminal_failure_classification.failure_disposition_from_substatus(
        terminal_substatus="worker_failed", reason=TRANSPORT_CANCEL,
    )

    assert (
        disposition["cause"]
        == terminal_failure_classification.CAUSE_PROVIDER_RUNTIME_UNCLASSIFIED
    )
    assert (
        disposition["action"]
        == terminal_failure_classification.ACTION_MANAGER_JUDGMENT_UNKNOWN
    )
    # The transport code is still recognised, still unplaced, and still no
    # turn-behaviour code.
    assert "vscode_lm_request_cancelled" not in terminal_failure_classification._REASON_CAUSE
    assert terminal_failure_classification.reviewer_route_behaviour_code(TRANSPORT_CANCEL) == ""


# -- the orchestrator relaunches exactly once --------------------------------


def test_a_transport_cancel_without_submit_relaunches_once_on_a_distinct_route(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _first_attempt_ends(monkeypatch, tmp_path)

    assert driver._execute(_correctness(chain, "accept")) is None

    first, second = driver._route_attempts(chain.chain_id, "correctness")
    assert first["state"] == "retired"
    assert review_orchestrator.route_attempt_hold(first["failure_reason"]) == ""
    assert (second["attempt_index"], second["state"]) == (2, "launched")
    assert (second["runner"], second["adapter_id"], second["model"]) != (
        first["runner"], first["adapter_id"], first["model"]
    )
    assert second["adapter_id"] == "claude_cli"
    assert len(manager.launches) == 2
    assert manager.launches[-1]["adapter_id"] == "claude_cli"
    assert manager.accepts == []


def test_a_second_transport_cancel_hits_the_ceiling_and_relaunches_nothing(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _first_attempt_ends(monkeypatch, tmp_path)
    accept = _correctness(chain, "accept")
    assert driver._execute(accept) is None
    _first, second = driver._route_attempts(chain.chain_id, "correctness")
    manager.statuses[second["reviewer_request_id"]] = _terminal_status(second)

    assert driver._execute(accept) is None

    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == review_orchestrator._MAX_ROUTE_ATTEMPTS == 2
    assert all(attempt["state"] == "retired" for attempt in attempts)
    assert len(manager.launches) == 2
    # A later pass waits on the named ceiling instead of planning a third route.
    with pytest.raises(review_orchestrator._DeferredLaunch) as deferred:
        driver._execute(accept)
    assert deferred.value.reason == "review_route_retries_exhausted:correctness"
    assert len(manager.launches) == 2


def test_a_manager_cancelled_reviewer_is_not_relaunched(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _first_attempt_ends(
        monkeypatch, tmp_path, substatus="cancelled",
    )

    assert driver._execute(_correctness(chain, "accept")) is None

    (first,) = driver._route_attempts(chain.chain_id, "correctness")
    assert first["state"] == "retired"
    assert review_orchestrator.route_attempt_hold(first["failure_reason"]) == (
        terminal_failure_classification.ACTION_CANCELLATION_FINAL
    )
    assert len(manager.launches) == 1
    with pytest.raises(review_orchestrator._DeferredLaunch) as deferred:
        driver._execute(_correctness(chain, "accept"))
    assert deferred.value.reason.startswith("review_route_hold:")
    assert len(manager.launches) == 1


def test_a_reviewer_that_submitted_then_went_terminal_is_not_relaunched(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _first_attempt_ends(monkeypatch, tmp_path, submitted=True)

    assert driver._execute(_correctness(chain, "accept")) is None

    (first,) = driver._route_attempts(chain.chain_id, "correctness")
    assert first["state"] == "retired"
    assert review_orchestrator.route_attempt_hold(first["failure_reason"]) != ""
    assert len(manager.launches) == 1
