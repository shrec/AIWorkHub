"""Tests for NF-2026-01412: mechanically complete mined skill drafts, and the
``aiworkhub_manager_learning_commit`` auto-mining/auto-propose hook.

``skill_miner``'s draft used to leave every closed-vocabulary judgement
dimension empty (task_family, triggers, applicability, confidence) and fixed
``stage`` to ``"rework"``, so no mined candidate was ever proposable without a
human retyping it by hand -- and in this repository's real ledger, none ever
was. This module proves two things changed, and nothing else did:

* given at least two of a cluster's member cards are readable through the
  exact derivation runtime selection uses, the draft is COMPLETE (every
  dimension mechanically derived, ``draft_incomplete`` empty) and passes
  ``manager_skill_tools.propose`` unmodified, landing PROPOSED;
* ``manager_ai_tools.learning_commit`` runs that mining+proposing pass,
  bounded and advisory, after every ACCEPTED commit, and a cluster that
  gains members (or a commit replayed idempotently) is reported
  ``already_proposed`` rather than minted as a second record.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from _taskdb_compat import upsert_card

import aiworkhub.core as core
import aiworkhub.skill_registry as sr
from aiworkhub import (
    evidence_levels,
    manager_ai_tools,
    manager_skill_tools as mst,
    skill_miner,
    skill_registry_store as store,
    task_store,
)

RULE = ("the candidate must enumerate every emitting branch before the field "
        "guarantee is treated as delivered to a reader")


def _db(root):
    return root / ".aiworkhub" / "tasking" / "task_queue.sqlite"


def _connect(root):
    task_store.initialize_repository(root)
    conn = sqlite3.connect(str(_db(root)))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_commits(
            commit_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            task_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            repo_area TEXT NOT NULL,
            outcome TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            projections_json TEXT NOT NULL,
            state TEXT NOT NULL,
            manager_id TEXT NOT NULL,
            manager_provider TEXT NOT NULL,
            provenance TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, request_id)
        );
        """
    )
    return conn


