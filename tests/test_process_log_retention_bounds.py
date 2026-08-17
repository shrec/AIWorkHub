"""Per-file worker-log and attempt-artifacts bounds.

``process_logs/processes`` held 4.4 GB of worker stdout with individual files at
26 MiB, and ``attempt-artifacts`` (a sibling directory inside the same
processes root) appeared in no retention module at all. Both grew without limit
by construction. This proves the two bounds:

* An oversized log is tail-capped to its last ``MAX_PROCESS_LOG_FILE_BYTES`` --
  the exact window the launcher itself reads to diagnose a failure -- so the
  head is released while the diagnostic tail an operator needs is always kept.
* An attempt-artifacts bundle whose run has aged past ``logs_days`` is moved
  into a reversible quarantine batch.

The bound fires ONLY for a run in a terminal ledger state (its writer has
stopped): a live/non-terminal run is never touched, so a tail an operator is
still watching is never cut. ``os.chmod`` is denied in the validation sandbox,
so the two mutating paths (tail-cap and bundle move) go through the coordinator's
canonical run; every read-only and protected-item assertion runs everywhere.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import task_store, terminal_log_retention


_FROZEN_NOW = datetime(2035, 1, 1, tzinfo=timezone.utc)
# The shrunk cap must exceed the tail-cap notice so ``keep = cap - len(notice)``
# stays positive and a diagnostic tail always survives -- exactly as in production,
# where the 4 MiB bound dwarfs the ~113-byte notice. A file just over this counts
# as "oversized" without writing MiB.
_SMALL_CAP = len(terminal_log_retention._TAIL_CAP_NOTICE) + 64  # bytes


@pytest.fixture(autouse=True)
def _freeze_and_shrink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_log_retention, "now_utc", lambda: _FROZEN_NOW)
    monkeypatch.setattr(terminal_log_retention, "MAX_PROCESS_LOG_FILE_BYTES", _SMALL_CAP)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _ledger_row(repo: Path, request_id: str, state: str) -> None:
    ledger = repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "request_id": request_id,
            "task_id": "TASK",
            "runner": "runner",
            "topic": "topic",
            "adapter_id": "codex_cli",
            "state": state,
            "finished_at": "2026-01-01T00:00:00+00:00",
        }) + "\n")


def _write_log(repo: Path, request_id: str, suffix: str, text: str) -> Path:
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    path = process_root / f"{request_id}{suffix}"
    path.write_text(text, encoding="utf-8")
    return path


def _write_bundle(repo: Path, request_id: str, text: str) -> Path:
    bundle = (
        repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
        / terminal_log_retention.ATTEMPT_ARTIFACTS_DIRNAME / request_id
    )
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "manifest.json").write_text(text, encoding="utf-8")
    return bundle


_TERMINAL = "a" * 32
_LIVE = "b" * 32
# A body whose last lines are unmistakable, comfortably larger than the cap.
_LOG_BODY = "".join(f"line-{i:03d}\n" for i in range(40))  # 360 bytes


# --- read-only preview -------------------------------------------------------

def test_preview_reports_an_oversized_terminal_log(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    _write_log(repo, _TERMINAL, ".stdout.log", _LOG_BODY)

    preview = terminal_log_retention.process_log_bounds_preview(repo)

    assert preview["oversized_log_count"] == 1
    assert preview["oversized_logs"][0]["name"] == f"{_TERMINAL}.stdout.log"
    assert preview["max_process_log_file_bytes"] == _SMALL_CAP


def test_preview_never_lists_a_live_run_log(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _LIVE, "processing")  # not a terminal state
    _write_log(repo, _LIVE, ".stdout.log", _LOG_BODY)

    preview = terminal_log_retention.process_log_bounds_preview(repo)

    assert preview["oversized_log_count"] == 0
    assert preview["protected_log_count"] == 1


def test_small_log_is_below_the_bound(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    _write_log(repo, _TERMINAL, ".stdout.log", "tiny\n")

    preview = terminal_log_retention.process_log_bounds_preview(repo)

    assert preview["oversized_log_count"] == 0


# --- protected items are never taken (runs fully in the sandbox) -------------

def test_enforce_never_touches_a_live_run_log(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _LIVE, "processing")
    path = _write_log(repo, _LIVE, ".stdout.log", _LOG_BODY)
    before = path.read_text(encoding="utf-8")

    result = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)

    assert result["logs_capped"] == 0
    # The live worker's tail is left exactly as it was.
    assert path.read_text(encoding="utf-8") == before


def test_enforce_leaves_a_small_terminal_log_intact(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    path = _write_log(repo, _TERMINAL, ".stdout.log", "tiny\n")

    result = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)

    assert result["logs_capped"] == 0
    assert path.read_text(encoding="utf-8") == "tiny\n"


# --- attempt-artifacts bound -------------------------------------------------

def test_preview_reports_an_aged_terminal_bundle(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    _write_bundle(repo, _TERMINAL, "{}")

    preview = terminal_log_retention.process_log_bounds_preview(repo)

    # Frozen far-future clock makes any just-written bundle aged past logs_days.
    assert preview["aged_bundle_count"] == 1
    assert preview["aged_bundles"][0]["request_id"] == _TERMINAL


def test_preview_protects_a_live_run_bundle(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _LIVE, "processing")
    _write_bundle(repo, _LIVE, "{}")

    preview = terminal_log_retention.process_log_bounds_preview(repo)

    assert preview["aged_bundle_count"] == 0
    assert preview["protected_bundle_count"] == 1


# --- mutating paths (canonical run; os.chmod denied in sandbox) --------------

def test_enforce_tail_caps_oversized_terminal_log_keeping_tail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    path = _write_log(repo, _TERMINAL, ".stdout.log", _LOG_BODY)

    result = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)

    assert result["logs_capped"] == 1
    assert result["log_bytes_freed"] > 0
    text = path.read_text(encoding="utf-8")
    # The diagnostic tail is kept and the truncation is explicit...
    assert "line-039" in text
    assert "aiworkhub-retention" in text
    # ...while the unbounded head is released and the file is now within bound.
    assert "line-000" not in text
    assert path.stat().st_size <= _SMALL_CAP + len(terminal_log_retention._TAIL_CAP_NOTICE)


# --- the per-file bound actually converges (NF-2026-00291 fixed-point repair) -

def test_enforce_caps_at_or_below_the_bound_not_a_notice_over_it(tmp_path: Path) -> None:
    # The written payload is notice + tail; reserving room for the notice keeps a
    # capped file at or under the bound, never one notice-length above it.
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    path = _write_log(repo, _TERMINAL, ".stdout.log", _LOG_BODY)

    result = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)

    assert result["logs_capped"] == 1
    assert path.stat().st_size <= terminal_log_retention.MAX_PROCESS_LOG_FILE_BYTES


def test_enforce_is_idempotent_a_capped_log_is_not_recapped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    _write_log(repo, _TERMINAL, ".stdout.log", _LOG_BODY)

    first = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)
    assert first["logs_capped"] == 1

    # A second immediate pass finds nothing above the bound: no rewrite, no re-fsync.
    second = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)
    assert second["logs_capped"] == 0
    preview = terminal_log_retention.process_log_bounds_preview(repo)
    assert preview["oversized_log_count"] == 0


def test_enforce_converges_when_the_kept_window_begins_on_a_line_boundary(
    tmp_path: Path,
) -> None:
    # The adversarial fixed point: a log whose last-``MAX`` window starts with a
    # newline. Keeping the full ``MAX`` bytes and then prepending the notice leaves
    # the file one notice over the bound forever, and every pass rewrites the whole
    # file. Reserving room for the notice converges in one pass; the next is a no-op.
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    cap = terminal_log_retention.MAX_PROCESS_LOG_FILE_BYTES
    # size == 2*cap; the last ``cap`` bytes are "\n" + "B"*(cap-1) -> window[0] == "\n".
    body = ("A" * cap) + "\n" + ("B" * (cap - 1))
    path = _write_log(repo, _TERMINAL, ".stdout.log", body)

    first = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)
    assert first["logs_capped"] == 1
    assert path.stat().st_size <= cap  # fails before the fix (stays cap + notice - 1)

    second = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)
    assert second["logs_capped"] == 0  # loops forever before the fix
    preview = terminal_log_retention.process_log_bounds_preview(repo)
    assert preview["oversized_log_count"] == 0


def test_enforce_quarantines_an_aged_terminal_bundle_reversibly(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger_row(repo, _TERMINAL, "exited")
    bundle = _write_bundle(repo, _TERMINAL, '{"artifacts": []}')

    result = terminal_log_retention.enforce_process_log_bounds(repo, confirm=True)

    assert result["bundles_quarantined"] == 1
    assert result["bundle_bytes"] > 0
    # The bundle left the unbounded processes tree...
    assert not bundle.exists()
    # ...and now sits in a bounded, reversible quarantine batch.
    batches = terminal_log_retention.list_batches(repo)["batches"]
    assert any(row["batch_id"] == result["bundle_batch_id"] for row in batches)
