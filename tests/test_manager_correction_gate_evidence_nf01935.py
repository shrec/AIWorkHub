"""NF-2026-01935: a manager correction may cite manager-measured gate evidence.

A defect only the manager's own gate or test run measured, on a review_ready
card with no reviewer finding, used to be uncorrectable: every ``finding_ids``
entry had to name a confirmed reviewer finding (or, on validation_failed only,
a failed ``validation:<index>`` row). The third form is
``manager:<repo-relative path>@<sha256>``, naming a file under ``.scratch/`` or
``.aiworkhub/runtime/`` of the VERIFIED canonical repo.

These tests drive the real ``accept_review`` and ``task_engine`` guard through
the NF-2026-01436 harness and the NF-2026-01845 helpers, imported, not copied.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import test_accept_with_manager_correction_nf01436 as _nf01436  # noqa: E402
import test_manager_correction_validation_failed_nf01845 as _nf01845  # noqa: E402

from aiworkhub import (  # noqa: E402
    process_launcher_accept_review,
    task_engine,
    task_store,
)

TARGET = _nf01436.TARGET
TASK_ID = _nf01436.TASK_ID
REQUEST_ID = _nf01436.REQUEST_ID
CORRECTED = _nf01436.CORRECTED
CORRECTED_DIGEST = _nf01436.CORRECTED_DIGEST

EVIDENCE_PATH = ".scratch/nf01935/gate.log"
EVIDENCE_BYTES = b"FAILED tests/test_widget.py::test_widget - duplicate def\n"
EVIDENCE_DIGEST = hashlib.sha256(EVIDENCE_BYTES).hexdigest()
MANAGER_ID = f"manager:{EVIDENCE_PATH}@{EVIDENCE_DIGEST}"
REASON = "the manager's gate run measured the duplicated test def"


def _write(repo: Path, relative: str, data: bytes) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs):
    """The NF-2026-01436 review_ready harness plus one canonical evidence file."""
    kwargs.setdefault("findings", [])
    kwargs.setdefault("dispositions", [])
    manager = _nf01436._accept_env(tmp_path, monkeypatch, **kwargs)
    _write(manager.repo, EVIDENCE_PATH, EVIDENCE_BYTES)
    return manager


def _correction(ids: list[str], reason: str | None = REASON) -> dict:
    payload = {"patch": _nf01436._patch(), "finding_ids": ids}
    if reason is not None:
        payload["reason"] = reason
    return payload


# (a) review_ready, no reviewer finding, a real .scratch evidence file.
def test_manager_evidence_correction_is_accepted_on_review_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _gate_env(tmp_path, monkeypatch)

    result = _nf01845._accept(manager, manager_correction=_correction([MANAGER_ID]))

    assert result["ok"] is True, result.get("error")
    assert result["promoted_paths"] == [TARGET]
    assert (manager.repo / TARGET).read_text(encoding="utf-8") == CORRECTED
    receipt = result["accepted_outcome_receipt"]
    assert receipt["changed_path_hashes"] == {TARGET: CORRECTED_DIGEST}
    correction = receipt["manager_correction"]
    assert set(correction) == {
        "patch_sha256", "finding_ids", "paths", "candidate_path_hashes",
        "corrected_path_hashes", "reason", "correction_origin", "manager_evidence",
    }
    assert correction["correction_origin"] == "manager_gate_evidence"
    assert correction["manager_evidence"] == [
        {"path": EVIDENCE_PATH, "sha256": EVIDENCE_DIGEST}
    ]
    assert correction["finding_ids"] == [MANAGER_ID]
    card = task_store.get_task(manager.repo, TASK_ID)
    assert card["status"] == "finished"
    assert card["accept_evidence"]["accepted_outcome_receipt"] == receipt


def test_runtime_root_is_accepted_and_evidence_is_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _gate_env(tmp_path, monkeypatch)
    runtime_path = ".aiworkhub/runtime/logs/gate.txt"
    runtime_bytes = b"gate: duplicate def\n"
    _write(manager.repo, runtime_path, runtime_bytes)
    runtime_digest = hashlib.sha256(runtime_bytes).hexdigest()
    runtime_id = f"manager:{runtime_path}@{runtime_digest}"

    result = _nf01845._accept(
        manager, manager_correction=_correction([MANAGER_ID, runtime_id])
    )

    assert result["ok"] is True, result.get("error")
    evidence = result["accepted_outcome_receipt"]["manager_correction"][
        "manager_evidence"
    ]
    assert evidence == [
        {"path": runtime_path, "sha256": runtime_digest},
        {"path": EVIDENCE_PATH, "sha256": EVIDENCE_DIGEST},
    ]


# (b) each refusal token, and nothing lands.
def _refusal_cases(repo: Path) -> list[tuple[str, str, str | None]]:
    _write(repo, "src/demo/outside.log", EVIDENCE_BYTES)
    big = b"x" * (process_launcher_accept_review.MAX_MANAGER_EVIDENCE_BYTES + 1)
    _write(repo, ".scratch/big.log", big)
    big_digest = hashlib.sha256(big).hexdigest()
    return [
        ("manager_evidence_malformed", f"manager:{EVIDENCE_PATH}", REASON),
        ("manager_evidence_malformed", f"manager:{EVIDENCE_PATH}@xyz", REASON),
        ("manager_evidence_malformed", f"manager:@{EVIDENCE_DIGEST}", REASON),
        (
            "manager_evidence_outside_root",
            f"manager:src/demo/outside.log@{EVIDENCE_DIGEST}",
            REASON,
        ),
        (
            "manager_evidence_outside_root",
            f"manager:.scratch/../src/demo/outside.log@{EVIDENCE_DIGEST}",
            REASON,
        ),
        (
            "manager_evidence_outside_root",
            f"manager:/{EVIDENCE_PATH}@{EVIDENCE_DIGEST}",
            REASON,
        ),
        (
            "manager_evidence_missing",
            f"manager:.scratch/absent.log@{EVIDENCE_DIGEST}",
            REASON,
        ),
        ("manager_evidence_too_large", f"manager:.scratch/big.log@{big_digest}", REASON),
        (
            "manager_evidence_digest_mismatch",
            f"manager:{EVIDENCE_PATH}@{'0' * 64}",
            REASON,
        ),
        ("manager_evidence_reason_required", MANAGER_ID, None),
        ("manager_evidence_reason_required", MANAGER_ID, "   "),
    ]


@pytest.mark.parametrize("case", range(11))
def test_each_bad_manager_id_is_refused_by_its_own_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: int
) -> None:
    manager = _gate_env(tmp_path, monkeypatch)
    token, finding_id, reason = _refusal_cases(manager.repo)[case]

    result = _nf01845._accept(
        manager, manager_correction=_correction([finding_id], reason=reason)
    )

    assert result["ok"] is False
    assert result["error"] == (
        "manager_correction_invalid:" + f"{token}:{finding_id}"[:200]
    )
    _nf01845._assert_nothing_landed(manager)


def test_every_refusal_token_is_covered(tmp_path: Path) -> None:
    tokens = {token for token, _id, _reason in _refusal_cases(tmp_path)}
    assert tokens == {
        "manager_evidence_malformed",
        "manager_evidence_outside_root",
        "manager_evidence_missing",
        "manager_evidence_too_large",
        "manager_evidence_digest_mismatch",
        "manager_evidence_reason_required",
    }


def test_symlinked_evidence_is_outside_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(tmp_path, "elsewhere/gate.log", EVIDENCE_BYTES)
    (repo / ".scratch").mkdir(parents=True)
    try:
        (repo / ".scratch" / "link.log").symlink_to(tmp_path / "elsewhere" / "gate.log")
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    entry, error = process_launcher_accept_review.manager_evidence_entry(
        repo, f"manager:.scratch/link.log@{EVIDENCE_DIGEST}"
    )
    assert (entry, error) == ({}, "manager_evidence_outside_root")


# (c) a confirmed reviewer finding and a manager id together.
def test_mixed_reviewer_and_manager_ids_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _gate_env(
        tmp_path,
        monkeypatch,
        findings=[_nf01436._low_finding()],
        dispositions=[_nf01436._disposition()],
    )

    result = _nf01845._accept(
        manager,
        manager_correction=_correction([_nf01436.FINDING_ID, MANAGER_ID]),
    )

    assert result["ok"] is True, result.get("error")
    correction = result["accepted_outcome_receipt"]["manager_correction"]
    assert correction["finding_ids"] == sorted([_nf01436.FINDING_ID, MANAGER_ID])
    assert correction["correction_origin"] == "manager_gate_evidence"
    assert correction["manager_evidence"] == [
        {"path": EVIDENCE_PATH, "sha256": EVIDENCE_DIGEST}
    ]
    # Only the reviewer finding is lifted; the manager id lifts nothing.
    assert result["auto_finding_dispositions"]["counts"]["fixed_by_correction"] == 1


def test_validation_row_plus_manager_id_keeps_the_validation_failed_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _nf01845._validation_failed_env(tmp_path, monkeypatch)
    _write(manager.repo, EVIDENCE_PATH, EVIDENCE_BYTES)

    result = _nf01845._accept(
        manager,
        manager_correction=_correction([_nf01845.FAILED_ROW, MANAGER_ID]),
    )

    assert result["ok"] is True, result.get("error")
    correction = result["accepted_outcome_receipt"]["manager_correction"]
    assert correction["correction_origin"] == "validation_failed"
    assert correction["manager_evidence"] == [
        {"path": EVIDENCE_PATH, "sha256": EVIDENCE_DIGEST}
    ]


# (d) the old forms are unchanged.
def test_empty_finding_ids_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _gate_env(tmp_path, monkeypatch)

    result = _nf01845._accept(manager, manager_correction=_correction([]))

    assert result["ok"] is False
    assert result["error"] == "manager_correction_invalid:finding_ids_empty"
    _nf01845._assert_nothing_landed(manager)


def test_unknown_reviewer_id_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _gate_env(tmp_path, monkeypatch)

    result = _nf01845._accept(
        manager, manager_correction=_correction([_nf01436.FINDING_ID])
    )

    assert result["ok"] is False
    assert result["error"] == (
        f"manager_correction_invalid:finding_not_found:{_nf01436.FINDING_ID}"
    )


# The engine-side receipt validator accepts exactly the new origin and field.
def test_receipt_validator_accepts_the_manager_gate_evidence_origin(
    tmp_path: Path,
) -> None:
    repo = _nf01436._promoted_repo(tmp_path, "gate-evidence", CORRECTED)
    receipt = _nf01436._corrected_receipt(
        repo,
        finding_ids=[MANAGER_ID],
        correction_origin="manager_gate_evidence",
        manager_evidence=[{"path": EVIDENCE_PATH, "sha256": EVIDENCE_DIGEST}],
    )
    correction = receipt["manager_correction"]
    assert correction["correction_origin"] == "manager_gate_evidence"
    assert correction["manager_evidence"] == [
        {"path": EVIDENCE_PATH, "sha256": EVIDENCE_DIGEST}
    ]
    card = _nf01436._receipt_card(repo)
    validated, error = task_engine._validate_accepted_outcome_receipt(
        repo, card, TASK_ID, REQUEST_ID, receipt
    )
    assert error == ""
    assert validated == receipt

    for bad in (
        {"correction_origin": "manager"},
        {"manager_evidence": [{"path": EVIDENCE_PATH, "sha256": "nothex"}]},
        {"manager_evidence": []},
    ):
        forged = _nf01436._resign(
            {**receipt, "manager_correction": {**correction, **bad}}
        )
        _validated, forged_error = task_engine._validate_accepted_outcome_receipt(
            repo, card, TASK_ID, REQUEST_ID, forged
        )
        assert forged_error == "accepted_outcome_receipt_manager_correction_malformed"


# Review corrections: containment is the evidence root, a drive colon in any
# part refuses, and the receipt's origin, ids and evidence agree.
def test_drive_colon_in_a_later_part_is_outside_root(tmp_path: Path) -> None:
    entry, error = process_launcher_accept_review.manager_evidence_entry(
        tmp_path, f"manager:.scratch/C:/gate.log@{EVIDENCE_DIGEST}"
    )
    assert (entry, error) == ({}, "manager_evidence_outside_root")


def test_unseen_link_cannot_leave_the_evidence_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Windows junction is not ``is_symlink()``; a link the component walk
    # misses must still not reach a repo file outside ``.scratch``.
    repo = tmp_path / "repo"
    _write(repo, "src/demo/outside.log", EVIDENCE_BYTES)
    (repo / ".scratch").mkdir(parents=True)
    link = repo / ".scratch" / "j"
    try:
        link.symlink_to(repo / "src", target_is_directory=True)
    except OSError:
        winapi = pytest.importorskip("_winapi")
        winapi.CreateJunction(str(repo / "src"), str(link))
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)

    entry, error = process_launcher_accept_review.manager_evidence_entry(
        repo, f"manager:.scratch/j/demo/outside.log@{EVIDENCE_DIGEST}"
    )

    assert (entry, error) == ({}, "manager_evidence_outside_root")


def test_receipt_validator_cross_checks_origin_ids_and_evidence(
    tmp_path: Path,
) -> None:
    repo = _nf01436._promoted_repo(tmp_path, "gate-evidence-cross", CORRECTED)
    card = _nf01436._receipt_card(repo)
    entry = {"path": EVIDENCE_PATH, "sha256": EVIDENCE_DIGEST}
    for overrides in (
        {"correction_origin": "manager_gate_evidence"},
        {"finding_ids": [MANAGER_ID]},
        {"correction_origin": "manager_gate_evidence", "manager_evidence": [entry]},
        {"finding_ids": [MANAGER_ID], "manager_evidence": [entry]},
    ):
        receipt = _nf01436._corrected_receipt(repo, **overrides)
        _validated, error = task_engine._validate_accepted_outcome_receipt(
            repo, card, TASK_ID, REQUEST_ID, receipt
        )
        assert error == "accepted_outcome_receipt_manager_correction_malformed", (
            overrides
        )
