"""NF-2026-01518: accept a candidate that promotion three-way merged.

Measured (needfix-NF-2026-01502-r1): canonical 7e2ee6ab changed a changed path
after the worker launched, so ``promotion_merge.promote_merged``
(NF-2026-01381 A) wrote MERGED bytes. ``accepted_outcome_receipt`` then hashed
canonical against the sealed (or manager-corrected) digests and raised
``post_promotion_candidate_mismatch`` AFTER the write -- canonical dirty, card
blocked -- and ``task_engine._validate_accepted_outcome_receipt`` would have
refused the same bytes. Every accept with a drifted parent failed.

The fix is one more OPTIONAL receipt field, ``promotion_merge``, exactly like
NF-2026-01436's ``manager_correction``:

* (a) canonical merged bytes plus ``promotion_merge`` build a receipt the real
  validator accepts against the sealed ``terminal_review`` hashes;
* (b) the same with a manager correction on the merged path;
* (c) without ``promotion_merge`` the builder and the validator still refuse
  merged bytes exactly as before;
* (d) the validator refuses a wrong candidate digest and an unsealed path;
* (e) the real ``accept_review`` accepts a candidate whose canonical parent
  changed after launch, promoting through the production three-way merge, and
  canonical holds the merged bytes;
* a receipt failure after a correction restores the retained worktree and
  leaves canonical exactly as promoted.

The harnesses are NF-2026-01436's accept environment and NF-2026-01381's
three-way-merge workspace, imported rather than restated.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher,
    process_launcher_acceptance,
    promotion_merge,
    task_engine,
    task_store,
)

import test_accept_with_manager_correction_nf01436 as _nf01436  # noqa: E402
import test_promotion_three_way_merge as _merge_suite  # noqa: E402

TARGET = _nf01436.TARGET
TASK_ID = _nf01436.TASK_ID
REQUEST_ID = _nf01436.REQUEST_ID
CANDIDATE_DIGEST = _nf01436.CANDIDATE_DIGEST
CORRECTED_DIGEST = _nf01436.CORRECTED_DIGEST
UNSEALED = "src/demo/other.py"
# The three-way-merge suite's workspace declares exactly ``other.txt`` and
# ``shared.txt``; the end-to-end accepts below promote the latter.
MERGE_TARGET = "shared.txt"

# Canonical at launch, and the header canonical gained afterwards. The
# candidate appends below line 2 while canonical inserts above line 1, so the
# hunks are disjoint and ``git merge-file`` keeps both.
LAUNCH = "def widget():\n    return 1\n"
DRIFT = "# canonical changed this file after the worker launched\n"
MERGED = DRIFT + _nf01436.CANDIDATE
MERGED_CORRECTED = DRIFT + _nf01436.CORRECTED
MERGED_DIGEST = hashlib.sha256(MERGED.encode("utf-8")).hexdigest()
MERGED_CORRECTED_DIGEST = hashlib.sha256(MERGED_CORRECTED.encode("utf-8")).hexdigest()


def _merge(candidate: str, merged: str, path: str = TARGET) -> dict:
    """One ``promotion_merge`` object: ``path`` merged from ``candidate``."""
    return {
        "paths": [path],
        "candidate_path_hashes": {path: candidate},
        "merged_path_hashes": {path: merged},
    }


def _receipt(repo: Path, changed_digest: str, **optional: dict) -> dict:
    """The production receipt builder over one promoted ``TARGET``."""
    return process_launcher_acceptance.accepted_outcome_receipt(
        repo,
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        claim_epoch=_nf01436.CLAIM_EPOCH,
        base_oid=_nf01436.BASE_OID,
        promoted_paths=[TARGET],
        changed_path_hashes={TARGET: changed_digest},
        attempt_artifact_manifest=_nf01436._manifest(),
        **optional,
    )


def _validate(repo: Path, receipt: dict) -> tuple[dict | None, str]:
    """The real validator against NF-2026-01436's sealed terminal review."""
    return task_engine._validate_accepted_outcome_receipt(
        repo, _nf01436._receipt_card(repo), TASK_ID, REQUEST_ID, receipt
    )


def _sealed_correction(tmp_path: Path) -> dict:
    """NF-2026-01436's well-formed correction, exactly as its receipt seals it."""
    repo = _nf01436._promoted_repo(tmp_path, "nf01436-corrected", _nf01436.CORRECTED)
    return _nf01436._corrected_receipt(repo)["manager_correction"]


