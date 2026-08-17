"""Terminal-log quarantine directories no batch record claims must be reconcilable.

The canonical store held 3.68 GB behind batches whose manifests said
``status: empty, bytes: 0`` while gigabytes sat in their directories -- a
record-versus-disk disagreement no purge would ever act on, so the bytes
stranded permanently (the same shape as the earlier stranded worktrees, in a
different store). Two invariants are proven here:

1. The path is closed: a batch whose record reads empty while its directory
   still holds files is never treated as a harmless empty batch. It keeps its
   full undo window, refuses an early purge, and is surfaced as ``unclaimed``
   with its real on-disk byte figure rather than as "0 bytes".
2. There is an operator-reachable reconciliation: ``reconcile_unclaimed`` adopts
   every such directory (ownership proved by its location inside this
   repository's own quarantine root, never by a name or an age) into a fresh,
   bounded batch that flows through the ordinary purge/enforce expiry. Nothing
   is deleted by reconciliation; the bytes merely become reclaimable.

``os.chmod`` is denied in the validation sandbox, so the confirm paths that
write a manifest through ``_atomic_json`` are exercised by the coordinator's
canonical run; the read-only reporting, the close-the-path refusal, and the
empty-batch collector run everywhere.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import task_store, terminal_log_retention


_FROZEN_NOW = datetime(2035, 1, 1, tzinfo=timezone.utc)
# Well past the frozen clock: any reap of such a batch is because it is empty,
# never because the deadline expired.
_FUTURE_DEADLINE = "2040-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _freeze_retention_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_log_retention, "now_utc", lambda: _FROZEN_NOW)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _write_batch(
    repo: Path,
    batch_id: str,
    *,
    item_states: tuple[str, ...],
    deadline: str = _FUTURE_DEADLINE,
    status: str = "empty",
    payload: tuple[tuple[str, str], ...] = (),
    with_manifest: bool = True,
) -> Path:
    """Materialise a quarantine batch directory.

    ``item_states`` populate the manifest's item records; ``payload`` writes real
    files onto disk *regardless* of what the record says, so a test can build the
    exact drift where the manifest records nothing while bytes physically sit in
    the directory.
    """
    repo_id = task_store.storage_readiness(repo).repo_id
    batch = repo / terminal_log_retention.QUARANTINE_RELATIVE_PATH / batch_id
    batch.mkdir(parents=True)
    items = []
    for index, state in enumerate(item_states):
        request_id = f"{index:032x}"
        (batch / request_id).mkdir()
        items.append({
            "request_id": request_id,
            "state": state,
            "files": [{"name": f"{request_id}.stdout.log", "size_bytes": 0, "mtime_ns": 1}],
        })
    for relpath, content in payload:
        target = batch / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if with_manifest:
        manifest = {
            "schema_id": terminal_log_retention.SCHEMA_ID,
            "repo_id": repo_id,
            "batch_id": batch_id,
            "created_at": "2035-01-01T00:00:00+00:00",
            "restore_deadline": deadline,
            "preview_digest": "0" * 64,
            "status": status,
            "items": items,
            "quarantined_files": 0,
            "quarantined_bytes": 0,
        }
        (batch / terminal_log_retention.MANIFEST_NAME).write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    return batch


def _payload_files(repo: Path) -> set[str]:
    qroot = repo / terminal_log_retention.QUARANTINE_RELATIVE_PATH
    return {
        str(path.relative_to(qroot))
        for path in qroot.rglob("*")
        if path.is_file() and path.name != terminal_log_retention.MANIFEST_NAME
    }


# --- close-the-path: record-empty but disk-nonempty is never a harmless purge --

def test_list_batches_reports_unclaimed_record_empty_disk_nonempty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_batch(
        repo, "l20260802T163426-5b12d921bd90",
        item_states=("skipped_identity_changed",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "x" * 4096),),
    )

    row = terminal_log_retention.list_batches(repo)["batches"][0]

    # The real bytes are reported, not "0" -- so the panel can never read as
    # "nothing here" while the directory holds files.
    assert row["on_disk_bytes"] == 4096
    assert row["bytes"] == 4096
    assert row["unclaimed"] is True
    # A batch holding bytes is never an early-purge candidate on a stale record.
    assert row["purge_eligible"] is False


def test_purge_refuses_a_record_empty_batch_that_still_holds_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    batch_id = "l20260802T163426-5b12d921bd90"
    _write_batch(
        repo, batch_id,
        item_states=("skipped_identity_changed",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "x" * 4096),),
    )

    with pytest.raises(
        terminal_log_retention.TerminalLogRetentionError,
        match="retention_undo_window_active",
    ):
        terminal_log_retention.purge(repo, batch_id=batch_id, confirm=True)

    # The files are untouched: the close-the-path guard never drops bytes on a
    # record that claims they are absent.
    assert _payload_files(repo) == {f"{batch_id}/{0:032x}/{0:032x}.stdout.log"}


# --- reconcile: read-only report and adoption -------------------------------

def test_reconcile_dry_run_reports_unclaimed_without_moving_anything(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_batch(
        repo, "l20260802T163426-5b12d921bd90",
        item_states=("skipped_identity_changed",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "y" * 2048),),
    )
    before = _payload_files(repo)

    report = terminal_log_retention.reconcile_unclaimed(repo)

    assert report["dry_run"] is True
    assert report["unclaimed_count"] == 1
    assert report["unclaimed_bytes"] == 2048
    assert report["unclaimed"][0]["reason"] == "record_empty_disk_nonempty"
    # A read-only report never mutates the store.
    assert _payload_files(repo) == before


def test_reconcile_lists_a_batch_shaped_dir_that_has_no_manifest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_batch(
        repo, "l20260802T163426-5b12d921bd90",
        item_states=(), with_manifest=False,
        payload=(("orphan/output.log", "z" * 512),),
    )

    report = terminal_log_retention.reconcile_unclaimed(repo)

    assert report["unclaimed_count"] == 1
    assert report["unclaimed"][0]["reason"] == "manifest_absent"
    assert report["unclaimed_bytes"] == 512


def test_reconcile_never_lists_a_healthy_claimed_batch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # A batch whose record claims a quarantined item (its file physically present)
    # is a healthy batch, not an unclaimed one -- reconcile must leave it alone.
    _write_batch(
        repo, "l20260802T163426-00000000aaaa",
        item_states=("quarantined",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "kept"),),
    )

    report = terminal_log_retention.reconcile_unclaimed(repo)

    assert report["unclaimed_count"] == 0


def test_reconcile_confirm_requires_a_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_batch(
        repo, "l20260802T163426-5b12d921bd90",
        item_states=("skipped_identity_changed",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "q" * 128),),
    )

    with pytest.raises(
        terminal_log_retention.TerminalLogRetentionError,
        match="terminal_log_reconcile_reason_required",
    ):
        terminal_log_retention.reconcile_unclaimed(repo, confirm=True, reason="  ")


def test_reconcile_confirm_adopts_unclaimed_bytes_into_a_bounded_batch(tmp_path: Path) -> None:
    # ``os.chmod`` is denied in the sandbox; the manifest write goes through
    # ``_atomic_json`` and is exercised by the coordinator's canonical run.
    repo = _repo(tmp_path)
    _write_batch(
        repo, "l20260802T163426-5b12d921bd90",
        item_states=("skipped_identity_changed",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "w" * 8192),),
    )

    report = terminal_log_retention.reconcile_unclaimed(
        repo, confirm=True, reason="strand recovery"
    )

    assert report["ok"] is True
    assert report["reconciled_count"] == 1
    assert report["reconciled_bytes"] >= 8192
    new_batch = report["batch_id"]
    assert new_batch

    # The adopted batch now truthfully claims its bytes: not unclaimed, refuses an
    # early purge inside the fresh undo window, and is collected once expired.
    row = next(
        r for r in terminal_log_retention.list_batches(repo)["batches"]
        if r["batch_id"] == new_batch
    )
    assert row["unclaimed"] is False
    assert row["bytes"] >= 8192
    assert row["purge_eligible"] is False
    with pytest.raises(
        terminal_log_retention.TerminalLogRetentionError,
        match="retention_undo_window_active",
    ):
        terminal_log_retention.purge(repo, batch_id=new_batch, confirm=True)


# --- purge_empty_batches: the named collector for genuinely empty batches -----

def test_purge_empty_batches_collects_only_truly_empty_and_spares_unclaimed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    # A genuinely empty batch: record empty AND no file on disk.
    _write_batch(repo, "l20260816T101142-000000000001", item_states=("skipped_identity_changed",) * 3)
    # An unclaimed batch: record empty but a file physically present.
    _write_batch(
        repo, "l20260816T101142-000000000002",
        item_states=("skipped_identity_changed",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "held"),),
    )

    result = terminal_log_retention.purge_empty_batches(repo, confirm=True)

    assert result["batch_ids"] == ["l20260816T101142-000000000001"]
    assert result["purged"] == 1
    remaining = {r["batch_id"] for r in terminal_log_retention.list_batches(repo)["batches"]}
    assert remaining == {"l20260816T101142-000000000002"}


def test_purge_empty_batches_requires_confirmation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(
        terminal_log_retention.TerminalLogRetentionError,
        match="explicit_confirmation_required",
    ):
        terminal_log_retention.purge_empty_batches(repo, confirm=False)


# --- ordering: a reconcile batch is never adoptable while it is being built -----

def test_a_batch_observed_mid_construction_is_never_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reconcile batch writes its claiming manifest BEFORE moving any payload in
    # (as ``quarantine`` does), so a concurrent scan never observes the
    # batch-shaped-directory-with-bytes-but-no-claiming-record signature it
    # adopts. Driven deterministically: an observer runs ``_scan_unclaimed`` in
    # the window immediately after each move rather than hoping two threads
    # interleave. (``os.chmod`` is denied in the validation sandbox, so the
    # confirm path's ``_atomic_json`` manifest writes run under the coordinator's
    # canonical suite; the observer wiring itself is exercised everywhere.)
    repo = _repo(tmp_path)
    _write_batch(
        repo, "l20260802T163426-aaaaaaaaaaaa",
        item_states=("skipped_identity_changed",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "a" * 4096),),
    )
    _write_batch(
        repo, "l20260802T163426-bbbbbbbbbbbb",
        item_states=("skipped_identity_changed",),
        payload=((f"{0:032x}/{0:032x}.stdout.log", "b" * 4096),),
    )

    snapshots: list[set[str]] = []

    def _observe(_batch: Path) -> None:
        # Exactly what a concurrent reconcile/purge would see mid-construction.
        snapshots.append(
            {row["name"] for row in terminal_log_retention._scan_unclaimed(repo)}
        )

    monkeypatch.setattr(terminal_log_retention, "_RECONCILE_MOVE_OBSERVER", _observe)

    report = terminal_log_retention.reconcile_unclaimed(
        repo, confirm=True, reason="mid-construction adoption guard"
    )

    new_batch = report["batch_id"]
    assert new_batch
    # The observer fired inside the move window (once per moved subtree).
    assert snapshots
    # In no in-window scan is the batch under construction ever adoptable: its
    # manifest was written first, so its record already claims content and
    # ``_scan_unclaimed`` skips it.
    for names in snapshots:
        assert new_batch not in names
    # The scan was genuinely running, not vacuously empty: the first snapshot,
    # taken after only the first move, still lists the second, untouched
    # unclaimed source -- so the batch's absence is discrimination, not silence.
    assert "l20260802T163426-bbbbbbbbbbbb" in snapshots[0]


# --- the empty-purge count promises exactly what the collector drains ----------

_PAST_DEADLINE = "2020-01-01T00:00:00+00:00"


def test_empty_eligible_count_matches_what_purge_empty_batches_reaps(
    tmp_path: Path,
) -> None:
    # The operator-facing empty-purge count must equal what ``purge_empty_batches``
    # -- the trigger named beside it -- actually drains, never the wider
    # ``purge_eligible`` population. A batch past its deadline that still holds
    # files is ``purge_eligible`` yet is reaped by ``enforce`` on expiry, never by
    # ``purge_empty_batches``; counting it would promise more than the named
    # trigger delivers.
    repo = _repo(tmp_path)
    # Genuinely empty: record-empty AND disk-empty -> the collector reaps it.
    _write_batch(
        repo, "l20260816T101142-000000000001",
        item_states=("skipped_identity_changed",) * 2,
    )
    # Expired but still full: past its deadline, record claims a quarantined item
    # whose file is physically present -> purge_eligible, but NOT reapable_empty.
    _write_batch(
        repo, "l20260816T101142-000000000002",
        item_states=("quarantined",), deadline=_PAST_DEADLINE,
        payload=((f"{0:032x}/{0:032x}.stdout.log", "held"),),
    )

    rows = {
        r["batch_id"]: r for r in terminal_log_retention.list_batches(repo)["batches"]
    }
    full_expired = rows["l20260816T101142-000000000002"]
    assert full_expired["purge_eligible"] is True
    assert full_expired["reapable_empty"] is False
    assert rows["l20260816T101142-000000000001"]["reapable_empty"] is True

    # The figure the dashboard surfaces (sum of ``reapable_empty``) equals exactly
    # what the named collector drains -- and the expired-full batch is neither
    # counted nor reaped.
    empty_purge_eligible_count = sum(
        1 for r in rows.values() if r.get("reapable_empty")
    )
    result = terminal_log_retention.purge_empty_batches(repo, confirm=True)
    assert empty_purge_eligible_count == result["purged"] == 1
    assert result["batch_ids"] == ["l20260816T101142-000000000001"]
