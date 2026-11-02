"""NF-2026-01962: a polling manager inbox is healthy; a stale backlog expires.

Field report: ``aiworkhub_dispatcher_health`` returned ``ok:false`` with
``manager_inbox_no_live_delivery`` and ``backlog_count:59`` (oldest pending
2026-10-15) for a Claude Code manager that receives callbacks by polling
``callback_wait`` -- the backlog never drained. This locks:

* a Claude manager_inbox route with a FRESH pending callback is healthy and
  names ``delivery_mode='poll'``; only a backlog older than
  ``callback_store.CALLBACK_BACKLOG_MAX_AGE_SECONDS`` is a problem;
* ``callback_store.expire_stale_pending_backlog`` moves pending rows older
  than the bound to ``superseded``/``expired_backlog`` with ONE audit event
  carrying the count and bound, never touching young pending, inflight or
  delivered rows.
"""
from __future__ import annotations

import json
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_store, core  # noqa: E402

_UNREGISTERED = {"dispatcher_running": False, "registered": False, "repo_id": "", "last_start_error": ""}
_BOUND = callback_store.CALLBACK_BACKLOG_MAX_AGE_SECONDS


def _patch_claude_inbox(monkeypatch, *, backlog: int, oldest: str) -> None:
    identity = {"provider": "claude", "session_id": str(uuid.uuid4()), "window_id": "claude_vscode_1"}
    readiness = types.SimpleNamespace(ready=True, repo_id="repo_canon", reason="")
    monkeypatch.setattr(core, "repo_root", lambda: Path("/tmp/aiworkhub-nf01962-repo"))
    monkeypatch.setattr(core.task_store, "storage_readiness", lambda root: readiness)
    # A verified Claude manager is judged on its own route only; the
    # repo-wide figure is deliberately empty to prove it is not the source.
    monkeypatch.setattr(core.task_store, "callback_bridge_health",
                        lambda root: {"backlog_count": 0, "oldest_pending_at": ""})
    monkeypatch.setattr(core, "_verified_manager_backlog",
                        lambda provider, origin: {"backlog_count": backlog, "oldest_pending_at": oldest})
    fake_bridge = types.SimpleNamespace(dispatcher_health=lambda root: dict(_UNREGISTERED))
    monkeypatch.setattr(core, "_callback_bridge_module", lambda: fake_bridge)
    monkeypatch.setattr(core, "read_selected_coordinator_target",
                        lambda root=None: {"selected_provider": "claude"})
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: identity)
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "")
    monkeypatch.setenv("AIWORKHUB_CALLBACK_TRANSPORT", "")


# --- health: polling is a supported delivery mode --------------------------

