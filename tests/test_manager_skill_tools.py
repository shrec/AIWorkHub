"""Tests for the manager-bound skill registry lifecycle driver.

Covers the acceptance contract: the three manager operations drive the exact
``skill_registry`` lifecycle under a verified manager identity and persist only
through the store's public API; a proposal is stored and readable by a later
call; a duplicate proposal is refused without overwriting; one accepted evidence
entry does not activate while two from distinct actors do; and activation below
the threshold fails closed with the registry's own reason, leaving the stored
record unchanged.
"""

from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

import aiworkhub.core as core
import aiworkhub.skill_registry as sr
from aiworkhub import manager_skill_tools as mst
from aiworkhub import skill_registry_store as store
from aiworkhub import task_store

BASE = {
    "identity": "commit-msg-check",
    "version": "1.0.0",
    "scope": "repository",
    "task_family": "commit",
    "path_or_symbol": "src/aiworkhub/skill_registry.py",
    "risk": "medium",
    "stage": "post-edit",
    "triggers": ["commit"],
    "confidence": 0.9,
}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """Install a verified manager identity rooted at an isolated repo."""
    route = {
        "role": "manager",
        "provider": "claude",
        "repo": str(tmp_path),
        "manager_route": {"thread_id": "sess-123", "provider": "claude"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


def _propose(**overrides):
    data = dict(BASE)
    data.update(overrides)
    return mst.propose(**data)


def _accept(actor_id, **overrides):
    args = {
        "identity": BASE["identity"],
        "version": BASE["version"],
        "source": f"src-{actor_id}",
        "outcome": "accepted",
        "actor_id": actor_id,
    }
    args.update(overrides)
    return mst.add_evidence(**args)


def test_propose_stores_a_record_readable_by_a_second_call(manager):
    result = _propose()
    assert result["ok"] is True
    assert result["identity"] == "commit-msg-check"
    assert result["version"] == "1.0.0"
    assert result["lifecycle_state"] == "proposed"
    assert result["digest"]

    # A later, independent call reads the same persisted record back.
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored is not None
    assert stored.lifecycle_state is sr.LifecycleState.PROPOSED
    assert sr.skill_digest(stored) == result["digest"]
    # And a second lifecycle tool call loads it and advances it.
    assert _accept("actor-a")["ok"] is True


def test_propose_never_generates_lifecycle_or_evidence(manager):
    result = _propose()
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    # The proposal is evidence-free with zero counters: nothing is inferred.
    assert result["lifecycle_state"] == "proposed"
    assert stored.evidence == ()
    assert stored.accepted_count == 0
    assert stored.negative_count == 0


def test_duplicate_propose_is_refused_without_overwriting(manager):
    assert _propose(confidence=0.9)["ok"] is True
    # A second proposal on the same identity/version is refused with the
    # registry's own immutability reason.
    duplicate = _propose(confidence=0.5)
    assert duplicate["ok"] is False
    assert duplicate["reason_code"].startswith("skill_registry.immutable")
    # The stored record is the untouched original, not the rejected 0.5 payload.
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored.confidence == 0.9


def test_one_accepted_evidence_does_not_activate(manager):
    _propose()
    assert _accept("actor-a")["ok"] is True

    result = mst.activate(identity="commit-msg-check", version="1.0.0")
    assert result["ok"] is False
    assert result["reason_code"] == "skill_registry.insufficient_evidence"
    # Fail-closed: the stored record is left as a proposal.
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored.lifecycle_state is sr.LifecycleState.PROPOSED


def test_two_accepted_from_same_actor_do_not_activate(manager):
    _propose()
    assert _accept("actor-a", source="first")["ok"] is True
    assert _accept("actor-a", source="second")["ok"] is True

    result = mst.activate(identity="commit-msg-check", version="1.0.0")
    assert result["ok"] is False
    assert result["reason_code"] == "skill_registry.insufficient_evidence"
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored.lifecycle_state is sr.LifecycleState.PROPOSED


def test_two_accepted_from_different_actors_activate(manager):
    _propose()
    assert _accept("actor-a")["ok"] is True
    assert _accept("actor-b")["ok"] is True

    result = mst.activate(identity="commit-msg-check", version="1.0.0")
    assert result["ok"] is True
    assert result["lifecycle_state"] == "active"

    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored.lifecycle_state is sr.LifecycleState.ACTIVE
    assert stored.accepted_count == 2
    assert len(stored.evidence) == 2


def test_requires_verified_manager_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        core, "manager_bootstrap", lambda: {"role": "worker_or_unverified_client"}
    )
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    result = _propose()
    assert result["ok"] is False
    assert result["error"] == "verified_manager_identity_required"


def test_write_gate_closed_blocks_persistence(tmp_path, monkeypatch):
    route = {
        "role": "manager",
        "provider": "claude",
        "repo": str(tmp_path),
        "manager_route": {"thread_id": "sess-123"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    result = _propose()
    assert result["ok"] is False
    assert result["error"] == "write_gate_closed"
    # Nothing was persisted.
    assert store.get_record(tmp_path, "commit-msg-check", "1.0.0") is None


def test_persists_only_through_public_store_and_registry_api():
    source = inspect.getsource(mst)
    # No private store internals and no raw connection: persistence is through
    # put_record / advance_record / load_registry only.
    assert "_db_path" not in source
    assert "sqlite3.connect" not in source
    # No private registry state is touched.
    assert "_entries" not in source
    assert "_digest_index" not in source


# ---------------------------------------------------------------------------
# The second evidence source: an actor identity READ from a finished card
# ---------------------------------------------------------------------------


def _seed_card(
    root,
    task_id,
    *,
    runner="claude_sonnet-5",
    topic="code",
    completed_at="2026-01-01T00:10:00+00:00",
):
    """Write one card into the repository's REAL canonical task store."""
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
                task_id,
                runner,
                topic,
                "",
                "done",
                "done",
                "normal",
                "objective",
                json.dumps({"task_id": task_id, "runner": runner, "topic": topic}),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:10:00+00:00",
                runner,
                "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:01:00+00:00",
                completed_at,
                "thread-1",
                "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _loaded(root, identity="commit-msg-check", version="1.0.0"):
    return store.load_registry(root).get(identity, version)


def test_task_evidence_derives_its_actor_from_the_card_not_the_caller(manager):
    _propose()
    _seed_card(manager, "AIWORKHUB_01085_V1", runner="claude_sonnet-5")
    result = mst.add_task_evidence(
        identity=BASE["identity"],
        version=BASE["version"],
        task_id="AIWORKHUB_01085_V1",
        outcome="accepted",
        note="fixed the ledger attribution",
    )
    assert result["ok"] is True

    record = _loaded(manager)
    entry = record.evidence[-1]
    # The identity was read from the card's runner, never supplied by the caller,
    # and the card id is recorded as the verifiable provenance anchor.
    assert entry.actor_id == "worker.claude.sonnet.5"
    assert entry.authority is sr.AuthorityRole.WORKER
    assert entry.source == "AIWORKHUB_01085_V1"


def test_a_quality_review_card_yields_a_reviewer_actor(manager):
    _propose()
    _seed_card(manager, "QR_1", runner="codex_cli", topic="quality_review")
    assert mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id="QR_1", outcome="accepted"
    )["ok"] is True
    assert _loaded(manager).evidence[-1].actor_id == "reviewer.codex.cli"


def test_a_card_derived_actor_can_never_collide_with_the_manager(manager):
    # The role token is emitted first by canonical_actor_id, so a card-derived
    # identity is structurally distinct from the manager's own -- even when the
    # runner is literally named after a manager.
    _propose()
    _seed_card(manager, "T_ODD", runner="claude_manager_7e6e8a47")
    assert mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id="T_ODD", outcome="accepted"
    )["ok"] is True
    actor = _loaded(manager).evidence[-1].actor_id
    assert actor == "worker.claude.7e6e8a47"
    assert sr.canonical_actor_id(actor) != sr.canonical_actor_id("manager.claude.7e6e8a47")


def test_two_cards_run_by_one_runner_are_one_actor(manager):
    # The runner is the actor; the card is only the provenance anchor. Filing
    # two cards from one runner must not manufacture independence.
    _propose()
    _seed_card(manager, "T_A", runner="claude_sonnet-5")
    _seed_card(manager, "T_B", runner="claude_sonnet-5")
    for task_id in ("T_A", "T_B"):
        assert mst.add_task_evidence(
            identity=BASE["identity"],
            version=BASE["version"],
            task_id=task_id,
            outcome="accepted",
        )["ok"] is True

    record = _loaded(manager)
    assert record.accepted_count == 2
    assert sr.independent_accepted_evidence_count(record) == 1
    assert mst.activate(identity=BASE["identity"], version=BASE["version"])["ok"] is False


def test_activation_is_reachable_through_the_new_source(manager):
    # The ordering that makes the fix safe: a second legitimate evidence source
    # exists, so canonicalizing provenance narrows what counts as independent
    # without making activation unsatisfiable.
    _propose()
    # One manager entry -- and a second manager entry under a DIFFERENT spelling
    # of the same identity, which no longer buys independence.
    _accept("manager.claude.7e6e8a47")
    _accept("claude_manager_7e6e8a47")
    assert sr.independent_accepted_evidence_count(_loaded(manager)) == 1
    denied = mst.activate(identity=BASE["identity"], version=BASE["version"])
    assert denied["ok"] is False
    assert denied["reason_code"] == "skill_registry.insufficient_evidence"

    # A card-derived worker is a genuinely distinct actor, and the gate opens.
    _seed_card(manager, "T_REAL", runner="claude_sonnet-5")
    assert mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id="T_REAL", outcome="accepted"
    )["ok"] is True
    assert sr.independent_accepted_evidence_count(_loaded(manager)) == 2

    activated = mst.activate(identity=BASE["identity"], version=BASE["version"])
    assert activated["ok"] is True
    assert activated["lifecycle_state"] == "active"
    # And the activation survives a reload, so it was not a demoted read.
    assert _loaded(manager).lifecycle_state is sr.LifecycleState.ACTIVE


