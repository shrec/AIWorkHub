"""Tests for NF-2026-02412: stateless process-ledger rows must never replace
the lifecycle row a reader (and the workspace janitor) folds per request.

``review_orchestrator`` appends ``auto_accepted`` / ``auto_accept_declined``
and ``review_orchestrator_lens_blocked`` rows that carry ``event_type`` but no
top-level ``state``. ``ProcessManager._latest_by_request`` folds with
``replace=True`` and skipped only ``runtime_notice`` plus the named
``ORCHESTRATOR_ADVISORY_EVENT_TYPES``, so such a row REPLACED a
``review_ready`` + ``workspace_retained`` row: the request read ``state=None``,
``gc_finalized_workspace`` returned None for it, and its worktree was never
collected. These tests pin (1) the three names in the skip set, (2) that an
UNNAMED sparse row (event_type, no state) stays a lifecycle delta, (3) a
janitor sweep that scans such a request again, and (4) that a projection or
archive checkpoint built under the old skip set is never served for the new.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import test_runtime_notice_lifecycle_readers_nf01194 as nf01194  # noqa: E402

from aiworkhub import launch_zero_delta, process_launcher  # noqa: E402
from aiworkhub import process_event_ledger as pel  # noqa: E402

NEW_STATELESS_TYPES = (
    "auto_accepted",
    "auto_accept_declined",
    "review_orchestrator_lens_blocked",
)
# The skip set ``_latest_by_request`` passed before NF-2026-02412.
OLD_SKIP = (
    launch_zero_delta.RUNTIME_NOTICE_EVENT_KIND,
    "review_orchestrator_mechanical_rework",
    "review_orchestrator_wait",
)
NEW_SKIP = (
    launch_zero_delta.RUNTIME_NOTICE_EVENT_KIND,
    *sorted(launch_zero_delta.ORCHESTRATOR_ADVISORY_EVENT_TYPES),
)
FOLD: dict[str, Any] = {"key_field": "request_id", "replace": True, "drop_fields": ()}


@pytest.fixture(autouse=True)
def _cold_projection_cache():
    pel.reset_latest_event_cache()
    yield
    pel.reset_latest_event_cache()


def _stateless_row(event_type: str, request_id: str = "r") -> dict[str, Any]:
    """The observation-copy shape: ``event_type``, no top-level ``state``."""
    return {
        "request_id": request_id,
        "task_id": nf01194.TASK_ID,
        "event_type": event_type,
        "reason": "observation_copy",
        "decision": {"accepted": event_type == "auto_accepted"},
    }


def _retained_review_ready(request_id: str, metadata_path: Path) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "task_id": nf01194.TASK_ID,
        "runner": "claude_worker",
        "topic": "coding",
        "adapter_id": "claude_cli",
        "state": "review_ready",
        "workspace_retained": True,
        "pid": 999_000,
        "pid_start_ticks": 4242,
        "metadata_path": str(metadata_path),
    }


def _terminal_card() -> dict[str, Any]:
    return {**nf01194._card(), "status": "finished", "worker_status": "done"}


# ---------------------------------------------------------------------------
# skip set and split_lifecycle_tail
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event_type", NEW_STATELESS_TYPES)
def test_new_stateless_types_are_named_advisory(event_type):
    assert event_type in launch_zero_delta.ORCHESTRATOR_ADVISORY_EVENT_TYPES
    # The launcher folds with the very set this module owns (single definition).
    assert process_launcher.ORCHESTRATOR_ADVISORY_EVENT_TYPES is (
        launch_zero_delta.ORCHESTRATOR_ADVISORY_EVENT_TYPES
    )


@pytest.mark.parametrize("event_type", NEW_STATELESS_TYPES)
def test_split_lifecycle_tail_keeps_retained_review_ready_row(event_type, tmp_path):
    review_ready = _retained_review_ready("r", tmp_path / "m.json")
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail(
        [review_ready, _stateless_row(event_type)]
    )
    assert lifecycle is review_ready
    assert advisory is None
    row, source = launch_zero_delta.stated_lifecycle_tail(
        [review_ready, _stateless_row(event_type)]
    )
    assert (row.get("state"), source) == ("review_ready", "lifecycle_tail")


def test_unnamed_sparse_row_stays_lifecycle():
    # Only NAMED orchestrator rows skip; an unnamed sparse row (event_type set,
    # no top-level state) is a lifecycle delta (NF-2026-02358 contract).
    future = "some_future_lifecycle_delta"
    assert future not in launch_zero_delta.ORCHESTRATOR_ADVISORY_EVENT_TYPES
    review_ready = {"request_id": "r", "state": "review_ready"}
    row = _stateless_row(future)
    assert launch_zero_delta._tail_row_kind(row) == "lifecycle"
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail([review_ready, row])
    assert lifecycle is row
    assert advisory is None


@pytest.mark.parametrize("event_type", NEW_STATELESS_TYPES)
def test_named_rows_skip_while_stated_and_plain_rows_stay_lifecycle(event_type):
    assert launch_zero_delta._tail_row_kind(_stateless_row(event_type)) == "skip"
    stated = {"request_id": "r", "event_type": "some_future_event", "state": "exited"}
    blank_state_key = {"request_id": "r", "event_type": "some_future_event", "state": None}
    plain = {"request_id": "r", "pid": 7}
    for row in (stated, blank_state_key, plain):
        assert launch_zero_delta._tail_row_kind(row) == "lifecycle"
    notice = {"request_id": "r", "event_kind": launch_zero_delta.RUNTIME_NOTICE_EVENT_KIND}
    assert launch_zero_delta._tail_row_kind(notice) == "advisory"


# ---------------------------------------------------------------------------
# ProcessManager._latest_by_request
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event_type", NEW_STATELESS_TYPES)
def test_latest_by_request_keeps_review_ready_with_workspace_retained(event_type, tmp_path):
    manager = nf01194._manager(tmp_path, _terminal_card())
    request_id = nf01194.REQUEST_ID
    manager._append_event(_retained_review_ready(request_id, tmp_path / "m.json"))
    manager._append_event(_stateless_row(event_type, request_id=request_id))

    latest = manager._latest_by_request()[request_id]

    assert latest.get("state") == "review_ready"
    assert latest.get("workspace_retained") is True
    assert latest.get("event_type") is None
    lifecycle, _advisory = launch_zero_delta.split_lifecycle_tail(
        manager._request_events(request_id)
    )
    assert lifecycle.get("state") == "review_ready"
    assert lifecycle.get("workspace_retained") is True


# ---------------------------------------------------------------------------
# janitor GC sweep (fakes only; no provider is launched)
# ---------------------------------------------------------------------------

def test_gc_sweep_scans_retained_review_ready_after_auto_accept_declined(
    tmp_path, monkeypatch,
):
    manager = nf01194._manager(tmp_path, _terminal_card())
    request_id = nf01194.REQUEST_ID
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    # Unparseable metadata makes the real GC path stop at a named, non-destructive
    # reason (``metadata_invalid``) once the request is a candidate at all.
    metadata_path = process_dir / f"{request_id}.request.json"
    metadata_path.write_text("{not json", encoding="utf-8")
    manager._append_event(_retained_review_ready(request_id, metadata_path))
    manager._append_event(_stateless_row("auto_accept_declined", request_id=request_id))

    monkeypatch.setattr(process_launcher, "_pid_matches", lambda *_a, **_k: False)
    seen: list[tuple[str, dict[str, Any], Any]] = []
    original = manager._gc_finalized_workspace

    def spy(rid, event, **kwargs):
        result = original(rid, event, **kwargs)
        seen.append((rid, dict(event), result))
        return result

    monkeypatch.setattr(manager, "_gc_finalized_workspace", spy)

    summary = manager._gc_finalized_workspaces()

    assert summary.get("refused") is None
    assert [rid for rid, _event, _result in seen] == [request_id]
    _rid, event, result = seen[0]
    assert event.get("state") == "review_ready"
    assert event.get("workspace_retained") is True
    assert result is not None, "the retained request is invisible to the janitor"
    assert result.get("gc") is False
    assert summary["gc_scanned"] == 1
    assert summary["gc_cleaned"] == 0
    assert metadata_path.read_text(encoding="utf-8") == "{not json"


# ---------------------------------------------------------------------------
# projection / checkpoint cache identity
# ---------------------------------------------------------------------------

def _archive_name(index: int) -> str:
    return f"process_events.20260101T0000{index:02d}.000000Z.1.{index:08x}.jsonl"


def _write_rows(target: Path, rows: list[dict[str, Any]]) -> None:
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _ledger_with_declined_archive(root: Path) -> Path:
    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    _write_rows(directory / _archive_name(0), [
        _retained_review_ready("req-retained", root / "m.json"),
        _stateless_row("auto_accept_declined", request_id="req-retained"),
    ])
    _write_rows(directory / _archive_name(1), [
        {"request_id": "req-other", "state": "running", "pid": 11},
    ])
    path = directory / "process_events.jsonl"
    _write_rows(path, [{"request_id": "req-other", "state": "finished"}])
    return path


def test_projection_under_old_skip_set_is_not_reused_for_new(tmp_path):
    path = _ledger_with_declined_archive(tmp_path)
    old_key = pel._projection_key(path, skip_event_kinds=OLD_SKIP, **FOLD)
    new_key = pel._projection_key(path, skip_event_kinds=NEW_SKIP, **FOLD)
    assert old_key != new_key

    # The pre-fix fold reproduces the defect and is cached (memory + checkpoint).
    old = pel.latest_events(path, skip_event_kinds=OLD_SKIP, **FOLD)
    assert old["req-retained"].get("state") is None
    assert old["req-retained"].get("event_type") == "auto_accept_declined"
    checkpoints = [p for p in pel.checkpoint_dir(path).iterdir() if p.suffix == ".json"]
    assert checkpoints, "the old fold must have persisted an archive checkpoint"

    # Warm: the in-memory projection under the old identity is not served.
    warm = pel.latest_events(path, skip_event_kinds=NEW_SKIP, **FOLD)
    assert warm["req-retained"].get("state") == "review_ready"
    assert warm["req-retained"].get("workspace_retained") is True

    # Cold: only the old checkpoint exists on disk; it is not served either.
    pel.reset_latest_event_cache()
    cold = pel.latest_events(path, skip_event_kinds=NEW_SKIP, **FOLD)
    assert cold == warm
    assert cold["req-other"].get("state") == "finished"

    # And the old identity still answers exactly as before (nothing was mutated).
    pel.reset_latest_event_cache()
    assert pel.latest_events(path, skip_event_kinds=OLD_SKIP, **FOLD) == old
