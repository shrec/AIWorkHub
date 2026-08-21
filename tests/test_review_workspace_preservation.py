"""NF-2026-00300 / NF-2026-00221: review-workspace preservation and claim order.

A review workspace failing its integrity check must be quarantined (never
destroyed), every removal must be recorded in the retention audit, a card still
in the review queue must not be reclaimable by a background sweep, and a VS Code
LM claim must be cancelled before the request workspace it refers to is deleted.

These exercise the module-level seams directly; they never construct a real
ProcessManager or launch a process (forbidden in the sandbox).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import process_launcher as pl


def _seed_workspace(tmp_path: Path, request_id: str = "req123") -> tuple[Path, Path, Path]:
    base = tmp_path / request_id
    worktree = base / "worktree"
    home = base / "home"
    worktree.mkdir(parents=True)
    home.mkdir(parents=True)
    sealed = worktree / "answer.py"
    sealed.write_text("VERIFIED = 1\n", encoding="utf-8")
    return worktree, home, sealed


class _FakeWorkspace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.request_id = "req123"


def test_failed_integrity_workspace_is_quarantined_not_deleted(tmp_path: Path) -> None:
    process_log = tmp_path / "state" / "process_events.jsonl"
    process_log.parent.mkdir(parents=True)
    worktree, home, sealed = _seed_workspace(tmp_path)

    # Detection is correct: the sealed hash no longer matches once the file is
    # mutated after review.  Only the RESPONSE changes -- the bytes survive.
    stored = pl._changed_path_hashes(_FakeWorkspace(worktree), ["answer.py"])
    sealed.write_text("VERIFIED = 999  # tampered\n", encoding="utf-8")
    observed = pl._changed_path_hashes(_FakeWorkspace(worktree), ["answer.py"])
    assert observed != stored  # integrity check still detects the mismatch

    dest = pl.quarantine_review_workspace(
        process_log,
        request_id="0123456789abcdef0123456789abcdef",
        path=worktree,
        home=home,
    )
    # The live tree is gone but the exact bytes survive in quarantine for a
    # manager to diff against the sealed hashes -- never straight to delete.
    assert not worktree.exists()
    assert dest.exists()
    quarantined = dest / "worktree" / "answer.py"
    assert quarantined.read_text(encoding="utf-8") == "VERIFIED = 999  # tampered\n"


def test_every_review_workspace_removal_is_recorded_in_retention_audit(tmp_path: Path) -> None:
    process_log = tmp_path / "state" / "process_events.jsonl"
    process_log.parent.mkdir(parents=True)

    quarantine_record = pl.record_review_workspace_retention_audit(
        process_log,
        request_id="req123",
        task_id="NF-2026-00300",
        card_status="review",
        reason="review_workspace_hash_mismatch",
        action="quarantine",
        moved_to=str(tmp_path / "q"),
    )
    purge_record = pl.record_review_workspace_retention_audit(
        process_log,
        request_id="req999",
        task_id="NF-OTHER",
        card_status="finished",
        reason="disposed_task_status:finished",
        action="purge",
    )

    audit_path = pl.review_workspace_retention_audit_path(process_log)
    rows = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 2
    assert rows[0] == quarantine_record
    assert rows[1] == purge_record
    for row in rows:
        # Every removal names the request id, card and reason.
        assert row["request_id"]
        assert row["task_id"]
        assert row["reason"]
        assert row["action"] in {"quarantine", "purge"}
    assert rows[0]["moved_to"] == str(tmp_path / "q")


def test_current_review_card_not_reclaimable_by_sweep() -> None:
    # A card still in the review queue whose terminal_review names this exact
    # request must not be eligible for a background sweep, on any ground.
    card = {
        "status": "review",
        "terminal_review": {
            "evidence": {"request_identity": {"request_id": "req-current"}},
        },
    }
    eligible, disposition = pl.ProcessManager._gc_disposition(card, "req-current")
    assert eligible is False
    assert disposition == "current_review_request"

    # A superseded request (different current request id) is genuinely disposable.
    eligible2, disposition2 = pl.ProcessManager._gc_disposition(card, "req-old")
    assert eligible2 is True
    assert disposition2.startswith("superseded_review_request:")


def test_lm_claim_cancelled_before_request_workspace_deleted(tmp_path: Path) -> None:
    order: list[str] = []

    class _FakeBridge:
        pass

    class _FakeWs:
        repo = tmp_path
        path = tmp_path / "worktree"
        home = tmp_path / "home"

    def fake_cancel(_req: object) -> None:
        order.append("cancel_claim")

    def fake_cleanup(_repo: object, _path: object, _home: object) -> None:
        order.append("cleanup_workspace")

    errors = pl._release_launch_request_resources(
        bridge_request=_FakeBridge(),
        workspace=_FakeWs(),
        cancel=fake_cancel,
        cleanup=fake_cleanup,
    )
    assert errors == []
    # The claim is cancelled BEFORE the workspace it refers to is deleted.
    assert order == ["cancel_claim", "cleanup_workspace"]


@pytest.mark.parametrize(
    "hostile_id",
    [
        "../escape",
        "..",
        ".",
        "a/b",
        "req123",
        "dead beef",
        "0xdeadbeef",
        "deadbeef",
        "A" * 32,
        "a" * 31,
        "a" * 33,
        "",
        None,
    ],
)
def test_quarantine_rejects_unsafe_request_id_before_destination_creation(
    tmp_path: Path, hostile_id: object
) -> None:
    process_log = tmp_path / "state" / "process_events.jsonl"
    process_log.parent.mkdir(parents=True)
    worktree, home, _ = _seed_workspace(tmp_path)
    quarantine_root = pl.review_workspace_quarantine_root(process_log)

    with pytest.raises(ValueError, match="unsafe request_id"):
        pl.quarantine_review_workspace(
            process_log,
            request_id=hostile_id,
            path=worktree,
            home=home,
        )

    assert not quarantine_root.exists()
    assert worktree.exists()
    assert home.exists()


def test_quarantine_accepts_canonical_request_id_and_preserves_collision_suffix(
    tmp_path: Path,
) -> None:
    process_log = tmp_path / "state" / "process_events.jsonl"
    process_log.parent.mkdir(parents=True)
    root = pl.review_workspace_quarantine_root(process_log)
    request_id = "0123456789abcdef0123456789abcdef"

    worktree, home, _ = _seed_workspace(tmp_path, request_id=request_id)
    first = pl.quarantine_review_workspace(
        process_log, request_id=request_id, path=worktree, home=home
    )
    assert first == root / request_id
    assert (first / "worktree" / "answer.py").read_text(encoding="utf-8") == "VERIFIED = 1\n"

    worktree_two, home_two, _ = _seed_workspace(
        tmp_path / "second", request_id=request_id
    )
    second = pl.quarantine_review_workspace(
        process_log, request_id=request_id, path=worktree_two, home=home_two
    )
    assert second == root / f"{request_id}.1"
    assert (second / "worktree" / "answer.py").exists()
