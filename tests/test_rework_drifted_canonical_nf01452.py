"""NF-2026-01452: a rework accept must keep canonical hunks accepted after it.

A rework worktree is seeded with the predecessor's whole-file bytes while the
recorded merge base came from the CURRENT canonical tree; an uncommitted accept
never moves ``base_oid``, so the candidate diff "removed" the later hunk and
promotion applied it.  These tests pin the predecessor's own parent baseline as
the merge ancestor, the seed-time merge used when that blob is unavailable (only
on an ancestor that hashes to the recorded digest), and the named refusal.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from aiworkhub import worker_workspace
from aiworkhub.promotion_merge import (
    promote_merged,
    rework_pinned_parent_baseline,
)

_LINES = tuple(f"line-{index}\n" for index in range(1, 11))
_TARGET = "out/result.txt"


def _content(**replaced: str) -> bytes:
    """The ten-line file with the named one-based lines replaced."""

    lines = list(_LINES)
    for key, value in replaced.items():
        lines[int(key.removeprefix("line")) - 1] = value
    return "".join(lines).encode("utf-8")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One committed single-file repository with worker worktrees outside it."""

    root = tmp_path / "canonical"
    (root / "out").mkdir(parents=True)
    for argv in (
        ("init", "-q"),
        ("config", "user.email", "nf01452@example.invalid"),
        ("config", "user.name", "NF-2026-01452"),
        ("config", "commit.gpgsign", "false"),
        # LF checkout even on hosts with a global core.autocrlf=true.
        ("config", "core.autocrlf", "false"),
    ):
        assert _git(root, *argv).returncode == 0
    (root / "out" / "result.txt").write_bytes(_content())
    assert _git(root, "add", _TARGET).returncode == 0
    assert _git(root, "commit", "-q", "-m", "canonical base").returncode == 0
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    return root.resolve()


def _seed_rework(
    repo: Path,
    *,
    candidate: bytes,
    drifted: bytes,
    blob_record: dict[str, str] | None = None,
    commit: bool = False,
) -> worker_workspace.WorkerWorkspace:
    """Provision a rework successor after a later canonical accept.

    Without ``commit`` HEAD never moves, so no base-drift rebase runs -- the
    NF-2026-01452 scenario; ``blob_record`` replaces the recorded blobs.
    """

    predecessor = worker_workspace.create_workspace(
        repo,
        "nf01452-predecessor",
        {"allowed_writes": [_TARGET]},
        "validation",
    )
    try:
        (predecessor.path / "out" / "result.txt").write_bytes(candidate)
        metadata = predecessor.as_metadata()
        # Not vacuous: the predecessor really did launch against the base bytes.
        assert metadata["parent_baseline"][_TARGET].endswith(_digest(_content()))
        if blob_record is not None:
            metadata["parent_baseline_blob"] = blob_record
        (repo / "out" / "result.txt").write_bytes(drifted)
        if commit:
            assert _git(repo, "add", _TARGET).returncode == 0
            assert _git(repo, "commit", "-q", "-m", "later accept").returncode == 0
        return worker_workspace.create_workspace(
            repo,
            "nf01452-successor",
            {
                "allowed_writes": [_TARGET],
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "nf01452-predecessor",
                    "task_id": "nf01452",
                    "claim_epoch": 1,
                    "workspace": metadata,
                    "changed_path_hashes": {_TARGET: _digest(candidate)},
                },
            },
            "validation",
        )
    finally:
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_rework_accept_keeps_the_canonical_hunk_accepted_after_the_predecessor(
    repo: Path,
) -> None:
    """The measured defect: promotion reverted the later accept's hunk."""

    candidate = _content(line1="predecessor-1\n")  # hunk X
    drifted = _content(line8="canonical-8\n")  # hunk Y, accepted afterwards
    successor = _seed_rework(repo, candidate=candidate, drifted=drifted)
    try:
        # The seeded worktree is still exactly the predecessor's bytes ...
        assert (successor.path / "out" / "result.txt").read_bytes() == candidate
        # ... but the recorded merge ancestor is the PREDECESSOR's parent, not
        # the drifted canonical file the successor was provisioned over.
        assert successor.parent_baseline[_TARGET].endswith(_digest(_content()))
        assert successor.parent_baseline_blob[_TARGET]

        promoted = promote_merged(successor, [_TARGET])

        assert promoted == [_TARGET]
        assert (repo / "out" / "result.txt").read_bytes() == _content(
            line1="predecessor-1\n", line8="canonical-8\n"
        )
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_rework_without_canonical_drift_promotes_the_candidate_unchanged(
    repo: Path,
) -> None:
    """An undrifted canonical path keeps exactly its pre-fix recording."""

    candidate = _content(line1="predecessor-1\n")
    successor = _seed_rework(repo, candidate=candidate, drifted=_content())
    try:
        canonical = (repo / "out" / "result.txt").read_bytes()
        assert successor.parent_baseline[_TARGET].endswith(_digest(canonical))

        assert promote_merged(successor, [_TARGET]) == [_TARGET]
        assert (repo / "out" / "result.txt").read_bytes() == candidate
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_unpinnable_drift_merges_at_seed_time_and_promotes_both_hunks(
    repo: Path,
) -> None:
    """With no recorded blob the merge cannot be deferred, so it happens now."""

    candidate = _content(line1="predecessor-1\n")
    drifted = _content(line8="canonical-8\n")
    merged = _content(line1="predecessor-1\n", line8="canonical-8\n")
    successor = _seed_rework(
        repo, candidate=candidate, drifted=drifted, blob_record={}
    )
    try:
        assert (successor.path / "out" / "result.txt").read_bytes() == merged

        assert promote_merged(successor, [_TARGET]) == [_TARGET]
        assert (repo / "out" / "result.txt").read_bytes() == merged
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_unpinnable_drift_on_the_same_hunk_refuses_provisioning(repo: Path) -> None:
    """Predecessor bytes are never silently preferred over drifted canonical."""

    candidate = _content(line5="predecessor-5\n")
    drifted = _content(line5="canonical-5\n")  # the same line: unmergeable
    with pytest.raises(worker_workspace.WorkspaceError) as exc:
        _seed_rework(repo, candidate=candidate, drifted=drifted, blob_record={})
    assert f"rework_canonical_drift_conflict:{_TARGET}" in str(exc.value)


