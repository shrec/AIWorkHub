"""Reviewer routing honours model disables and leaves a blind adapter (NF-2026-01620).

M4.1: ``select_reviewer_route`` took the first ranked route without consulting
the owner's model settings, so a provider, adapter or model the owner disabled
could still be picked as a reviewer.

NF-2026-01620: a vscode_lm reviewer could not read a 732 KB candidate. Its sealed
report filed only ``process_limit`` findings -- ``reviewer_could_not_inspect`` --
yet nothing retired that attempt, and no retry of the lens ever excluded the
adapter that could not read the bytes.

That retirement acts only on a report the terminal-receipt verifier has already
bound to the attempt, and only when a route off the blind adapter is selectable;
with none, the blind report is kept for the accept fold instead of being lost.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    model_settings,
    quality_evidence,
    review_orchestrator,
    terminal_failure_classification,
)
import pytest  # noqa: E402


NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)
_VSCODE_OPUS = {
    "runner": "copilot_claude-opus-5",
    "adapter_id": "vscode_lm",
    "model": "claude-opus-5",
}
_VSCODE_SONNET = {
    "runner": "copilot_claude-sonnet-5",
    "adapter_id": "vscode_lm",
    "model": "claude-sonnet-5",
}
_CLAUDE_OPUS = {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "opus-5"}
_CLAUDE_SONNET = {"runner": "claude_sonnet-5", "adapter_id": "claude_cli", "model": "sonnet-5"}
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
# What the accept path records on an attempt whose reviewer could not inspect.
COULD_NOT_INSPECT = "review_ready:" + quality_evidence.STATUS_REVIEWER_COULD_NOT_INSPECT


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


def _rank(monkeypatch, tmp_path: Path, *routes: dict) -> None:
    """The real route selector over a ranking of exactly these routes, best first."""
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


def _driver(monkeypatch, tmp_path: Path, *routes: dict):
    _rank(monkeypatch, tmp_path, *routes)
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


def _write_model_settings(repo: Path, **sections: object) -> None:
    path = repo / model_settings.SETTINGS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_id": model_settings.SCHEMA_ID, "revision": 1, **sections}),
        encoding="utf-8",
    )


def _select(tmp_path: Path, lens: str = "correctness"):
    return review_orchestrator.select_reviewer_route(tmp_path, "QUALITY_REVIEW", lens)


# -- (1) M4.1: select_reviewer_route honours the owner's model settings -------


def test_a_model_the_owner_disabled_is_never_selected(monkeypatch, tmp_path: Path) -> None:
    _rank(monkeypatch, tmp_path, _VSCODE_OPUS, _VSCODE_SONNET, _CLAUDE_OPUS)
    _write_model_settings(
        tmp_path, models={"copilot": {"vscode_lm": {"claude-opus-5": False}}}
    )

    assert _select(tmp_path) == _VSCODE_SONNET


def test_an_adapter_the_owner_disabled_is_skipped_for_every_model(
    monkeypatch, tmp_path: Path,
) -> None:
    _rank(monkeypatch, tmp_path, _VSCODE_OPUS, _VSCODE_SONNET, _CLAUDE_OPUS)
    _write_model_settings(tmp_path, adapters={"copilot": {"vscode_lm": False}})

    assert _select(tmp_path) == _CLAUDE_OPUS


def test_a_provider_the_owner_disabled_is_skipped_on_every_route(
    monkeypatch, tmp_path: Path,
) -> None:
    _rank(monkeypatch, tmp_path, _CLAUDE_OPUS, _CLAUDE_SONNET, _DEEPSEEK)
    _write_model_settings(tmp_path, providers={"anthropic": False})

    assert _select(tmp_path) == _DEEPSEEK


def test_every_candidate_disabled_raises_review_route_unavailable(
    monkeypatch, tmp_path: Path,
) -> None:
    _rank(monkeypatch, tmp_path, _VSCODE_OPUS, _CLAUDE_OPUS)
    _write_model_settings(tmp_path, providers={"copilot": False, "anthropic": False})

    with pytest.raises(RuntimeError, match=r"^review_route_unavailable:security$"):
        _select(tmp_path, "security")


def test_each_candidate_is_judged_by_model_settings_evaluate_state(
    monkeypatch, tmp_path: Path,
) -> None:
    _rank(monkeypatch, tmp_path, _VSCODE_OPUS, _CLAUDE_OPUS)
    asked: list[dict] = []

    def evaluate_state(_state, **identity):
        asked.append(identity)
        return identity["adapter"] != "vscode_lm"

    monkeypatch.setattr(model_settings, "evaluate_state", evaluate_state)

    assert _select(tmp_path) == _CLAUDE_OPUS
    # The policy identity repo_policy readiness and manager_loop_service resolve.
    assert asked == [
        {"provider": "copilot", "adapter": "vscode_lm", "model": "claude-opus-5"},
        {"provider": "anthropic", "adapter": "claude_cli", "model": "opus-5"},
    ]


def test_unreadable_model_settings_defer_with_a_named_cause_instead_of_enabling_all(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _driver(monkeypatch, tmp_path, _VSCODE_OPUS, _CLAUDE_OPUS)
    path = tmp_path / model_settings.SETTINGS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(review_orchestrator._DeferredLaunch) as deferred:
        driver._plan_deferrable_route_attempt(_correctness(chain, "launch"))

    assert deferred.value.reason.startswith(
        "review_route_model_settings_invalid:model_settings_invalid_json:"
    )
    assert driver._route_attempts(chain.chain_id, "correctness") == []
    assert manager.launches == []


def test_a_planned_reviewer_attempt_never_lands_on_a_route_the_owner_disabled(
    monkeypatch, tmp_path: Path,
) -> None:
    _manager, driver, chain = _driver(monkeypatch, tmp_path, _VSCODE_OPUS, _CLAUDE_OPUS)
    _write_model_settings(tmp_path, adapters={"copilot": {"vscode_lm": False}})

    planned = driver._plan_route_attempt(_correctness(chain, "launch"))

    assert (planned["attempt_index"], planned["adapter_id"]) == (1, "claude_cli")


# -- (2) NF-2026-01620: a lens leaves the adapter that could not inspect ------


def test_the_could_not_inspect_classifier_recognises_the_recorded_reason() -> None:
    assert review_orchestrator.reviewer_route_could_not_inspect(COULD_NOT_INSPECT) is True
    # Neither existing adapter classifier claims it, so without this one the
    # retired attempt indicted only its exact route.
    assert review_orchestrator.reviewer_route_account_limited(COULD_NOT_INSPECT) is False
    assert terminal_failure_classification.reviewer_route_behaviour_code(COULD_NOT_INSPECT) == ""


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "worker_failed:mechanical_terminal",
        "launch_failed:provider_account_limit",
        "worker_failed:vscode_lm_quality_review_submit_required:exit_code=1",
        "review_ready:reviewer_could_not_inspect_typo",
        "review_ready:xreviewer_could_not_inspect",
    ],
)
def test_the_could_not_inspect_classifier_ignores_every_other_reason(reason) -> None:
    assert review_orchestrator.reviewer_route_could_not_inspect(reason) is False


def test_a_could_not_inspect_attempt_moves_the_successor_off_the_whole_adapter(
    monkeypatch, tmp_path: Path,
) -> None:
    _manager, driver, chain = _driver(
        monkeypatch, tmp_path, _VSCODE_OPUS, _VSCODE_SONNET, _CLAUDE_OPUS
    )
    launch = _correctness(chain, "launch")
    first = driver._plan_route_attempt(launch)
    assert driver._retire_route_attempt(launch, first, COULD_NOT_INSPECT) is True

    second = driver._plan_route_attempt(launch)

    assert first["adapter_id"] == "vscode_lm"
    assert second["attempt_index"] == 2
    assert second["adapter_id"] == "claude_cli"


def _blind_status(attempt: dict, lens: str = "correctness") -> dict:
    """A sealed review_ready status whose own report filed only process_limit findings."""
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
            "can_mutate_repo": False,
            "findings": [{
                "id": "process-limit-1",
                "disposition": quality_evidence.FINDING_DISPOSITION_PROCESS_LIMIT,
                "severity": "low",
                "summary": "candidate exceeds what this route can read",
                "evidence": "the 732 KB candidate is past this route's read limit",
            }],
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


def _first_attempt_ends_blind(monkeypatch, tmp_path: Path):
    """Launch the correctness reviewer on vscode_lm and seal a blind report for it."""
    manager, driver, chain = _driver(
        monkeypatch, tmp_path, _VSCODE_OPUS, _VSCODE_SONNET, _CLAUDE_OPUS
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    (first,) = driver._route_attempts(chain.chain_id, "correctness")
    manager.statuses[first["reviewer_request_id"]] = _blind_status(first)
    return manager, driver, chain


def test_a_blind_sealed_report_is_recorded_and_the_retry_leaves_its_adapter(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _first_attempt_ends_blind(monkeypatch, tmp_path)

    assert driver._execute(_correctness(chain, "accept")) is None

    first, second = driver._route_attempts(chain.chain_id, "correctness")
    assert (first["adapter_id"], first["state"]) == ("vscode_lm", "retired")
    assert first["failure_reason"] == COULD_NOT_INSPECT
    assert (second["adapter_id"], second["state"]) == ("claude_cli", "launched")
    assert manager.launches[-1]["adapter_id"] == "claude_cli"
    # A report that could not inspect the candidate is never this lens's verdict.
    assert manager.accepts == []


def test_the_last_slot_keeps_the_blind_report_for_the_accept_fold(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _first_attempt_ends_blind(monkeypatch, tmp_path)
    accept = _correctness(chain, "accept")
    assert driver._execute(accept) is None
    _first, second = driver._route_attempts(chain.chain_id, "correctness")
    manager.statuses[second["reviewer_request_id"]] = _blind_status(second)

    receipt = driver._execute(accept)

    assert receipt is not None
    assert receipt["reviewer_request_id"] == second["reviewer_request_id"]
    assert manager.accepts == [(second["reviewer_request_id"], second["reviewer_task_id"])]
    assert len(driver._route_attempts(chain.chain_id, "correctness")) == (
        review_orchestrator._MAX_ROUTE_ATTEMPTS
    )


def _tamper_event_copy(status: dict) -> None:
    """The process event's copy differs from the card's sealed receipt in one field."""
    sealed = status["latest_event"]["quality_review_receipt"]
    status["latest_event"] = {
        "quality_review_receipt": {**sealed, "logical_submission_count": 2}
    }


def _tamper_packet_binding(status: dict) -> None:
    """The sealed receipt itself -- the event copy is the same object -- names another packet."""
    status["latest_event"]["quality_review_receipt"]["packet_sha256"] = "c" * 64


@pytest.mark.parametrize(
    ("tamper", "refusal"),
    [
        (_tamper_event_copy, "reviewer_terminal_receipt_mismatch"),
        (_tamper_packet_binding, "reviewer_receipt_binding_invalid"),
    ],
    ids=["event-copy-differs", "packet-mis-bound"],
)
def test_a_blind_report_the_verifier_refuses_never_retires_its_attempt(
    monkeypatch, tmp_path: Path, tamper, refusal,
) -> None:
    manager, driver, chain = _first_attempt_ends_blind(monkeypatch, tmp_path)
    (first,) = driver._route_attempts(chain.chain_id, "correctness")
    tamper(manager.statuses[first["reviewer_request_id"]])

    with pytest.raises(RuntimeError, match=f"^{refusal}$"):
        driver._execute(_correctness(chain, "accept"))

    (still,) = driver._route_attempts(chain.chain_id, "correctness")
    assert still["state"] == "launched"
    assert len(manager.launches) == 1
    assert manager.accepts == []


@pytest.mark.parametrize(
    "routes",
    [(_VSCODE_OPUS, _VSCODE_SONNET), (_CLAUDE_OPUS, _CLAUDE_SONNET)],
    ids=["every-route-vscode_lm", "every-route-claude_cli"],
)
def test_no_route_off_the_blind_adapter_keeps_the_blind_report_for_the_accept_fold(
    monkeypatch, tmp_path: Path, routes,
) -> None:
    manager, driver, chain = _driver(monkeypatch, tmp_path, *routes)
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    (first,) = driver._route_attempts(chain.chain_id, "correctness")
    status = _blind_status(first)
    manager.statuses[first["reviewer_request_id"]] = status

    # No _DeferredLaunch: with no successor there is nothing to retire it for.
    receipt = driver._execute(_correctness(chain, "accept"))

    (still,) = driver._route_attempts(chain.chain_id, "correctness")
    assert still["state"] == "launched"
    assert len(manager.launches) == 1
    assert receipt is not None
    assert receipt["reviewer_request_id"] == first["reviewer_request_id"]
    assert manager.accepts == [(first["reviewer_request_id"], first["reviewer_task_id"])]
    # The report is not dropped: the receipt binds the blind report, and the
    # accept fold names the lens reviewer_could_not_inspect from it.
    report = status["latest_event"]["quality_review_receipt"]["report"]
    assert receipt["review_report_sha256"] == review_orchestrator.canonical_digest(report)
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=quality_evidence.resolve_risk_profile(quality_evidence.RISK_HIGH),
        reviewer_reports=[report],
    )
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    (row,) = [row for row in verdict["lenses"] if row["lens"] == "correctness"]
    assert row["status"] == quality_evidence.STATUS_REVIEWER_COULD_NOT_INSPECT


def test_unreadable_model_settings_at_the_blind_decision_defer_without_retiring(
    monkeypatch, tmp_path: Path,
) -> None:
    manager, driver, chain = _first_attempt_ends_blind(monkeypatch, tmp_path)
    path = tmp_path / model_settings.SETTINGS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(review_orchestrator._DeferredLaunch) as deferred:
        driver._execute(_correctness(chain, "accept"))

    # Whether a successor exists is unknown, so the attempt is neither retired
    # nor accepted: the lens waits on the named cause and asks again.
    assert deferred.value.reason.startswith("review_route_model_settings_invalid:")
    (still,) = driver._route_attempts(chain.chain_id, "correctness")
    assert still["state"] == "launched"
    assert len(manager.launches) == 1
    assert manager.accepts == []