@pytest.mark.parametrize(
    "task_id,seed",
    [
        ("MISSING", None),
        ("UNFINISHED", {"completed_at": ""}),
        ("NO_RUNNER", {"runner": ""}),
    ],
)
def test_task_evidence_fails_closed_without_a_verified_actor(manager, task_id, seed):
    _propose()
    task_store.initialize_repository(manager)
    if seed is not None:
        _seed_card(manager, task_id, **seed)
    result = mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id=task_id, outcome="accepted"
    )
    assert result["ok"] is False
    assert result["reason_code"] == "skill_registry.invalid_evidence"
    # Nothing was appended: no actor, no evidence.
    assert _loaded(manager).evidence == ()


def test_audit_reports_an_unverified_active_record_without_repairing_it(manager):
    _propose()
    _accept("manager.claude.7e6e8a47")
    _seed_card(manager, "T_REAL", runner="claude_sonnet-5")
    mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id="T_REAL", outcome="accepted"
    )
    mst.activate(identity=BASE["identity"], version=BASE["version"])

    report = mst.audit()
    assert report["ok"] is True
    assert report["unverified_active"] == []
    entry = report["active_records"][0]
    assert entry["independent_accepted_actors"] == 2
    assert entry["actor_ids"] == ["manager.claude.7e6e8a47", "worker.claude.sonnet.5"]
    assert entry["verified"] is True


def test_task_evidence_uses_only_public_store_and_task_api():
    source = inspect.getsource(mst)
    assert "sqlite3.connect" not in source
    assert "_decode_task_card" not in source
    assert "task_store.get_task" in source
