"""Tests for the durable skill registry store and its dashboard read.

Covers the acceptance contract: a SkillRecord round-trips with its content
digest byte-identical; a tampered stored digest fails closed on read; two
versions of one identity both persist without overwrite; a digest can never be
rebound to a different identity; and the dashboard skills projection input loads
persisted records while degrading to an empty registry when the store is absent
or unreadable.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import aiworkhub.skill_registry as sr
from aiworkhub import skill_registry_store as store
from aiworkhub.dashboard import DashboardProvider

WORKER = sr.Authority(sr.AuthorityRole.WORKER, actor_id="worker-1", token="worker-token")
MANAGER = sr.Authority(sr.AuthorityRole.MANAGER, actor_id="manager-1", token="manager-secret")

BASE_DATA = {
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


def base_record(**overrides):
    data = dict(BASE_DATA)
    data.update(overrides)
    return sr.SkillRecord.from_mapping(data)


def active_record(identity="commit-msg-check", version="1.0.0", **overrides):
    """A realistic active record carrying two accepted evidence entries."""
    registry = sr.SkillRegistry(min_accepted_evidence=2)
    registry.propose(base_record(identity=identity, version=version, **overrides), WORKER)
    for actor_id in ("actor-a", "actor-b"):
        actor = sr.Authority(sr.AuthorityRole.WORKER, actor_id=actor_id, token=f"tok-{actor_id}")
        registry.add_evidence(
            identity,
            version,
            {"source": f"src-{actor_id}", "outcome": "accepted"},
            actor,
        )
    return registry.activate(identity, version, MANAGER)


def _stored_digest(repo_root, identity, version):
    conn = sqlite3.connect(str(store._db_path(repo_root)))
    try:
        row = conn.execute(
            "SELECT digest FROM skill_records WHERE identity=? AND version=?",
            (identity, version),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_round_trip_preserves_digest_and_runtime_state(tmp_path):
    record = active_record()
    put = store.put_record(tmp_path, record)

    loaded = store.get_record(tmp_path, record.identity, record.version)
    assert loaded == record
    # The content digest is byte-identical across the write and the read.
    assert sr.skill_digest(loaded) == sr.skill_digest(record)
    assert put["digest"] == sr.skill_digest(record)
    assert _stored_digest(tmp_path, record.identity, record.version) == sr.skill_digest(record)
    # Runtime state survives, not just the digested content fields.
    assert loaded.lifecycle_state is sr.LifecycleState.ACTIVE
    assert loaded.accepted_count == 2
    assert len(loaded.evidence) == 2


def test_tampered_stored_digest_fails_closed(tmp_path):
    record = active_record()
    store.put_record(tmp_path, record)
    conn = sqlite3.connect(str(store._db_path(tmp_path)))
    try:
        conn.execute("UPDATE skill_records SET digest=?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(store.SkillStoreIntegrityError):
        store.get_record(tmp_path, record.identity, record.version)


def test_tampered_payload_fails_closed(tmp_path):
    record = active_record()
    store.put_record(tmp_path, record)
    conn = sqlite3.connect(str(store._db_path(tmp_path)))
    try:
        row = conn.execute("SELECT payload_json FROM skill_records").fetchone()
        payload = json.loads(row[0])
        # A valid but different content value: the digest column is left intact,
        # so the recomputed digest no longer matches and the read fails closed.
        payload["confidence"] = 0.1
        conn.execute(
            "UPDATE skill_records SET payload_json=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(store.SkillStoreIntegrityError):
        store.get_record(tmp_path, record.identity, record.version)


def test_consistently_tampered_runtime_state_fails_closed(tmp_path):
    # Store an honest PROPOSED record carrying zero evidence.
    proposed = base_record()
    store.put_record(tmp_path, proposed)
    # A real, internally-consistent ACTIVE record over the SAME content fields.
    # skill_digest hashes only content, so its content digest is identical and
    # the intact digest column still matches -- only the runtime authorization
    # state (lifecycle, accepted_count, accepted evidence) differs.
    forged_active = active_record()
    assert sr.skill_digest(forged_active) == sr.skill_digest(proposed)
    forged_payload = json.dumps(
        store._record_payload(forged_active), sort_keys=True, separators=(",", ":")
    )
    conn = sqlite3.connect(str(store._db_path(tmp_path)))
    try:
        conn.execute("UPDATE skill_records SET payload_json=?", (forged_payload,))
        conn.commit()
    finally:
        conn.close()
    # from_mapping accepts the consistent record and the content digest still
    # matches; the full-payload state digest must reject the forged authority.
    with pytest.raises(store.SkillStoreIntegrityError):
        store.get_record(tmp_path, proposed.identity, proposed.version)


def test_two_versions_of_one_identity_both_persist(tmp_path):
    v1 = active_record(version="1.0.0")
    v2 = active_record(version="2.0.0")
    store.put_record(tmp_path, v1)
    store.put_record(tmp_path, v2)

    assert store.get_record(tmp_path, "commit-msg-check", "1.0.0") == v1
    assert store.get_record(tmp_path, "commit-msg-check", "2.0.0") == v2
    records = store.list_records(tmp_path)
    assert {(r.identity, r.version) for r in records} == {
        ("commit-msg-check", "1.0.0"),
        ("commit-msg-check", "2.0.0"),
    }
    assert sr.skill_digest(v1) != sr.skill_digest(v2)


def test_duplicate_identity_version_is_immutable(tmp_path):
    record = active_record()
    store.put_record(tmp_path, record)
    with pytest.raises(store.SkillStoreConflictError):
        store.put_record(tmp_path, record)
    # A different-content write to the same identity/version is also refused.
    with pytest.raises(store.SkillStoreConflictError):
        store.put_record(tmp_path, base_record(confidence=0.5))


def test_digest_can_never_be_rebound(tmp_path):
    record = active_record()
    put = store.put_record(tmp_path, record)
    conn = sqlite3.connect(str(store._db_path(tmp_path)))
    try:
        # A second identity/version carrying an already-stored digest must be
        # refused by the unique digest index -- the digest cannot be rebound.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO skill_records (identity,version,digest,payload_json,created_at) "
                "VALUES (?,?,?,?,?)",
                ("other-skill", "9.9.9", put["digest"], "{}", "now"),
            )
    finally:
        conn.close()


def test_get_record_absent_returns_none(tmp_path):
    assert store.get_record(tmp_path, "missing", "1.0.0") is None
    # A read must never create the database as a side effect.
    assert not store._db_path(tmp_path).exists()


def test_load_registry_absent_store_is_empty(tmp_path):
    registry = store.load_registry(tmp_path)
    assert isinstance(registry, sr.SkillRegistry)
    assert len(registry) == 0


def test_load_registry_unreadable_store_is_empty(tmp_path):
    db_path = store._db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text("this is not a sqlite database")
    registry = store.load_registry(tmp_path)
    assert isinstance(registry, sr.SkillRegistry)
    assert len(registry) == 0


def test_load_registry_returns_persisted_records(tmp_path):
    store.put_record(tmp_path, active_record(version="1.0.0"))
    store.put_record(tmp_path, active_record(version="2.0.0"))
    registry = store.load_registry(tmp_path)
    assert len(registry) == 2
    assert {(r.identity, r.version) for r in registry.records()} == {
        ("commit-msg-check", "1.0.0"),
        ("commit-msg-check", "2.0.0"),
    }


def test_dashboard_projection_input_absent_is_empty_registry(tmp_path):
    provider = DashboardProvider(repo_root=tmp_path)
    registry = provider.get_skills_projection_input()
    assert type(registry).__name__ == "SkillRegistry"
    assert len(registry) == 0


def test_dashboard_projection_input_returns_persisted_records(tmp_path):
    store.put_record(tmp_path, active_record(version="1.0.0"))
    store.put_record(tmp_path, active_record(version="2.0.0"))
    provider = DashboardProvider(repo_root=tmp_path)
    registry = provider.get_skills_projection_input()
    assert type(registry).__name__ == "SkillRegistry"
    assert len(registry) == 2


def test_dashboard_projection_input_unreadable_store_is_empty(tmp_path):
    db_path = store._db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text("corrupt")
    provider = DashboardProvider(repo_root=tmp_path)
    registry = provider.get_skills_projection_input()
    assert type(registry).__name__ == "SkillRegistry"
    assert len(registry) == 0
