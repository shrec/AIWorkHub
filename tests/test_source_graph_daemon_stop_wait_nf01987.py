"""NF-2026-01987: stop_daemon waits for the daemon thread to really exit.

The daemon thread's own bounded stop path (tree termination plus output
drain) can outlast a fixed 5 s join under load. stop_daemon now waits a named,
measured stop wait; a thread still alive after it is never reported stopped.
"""

from __future__ import annotations

import threading

import pytest

from aiworkhub import source_graph_daemon
from aiworkhub.source_graph_daemon import SourceGraphDaemon


@pytest.fixture
def repo_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    yield root
    # Never leave a registered daemon behind for another test.
    source_graph_daemon._REGISTRY.pop(source_graph_daemon._registry_key(root), None)


def _install_slow_exit_loop(monkeypatch, release: threading.Event, started: threading.Event):
    """A daemon loop whose current build step observes the stop event but then
    needs ``release`` before it can return -- a step slower than the join."""

    def fake_loop(self: SourceGraphDaemon) -> None:
        started.set()
        self._stop_event.wait()
        release.wait(timeout=10.0)

    monkeypatch.setattr(SourceGraphDaemon, "_loop", fake_loop)


def test_stop_wait_constant_exceeds_old_fixed_join():
    assert source_graph_daemon._STOP_DAEMON_WAIT_SECONDS > 5.0


def test_slow_exiting_thread_is_waited_for_and_unregistered(monkeypatch, repo_root):
    release = threading.Event()
    started = threading.Event()
    _install_slow_exit_loop(monkeypatch, release, started)
    # Scaled-down stand-in for the real wait: the slow step (0.4 s) is longer
    # than nothing but well inside this wait, as a >5 s step is inside 30 s.
    monkeypatch.setattr(source_graph_daemon, "_STOP_DAEMON_WAIT_SECONDS", 5.0)
    seen_timeouts: list[float] = []
    original_stop = SourceGraphDaemon.stop

    # A sentinel default, not stop()'s own 5.0: a stop_daemon that passes no
    # timeout must fail this test instead of matching the patched wait.
    def spy_stop(self, *, timeout=None):
        seen_timeouts.append(timeout)
        return original_stop(self, timeout=timeout)

    monkeypatch.setattr(SourceGraphDaemon, "stop", spy_stop)

    daemon = source_graph_daemon.ensure_started(repo_root)
    assert started.wait(timeout=5.0)
    timer = threading.Timer(0.4, release.set)
    timer.start()
    try:
        assert source_graph_daemon.stop_daemon(repo_root) is True
    finally:
        release.set()
        timer.cancel()
    assert seen_timeouts == [5.0]
    assert daemon.is_running() is False
    assert source_graph_daemon.get_daemon(repo_root) is None


def test_thread_alive_after_wait_is_not_reported_stopped(monkeypatch, repo_root):
    release = threading.Event()
    started = threading.Event()
    _install_slow_exit_loop(monkeypatch, release, started)
    monkeypatch.setattr(source_graph_daemon, "_STOP_DAEMON_WAIT_SECONDS", 0.1)

    daemon = source_graph_daemon.ensure_started(repo_root)
    assert started.wait(timeout=5.0)
    try:
        assert source_graph_daemon.stop_daemon(repo_root) is False
        assert daemon.is_running() is True
        assert source_graph_daemon.get_daemon(repo_root) is daemon
    finally:
        release.set()
        thread = daemon._thread
        if thread is not None:
            thread.join(timeout=5.0)
    assert source_graph_daemon.stop_daemon(repo_root) is True
    assert source_graph_daemon.get_daemon(repo_root) is None


def test_stop_during_build_skips_recommendation_selfcheck(monkeypatch, repo_root):
    from aiworkhub import worker_ai_tools_mcp

    daemon = SourceGraphDaemon(repo_root)
    gate_calls: list[object] = []
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "source_graph_recommendation_roundtrip_gate",
        lambda context: gate_calls.append(context) or {"ok": True},
    )
    monkeypatch.setattr(daemon, "_has_prior_build", lambda: False)
    report = {"files_seen": 3, "files_changed": 3, "files_removed": 0}

    def build_then_stop(*, incremental: bool):
        # stop() arrives while the build child is finishing.
        daemon._stop_event.set()
        return {"kind": "success", "report": dict(report)}

    monkeypatch.setattr(daemon, "_execute_build", build_then_stop)

    assert daemon._run_one_build() is True
    assert gate_calls == []
    health_status = daemon._status
    assert health_status == source_graph_daemon.STATUS_STOPPED
    assert daemon._last_report == report
    assert daemon._last_success_at
