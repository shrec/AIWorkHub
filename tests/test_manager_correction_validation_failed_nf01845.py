"""NF-2026-01845: a manager correction may land a ``validation_failed`` candidate.

A ``validation_failed`` candidate used to be a dead end for ``accept_review``:
the substatus gate refused it before the correction was even read, so a manager
who could see the one-line fix had to reject and pay for a whole rework round.

These tests drive the real ``process_launcher_accept_review.accept_review`` and
the real ``task_engine.accept_review`` guard through the NF-2026-01436 harness
(the same one other accept tests import), with the target card re-sealed as
``validation_failed``. The declared validation is a real byte check on the
accept tree, so "the corrected bytes pass" and "still fail" are decided by the
bytes rather than asserted:

* a correction naming the failing row as ``validation:<i>`` whose corrected
  bytes pass is accepted and committed, and the receipt carries
  ``correction_origin`` and the patch digest;
* the same correction whose bytes still fail is refused with a named blocker
  and nothing lands;
* ``validation:<i>`` naming a row that did not fail is refused by name;
* without a correction the refusal is exactly what it always was;
* an explicit reviewer launch is admitted for the target, an automatic one is
  not.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import test_accept_with_manager_correction_nf01436 as _nf01436  # noqa: E402

from aiworkhub import (  # noqa: E402
    process_launcher,
    process_launcher_accept_review,
    quality_evidence,
    task_engine,
    task_store,
)
from aiworkhub.worker_workspace import WorkspaceError  # noqa: E402

TARGET = _nf01436.TARGET
TASK_ID = _nf01436.TASK_ID
REQUEST_ID = _nf01436.REQUEST_ID
CANDIDATE = _nf01436.CANDIDATE
CORRECTED = _nf01436.CORRECTED
CANDIDATE_DIGEST = _nf01436.CANDIDATE_DIGEST
CORRECTED_DIGEST = _nf01436.CORRECTED_DIGEST

FAILED_ROW = "validation:0"
PASSED_ROW = "validation:1"

# Edits the duplicated def instead of removing it, so the byte check still fails.
STILL_FAILING_PATCH = (
    f"--- a/{TARGET}\n"
    f"+++ b/{TARGET}\n"
    "@@ -9,2 +9,2 @@\n"
    " def test_widget():\n"
    "-    assert widget() == 1\n"
    "+    assert widget() == 1  # touched\n"
)


def _validation_rows() -> list[dict]:
    return [
        {"command": "python -m pytest -q tests/test_widget.py", "returncode": 1},
        {"command": "python -m ruff check src", "returncode": 0},
    ]


def _persist_target_card(manager) -> None:
    _readiness, db_path = task_store._require_ready(manager.repo)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(manager.cards[TASK_ID]), TASK_ID),
        )


def _validation_failed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _nf01436._StubManager:
    """The NF-2026-01436 harness with the target re-sealed as validation_failed.

    A validation_failed card carries neither the attempt-artifact manifest nor
    the evidence record in its sealed evidence; both live on the finalization
    event only. The declared validation is a real check on the accept tree's
    bytes: the candidate's duplicated test def is exactly what fails it.
    """
    manager = _nf01436._accept_env(tmp_path, monkeypatch, findings=[], dispositions=[])
    card = manager.cards[TASK_ID]
    terminal = card["terminal_review"]
    evidence = terminal["evidence"]
    terminal["substatus"] = "validation_failed"
    manifest = evidence.pop("attempt_artifact_manifest")
    record = evidence.pop("evidence_record")
    evidence["validation"] = _validation_rows()
    manager.events[REQUEST_ID][0].update(
        {
            "state": "validation_failed",
            "attempt_artifact_manifest": manifest,
            "evidence_record": record,
        }
    )
    _persist_target_card(manager)

    def _revalidate(workspace, card, latest, changed, **_kwargs):
        text = (workspace.path / TARGET).read_text(encoding="utf-8")
        if text.count("def test_widget") != 1:
            raise WorkspaceError("validation_failed:duplicate_test_def")
        return [], {"combined_tree": {}}

    monkeypatch.setattr(process_launcher, "_run_full_snapshot_validations", _revalidate)
    return manager


def _correction(patch: str = _nf01436._patch(), ids: list[str] | None = None) -> dict:
    return {
        "patch": patch,
        "finding_ids": ids if ids is not None else [FAILED_ROW],
        "reason": "dropped the duplicated test def that failed validation",
    }


def _accept(manager, **kwargs) -> dict:
    return process_launcher_accept_review.accept_review(
        manager, REQUEST_ID, TASK_ID, **kwargs
    )


def _assert_nothing_landed(manager) -> None:
    assert manager.promoted == []
    assert not (manager.repo / TARGET).exists()
    workspace_path = Path(
        manager.cards[TASK_ID]["terminal_review"]["evidence"]["workspace"]["path"]
    )
    assert (workspace_path / TARGET).read_text(encoding="utf-8") == CANDIDATE
    assert task_store.get_task(manager.repo, TASK_ID)["status"] == "review"


def test_corrected_validation_failed_candidate_is_accepted_and_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _validation_failed_env(tmp_path, monkeypatch)

    result = _accept(manager, manager_correction=_correction())

    assert result["ok"] is True, result.get("error")
    assert result["promoted_paths"] == [TARGET]
    assert (manager.repo / TARGET).read_text(encoding="utf-8") == CORRECTED

    receipt = result["accepted_outcome_receipt"]
    assert receipt["changed_path_hashes"] == {TARGET: CORRECTED_DIGEST}
    correction = receipt["manager_correction"]
    assert set(correction) == {
        "patch_sha256", "finding_ids", "paths", "candidate_path_hashes",
        "corrected_path_hashes", "reason", "correction_origin",
    }
    assert correction["correction_origin"] == "validation_failed"
    assert correction["patch_sha256"] == hashlib.sha256(
        _nf01436._patch().encode("utf-8")
    ).hexdigest()
    assert correction["finding_ids"] == [FAILED_ROW]
    assert correction["candidate_path_hashes"] == {TARGET: CANDIDATE_DIGEST}
    assert correction["corrected_path_hashes"] == {TARGET: CORRECTED_DIGEST}

    card = task_store.get_task(manager.repo, TASK_ID)
    assert card["status"] == "finished"
    assert card["accepted_request_id"] == REQUEST_ID
    assert card["accept_evidence"]["accepted_outcome_receipt"] == receipt


def test_correction_whose_bytes_still_fail_validation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _validation_failed_env(tmp_path, monkeypatch)

    result = _accept(manager, manager_correction=_correction(STILL_FAILING_PATCH))

    assert result["ok"] is False
    assert result["error"] == "revalidation_failed:validation_failed:duplicate_test_def"
    _assert_nothing_landed(manager)


@pytest.mark.parametrize("row_id", [PASSED_ROW, "validation:9", "validation:x"])
def test_naming_a_row_that_did_not_fail_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row_id: str
) -> None:
    manager = _validation_failed_env(tmp_path, monkeypatch)

    result = _accept(manager, manager_correction=_correction(ids=[row_id]))

    assert result["ok"] is False
    assert result["error"] == (
        f"manager_correction_validation_row_not_failed:{row_id.split(':', 1)[1]}"
    )
    _assert_nothing_landed(manager)


def test_validation_failed_without_a_correction_is_refused_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _validation_failed_env(tmp_path, monkeypatch)

    result = _accept(manager)

    assert result["ok"] is False
    assert result["error"].startswith("terminal_substatus_not_review_ready")
    _assert_nothing_landed(manager)


def test_validation_row_ids_mean_nothing_on_a_review_ready_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _nf01436._accept_env(tmp_path, monkeypatch)

    result = _accept(manager, manager_correction=_correction())

    assert result["ok"] is False
    assert result["error"] == f"manager_correction_invalid:finding_not_found:{FAILED_ROW}"
    assert manager.promoted == []


def _receipt_with_origin(tmp_path: Path, origin: str | None) -> tuple[Path, dict]:
    repo = _nf01436._promoted_repo(tmp_path, f"origin-{origin}", CORRECTED)
    overrides = {} if origin is None else {"correction_origin": origin}
    return repo, _nf01436._corrected_receipt(repo, **overrides)


def test_receipt_validator_accepts_only_the_validation_failed_origin(
    tmp_path: Path,
) -> None:
    repo, receipt = _receipt_with_origin(tmp_path, "validation_failed")
    assert receipt["manager_correction"]["correction_origin"] == "validation_failed"
    validated, error = task_engine._validate_accepted_outcome_receipt(
        repo, _nf01436._receipt_card(repo), TASK_ID, REQUEST_ID, receipt
    )
    assert error == ""
    assert validated == receipt

    # The builder never invents an origin, and the validator refuses a forged one.
    _repo, plain = _receipt_with_origin(tmp_path, None)
    assert "correction_origin" not in plain["manager_correction"]
    forged = _nf01436._resign(
        {
            **receipt,
            "manager_correction": {
                **receipt["manager_correction"],
                "correction_origin": "reviewer",
            },
        }
    )
    _validated, forged_error = task_engine._validate_accepted_outcome_receipt(
        repo, _nf01436._receipt_card(repo), TASK_ID, REQUEST_ID, forged
    )
    assert forged_error == "accepted_outcome_receipt_manager_correction_malformed"


def test_engine_refuses_a_validation_failed_card_without_the_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _validation_failed_env(tmp_path, monkeypatch)
    _repo, plain = _receipt_with_origin(tmp_path, None)

    result = task_engine.accept_review(
        manager.repo,
        TASK_ID,
        runner=_nf01436.RUNNER,
        topic=_nf01436.TOPIC,
        request_id=REQUEST_ID,
        evidence={"attempt_artifact_manifest": _nf01436._manifest()},
        accepted_outcome_receipt=plain,
    )

    assert result["ok"] is False
    assert result["stderr"] == "terminal_substatus_not_review_ready:validation_failed"
    assert task_store.get_task(manager.repo, TASK_ID)["status"] == "review"


_SENTINEL = {"ok": False, "error": "past_the_target_gate"}


def _launch(substatus: str, *, explicit: bool) -> dict:
    card = {
        "task_id": TASK_ID,
        "status": "review",
        "worker_status": "review",
        "terminal_substatus": substatus,
        "terminal_review": {"substatus": substatus},
    }
    stub = SimpleNamespace(
        _show_task=lambda task_id: {"returncode": 0, "stdout": json.dumps(card)},
        _live_reviewer_receipt=lambda *a, **k: None,
        _exact_quality_review_target_refusal=lambda *a, **k: dict(_SENTINEL),
    )
    return process_launcher.ProcessManager.launch_quality_reviewer(
        stub,
        target_request_id=REQUEST_ID,
        target_task_id=TASK_ID,
        reviewer_task_id="QR_NF01845",
        runner="claude_opus-5",
        adapter_id="claude_cli",
        lens=quality_evidence.LENS_CORRECTNESS,
        refuse_live_lens=explicit,
    )


def test_explicit_reviewer_launch_is_admitted_for_a_validation_failed_target() -> None:
    assert _launch("validation_failed", explicit=True) == _SENTINEL
    assert _launch("review_ready", explicit=True) == _SENTINEL


@pytest.mark.parametrize(
    "substatus, explicit",
    [
        ("validation_failed", False),
        ("launch_failed", True),
        ("launch_failed", False),
    ],
)
def test_other_reviewer_launches_still_require_review_ready(
    substatus: str, explicit: bool
) -> None:
    result = _launch(substatus, explicit=explicit)

    assert result["ok"] is False
    assert result["error"] == "quality_review_target_not_review_ready"
    assert result["fails_at_launch"] is True
