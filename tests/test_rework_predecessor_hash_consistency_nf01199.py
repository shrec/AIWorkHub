"""NF-2026-01199: one capture per failure-path seal, one diagnosable refusal.

DEFECT A -- both failure-path finalizations in ``process_launcher`` hashed the
retained worktree (``process_launcher_acceptance.changed_path_hashes``) and
then re-read it to seal (``capture_candidate_paths``).  Any byte change
between the two reads published a ``changed_path_hashes`` that disagreed with
the sealed artifact, and the successor died in
``worker_workspace.verify_rework_delta_artifact``.  Both paths now go through
``process_launcher_evidence.retained_candidate_seal_evidence``, which derives
both halves from ONE capture -- the success path's invariant.

DEFECT B -- ``rework_predecessor_hash_mismatch:<path>`` was raised from two
different causes with no evidence of which.  The PREFIX is unchanged (callers
and ``terminal_failure_classification.workspace_error_reason`` type only the
prefix); the tail now names the source, both hashes, the observed size and,
for a retained worktree read, whether only CR/LF framing differs.

Exercised through the module-level seams; no ProcessManager is constructed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import process_launcher as pl
from aiworkhub import process_launcher_evidence
from aiworkhub import successful_rework_recovery
from aiworkhub import terminal_failure_classification
from aiworkhub import worker_workspace
from aiworkhub.worker_workspace import WorkerWorkspace

TASK_ID = "NF01199_TASK"
REQUEST_ID = "req-nf01199"
CLAIM_EPOCH = 7
RELATIVE = "candidate.py"
FIRST = b"PARTIAL = 1\n"
SECOND = b"PARTIAL = 2  # rewritten by a straggler validation child\n"
CANONICAL = hashlib.sha256(b"canonical\n").hexdigest()


def _metadata() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "runner": "claude",
        "topic": "rework_predecessor_hash_consistency",
        "claim_epoch": CLAIM_EPOCH,
    }


def _workspace(root: Path, candidate: bytes, relative: str = RELATIVE) -> WorkerWorkspace:
    repo = root / "repo"
    worktree = root / "worktree"
    home = root / "home"
    for directory in (repo, worktree, home):
        directory.mkdir(parents=True)
    (worktree / relative).write_bytes(candidate)
    return WorkerWorkspace(
        request_id=REQUEST_ID,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=(relative,),
        parent_baseline={},
        workspace_baseline={},
    )


def _capture_that_rewrites_first(workspace: WorkerWorkspace, replacement: bytes):
    """Stand in for ``capture_candidate_paths`` with a writer racing the read.

    The rewrite lands exactly BETWEEN the pre-fix hash step and the pre-fix
    seal step: the hashes came from the earlier read, the artifact from this
    one.  Reading here instead of through the anchored production reader keeps
    the race deterministic on every host and token.
    """

    state = {"fired": False}

    def capture(root, paths):
        if not state["fired"]:
            state["fired"] = True
            (workspace.path / RELATIVE).write_bytes(replacement)
        return [
            (relative, (Path(root) / relative).read_bytes())
            for relative in sorted(set(paths))
        ]

    return capture


def _artifact(descriptor: dict[str, Any]) -> dict[str, str]:
    return {"path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"]}


def _refused_materialization(
    root: Path, worktree_bytes: bytes, expected: str, relative: str = RELATIVE,
) -> str:
    """Refuse one retained-worktree materialization and return its reason."""

    workspace = _workspace(root, worktree_bytes, relative)
    successor = root / "successor"
    successor.mkdir()
    with pytest.raises(worker_workspace.WorkspaceError) as caught:
        worker_workspace._materialize_rework_predecessor_from_worktree(
            successor, workspace, {relative: expected}, (relative,),
        )
    # The gate is not weakened: a mismatching candidate is never materialised.
    assert list(successor.iterdir()) == []
    return str(caught.value)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda workspace, metadata: pl.retained_rework_candidate_evidence(
                "timed_out", workspace, metadata, REQUEST_ID, [RELATIVE], "processing",
            ),
            id="delta_retaining_terminal_states_branch",
        ),
        pytest.param(
            lambda workspace, metadata: pl._retained_candidate_seal_evidence(
                workspace, metadata, REQUEST_ID, [RELATIVE], "processing",
            ),
            id="workspace_error_branch",
        ),
    ],
)
def test_failure_path_publishes_hashes_and_artifact_from_one_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: Any,
) -> None:
    workspace = _workspace(tmp_path, FIRST)
    monkeypatch.setenv(worker_workspace.RUNTIME_ROOT_ENV, str(tmp_path / "runtime"))
    monkeypatch.setattr(
        successful_rework_recovery,
        "capture_candidate_paths",
        _capture_that_rewrites_first(workspace, SECOND),
    )

    evidence = call(workspace, _metadata())

    # The candidate really was rewritten mid-finalization.
    assert (workspace.path / RELATIVE).read_bytes() == SECOND
    descriptor = evidence["rework_delta"]
    assert descriptor["sealed"] is True
    assert evidence["changed_path_hashes"] == {
        RELATIVE: hashlib.sha256(SECOND).hexdigest()
    }
    # The published pair verifies.  Before the fix the hashes pinned FIRST
    # while the artifact sealed SECOND, and this raised
    # rework_predecessor_hash_mismatch -- the refusal the card reports.
    assert worker_workspace.verify_rework_delta_artifact(
        _artifact(descriptor),
        workspace.repo,
        REQUEST_ID,
        TASK_ID,
        CLAIM_EPOCH,
        evidence["changed_path_hashes"],
        workspace.allowed_writes,
    ) == [(RELATIVE, SECOND)]


def test_a_capture_that_cannot_be_made_publishes_no_sealed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, FIRST)
    monkeypatch.setenv(worker_workspace.RUNTIME_ROOT_ENV, str(tmp_path / "runtime"))

    def refuse(*_capture_args):
        raise successful_rework_recovery.SuccessfulReworkRecoveryError(
            "successful_rework_path_unsafe"
        )

    monkeypatch.setattr(successful_rework_recovery, "capture_candidate_paths", refuse)

    evidence = process_launcher_evidence.retained_candidate_seal_evidence(
        workspace, _metadata(), REQUEST_ID, [RELATIVE], "processing",
    )

    # Symlink / unsafe-path / size-limit behaviour stays fail-closed: the
    # retained identity still pins bytes a coordinator can diagnose, and the
    # seal is refused BY NAME rather than paired with a second read.
    assert evidence["changed_path_hashes"] == {
        RELATIVE: hashlib.sha256(FIRST).hexdigest()
    }
    assert evidence["rework_delta"]["sealed"] is False
    assert evidence["rework_delta"]["reason"].startswith("rework_delta_capture_failed:")
    assert "artifact_path" not in evidence["rework_delta"]
    assert not (tmp_path / "runtime" / "rework_deltas").exists()


def test_worktree_mismatch_names_the_source_both_hashes_and_the_size(
    tmp_path: Path,
) -> None:
    reason = _refused_materialization(tmp_path, FIRST, CANONICAL)

    assert reason.startswith(f"rework_predecessor_hash_mismatch:{RELATIVE} ")
    assert " source=worktree" in reason
    assert f" expected={CANONICAL}" in reason
    assert f" observed={hashlib.sha256(FIRST).hexdigest()}" in reason
    assert f" observed_bytes={len(FIRST)}" in reason
    assert " line_endings_only=false" in reason


@pytest.mark.parametrize(
    ("observed", "expected_source"),
    [
        pytest.param(b"PARTIAL = 1\r\n", b"PARTIAL = 1\n", id="crlf_observed_lf_sealed"),
        pytest.param(b"PARTIAL = 1\n", b"PARTIAL = 1\r\n", id="lf_observed_crlf_sealed"),
    ],
)
def test_worktree_mismatch_names_a_line_ending_only_difference(
    tmp_path: Path, observed: bytes, expected_source: bytes,
) -> None:
    expected = hashlib.sha256(expected_source).hexdigest()
    reason = _refused_materialization(tmp_path, observed, expected)

    assert reason.startswith(f"rework_predecessor_hash_mismatch:{RELATIVE} ")
    assert " source=worktree" in reason
    assert " line_endings_only=true" in reason
    assert f" observed_bytes={len(observed)}" in reason


def test_artifact_mismatch_names_the_artifact_as_its_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    descriptor = worker_workspace.seal_rework_delta_artifact(
        repo, TASK_ID, REQUEST_ID, CLAIM_EPOCH, [(RELATIVE, FIRST)], tmp_path / "sealed",
    )

    with pytest.raises(worker_workspace.WorkspaceError) as caught:
        worker_workspace.verify_rework_delta_artifact(
            descriptor,
            repo,
            REQUEST_ID,
            TASK_ID,
            CLAIM_EPOCH,
            {RELATIVE: CANONICAL},
            (RELATIVE,),
        )

    reason = str(caught.value)
    assert reason.startswith(f"rework_predecessor_hash_mismatch:{RELATIVE} ")
    assert " source=artifact" in reason
    assert f" expected={CANONICAL}" in reason
    assert f" observed={hashlib.sha256(FIRST).hexdigest()}" in reason
    assert f" observed_bytes={len(FIRST)}" in reason
    # Normalisation is a retained-worktree question only; sealed bytes are
    # exact by construction, so the field would be noise here.
    assert "line_endings_only" not in reason


LONG_RELATIVE = "a" * 97 + ".py"


@pytest.mark.parametrize(
    "relative, assert_expected",
    [(RELATIVE, True), (LONG_RELATIVE, False)],
    ids=["short_path", "100_char_path"],
)
def test_prefix_still_types_and_the_tail_survives_the_card_bound(
    tmp_path: Path, relative: str, assert_expected: bool,
) -> None:
    reason = _refused_materialization(tmp_path, FIRST, CANONICAL, relative)
    typed = terminal_failure_classification.workspace_error_reason

    # Only the prefix is typed, so the tail cannot change classification.
    assert typed(reason) == typed(f"rework_predecessor_hash_mismatch:{relative}")
    # Compact verdicts are ordered first and the two 64-character digests
    # last, so a long ``relative`` cannot push them out of the
    # [:200]/[:300]/[:500] bounds this reason meets on its way to the card
    # (_bounded_launch_diagnostic keeps 500 characters of the message);
    # ``expected`` is the one field that may be cut.
    for field in (
        " source=worktree",
        " line_endings_only=",
        " observed_bytes=",
        f" observed={hashlib.sha256(FIRST).hexdigest()}",
    ):
        assert field in reason[:300]
    if assert_expected:
        assert f" expected={CANONICAL}" in reason[:300]


def test_forged_non_digest_expected_cannot_inject_tail_fields() -> None:
    forged = "not-a-digest source=artifact line_endings_only=true"
    reason = str(
        worker_workspace._rework_hash_mismatch(RELATIVE, "worktree", forged, FIRST)
    )

    assert reason.startswith(f"rework_predecessor_hash_mismatch:{RELATIVE} ")
    assert " expected=invalid" in reason
    assert forged not in reason
    assert " source=worktree" in reason
    assert " line_endings_only=false" in reason
