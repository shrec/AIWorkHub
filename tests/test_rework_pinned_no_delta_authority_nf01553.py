"""NF-2026-01553: strict rework credits the hash-pinned bytes provisioning wrote.

A predecessor pinned without a sealed ``rework_delta`` still has its
``changed_path_hashes`` hash-pinned and verified by
``_materialize_rework_predecessor`` before it overlays those paths into the
successor.  Under strict inheritance the required-output gate used to fall to
``{}`` and reported every untouched inherited output as unchanged.  It now
credits those pinned digests, but only for ``workspace.inherited_rework_paths``.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_TESTS = Path(__file__).resolve().parent
for _entry in (_SRC, _TESTS):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from aiworkhub import worker_workspace  # noqa: E402
from test_rework_rebased_required_output_nf01552 import (  # noqa: E402
    CANDIDATE,
    REQUIRED,
    _seed_sealed_rebase,
    _sha256,
    _unchanged_outputs,
)
from test_worker_workspace import (  # noqa: E402
    _git,
    _rebase_bytes,
    _seed_rebased_rework,
    repo,  # noqa: F401 -- fixture
)


def _seed_pinned_no_delta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> tuple[worker_workspace.WorkerWorkspace, dict[str, object]]:
    """Seed a successor from a predecessor that has no sealed delta artifact."""
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    monkeypatch.setenv(worker_workspace.RUNTIME_ROOT_ENV, str(tmp_path / "runtime"))
    assert _git(repo, "config", "core.autocrlf", "false").returncode == 0
    rework: dict[str, object] = {}
    successor = _seed_rebased_rework(
        repo, tmp_path, CANDIDATE, None, sealed=False, rework_sink=rework
    )
    assert "delta_artifact" not in rework
    assert worker_workspace.verified_rework_delta_file_hashes(
        rework, authority_repo=successor.repo
    ) is None
    return successor, rework


def _strict(
    workspace: worker_workspace.WorkerWorkspace, rework: dict[str, object]
) -> list[dict[str, object]]:
    return worker_workspace.validate_required_outputs(
        workspace,
        [REQUIRED],
        rework_predecessor=rework,
        strict_rework_inheritance=True,
    )


def test_pinned_no_delta_untouched_inherited_output_is_credited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    successor, rework = _seed_pinned_no_delta(monkeypatch, tmp_path, repo)
    try:
        assert (successor.path / REQUIRED).read_bytes() == CANDIDATE
        assert successor.inherited_rework_paths == (REQUIRED,)
        assert _sha256(CANDIDATE) != _sha256(_rebase_bytes())

        restored = worker_workspace.WorkerWorkspace.from_metadata(successor.as_metadata())
        records = _strict(restored, rework)
        assert [(record["path"], record["unchanged_allowed"]) for record in records] == [
            (REQUIRED, False)
        ]

        # Without the predecessor claimed there is still no authority at all.
        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            worker_workspace.validate_required_outputs(
                restored, [REQUIRED], strict_rework_inheritance=True
            )
        assert _unchanged_outputs(excinfo.value) == [REQUIRED]

        # A malformed pin never earns credit, and one malformed entry voids the
        # whole pin -- also for a rebased path, whose pin value the credit gate
        # never compares against the bytes on disk.
        rebased_payload = restored.as_metadata()
        rebased_payload["inherited_rebased_hashes"] = {REQUIRED: _sha256(CANDIDATE)}
        rebased = worker_workspace.WorkerWorkspace.from_metadata(rebased_payload)
        valid = _sha256(CANDIDATE)
        for malformed in (
            None,
            "not-a-dict",
            {REQUIRED: 7},
            {REQUIRED: ""},
            {REQUIRED: "x"},
            {REQUIRED: valid.upper()},
            {REQUIRED: valid, "src/other.py": "x"},
        ):
            broken = {**rework, "changed_path_hashes": malformed}
            for workspace in (restored, rebased):
                with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
                    _strict(workspace, broken)
                assert _unchanged_outputs(excinfo.value) == [REQUIRED]
        # A pinned digest that is not the bytes on disk never earns credit.
        wrong = {**rework, "changed_path_hashes": {REQUIRED: _sha256(b"other\n")}}
        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            _strict(restored, wrong)
        assert _unchanged_outputs(excinfo.value) == [REQUIRED]
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_pinned_path_outside_inherited_rework_paths_is_not_credited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    successor, rework = _seed_pinned_no_delta(monkeypatch, tmp_path, repo)
    try:
        payload = successor.as_metadata()
        payload["inherited_rework_paths"] = []
        not_inherited = worker_workspace.WorkerWorkspace.from_metadata(payload)
        assert not_inherited.inherited_rework_paths == ()
        assert rework["changed_path_hashes"] == {REQUIRED: _sha256(CANDIDATE)}

        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            _strict(not_inherited, rework)
        assert _unchanged_outputs(excinfo.value) == [REQUIRED]
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_pinned_path_equal_to_parent_baseline_is_not_credited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    successor, rework = _seed_pinned_no_delta(monkeypatch, tmp_path, repo)
    try:
        payload = successor.as_metadata()
        payload["parent_baseline"] = {
            **payload["parent_baseline"],
            REQUIRED: payload["workspace_baseline"][REQUIRED],
        }
        parent_equal = worker_workspace.WorkerWorkspace.from_metadata(payload)
        output = parent_equal.path / REQUIRED
        assert output.read_bytes() == CANDIDATE
        assert worker_workspace._hash_path(output) == parent_equal.parent_baseline[REQUIRED]
        assert hashlib.sha256(output.read_bytes()).hexdigest() == rework[
            "changed_path_hashes"
        ][REQUIRED]

        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            _strict(parent_equal, rework)
        assert _unchanged_outputs(excinfo.value) == [REQUIRED]
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_sealed_delta_still_wins_over_pinned_hashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    successor, rework = _seed_sealed_rebase(monkeypatch, tmp_path, repo, None)
    try:
        assert (successor.path / REQUIRED).read_bytes() == CANDIDATE
        restored = worker_workspace.WorkerWorkspace.from_metadata(successor.as_metadata())
        assert [record["path"] for record in _strict(restored, rework)] == [REQUIRED]

        # The sealed digest is authoritative: a conflicting pin cannot override it.
        conflicting = {**rework, "changed_path_hashes": {REQUIRED: _sha256(b"other\n")}}
        assert [record["path"] for record in _strict(restored, conflicting)] == [REQUIRED]

        # A claimed sealed delta that fails verification yields {} (not None),
        # so a valid pin can never downgrade it into the pinned-no-delta credit.
        epoch = rework["claim_epoch"]
        unverifiable = {
            **rework,
            "claim_epoch": (epoch if isinstance(epoch, int) else 0) + 1,
            "changed_path_hashes": {REQUIRED: _sha256(CANDIDATE)},
        }
        assert worker_workspace.verified_rework_delta_file_hashes(
            unverifiable, authority_repo=restored.repo
        ) == {}
        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            _strict(restored, unverifiable)
        assert _unchanged_outputs(excinfo.value) == [REQUIRED]
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
