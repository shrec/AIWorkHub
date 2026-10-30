"""NF-2026-01856: reviewer route fallback and per-candidate skip diagnostics."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from aiworkhub import review_orchestrator  # noqa: E402

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)
SOFT = "capability_not_declared_for_route_family:source-graph"
CANNOT_RUN = "worker_unavailable"
_CATALOG: dict = {"routes": []}

_CANNOT_RUN_ROUTE = {"runner": "copilot", "adapter_id": "copilot", "model": "gpt-5.5"}
_SOFT_ROUTE = {"runner": "claude_cli", "adapter_id": "claude_cli", "model": "opus"}


def _candidate(route: dict, *reasons: str) -> dict:
    return {
        **route,
        "execution_runner": route["runner"],
        "excluded": True,
        "exclusion_reasons": list(reasons),
    }


def _stub(monkeypatch, tmp_path: Path, *candidates: dict, circuit: str = "closed") -> None:
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
        lambda *_args, **_kwargs: {"launch_contract": None, "candidates": list(candidates)},
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: dict(_CATALOG),
    )
    monkeypatch.setattr(
        review_orchestrator, "_review_route_policy_enabled", lambda _policy, _route: True,
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "route_circuit_for",
        lambda _repo, _adapter, _model: {"state": circuit},
    )


def _select(tmp_path: Path, **kwargs):
    return review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_FALLBACK", "correctness", **kwargs,
    )


def test_fallback_returns_the_soft_excluded_route_with_its_ranker_exclusion(
    monkeypatch, tmp_path: Path,
) -> None:
    _stub(
        monkeypatch, tmp_path,
        _candidate(_CANNOT_RUN_ROUTE, CANNOT_RUN),
        _candidate(_SOFT_ROUTE, SOFT),
    )

    route = _select(tmp_path)

    assert {key: route[key] for key in _SOFT_ROUTE} == _SOFT_ROUTE
    assert route["route_fallback"] == {
        "reason": "ranked_routes_exhausted",
        "ranker_exclusion": [SOFT],
    }


def test_a_route_with_any_cannot_run_reason_is_not_a_fallback(
    monkeypatch, tmp_path: Path,
) -> None:
    _stub(monkeypatch, tmp_path, _candidate(_SOFT_ROUTE, SOFT, CANNOT_RUN))

    with pytest.raises(review_orchestrator.ReviewRouteUnavailable) as caught:
        _select(tmp_path)

    assert caught.value.skipped == [
        {**_SOFT_ROUTE, "skip": f"ranker_excluded:{SOFT},{CANNOT_RUN}"}
    ]


def test_no_surviving_route_raises_with_the_exact_string_and_every_skip(
    monkeypatch, tmp_path: Path,
) -> None:
    _stub(
        monkeypatch, tmp_path,
        _candidate(_CANNOT_RUN_ROUTE, CANNOT_RUN),
        _candidate(_SOFT_ROUTE, SOFT),
    )

    with pytest.raises(review_orchestrator.ReviewRouteUnavailable) as caught:
        _select(tmp_path, excluded_adapters=frozenset({"claude_cli"}))

    assert str(caught.value) == "review_route_unavailable:correctness"
    assert isinstance(caught.value, RuntimeError)
    assert caught.value.skipped == [
        {**_CANNOT_RUN_ROUTE, "skip": f"ranker_excluded:{CANNOT_RUN}"},
        {**_SOFT_ROUTE, "skip": "excluded_adapter"},
    ]


def test_skip_rows_are_bounded_to_sixteen(monkeypatch, tmp_path: Path) -> None:
    routes = [
        {"runner": f"r{index}", "adapter_id": f"a{index}", "model": f"m{index}"}
        for index in range(20)
    ]
    _stub(monkeypatch, tmp_path, *(_candidate(route, CANNOT_RUN) for route in routes))

    with pytest.raises(review_orchestrator.ReviewRouteUnavailable) as caught:
        _select(tmp_path)

    assert len(caught.value.skipped) == 16


def test_hard_filters_still_win_in_the_fallback_pass(
    monkeypatch, tmp_path: Path,
) -> None:
    codex = {"runner": "codex", "adapter_id": "codex_cli", "model": "gpt-5.6"}
    disabled = {"runner": "off", "adapter_id": "off_adapter", "model": "off-model"}
    _stub(
        monkeypatch, tmp_path,
        _candidate(_SOFT_ROUTE, SOFT),
        _candidate(codex, SOFT),
        _candidate(disabled, SOFT),
    )
    monkeypatch.setattr(
        review_orchestrator,
        "_review_route_policy_enabled",
        lambda _policy, route: route["adapter_id"] != "off_adapter",
    )
    with pytest.raises(review_orchestrator.ReviewRouteUnavailable) as caught:
        _select(
            tmp_path,
            excluded_routes=frozenset({("claude_cli", "claude_cli", "opus")}),
        )
    assert [row["skip"] for row in caught.value.skipped] == [
        "excluded_route", "manager_reserved", "policy_disabled",
    ]

    _stub(monkeypatch, tmp_path, _candidate(_SOFT_ROUTE, SOFT), circuit="open")
    with pytest.raises(review_orchestrator.ReviewRouteUnavailable) as caught:
        _select(tmp_path)
    assert [row["skip"] for row in caught.value.skipped] == ["circuit_open"]


def test_ranked_survivor_still_wins_over_any_fallback(
    monkeypatch, tmp_path: Path,
) -> None:
    ranked = {**_CANNOT_RUN_ROUTE, "execution_runner": "copilot", "excluded": False}
    _stub(monkeypatch, tmp_path, _candidate(_SOFT_ROUTE, SOFT), ranked)

    route = _select(tmp_path)

    assert route == _CANNOT_RUN_ROUTE
    assert "route_fallback" not in route


class _Manager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.launches: list[dict] = []
        self.events: list[dict] = []

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(kwargs)
        return {
            "ok": True,
            "request_id": "review-request-" + kwargs["lens"],
            "task_id": kwargs["reviewer_task_id"],
            "state": "starting",
        }

    def status(self, request_id):
        card = {
            "task_id": "TARGET", "request_id": "target-request", "claim_epoch": "1",
            "packet_sha256": "a" * 64, "candidate_sha256": "b" * 64,
            "workspace_identity": "workspace-candidate-a",
            "evidence": {"source_graph_partition_readiness": {"target": True}},
        }
        if request_id == "target-request":
            return {"ok": True, "state": "review_ready", "task_card": card}
        return {"request_id": request_id, "ok": True, "state": "starting"}

    def _append_event(self, event):
        self.events.append(event)


def _driver(tmp_path: Path):
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite",
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return manager, driver


def test_orchestrator_records_route_fallback_for_a_fallback_selected_attempt(
    monkeypatch, tmp_path: Path,
) -> None:
    _stub(monkeypatch, tmp_path, _candidate(_SOFT_ROUTE, SOFT))
    manager, driver = _driver(tmp_path)

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert manager.launches[0]["adapter_id"] == "claude_cli"
    with sqlite3.connect(tmp_path / "review.sqlite") as conn:
        rows = conn.execute(
            "SELECT lens,attempt_index,fallback_json "
            "FROM review_orchestrator_route_fallbacks"
        ).fetchall()
    assert [(row[0], row[1], json.loads(row[2])) for row in rows] == [
        ("correctness", 1, {
            "reason": "ranked_routes_exhausted", "ranker_exclusion": [SOFT],
        }),
    ]


def test_parked_lens_wait_event_carries_route_skips(
    monkeypatch, tmp_path: Path,
) -> None:
    _stub(monkeypatch, tmp_path, _candidate(_CANNOT_RUN_ROUTE, CANNOT_RUN))
    manager, driver = _driver(tmp_path)

    result = driver.drain(max_actions=1, now=NOW)

    assert result.pending == 1
    assert manager.launches == []
    waits = [
        event for event in manager.events
        if event["event_type"] == "review_orchestrator_wait"
    ]
    assert waits[0]["review_automation"]["reason"] == "review_route_unavailable:correctness"
    assert waits[0]["review_automation"]["route_skips"] == [
        {**_CANNOT_RUN_ROUTE, "skip": f"ranker_excluded:{CANNOT_RUN}"},
    ]
