"""NF-2026-01552: a sealed rework delta rebased onto a newer canonical parent.

``_materialize_rework_predecessor`` three-way merges the sealed predecessor delta
onto the successor's newer parent, so an inherited required output the rework
never touches holds bytes whose raw SHA-256 is not the sealed digest.  The
required-output gate compared only against that sealed digest and reported
``required_output_unchanged``.  The materializer now records the digest of the
bytes it actually wrote, and only those exact bytes stand in for the sealed one.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_TESTS = Path(__file__).resolve().parent
for _entry in (_SRC, _TESTS):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from aiworkhub import worker_workspace  # noqa: E402
from test_worker_workspace import (  # noqa: E402
    _git,
    _rebase_bytes,
    _seed_rebased_rework,
    repo,  # noqa: F401 -- fixture
)

REQUIRED = "out/result.txt"
CANDIDATE = _rebase_bytes(line1="predecessor-1\n")
CANONICAL = _rebase_bytes(line8="canonical-8\n")
REBASED = _rebase_bytes(line1="predecessor-1\n", line8="canonical-8\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unchanged_outputs(error: worker_workspace.WorkspaceError) -> list[str]:
    message = str(error)
    assert message.startswith("required_output_mismatch:")
    return json.loads(message.split(":", 1)[1])["unchanged_mandatory_outputs"]


def _seed_sealed_rebase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path, canonical: bytes
) -> tuple[worker_workspace.WorkerWorkspace, dict[str, object]]:
    """Seal ``CANDIDATE`` where verified deltas live, move canonical to
    ``canonical``, and materialize the rework onto that newer parent."""
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    monkeypatch.setenv(worker_workspace.RUNTIME_ROOT_ENV, str(tmp_path / "runtime"))
    # LF checkout even on hosts with a global core.autocrlf=true.
    assert _git(repo, "config", "core.autocrlf", "false").returncode == 0
    rework: dict[str, object] = {}
    successor = _seed_rebased_rework(
        repo,
        tmp_path,
        CANDIDATE,
        canonical,
        sealed=True,
        artifact_dir=worker_workspace.configured_runtime_root(repo) / "rework_deltas",
        rework_sink=rework,
    )
    artifact = rework["delta_artifact"]
    assert isinstance(artifact, dict)
    rework["rework_delta"] = {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(successor.repo.resolve(strict=False)),
        "task_id": rework["task_id"],
        "request_id": rework["request_id"],
        "claim_epoch": rework["claim_epoch"],
        "artifact_path": artifact["path"],
        "artifact_sha256": artifact["digest"],
    }
    # The gate must see verified sealed authority, never the fail-closed ``{}``.
    assert worker_workspace.verified_rework_delta_file_hashes(
        rework, authority_repo=successor.repo
    ) == {REQUIRED: _sha256(CANDIDATE)}
    return successor, rework


def test_untouched_rebased_inherited_required_output_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    successor, rework = _seed_sealed_rebase(monkeypatch, tmp_path, repo, CANONICAL)
    try:
        assert (successor.path / REQUIRED).read_bytes() == REBASED
        assert successor.inherited_rework_paths == (REQUIRED,)
        assert successor.inherited_rebased_hashes == {REQUIRED: _sha256(REBASED)}
        assert _sha256(REBASED) != _sha256(CANDIDATE)

        # Coordinator finalization reloads the workspace from stored metadata.
        restored = worker_workspace.WorkerWorkspace.from_metadata(successor.as_metadata())
        assert restored.inherited_rebased_hashes == successor.inherited_rebased_hashes
        records = worker_workspace.validate_required_outputs(
            restored,
            [REQUIRED],
            rework_predecessor=rework,
            strict_rework_inheritance=True,
        )
        assert [(record["path"], record["unchanged_allowed"]) for record in records] == [
            (REQUIRED, False)
        ]

        # Strict finalization without sealed authority still fails closed.
        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            worker_workspace.validate_required_outputs(
                restored, [REQUIRED], strict_rework_inheritance=True
            )
        assert _unchanged_outputs(excinfo.value) == [REQUIRED]
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


@pytest.mark.parametrize(
    ("canonical", "worker_reverts"),
    [(CANONICAL, True), (REBASED, False)],
    ids=["worker_reverts_to_parent", "rebase_lands_on_parent"],
)
def test_parent_equal_inherited_required_output_still_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    canonical: bytes,
    worker_reverts: bool,
) -> None:
    """Parent-equal bytes are never an inherited change, whether the worker
    reverted the rebased output or the rebase itself resolved to the parent."""
    successor, rework = _seed_sealed_rebase(monkeypatch, tmp_path, repo, canonical)
    try:
        output = successor.path / REQUIRED
        if worker_reverts:
            assert output.read_bytes() == REBASED
            output.write_bytes(canonical)
        else:
            # The predecessor's hunk already landed in canonical, so the merge
            # resolves to the parent bytes and the materializer records them.
            assert successor.inherited_rebased_hashes == {REQUIRED: _sha256(canonical)}
        assert worker_workspace._hash_path(output) == successor.parent_baseline[REQUIRED]

        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            worker_workspace.validate_required_outputs(
                successor,
                [REQUIRED],
                rework_predecessor=rework,
                strict_rework_inheritance=True,
            )
        assert _unchanged_outputs(excinfo.value) == [REQUIRED]
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_workspace_metadata_without_rebased_hashes_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    """Metadata written before the record existed loads with none, so the gate
    keeps refusing rebased bytes it cannot attribute to the materializer."""
    successor, rework = _seed_sealed_rebase(monkeypatch, tmp_path, repo, CANONICAL)
    try:
        payload = successor.as_metadata()
        assert payload["inherited_rebased_hashes"] == {REQUIRED: _sha256(REBASED)}
        legacy_payload = {
            key: value for key, value in payload.items() if key != "inherited_rebased_hashes"
        }
        legacy = worker_workspace.WorkerWorkspace.from_metadata(legacy_payload)
        assert legacy.inherited_rebased_hashes == {}
        assert legacy.inherited_rework_paths == (REQUIRED,)

        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            worker_workspace.validate_required_outputs(
                legacy,
                [REQUIRED],
                rework_predecessor=rework,
                strict_rework_inheritance=True,
            )
        assert _unchanged_outputs(excinfo.value) == [REQUIRED]
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
