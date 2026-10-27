"""NF-2026-01730: reviewers launch before the finalized-workspace GC sweep, and
the sweep reads the full ledger projection once instead of once per candidate.

Measured on the canonical repo: a ``gc_included`` reconcile took 1321.7 s
(``gc_cleaned=0``) against 2.7 s without GC, and because the review drain ran
after the sweep no reviewer for a ``review_ready`` card could launch for ~22
minutes.  The sweep paid one full multi-segment ledger reparse per candidate.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SIBLING = Path(__file__).with_name("test_finalized_workspace_gc_b512_v1.py")
_SPEC = importlib.util.spec_from_file_location("nf01730_b512_fixtures", _SIBLING)
assert _SPEC is not None and _SPEC.loader is not None
_B512 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_B512)

process_launcher = _B512.process_launcher
worker_workspace = _B512.worker_workspace
from aiworkhub import process_event_ledger  # noqa: E402

STALE = "latest_event_changed_since_snapshot"
RESULT_KEYS = {
    "ok", "reservations_retired", "terminal_intents_settled", "watched", "finalized",
    "ambiguous_claims_recovered", "gc_scanned", "gc_cleaned", "gc_skipped",
    "attempted", "completed", "failed", "pending", "review_actions",
    "automation_retried", "automation_seeded", "automation_failed", "automation_failures",
}


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch):
    if _B512._chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


# --- reconcile ordering -------------------------------------------------------


def _ordered_reconcile_manager(tmp_path, monkeypatch, order: list[str]):
    manager = process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=False,
    )
    review_db = tmp_path / "canonical-task-queue.sqlite"
    monkeypatch.setattr(
        process_launcher.review_orchestrator, "canonical_review_db", lambda _m: review_db
    )
    monkeypatch.setattr(
        manager, "_reconcile_persisted_requests", lambda: {"watched": 0, "finalized": 0}
    )

    def _retry(_manager, *, db_path, events):
        order.append("retry_pending_registrations")
        return dict(automation_retried=1, automation_seeded=1, automation_failed=0,
                    automation_failures=[])

    def _drain(_self, *args, **kwargs):
        order.append("drain")
        return SimpleNamespace(as_dict=lambda: dict(
            attempted=2, completed=2, failed=0, pending=0, review_actions={"launch": 2}))

    def _slow_gc():
        order.append("gc_start")
        time.sleep(0.25)
        order.append("gc_end")
        return {"gc_scanned": 3, "gc_cleaned": 1, "gc_skipped": 2}

    monkeypatch.setattr(
        process_launcher.review_orchestrator, "retry_pending_registrations", _retry
    )
    monkeypatch.setattr(process_launcher.review_orchestrator.ReviewOrchestrator, "drain", _drain)
    monkeypatch.setattr(manager, "_gc_finalized_workspaces", _slow_gc)
    return manager


def test_reconcile_drains_reviews_before_a_slow_gc_sweep(tmp_path, monkeypatch):
    order: list[str] = []
    manager = _ordered_reconcile_manager(tmp_path, monkeypatch, order)

    result = manager.reconcile(include_gc=True)

    assert order == ["retry_pending_registrations", "drain", "gc_start", "gc_end"]
    assert set(result) == RESULT_KEYS
    assert (result["gc_scanned"], result["gc_cleaned"], result["gc_skipped"]) == (3, 1, 2)
    assert (result["attempted"], result["completed"], result["review_actions"]) == (
        2, 2, {"launch": 2})
    assert (result["automation_retried"], result["automation_seeded"]) == (1, 1)


def test_reconcile_without_gc_still_drains_and_reports_zero_gc(tmp_path, monkeypatch):
    order: list[str] = []
    manager = _ordered_reconcile_manager(tmp_path, monkeypatch, order)

    result = manager.reconcile(include_gc=False)

    assert order == ["retry_pending_registrations", "drain"]
    assert set(result) == RESULT_KEYS
    assert (result["gc_scanned"], result["gc_cleaned"], result["gc_skipped"]) == (0, 0, 0)


# --- one projection read per sweep -------------------------------------------


def _finished_manager(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _B512._card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    return _B512._build_manager(tmp_path, card), card


def _seed(manager, tmp_path, card, index: int):
    return _B512._seed_gc_candidate(
        manager, tmp_path, card,
        request_id=f"req-nf01730-{index}",
        pid=2_147_483_100 + index, pid_start_ticks=999_999_800 + index,
        state="validation_failed",
    )


def _count_calls(monkeypatch, owner, name: str) -> list[int]:
    calls = [0]
    real = getattr(owner, name)

    def counting(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, counting)
    return calls


def test_sweep_reads_the_full_projection_once_over_many_candidates(tmp_path, monkeypatch):
    manager, card = _finished_manager(tmp_path, monkeypatch)
    seeded = [_seed(manager, tmp_path, card, index) for index in range(6)]
    process_event_ledger.reset_request_event_cache()
    projection_reads = _count_calls(monkeypatch, manager, "_latest_by_request")
    full_passes = _count_calls(monkeypatch, process_event_ledger, "iter_events")

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 6, "gc_cleaned": 6, "gc_skipped": 0}
    assert all(not path.exists() and not home.exists() for path, home, _ in seeded)
    # Every cleanup appended a retention row, so each later re-check saw a
    # grown ledger: still one projection read and at most one full pass.
    assert projection_reads[0] == 1
    assert full_passes[0] <= 1


# --- a candidate that changed after the snapshot is skipped -------------------


def _late_row_before_lock(monkeypatch, manager, publish):
    """Land ``publish()`` after the sweep's snapshot, just before the lock."""
    real_lock = manager._request_lock
    published: list[str] = []

    @contextlib.contextmanager
    def _lock(request_id, **kwargs):
        if not published:
            published.append(request_id)
            publish(request_id)
        with real_lock(request_id, **kwargs):
            yield

    monkeypatch.setattr(manager, "_request_lock", _lock)
    return published


