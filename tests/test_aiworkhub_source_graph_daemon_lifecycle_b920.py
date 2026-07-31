"""0.6.30: automatic Source Graph indexing lifecycle (source_graph_daemon.py).

Covers: InitRepo triggers an initial index without blocking the caller,
reload/ensure_started converges on the same repo-bound daemon, the periodic
loop and an explicit refresh_now() never overlap on the same repository's
database, two repositories never share or interfere with each other's
daemon, a failed build reports degraded health without crashing, and stop()
cleanly unregisters.

Every fixture uses its own ``tmp_path`` repository (Parallel-Tests-First
rule) -- no test shares a canonical DB or daemon-registry key with another.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, repository_bootstrap, server, source_graph, source_graph_daemon, task_store  # noqa: E402


@pytest.fixture(autouse=True)
def in_process_builds_for_monkeypatches(monkeypatch):
    """Keep unit-test monkeypatches in-process.

    Production defaults to a dedicated indexing subprocess so a CPU-heavy
    repository scan cannot starve MCP stdio. These unit tests deliberately
    replace ``source_graph.build_index`` with local closures/events, which
    cannot cross a process boundary.
    """
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_THREAD,
    )


@pytest.fixture
def cleanup_daemons():
    """Stop every daemon this test registered, even on failure -- a stray
    indexing thread from one test must never leak into the next."""
    roots: list[Path] = []
    yield roots
    for root in roots:
        source_graph_daemon.stop_daemon(root)


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


# ---------------------------------------------------------------------------
# 1. InitRepo triggers an initial index (full build, since no prior
#    last_build meta) and never blocks the caller on the whole build.
# ---------------------------------------------------------------------------


def test_init_repo_triggers_initial_index_without_blocking(tmp_path, cleanup_daemons):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.php").write_text("<?php\nfunction live_probe(): int { return 1; }\n", encoding="utf-8")
    cleanup_daemons.append(root)

    result = repository_bootstrap.initialize_repository_full(root)

    assert result["ok"]
    assert result["source_graph_ready"] is True
    assert result["source_graph_daemon_started"] is True
    ignore_config = root / ".aiworkhub" / "config" / "source_graph.json"
    assert ignore_config.is_file()
    assert source_graph.load_ignore_policy(root).exclude_dirs >= source_graph.DEFAULT_EXCLUDE_DIR_NAMES

    daemon = source_graph_daemon.get_daemon(root)
    assert daemon is not None
    assert daemon.is_running()

    # Wait on the daemon's build-completion event, not timing-sensitive
    # sleep/poll loops. InitRepo itself remains non-blocking.
    assert daemon.wait_for_first_build(timeout=10), "initial background build never completed"
    health = daemon.health()

    assert health["status"] == source_graph_daemon.STATUS_READY
    assert health["last_report"]["files_seen"] == 1
    assert health["last_report"]["incremental"] is False
    assert health["language_capabilities"]["php"] == "semantic_lexical"
    assert ".php" in health["indexed_extensions"]


def test_successful_zero_file_build_is_truthful_empty_not_ready(tmp_path):
    root = _init_repo(tmp_path)
    daemon = source_graph_daemon.SourceGraphDaemon(root)

    assert daemon._run_one_build() is True
    health = daemon.health()

    assert health["ok"] is True
    assert health["status"] == source_graph_daemon.STATUS_EMPTY
    assert health["last_report"]["files_seen"] == 0
    assert health["last_report"]["entities_written"] == 0


def test_production_default_build_runs_in_dedicated_subprocess(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    (root / "app.py").write_text("def live_probe():\n    return 1\n", encoding="utf-8")
    monkeypatch.delenv(source_graph_daemon.BUILD_EXECUTION_ENV, raising=False)
    monkeypatch.setenv("PYTHONPATH", str(_SRC))
    daemon = source_graph_daemon.SourceGraphDaemon(root)

    assert daemon._build_execution == source_graph_daemon.BUILD_EXECUTION_SUBPROCESS
    assert daemon._run_one_build() is True
    health = daemon.health()
    assert health["ok"] is True
    assert health["status"] == source_graph_daemon.STATUS_READY
    assert health["last_report"]["files_seen"] == 1


def test_old_success_is_truthfully_stale_until_next_success(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
        stale_after_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon._thread = threading.current_thread()
    daemon._status = source_graph_daemon.STATUS_READY
    daemon._started_at = datetime.now(timezone.utc).isoformat()
    daemon._last_success_at = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).isoformat()

    stale = daemon.health()

    assert stale["ok"] is False
    assert stale["status"] == source_graph_daemon.STATUS_STALE
    assert stale["stale_reason"] == "last_success_exceeded_threshold"
    assert stale["index_age_seconds"] >= 119

    def successful_build(repo_root, *, incremental=True, db_path=None):
        return source_graph.BuildReport(
            repo_root=str(repo_root), db_path="fake.sqlite", incremental=incremental,
            files_seen=1, files_changed=1, files_unchanged=0, files_removed=0,
            entities_written=1, edges_written=0, errors=[],
            build_revision="test", finished_at="t",
        )

    monkeypatch.setattr(source_graph, "build_index", successful_build)
    assert daemon._run_one_build() is True
    fresh = daemon.health()
    assert fresh["ok"] is True
    assert fresh["status"] == source_graph_daemon.STATUS_READY
    assert fresh["stale_reason"] == ""


# ---------------------------------------------------------------------------
# 2. Reload / repeated ensure_started converges on the same daemon.
# ---------------------------------------------------------------------------


def test_reload_ensure_started_converges_on_same_daemon(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    first = source_graph_daemon.ensure_started(root, refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS)
    again = source_graph_daemon.ensure_started(root, refresh_interval_seconds=9999)

    assert first is again
    assert source_graph_daemon.get_daemon(root) is first
    # The second call's interval kwarg is ignored -- exactly one daemon,
    # its original configuration untouched.
    assert first.refresh_interval_seconds == source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS


def test_core_source_graph_ensure_started_uninitialized_repo_not_degraded(tmp_path, monkeypatch, cleanup_daemons):
    root = tmp_path / "never_initialized"
    root.mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    cleanup_daemons.append(root)

    result = core.source_graph_ensure_started()

    assert result["ok"] is True
    assert result["status"] == "uninitialized"
    assert result["daemon_started"] is False
    assert source_graph_daemon.get_daemon(root) is None


def test_core_source_graph_ensure_started_then_stop(tmp_path, monkeypatch, cleanup_daemons):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    cleanup_daemons.append(root)

    started = core.source_graph_ensure_started()
    assert started["ok"] is True
    assert started["daemon_started"] is True

    # A second "handshake" converges -- never a second thread.
    started_again = core.source_graph_ensure_started()
    assert started_again["daemon_started"] is True
    assert started_again["repo_root"] == str(root.resolve())

    stopped = core.source_graph_stop()
    assert stopped["ok"] is True
    assert stopped["stopped"] is True
    assert source_graph_daemon.get_daemon(root) is None


def test_server_main_bootstraps_source_graph_before_stdio(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(core, "source_graph_ensure_started", lambda: calls.append("source_graph"))
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("mcp"))

    server.main()

    assert calls == ["source_graph", "mcp"]


def test_server_main_keeps_mcp_available_when_source_graph_bootstrap_fails(monkeypatch):
    calls: list[str] = []

    def fail_start():
        calls.append("source_graph")
        raise RuntimeError("indexer_failed")

    monkeypatch.setattr(core, "source_graph_ensure_started", fail_start)
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("mcp"))

    server.main()

    assert calls == ["source_graph", "mcp"]


# ---------------------------------------------------------------------------
# 3. Periodic loop and refresh_now() never overlap on the same repository.
# ---------------------------------------------------------------------------


def test_periodic_and_refresh_now_never_overlap(tmp_path, monkeypatch, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    build_started = threading.Event()
    release_build = threading.Event()
    call_count = {"n": 0}

    def fake_build_index(repo_root, *, incremental=True, db_path=None):
        call_count["n"] += 1
        build_started.set()
        release_build.wait(timeout=5)
        return source_graph.BuildReport(
            repo_root=str(repo_root), db_path="fake.sqlite", incremental=incremental,
            files_seen=0, files_changed=0, files_unchanged=0, files_removed=0,
            entities_written=0, edges_written=0, errors=[],
            build_revision="test", finished_at="t",
        )

    monkeypatch.setattr(source_graph, "build_index", fake_build_index)

    daemon = source_graph_daemon.SourceGraphDaemon(
        root, refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()
    try:
        assert build_started.wait(timeout=5), "background build never started"

        # The periodic loop's first build is still in flight (blocked on
        # release_build) -- an explicit refresh_now() must not join it.
        result = daemon.refresh_now()
        assert result["triggered"] is False
        assert result["reason"] == "build_in_progress"

        release_build.set()
    finally:
        daemon.stop()

    assert call_count["n"] == 1


def test_cross_process_writer_contention_is_healthy_standby(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    with source_graph.index_write_lease(root) as acquired:
        assert acquired is True
        assert daemon._run_one_build() is True
    health = daemon.health()
    assert health["ok"] is True
    assert health["status"] == source_graph_daemon.STATUS_STANDBY
    assert health["writer_state"] == "standby"
    assert health["last_error"] == ""


def test_prior_build_probe_failure_is_degraded_not_dead_indexing(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    daemon = source_graph_daemon.SourceGraphDaemon(root)

    def fail_probe():
        raise RuntimeError("transient probe failure")

    monkeypatch.setattr(daemon, "_has_prior_build", fail_probe)
    assert daemon._run_one_build() is True
    health = daemon.health()
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED
    assert health["ok"] is False
    assert "transient probe failure" in health["last_error"]


# ---------------------------------------------------------------------------
# 4. Two repositories never share or interfere with each other's daemon.
# ---------------------------------------------------------------------------


def test_repo_isolation_distinct_daemons(tmp_path, cleanup_daemons):
    root_a = _init_repo(tmp_path, "repo_a")
    root_b = _init_repo(tmp_path, "repo_b")
    cleanup_daemons.append(root_a)
    cleanup_daemons.append(root_b)

    daemon_a = source_graph_daemon.ensure_started(root_a)
    daemon_b = source_graph_daemon.ensure_started(root_b)

    assert daemon_a is not daemon_b
    assert daemon_a.repo_root != daemon_b.repo_root
    assert daemon_a.is_running()
    assert daemon_b.is_running()

    assert source_graph_daemon.stop_daemon(root_a) is True
    assert source_graph_daemon.get_daemon(root_a) is None
    assert source_graph_daemon.get_daemon(root_b) is daemon_b
    assert daemon_b.is_running()


# ---------------------------------------------------------------------------
# 5. A failed build reports degraded health, never crashes the process.
# ---------------------------------------------------------------------------


def test_failed_build_reports_degraded_health_never_raises(tmp_path, monkeypatch, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    def boom(repo_root, *, incremental=True, db_path=None):
        raise RuntimeError("simulated index failure")

    monkeypatch.setattr(source_graph, "build_index", boom)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    result = daemon.refresh_now()  # must not raise

    assert result["triggered"] is True
    assert result["ok"] is False
    assert result["status"] == source_graph_daemon.STATUS_DEGRADED
    assert "simulated index failure" in result["last_error"]

    health = daemon.health()
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED
    assert health["ok"] is False


def test_core_source_graph_health_reports_not_registered_not_degraded(tmp_path, monkeypatch, cleanup_daemons):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    cleanup_daemons.append(root)

    health = core.source_graph_health()

    assert health["ok"] is True
    assert health["registered"] is False
    assert health["status"] == source_graph_daemon.STATUS_STOPPED


# ---------------------------------------------------------------------------
# 6. stop() cleanly unregisters and the underlying thread actually exits.
# ---------------------------------------------------------------------------


def test_stop_unregisters_and_thread_exits(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    daemon = source_graph_daemon.ensure_started(root)
    assert daemon.is_running()

    assert source_graph_daemon.stop_daemon(root) is True
    assert daemon.is_running() is False
    assert source_graph_daemon.get_daemon(root) is None
    # Idempotent: stopping again (already unregistered) is a clean no-op.
    assert source_graph_daemon.stop_daemon(root) is False
