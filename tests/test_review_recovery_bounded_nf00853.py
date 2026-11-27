"""NF-00853: review recovery counts 'ensured' only with a reviewer identity,
and one pass is bounded by a wall-clock budget with round-robin resumption."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_orchestrator as ro  # noqa: E402


class _Manager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def status(self, request_id: str) -> dict:
        return {"task_card": {"request_id": request_id}}


def _action(action_type: str, lens: str, action_id: int) -> SimpleNamespace:
    return SimpleNamespace(action_type=action_type, lens=lens, action_id=action_id)


def _seed(monkeypatch, request_ids, chain_actions, clock_now, holds, registered):
    """Stub the store and identity seams so one real recovery pass runs.

    ``chain_actions(request_id)`` gives the chain ensure_chain would leave
    behind. Every register step advances ``clock_now`` by one second.
    """
    cards = [{"request_id": rid, "task_id": f"task-{rid}"} for rid in request_ids]
    monkeypatch.setattr(ro.task_store, "list_review_queue_cards", lambda repo, limit: cards)
    monkeypatch.setattr(ro.task_store, "manager_ready_marker", lambda card: None)
    monkeypatch.setattr(ro, "_review_substatus", lambda card: "review_ready")
    monkeypatch.setattr(ro, "_card_request_id", lambda card: card["request_id"])
    monkeypatch.setattr(ro, "_status_review_ready", lambda status: True)
    monkeypatch.setattr(
        ro, "resolve_target_identity",
        lambda status: {
            "identity": {
                "target_task_id": f"task-{status['task_card']['request_id']}",
                "target_request_id": status["task_card"]["request_id"],
                "claim_epoch": "e1",
                "packet_sha256": "p",
                "candidate_sha256": "c",
            },
            "identity_source": "terminal_review_evidence",
            "conflict": "",
        },
    )
    monkeypatch.setattr(ro, "_sealed_hashes", lambda status_card: {"f": "h"})
    monkeypatch.setattr(ro, "_quality_gate", lambda status_card: {})
    monkeypatch.setattr(
        ro, "candidate_registration",
        lambda *, metadata, artifact_receipt, changed_path_hashes, quality_gate: {
            "request_id": metadata["request_id"],
            "target_task_id": metadata["task_id"],
            "target_request_id": metadata["request_id"],
            "claim_epoch": "e1",
            "packet_sha256": "p",
            "candidate_sha256": "c",
            "required_reviewer_lenses": ["correctness"],
        },
    )
    monkeypatch.setattr(
        ro, "_reviewer_lens_coverage",
        lambda repo, identity, planned, **kwargs: {lens: "absent" for lens in planned},
    )
    monkeypatch.setattr(ro, "_manager_lens_lookup", lambda manager, identity: None)
    monkeypatch.setattr(
        ro, "_carried_lens_lookup", lambda manager, card, task_id, request_id: None,
    )

    def _register(manager, *, db_path, registration):
        rid = registration["request_id"]
        registered.append(rid)
        clock_now[0] += 1.0
        return SimpleNamespace(chain_id=1, actions=tuple(chain_actions(rid)))

    monkeypatch.setattr(ro, "register_candidate", _register)
    monkeypatch.setattr(
        ro, "_stamp_recovery_skip",
        lambda manager, identity, missing, reason: holds.append(
            (identity["target_request_id"], reason),
        ),
    )
    ro._RECOVERY_STATE.clear()


def test_parent_without_reviewer_identity_is_not_ensured(monkeypatch, tmp_path):
    holds: list = []
    registered: list = []
    clock = [0.0]
    # Zero reviewer children: the chain holds only a non-launch action.
    _seed(
        monkeypatch, ["r1"],
        lambda rid: (_action("accept", "correctness", 5),),
        clock, holds, registered,
    )
    result = ro.recover_review_ready_targets(
        _Manager(tmp_path), db_path=tmp_path / "review.sqlite", budget_s=100.0,
        clock=lambda: clock[0],
    )
    assert registered == ["r1"]
    assert result["review_recovery_ensured"] == 0
    assert result["review_recovery_unactionable"] == 1
    assert result["review_recovery_reasons"].get("ensured", 0) == 0
    assert result["review_recovery_reasons"]["no_reviewer_identity"] == 1
    assert holds == [("r1", "no_reviewer_identity")]


def test_parent_with_durable_launch_identity_is_ensured(monkeypatch, tmp_path):
    holds: list = []
    registered: list = []
    clock = [0.0]
    _seed(
        monkeypatch, ["r1"],
        lambda rid: (_action("launch", "correctness", 9),),
        clock, holds, registered,
    )
    result = ro.recover_review_ready_targets(
        _Manager(tmp_path), db_path=tmp_path / "review.sqlite", budget_s=100.0,
        clock=lambda: clock[0],
    )
    assert result["review_recovery_ensured"] == 1
    assert result["review_recovery_unactionable"] == 0
    assert holds == []


def test_pass_budget_defers_remainder_and_resumes_round_robin(monkeypatch, tmp_path):
    holds: list = []
    registered: list = []
    clock = [0.0]
    ids = ["r1", "r2", "r3", "r4", "r5"]
    _seed(
        monkeypatch, ids,
        lambda rid: (_action("launch", "correctness", 1),),
        clock, holds, registered,
    )
    repo = _Manager(tmp_path)
    passes = []
    for _ in range(3):
        passes.append(ro.recover_review_ready_targets(
            repo, db_path=tmp_path / "review.sqlite", budget_s=2.0,
            clock=lambda: clock[0],
        ))
    # Each pass stops once its 2 s budget is spent: two parents per pass.
    assert [p["review_recovery_ensured"] for p in passes] == [2, 2, 2]
    assert [p["review_recovery_deferred"] for p in passes] == [3, 3, 3]
    # Resumes after the last examined parent; no parent is starved.
    assert registered == ["r1", "r2", "r3", "r4", "r5", "r1"]
    assert passes[0]["review_recovery_next_cursor"] == "r2"
    assert passes[1]["review_recovery_next_cursor"] == "r4"
    assert passes[2]["review_recovery_next_cursor"] == "r1"


def test_default_budget_is_positive_and_bounded():
    assert 0.0 < ro.RECOVERY_TICK_BUDGET_S < 30.0


def test_cursor_is_keyed_per_repo_and_never_shared(monkeypatch, tmp_path):
    holds: list = []
    registered: list = []
    clock = [0.0]
    _seed(
        monkeypatch, ["r1", "r2", "r3"],
        lambda rid: (_action("launch", "correctness", 1),),
        clock, holds, registered,
    )
    repo_a = _Manager(tmp_path / "a")
    repo_b = _Manager(tmp_path / "b")
    ro.recover_review_ready_targets(
        repo_a, db_path=tmp_path / "review.sqlite", budget_s=2.0,
        clock=lambda: clock[0],
    )
    # Repo A stopped after r2; repo B has its own cursor and starts at the head.
    result_b = ro.recover_review_ready_targets(
        repo_b, db_path=tmp_path / "review.sqlite", budget_s=2.0,
        clock=lambda: clock[0],
    )
    assert registered == ["r1", "r2", "r1", "r2"]
    assert result_b["review_recovery_next_cursor"] == "r2"


def test_cursor_survives_removed_card_and_resumes_at_successor(monkeypatch, tmp_path):
    holds: list = []
    registered: list = []
    clock = [0.0]
    _seed(
        monkeypatch, ["A", "B", "C", "D"],
        lambda rid: (_action("launch", "correctness", 1),),
        clock, holds, registered,
    )
    repo = _Manager(tmp_path)
    # Pass 1: the 2 s budget is spent after A and B (each register costs 1 s).
    first = ro.recover_review_ready_targets(
        repo, db_path=tmp_path / "review.sqlite", budget_s=2.0,
        clock=lambda: clock[0],
    )
    assert registered == ["A", "B"]
    assert first["review_recovery_next_cursor"] == "B"
    # B is accepted and leaves the queue between ticks; the cursor must still
    # resume at its successor C, not restart at the head A.
    remaining = [
        {"request_id": rid, "task_id": f"task-{rid}"} for rid in ["A", "C", "D"]
    ]
    monkeypatch.setattr(
        ro.task_store, "list_review_queue_cards", lambda repo, limit: remaining,
    )
    ro.recover_review_ready_targets(
        repo, db_path=tmp_path / "review.sqlite", budget_s=2.0,
        clock=lambda: clock[0],
    )
    assert registered[2:4] == ["C", "D"]