# ---------------------------------------------------------------------------
# (a) merged canonical bytes plus ``promotion_merge``: built, then validated.
# ---------------------------------------------------------------------------
def test_merged_receipt_is_built_and_validated_against_the_sealed_hashes(
    tmp_path: Path,
) -> None:
    repo = _nf01436._promoted_repo(tmp_path, "merged", MERGED)

    receipt = _receipt(
        repo, CANDIDATE_DIGEST, promotion_merge=_merge(CANDIDATE_DIGEST, MERGED_DIGEST)
    )

    # Canonical holds the MERGED bytes, so that is what the receipt binds; the
    # merge still names the sealed candidate digest the reviewers judged.
    assert receipt["changed_path_hashes"] == {TARGET: MERGED_DIGEST}
    assert receipt["promotion_merge"] == _merge(CANDIDATE_DIGEST, MERGED_DIGEST)
    assert "manager_correction" not in receipt
    # ``receipt_id`` covers the new field like every other one.
    assert receipt == _nf01436._resign(receipt)

    validated, error = _validate(repo, receipt)

    assert error == ""
    assert validated == receipt


# ---------------------------------------------------------------------------
# (b) the same with a manager correction on the merged path.
# ---------------------------------------------------------------------------
def test_merged_receipt_with_a_manager_correction_on_the_merged_path(
    tmp_path: Path,
) -> None:
    repo = _nf01436._promoted_repo(tmp_path, "merged-corrected", MERGED_CORRECTED)
    correction = _sealed_correction(tmp_path)

    receipt = _receipt(
        repo,
        CORRECTED_DIGEST,
        manager_correction=correction,
        # The correction is written before the combined tree is built, so the
        # merge consumed the CORRECTED bytes, never the sealed ones.
        promotion_merge=_merge(CORRECTED_DIGEST, MERGED_CORRECTED_DIGEST),
    )

    assert receipt["changed_path_hashes"] == {TARGET: MERGED_CORRECTED_DIGEST}
    # The corrected digest was judged against the bytes the merge consumed, not
    # against canonical, which holds the merge of them.
    assert receipt["manager_correction"] == correction
    assert receipt["promotion_merge"] == _merge(CORRECTED_DIGEST, MERGED_CORRECTED_DIGEST)

    validated, error = _validate(repo, receipt)

    assert error == ""
    assert validated == receipt


# ---------------------------------------------------------------------------
# (c) without ``promotion_merge`` nothing changed.
# ---------------------------------------------------------------------------
def test_without_promotion_merge_merged_bytes_still_refuse(tmp_path: Path) -> None:
    repo = _nf01436._promoted_repo(tmp_path, "unexplained", MERGED)

    with pytest.raises(process_launcher_acceptance.WorkspaceError) as excinfo:
        _receipt(repo, CANDIDATE_DIGEST)

    assert str(excinfo.value) == "post_promotion_candidate_mismatch"


def test_without_promotion_merge_the_receipt_and_its_validation_are_unchanged(
    tmp_path: Path,
) -> None:
    repo = _nf01436._promoted_repo(tmp_path, "plain", _nf01436.CANDIDATE)

    receipt = _receipt(repo, CANDIDATE_DIGEST)

    assert set(receipt) == {
        "schema_id", "receipt_id", "task_id", "request_id", "claim_epoch",
        "base_oid", "promoted_paths", "changed_path_hashes",
        "attempt_artifact_manifest_id", "repository_revision",
    }
    assert _validate(repo, receipt) == (receipt, "")
    # Merged bytes under a receipt that does not declare the merge are still
    # refused, with the error string they always had.
    (repo / TARGET).write_bytes(MERGED.encode("utf-8"))
    assert _validate(repo, receipt) == (
        None, "accepted_outcome_receipt_canonical_hash_mismatch"
    )


@pytest.mark.parametrize(
    "merge, expected",
    [
        pytest.param(
            _merge("a" * 64, MERGED_DIGEST),
            "promotion_merge_receipt_mismatch",
            id="candidate_is_not_the_accepted_digest",
        ),
        pytest.param(
            _merge(CANDIDATE_DIGEST, MERGED_DIGEST, path=UNSEALED),
            "promotion_merge_receipt_mismatch",
            id="merged_path_was_never_promoted",
        ),
        pytest.param(
            {"paths": [], "candidate_path_hashes": {}, "merged_path_hashes": {}},
            "promotion_merge_receipt_mismatch",
            id="names_no_path",
        ),
        pytest.param(
            _merge(CANDIDATE_DIGEST, "b" * 64),
            "post_promotion_candidate_mismatch",
            id="canonical_does_not_hold_the_merged_bytes",
        ),
    ],
)
def test_builder_refuses_a_merge_it_cannot_reconcile(
    tmp_path: Path, merge: dict, expected: str
) -> None:
    repo = _nf01436._promoted_repo(tmp_path, "miswired", MERGED)

    with pytest.raises(process_launcher_acceptance.WorkspaceError) as excinfo:
        _receipt(repo, CANDIDATE_DIGEST, promotion_merge=merge)

    assert str(excinfo.value) == expected


