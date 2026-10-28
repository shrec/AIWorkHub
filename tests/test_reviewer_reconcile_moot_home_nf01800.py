"""NF-2026-01800: a finished reviewer whose home is gone is moot, not pending.

Measured on 0.12.74: a vscode_lm reviewer exited 0 with a verified receipt,
its card was auto-accepted and archived (home removed), and the bridge cancel
published afterwards failed 3/3 with ``FileNotFoundError``. The moot check
demanded ``PidIdentityVerdict.MISMATCH``, but an exited supervisor is not
reliably MISMATCH (see ``test_exited_process_identity_verdict_is_measured``),
so the request sat in ``reconcile_pending`` and accept_preview reported the
lens unusable forever.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import process_launcher, process_launcher_accept_review, runtime_temp


_SPEC = importlib.util.spec_from_file_location(
    "nf01574_bridge_orphan_setup",
    Path(__file__).with_name("test_reconcile_bridge_cancel_orphan_nf01574.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_NF01574 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_NF01574)

REQUEST_ID = _NF01574.REQUEST_ID
TASK_ID = _NF01574.TASK_ID
PARENT_TASK_ID = "NF01800_PARENT_TASK"
PARENT_REQUEST_ID = "c" * 32
CLAIM_EPOCH = 3
LENS = "correctness"
MOOT = "bridge_cancel_moot_home_removed"
Verdict = process_launcher.PidIdentityVerdict


def _manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    card: dict,
    verdict: Verdict,
    **kwargs,
) -> process_launcher.ProcessManager:
    manager = _NF01574._setup(  # noqa: SLF001 - the exact persisted nf01574 shape
        monkeypatch, tmp_path, card=card, verdict=verdict, **kwargs
    )
    # Liveness reads its own probe; pin it so "dead" never depends on the host.
    monkeypatch.setattr(process_launcher, "_pid_matches", lambda *_args: False)
    monkeypatch.setattr(
        process_launcher.storage_retention,
        "schedule_repository_cleanup",
        lambda _repo: None,
    )
    return manager


def _rows(manager: process_launcher.ProcessManager) -> list[dict]:
    return manager._request_events(REQUEST_ID)  # noqa: SLF001


def _moot(manager: process_launcher.ProcessManager) -> str | None:
    events = _rows(manager)
    latest = {**manager._event_identity(events), **events[-1]}  # noqa: SLF001
    status = {
        "request_id": REQUEST_ID,
        "state": latest.get("state"),
        "adapter_id": latest.get("adapter_id"),
        "latest_event": latest,
    }
    return manager._bridge_cancel_moot_home_removed(  # noqa: SLF001
        REQUEST_ID, events, latest, None, status
    )


def test_exited_process_identity_verdict_is_measured() -> None:
    """The verdict ``_request_pid_identity`` sees for a cleanly exited process.

    Windows: MATCH while any handle to the exited process stays open (here the
    ``Popen`` itself, in production the monitoring manager's), because
    OpenProcess succeeds and the creation time is still readable. POSIX: a
    reaped child is MISMATCH (``ProcessLookupError``). Liveness says dead on
    both, which is why the moot decision must not hinge on the verdict.
    """
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
    ) as proc:
        ticks = runtime_temp.process_start_ticks(proc.pid)
        assert ticks is not None
        assert process_launcher._pid_identity_evidence(  # noqa: SLF001
            proc.pid, ticks
        ).verdict is Verdict.MATCH
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=60) == 0

        evidence = process_launcher._pid_identity_evidence(proc.pid, ticks)  # noqa: SLF001
        assert process_launcher._pid_matches(proc.pid, ticks) is False  # noqa: SLF001
        expected = Verdict.MATCH if os.name == "nt" else Verdict.MISMATCH
        assert evidence.verdict is expected, evidence
        # Missing start ticks can never be decided on any platform.
        assert process_launcher._pid_identity_evidence(  # noqa: SLF001
            proc.pid, None
        ).verdict is Verdict.UNKNOWN


@pytest.mark.parametrize("card_status", ["finished", "archived"])
@pytest.mark.parametrize("verdict", [Verdict.MATCH, Verdict.UNKNOWN])
def test_decided_card_with_removed_home_is_moot_for_any_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdict: Verdict,
    card_status: str,
) -> None:
    manager = _manager(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": card_status},
        verdict=verdict,
    )

    assert _moot(manager) == MOOT

    # The real publication fails (the home is gone) and lands terminal.
    with pytest.raises(process_launcher._BridgeCancellationDeferred):  # noqa: SLF001
        manager._publish_bridge_cancellation_before_finalization(REQUEST_ID)  # noqa: SLF001
    rows = _rows(manager)
    assert [row for row in rows if row.get("state") == "reconcile_pending"] == []
    assert rows[-1]["state"] == "finalize_abandoned"
    assert rows[-1]["bridge_cancel_moot"] == MOOT
    assert rows[-1]["bridge_provider_may_be_active"] is False


def test_match_verdict_sweep_finalizes_instead_of_deferring(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _manager(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": "archived"},
        verdict=Verdict.MATCH,
    )

    sweep = manager._reconcile_persisted_requests  # noqa: SLF001
    assert sweep() == {"watched": 0, "finalized": 1}
    rows = _rows(manager)
    assert rows[-1]["state"] == "finalize_abandoned"
    assert [row for row in rows if row.get("state") == "reconcile_pending"] == []


def test_present_home_still_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _manager(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": "archived"},
        verdict=Verdict.MATCH,
        home_exists=True,
    )
    assert _moot(manager) is None


@pytest.mark.parametrize("verdict", [Verdict.MATCH, Verdict.MISMATCH])
def test_unfinished_card_bound_to_this_request_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdict: Verdict,
) -> None:
    manager = _manager(
        monkeypatch,
        tmp_path,
        card={
            "task_id": TASK_ID,
            "status": "processing",
            "launch_request_id": REQUEST_ID,
        },
        verdict=verdict,
    )
    assert _moot(manager) is None


def test_card_bound_elsewhere_still_requires_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    card = {"task_id": TASK_ID, "status": "processing", "launch_request_id": "a" * 32}
    # Each setup re-points the bridge root, so each is probed before the next.
    mismatch = _manager(monkeypatch, tmp_path / "m", card=card, verdict=Verdict.MISMATCH)
    assert _moot(mismatch) == MOOT
    unknown = _manager(monkeypatch, tmp_path / "u", card=card, verdict=Verdict.UNKNOWN)
    assert _moot(unknown) is None
    match = _manager(monkeypatch, tmp_path / "a", card=card, verdict=Verdict.MATCH)
    assert _moot(match) is None


def test_probe_exception_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _manager(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": "archived"},
        verdict=Verdict.MATCH,
    )

    def _raise(_task_id: str) -> dict:
        raise RuntimeError("task store unavailable")

    monkeypatch.setattr(manager, "_show_task", _raise)
    assert _moot(manager) is None


def test_live_child_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _manager(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": "archived"},
        verdict=Verdict.MATCH,
    )
    monkeypatch.setattr(process_launcher, "_pid_matches", lambda *_args: True)
    assert _moot(manager) is None


# --- an EXISTING record already stuck in reconcile_pending -----------------


def _receipt() -> dict:
    return {
        "target": {"request_id": PARENT_REQUEST_ID, "task_id": PARENT_TASK_ID},
        "reviewer": {"request_id": REQUEST_ID, "task_id": TASK_ID},
        "report": {"lens": LENS, "findings": []},
        "authority": {"verified": True},
    }


def _stuck_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    card_receipt: dict,
) -> process_launcher.ProcessManager:
    """running -> accepted (home removed) -> reconcile_pending, as measured."""
    card = {
        "task_id": TASK_ID,
        "status": "archived",
        "topic": "quality_review",
        "claim_epoch": CLAIM_EPOCH,
        "accepted_request_id": REQUEST_ID,
        "accept_evidence": {
            "accepted_outcome_receipt": {
                "request_id": REQUEST_ID,
                "claim_epoch": CLAIM_EPOCH,
            },
            "quality_review_receipt": card_receipt,
        },
        "terminal_review": {"evidence": {"quality_review_receipt": card_receipt}},
    }
    manager = _manager(monkeypatch, tmp_path, card=card, verdict=Verdict.MISMATCH)
    metadata_path = Path(_rows(manager)[0]["metadata_path"])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(task_id=TASK_ID, topic="quality_review", claim_epoch=CLAIM_EPOCH)
    process_launcher.write_json_0600(metadata_path, metadata)
    lineage = {
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "runner": "worker",
        "topic": "quality_review",
        "adapter_id": "vscode_lm",
    }
    manager._retention_event({  # noqa: SLF001 - the standalone reviewer accept row
        **lineage,
        "state": "accepted",
        "accepted": True,
        "promoted_paths": [],
        "quality_review_receipt": _receipt(),
        "finished_at": "2026-10-27T00:00:00+00:00",
    }, disposition="removed")
    manager._retention_event({  # noqa: SLF001 - the failed bridge cancel after it
        **lineage,
        "state": "reconcile_pending",
        "metadata_path": str(metadata_path),
        "supervisor_status_path": _rows(manager)[0]["supervisor_status_path"],
        "bridge_provider_may_be_active": True,
        "bridge_cancel_status": "failed",
        "reconciliation_deferred": "bridge_cancel_publication_failed",
        "error": "bridge_cancel_publication_failed:attempt=1:FileNotFoundError",
    }, disposition="retained_in_place")
    monkeypatch.setattr(
        process_launcher_accept_review,
        "bound_reviewer_rows",
        lambda *_args: [{
            "task_id": TASK_ID,
            "lens": LENS,
            "packet_sha256": "",
            "terminal_substatus": "review_ready",
            "receipt": card_receipt,
        }],
    )
    return manager


def _per_lens(manager: process_launcher.ProcessManager) -> dict:
    reviewers = process_launcher_accept_review.reviewer_evidence(
        manager, PARENT_TASK_ID, PARENT_REQUEST_ID
    )
    fold = process_launcher_accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile={"required_reviewer_lenses": [LENS]},
        terminal_substatus="review_ready",
    )
    assert len(fold["per_lens"]) == 1, fold
    return {**fold["per_lens"][0], "blocker_kinds": [b["kind"] for b in fold["blockers"]]}


@pytest.mark.parametrize("reconcile_pass", ["status", "sweep"])
def test_stuck_reconcile_pending_reviewer_is_restored_and_usable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reconcile_pass: str,
) -> None:
    manager = _stuck_manager(monkeypatch, tmp_path, card_receipt=_receipt())
    before = _per_lens(manager)
    assert before["state"] == "reconcile_pending"
    assert before["usable"] is False
    assert before["has_verified_report"] is True

    if reconcile_pass == "status":
        manager.status(REQUEST_ID)
    else:
        manager._reconcile_persisted_requests()  # noqa: SLF001

    rows = _rows(manager)
    assert rows[-1]["state"] == "accepted"
    assert rows[-1]["accepted"] is True
    assert rows[-1]["quality_review_receipt"] == _receipt()
    assert rows[-1]["restored_over_state"] == "reconcile_pending"
    assert rows[-1]["restored_over_reason"] == "bridge_cancel_publication_failed"
    after = _per_lens(manager)
    assert after["state"] == "accepted"
    assert after["usable"] is True
    assert after["has_verified_report"] is True
    assert "required_reviewer_missing" not in after["blocker_kinds"]

    # Idempotent: the restored terminal row is never re-candidated.
    manager._reconcile_persisted_requests()  # noqa: SLF001
    assert _rows(manager) == rows


def test_stuck_reviewer_with_unverified_receipt_is_not_restored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tampered = {**_receipt(), "report": {"lens": LENS, "findings": ["edited"]}}
    manager = _stuck_manager(monkeypatch, tmp_path, card_receipt=tampered)

    manager._reconcile_persisted_requests()  # noqa: SLF001

    assert _rows(manager)[-1]["state"] == "reconcile_pending"
    after = _per_lens(manager)
    assert after["usable"] is False
    assert "required_reviewer_missing" in after["blocker_kinds"]
