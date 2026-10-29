"""NF-2026-01588: reviewer route selection honors the route failure circuit."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from aiworkhub import review_orchestrator  # noqa: E402

# The route the quota outage kept relaunching reviewers on.
_VSCODE_LM_ROUTE = {
    "runner": "vscode_lm",
    "adapter_id": "vscode_lm",
    "model": "glm-5.3",
}
_OTHER_ROUTE = {
    "runner": "copilot",
    "adapter_id": "copilot",
    "model": "gpt-5.5",
}
_CATALOG: dict = {"routes": []}


def _rank(monkeypatch, tmp_path: Path, *routes: dict) -> None:
    """The real route selector over a ranking of exactly these routes, best first."""
    review_orchestrator.reset_routing_catalog_cache()
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
        review_orchestrator,
        "_review_route_policy_enabled",
        lambda _policy, _route: True,
    )


def _circuit(monkeypatch, state_for) -> list[tuple[str, str]]:
    """Patch route_circuit_for; return the (adapter_id, model) pairs consulted."""
    consulted: list[tuple[str, str]] = []

    def _route_circuit_for(_repo, adapter_id, model):
        consulted.append((str(adapter_id), str(model)))
        return {"state": state_for(adapter_id, model)}

    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "route_circuit_for",
        _route_circuit_for,
    )
    return consulted


def test_a_route_whose_circuit_is_open_is_skipped(
    monkeypatch, tmp_path: Path,
) -> None:
    _rank(monkeypatch, tmp_path, _VSCODE_LM_ROUTE, _OTHER_ROUTE)
    open_route = (_VSCODE_LM_ROUTE["adapter_id"], _VSCODE_LM_ROUTE["model"])
    consulted = _circuit(
        monkeypatch,
        lambda adapter_id, model: "open" if (adapter_id, model) == open_route else "closed",
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_CIRCUIT_OPEN", "correctness",
    )

    assert route == _OTHER_ROUTE
    assert open_route in consulted


def test_a_circuit_that_cannot_be_read_excludes_no_route(
    monkeypatch, tmp_path: Path,
) -> None:
    _rank(monkeypatch, tmp_path, _VSCODE_LM_ROUTE, _OTHER_ROUTE)

    def _unreadable(_adapter_id, _model):
        raise RuntimeError("circuit_unreadable")

    _circuit(monkeypatch, _unreadable)

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_CIRCUIT_UNREADABLE", "correctness",
    )

    assert route == _VSCODE_LM_ROUTE


def test_every_circuit_open_raises_review_route_unavailable(
    monkeypatch, tmp_path: Path,
) -> None:
    _rank(monkeypatch, tmp_path, _VSCODE_LM_ROUTE, _OTHER_ROUTE)
    _circuit(monkeypatch, lambda _adapter_id, _model: "open")

    with pytest.raises(RuntimeError, match="review_route_unavailable:correctness"):
        review_orchestrator.select_reviewer_route(
            tmp_path, "QUALITY_REVIEW_CIRCUIT_ALL_OPEN", "correctness",
        )
