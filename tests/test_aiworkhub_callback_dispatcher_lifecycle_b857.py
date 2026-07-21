"""B857: repository-bound callback dispatcher lifecycle.

Covers the acceptance surface owned by this card: exactly one
lifecycle-owned dispatcher per repository, idempotent convergence across
activation/tab-deserialization/reload, clean stop on workspace switch and
deactivation, explicit-target routing to the correct canonical transport,
unsupported-provider fail-closed (never fabricated delivery), and health
that distinguishes "not yet registered" from "broken".

Every dispatcher started here points at a temp outbox DB/state file (never
a shared repo-default) so this file stays parallel-safe: two dispatcher
tests running concurrently never touch the same on-disk outbox. B859: the
package-local ``callback_store.py`` module owns this schema now -- never
``AITools/taskdb.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_bridge, callback_store, core, task_store  # noqa: E402


def _outbox_kwargs(tmp_path: Path, name: str = "outbox") -> dict:
    db_path = tmp_path / f"{name}.sqlite"
    # Pre-create/init the outbox DB (WAL mode + schema) here, on the main
    # thread, before a dispatcher thread's own first _conn() call races to
    # set journal_mode=WAL on the same brand-new file -- avoids a spurious
    # "database is locked" on the very first concurrent open.
    conn = callback_store.open_db(db_path)
    callback_store.init_db(conn)
    conn.close()
    return {
        "db_path": db_path,
        "state_path": tmp_path / f"{name}_state.json",
    }


@pytest.fixture
def cleanup_dispatchers():
    """Stop every dispatcher this test registered, even on failure -- a
    stray daemon thread from one test must never leak into the next."""
    roots: list[Path] = []
    yield roots
    for root in roots:
        callback_bridge.stop_dispatcher(root)


def test_ensure_dispatcher_idempotent_same_provider(tmp_path, cleanup_dispatchers):
    root = tmp_path / "repo"
    cleanup_dispatchers.append(root)
    first = callback_bridge.ensure_dispatcher(
        root, "codex", bridge_kwargs=_outbox_kwargs(tmp_path)
    )
    assert first.is_running()
    second = callback_bridge.ensure_dispatcher(
        root, "codex", bridge_kwargs=_outbox_kwargs(tmp_path)
    )
    # Same object, same live thread -- a repeat activation/tab-deserialize/
    # reload call never spawns a second dispatcher for the same repo.
    assert second is first
    assert first.is_running()


def test_ensure_dispatcher_provider_switch_stops_old_starts_new(tmp_path, cleanup_dispatchers):
    root = tmp_path / "repo"
    cleanup_dispatchers.append(root)
    codex_dispatcher = callback_bridge.ensure_dispatcher(
        root, "codex", bridge_kwargs=_outbox_kwargs(tmp_path, "codex")
    )
    assert codex_dispatcher.is_running()

    claude_dispatcher = callback_bridge.ensure_dispatcher(
        root,
        "claude",
        repo_id="repo_switch_test",
        window_id="window_switch_test",
        bridge_kwargs=_outbox_kwargs(tmp_path, "claude"),
    )
    # An explicit coordinator-target switch stops the stale-provider
    # dispatcher first -- never two live pollers for one repository.
    assert codex_dispatcher.is_running() is False
    assert claude_dispatcher is not codex_dispatcher
    assert claude_dispatcher.is_running()
    assert callback_bridge.get_dispatcher(root) is claude_dispatcher


def test_stop_dispatcher_prevents_cross_repository_state(tmp_path, cleanup_dispatchers):
    root = tmp_path / "repo"
    dispatcher = callback_bridge.ensure_dispatcher(
        root, "codex", bridge_kwargs=_outbox_kwargs(tmp_path)
    )
    assert dispatcher.is_running()

    stopped = callback_bridge.stop_dispatcher(root)
    assert stopped is True
    assert dispatcher.is_running() is False
    assert callback_bridge.get_dispatcher(root) is None

    # A second stop on an already-unregistered repo is a clean no-op, not
    # an error -- deactivate()/workspace-switch may call it repeatedly.
    assert callback_bridge.stop_dispatcher(root) is False


def test_stop_all_dispatchers_stops_every_registered_repo(tmp_path):
    root_a = tmp_path / "repo_a"
    root_b = tmp_path / "repo_b"
    dispatcher_a = callback_bridge.ensure_dispatcher(
        root_a, "codex", bridge_kwargs=_outbox_kwargs(tmp_path, "a")
    )
    dispatcher_b = callback_bridge.ensure_dispatcher(
        root_b, "codex", bridge_kwargs=_outbox_kwargs(tmp_path, "b")
    )
    assert dispatcher_a.is_running()
    assert dispatcher_b.is_running()

    stopped_count = callback_bridge.stop_all_dispatchers()
    assert stopped_count == 2
    assert dispatcher_a.is_running() is False
    assert dispatcher_b.is_running() is False
    assert callback_bridge.get_dispatcher(root_a) is None
    assert callback_bridge.get_dispatcher(root_b) is None


def test_two_repositories_never_share_a_dispatcher(tmp_path, cleanup_dispatchers):
    root_a = tmp_path / "repo_a"
    root_b = tmp_path / "repo_b"
    cleanup_dispatchers.extend([root_a, root_b])
    dispatcher_a = callback_bridge.ensure_dispatcher(
        root_a, "codex", bridge_kwargs=_outbox_kwargs(tmp_path, "a")
    )
    dispatcher_b = callback_bridge.ensure_dispatcher(
        root_b, "codex", bridge_kwargs=_outbox_kwargs(tmp_path, "b")
    )
    assert dispatcher_a is not dispatcher_b
    assert dispatcher_a.repo_root == root_a
    assert dispatcher_b.repo_root == root_b


def test_unsupported_provider_fails_closed_no_thread_no_fabricated_delivery(tmp_path, cleanup_dispatchers):
    root = tmp_path / "repo"
    cleanup_dispatchers.append(root)
    dispatcher = callback_bridge.ensure_dispatcher(
        root, "copilot", bridge_kwargs=_outbox_kwargs(tmp_path)
    )
    assert dispatcher.supported is False
    assert dispatcher.is_running() is False

    health = dispatcher.health()
    assert health["ok"] is False
    assert health["dispatcher_running"] is False
    assert health["provider_supported"] is False
    assert health["last_start_error"] == "unsupported_provider:copilot"
    # Visible, never silently treated as delivered.
    assert health["bridge"] is None


def test_dispatcher_health_reports_running_provider_repo_pending_and_error(tmp_path, cleanup_dispatchers):
    root = tmp_path / "repo"
    cleanup_dispatchers.append(root)
    dispatcher = callback_bridge.ensure_dispatcher(
        root, "codex", bridge_kwargs=_outbox_kwargs(tmp_path)
    )
    health = dispatcher.health()
    assert health["dispatcher_running"] is True
    assert health["provider"] == "codex"
    assert health["repo_root"] == str(root)
    assert health["pending_count"] == 0
    assert health["last_delivery_error"] == ""

    registry_health = callback_bridge.dispatcher_health(root)
    assert registry_health["registered"] is True
    assert registry_health["dispatcher_running"] is True


def test_dispatcher_health_unregistered_repo_is_not_degraded():
    """An otherwise-healthy repository with no dispatcher registered yet
    (e.g. never activated) must never read as a broken/degraded dispatcher."""
    health = callback_bridge.dispatcher_health(Path("/tmp/never_registered_repo_b857"))
    assert health["ok"] is True
    assert health["dispatcher_running"] is False
    assert health["registered"] is False


def test_core_dispatcher_ensure_started_uninitialized_repo_not_degraded(tmp_path, monkeypatch):
    root = tmp_path / "uninitialized_repo"
    root.mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    result = core.dispatcher_ensure_started()
    assert result["ok"] is True
    assert result["status"] == "uninitialized"
    assert result["dispatcher_started"] is False
    # No dispatcher thread was ever spawned for an uninitialized repo.
    assert callback_bridge.get_dispatcher(root) is None


def test_core_dispatcher_ensure_started_then_stop_on_initialized_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    init = task_store.initialize_repository(root)
    assert init["ok"], init
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "window_core_test")
    # B859: no separate outbox DB / monkeypatch needed -- an initialized
    # repository's CallbackBridge resolves the SAME canonical
    # .aiworkhub/tasking/task_queue.sqlite task_store.py just created,
    # through callback_store.resolve_db_path(), never a second database.
    try:
        started = core.dispatcher_ensure_started()
        assert started["ok"] is True
        assert started["status"] == "started"
        assert started["dispatcher_started"] is True
        assert started["provider"] == "codex"

        # Idempotent: a repeated call (tab-deserialize/reload) is a no-op,
        # never a second thread.
        started_again = core.dispatcher_ensure_started()
        assert started_again["dispatcher_started"] is True

        health = core.dispatcher_health()
        assert health["registered"] is True
        assert health["dispatcher_running"] is True

        stopped = core.dispatcher_stop()
        assert stopped["ok"] is True
        assert stopped["stopped"] is True

        health_after_stop = core.dispatcher_health()
        assert health_after_stop["registered"] is False
        assert health_after_stop["dispatcher_running"] is False
    finally:
        callback_bridge.stop_dispatcher(root)
