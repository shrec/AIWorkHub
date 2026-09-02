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

import pytest

import aiworkhub.core as core
import aiworkhub.skill_registry as sr
from aiworkhub import manager_skill_tools as mst
from aiworkhub import skill_registry_store as store

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
