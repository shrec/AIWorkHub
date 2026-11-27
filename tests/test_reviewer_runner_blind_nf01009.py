"""NF-2026-01009: auto reviewers on an unlaunchable runner and on blind shards.

(a) Reviewer route selection folds every candidate runner through the one
    canonical runner fold onto this host's workforce rows, and refuses at
    selection -- typed -- a claude-family runner that has no row here.
(b) No automatic reviewer is seeded or launched for a blind / annotation card.

Fakes only: no provider CLI, no live manager loop.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import process_launcher, runner_topic_policy
from aiworkhub import review_orchestrator as ro

_ROW = {"model": "claude-opus-5", "enabled": True, "available": True,
        "risk_tiers": frozenset({"high"})}


def _stub_selection(monkeypatch, tmp_path: Path, *routes: dict) -> None:
    ro.reset_routing_catalog_cache()
    monkeypatch.setattr(
        ro.task_store, "storage_readiness",
        lambda _repo: SimpleNamespace(
            ready=True, reason="ready", repo_id="repo-test",
            canonical_db=str(tmp_path / "queue.sqlite"),
        ),
    )
    candidates = [
        {**route, "execution_runner": route["runner"], "excluded": False}
        for route in routes
    ]
    monkeypatch.setattr(
        ro.workforce_catalog, "rank_task",
        lambda *_a, **_k: {"launch_contract": None, "candidates": candidates},
    )
    monkeypatch.setattr(ro.workforce_catalog, "build_routing_catalog", lambda _r: {"routes": []})
    monkeypatch.setattr(ro, "_review_route_policy_enabled", lambda _p, _r: True)
    monkeypatch.setattr(
        ro.workforce_catalog, "route_circuit_for",
        lambda _repo, _adapter, _model: {"state": "closed"},
    )


def _select(tmp_path: Path):
    return ro.select_reviewer_route(tmp_path, "QUALITY_REVIEW_NF01009", "correctness")


# --- (a) runner fold / refusal at selection ---------------------------------


def test_fold_uses_the_canonical_runner_fold_owner() -> None:
    owners = {owner for owner, _ in process_launcher._CANONICAL_WORKFORCE}
    for variant in ("claude_opus-5", "claude_opus_5", "claude_opus5", "Claude_Opus-5"):
        expected = runner_topic_policy.canonical_runner_id(variant, owners)
        assert expected == "claude_opus-5"
        assert ro.fold_reviewer_runner(variant) == (expected, "")


def test_catalog_hyphen_runner_folds_onto_the_host_underscore_route(
    monkeypatch, tmp_path: Path,
) -> None:
    # The host's workforce spells the runner claude_opus_5; the catalog says
    # claude_opus-5. Selection returns the ONE spelling the launcher owns.
    monkeypatch.setattr(
        process_launcher, "_CANONICAL_WORKFORCE",
        {("claude_opus_5", "claude_cli"): dict(_ROW)},
    )
    _stub_selection(
        monkeypatch, tmp_path,
        {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "claude-opus-5"},
    )

    route = _select(tmp_path)

    assert route["runner"] == "claude_opus_5"
    assert (route["runner"], route["adapter_id"]) in process_launcher._CANONICAL_WORKFORCE


def test_runner_without_host_route_is_refused_at_selection_typed(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        process_launcher, "_CANONICAL_WORKFORCE",
        {("claude_sonnet-5", "claude_cli"): {**_ROW, "model": "claude-sonnet-5"}},
    )
    absent = {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "claude-opus-5"}
    _stub_selection(monkeypatch, tmp_path, absent)

    with pytest.raises(ro.ReviewRouteUnavailable) as raised:
        _select(tmp_path)

    assert raised.value.skipped == [
        {**absent, "skip": "workforce_route_absent:runner=claude_opus-5"}
    ]


def test_unlaunchable_runner_is_skipped_and_the_next_launchable_route_wins(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        process_launcher, "_CANONICAL_WORKFORCE",
        {("claude_sonnet-5", "claude_cli"): {**_ROW, "model": "claude-sonnet-5"}},
    )
    sonnet = {"runner": "claude_sonnet_5", "adapter_id": "claude_cli", "model": "claude-sonnet-5"}
    _stub_selection(
        monkeypatch, tmp_path,
        {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "claude-opus-5"},
        sonnet,
    )

    route = _select(tmp_path)

    assert route == {**sonnet, "runner": "claude_sonnet-5"}
    assert route["runner"] != "claude_opus-5"


def test_non_claude_runner_families_keep_launcher_handling() -> None:
    assert ro.fold_reviewer_runner("copilot") == ("copilot", "")
    assert ro.fold_reviewer_runner("claude_cli") == ("claude_cli", "")


# --- (b) blind / annotation cards get no automatic reviewer ------------------


@pytest.mark.parametrize("card", [
    {"work_kind": "blind"},
    {"work_kind": "annotation"},
    {"work_kind": "blind_annotation"},
    {"work_kinds": ["code", "annotation-shard"]},
    {"tags": ["linux", "Blind"]},
])
def test_blind_card_predicate_marks_blind_and_annotation(card) -> None:
    assert ro.reviewer_blind_card_reason(card) == "reviewer_skipped:blind_card"


@pytest.mark.parametrize("card", [
    {"work_kind": "bugfix"},
    {"tags": ["test_impact_blind_spots", "type_annotations"]},
    {},
    None,
])
def test_blind_card_predicate_leaves_code_cards_alone(card) -> None:
    assert ro.reviewer_blind_card_reason(card) == ""


IDENTITY = {
    "target_task_id": "T_BLIND",
    "target_request_id": "REQ_BLIND",
    "claim_epoch": "1",
    "packet_sha256": "a" * 64,
    "candidate_sha256": "b" * 64,
}


class _Manager:
    def __init__(self, repo: Path, card: dict) -> None:
        self.repo = repo
        self._card = card
        self.launches: list[dict[str, Any]] = []

    def status(self, request_id):
        assert request_id == IDENTITY["target_request_id"]
        return {"ok": True, "state": "review_ready", "task_card": self._card}

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(dict(kwargs))
        return {"ok": True, "request_id": "REQ_R", "state": "starting"}

    def existing_lens_reviewer(self, **_kwargs):
        return None

    def _append_event(self, _event):
        return None


def _blind_card() -> dict:
    return {
        "task_id": IDENTITY["target_task_id"],
        "topic": "code",
        "work_kind": "annotation",
        "tags": ["blind"],
        "status": "review",
        "worker_status": "review",
        "terminal_substatus": "review_ready",
        "terminal_review": {"evidence": {
            "request_identity": {"request_id": IDENTITY["target_request_id"]},
            "changed_path_hashes": {"shard/a.jsonl": "c" * 64},
        }},
    }


def test_blind_card_is_never_seeded_a_reviewer(monkeypatch, tmp_path: Path) -> None:
    card = _blind_card()
    manager = _Manager(tmp_path, card)
    registered: list[Any] = []
    monkeypatch.setattr(ro.task_store, "list_review_queue_cards", lambda *_a, **_k: [card])
    monkeypatch.setattr(ro.task_store, "manager_ready_marker", lambda _card: None)
    monkeypatch.setattr(ro.task_store, "get_task", lambda *_a, **_k: None)
    monkeypatch.setattr(ro, "resolve_target_identity", lambda _status: {
        "identity": dict(IDENTITY), "conflict": "",
        "identity_source": "terminal_review_evidence", "workspace_identity": "",
    })
    monkeypatch.setattr(ro, "candidate_registration", lambda **_kw: {
        "candidate_sha256": IDENTITY["candidate_sha256"],
        "required_reviewer_lenses": ["correctness"],
    })
    monkeypatch.setattr(
        ro, "register_candidate",
        lambda _manager, **kw: registered.append(kw) or None,
    )

    result = ro.recover_review_ready_targets(manager, db_path=tmp_path / "lifecycle.db")

    assert registered == [], "a reviewer chain was seeded for a blind card"
    assert result["review_recovery_reasons"] == {"reviewer_skipped:blind_card": 1}
    assert manager.launches == []


def test_blind_card_gets_no_automatic_reviewer_launch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ro, "resolve_target_identity", lambda _status: {
        "identity": dict(IDENTITY), "conflict": "",
        "identity_source": "terminal_review_evidence", "workspace_identity": "",
    })
    manager = _Manager(tmp_path, _blind_card())
    orchestrator = object.__new__(ro.ReviewOrchestrator)
    orchestrator.manager = manager
    action = SimpleNamespace(
        action_id=1, chain_id=1, lens="correctness", action_type="launch",
        descriptor={"chain_identity": dict(IDENTITY)},
    )
    attempt = {
        "attempt_index": 1, "state": "planned", "failure_reason": "",
        "reviewer_task_id": ro.ReviewOrchestrator._reviewer_task_id(IDENTITY, "correctness"),
        "reviewer_request_id": "",
        "runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "claude-opus-5",
    }

    with pytest.raises(ro._DeferredLaunch) as raised:
        orchestrator._launch_route_attempt(action, attempt)

    assert manager.launches == [], "a reviewer was launched on a blind card"
    assert raised.value.reason == "reviewer_skipped:blind_card"
