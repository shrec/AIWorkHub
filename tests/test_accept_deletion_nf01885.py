"""NF-2026-01885: a card whose sealed delta deletes a tracked file is accepted.

The chain under test is the production one, run for real over a temporary
repository and isolated workspace: seal the delta (``changed_path_hashes``),
audit it (``review_evidence_audit``), promote it (``worker_workspace.promote``)
and build the post-promotion identity (``accepted_outcome_receipt``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import evidence_instruments, process_launcher_acceptance as acceptance
from aiworkhub import worker_workspace as ww

DOOMED = "doomed.txt"
KEPT = "kept.txt"
BASE_OID = "a" * 40
MANIFEST = {"schema_id": "aiworkhub.attempt_artifact_manifest.v1", "entries": []}


def _env(tmp_path: Path) -> tuple[Path, ww.WorkerWorkspace]:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    for root in (repo, worktree):
        (root / DOOMED).write_bytes(b"to be deleted\n")
        (root / KEPT).write_bytes(b"untouched\n")
    baseline = {name: ww._hash_path(repo / name) for name in (DOOMED, KEPT)}
    workspace = ww.WorkerWorkspace(
        request_id="req-nf01885",
        repo=repo,
        path=worktree,
        home=tmp_path / "home",
        allowed_writes=(DOOMED,),
        parent_baseline=baseline,
        workspace_baseline=dict(baseline),
        base_oid=BASE_OID,
    )
    return repo, workspace


def _audit(workspace: ww.WorkerWorkspace, hashes: dict) -> dict:
    return evidence_instruments.review_evidence_audit(
        workspace.repo,
        workspace.path,
        changed_paths=[DOOMED],
        stored_hashes=hashes,
        required_outputs=[],
    )


def test_delete_only_delta_is_accepted_and_file_leaves_canonical_tree(
    tmp_path: Path,
) -> None:
    repo, workspace = _env(tmp_path)
    (workspace.path / DOOMED).unlink()

    hashes = acceptance.changed_path_hashes(workspace, [DOOMED])
    assert hashes == {DOOMED: None}

    audit = _audit(workspace, hashes)
    assert audit["blocking"] is False, audit["blockers"]
    assert audit["changed_paths_verified"] == [{"path": DOOMED, "deleted": True}]

    promoted = ww.promote(workspace, [DOOMED])
    assert promoted == [DOOMED]
    assert not (repo / DOOMED).exists()
    assert (repo / KEPT).read_bytes() == b"untouched\n"

    receipt = acceptance.accepted_outcome_receipt(
        repo,
        task_id="NF-DEL",
        request_id=workspace.request_id,
        claim_epoch=1,
        base_oid=BASE_OID,
        promoted_paths=promoted,
        changed_path_hashes=hashes,
        attempt_artifact_manifest=MANIFEST,
    )
    assert receipt["promoted_paths"] == [DOOMED]
    assert receipt["changed_path_hashes"] == {DOOMED: None}


def test_stale_deletion_receipt_over_still_present_file_is_refused(
    tmp_path: Path,
) -> None:
    repo, workspace = _env(tmp_path)
    # The receipt claims a deletion, but the candidate worktree still holds the
    # file: nothing was actually deleted, so the audit must refuse.
    audit = _audit(workspace, {DOOMED: None})

    assert audit["blocking"] is True
    assert audit["blockers"] == [f"changed_path_hash_mismatch:{DOOMED}"]
    assert (repo / DOOMED).read_bytes() == b"to be deleted\n"


def test_real_hash_over_deleted_file_is_refused(tmp_path: Path) -> None:
    _repo, workspace = _env(tmp_path)
    (workspace.path / DOOMED).unlink()

    audit = _audit(workspace, {DOOMED: "b" * 64})

    assert audit["blocking"] is True
    assert audit["blockers"][0].startswith(f"changed_path_invalid:{DOOMED}:")


@pytest.mark.parametrize("stored", [{}, {KEPT: None}])
def test_deletion_without_its_own_none_receipt_is_refused(
    tmp_path: Path, stored: dict
) -> None:
    _repo, workspace = _env(tmp_path)
    (workspace.path / DOOMED).unlink()

    audit = _audit(workspace, stored)

    assert audit["blocking"] is True
