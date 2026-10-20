"""NF-2026-01625: a late supervisor finalization must not bury an accepted reviewer.

Measured on reviewer card QR-NF01616-A2-SECURITY, request
7cf5b53ab86e426d916a670d60c8dd4c. The status-artifact finalization made the
card review_ready at 10:53:42.913; automatic review accepted it at
10:53:43.605 -- releasing its read-only worktree and appending the
``accepted`` process row -- and archived it at 10:53:44.415. Only then did the
reviewer's supervisor exit, and its monitor finalized the SAME request at
10:53:52.159. ``accepted`` is not a launcher terminal state, so that call ran
the finalizer again, failed ``worktree_is_not_detached_and_isolated`` on the
released worktree and appended ``finalize_failed``
(``terminal_skipped_already_finalized:archived``, usage
``lifecycle_mismatch:archived``) over the accepted attempt. With that row
latest, the accept path and accept_preview's lens census called the lens
unusable, and a fresh reviewer had to be paid for.

An attempt the archived card provably accepted -- ``accepted_request_id`` and
the stored accepted-outcome receipt name this request and claim epoch -- is
final. Every other archive keeps today's finalization.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from test_quality_reviewer_contract import _accepted_reviewer_state  # noqa: E402
from test_spend_is_recorded_for_work_that_succeeded import PAYLOAD, _repo  # noqa: E402

from aiworkhub import (  # noqa: E402
    process_launcher,
    process_launcher_accept_review,
    task_engine,
    task_store,
    worker_workspace,
)

# The reviewer the shared fixture seals as accepted, and the target it reviewed.
_SEALED_ROW, _ = _accepted_reviewer_state()
REQUEST_ID = str(_SEALED_ROW["request_id"])
TASK_ID = str(_SEALED_ROW["task_id"])
RUNNER = str(_SEALED_ROW["runner"])
TOPIC = str(_SEALED_ROW["topic"])
ADAPTER = str(_SEALED_ROW["adapter_id"])
TARGET_REQUEST_ID = str(_SEALED_ROW["quality_review_receipt"]["target"]["request_id"])
TARGET_TASK_ID = str(_SEALED_ROW["quality_review_receipt"]["target"]["task_id"])
CLAIM_EPOCH = 1
# What the scope check raises once acceptance has removed the worktree.
RELEASED_WORKTREE = "worktree_is_not_detached_and_isolated"


def _ledger(manager: process_launcher.ProcessManager, row: dict) -> None:
    """Seed one process row, framed as ``ProcessManager._append_event`` frames it."""
    with manager.process_log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "schema_id": "aiworkhub.task_mcp.process_event.v1",
                    "timestamp": process_launcher._utcnow(),
                    **row,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


def _states(manager: process_launcher.ProcessManager) -> list[str]:
    return [str(row.get("state") or "") for row in manager._request_events(REQUEST_ID)]


def _record_spend(
    repo: Path, *, request_id: str = REQUEST_ID, claim_epoch: int = CLAIM_EPOCH
) -> tuple[bool, str]:
    return task_store.append_live_usage_event(
        repo,
        TASK_ID,
        RUNNER,
        request_id=request_id,
        claimed_by=RUNNER,
        claim_epoch=claim_epoch,
        payload=PAYLOAD,
    )


def _usage_records(repo: Path) -> int:
    with contextlib.closing(sqlite3.connect(task_store.canonical_db_path(repo))) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND event='usage_record'",
                (TASK_ID,),
            ).fetchone()[0]
        )


def _review_ready_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    metadata_claim_epoch: int = CLAIM_EPOCH,
) -> SimpleNamespace:
    """The reviewer exactly as the status-artifact finalization left it.

    Its card is review_ready with the sealed receipt, its spend is recorded
    once, and its process rows end in ``review_ready`` -- while its supervisor,
    still exiting, has not been finalized by its own monitor yet.
    """
    # The late finalizer runs inside the MCP server while its write gate is open.
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setenv("AIWORKHUB_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))

    def released_worktree(*_args: object, **_kwargs: object) -> list[str]:
        raise worker_workspace.WorkspaceError(RELEASED_WORKTREE)

    monkeypatch.setattr(process_launcher, "enforce_scope", released_worktree)

    repo = _repo(tmp_path)
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        collision_guard=lambda **_: {
            "returncode": 0,
            "stdout": '{"collision_free":true}',
            "stderr": "",
        },
    )
    accepted_row, card = _accepted_reviewer_state()
    workspace = worker_workspace.WorkerWorkspace(
        request_id=REQUEST_ID,
        repo=repo,
        path=tmp_path / "worktrees" / REQUEST_ID,
        home=tmp_path / "homes" / REQUEST_ID,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
        base_oid="b" * 40,
    ).as_metadata()
    manifest = {
        "schema_id": "aiworkhub.attempt_artifact_bundle_receipt.v1",
        "attempt_id": REQUEST_ID,
        "manifest_sha256": "a" * 64,
        "verified": True,
    }
    # The fixture's accepted card, wound back to the review it was accepted from.
    del card["accepted_request_id"], card["accept_evidence"]
    evidence = card["terminal_review"]["evidence"]
    evidence["workspace"] = workspace
    evidence["attempt_artifact_manifest"] = manifest
    evidence["quality_review"].update(
        lens="correctness",
        target_task_id=TARGET_TASK_ID,
        target_request_id=TARGET_REQUEST_ID,
    )
    card["terminal_review"]["substatus"] = "review_ready"
    card.update(
        runner=RUNNER,
        claimed_by=RUNNER,
        review_requested_by=RUNNER,
        launch_request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH,
        status="review",
        worker_status="review",
    )
    now = process_launcher._utcnow()
    with contextlib.closing(sqlite3.connect(task_store.canonical_db_path(repo))) as conn, conn:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, claimed_by,"
            " card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (TASK_ID, RUNNER, TOPIC, "review", "review", RUNNER, json.dumps(card), now, now),
        )
    # The status-artifact finalization recorded the spend while still in review.
    assert _record_spend(repo) == (True, "recorded")

    stdout_path = process_dir / f"{REQUEST_ID}.stdout.log"
    stderr_path = process_dir / f"{REQUEST_ID}.stderr.log"
    status_path = process_dir / f"{REQUEST_ID}.supervisor.json"
    metadata_path = process_dir / f"{REQUEST_ID}.request.json"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    # Owner-only, as the supervisor writes it: ``read_supervisor_status``
    # refuses anything else on POSIX.
    worker_workspace.write_json_0600(status_path, {"state": "exited", "exit_code": 0})
    worker_workspace.write_json_0600(
        metadata_path,
        {
            "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": ADAPTER,
            "model": ADAPTER,
            "claim_epoch": metadata_claim_epoch,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "supervisor_status_path": str(status_path),
            "cancel_path": str(process_dir / f"{REQUEST_ID}.cancel.json"),
            "required_outputs": [],
            "quality_review": evidence["quality_review"],
            "workspace": workspace,
        },
    )
    identity = {
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
    }
    _ledger(
        manager,
        {
            **identity,
            "model": ADAPTER,
            "state": "running",
            # The supervisor has exited by the time its own monitor finalizes.
            "pid": 0,
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "workspace_isolated": True,
        },
    )
    _ledger(
        manager,
        {
            **identity,
            "state": "review_ready",
            "review_transition_ok": True,
            "release_transition_ok": True,
            "usage_recorded": True,
            "workspace_retained": True,
        },
    )
    return SimpleNamespace(
        manager=manager,
        repo=repo,
        accepted_row=accepted_row,
        sealed=accepted_row["quality_review_receipt"],
        workspace=workspace,
        manifest=manifest,
    )


def _accept(case: SimpleNamespace) -> dict:
    """What the accept path does for a read-only reviewer it accepts."""
    receipt = process_launcher._accepted_outcome_receipt(
        case.repo,
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH,
        base_oid=case.workspace["base_oid"],
        promoted_paths=[],
        changed_path_hashes={},
        attempt_artifact_manifest=case.manifest,
    )
    result = task_engine.accept_review(
        case.repo,
        TASK_ID,
        runner=RUNNER,
        topic=TOPIC,
        request_id=REQUEST_ID,
        evidence={
            "promoted_paths": [],
            "validation": [],
            "required_outputs": [],
            "quality_review_receipt": case.sealed,
            "attempt_artifact_manifest": case.manifest,
        },
        accepted_outcome_receipt=receipt,
    )
    assert result["ok"], result
    _ledger(
        case.manager,
        {
            **case.accepted_row,
            "accepted_outcome_receipt": receipt,
            "workspace_disposition": "removed",
        },
    )
    return receipt


def _archive(case: SimpleNamespace, *, actor: str, reason: str, supersede: bool = False) -> None:
    result = task_engine.archive_task(
        case.repo, TASK_ID, actor=actor, reason=reason, supersede=supersede
    )
    assert result["ok"], result


def _automatic_review(case: SimpleNamespace) -> dict:
    """Automatic review: accept the reviewer, then archive its card."""
    receipt = _accept(case)
    _archive(case, actor=RUNNER, reason=f"automatic review accepted:{TARGET_REQUEST_ID}")
    return receipt


def test_supervisor_finalization_after_automatic_accept_keeps_the_reviewer_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    receipt = _automatic_review(case)

    # What ``_monitor`` calls once the reviewer's supervisor finally exits.
    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["state"] == "accepted", event
    assert _states(case.manager) == ["running", "review_ready", "accepted"]

    # The accept path's own reviewer check still verifies it as accepted ...
    latest = case.manager._request_events(REQUEST_ID)[-1]
    card = process_launcher._parse_card(case.manager._show_task(TASK_ID), TASK_ID)
    assert task_store.canonical_status(card) == "archived"
    assert latest["accepted_outcome_receipt"] == receipt
    assert process_launcher._verified_accepted_quality_review_receipt(
        latest, card, REQUEST_ID, TARGET_REQUEST_ID, TARGET_TASK_ID
    ) == case.sealed
    # ... and accept_preview's lens census still counts the lens as usable.
    lenses = process_launcher_accept_review.reviewer_evidence(
        case.manager, TARGET_TASK_ID, TARGET_REQUEST_ID
    )
    assert [
        (row["lens"], row["request_id"], row["state"], row["usable"]) for row in lenses
    ] == [("correctness", REQUEST_ID, "accepted", True)]

    # The spend recorded at review_ready stays its only record.
    assert _record_spend(case.repo) == (True, "already_recorded")
    assert _usage_records(case.repo) == 1


def _manager_archive_without_receipt(case: SimpleNamespace) -> None:
    """A manager archived the card: it carries no seal and no receipt."""
    _archive(case, actor="manager", reason="manager disposal")
    # The process row claims acceptance; the card, the authority, does not.
    _ledger(case.manager, {**case.accepted_row, "workspace_disposition": "removed"})


def _superseded_after_acceptance(case: SimpleNamespace) -> None:
    _accept(case)
    _archive(case, actor="manager", reason="superseded", supersede=True)


@pytest.mark.parametrize(
    ("dispose", "metadata_claim_epoch"),
    [
        pytest.param(
            _manager_archive_without_receipt, CLAIM_EPOCH, id="manager_archive_without_receipt"
        ),
        pytest.param(_superseded_after_acceptance, CLAIM_EPOCH, id="superseded_after_acceptance"),
        pytest.param(_automatic_review, CLAIM_EPOCH + 1, id="receipt_for_another_claim_epoch"),
    ],
)
def test_archive_without_this_attempts_receipt_keeps_todays_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispose: Callable[[SimpleNamespace], object],
    metadata_claim_epoch: int,
) -> None:
    case = _review_ready_reviewer(
        tmp_path, monkeypatch, metadata_claim_epoch=metadata_claim_epoch
    )
    dispose(case)

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    # Exactly the measured outcome: the attempt is finalized again, fails on
    # the released worktree, and its spend is refused for the archived card.
    assert event is not None and event["state"] == "finalize_failed", event
    assert _states(case.manager)[-3:] == ["accepted", "finalizing", "finalize_failed"]
    assert event["release_transition_ok"] is True
    assert event["review_transition_ok"] is False
    assert str(event["terminal_review_disposition"]).startswith(
        "terminal_skipped_already_finalized:"
    )
    assert event["usage_recorded"] is False
    assert event["usage_error"] == "lifecycle_mismatch:archived"
    assert _usage_records(case.repo) == 1


def test_spend_on_an_archived_card_is_owed_only_by_the_attempt_it_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _automatic_review(case)

    assert _record_spend(case.repo) == (True, "already_recorded")
    for refused in (
        _record_spend(case.repo, request_id="f" * 32),
        _record_spend(case.repo, claim_epoch=CLAIM_EPOCH + 1),
    ):
        assert refused == (False, "lifecycle_mismatch:archived")
    assert _usage_records(case.repo) == 1


@pytest.mark.parametrize(
    "dispose",
    [
        pytest.param(_manager_archive_without_receipt, id="manager_archive_without_receipt"),
        pytest.param(_superseded_after_acceptance, id="superseded_after_acceptance"),
    ],
)
def test_spend_on_an_archive_without_this_attempts_receipt_still_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispose: Callable[[SimpleNamespace], object],
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    dispose(case)

    assert _record_spend(case.repo) == (False, "lifecycle_mismatch:archived")
    assert _usage_records(case.repo) == 1


@pytest.mark.parametrize(
    ("mutate", "request_id", "claim_epoch", "expected"),
    [
        pytest.param(lambda card: None, REQUEST_ID, CLAIM_EPOCH, True, id="this_attempt"),
        pytest.param(lambda card: None, "f" * 32, CLAIM_EPOCH, False, id="another_request"),
        pytest.param(
            lambda card: None, REQUEST_ID, CLAIM_EPOCH + 1, False, id="another_claim_epoch"
        ),
        pytest.param(lambda card: None, REQUEST_ID, True, False, id="bool_claim_epoch"),
        pytest.param(
            lambda card: card.update(archived_at=""),
            REQUEST_ID,
            CLAIM_EPOCH,
            False,
            id="not_archived",
        ),
        pytest.param(
            lambda card: card.update(archive_operation="superseded"),
            REQUEST_ID,
            CLAIM_EPOCH,
            False,
            id="superseded",
        ),
        pytest.param(
            lambda card: card.pop("accept_evidence"),
            REQUEST_ID,
            CLAIM_EPOCH,
            False,
            id="no_receipt",
        ),
        pytest.param(
            lambda card: card["accept_evidence"]["accepted_outcome_receipt"].update(
                claim_epoch=True
            ),
            REQUEST_ID,
            CLAIM_EPOCH,
            False,
            id="bool_receipt_claim_epoch",
        ),
    ],
)
def test_archived_after_accepting_names_exactly_one_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict], object],
    request_id: str,
    claim_epoch: object,
    expected: bool,
) -> None:
    """The one predicate both the finalizer and the spend gate consult."""
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _automatic_review(case)
    card = task_store.get_task(case.repo, TASK_ID)
    assert card is not None
    mutate(card)

    assert (
        task_store.archived_after_accepting(
            card, request_id=request_id, claim_epoch=claim_epoch
        )
        is expected
    )