def seed_card(root, task_id, *, runner="claude", paths=(), instruction="", read_only=False):
    """Write one card carrying a real rejection AND a complete, hand-declared
    selection vocabulary -- so ``project_context._skill_selection_context``
    resolves deterministically instead of depending on risk-signal heuristics
    this module is not testing.
    """
    card = {
        "task_id": task_id,
        "runner": runner,
        "allowed_writes": list(paths),
        "read_only": read_only,
        "skill_task_family": "bugfix",
        "skill_stage": "implementation",
        "skill_triggers": ["code_change"],
        "skill_applicability": ["quality_gate"],
        "risk_tier": "low",
    }
    if instruction:
        card["review_feedback"] = {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": instruction,
            "predecessor_request_id": f"req-{task_id}",
        }
    conn = _connect(root)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tasks (task_id,runner,topic,mode,status,worker_status,"
            "priority,objective,card_json,created_at,updated_at,claimed_by,claimed_at,"
            "started_at,completed_at,origin_thread_id,archived_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, runner, "code", "", "done", "done", "normal", "objective",
                json.dumps(card), "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:10:00+00:00", runner, "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:01:00+00:00", "2026-01-01T00:10:00+00:00", "t", "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def seed_commit(root, task_id, request_id, *, invariant="", outcome="rejected",
                 area="src/aiworkhub", failure_category="candidate_code"):
    """Write one ledger-only correction statement, with no backing task card."""
    payload = {
        "invariant_candidate": invariant,
        "lesson_candidate": "",
        "failure_category": failure_category,
    }
    conn = _connect(root)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO learning_commits(commit_id,idempotency_key,task_id,"
            "request_id,repository_id,repo_area,outcome,payload_json,payload_sha256,"
            "projections_json,state,manager_id,manager_provider,provenance,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{task_id}:{request_id}", f"{task_id}:{request_id}", task_id,
                request_id, "repo", area, outcome, json.dumps(payload), "sha",
                "{}", "completed", "m", "claude", "test",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_accepted_card(root, *, task_id, request_id):
    """A finished, manager-accepted card -- the shape ``learning_commit``
    requires for an ``outcome="accepted"`` commit to be durable.
    """
    record = evidence_levels.EvidenceRecord(
        evidence_level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
        severity="NONE", confidence="HIGH",
        reference=f"file:test-fixture/{request_id}",
        verified_by="codex", message="fixture manager acceptance",
    ).to_dict()
    card = {
        "task_id": task_id, "runner": "worker", "topic": "learning", "mode": "edit",
        "status": "finished", "worker_status": "done",
        "accepted_request_id": request_id,
        "accept_evidence": {"acceptance_evidence_record": record},
    }
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, card)
    finally:
        con.close()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    route = {
        "role": "manager", "provider": "claude", "repo": str(tmp_path),
        "manager_route": {"thread_id": "sess-1", "provider": "claude"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


def _seed_cluster(manager, *, count=3):
    """Three (or more) cards whose review feedback is one rule family, each
    on its own file under a shared directory and with a complete selection
    vocabulary already declared.
    """
    for index in range(count):
        seed_card(
            manager, f"auto{index}", runner=f"worker{index % 2}",
            paths=[f"src/aiworkhub/shared_{index}.py"],
            instruction=f"{RULE} seen in round {index}",
        )


def _accept(manager, *, task_id, request_id, idempotency_key):
    _seed_accepted_card(manager, task_id=task_id, request_id=request_id)
    return manager_ai_tools.learning_commit(
        task_id=task_id, request_id=request_id, repo_area="src/aiworkhub",
        outcome="accepted", evidence_ids=["file:tests/test_skill_auto_mining.py"],
        idempotency_key=idempotency_key, provenance="auto-mining regression test",
    )


# ---------------------------------------------------------------------------
# Mechanical draft completion
# ---------------------------------------------------------------------------


def test_a_complete_candidate_passes_propose_and_lands_proposed(manager):
    _seed_cluster(manager, count=3)

    report = skill_miner.mine(manager)
    assert len(report["candidates"]) == 1
    candidate = report["candidates"][0]
    draft = candidate["proposal_draft"]

    assert candidate["draft_incomplete"] == []
    assert draft["task_family"] == "bugfix"
    assert draft["stage"] in {"orientation", "implementation"}
    assert draft["triggers"] == ["code_change"]
    assert draft["applicability"] == ["quality_gate"]
    assert draft["path_or_symbol"]
    assert 0.0 < draft["confidence"] <= 1.0
    assert draft["procedure_steps"]

    result = mst.propose(**skill_miner.candidate_proposal_payload(candidate))
    assert result["ok"] is True, result

    record = store.load_registry(manager).get(draft["identity"], draft["version"])
    assert record is not None
    assert record.lifecycle_state is sr.LifecycleState.PROPOSED


def test_cluster_without_stored_member_cards_stays_incomplete(manager, monkeypatch):
    _seed_cluster(manager)
    monkeypatch.setattr(task_store, "get_task", lambda *_args, **_kwargs: None)

    report = skill_miner.mine(manager)
    assert len(report["candidates"]) == 1
    candidate = report["candidates"][0]

    assert candidate["draft_incomplete"] == ["member_cards_not_in_store"]
    assert candidate["proposal_draft"]["task_family"] == ""

    propose_result = mst.propose(**skill_miner.candidate_proposal_payload(candidate))
    assert propose_result["ok"] is False


# ---------------------------------------------------------------------------
# Deterministic helpers, pinned directly
# ---------------------------------------------------------------------------


def test_modal_tie_breaks_to_lexicographically_smallest():
    assert skill_miner._modal(["b", "a"]) == "a"
    assert skill_miner._modal(["a", "a", "b"]) == "a"
    assert skill_miner._modal([]) == ""


def test_majority_tokens_requires_a_strict_majority():
    assert skill_miner._majority_tokens([["x"], ["x"], ["y"]]) == ("x",)
    assert skill_miner._majority_tokens([["x"], ["y"]]) == ()
    assert skill_miner._majority_tokens([]) == ()


def test_confidence_is_deterministic_and_bounded():
    low = skill_miner._confidence(3, 1)
    same = skill_miner._confidence(3, 1)
    high = skill_miner._confidence(6, 3)
    assert low == same
    assert 0.0 < low <= high <= 1.0


# ---------------------------------------------------------------------------
# The learning_commit auto-mining/auto-propose hook
# ---------------------------------------------------------------------------


def test_hook_proposes_new_complete_candidates_after_accepted_commit(manager):
    _seed_cluster(manager, count=3)

    result = _accept(manager, task_id="TRIGGER-1", request_id="req-trigger-1",
                      idempotency_key="trigger-key-1")

    assert result["ok"] is True
    mining = result["skill_mining"]
    assert set(mining) == {
        "candidates", "proposed", "already_proposed", "refused_by_reason", "elapsed_ms",
    }
    assert mining["candidates"] == 1
    assert len(mining["proposed"]) == 1
    identity = mining["proposed"][0]

    record = store.load_registry(manager).get(identity, "0.1.0")
    assert record is not None
    assert record.lifecycle_state is sr.LifecycleState.PROPOSED


def test_remining_after_a_new_member_reports_already_proposed(manager):
    _seed_cluster(manager, count=3)
    first = _accept(manager, task_id="TRIGGER-A", request_id="req-a",
                     idempotency_key="trigger-key-a")
    assert len(first["skill_mining"]["proposed"]) == 1
    identity = first["skill_mining"]["proposed"][0]

    # A 4th member joins the same rule family after the first proposal.
    seed_card(
        manager, "auto3", runner="worker1", paths=["src/aiworkhub/shared_3.py"],
        instruction=f"{RULE} seen in round 3",
    )
    second_report = skill_miner.mine(manager)
    assert len(second_report["candidates"]) == 1
    assert second_report["candidates"][0]["candidate_id"] == identity

    second = _accept(manager, task_id="TRIGGER-B", request_id="req-b",
                      idempotency_key="trigger-key-b")
    assert second["skill_mining"]["proposed"] == []
    assert identity in second["skill_mining"]["already_proposed"]

    matching = [r for r in store.load_registry(manager).records() if r.identity == identity]
    assert len(matching) == 1


def test_hook_run_twice_for_the_same_commit_proposes_nothing_new(manager):
    _seed_cluster(manager, count=3)

    def _run():
        return _accept(manager, task_id="TRIGGER-C", request_id="req-c",
                        idempotency_key="trigger-key-c")

    first = _run()
    assert len(first["skill_mining"]["proposed"]) == 1
    identity = first["skill_mining"]["proposed"][0]

    second = _run()
    assert second["idempotent"] is True
    assert second["skill_mining"]["proposed"] == []
    assert identity in second["skill_mining"]["already_proposed"]
