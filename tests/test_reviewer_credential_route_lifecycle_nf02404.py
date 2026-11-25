"""NF-2026-02404: a vscode_lm credential rejection trips the route circuit,
demotes the route for reviewer selection until it succeeds again, and archives
the dead earlier reviewer attempts of a lens once a replacement verifies it.

Fakes only: no provider CLI is started.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pytest  # noqa: E402

import test_review_lens_failover_nf02086 as failover  # noqa: E402
import test_reviewer_route_circuit_nf01588 as circuit_case  # noqa: E402
from aiworkhub import review_orchestrator, workforce_catalog  # noqa: E402

NOW = 1_800_000_000.0
CREDENTIAL_ERROR = "worker_failed:vscode_lm_credential_rejected:exit_code=1"
DIAGNOSTICS_ERROR = (
    "worker_failed:exit_code=1:diagnostics=vscode_lm_credential_rejected"
)
VSCODE_LM_ROUTE = circuit_case._VSCODE_LM_ROUTE
OTHER_ROUTE = circuit_case._OTHER_ROUTE
DEMOTED_LAST_RESORT = {"reason": "credential_unrecovered_last_resort"}


def _failure(adapter_id: str, model: str, error: str, *, age_seconds: float) -> dict:
    finished = datetime.fromtimestamp(NOW - age_seconds, tz=timezone.utc).isoformat()
    return {
        "adapter_id": adapter_id,
        "model": model,
        "state": "worker_failed",
        "error": error,
        "finished_at": finished,
    }


def _success(adapter_id: str, model: str, *, age_seconds: float) -> dict:
    finished = datetime.fromtimestamp(NOW - age_seconds, tz=timezone.utc).isoformat()
    return {
        "adapter_id": adapter_id,
        "model": model,
        "state": "review_ready",
        "error": "",
        "finished_at": finished,
    }


def _circuit_after(tmp_path: Path, adapter_id: str, model: str, *observations: dict) -> dict:
    return workforce_catalog.route_circuit_for(
        tmp_path, adapter_id, model, now_epoch=NOW, observations=list(observations),
    )


# --- (1) the classifier trips the shared single-failure circuit -------------


def test_vscode_lm_credential_rejection_opens_the_route_circuit(tmp_path: Path) -> None:
    circuit = _circuit_after(
        tmp_path, "vscode_lm", "glm-5.3",
        _failure("vscode_lm", "glm-5.3", CREDENTIAL_ERROR, age_seconds=10),
    )

    assert circuit["state"] == "open"
    assert circuit["failure_kind"] == "auth"
    assert circuit["threshold"] == 1


def test_the_same_token_inside_diagnostics_never_trips_the_circuit(tmp_path: Path) -> None:
    circuit = _circuit_after(
        tmp_path, "vscode_lm", "glm-5.3",
        _failure("vscode_lm", "glm-5.3", DIAGNOSTICS_ERROR, age_seconds=10),
    )

    assert circuit["state"] == "closed"
    assert circuit["failure_kind"] == ""


def test_the_same_token_on_a_non_vscode_lm_adapter_never_trips_the_circuit(
    tmp_path: Path,
) -> None:
    circuit = _circuit_after(
        tmp_path, "copilot", "gpt-5.5",
        _failure("copilot", "gpt-5.5", CREDENTIAL_ERROR, age_seconds=10),
    )

    assert circuit["state"] == "closed"


def test_a_credential_route_past_its_cooldown_is_half_open(tmp_path: Path) -> None:
    cooldown = workforce_catalog.ROUTE_CIRCUIT_COOLDOWN_SECONDS
    circuit = _circuit_after(
        tmp_path, "vscode_lm", "glm-5.3",
        _failure("vscode_lm", "glm-5.3", CREDENTIAL_ERROR, age_seconds=cooldown + 60),
    )

    assert circuit["state"] == "half_open"
    assert circuit["failure_kind"] == "auth"


def test_a_success_after_the_credential_failure_closes_the_circuit(tmp_path: Path) -> None:
    cooldown = workforce_catalog.ROUTE_CIRCUIT_COOLDOWN_SECONDS
    circuit = _circuit_after(
        tmp_path, "vscode_lm", "glm-5.3",
        _failure("vscode_lm", "glm-5.3", CREDENTIAL_ERROR, age_seconds=cooldown + 120),
        _success("vscode_lm", "glm-5.3", age_seconds=30),
    )

    assert circuit["state"] == "closed"


# --- (2) selection demotes a half_open credential route --------------------


def _patch_circuits(monkeypatch, circuit_for) -> None:
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "route_circuit_for",
        lambda _repo, adapter_id, model: circuit_for(str(adapter_id), str(model)),
    )


def _credential_unrecovered(adapter_id: str, _model: str) -> dict:
    return {"state": "half_open", "failure_kind": "auth"}


def _closed(_adapter_id: str, _model: str) -> dict:
    return {"state": "closed", "failure_kind": ""}


def _open(_adapter_id: str, _model: str) -> dict:
    return {"state": "open", "failure_kind": "auth"}


def _demote_vscode_lm(adapter_id: str, model: str) -> dict:
    if adapter_id == "vscode_lm":
        return _credential_unrecovered(adapter_id, model)
    return _closed(adapter_id, model)


def test_a_demoted_credential_route_yields_to_another_launchable_route(
    monkeypatch, tmp_path: Path,
) -> None:
    circuit_case._rank(monkeypatch, tmp_path, VSCODE_LM_ROUTE, OTHER_ROUTE)
    _patch_circuits(monkeypatch, _demote_vscode_lm)

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_DEMOTED", "correctness",
    )

    assert route == OTHER_ROUTE


def test_a_demoted_credential_route_is_the_last_resort_when_nothing_else_launches(
    monkeypatch, tmp_path: Path,
) -> None:
    circuit_case._rank(monkeypatch, tmp_path, VSCODE_LM_ROUTE)
    _patch_circuits(monkeypatch, _credential_unrecovered)

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_LAST_RESORT", "correctness",
    )

    assert route == {**VSCODE_LM_ROUTE, "route_fallback": DEMOTED_LAST_RESORT}


def test_a_demoted_route_still_beats_a_hard_refused_route_as_last_resort(
    monkeypatch, tmp_path: Path,
) -> None:
    circuit_case._rank(monkeypatch, tmp_path, VSCODE_LM_ROUTE, OTHER_ROUTE)
    _patch_circuits(
        monkeypatch,
        lambda adapter_id, model: (
            _credential_unrecovered(adapter_id, model)
            if adapter_id == "vscode_lm" else _open(adapter_id, model)
        ),
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_OPEN_OTHER", "correctness",
    )

    assert route == {**VSCODE_LM_ROUTE, "route_fallback": DEMOTED_LAST_RESORT}


def test_an_open_route_stays_hard_refused(monkeypatch, tmp_path: Path) -> None:
    circuit_case._rank(monkeypatch, tmp_path, VSCODE_LM_ROUTE)
    _patch_circuits(monkeypatch, _open)

    with pytest.raises(review_orchestrator.ReviewRouteUnavailable):
        review_orchestrator.select_reviewer_route(
            tmp_path, "QUALITY_REVIEW_OPEN", "correctness",
        )


def test_a_recovered_credential_route_ranks_normally_again(
    monkeypatch, tmp_path: Path,
) -> None:
    circuit_case._rank(monkeypatch, tmp_path, VSCODE_LM_ROUTE, OTHER_ROUTE)
    _patch_circuits(monkeypatch, _closed)

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_RECOVERED", "correctness",
    )

    assert route == VSCODE_LM_ROUTE


# --- (3) the lens archive action supersedes dead earlier attempts ----------


def _chain_with_two_attempts(tmp_path: Path):
    _manager, _db_path, chain, _first_task, seed = failover.base._seed_terminal_launch(
        tmp_path, "superseded-attempts", failover._denied_launch(),
    )
    seed.drain(max_actions=1, now=failover.base.NOW)
    first, successor = seed._route_attempts(chain.chain_id, "correctness")
    return seed, chain, first, successor


def test_archive_supersedes_each_earlier_attempt_with_the_verifying_request(
    tmp_path: Path, monkeypatch,
) -> None:
    seed, chain, first, successor = _chain_with_two_attempts(tmp_path)
    archived: list[tuple[str, str]] = []

    def _archive(_repo, task_id, *, actor, reason):
        archived.append((task_id, reason))
        return {"ok": True}

    monkeypatch.setattr(review_orchestrator.task_engine, "archive_task", _archive)
    monkeypatch.setattr(seed, "_is_archived", lambda _task_id: False)

    seed._archive_superseded_attempts(
        chain.chain_id, "correctness", str(successor["reviewer_task_id"]),
        "reviewer-request-accepted",
    )

    assert archived == [
        (
            str(first["reviewer_task_id"]),
            "superseded reviewer attempt:verified_by:reviewer-request-accepted",
        ),
    ]


def test_an_already_archived_attempt_is_not_archived_twice(
    tmp_path: Path, monkeypatch,
) -> None:
    seed, chain, first, successor = _chain_with_two_attempts(tmp_path)
    archived: list[str] = []

    def _archive(_repo, task_id, *, actor, reason):
        archived.append(task_id)
        return {"ok": True}

    monkeypatch.setattr(review_orchestrator.task_engine, "archive_task", _archive)
    monkeypatch.setattr(
        seed, "_is_archived",
        lambda task_id: task_id == str(first["reviewer_task_id"]),
    )

    seed._archive_superseded_attempts(
        chain.chain_id, "correctness", str(successor["reviewer_task_id"]),
        "reviewer-request-accepted",
    )

    assert archived == []


def test_a_failing_attempt_archive_does_not_fail_the_action(
    tmp_path: Path, monkeypatch,
) -> None:
    seed, chain, first, successor = _chain_with_two_attempts(tmp_path)
    attempted: list[str] = []

    def _archive(_repo, task_id, *, actor, reason):
        attempted.append(task_id)
        raise RuntimeError("archive_store_unavailable")

    monkeypatch.setattr(review_orchestrator.task_engine, "archive_task", _archive)
    monkeypatch.setattr(seed, "_is_archived", lambda _task_id: False)

    seed._archive_superseded_attempts(
        chain.chain_id, "correctness", str(successor["reviewer_task_id"]),
        "reviewer-request-accepted",
    )

    assert attempted == [str(first["reviewer_task_id"])]


def test_the_lens_archive_action_archives_the_dead_earlier_attempt(
    tmp_path: Path, monkeypatch,
) -> None:
    base = failover.base
    manager, db_path, chain, first_task, seed = base._seed_terminal_launch(
        tmp_path, "archive-sweep", failover._denied_launch(),
    )
    seed.drain(max_actions=1, now=base.NOW)
    first, successor = seed._route_attempts(chain.chain_id, "correctness")
    successor_request = str(successor["reviewer_request_id"])
    successor_task = str(successor["reviewer_task_id"])
    manager.status_results[successor_request] = base._review_status(
        reviewer_request=successor_request,
        reviewer_task=successor_task,
        provider=base.SUCCESSOR_ROUTE["adapter_id"],
        route=base.SUCCESSOR_ROUTE,
    )
    archived: list[tuple[str, str]] = []

    def _archive(_repo, task_id, *, actor, reason):
        archived.append((task_id, reason))
        return {"ok": True}

    monkeypatch.setattr(review_orchestrator.task_engine, "archive_task", _archive)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db_path, route_selector=base._two_route_selector(first_task),
    )
    monkeypatch.setattr(driver, "_is_archived", lambda _task_id: False)

    assert driver.drain(max_actions=1, now=base.NOW).completed == 1  # accept
    assert archived == []
    assert driver.drain(max_actions=1, now=base.NOW).completed == 1  # archive

    assert [task for task, _reason in archived] == [
        successor_task, str(first["reviewer_task_id"]),
    ]
    assert archived[1][1] == (
        f"superseded reviewer attempt:verified_by:{successor_request}"
    )
