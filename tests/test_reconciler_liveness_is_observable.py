"""The thing that closes the loop must be able to say whether it is running.

The reconciler is the only component that finalizes a worker that exited on its
own. Its health lived entirely in one process's memory: a dict keyed by repo
path inside the MCP server. If the thread never started, the startup exception
was swallowed by a bare ``except Exception: pass``; if it started and died, the
dict simply stopped being updated. Either way every surface a manager could
reach still answered "healthy", and finished work sat in ``processing``.

That is not a slow loop, it is an unobservable one. These tests pin the
difference: a scan leaves a durable record, a failing scan leaves a record that
says so, a record too old to be evidence is reported stale rather than healthy,
and a startup failure is written down instead of vanishing.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_reconciler as tr  # noqa: E402


def _service(repo: Path, scan):
    svc = tr.ReconcilerService.__new__(tr.ReconcilerService)
    svc.repo = repo
    svc.scan_interval_seconds = tr.MIN_SCAN_INTERVAL_SECONDS
    svc._manager = None
    import threading

    svc._stop_event = threading.Event()
    svc._thread = None
    svc._state_lock = threading.Lock()
    svc._last_scan = {}
    svc._last_error = ""
    return svc


def test_a_completed_scan_leaves_a_durable_record(tmp_path: Path, monkeypatch):
    svc = _service(tmp_path, None)

    def _one_scan(*_a, **_k):
        svc._stop_event.set()  # stop after this pass, but let it complete
        return {"ok": True, "finalized": 2, "watched": 1, "scanned_at": "now"}

    monkeypatch.setattr(tr, "run_scan", _one_scan)
    svc._loop()

    record = tr.read_status(tmp_path)
    assert record["finalized"] == 2
    assert record["watched"] == 1
    assert record["last_error"] == ""
    assert record["schema_id"] == "aiworkhub.task_reconciler_status.v1"
    assert isinstance(record["scan_finished_epoch"], float)


def test_a_failing_scan_does_not_look_like_a_working_one(tmp_path: Path, monkeypatch):
    svc = _service(tmp_path, None)

    def _boom(*_a, **_k):
        svc._stop_event.set()
        raise RuntimeError("registry locked")

    monkeypatch.setattr(tr, "run_scan", _boom)
    svc._loop()

    record = tr.read_status(tmp_path)
    assert "registry locked" in record["last_error"]
    assert record["finalized"] == 0


def test_health_answers_from_the_record_when_no_service_is_registered(tmp_path: Path):
    """A manager chat is a different process than the server that scans."""
    tr.write_status(tmp_path, {
        "scan_finished_epoch": time.time(),
        "scan_interval_seconds": 30.0,
        "last_error": "",
        "finalized": 0,
    })
    health = tr.reconciler_health(tmp_path)
    assert health["ok"] is True
    assert health["running"] is False       # not in THIS process
    assert health["durable_status_present"] is True
    assert health["durable_scan_stale"] is False


def test_a_record_too_old_to_be_evidence_is_stale_not_healthy(tmp_path: Path):
    tr.write_status(tmp_path, {
        "scan_finished_epoch": time.time() - 86400,
        "scan_interval_seconds": 30.0,
        "last_error": "",
    })
    health = tr.reconciler_health(tmp_path)
    assert health["ok"] is False
    assert health["durable_scan_stale"] is True
    assert health["durable_scan_age_seconds"] > 80000


def test_no_record_at_all_is_reported_as_no_recent_scan(tmp_path: Path):
    health = tr.reconciler_health(tmp_path)
    assert health["ok"] is False
    assert health["last_error"] == "reconciler_unregistered_and_no_recent_scan"
    assert health["durable_status_present"] is False


def test_a_startup_failure_is_written_down_rather_than_swallowed(tmp_path: Path):
    """server.py records the exception it must not raise."""
    tr.write_status(tmp_path, {
        "startup_error": "OSError:database is locked",
        "scan_finished_epoch": None,
    })
    health = tr.reconciler_health(tmp_path)
    assert health["ok"] is False
    assert health["startup_error"] == "OSError:database is locked"


def test_an_unwritable_status_location_never_breaks_the_loop(tmp_path: Path, monkeypatch):
    """Bookkeeping failure must not stop the only thing that finalizes work."""
    target = tmp_path / "blocked"
    target.write_text("i am a file, not a directory", encoding="utf-8")
    tr.write_status(target / "repo", {"scan_finished_epoch": time.time()})
    assert tr.read_status(target / "repo") == {}


def test_a_corrupt_record_reads_as_absent_not_as_healthy(tmp_path: Path):
    path = tr.status_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert tr.read_status(tmp_path) == {}
    assert tr.reconciler_health(tmp_path)["ok"] is False


def test_finalization_runs_every_pass_and_housekeeping_does_not(tmp_path: Path, monkeypatch):
    """A finished card's time-to-review must not depend on garbage collection.

    Finalizing exited workers is correctness (0.01 s measured); sweeping
    retained workspaces is housekeeping dominated by re-proving that ~100
    pinned rework predecessors are still pinned, ~3 s each. Bundled on one
    cadence, the sweep is what the loop spends its interval on.
    """
    passes: list[bool] = []
    svc = _service(tmp_path, None)

    def _scan(_mgr=None, *, repo=None, include_gc=True):
        passes.append(include_gc)
        if len(passes) >= tr.GC_SCAN_EVERY_N_PASSES + 1:
            svc._stop_event.set()
        return {"ok": True, "finalized": 0, "watched": 0, "gc_included": include_gc}

    monkeypatch.setattr(tr, "run_scan", _scan)
    svc.scan_interval_seconds = 0.0
    svc._loop()

    # The first pass sweeps so a fresh reconciler still reclaims immediately,
    # then housekeeping is periodic -- never every pass.
    assert passes[0] is True
    assert passes[1:tr.GC_SCAN_EVERY_N_PASSES] == [False] * (tr.GC_SCAN_EVERY_N_PASSES - 1)
    assert passes[tr.GC_SCAN_EVERY_N_PASSES] is True


def test_the_durable_record_says_whether_a_pass_swept(tmp_path: Path, monkeypatch):
    """'No workspaces cleaned' and 'no sweep ran' are different facts."""
    svc = _service(tmp_path, None)

    def _scan(_mgr=None, *, repo=None, include_gc=True):
        svc._stop_event.set()
        return {"ok": True, "finalized": 1, "watched": 0,
                "gc_included": include_gc, "gc_cleaned": 3}

    monkeypatch.setattr(tr, "run_scan", _scan)
    svc._loop()
    record = tr.read_status(tmp_path)
    assert record["gc_included"] is True
    assert record["gc_cleaned"] == 3
    assert record["finalized"] == 1


def test_a_pass_announces_itself_before_it_runs(tmp_path: Path, monkeypatch):
    """A long sweep must not make the loop look absent while it works.

    The record was written only on completion, so during the minutes a sweep
    takes -- exactly when a reader most wants to know a reconciler is alive --
    there was nothing to read.
    """
    seen: list[dict] = []
    svc = _service(tmp_path, None)

    def _scan(*_a, **_k):
        seen.append(tr.read_status(tmp_path))   # what a reader sees mid-pass
        svc._stop_event.set()
        return {"ok": True, "finalized": 0, "watched": 0}

    monkeypatch.setattr(tr, "run_scan", _scan)
    svc._loop()

    (during,) = seen
    assert during["scan_in_progress"] is True
    assert during["scan_finished_epoch"] is None
    assert isinstance(during["scan_started_epoch"], float)

    after = tr.read_status(tmp_path)
    assert after["scan_in_progress"] is False
    assert isinstance(after["scan_finished_epoch"], float)


def test_a_running_pass_is_healthy_not_stale(tmp_path: Path):
    """Measure an in-progress pass from when it started, not from never."""
    tr.write_status(tmp_path, {
        "scan_started_epoch": time.time(),
        "scan_finished_epoch": None,
        "scan_in_progress": True,
        "scan_interval_seconds": 30.0,
        "last_error": "",
    })
    health = tr.reconciler_health(tmp_path)
    assert health["durable_scan_stale"] is False
    assert health["ok"] is True


def test_a_pass_that_started_hours_ago_is_still_stale(tmp_path: Path):
    """In-progress is not a licence to look alive forever."""
    tr.write_status(tmp_path, {
        "scan_started_epoch": time.time() - 86400,
        "scan_finished_epoch": None,
        "scan_in_progress": True,
        "scan_interval_seconds": 30.0,
        "last_error": "",
    })
    assert tr.reconciler_health(tmp_path)["durable_scan_stale"] is True