def test_committed_canonical_drift_still_promotes_both_hunks(repo: Path) -> None:
    """The pin leaves NF-2026-01113's committed-drift rebase working."""

    candidate = _content(line1="predecessor-1\n")
    drifted = _content(line8="canonical-8\n")
    merged = _content(line1="predecessor-1\n", line8="canonical-8\n")
    successor = _seed_rework(
        repo, candidate=candidate, drifted=drifted, commit=True
    )
    try:
        assert (successor.path / "out" / "result.txt").read_bytes() == merged

        assert promote_merged(successor, [_TARGET]) == [_TARGET]
        assert (repo / "out" / "result.txt").read_bytes() == merged
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def _unpinnable(
    successor: worker_workspace.WorkerWorkspace, digest: str
) -> worker_workspace.WorkerWorkspace:
    """A predecessor source recording ``digest`` and no blob for the target.

    An empty ``parent_baseline_blob`` leaves the path unpinnable, so
    ``rework_pinned_parent_baseline`` must merge at seed time against the
    ``base_oid`` blob -- which is the predecessor's real ancestor only while it
    hashes to the recorded parent digest.
    """

    recorded = successor.parent_baseline[_TARGET]
    return replace(
        successor,
        parent_baseline={_TARGET: f"{recorded.rsplit(':', 1)[0]}:{digest}"},
        parent_baseline_blob={},
    )


def _canonical(repo: Path) -> dict[str, str | None]:
    return {_TARGET: worker_workspace._hash_path(repo / "out" / "result.txt")}


def test_seed_merge_refuses_an_ancestor_that_is_not_the_recorded_parent(
    repo: Path,
) -> None:
    """An unverifiable ancestor refuses instead of merging on the wrong base.

    The uncommitted accept left ``base_oid`` on the predecessor's launch commit,
    but here the predecessor recorded the candidate bytes as its own parent --
    the ancestor and the committed blob disagree.  Merging ours=canonical,
    base=that blob, theirs=candidate would take "theirs" and resurrect a hunk
    canonical had dropped, so the mismatch is named rather than merged.
    """

    candidate = _content(line1="predecessor-1\n")
    drifted = _content(line8="canonical-8\n")
    successor = _seed_rework(repo, candidate=candidate, drifted=drifted)
    try:
        seeded = successor.path / "out" / "result.txt"
        assert seeded.read_bytes() == candidate
        source = _unpinnable(successor, _digest(candidate))

        with pytest.raises(worker_workspace.WorkspaceError) as exc:
            rework_pinned_parent_baseline(
                repo, successor.path, source, [_TARGET], _canonical(repo)
            )

        assert f"rework_canonical_drift_conflict:{_TARGET}" in str(exc.value)
        # The refusal leaves the seeded bytes exactly as provisioned.
        assert seeded.read_bytes() == candidate
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_seed_merge_proceeds_on_the_recorded_parent_ancestor(repo: Path) -> None:
    """The matching digest is the positive control for the refusal above."""

    candidate = _content(line1="predecessor-1\n")
    drifted = _content(line8="canonical-8\n")
    successor = _seed_rework(repo, candidate=candidate, drifted=drifted)
    try:
        seeded = successor.path / "out" / "result.txt"
        # Not vacuous: the committed ``base_oid`` blob really is this ancestor.
        assert successor.parent_baseline[_TARGET].endswith(_digest(_content()))
        source = _unpinnable(successor, _digest(_content()))

        baseline, blobs = rework_pinned_parent_baseline(
            repo, successor.path, source, [_TARGET], _canonical(repo)
        )

        assert (baseline, blobs) == ({}, {})
        assert seeded.read_bytes() == _content(
            line1="predecessor-1\n", line8="canonical-8\n"
        )
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_seed_merge_records_the_merged_digest_for_sealed_credit(repo: Path) -> None:
    """A seed-time merge records its digest so sealed inherited output credits."""

    candidate = _content(line1="predecessor-1\n")
    drifted = _content(line8="canonical-8\n")
    successor = _seed_rework(repo, candidate=candidate, drifted=drifted)
    try:
        seeded = successor.path / "out" / "result.txt"
        assert successor.parent_baseline[_TARGET].endswith(_digest(_content()))
        source = _unpinnable(successor, _digest(_content()))
        recorded: dict[str, str] = {}

        baseline, blobs = rework_pinned_parent_baseline(
            repo,
            successor.path,
            source,
            [_TARGET],
            _canonical(repo),
            rebased_hashes=recorded,
        )

        assert (baseline, blobs) == ({}, {})
        assert seeded.read_bytes() != candidate
        assert recorded == {_TARGET: hashlib.sha256(seeded.read_bytes()).hexdigest()}
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