# ---------------------------------------------------------------------------
# (d) the validator refuses a ``promotion_merge`` nobody could have produced.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda merge: {**merge, "candidate_path_hashes": {TARGET: "a" * 64}},
            id="wrong_candidate_digest",
        ),
        pytest.param(
            lambda merge: _merge(CANDIDATE_DIGEST, MERGED_DIGEST, path=UNSEALED),
            id="unsealed_path",
        ),
        pytest.param(
            lambda merge: {
                key: value for key, value in merge.items() if key != "merged_path_hashes"
            },
            id="missing_field",
        ),
        pytest.param(
            lambda merge: {**merge, "merged_path_hashes": {TARGET: "not-a-digest"}},
            id="merged_digest_is_not_a_digest",
        ),
        pytest.param(
            lambda merge: {"paths": [], "candidate_path_hashes": {}, "merged_path_hashes": {}},
            id="names_no_path",
        ),
        pytest.param(lambda merge: [merge], id="not_an_object"),
    ],
)
def test_validator_refuses_a_malformed_promotion_merge(tmp_path: Path, mutate) -> None:
    repo = _nf01436._promoted_repo(tmp_path, "tampered", MERGED)
    receipt = _receipt(
        repo, CANDIDATE_DIGEST, promotion_merge=_merge(CANDIDATE_DIGEST, MERGED_DIGEST)
    )
    tampered = _nf01436._resign(
        {**receipt, "promotion_merge": mutate(receipt["promotion_merge"])}
    )

    assert _validate(repo, tampered) == (
        None, "accepted_outcome_receipt_promotion_merge_malformed"
    )


def test_validator_refuses_a_merge_of_the_sealed_bytes_after_a_correction(
    tmp_path: Path,
) -> None:
    """Once a correction landed, the merge consumed the CORRECTED bytes."""
    repo = _nf01436._promoted_repo(tmp_path, "merged-corrected", MERGED_CORRECTED)
    receipt = _receipt(
        repo,
        CORRECTED_DIGEST,
        manager_correction=_sealed_correction(tmp_path),
        promotion_merge=_merge(CORRECTED_DIGEST, MERGED_CORRECTED_DIGEST),
    )
    tampered = _nf01436._resign(
        {**receipt, "promotion_merge": _merge(CANDIDATE_DIGEST, MERGED_CORRECTED_DIGEST)}
    )

    assert _validate(repo, tampered) == (
        None, "accepted_outcome_receipt_promotion_merge_malformed"
    )


# ---------------------------------------------------------------------------
# (e) end to end: the canonical parent changed after launch.
# ---------------------------------------------------------------------------
class _MergingStubManager(_nf01436._StubManager):
    """The nf01436 stub promoting through the PRODUCTION promotion: the version
    guard, then ``promote_merged`` because the workspace recorded parent blobs."""

    _refuse_backwards_version_promotion = (
        process_launcher.ProcessManager._refuse_backwards_version_promotion
    )
    _promote_accepted_candidate = (
        process_launcher.ProcessManager._promote_accepted_candidate
    )


def _drifted_accept_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs
) -> _MergingStubManager:
    """nf01436's accept environment over a REAL repository and worker workspace
    whose canonical parent changes after launch.

    ``_merge_suite._make_workspace`` is the real ``create_workspace`` with its
    recorded parent blobs, reused unchanged -- which is why the target is its
    ``shared.txt``. The candidate bytes stay nf01436's, so its sealed digests,
    its finding and its correction patch all still hold.
    """
    monkeypatch.setattr(_nf01436, "TARGET", MERGE_TARGET)
    monkeypatch.setattr(_nf01436, "_StubManager", _MergingStubManager)
    manager = _nf01436._accept_env(tmp_path, monkeypatch, **kwargs)
    repo = manager.repo
    _merge_suite._git(repo, "init", "-q")
    _merge_suite._git(repo, "config", "user.email", "test@example.com")
    _merge_suite._git(repo, "config", "user.name", "Test")
    # The same two tracked files as the three-way-merge suite's own repository.
    (repo / MERGE_TARGET).write_bytes(LAUNCH.encode("utf-8"))
    (repo / "other.txt").write_bytes(_merge_suite.BASE_OTHER)
    _merge_suite._git(repo, "add", MERGE_TARGET, "other.txt")
    _merge_suite._git(repo, "commit", "-qm", "launch")
    workspace = _merge_suite._make_workspace(monkeypatch, tmp_path, repo, REQUEST_ID)
    (workspace.path / MERGE_TARGET).write_bytes(_nf01436.CANDIDATE.encode("utf-8"))
    # Canonical changes the same file after launch, as 7e2ee6ab did.
    (repo / MERGE_TARGET).write_bytes((DRIFT + LAUNCH).encode("utf-8"))
    _merge_suite._git(repo, "commit", "-qam", "canonical changed after launch")

    def _full_snapshot(source, card, latest, changed):
        # ``merged_paths`` exactly as ``create_combined_validation_workspace``
        # reports it: the same two functions over the same source workspace.
        merges = promotion_merge.plan_merges(source, changed)
        return [], {
            "combined_tree": {"merged_paths": promotion_merge.merged_path_hashes(merges)}
        }

    monkeypatch.setattr(
        process_launcher,
        "WorkerWorkspace",
        SimpleNamespace(from_metadata=lambda meta: workspace),
    )
    monkeypatch.setattr(process_launcher, "_run_full_snapshot_validations", _full_snapshot)
    return manager


