"""NF-2026-01739: over-cap required outputs are sealed as sidecar blobs, never refused.

GeoAI NF-00193 produced train.jsonl (31.9 MB) and dev.jsonl (6.5 MB) as
required outputs and could not reach review: ``_read_regular`` refused any
file over the 8 MiB inline cap that bounds the base64 JSON delta artifact.
Such a file is now captured by digest, streamed into
``<runtime>/rework_deltas/blobs/<sha256>`` at seal, and named by the SEALED
v1 descriptor's artifact entry as ``blob_sha256`` + ``size``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import attempt_artifacts, task_store, worker_workspace
from aiworkhub import successful_rework_recovery as recovery

LARGE_BYTES = 40 * 1024 * 1024
LARGE = "data/train.jsonl"
SMALL = "src/consumer.py"
CLAIM_EPOCH = 3


def _write_large(path: Path) -> str:
    """A quickly generated 40 MB file: real bytes at both ends, zeros between."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(b'{"head": 1}\n')
        handle.truncate(LARGE_BYTES - 12)
        handle.seek(LARGE_BYTES - 12)
        handle.write(b'{"tail": 2}\n')
    return _sha(path)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture
def large_episode(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    request_id = "c" * 32
    task_id = "SUCCESSFUL_REWORK_LARGE"
    runtime = repo / ".aiworkhub" / "runtime"
    workspace = runtime / "worktrees" / request_id / "worktree"
    small = workspace / SMALL
    small.parent.mkdir(parents=True)
    small.write_bytes(b"exact candidate\n")
    large_sha = _write_large(workspace / LARGE)
    hashes = {SMALL: hashlib.sha256(b"exact candidate\n").hexdigest(), LARGE: large_sha}
    workspace_meta = {"request_id": request_id, "repo": str(repo), "path": str(workspace)}
    identity = {"request_id": request_id, "task_id": task_id, "runner": "worker", "topic": "rework"}
    payloads = {
        "metadata": {"schema_id": "aiworkhub.attempt_metadata.v1", "request_identity": identity, "workspace": workspace_meta},
        "diff": {"schema_id": "aiworkhub.attempt_diff_index.v1", "changed_paths": sorted(hashes), "changed_path_hashes": hashes, "required_outputs": []},
        "validation": {"schema_id": "aiworkhub.attempt_validation.v1", "checks": []},
        "usage": {"schema_id": "aiworkhub.attempt_usage.v1"},
        "review": {"schema_id": "aiworkhub.attempt_review.v1", "kind": "worker_candidate", "target_state": "review_ready", "error": ""},
    }
    bundle = runtime / "process_logs" / "processes" / "attempt-artifacts" / request_id
    receipt = attempt_artifacts.persist_json_bundle(bundle, attempt_id=request_id, payloads=payloads)
    evidence = {"request_identity": identity, "workspace": workspace_meta, "changed_path_hashes": hashes,
                "changed_paths": sorted(hashes), "attempt_artifact_manifest": receipt}
    terminal = {"request_id": request_id, "claim_epoch": CLAIM_EPOCH, "runner": "worker",
                "substatus": "review_ready", "evidence": evidence}
    return SimpleNamespace(repo=repo, request_id=request_id, task_id=task_id, runtime=runtime, workspace=workspace,
                           hashes=hashes, large_sha=large_sha, evidence=evidence, terminal=terminal)


def _recover(episode):
    return recovery.recover_descriptor(
        episode.repo, episode.task_id, episode.request_id, CLAIM_EPOCH, episode.evidence,
        terminal_episode=episode.terminal,
    )


def _candidate(episode, paths=(LARGE, SMALL)):
    workspace = SimpleNamespace(path=episode.workspace, repo=episode.repo)
    return recovery.successful_candidate_evidence(
        workspace, {"task_id": episode.task_id, "claim_epoch": CLAIM_EPOCH}, episode.request_id, list(paths),
    )


def _artifact(descriptor):
    return {"path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"]}


def _predecessor(episode, descriptor):
    return {"task_id": episode.task_id, "request_id": episode.request_id, "claim_epoch": CLAIM_EPOCH,
            "rework_delta": descriptor, "delta_artifact": _artifact(descriptor)}