def test_claude_manager_inbox_with_fresh_pending_is_healthy_poll(monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _patch_claude_inbox(monkeypatch, backlog=1, oldest=fresh)
    h = core.dispatcher_health()
    assert h["status"] == "manager_inbox" and h["dispatch_expected"] is True
    assert h["delivery_mode"] == "poll"
    assert h["ok"] is True and h["healthy"] is True
    assert "manager_inbox_no_live_delivery" not in h["problems"]
    assert h["problems"] == []
    assert h["backlog_count"] == 1 and h["backlog_max_age_seconds"] == _BOUND


def test_claude_manager_inbox_backlog_older_than_bound_is_flagged(monkeypatch):
    stale = (datetime.now(timezone.utc) - timedelta(seconds=_BOUND + 3600)).isoformat()
    _patch_claude_inbox(monkeypatch, backlog=59, oldest=stale)
    h = core.dispatcher_health()
    assert h["delivery_mode"] == "poll"
    assert h["ok"] is False and h["problems"] == ["manager_inbox_no_live_delivery"]
    assert h["backlog_max_age_seconds"] == _BOUND


def test_health_never_runs_the_sweep(monkeypatch):
    fresh = datetime.now(timezone.utc).isoformat()
    _patch_claude_inbox(monkeypatch, backlog=1, oldest=fresh)

    def forbidden(*args, **kwargs):
        raise AssertionError("dispatcher_health must stay read-only")

    monkeypatch.setattr(callback_store, "expire_stale_pending_backlog", forbidden)
    assert core.dispatcher_health()["ok"] is True


# --- sweep: expire only pending rows older than the bound ------------------

_MANAGER_ORIGIN = "origin-1"


def _insert_row(
    conn, task_id: str, state: str, created: datetime, batch_id: str = "",
    *, provider: str = "claude", origin: str = _MANAGER_ORIGIN,
) -> None:
    stamp = created.isoformat()
    conn.execute(
        "INSERT INTO callback_outbox(task_id, provider, origin_thread_id, batch_id, transition, "
        "state, created_at, updated_at) VALUES (?, ?, ?, ?, 'review_ready', ?, ?, ?)",
        (task_id, provider, origin, batch_id, state, stamp, stamp),
    )


def _insert_batch(
    conn, batch_id: str, state: str, created: datetime, *, lease_expires: datetime | None = None,
) -> None:
    stamp = created.isoformat()
    lease = lease_expires.isoformat() if lease_expires is not None else ""
    conn.execute(
        "INSERT INTO callback_batches(batch_id, provider, origin_thread_id, state, created_at, "
        "updated_at, member_count, lease_expires_at) VALUES (?, 'claude', ?, ?, ?, ?, 1, ?)",
        (batch_id, _MANAGER_ORIGIN, state, stamp, stamp, lease),
    )


def _states(conn) -> dict[str, tuple[str, str]]:
    return {
        row["task_id"]: (row["state"], row["last_error"])
        for row in conn.execute("SELECT task_id, state, last_error FROM callback_outbox")
    }


def test_sweep_expires_only_old_pending_rows_with_one_audit_event(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    try:
        callback_store.init_db(conn)
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=_BOUND + 86400)
        young = now - timedelta(seconds=_BOUND - 3600)
        _insert_row(conn, "OLD_1", "pending", old)
        _insert_row(conn, "OLD_2", "pending", old - timedelta(days=3))
        # A dead / other session of the SAME provider drains too.
        _insert_row(conn, "OLD_DEAD_SESSION", "pending", old, origin="origin-dead")
        _insert_batch(conn, "batch_pending_old", "pending", old)
        _insert_row(conn, "OLD_IN_PENDING_BATCH", "pending", old, batch_id="batch_pending_old")
        # Inflight batch whose lease EXPIRED: its pending member is stale -> swept.
        _insert_batch(conn, "batch_lease_expired", "inflight", old,
                      lease_expires=now - timedelta(hours=1))
        _insert_row(conn, "OLD_IN_EXPIRED_LEASE", "pending", old, batch_id="batch_lease_expired")
        # Inflight batch with a LIVE lease: left alone.
        _insert_batch(conn, "batch_lease_live", "inflight", old,
                      lease_expires=now + timedelta(hours=1))
        _insert_row(conn, "OLD_IN_LIVE_LEASE", "pending", old, batch_id="batch_lease_live")
        # Another provider's row never moves on a Claude bootstrap sweep.
        _insert_row(conn, "OLD_CODEX", "pending", old, provider="codex", origin="codex-thread")
        _insert_row(conn, "YOUNG_1", "pending", young)
        _insert_row(conn, "YOUNG_2", "pending", now)
        _insert_row(conn, "OLD_INFLIGHT", "inflight", old)
        _insert_row(conn, "OLD_DELIVERED", "delivered", old)
        conn.commit()

        result = callback_store.expire_stale_pending_backlog(conn, provider="claude", now=now)

        swept = ("OLD_1", "OLD_2", "OLD_DEAD_SESSION", "OLD_IN_PENDING_BATCH", "OLD_IN_EXPIRED_LEASE")
        assert result["expired"] == len(swept)
        assert result["batches_superseded"] == 2
        assert result["max_age_seconds"] == _BOUND
        states = _states(conn)
        for task_id in swept:
            assert states[task_id] == ("superseded", callback_store.EXPIRED_BACKLOG_REASON)
        assert states["OLD_CODEX"] == ("pending", "")
        assert states["OLD_IN_LIVE_LEASE"] == ("pending", "")
        assert states["YOUNG_1"] == ("pending", "")
        assert states["YOUNG_2"] == ("pending", "")
        assert states["OLD_INFLIGHT"] == ("inflight", "")
        assert states["OLD_DELIVERED"] == ("delivered", "")
        batches = {row["batch_id"]: row["state"] for row in conn.execute(
            "SELECT batch_id, state FROM callback_batches")}
        assert batches == {
            "batch_pending_old": "superseded",
            "batch_lease_expired": "superseded",
            "batch_lease_live": "inflight",
        }

        events = conn.execute(
            "SELECT payload_json FROM task_events WHERE event=?",
            (callback_store.CALLBACK_BACKLOG_EXPIRED_EVENT,),
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload_json"])
        assert payload["count"] == len(swept)
        assert payload["provider"] == "claude"
        assert payload["max_age_seconds"] == _BOUND
        assert payload["reason"] == "expired_backlog"
        audited = {row["task_id"]: (row["provider"], row["origin_thread_id"]) for row in payload["superseded"]}
        assert audited == {
            "OLD_1": ("claude", _MANAGER_ORIGIN),
            "OLD_2": ("claude", _MANAGER_ORIGIN),
            "OLD_DEAD_SESSION": ("claude", "origin-dead"),
            "OLD_IN_PENDING_BATCH": ("claude", _MANAGER_ORIGIN),
            "OLD_IN_EXPIRED_LEASE": ("claude", _MANAGER_ORIGIN),
        }

        # Afterwards the manager's backlog -- the exact source dispatcher_health
        # judges for a verified manager -- counts only the young pending rows
        # (the live-lease member is in delivery, not backlog).
        backlog = callback_store.manager_inbox_backlog(
            conn, provider="claude", origin_thread_id=_MANAGER_ORIGIN, now=now)
        assert backlog == {"backlog_count": 2, "oldest_pending_at": young.isoformat()}

        # A second sweep finds nothing and writes no further audit event.
        again = callback_store.expire_stale_pending_backlog(conn, provider="claude", now=now)
        assert again["expired"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE event=?",
            (callback_store.CALLBACK_BACKLOG_EXPIRED_EVENT,),
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_health_backlog_source_reads_the_real_store(tmp_path, monkeypatch):
    # core._verified_manager_backlog is what dispatcher_health calls for a
    # verified manager; drive it against a real store, scoped to one route.
    db_path = tmp_path / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        now = datetime.now(timezone.utc)
        _insert_row(conn, "MINE", "pending", now)
        _insert_row(conn, "OTHER_SESSION", "pending", now - timedelta(days=30), origin="origin-dead")
        _insert_row(conn, "OTHER_PROVIDER", "pending", now - timedelta(days=30),
                    provider="codex", origin=_MANAGER_ORIGIN)
        conn.commit()
    finally:
        conn.close()

    def connect(*, readonly=False):
        assert readonly is True, "health backlog read must be read-only"
        return callback_store.open_db(db_path)

    monkeypatch.setattr(core, "_canonical_connect", connect)
    backlog = core._verified_manager_backlog("claude", _MANAGER_ORIGIN)
    assert backlog == {"backlog_count": 1, "oldest_pending_at": now.isoformat()}


def test_sweep_requires_the_callers_provider(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    try:
        callback_store.init_db(conn)
        try:
            callback_store.expire_stale_pending_backlog(conn, provider="")
        except ValueError:
            pass
        else:
            raise AssertionError("an unscoped sweep must be refused")
    finally:
        conn.close()


def test_sweep_never_expires_rows_younger_than_bound(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    try:
        callback_store.init_db(conn)
        now = datetime.now(timezone.utc)
        _insert_row(conn, "EDGE", "pending", now - timedelta(seconds=_BOUND - 1))
        _insert_row(conn, "UNREADABLE", "pending", now)
        conn.execute("UPDATE callback_outbox SET created_at='not-a-time' WHERE task_id='UNREADABLE'")
        conn.commit()
        result = callback_store.expire_stale_pending_backlog(conn, provider="claude", now=now)
        assert result["expired"] == 0
        assert _states(conn) == {"EDGE": ("pending", ""), "UNREADABLE": ("pending", "")}
        assert conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"] == 0
    finally:
        conn.close()
