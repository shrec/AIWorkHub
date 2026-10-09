from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_orchestrator, terminal_failure_classification  # noqa: E402
import pytest  # noqa: E402


_VSCODE_LM_ROUTE = {
    "runner": "copilot_claude-opus-5",
    "adapter_id": "vscode_lm",
    "model": "claude-opus-5",
}
_OTHER_ROUTE = {
    "runner": "deepseek_native-v4-pro",
    "adapter_id": "deepseek_copilot_cli",
    "model": "deepseek-v4-pro",
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


def _reviewer_status(diagnostic: str) -> dict:
    """A terminal reviewer status whose only failure signal is this diagnostic."""
    return {
        "ok": True,
        "request_id": "review-request",
        "task_id": "QUALITY_REVIEW_TASK",
        "state": "worker_failed",
        "latest_event": {
            "failure_kind": "worker_failed",
            "diagnostic": diagnostic,
        },
        "task_card": {
            "terminal_substatus": "worker_failed",
            "worker_status": "worker_failed",
        },
    }


# -- (a) reviewer_route_behaviour_code ---------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "worker_failed:vscode_lm_quality_review_submit_required:exit_code=1",
            "vscode_lm_quality_review_submit_required",
        ),
        (
            "worker_failed:vscode_lm_request_failed:exit_code=1",
            "vscode_lm_request_failed",
        ),
        ("vscode_lm_agent_turn_limit", "vscode_lm_agent_turn_limit"),
    ],
)
def test_reviewer_route_behaviour_code_finds_the_code_past_a_prefix(
    reason: str, expected: str,
) -> None:
    assert terminal_failure_classification.reviewer_route_behaviour_code(reason) == expected


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "worker_failed:provider_timeout:exit_code=1",
        "manager_judgment_unknown",
        "worker_failed:runtime_error",
        "vscode_lm_quality_review_submit_required_typo",
    ],
)
def test_reviewer_route_behaviour_code_is_empty_for_unrelated_reasons(reason: str) -> None:
    assert terminal_failure_classification.reviewer_route_behaviour_code(reason) == ""


# -- (b) reviewer_terminal_disposition routes instead of holding ------------


def test_reviewer_terminal_disposition_routes_retry_not_manager_judgment_unknown() -> None:
    status = _reviewer_status(
        "worker_failed:vscode_lm_quality_review_submit_required:exit_code=1"
    )

    disposition = review_orchestrator.reviewer_terminal_disposition(status)

    assert disposition["action"] == terminal_failure_classification.ACTION_ROUTE_RETRY
    assert (
        disposition["action"]
        != terminal_failure_classification.ACTION_MANAGER_JUDGMENT_UNKNOWN
    )
    assert disposition["cause"] == terminal_failure_classification.CAUSE_PROVIDER_TRANSIENT


def test_reviewer_terminal_disposition_routes_retry_for_the_request_failed_wrapper() -> None:
    status = _reviewer_status("worker_failed:vscode_lm_request_failed:exit_code=1")

    disposition = review_orchestrator.reviewer_terminal_disposition(status)

    assert disposition["action"] == terminal_failure_classification.ACTION_ROUTE_RETRY


# -- (c) select_reviewer_route excludes the whole failing adapter -----------


def test_select_reviewer_route_with_excluded_adapters_skips_the_whole_adapter(
    monkeypatch, tmp_path: Path,
) -> None:
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "rank_task",
        lambda *_args, **_kwargs: {
            "launch_contract": dict(_VSCODE_LM_ROUTE),
            "candidates": [
                {
                    **_VSCODE_LM_ROUTE,
                    "execution_runner": _VSCODE_LM_ROUTE["runner"],
                    "excluded": False,
                },
                {
                    **_OTHER_ROUTE,
                    "execution_runner": _OTHER_ROUTE["runner"],
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
        tmp_path, "QUALITY_REVIEW_EXCLUDED_ADAPTER", "correctness",
        excluded_adapters=frozenset({"vscode_lm"}),
    )

    assert route == _OTHER_ROUTE
    assert route["adapter_id"] != "vscode_lm"


def test_select_reviewer_route_raises_when_excluded_adapters_leaves_no_route(
    monkeypatch, tmp_path: Path,
) -> None:
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "rank_task",
        lambda *_args, **_kwargs: {
            "launch_contract": dict(_VSCODE_LM_ROUTE),
            "candidates": [
                {
                    **_VSCODE_LM_ROUTE,
                    "execution_runner": _VSCODE_LM_ROUTE["runner"],
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

    with pytest.raises(RuntimeError, match="review_route_unavailable:"):
        review_orchestrator.select_reviewer_route(
            tmp_path, "QUALITY_REVIEW_NO_ROUTE_LEFT", "correctness",
            excluded_adapters=frozenset({"vscode_lm"}),
        )


# -- (d) non-reviewer worker classification is unchanged --------------------


def test_worker_reason_with_vscode_lm_code_still_classifies_as_before() -> None:
    reason = "worker_failed:vscode_lm_quality_review_submit_required:exit_code=1"

    disposition = terminal_failure_classification.failure_disposition_from_substatus(
        terminal_substatus="worker_failed",
        reason=reason,
    )

    assert (
        disposition["cause"]
        == terminal_failure_classification.CAUSE_PROVIDER_RUNTIME_UNCLASSIFIED
    )
    assert (
        disposition["action"]
        == terminal_failure_classification.ACTION_MANAGER_JUDGMENT_UNKNOWN
    )