def test_accept_promotes_the_merged_bytes_when_the_parent_changed_after_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _drifted_accept_env(tmp_path, monkeypatch, findings=[], dispositions=[])

    result = _nf01436._accept(manager)

    assert result["ok"] is True, result.get("error")
    assert result["promoted_paths"] == [MERGE_TARGET]
    # Canonical holds BOTH edits: its own post-launch header and the candidate.
    assert (manager.repo / MERGE_TARGET).read_bytes() == MERGED.encode("utf-8")
    receipt = result["accepted_outcome_receipt"]
    assert receipt["changed_path_hashes"] == {MERGE_TARGET: MERGED_DIGEST}
    assert receipt["promotion_merge"] == _merge(
        CANDIDATE_DIGEST, MERGED_DIGEST, path=MERGE_TARGET
    )
    # The real ``task_engine.accept_review`` guard validated and persisted it.
    card = task_store.get_task(manager.repo, TASK_ID)
    assert card["status"] == "finished"
    assert card["accept_evidence"]["accepted_outcome_receipt"] == receipt


def test_accept_with_a_correction_promotes_the_merged_corrected_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _drifted_accept_env(tmp_path, monkeypatch)

    result = _nf01436._accept(
        manager,
        manager_correction={
            "patch": _nf01436._patch(MERGE_TARGET),
            "finding_ids": [_nf01436.FINDING_ID],
            "reason": "dropped the duplicated test def the reviewer confirmed",
        },
    )

    assert result["ok"] is True, result.get("error")
    assert (manager.repo / MERGE_TARGET).read_bytes() == MERGED_CORRECTED.encode("utf-8")
    receipt = result["accepted_outcome_receipt"]
    assert receipt["changed_path_hashes"] == {MERGE_TARGET: MERGED_CORRECTED_DIGEST}
    assert receipt["manager_correction"]["corrected_path_hashes"] == {
        MERGE_TARGET: CORRECTED_DIGEST
    }
    assert receipt["promotion_merge"] == _merge(
        CORRECTED_DIGEST, MERGED_CORRECTED_DIGEST, path=MERGE_TARGET
    )
    assert task_store.get_task(manager.repo, TASK_ID)["status"] == "finished"


# ---------------------------------------------------------------------------
# A receipt failure after promotion still restores the retained worktree.
# ---------------------------------------------------------------------------
def test_receipt_failure_after_a_correction_restores_the_retained_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _nf01436._accept_env(tmp_path, monkeypatch)
    workspace_path = Path(
        manager.cards[TASK_ID]["terminal_review"]["evidence"]["workspace"]["path"]
    )

    def _refuse_receipt(*_args, **_kwargs):
        raise process_launcher.WorkspaceError("post_promotion_candidate_mismatch")

    monkeypatch.setattr(process_launcher, "_accepted_outcome_receipt", _refuse_receipt)

    result = _nf01436._accept(manager, manager_correction=_nf01436._correction_payload())

    assert result["ok"] is False
    assert result["error"] == (
        "accepted_outcome_receipt_failed:post_promotion_candidate_mismatch"
    )
    assert result["promoted_paths"] == [TARGET]
    # No canonical rollback: canonical keeps the bytes promotion wrote ...
    assert (manager.repo / TARGET).read_bytes() == _nf01436.CORRECTED.encode("utf-8")
    # ... but the retained worktree is back on the sealed candidate, so the next
    # review does not trip review_workspace_hash_mismatch.
    assert (workspace_path / TARGET).read_bytes() == _nf01436.CANDIDATE.encode("utf-8")
    assert task_store.get_task(manager.repo, TASK_ID)["status"] == "review"
