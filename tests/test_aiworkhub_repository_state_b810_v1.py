from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aiworkhub import repository_state as rs
from aiworkhub.storage_registry import load_storage_registry


def _git_init(path: Path) -> None:
    path.mkdir(parents=True)
    (path / ".git").mkdir()


def test_two_same_named_repositories_keep_distinct_ids(tmp_path: Path) -> None:
    first = tmp_path / "one" / "AIWorkHub"
    second = tmp_path / "two" / "AIWorkHub"
    _git_init(first)
    _git_init(second)

    a = rs.bootstrap_repository(first, repo_id="repo_a0000000000000000000000000000001")
    b = rs.bootstrap_repository(second, repo_id="repo_b0000000000000000000000000000002")

    assert a.root.name == b.root.name == "AIWorkHub"
    assert a.manifest.repo_id != b.manifest.repo_id
    assert rs.inspect_repository(first).manifest.repo_id == a.manifest.repo_id
    assert rs.inspect_repository(second).manifest.repo_id == b.manifest.repo_id


def test_repo_id_survives_directory_move(tmp_path: Path) -> None:
    original = tmp_path / "before" / "project"
    _git_init(original)
    state = rs.bootstrap_repository(original, repo_id="repo_move_stable_001")
    moved = tmp_path / "after" / "renamed"
    moved.parent.mkdir()
    original.rename(moved)

    inspected = rs.inspect_repository(moved)
    assert inspected.manifest.repo_id == state.manifest.repo_id
    assert inspected.root == moved.resolve()


def test_missing_and_invalid_manifests_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    with pytest.raises(rs.ManifestMissingError):
        rs.inspect_repository(repo)

    hub = repo / rs.HUB_DIRNAME
    hub.mkdir()
    (hub / "project.json").write_text('{"schema_id":"wrong"}\n', encoding="utf-8")
    with pytest.raises(rs.ManifestInvalidError):
        rs.inspect_repository(repo)


def test_resolver_precedence_explicit_env_manifest_then_git(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    env_repo = tmp_path / "env"
    cwd_repo = tmp_path / "cwd"
    for path in (explicit, env_repo, cwd_repo):
        _git_init(path)
        rs.bootstrap_repository(path, repo_id=f"repo_{path.name}_123456789")
    nested = cwd_repo / "sub" / "dir"
    nested.mkdir(parents=True)

    env = {"AIWORKHUB_REPO_ROOT": str(env_repo)}
    assert rs.resolve_repository_root(explicit, cwd=nested, env=env) == explicit.resolve()
    assert rs.resolve_repository_root(cwd=nested, env=env) == env_repo.resolve()
    assert rs.resolve_repository_root(cwd=nested, env={}) == cwd_repo.resolve()

    no_manifest_git = tmp_path / "git-only"
    _git_init(no_manifest_git)
    with pytest.raises(rs.ManifestMissingError):
        rs.resolve_repository_root(cwd=no_manifest_git, env={})
    assert rs.resolve_repository_root(cwd=no_manifest_git, env={}, require_manifest=False) == no_manifest_git.resolve()


def test_bootstrap_is_non_destructive_and_manifest_write_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    created = rs.bootstrap_repository(repo, repo_id="repo_atomic_success")
    with pytest.raises(rs.ManifestExistsError):
        rs.bootstrap_repository(repo, repo_id=created.manifest.repo_id)
    assert rs.inspect_repository(repo).manifest.repo_id == "repo_atomic_success"

    failing = tmp_path / "failing"
    _git_init(failing)

    def boom(_src: str, _dst: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        rs.bootstrap_repository(failing, repo_id="repo_atomic_failure")
    assert not (failing / rs.PROJECT_MANIFEST_REL).exists()
    assert not list((failing / rs.HUB_DIRNAME).glob(".project.json.*.tmp"))


def test_expected_repo_id_blocks_silent_adoption(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    rs.bootstrap_repository(repo, repo_id="repo_owner_a")
    with pytest.raises(rs.ManifestInvalidError):
        rs.inspect_repository(repo, expected_repo_id="repo_owner_b")
    with pytest.raises(rs.ManifestInvalidError):
        rs.bootstrap_repository(repo, repo_id="repo_owner_b")


def test_path_traversal_and_symlink_escapes_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / rs.HUB_DIRNAME).symlink_to(tmp_path)
    with pytest.raises(rs.PathEscapeError):
        rs.bootstrap_repository(repo, repo_id="repo_symlink_rejected")

    other = tmp_path / "other"
    _git_init(other)
    rs.bootstrap_repository(other, repo_id="repo_layout_rejected")
    manifest_path = other / rs.PROJECT_MANIFEST_REL
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["layout"]["durable"]["kb"] = "../outside"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((rs.ManifestInvalidError, rs.PathEscapeError)):
        rs.inspect_repository(other)


def test_bootstrap_does_not_discover_or_adopt_a_planted_legacy_path(tmp_path: Path) -> None:
    """B878: this repository state carries no legacy-discovery feature at
    all -- a planted ``bitnnv2/data/tasking`` legacy path must be left
    completely untouched and unreferenced by bootstrap, not surfaced as a
    read-only "candidate" (that mechanism was removed; ``RepositoryState``
    has no such field)."""
    repo = tmp_path / "repo"
    _git_init(repo)
    legacy = repo / "bitnnv2" / "data" / "tasking"
    legacy.mkdir(parents=True)
    (legacy / "machine_task_cards_v1.jsonl").write_text("legacy\n", encoding="utf-8")
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))

    state = rs.bootstrap_repository(repo, repo_id="repo_legacy_readonly")
    after = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))

    assert not hasattr(state, "legacy_candidates")
    assert state.manifest.to_json()["security"]["automatic_legacy_discovery"] is False
    registry = load_storage_registry(repo)
    assert "bitnnv2" not in json.dumps(registry.payload)
    # The legacy directory is neither deleted nor rewritten: bootstrap only
    # ever ADDS its own canonical .aiworkhub tree.
    assert "bitnnv2/data/tasking/machine_task_cards_v1.jsonl" in after
    assert set(before).issubset(after)
