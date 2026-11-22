"""A reviewer successor that cannot start falls back to one that can (NF-2026-02356).

Field 0.13.9 GeoAI, five times in one night: the first reviewer was retired as
could-not-inspect (NF-2026-01620), the automatically planned cross-provider
successor (deepseek v4-pro) failed AT START, and the lens parked until a
hand-launched claude_cli reviewer completed it.

Measured shape (the real dispatch over fake launcher receipts): the successor
retired as ``launch_failed:mechanical_terminal:hold=manager_judgment_unknown``
with ``relaunch_distinct_route`` False, and attempt 2 already met the NF847
``_MAX_ROUTE_ATTEMPTS`` ceiling, so the lens waited on
``review_route_retries_exhausted``.

Fakes only: no provider CLI and no manager loop is ever started.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import quality_evidence, review_orchestrator  # noqa: E402
import pytest  # noqa: E402


NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)
_VSCODE_OPUS = {
    "runner": "copilot_claude-opus-5",
    "adapter_id": "vscode_lm",
    "model": "claude-opus-5",
}
_CLAUDE_OPUS = {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "opus-5"}
_DEEPSEEK = {
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
COULD_NOT_INSPECT = "review_ready:" + quality_evidence.STATUS_REVIEWER_COULD_NOT_INSPECT
CEILING = review_orchestrator._MAX_ROUTE_ATTEMPTS
ABSOLUTE = review_orchestrator._MAX_ROUTE_ATTEMPTS + review_orchestrator._MAX_START_FAILURE_SLOTS


def _deepseek(model: str) -> dict:
    return {**_DEEPSEEK, "runner": f"deepseek_native-{model}", "model": model}


class _Manager:
    """ProcessManager-shaped fake; ``start_failures`` maps an adapter to its start fault."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.launches: list[dict] = []
        self.accepts: list[tuple[str, str]] = []
        self.statuses: dict[str, dict] = {}
        self.requests_by_task: dict[str, str] = {}
        self.events: list[dict] = []
        self.start_failures: dict[str, str] = {}

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(dict(kwargs))
        task_id = kwargs["reviewer_task_id"]
        fault = self.start_failures.get(kwargs["adapter_id"])
        if fault == "raise":
            raise OSError("provider process could not be spawned")
        if fault and fault.startswith("raise:"):
            raise RuntimeError(fault.partition(":")[2])
        if fault == "launch_failed":
            return {
                "ok": False, "state": "launch_failed", "error": "provider_start_failed",
                "task_id": task_id, "adapter_id": kwargs["adapter_id"],
                "runner": kwargs["runner"],
            }
        if fault == "refused":
            return {"ok": False, "error": "provider_start_refused", "task_id": task_id}
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
    """A real orchestrator over the real selector, ranking exactly ``routes``."""
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
    monkeypatch.setattr(
        review_orchestrator, "_review_route_policy_enabled", lambda _policy, _route: True,
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


def _report_status(attempt: dict, findings: list[dict], lens: str = "correctness") -> dict:
    """A sealed review_ready status carrying ``findings`` as its own report."""
    packet_sha256 = hashlib.sha256(f"review-packet:target-request:{lens}".encode()).hexdigest()
    provider = attempt["adapter_id"]
    receipt = {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": packet_sha256,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 1},
        "reviewer": {
            "request_id": attempt["reviewer_request_id"],
            "task_id": attempt["reviewer_task_id"],
            "provider": provider,
        },
        "report": {
            "lens": lens, "provider": provider, "read_only": True,
            "can_mutate_repo": False, "findings": findings,
        },
        "authority": {
            "process_identity_verified": True, "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": hashlib.sha256(attempt["reviewer_request_id"].encode()).hexdigest(),
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }
    return {
        "ok": True, "state": "review_ready",
        "request_id": attempt["reviewer_request_id"],
        "task_id": attempt["reviewer_task_id"],
        "runner": attempt["runner"], "adapter_id": provider, "model": attempt["model"],
        "latest_event": {"quality_review_receipt": receipt},
        "task_card": {
            "terminal_review": {
                "evidence": {
                    "quality_review": {
                        "lens": lens, "packet_sha256": packet_sha256,
                        "target_request_id": "target-request", "target_task_id": "TARGET",
                        "target_claim_epoch": 1,
                    },
                    "quality_review_receipt": receipt,
                }
            }
        },
    }


_BLIND_FINDINGS = [{
    "id": "process-limit-1",
    "disposition": quality_evidence.FINDING_DISPOSITION_PROCESS_LIMIT,
    "severity": "low",
    "summary": "candidate exceeds what this route can read",
    "evidence": "the candidate is past this route's read limit",
}]
_VERDICT_FINDINGS = [{
    "id": "defect-1",
    "disposition": "defect",
    "actionable": True,
    "severity": "medium",
    "summary": "an off-by-one in the bound check",
    "evidence": "src/example.py:10 compares with <= instead of <",
}]


def _first_attempt_ends_blind(manager, driver, chain) -> dict:
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    (first,) = driver._route_attempts(chain.chain_id, "correctness")
    assert first["adapter_id"] == "vscode_lm"
    manager.statuses[first["reviewer_request_id"]] = _report_status(first, _BLIND_FINDINGS)
    return first


def _seed_started_route(db_path: Path, route: dict) -> None:
    """A reviewer on another chain that really started on this host."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(review_orchestrator.ROUTE_ATTEMPT_TABLE)
        conn.execute(
            "INSERT INTO review_orchestrator_route_attempts (chain_id,lens,attempt_index,"
            "reviewer_task_id,reviewer_request_id,runner,adapter_id,model,state,"
            "failure_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                9999, "correctness", 1, "OTHER-REVIEW-1", "other-request-1",
                route["runner"], route["adapter_id"], route["model"], "launched", "",
            ),
        )


@pytest.mark.parametrize("fault", ["launch_failed", "refused"])
def test_blind_reviewer_then_successor_start_failure_launches_a_third_route_whose_report_is_the_verdict(
    monkeypatch, tmp_path: Path, fault: str,
) -> None:
    manager, driver, chain = _driver(
        monkeypatch, tmp_path, _VSCODE_OPUS, _DEEPSEEK, _CLAUDE_OPUS,
    )
    _first_attempt_ends_blind(manager, driver, chain)
    manager.start_failures["deepseek_copilot_cli"] = fault

    assert driver._execute(_correctness(chain, "accept")) is None

    first, second, third = driver._route_attempts(chain.chain_id, "correctness")
    assert first["failure_reason"] == COULD_NOT_INSPECT
    assert (second["adapter_id"], second["state"]) == ("deepseek_copilot_cli", "retired")
    assert review_orchestrator.reviewer_route_start_failed(second["failure_reason"])
    assert review_orchestrator.route_attempt_hold(second["failure_reason"]) == ""
    assert (third["adapter_id"], third["state"]) == ("claude_cli", "launched")
    assert manager.accepts == []

    manager.statuses[third["reviewer_request_id"]] = _report_status(third, _VERDICT_FINDINGS)
    receipt = driver._execute(_correctness(chain, "accept"))

    assert manager.accepts == [(third["reviewer_request_id"], third["reviewer_task_id"])]
    assert receipt is not None
    assert receipt["route_attempt_index"] == 3
    assert receipt["actionable_findings"] is True


def test_successor_that_raises_twice_retires_as_start_failure_and_the_next_route_launches(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _driver(
        monkeypatch, tmp_path, _VSCODE_OPUS, _DEEPSEEK, _CLAUDE_OPUS,
    )
    _first_attempt_ends_blind(manager, driver, chain)
    manager.start_failures["deepseek_copilot_cli"] = "raise"
    action = _correctness(chain, "accept")

    # First raise: the pre-bound successor keeps its reconcile chance.
    assert driver._execute(action) is None
    _first, second = driver._route_attempts(chain.chain_id, "correctness")
    assert second["state"] == "planned"
    assert second["failure_reason"].startswith("launch_exception:OSError")

    # Second raise of the same attempt with no request bound: a start failure.
    assert driver._launch_with_successor(
        action, driver._plan_deferrable_route_attempt(action)
    ) is not None
    _first, second, third = driver._route_attempts(chain.chain_id, "correctness")
    assert second["state"] == "retired"
    assert review_orchestrator.reviewer_route_start_failed(second["failure_reason"])
    assert (third["adapter_id"], third["state"]) == ("claude_cli", "launched")


def test_every_off_adapter_route_fails_to_start_falls_back_to_a_started_route(
    monkeypatch, tmp_path: Path,
) -> None:
    # claude_cli is not even ranked; it has only its recorded start on this host.
    manager, driver, chain = _driver(monkeypatch, tmp_path, _VSCODE_OPUS, _DEEPSEEK)
    _seed_started_route(driver.db_path, _CLAUDE_OPUS)
    _first_attempt_ends_blind(manager, driver, chain)
    manager.start_failures["deepseek_copilot_cli"] = "launch_failed"

    assert driver._execute(_correctness(chain, "accept")) is None

    _first, second, third = driver._route_attempts(chain.chain_id, "correctness")
    assert review_orchestrator.reviewer_route_start_failed(second["failure_reason"])
    assert (third["adapter_id"], third["state"]) == ("claude_cli", "launched")
    assert manager.launches[-1]["adapter_id"] == "claude_cli"


def test_a_ranked_route_that_has_started_beats_a_never_started_one(
    monkeypatch, tmp_path: Path,
) -> None:
    flash = _deepseek("deepseek-v4-flash")
    manager, driver, chain = _driver(
        monkeypatch, tmp_path, _VSCODE_OPUS, flash, _CLAUDE_OPUS,
    )
    _seed_started_route(driver.db_path, _CLAUDE_OPUS)
    _first_attempt_ends_blind(manager, driver, chain)

    assert driver._execute(_correctness(chain, "accept")) is None

    _first, second = driver._route_attempts(chain.chain_id, "correctness")
    assert (second["adapter_id"], second["state"]) == ("claude_cli", "launched")
    assert all(launch["model"] != flash["model"] for launch in manager.launches)


def test_a_provider_asserted_verdict_failure_keeps_todays_hold(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _driver(
        monkeypatch, tmp_path, _VSCODE_OPUS, _DEEPSEEK, _CLAUDE_OPUS,
    )
    _first_attempt_ends_blind(manager, driver, chain)
    action = _correctness(chain, "accept")
    assert driver._execute(action) is None
    _first, second = driver._route_attempts(chain.chain_id, "correctness")
    assert (second["adapter_id"], second["state"]) == ("deepseek_copilot_cli", "launched")
    # The successor RAN and then failed: not a start failure.
    manager.statuses[second["reviewer_request_id"]] = {
        "ok": True, "state": "worker_failed",
        "task_id": second["reviewer_task_id"],
        "runner": second["runner"], "adapter_id": second["adapter_id"],
        "model": second["model"],
    }
    launches_before = len(manager.launches)

    with pytest.raises(review_orchestrator._DeferredLaunch) as caught:
        driver._plan_deferrable_route_attempt(action)

    _first, second = driver._route_attempts(chain.chain_id, "correctness")
    assert second["state"] == "retired"
    assert not review_orchestrator.reviewer_route_start_failed(second["failure_reason"])
    assert str(caught.value).startswith("review_route_")
    assert len(manager.launches) == launches_before
    assert len(driver._route_attempts(chain.chain_id, "correctness")) == 2


def test_start_failures_stay_bounded_by_the_absolute_attempt_ceiling(
    monkeypatch, tmp_path: Path,
) -> None:
    routes = [_deepseek(f"deepseek-v{n}") for n in range(4, 10)]
    manager, driver, chain = _driver(monkeypatch, tmp_path, _VSCODE_OPUS, *routes)
    _first_attempt_ends_blind(manager, driver, chain)
    manager.start_failures["deepseek_copilot_cli"] = "launch_failed"
    action = _correctness(chain, "accept")
    assert driver._execute(action) is None

    deferred = None
    for _ in range(10):
        try:
            driver._launch_with_successor(
                action, driver._plan_deferrable_route_attempt(action)
            )
        except review_orchestrator._DeferredLaunch as exc:
            deferred = exc
            break

    assert deferred is not None
    assert str(deferred).startswith("review_route_retries_exhausted:")
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == ABSOLUTE
    assert len(manager.launches) == ABSOLUTE
    assert review_orchestrator._spent_route_attempts(attempts) == 1 < CEILING


def test_start_failure_predicate_refuses_typed_holds_and_first_attempts() -> None:
    refused = {"ok": False, "state": "launch_failed", "error": "provider_start_failed"}
    unknown = {"action": "manager_judgment_unknown", "adapter_credential": False}
    successor = {"attempt_index": 2}

    _reason, dispatch = review_orchestrator._mark_start_failure(
        successor, refused, "launch_failed:mechanical_terminal", dict(unknown),
    )
    assert dispatch["start_failure"] is True and dispatch["relaunch_distinct_route"] is True
    # The first attempt is no successor: today's behaviour.
    _reason, dispatch = review_orchestrator._mark_start_failure(
        {"attempt_index": 1}, refused, "launch_failed:mechanical_terminal", dict(unknown),
    )
    assert "start_failure" not in dispatch
    for action in ("credential_hold", "dependency_hold", "capacity_hold"):
        assert not review_orchestrator._reviewer_start_failure(
            refused, {"action": action, "adapter_credential": False},
        )
    assert not review_orchestrator._reviewer_start_failure(
        refused, {**unknown, "adapter_credential": True},
    )
    assert not review_orchestrator._reviewer_start_failure(
        {"ok": True, "state": "worker_failed"}, unknown,
    )
    assert not review_orchestrator._reviewer_start_failure(
        {"ok": False, "error": "x", "recovery_state": "starting_reservation_retained"},
        unknown,
    )


# F0: a provider process that exited before any output or heartbeat never ran.
@pytest.mark.parametrize("stdout_shape", ["missing", "empty"])
def test_provider_that_exited_before_any_output_or_heartbeat_is_a_start_failure(
    tmp_path: Path, stdout_shape: str,
) -> None:
    stdout = tmp_path / "provider.stdout.jsonl"
    if stdout_shape == "empty":
        stdout.write_bytes(b"")
    exited = {
        "ok": True, "state": "worker_failed",
        "metadata": {"stdout_path": str(stdout)},
    }
    unknown = {"action": "manager_judgment_unknown", "adapter_credential": False}

    assert review_orchestrator._reviewer_start_failure(exited, unknown)
    # A heartbeat is progress: the provider started.
    assert not review_orchestrator._reviewer_start_failure(
        {**exited, "last_heartbeat_at": "2026-11-22T00:00:01Z"}, unknown,
    )
    # A credential hold is a typed cause a second route cannot fix.
    assert not review_orchestrator._reviewer_start_failure(
        exited, {"action": "credential_hold", "adapter_credential": False},
    )
    assert not review_orchestrator._reviewer_start_failure(
        exited, {**unknown, "adapter_credential": True},
    )
    # A status that names no stdout record is unknown, never a start failure.
    assert not review_orchestrator._reviewer_start_failure(
        {"ok": True, "state": "worker_failed"}, unknown,
    )


def test_provider_that_wrote_output_before_failing_is_not_a_start_failure(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "provider.stdout.jsonl"
    stdout.write_bytes(b'{"type":"system","subtype":"init"}\n')
    ran = {"ok": True, "state": "worker_failed", "metadata": {"stdout_path": str(stdout)}}

    assert not review_orchestrator._reviewer_start_failure(
        ran, {"action": "manager_judgment_unknown", "adapter_credential": False},
    )


# F1: a successor that raises twice with a typed hold keeps today's reconcile path.
@pytest.mark.parametrize(
    "typed_hold",
    [
        review_orchestrator.terminal_failure_classification.VSCODE_LM_CREDENTIAL_REJECTED,
        review_orchestrator._ACCOUNT_LIMIT_FAILURE_CLASS,
    ],
)
def test_successor_that_raises_twice_with_a_typed_hold_is_not_a_start_failure(
    monkeypatch, tmp_path: Path, typed_hold: str,
) -> None:
    manager, driver, chain = _driver(
        monkeypatch, tmp_path, _VSCODE_OPUS, _DEEPSEEK, _CLAUDE_OPUS,
    )
    _first_attempt_ends_blind(manager, driver, chain)
    manager.start_failures["deepseek_copilot_cli"] = f"raise:{typed_hold}"
    action = _correctness(chain, "accept")

    assert driver._execute(action) is None
    _first, second = driver._route_attempts(chain.chain_id, "correctness")
    assert second["state"] == "planned"

    # Second raise of the same attempt: the typed hold is NOT a start failure.
    assert driver._launch_with_successor(
        action, driver._plan_deferrable_route_attempt(action)
    ) is None
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 2
    assert not review_orchestrator.reviewer_route_start_failed(attempts[1]["failure_reason"])
    assert all(launch["adapter_id"] != "claude_cli" for launch in manager.launches)


# F2: the started-route scan is a plain read.
def test_started_routes_on_a_db_without_the_side_table_returns_nothing_and_creates_no_table(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review.sqlite"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    holder = SimpleNamespace(db_path=db_path)

    assert review_orchestrator.ReviewOrchestrator._started_routes(holder) == ()

    with closing(sqlite3.connect(db_path)) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert tables == {"unrelated"}


def test_started_routes_reads_promptly_while_another_writer_holds_the_lock(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review.sqlite"
    _seed_started_route(db_path, _CLAUDE_OPUS)
    holder = SimpleNamespace(db_path=db_path)
    writer = sqlite3.connect(db_path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        routes = review_orchestrator.ReviewOrchestrator._started_routes(holder)
        elapsed = time.monotonic() - started
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert routes == ((_CLAUDE_OPUS["runner"], _CLAUDE_OPUS["adapter_id"], _CLAUDE_OPUS["model"]),)
    assert elapsed < 2.0
