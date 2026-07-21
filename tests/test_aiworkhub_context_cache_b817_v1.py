from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from aiworkhub.context_cache import (
    EMPTY_INPUT_SHARD_SHA256,
    OUTCOME_CORRUPT,
    OUTCOME_HIT,
    OUTCOME_MISS,
    OUTCOME_STALE,
    ContextCache,
    ContextCacheError,
    ContextCacheKey,
    cache_key_sha256,
    key_material,
    reuse_metadata,
)


def _key(repo_id: str = "repo-a", *, revision: str = "abc123") -> ContextCacheKey:
    return ContextCacheKey(
        repo_id=repo_id,
        source_revision=revision,
        context_policy={
            "schema_id": "aiworkhub.task_mcp.project_context_bundle.v1",
            "task_type": "code",
            "primary_context": "source_graph",
            "source_graph_required": True,
            "sections": ["source_graph", "session_current_state", "ai_memory", "kb"],
        },
        tool_schema_id="aiworkhub.task_mcp.project_context_bundle.v1",
        tool_version="project_context.py:b817-v1",
        immutable_input_shard_sha256=EMPTY_INPUT_SHARD_SHA256,
    )


def _entry_path(cache: ContextCache, key: ContextCacheKey) -> Path:
    key_sha = cache_key_sha256(key)
    return cache.repo_root / key_sha[:2] / f"{key_sha}.json"


def test_cache_key_uses_repo_revision_policy_tool_and_input_shard() -> None:
    base = _key()
    same = _key()
    changed_policy = replace(
        base, context_policy={**base.context_policy, "task_type": "research"}
    )
    changed_tool = replace(base, tool_version="project_context.py:next")
    changed_repo = _key(repo_id="repo-b")
    assert cache_key_sha256(base) == cache_key_sha256(same)
    assert len({
        cache_key_sha256(base),
        cache_key_sha256(changed_policy),
        cache_key_sha256(changed_tool),
        cache_key_sha256(changed_repo),
        cache_key_sha256(_key(revision="def456")),
    }) == 5
    material = key_material(base)
    assert material["repo_id"] == "repo-a"
    assert material["immutable_input_shard_sha256"] == EMPTY_INPUT_SHARD_SHA256
    assert "task_id" not in json.dumps(material)
    assert "prompt" not in json.dumps(material).lower()


def test_repo_scoped_roots_prevent_cross_repo_reads(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    cache_a = ContextCache(cache_root=root, repo_id="repo-a")
    cache_b = ContextCache(cache_root=root, repo_id="repo-b")
    key_a = _key("repo-a")
    key_b = _key("repo-b")
    written = cache_a.write(key_a, "PROJECT_CONTEXT_BUNDLE:\n{}", metadata={"source": "unit"})
    assert written.stored is True
    assert cache_a.read(key_a).outcome == OUTCOME_HIT
    assert cache_b.read(key_b).outcome == OUTCOME_MISS
    with pytest.raises(ContextCacheError, match="repo_id_cache_mismatch"):
        cache_b.write(key_a, "PROJECT_CONTEXT_BUNDLE:\n{}")


def test_payload_hash_substitution_is_corrupt_and_fail_closed(tmp_path: Path) -> None:
    cache = ContextCache(cache_root=tmp_path / "cache", repo_id="repo-a")
    key = _key()
    cache.write(key, "PROJECT_CONTEXT_BUNDLE:\n{\"ok\":true}")
    path = _entry_path(cache, key)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["payload"] = "PROJECT_CONTEXT_BUNDLE:\n{\"tampered\":true}"
    path.write_text(json.dumps(entry), encoding="utf-8")
    result = cache.read(key)
    assert result.outcome == OUTCOME_CORRUPT
    assert "payload_hash_mismatch" in result.reason
    assert not path.exists()
    assert cache.read(key).outcome == OUTCOME_MISS


def test_stale_entries_have_explicit_outcome_and_are_removed(tmp_path: Path) -> None:
    cache = ContextCache(cache_root=tmp_path / "cache", repo_id="repo-a", max_age_seconds=1)
    key = _key()
    cache.write(key, "PROJECT_CONTEXT_BUNDLE:\n{}")
    path = _entry_path(cache, key)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["created_at_epoch"] = time.time() - 10
    path.write_text(json.dumps(entry), encoding="utf-8")
    result = cache.read(key)
    assert result.outcome == OUTCOME_STALE
    assert result.reason == "entry_age_limit"
    assert not path.exists()


def test_entry_and_total_byte_limits_are_bounded_and_lru(tmp_path: Path) -> None:
    cache = ContextCache(
        cache_root=tmp_path / "cache",
        repo_id="repo-a",
        max_entry_bytes=24,
        max_total_bytes=2200,
        max_age_seconds=3600,
    )
    with pytest.raises(ContextCacheError, match="payload_too_large"):
        cache.write(_key(revision="too-big"), "x" * 25)

    first = _key(revision="rev-1")
    second = _key(revision="rev-2")
    third = _key(revision="rev-3")
    cache.write(first, "bundle-one")
    cache.write(second, "bundle-two")
    assert cache.read(first).outcome == OUTCOME_HIT
    cache.write(third, "bundle-three")
    outcomes = {key.source_revision: cache.read(key).outcome for key in (first, second, third)}
    assert outcomes["rev-1"] == OUTCOME_HIT
    assert outcomes["rev-2"] == OUTCOME_MISS
    assert outcomes["rev-3"] == OUTCOME_HIT


def test_symlink_and_traversal_inputs_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "cache-link"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(ContextCacheError, match="cache_root_symlink"):
        ContextCache(cache_root=symlink, repo_id="repo-a")
    with pytest.raises(ContextCacheError, match="repo_id_invalid"):
        ContextCache(cache_root=tmp_path / "cache", repo_id="../repo-a")

    cache = ContextCache(cache_root=tmp_path / "cache", repo_id="repo-a")
    key = _key()
    bucket = cache.repo_root / cache_key_sha256(key)[:2]
    bucket.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ContextCacheError, match="cache_bucket_symlink"):
        cache.write(key, "PROJECT_CONTEXT_BUNDLE:\n{}")


def test_sensitive_material_and_provider_billing_claims_are_not_cached(tmp_path: Path) -> None:
    cache = ContextCache(cache_root=tmp_path / "cache", repo_id="repo-a")
    with pytest.raises(ContextCacheError, match="metadata_contains_sensitive_field"):
        cache.write(_key(), "PROJECT_CONTEXT_BUNDLE:\n{}", metadata={"coordinator_token": "x"})
    with pytest.raises(ContextCacheError, match="payload_contains_sensitive_field"):
        cache.write(_key(revision="secret"), 'PROJECT_CONTEXT_BUNDLE:\n{"api_key":"abc"}')
    metadata = reuse_metadata(cache_key=cache_key_sha256(_key()), outcome=OUTCOME_HIT)
    assert metadata["local_recomputation_avoided"] is True
    assert metadata["future_exact_bundle_reuse_candidate"] is True
    assert metadata["provider_prompt_cache_billing_savings_claimed"] is False


def test_entry_symlink_is_corrupt_not_followed(tmp_path: Path) -> None:
    cache = ContextCache(cache_root=tmp_path / "cache", repo_id="repo-a")
    key = _key()
    path = _entry_path(cache, key)
    path.parent.mkdir(parents=True)
    os.symlink(tmp_path / "external.json", path)
    result = cache.read(key)
    assert result.outcome == OUTCOME_CORRUPT
    assert "entry_symlink" in result.reason