def _blob(episode) -> Path:
    return (episode.runtime / "rework_deltas").resolve() / worker_workspace.REWORK_DELTA_BLOB_DIRNAME / episode.large_sha


def _materialize(episode, descriptor, target: Path, hashes=None, rebase=None):
    hashes = episode.hashes if hashes is None else hashes
    return worker_workspace.materialize_rework_delta_artifact(
        _artifact(descriptor), episode.repo, episode.request_id, episode.task_id, CLAIM_EPOCH, target,
        hashes, tuple(hashes), rebase=rebase,
    )


def _tamper(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.seek(LARGE_BYTES // 2)
        handle.write(b"X")


def test_capture_carries_an_over_cap_file_by_digest_and_keeps_small_bytes_inline(large_episode):
    entries = dict(recovery.capture_candidate_paths(large_episode.workspace, [LARGE, SMALL]))
    assert entries[SMALL] == b"exact candidate\n"
    carried = entries[LARGE]
    assert isinstance(carried, recovery.DigestCarriedContent)
    assert (carried.path, carried.sha256, carried.size) == (LARGE, large_episode.large_sha, LARGE_BYTES)
    assert recovery.captured_path_hashes(list(entries.items())) == large_episode.hashes


def test_over_cap_file_is_hashed_in_bounded_chunks(large_episode, monkeypatch):
    reads = []
    original = recovery._digest_open_regular

    def spy(fd, stream, before, sink=None):
        real_read = stream.read

        class Bounded:
            def read(self, size=-1):
                reads.append(size)
                return real_read(size)

        return original(fd, Bounded(), before, sink)

    monkeypatch.setattr(recovery, "_digest_open_regular", spy)
    recovery.capture_candidate_paths(large_episode.workspace, [LARGE])
    assert reads and all(0 < size <= worker_workspace._REWORK_BLOB_CHUNK_BYTES for size in reads)


def test_recovery_over_a_40mb_required_output_reaches_a_sealed_descriptor(large_episode, require_directory_authority):
    require_directory_authority(large_episode.workspace)
    descriptor = _recover(large_episode)
    assert set(descriptor) == {
        "schema_id", "sealed", "authority_repo", "task_id", "request_id", "claim_epoch",
        "artifact_path", "artifact_sha256",
    }
    assert descriptor["schema_id"] == "aiworkhub.rework_delta_descriptor.v1"
    assert descriptor["sealed"] is True
    assert worker_workspace.has_verified_rework_delta(
        _predecessor(large_episode, descriptor), authority_repo=large_episode.repo,
    )
    artifact = Path(descriptor["artifact_path"])
    assert artifact.stat().st_size < 1024 * 1024  # the 40 MB never rides inline
    files = {entry["path"]: entry for entry in json.loads(artifact.read_text(encoding="utf-8"))["files"]}
    assert files[LARGE] == {"path": LARGE, "sha256": large_episode.large_sha,
                            "blob_sha256": large_episode.large_sha, "size": LARGE_BYTES}
    assert "content_base64" in files[SMALL] and "blob_sha256" not in files[SMALL]
    assert _blob(large_episode).stat().st_size == LARGE_BYTES
    assert _sha(_blob(large_episode)) == large_episode.large_sha
    assert worker_workspace.verified_rework_delta_file_hashes(
        _predecessor(large_episode, descriptor), authority_repo=large_episode.repo,
    ) == large_episode.hashes


def test_sealed_blob_survives_releasing_the_worktree_and_materializes_byte_exact(large_episode, tmp_path):
    _paths, hashes, descriptor = _candidate(large_episode)
    assert hashes == large_episode.hashes
    # Retention may now release the worktree: the bytes live beside the delta.
    (large_episode.workspace / LARGE).unlink()
    (large_episode.workspace / SMALL).unlink()
    target = tmp_path / "successor"
    target.mkdir()
    assert _materialize(large_episode, descriptor, target) == [LARGE, SMALL]
    assert _sha(target / LARGE) == large_episode.large_sha
    assert (target / LARGE).stat().st_size == LARGE_BYTES
    assert (target / SMALL).read_bytes() == b"exact candidate\n"
    assert sorted(p.name for p in (target / LARGE).parent.iterdir()) == [Path(LARGE).name]


def test_a_tampered_blob_fails_closed_and_writes_nothing(large_episode, tmp_path):
    _paths, _hashes, descriptor = _candidate(large_episode)
    _tamper(_blob(large_episode))
    target = tmp_path / "successor"
    target.mkdir()
    with pytest.raises(worker_workspace.WorkspaceError, match="rework_delta_blob_hash_mismatch"):
        _materialize(large_episode, descriptor, target)
    assert list(target.iterdir()) == []


def test_a_truncated_blob_fails_closed_and_is_not_a_verified_delta(large_episode, tmp_path):
    _paths, _hashes, descriptor = _candidate(large_episode)
    with _blob(large_episode).open("r+b") as handle:
        handle.truncate(LARGE_BYTES - 1)
    assert not worker_workspace.has_verified_rework_delta(
        _predecessor(large_episode, descriptor), authority_repo=large_episode.repo,
    )
    target = tmp_path / "successor"
    target.mkdir()
    with pytest.raises(worker_workspace.WorkspaceError, match="rework_delta_blob_size_mismatch"):
        _materialize(large_episode, descriptor, target)
    assert list(target.iterdir()) == []


def test_a_missing_blob_is_not_a_verified_delta(large_episode, tmp_path):
    _paths, _hashes, descriptor = _candidate(large_episode)
    _blob(large_episode).unlink()
    assert not worker_workspace.has_verified_rework_delta(
        _predecessor(large_episode, descriptor), authority_repo=large_episode.repo,
    )
    target = tmp_path / "successor"
    target.mkdir()
    with pytest.raises(worker_workspace.WorkspaceError, match="rework_delta_blob_missing"):
        _materialize(large_episode, descriptor, target)
    assert list(target.iterdir()) == []


def test_a_symlinked_blob_fails_closed(large_episode, tmp_path):
    _paths, _hashes, descriptor = _candidate(large_episode)
    blob = _blob(large_episode)
    outside = tmp_path / "outside.blob"
    blob.replace(outside)
    try:
        blob.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unsupported here: {exc}")
    assert not worker_workspace.has_verified_rework_delta(
        _predecessor(large_episode, descriptor), authority_repo=large_episode.repo,
    )
    target = tmp_path / "successor"
    target.mkdir()
    with pytest.raises(worker_workspace.WorkspaceError, match="rework_delta_blob_unsafe"):
        _materialize(large_episode, descriptor, target)
    assert list(target.iterdir()) == []


def test_a_file_changed_after_capture_fails_closed_at_seal(large_episode):
    entries = recovery.capture_candidate_paths(large_episode.workspace, [LARGE])
    _tamper(large_episode.workspace / LARGE)
    deltas = large_episode.runtime / "rework_deltas"
    with pytest.raises(worker_workspace.WorkspaceError, match="rework_delta_blob_source_changed"):
        worker_workspace.seal_rework_delta_artifact(
            large_episode.repo, large_episode.task_id, large_episode.request_id, CLAIM_EPOCH, entries, deltas,
        )
    assert list(deltas.glob("*.json")) == []
    assert list((deltas / worker_workspace.REWORK_DELTA_BLOB_DIRNAME).iterdir()) == []


def test_a_changed_file_fails_closed_at_recovery(large_episode, require_directory_authority):
    require_directory_authority(large_episode.workspace)
    _tamper(large_episode.workspace / LARGE)
    with pytest.raises(recovery.SuccessfulReworkRecoveryError, match="successful_rework_hash_mismatch"):
        _recover(large_episode)


def test_resealing_reuses_the_same_content_addressed_blob(large_episode):
    _candidate(large_episode)
    first = _blob(large_episode).stat()
    _paths, _hashes, descriptor = _candidate(large_episode)
    blobs = list(_blob(large_episode).parent.iterdir())
    assert blobs == [_blob(large_episode)]
    assert _blob(large_episode).stat().st_size == first.st_size == LARGE_BYTES
    assert worker_workspace.has_verified_rework_delta(
        _predecessor(large_episode, descriptor), authority_repo=large_episode.repo,
    )


def test_the_carried_cap_fails_closed_with_an_explicit_error(large_episode, monkeypatch):
    monkeypatch.setattr(worker_workspace, "MAX_REWORK_CARRIED_BYTES", LARGE_BYTES - 1)
    with pytest.raises(recovery.SuccessfulReworkRecoveryError, match="successful_rework_carried_bytes_exceeds_limit"):
        recovery.capture_candidate_paths(large_episode.workspace, [LARGE])
    carried = recovery.DigestCarriedContent(LARGE, large_episode.large_sha, LARGE_BYTES, large_episode.workspace)
    with pytest.raises(worker_workspace.WorkspaceError, match="rework_delta_carried_bytes_exceeds_limit"):
        worker_workspace.seal_rework_delta_artifact(
            large_episode.repo, large_episode.task_id, large_episode.request_id, CLAIM_EPOCH,
            [(LARGE, carried)], large_episode.runtime / "rework_deltas",
        )


def test_rebase_never_merges_a_blob_carried_path(large_episode, tmp_path):
    _paths, _hashes, descriptor = _candidate(large_episode)
    seen: list[list[str]] = []

    def drifted_large(planned):
        seen.append([relative for relative, _content in planned])
        assert all(not isinstance(content, worker_workspace.ReworkDeltaBlob) for _r, content in planned)
        return {relative: None for relative, _content in planned if relative == LARGE}

    target = tmp_path / "drifted"
    target.mkdir()
    with pytest.raises(worker_workspace.WorkspaceError, match=f"rework_delta_blob_rebase_conflict:{LARGE}"):
        _materialize(large_episode, descriptor, target, rebase=drifted_large)
    assert seen[0] == [SMALL]
    assert list(target.iterdir()) == []

    target = tmp_path / "undrifted"
    target.mkdir()
    assert _materialize(large_episode, descriptor, target, rebase=lambda planned: {}) == [LARGE, SMALL]
    assert _sha(target / LARGE) == large_episode.large_sha


def test_a_hard_linked_large_file_keeps_failing_closed(large_episode, tmp_path):
    try:
        os.link(large_episode.workspace / LARGE, tmp_path / "alias.jsonl")
    except OSError as exc:
        pytest.skip(f"hard links unsupported here: {exc}")
    with pytest.raises(recovery.SuccessfulReworkRecoveryError, match="successful_rework_path_unsafe"):
        recovery.capture_candidate_paths(large_episode.workspace, [LARGE])


def test_a_symlinked_large_file_keeps_failing_closed(large_episode, tmp_path):
    outside = tmp_path / "outside.jsonl"
    (large_episode.workspace / LARGE).replace(outside)
    try:
        (large_episode.workspace / LARGE).symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unsupported here: {exc}")
    with pytest.raises(recovery.SuccessfulReworkRecoveryError, match="successful_rework_path_unsafe"):
        recovery.capture_candidate_paths(large_episode.workspace, [LARGE])


def test_only_large_outputs_still_reach_a_sealed_descriptor(large_episode, tmp_path):
    (large_episode.workspace / SMALL).unlink()
    _paths, hashes, descriptor = _candidate(large_episode, [LARGE])
    assert hashes == {LARGE: large_episode.large_sha}
    assert descriptor["sealed"] is True
    target = tmp_path / "successor"
    target.mkdir()
    _materialize(large_episode, descriptor, target, hashes=hashes)
    assert _sha(target / LARGE) == large_episode.large_sha


def test_small_outputs_keep_todays_sealed_descriptor(large_episode):
    (large_episode.workspace / LARGE).unlink()
    _paths, _hashes, descriptor = _candidate(large_episode, [SMALL])
    assert set(descriptor) == {
        "schema_id", "sealed", "authority_repo", "task_id", "request_id", "claim_epoch",
        "artifact_path", "artifact_sha256",
    }
    assert descriptor["schema_id"] == "aiworkhub.rework_delta_descriptor.v1"
    assert descriptor["sealed"] is True
    packet = json.loads(Path(descriptor["artifact_path"]).read_text(encoding="utf-8"))
    assert packet["files"] == [{
        "path": SMALL, "sha256": large_episode.hashes[SMALL],
        "content_base64": "ZXhhY3QgY2FuZGlkYXRlCg==",
    }]
    assert not (large_episode.runtime / "rework_deltas" / worker_workspace.REWORK_DELTA_BLOB_DIRNAME).exists()
