"""End-to-end tests for NF-2026-01411.

Covers the parts of the learning-commit evidence flow that a single-stage,
single-actor unit test cannot show: a reworked card's evidence reaches both
the rework stage and the first-launch stage it superseded, a rejected commit
stays non-evidence even on such a card, and a matched PROPOSED record gets a
gated auto-activation attempt through the registry's own two-actor gate.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import aiworkhub.core as core
import aiworkhub.skill_registry as sr
from aiworkhub import learning_commit_store
from aiworkhub import manager_skill_tools as mst
from aiworkhub import skill_registry_store as store
from aiworkhub import task_store

_PATH = "src/aiworkhub/foo.py"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """Install a verified manager identity rooted at an isolated repo."""
    route = {
        "role": "manager",
        "provider": "claude",
        "repo": str(tmp_path),
        "manager_route": {"thread_id": "sess-flow-1", "provider": "claude"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


def _card_json(task_id, runner, *, claim_epoch=0, read_only=False):
    card = {
        "task_id": task_id,
        "runner": runner,
        "allowed_writes": [_PATH],
        "skill_path_scope": _PATH,
        "skill_task_family": "bugfix",
        "skill_triggers": ["code_change"],
        "skill_applicability": ["quality_gate"],
        "risk_tier": "medium",
    }
    if claim_epoch:
        card["claim_epoch"] = claim_epoch
    if read_only:
        card["read_only"] = True
    return card


def _seed_card(root, task_id, *, runner, claim_epoch=0, read_only=False):
    task_store.initialize_repository(root)
    db = root / ".aiworkhub" / "tasking" / "task_queue.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by,claimed_at,started_at,"
            "completed_at,origin_thread_id,archived_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, runner, "code", "", "done", "done", "normal", "objective",
                json.dumps(_card_json(task_id, runner, claim_epoch=claim_epoch, read_only=read_only)),
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00",
                runner, "2026-01-01T00:01:00+00:00", "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:10:00+00:00", "thread-1", "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_learning_commit(root, task_id, request_id, *, outcome):
    task_store.initialize_repository(root)
    db = root / ".aiworkhub" / "tasking" / "task_queue.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(learning_commit_store._SCHEMA)
        if outcome == "accepted":
            from _taskdb_compat import upsert_card
            from aiworkhub import evidence_levels

            card = task_store.get_task(root, task_id)
            if card is not None:
                card.update({
                    "status": "finished", "worker_status": "done",
                    "accepted_request_id": request_id,
                    "accept_evidence": {
                        "acceptance_evidence_record": evidence_levels.EvidenceRecord(
                            evidence_level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
                            severity="NONE", confidence="HIGH",
                            reference=f"file:test-acceptance/{request_id}",
                            verified_by="codex", message="fixture manager acceptance",
                        ).to_dict(),
                    },
                })
                conn.row_factory = sqlite3.Row
                upsert_card(conn, card)
        payload = {"failure_category": None, "edge_candidates": []}
        projections = {"ai_memory": {"state": "not_requested"}}
        conn.execute(
            "INSERT OR REPLACE INTO learning_commits(commit_id,idempotency_key,task_id,"
            "request_id,repository_id,repo_area,outcome,payload_json,payload_sha256,"
            "projections_json,state,manager_id,manager_provider,provenance,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{task_id}:{request_id}", f"{task_id}:{request_id}", task_id, request_id,
                "repo", "src/aiworkhub", outcome, json.dumps(payload), "sha",
                json.dumps(projections), "completed", "m", "claude", "test",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _propose(identity, stage):
    return mst.propose(
        identity=identity,
        version="1.0.0",
        scope="repository",
        task_family="bugfix",
        path_or_symbol=_PATH,
        risk="medium",
        stage=stage,
        triggers=["code_change"],
        confidence=0.9,
        applicability=["quality_gate"],
    )


def test_reworked_card_matches_both_first_launch_and_rework_stage_records(manager):
    assert _propose("skill-implementation-stage", "implementation")["ok"] is True
    assert _propose("skill-rework-stage", "rework")["ok"] is True
    _seed_card(manager, "T_REWORK1", runner="claude_sonnet-5", claim_epoch=2)
    _seed_learning_commit(manager, "T_REWORK1", "req-rw1", outcome="accepted")

    result = mst.add_learning_commit_evidence(task_id="T_REWORK1", request_id="req-rw1")

    assert result["ok"] is True
    assert result["traversed_stages"] == ["implementation", "rework"]
    identities = {row["identity"] for row in result["recorded"]}
    assert identities == {"skill-implementation-stage", "skill-rework-stage"}
    assert all(row["idempotent"] is False for row in result["recorded"])

    impl_record = store.load_registry(manager).get("skill-implementation-stage", "1.0.0")
    rework_record = store.load_registry(manager).get("skill-rework-stage", "1.0.0")
    assert len(impl_record.evidence) == 1
    assert len(rework_record.evidence) == 1
    assert impl_record.evidence[-1].outcome is sr.EvidenceOutcome.ACCEPTED
    assert rework_record.evidence[-1].outcome is sr.EvidenceOutcome.ACCEPTED


def test_non_reworked_card_yields_single_traversed_stage(manager):
    assert _propose("skill-plain-implementation", "implementation")["ok"] is True
    _seed_card(manager, "T_PLAIN1", runner="claude_sonnet-5")
    _seed_learning_commit(manager, "T_PLAIN1", "req-p1", outcome="accepted")

    result = mst.add_learning_commit_evidence(task_id="T_PLAIN1", request_id="req-p1")

    assert result["ok"] is True
    assert result["traversed_stages"] == ["implementation"]
    assert [row["identity"] for row in result["recorded"]] == ["skill-plain-implementation"]


def test_reworked_read_only_card_traverses_orientation_first(manager):
    assert _propose("skill-orientation-stage", "orientation")["ok"] is True
    assert _propose("skill-implementation-only", "implementation")["ok"] is True
    _seed_card(manager, "T_REWORK_RO", runner="claude_sonnet-5", claim_epoch=2, read_only=True)
    _seed_learning_commit(manager, "T_REWORK_RO", "req-ro1", outcome="accepted")

    result = mst.add_learning_commit_evidence(task_id="T_REWORK_RO", request_id="req-ro1")

    assert result["ok"] is True
    assert result["traversed_stages"] == ["orientation", "rework"]
    assert [row["identity"] for row in result["recorded"]] == ["skill-orientation-stage"]


def test_rejected_commit_on_a_reworked_card_records_nothing(manager):
    assert _propose("skill-rework-rejected-check", "rework")["ok"] is True
    _seed_card(manager, "T_REWORK_REJ", runner="claude_sonnet-5", claim_epoch=2)
    _seed_learning_commit(manager, "T_REWORK_REJ", "req-rj1", outcome="rejected")

    result = mst.add_learning_commit_evidence(task_id="T_REWORK_REJ", request_id="req-rj1")

    assert result["ok"] is True
    assert result["reason"] == "rejected_commit_is_not_applicability_evidence"
    assert result["recorded"] == []
    record = store.load_registry(manager).get("skill-rework-rejected-check", "1.0.0")
    assert record.evidence == ()


def test_two_distinct_actor_commits_activate_a_proposed_record(manager):
    assert _propose("skill-two-actor-activation", "implementation")["ok"] is True
    _seed_card(manager, "T_ACT1", runner="claude_sonnet-5")
    _seed_learning_commit(manager, "T_ACT1", "req-act1", outcome="accepted")

    first = mst.add_learning_commit_evidence(task_id="T_ACT1", request_id="req-act1")
    assert first["ok"] is True
    assert first["unlinked"] == []
    assert first["activation"] == [
        {
            "identity": "skill-two-actor-activation", "version": "1.0.0",
            "activated": False, "reason": "activation_evidence_below_two_distinct_actors",
        }
    ]
    record = store.load_registry(manager).get("skill-two-actor-activation", "1.0.0")
    assert record.lifecycle_state is sr.LifecycleState.PROPOSED

    _seed_card(manager, "T_ACT2", runner="codex_gpt-5.5")
    _seed_learning_commit(manager, "T_ACT2", "req-act2", outcome="accepted")

    second = mst.add_learning_commit_evidence(task_id="T_ACT2", request_id="req-act2")
    assert second["ok"] is True
    assert second["unlinked"] == []
    assert second["activation"] == [
        {
            "identity": "skill-two-actor-activation", "version": "1.0.0",
            "activated": True, "reason": "activated",
        }
    ]
    record = store.load_registry(manager).get("skill-two-actor-activation", "1.0.0")
    assert record.lifecycle_state is sr.LifecycleState.ACTIVE


def test_unresolved_negative_evidence_blocks_auto_activation_even_with_two_actors(manager):
    assert _propose("skill-blocked-by-negative-evidence", "implementation")["ok"] is True
    # A real negative entry from a genuinely distinct actor. NEGATIVE evidence
    # never comes from add_learning_commit_evidence any more (NF-2026-01411);
    # it is recorded here through the unchanged add_evidence path instead.
    assert mst.add_evidence(
        identity="skill-blocked-by-negative-evidence", version="1.0.0",
        source="src-neg", outcome="negative", actor_id="actor-neg",
    )["ok"] is True

    _seed_card(manager, "T_NEG1", runner="claude_sonnet-5")
    _seed_learning_commit(manager, "T_NEG1", "req-neg1", outcome="accepted")
    mst.add_learning_commit_evidence(task_id="T_NEG1", request_id="req-neg1")

    _seed_card(manager, "T_NEG2", runner="codex_gpt-5.5")
    _seed_learning_commit(manager, "T_NEG2", "req-neg2", outcome="accepted")
    result = mst.add_learning_commit_evidence(task_id="T_NEG2", request_id="req-neg2")

    assert result["ok"] is True
    assert result["unlinked"] == []
    assert result["activation"] == [
        {
            "identity": "skill-blocked-by-negative-evidence", "version": "1.0.0",
            "activated": False, "reason": "unresolved_negative_evidence",
        }
    ]
    record = store.load_registry(manager).get("skill-blocked-by-negative-evidence", "1.0.0")
    assert record.lifecycle_state is sr.LifecycleState.PROPOSED
