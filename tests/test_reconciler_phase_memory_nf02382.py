"""Per-phase RSS high-water of a reconciler scan (NF-2026-02382, measurement only).

Measured 2026-11: the extension-hosted server swung 344 MB -> 6.1 GB -> back
several times an hour while the reconcile phase held 59 s of a 64 s scan. The
status now says which phase owned the peak. Every test injects a fake memory
reader and drives samples by hand, so none needs a sleep over 0.2 s.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_reconciler as tr  # noqa: E402

BASE = 100
SPIKE = 5000
SAMPLER_THREAD = "aiworkhub-reconciler-rss-sampler"


class _NoWaitEvent(threading.Event):
    """Skips the inter-scan wait so a many-scan loop runs instantly."""

    def wait(self, timeout=None):  # noqa: D401
        return self.is_set()


def _service(tmp_path, monkeypatch, reader, *, no_wait=False):
    svc = tr.ReconcilerService.__new__(tr.ReconcilerService)
    svc.repo = tmp_path
    svc.scan_interval_seconds = tr.MIN_SCAN_INTERVAL_SECONDS
    svc._manager = None
    svc._stop_event = _NoWaitEvent() if no_wait else threading.Event()
    svc._thread = None
    svc._state_lock = threading.Lock()
    svc._last_scan = {}
    svc._last_error = ""
    svc._pass_index = 0
    svc._authority_identity = {}
    svc._memory_reader = reader
    monkeypatch.setattr(svc, "_superseded_reason", lambda: "")
    return svc


def _scan_in_phases(state, spike_phase, phases, svc, *, stop=True):
    """A run_scan stand-in: RSS spikes only while ``spike_phase`` is active."""

    sampler, _ = svc._memory_state()

    def _scan(_mgr=None, *, repo=None, include_gc=True, on_phase=None):
        for name in phases:
            on_phase(name)
            state["rss"] = SPIKE if name.partition(":")[0] == spike_phase else BASE
            sampler.sample()
        state["rss"] = BASE
        if stop:
            svc._stop_event.set()
        return {
            "ok": True,
            "scanned_at": "2026-11-23T00:00:00Z",
            "phase_durations_ms": {"review_recovery": 7},
        }

    return _scan


def test_spike_lands_on_the_active_phase_only(tmp_path, monkeypatch):
    state = {"rss": BASE}
    svc = _service(tmp_path, monkeypatch, lambda: state["rss"])
    phases = ["reconcile", "callback_prune", "review_recovery", "sdlc_sync:fetch", "sdlc_sync:push"]
    monkeypatch.setattr(tr, "run_scan", _scan_in_phases(state, "review_recovery", phases, svc))
    svc._owner_scan_loop()
    status = tr.read_status(tmp_path)
    peaks = status["phase_rss_peak_bytes"]
    assert peaks["review_recovery"] == SPIKE
    assert all(v == BASE for k, v in peaks.items() if k != "review_recovery")
    assert set(peaks) == {"idle", "reconcile", "callback_prune", "review_recovery", "sdlc_sync"}
    assert all(isinstance(v, int) for v in peaks.values())
    assert status["scan_rss_start_bytes"] == BASE
    assert status["scan_rss_end_bytes"] == BASE
    assert status["gc_included"] is True
    assert status["phase_durations_ms"] == {"review_recovery": 7}


def test_sdlc_sync_sub_stages_fold_into_one_label(tmp_path, monkeypatch):
    state = {"rss": BASE}
    svc = _service(tmp_path, monkeypatch, lambda: state["rss"])
    phases = ["sdlc_sync:fetch", "sdlc_sync:push"]
    monkeypatch.setattr(tr, "run_scan", _scan_in_phases(state, "sdlc_sync", phases, svc))
    svc._owner_scan_loop()
    peaks = tr.read_status(tmp_path)["phase_rss_peak_bytes"]
    assert peaks["sdlc_sync"] == SPIKE
    assert not [k for k in peaks if ":" in k]


def test_idle_window_between_scans_is_attributed_to_idle(tmp_path, monkeypatch):
    state = {"rss": BASE}
    svc = _service(tmp_path, monkeypatch, lambda: state["rss"])
    sampler, _ = svc._memory_state()
    state["rss"] = SPIKE  # grown while no scan runs
    sampler.sample()
    state["rss"] = BASE
    monkeypatch.setattr(tr, "run_scan", _scan_in_phases(state, "none", ["reconcile"], svc))
    svc._owner_scan_loop()
    peaks = tr.read_status(tmp_path)["phase_rss_peak_bytes"]
    assert peaks["idle"] == SPIKE
    assert peaks["reconcile"] == BASE


def test_history_is_bounded_and_newest_matches_the_latest_scan(tmp_path, monkeypatch):
    state = {"rss": BASE}
    svc = _service(tmp_path, monkeypatch, lambda: state["rss"], no_wait=True)
    count = tr.PHASE_MEMORY_HISTORY_MAX + 5
    seen = {"n": 0}
    sampler, _ = svc._memory_state()

    def _scan(_mgr=None, *, repo=None, include_gc=True, on_phase=None):
        seen["n"] += 1
        on_phase("reconcile")
        state["rss"] = BASE + seen["n"]
        sampler.sample()
        return {"ok": True, "scanned_at": f"scan-{seen['n']}", "phase_durations_ms": {}}

    monkeypatch.setattr(tr, "run_scan", _scan)
    svc._owner_scan_loop(max_iterations=count)
    status = tr.read_status(tmp_path)
    history = status["phase_memory_history"]
    assert len(history) == tr.PHASE_MEMORY_HISTORY_MAX == 120
    newest = history[-1]
    assert newest["scanned_at"] == f"scan-{count}"
    assert newest["phase_rss_peak_bytes"] == status["phase_rss_peak_bytes"]
    assert newest["phase_rss_peak_bytes"]["reconcile"] == BASE + count
    assert newest["gc_included"] == status["gc_included"]
    assert newest["scan_duration_seconds"] == status["scan_duration_seconds"]
    assert history[0]["scanned_at"] == f"scan-{count - 119}"
    assert set(newest) == {
        "scanned_at", "gc_included", "scan_duration_seconds", "phase_rss_peak_bytes",
    }


def _failing_readers():
    def _raises():
        raise OSError("proc gone")

    return {"none": lambda: None, "raises": _raises}


def test_failing_reader_leaves_scan_unchanged_and_memory_fields_null(tmp_path, monkeypatch):
    for label, reader in _failing_readers().items():
        svc = _service(tmp_path, monkeypatch, reader)
        sampler, _ = svc._memory_state()
        assert sampler.sample() is None

        def _scan(_mgr=None, *, repo=None, include_gc=True, on_phase=None, _svc=svc):
            on_phase("review_recovery")
            _svc._stop_event.set()
            return {"ok": True, "phase_durations_ms": {"review_recovery": 7}}

        monkeypatch.setattr(tr, "run_scan", _scan)
        svc._owner_scan_loop()  # no exception escapes
        status = tr.read_status(tmp_path)
        assert status["phase_durations_ms"] == {"review_recovery": 7}, label
        assert status["last_error"] == "", label
        assert status["phase_rss_peak_bytes"] is None, label
        assert status["scan_rss_start_bytes"] is None, label
        assert status["scan_rss_end_bytes"] is None, label
        assert status["phase_memory_history"][-1]["phase_rss_peak_bytes"] is None, label


def test_default_reader_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("no platform")

    monkeypatch.setattr(tr, "process_memory_bytes", _boom)
    assert tr._read_rss_bytes() is None
    monkeypatch.setattr(tr, "process_memory_bytes", lambda: {"rss_bytes": None})
    assert tr._read_rss_bytes() is None
    monkeypatch.setattr(tr, "process_memory_bytes", lambda: {"rss_bytes": 42})
    assert tr._read_rss_bytes() == 42


def _live_samplers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == SAMPLER_THREAD and t.is_alive()]


def test_sampler_thread_starts_samples_and_joins_across_two_cycles():
    for _ in range(2):
        got = threading.Event()

        def _reader():
            got.set()
            return BASE

        sampler = tr._PhaseMemorySampler(reader=_reader, interval=0.01)
        sampler.start()
        sampler.start()  # a second start is a no-op, not a second thread
        assert got.wait(0.2)
        assert len(_live_samplers()) == 1
        sampler.stop()
        assert _live_samplers() == []
    assert tr.MEMORY_SAMPLE_INTERVAL_SECONDS == 1.0


def test_owner_loop_stops_and_joins_its_sampler(tmp_path, monkeypatch):
    state = {"rss": BASE}
    for _ in range(2):
        svc = _service(tmp_path, monkeypatch, lambda: state["rss"])
        monkeypatch.setattr(
            tr, "run_scan", _scan_in_phases(state, "reconcile", ["reconcile"], svc)
        )
        svc._run_as_owner(max_iterations=1)
        assert _live_samplers() == []
        assert tr.read_status(tmp_path)["phase_rss_peak_bytes"]["reconcile"] == SPIKE


def test_a_sample_is_attributed_to_the_phase_it_was_read_in():
    reading, release = threading.Event(), threading.Event()

    def _reader():
        reading.set()
        release.wait(1)
        return SPIKE

    sampler = tr._PhaseMemorySampler(reader=_reader)
    sampler.set_phase("reconcile")
    worker = threading.Thread(target=sampler.sample)
    worker.start()
    assert reading.wait(1)
    switcher = threading.Thread(target=sampler.set_phase, args=("callback_prune",))
    switcher.start()
    switcher.join(0.05)  # the phase changes while the read is still in flight
    release.set()
    worker.join(1)
    switcher.join(1)
    assert sampler._peaks == {"reconcile": SPIKE}


def test_start_after_a_timed_out_stop_runs_a_fresh_sampler():
    first, later, release = threading.Event(), threading.Event(), threading.Event()
    calls = {"n": 0}

    def _reader():
        calls["n"] += 1
        if calls["n"] == 1:
            first.set()
            release.wait(2)  # the first read outlives stop()'s timeout
        else:
            later.set()
        return BASE

    sampler = tr._PhaseMemorySampler(reader=_reader, interval=0.01)
    sampler.start()
    assert first.wait(1)
    sampler.stop(timeout=0.01)
    stale = sampler._thread
    assert stale is not None and stale.is_alive()
    sampler.start()
    assert sampler._thread is not stale
    release.set()
    stale.join(1)
    assert not stale.is_alive()
    assert later.wait(1)
    assert len(_live_samplers()) == 1
    sampler.stop()
    assert _live_samplers() == []
