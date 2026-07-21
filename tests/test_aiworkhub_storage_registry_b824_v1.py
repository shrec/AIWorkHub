"""B878: storage registry is canonical-only from creation.

Superseded from the original B824 shadow/legacy-fallback design: a fresh
registry now carries only ``id``/``component``/``canonical_durable_path`` and
an ``authority`` block already asserting ``canonical_active`` -- no
``legacy_source`` field, no baked-in rollback hash, no per-entry
``integrity``/``migration`` bookkeeping. ``_parse_database`` still parses an
older, richer registry (produced by an explicit migration/cutover tool) for
backward compatibility, which is exercised separately in
``test_aiworkhub_taskdb_migrator_b825_v1.py`` /
``test_aiworkhub_fresh_taskdb_cutover_b832_v1.py``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from aiworkhub.repository_state import PathEscapeError, bootstrap_repository
from aiworkhub.storage_registry import (
    CANONICAL_DATABASES,
    STORAGE_REGISTRY_SCHEMA_ID,
    StorageRegistryInvalidError,
    default_registry_payload,
    load_storage_registry,
    resolve_database_path,
)


EXPECTED_PATHS = {
    "task_queue": "tasking/task_queue.sqlite",
    "source_graph": "source_graph/source_graph.sqlite",
    "universal": "source_graph/universal.sqlite",
    "session": "sessions/sessions.sqlite",
    "transcript": "sessions/transcript_graph.sqlite",
    "memory": "memory/memory.sqlite",
    "kb": "kb/knowledge.sqlite",
}


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _copy_registry_to_tmp_repo(tmp_path: Path, payload: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    state = bootstrap_repository(
        repo,
        repo_id="repo_1234567890abcdef1234567890abcdef",
        repo_name="tmp",
        created_at="2026-07-20T00:00:00+00:00",
    )
    registry = state.hub_dir / "config" / "storage.json"
    registry.write_text(
        json.dumps(payload or default_registry_payload(state.manifest.repo_id), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return repo


def test_canonical_databases_inventory_carries_no_legacy_or_hash_fields():
    for item in CANONICAL_DATABASES:
        assert set(item) == {"id", "component", "canonical_path"}
        assert "legacy_source" not in item
        assert "sha256" not in item


def test_default_registry_payload_entries_are_minimal_and_already_canonical():
    payload = default_registry_payload("repo_deadbeefdeadbeefdeadbeefdeadbeef")
    assert set(EXPECTED_PATHS) == {item["id"] for item in payload["databases"]}
    for item in payload["databases"]:
        assert set(item) == {"id", "component", "canonical_durable_path", "authority"}
        assert item["canonical_durable_path"] == EXPECTED_PATHS[item["id"]]
        assert item["authority"] == {
            "state": "canonical_active",
            "canonical_active": True,
            "legacy_active": False,
            "live_cutover": True,
        }
        assert "legacy_source" not in item
        assert "integrity" not in item
        assert "migration" not in item


def test_repository_registry_loads_canonical_inventory_without_host_paths(tmp_path: Path):
    repo = _copy_registry_to_tmp_repo(tmp_path)
    payload = json.loads((repo / ".aiworkhub" / "config" / "storage.json").read_text(encoding="utf-8"))
    project = json.loads((repo / ".aiworkhub" / "project.json").read_text(encoding="utf-8"))

    assert payload["schema_id"] == STORAGE_REGISTRY_SCHEMA_ID
    assert payload["repo_id"] == project["repo_id"]
    assert payload["host_absolute_paths_allowed"] is False
    assert payload["durable_root"] == ".aiworkhub"
    assert payload["runtime_policy"]["ignored"] is True
    assert payload["runtime_policy"]["durable"] is False
    assert payload["runtime_policy"]["sqlite_policy"]["wal_shm_are_runtime_only"] is True
    assert payload["migration_policy"]["no_live_cutover_in_this_registry"] is True
    assert payload["migration_policy"]["no_legacy_delete_in_this_registry"] is True

    for value in _walk_strings(payload):
        assert not Path(value).is_absolute()
        assert not value.startswith("file:")
        assert "/tmp/" not in value
        assert "/home/" not in value

    registry = load_storage_registry(repo)
    assert set(registry.databases) == set(EXPECTED_PATHS)
    for db_id, db in registry.databases.items():
        assert db.canonical_path == EXPECTED_PATHS[db_id]
        assert db.rollback_source_sha256 == ""
        assert db.integrity_state == ""
        assert db.authority_state == "canonical_active"
        assert db.canonical_active is True
        assert db.legacy_active is False
        assert db.live_cutover is True
        assert db.migration_generation == 1


def test_resolved_paths_are_manifest_bound_under_aiworkhub(tmp_path: Path):
    repo = _copy_registry_to_tmp_repo(tmp_path)
    registry = load_storage_registry(repo)
    for db_id, rel in EXPECTED_PATHS.items():
        assert resolve_database_path(registry, db_id) == repo / ".aiworkhub" / rel

    with pytest.raises(StorageRegistryInvalidError, match="repo_id_mismatch"):
        load_storage_registry(repo, expected_repo_id="repo_different")


def test_registry_rejects_cross_repository_reuse(tmp_path):
    repo = _copy_registry_to_tmp_repo(tmp_path)
    payload = json.loads((repo / ".aiworkhub/config/storage.json").read_text(encoding="utf-8"))
    payload["repo_id"] = "repo_other1234567890abcdef1234567890ab"
    (repo / ".aiworkhub/config/storage.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StorageRegistryInvalidError, match="repo_id_mismatch"):
        load_storage_registry(repo)


def test_registry_rejects_traversal_and_symlinked_durable_paths(tmp_path):
    repo = _copy_registry_to_tmp_repo(tmp_path)
    registry_path = repo / ".aiworkhub/config/storage.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    payload["databases"][0]["canonical_durable_path"] = "../task_queue.sqlite"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PathEscapeError, match="path_escape"):
        load_storage_registry(repo)

    repo = _copy_registry_to_tmp_repo(tmp_path / "symlink")
    outside = tmp_path / "outside"
    outside.mkdir()
    kb_dir = repo / ".aiworkhub/kb"
    shutil.rmtree(kb_dir)
    os.symlink(outside, kb_dir)
    with pytest.raises(PathEscapeError, match="symlink"):
        load_storage_registry(repo)


def test_richer_legacy_style_entry_still_parses_for_backward_compatibility(tmp_path: Path):
    """An older registry, produced before this task's simplification (or by
    an explicit migration/cutover tool), still supplies ``integrity`` and
    ``migration`` blocks -- those remain valid, non-fresh input."""
    repo = _copy_registry_to_tmp_repo(tmp_path)
    registry_path = repo / ".aiworkhub/config/storage.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    kb_entry = next(item for item in payload["databases"] if item["id"] == "kb")
    kb_entry["integrity"] = {"state": "cutover_verified", "rollback_source_sha256": "a" * 64}
    kb_entry["migration"] = {
        "generation": 3,
        "cutover_performed": True,
        "rollback_performed": False,
        "legacy_deleted": False,
        "rollback_source_hash": "a" * 64,
    }
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    registry = load_storage_registry(repo)
    db = registry.databases["kb"]
    assert db.migration_generation == 3
    assert db.integrity_state == "cutover_verified"
    assert db.rollback_source_sha256 == "a" * 64