def _newer_row(manager, request_id: str) -> dict:
    """The request's latest row again, still a GC candidate but a NEWER row."""
    row = dict(manager._latest_by_request()[request_id])
    row.pop("timestamp", None)  # ``_append_event`` stamps the new row itself
    row["note"] = "concurrent"
    return row


def test_candidate_whose_latest_event_changed_after_the_snapshot_is_skipped(
    tmp_path, monkeypatch,
):
    manager, card = _finished_manager(tmp_path, monkeypatch)
    path, home, _ = _seed(manager, tmp_path, card, 0)
    published = _late_row_before_lock(
        monkeypatch, manager, lambda rid: manager._append_event(_newer_row(manager, rid)),
    )

    result = manager._gc_finalized_workspaces()

    assert published == ["req-nf01730-0"]
    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists(), "a changed request is skipped, never cleaned"
    # The skip lasts one pass: the next snapshot sees the newer row and acts on it.
    assert manager._gc_finalized_workspaces()["gc_cleaned"] == 1
    assert not path.exists() and not home.exists()


def test_a_newer_row_published_as_a_spill_segment_is_seen_by_the_recheck(
    tmp_path, monkeypatch,
):
    manager, card = _finished_manager(tmp_path, monkeypatch)
    path, home, _ = _seed(manager, tmp_path, card, 0)
    log = manager.process_log_path

    def _spill(rid):
        # Exactly what ``_write_immutable_spill`` publishes when the append lock is stuck.
        row = {**_newer_row(manager, rid), "timestamp": "2099-12-31T00:00:00.000000+00:00"}
        spill = log.with_name(f"{log.stem}.spill.20991231T000000.000000Z.1.nf01730{log.suffix}")
        spill.write_text(json.dumps(row) + "\n", encoding="utf-8")

    _late_row_before_lock(monkeypatch, manager, _spill)

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists()


def test_a_rotation_after_the_snapshot_fails_closed_instead_of_replaying(
    tmp_path, monkeypatch,
):
    manager, card = _finished_manager(tmp_path, monkeypatch)
    path, home, _ = _seed(manager, tmp_path, card, 0)
    log = manager.process_log_path

    def _rotate(_rid):
        log.rename(log.with_name(f"{log.stem}.20991231T000000.1.nf01730{log.suffix}"))
        log.write_text("", encoding="utf-8")

    _late_row_before_lock(monkeypatch, manager, _rotate)
    detail = manager._gc_finalized_workspace(
        "req-nf01730-0", manager._latest_by_request()["req-nf01730-0"],
        snapshot=process_launcher.GcSweepSnapshot(log, manager._ledger_generation()),
    )

    assert detail == {
        "request_id": "req-nf01730-0", "gc": False, "reason": "ledger_snapshot_unverifiable",
    }
    assert path.exists() and home.exists()


def test_direct_call_with_a_stale_snapshot_row_is_skipped(tmp_path, monkeypatch):
    manager, card = _finished_manager(tmp_path, monkeypatch)
    path, home, _ = _seed(manager, tmp_path, card, 0)
    snapshot_row = manager._latest_by_request()["req-nf01730-0"]
    manager._append_event(_newer_row(manager, "req-nf01730-0"))

    detail = manager._gc_finalized_workspace("req-nf01730-0", snapshot_row)

    assert detail == {"request_id": "req-nf01730-0", "gc": False, "reason": STALE}
    assert path.exists() and home.exists()


# --- measurement --------------------------------------------------------------


def test_gc_included_sweep_duration_at_scale(tmp_path, monkeypatch, capsys):
    """One sweep over a ledger shaped like the canonical one: thousands of
    requests, hundreds of retained candidates kept by their card (gc_cleaned=0),
    a persistent spill segment, and another writer appending between candidates
    -- the state in which every re-read was a full multi-segment reparse."""
    requests, candidates = 6000, 300
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _B512._card()  # processing: every candidate is kept, as measured
    manager = _B512._build_manager(tmp_path, card)
    repo, process_dir, log = tmp_path / "repo", tmp_path / "processes", manager.process_log_path
    repo.mkdir(parents=True, exist_ok=True)
    process_dir.mkdir(parents=True, exist_ok=True)
    stamp = "2026-10-27T00:00:00.000000+00:00"
    lines: list[str] = []
    for index in range(requests):
        rid = f"{index:032x}"
        base = {"schema_id": "aiworkhub.task_mcp.process_event.v1", "timestamp": stamp,
                "request_id": rid, "task_id": card["task_id"], "runner": card["runner"],
                "topic": card["topic"], "adapter_id": "claude_cli",
                "pid": 2_147_480_000, "pid_start_ticks": 1}
        lines.append(json.dumps({**base, "state": "running"}))
        if index < candidates:
            path = tmp_path / "wtroot" / rid / "worktree"
            home = tmp_path / "wtroot" / rid / "home"
            path.mkdir(parents=True)
            home.mkdir(parents=True)
            metadata = process_dir / f"{rid}.request.json"
            metadata.write_text(json.dumps({"workspace": {
                "request_id": rid, "repo": str(repo), "path": str(path), "home": str(home),
                "allowed_writes": list(card["allowed_writes"]),
                "parent_baseline": {}, "workspace_baseline": {},
            }}), encoding="utf-8")
            lines.append(json.dumps({**base, "state": "validation_failed",
                                     "workspace_retained": True, "metadata_path": str(metadata)}))
        else:
            lines.append(json.dumps({**base, "state": "accepted", "workspace_retained": False}))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.with_name(f"{log.stem}.spill.20261026T000000.000000Z.1.nf01730{log.suffix}").write_text(
        json.dumps({"request_id": "spilled", "state": "running", "timestamp": stamp}) + "\n",
        encoding="utf-8")
    appended = [0]
    real_show = manager._show_task

    def _show_with_concurrent_writer(task_id):
        appended[0] += 1
        manager._append_event({"request_id": f"other-{appended[0]}", "state": "exited"})
        return real_show(task_id)

    monkeypatch.setattr(manager, "_show_task", _show_with_concurrent_writer)
    manager._latest_by_request()  # the reconcile pass's earlier steps warm it
    projection_reads = _count_calls(monkeypatch, manager, "_latest_by_request")
    full_passes = _count_calls(monkeypatch, process_event_ledger, "iter_events")

    started = time.perf_counter()
    result = manager._gc_finalized_workspaces()
    elapsed = time.perf_counter() - started

    with capsys.disabled():
        print(
            f"\nNF-2026-01730 gc sweep: requests={requests} candidates={candidates} "
            f"seconds={elapsed:.3f} projection_reads={projection_reads[0]} "
            f"full_ledger_passes={full_passes[0]} result={result}"
        )
    assert result == {"gc_scanned": candidates, "gc_cleaned": 0, "gc_skipped": candidates}
    assert appended[0] == candidates
    assert projection_reads[0] == 1
    assert full_passes[0] <= 1
    assert elapsed < 60.0
