"""Regression tests for fix batch 14 (NF-2026-00019 and siblings)."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from aiworkhub import callback_bridge, process_event_ledger
from aiworkhub.route_identity import CoordinatorRouteKey


_WORKER_WORKSPACE_TESTS = Path(__file__).with_name("test_worker_workspace.py")


# --- NF-2026-00024 / NF-2026-00019: byte-exact fixtures on Windows ----------


def test_npm_prefix_seeding_fixtures_are_written_byte_exact() -> None:
    """write_text translates "\\n" to CRLF on Windows, so a fixture that is later
    compared byte-for-byte against its seeded worktree copy must be bytes."""

    source = _WORKER_WORKSPACE_TESTS.read_text(encoding="utf-8")
    for name in (
        '"README.md"',
        '"aiworkhub-app-server-mux"',
        '"aiworkhub-app-server-mux.cmd"',
    ):
        assert not re.search(re.escape(name) + r"\)\.write_text\(", source), name
    assert '(repo / "README.md").write_bytes(b"# AIWorkHub\\n")' in source
    assert '.write_bytes(b"@echo off\\r\\n")' in source


# --- NF-2026-00240: health tells push delivery from a passive mailbox -------


def test_claude_adapter_delivery_mode_distinguishes_push_from_mailbox() -> None:
    passive = callback_bridge.PASSIVE_MAILBOX_DELIVERY
    assert callback_bridge.ClaudeCallbackAdapter().delivery_mode == passive
    # A transport object alone is not evidence that a session answers.
    assert callback_bridge.ClaudeCallbackAdapter(cli_client=object()).delivery_mode == passive
    paneled = callback_bridge.ClaudeCallbackAdapter(
        wake_transport=lambda **_: {"delivered": True}
    )
    assert paneled.delivery_mode == passive
    paneled.deliver_or_defer(_route())
    assert paneled.delivery_mode == callback_bridge.PUSH_DELIVERY


def test_dispatcher_health_reports_delivery_mode(tmp_path: Path) -> None:
    repo = tmp_path / "unregistered"
    scoped = callback_bridge.dispatcher_health(repo, provider="claude")
    assert scoped["delivery_mode"] == callback_bridge.PASSIVE_MAILBOX_DELIVERY
    # The unscoped legacy shape is unchanged.
    assert "delivery_mode" not in callback_bridge.dispatcher_health(repo)

    try:
        callback_bridge.ensure_claude_delivery(
            tmp_path,
            repo_id="repo-x",
            window_id="window-x",
            bridge_kwargs={
                "db_path": tmp_path / "task_queue.sqlite",
                "state_path": tmp_path / "state.json",
            },
        )
        health = callback_bridge.dispatcher_health(tmp_path, provider="claude")
        assert health["dispatcher_running"] is True
        # Running but no session has acknowledged yet: still a mailbox.
        assert health["delivery_mode"] == callback_bridge.PASSIVE_MAILBOX_DELIVERY
    finally:
        callback_bridge.stop_dispatcher(tmp_path)


# --- NF-2026-00546: terminal cause survives a causeless workspace-GC row ----


def test_terminal_cause_survives_workspace_gc_row(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    process_event_ledger.append_event(
        path, {"request_id": "r-1", "state": "worker_failed", "error": "disk full on /x"}
    )
    process_event_ledger.append_event(
        path, {"request_id": "r-1", "state": "worker_failed", "workspace_gc": True}
    )
    latest = process_event_ledger.latest_events(path)["r-1"]
    assert latest["terminal_reason"]["missing_cause"] is False
    assert latest["terminal_reason"]["message"] == "disk full on /x"


# --- NF-2026-00855: idempotent ``starting`` append --------------------------


def _rows(path: Path) -> list[dict]:
    return list(process_event_ledger.iter_events(path))


def test_starting_row_is_idempotent_on_request_epoch_and_spawn(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    start = {"request_id": "r-1", "state": "starting", "claim_epoch": 1, "pid": 41}
    process_event_ledger.append_event(path, {**start, "timestamp": "t1"})
    process_event_ledger.append_event(path, {**start, "timestamp": "t2"})
    assert len(_rows(path)) == 1

    # A later lifecycle row does not reopen the spawn's identity.
    process_event_ledger.append_event(
        path, {"request_id": "r-1", "state": "running", "claim_epoch": 1}
    )
    process_event_ledger.append_event(path, {**start, "timestamp": "t3"})
    assert [row["state"] for row in _rows(path)] == ["starting", "running"]

    # A cold process (no digest index) still sees the persisted spawn.
    process_event_ledger.reset_append_digest_cache()
    process_event_ledger.append_event(path, {**start, "timestamp": "t4"})
    assert len(_rows(path)) == 2

    # A new claim epoch or a new spawn is a distinct start and is written.
    process_event_ledger.append_event(path, {**start, "claim_epoch": 2})
    process_event_ledger.append_event(path, {**start, "claim_epoch": 2, "pid": 42})
    assert len(_rows(path)) == 4


def test_starting_row_without_claim_epoch_is_always_written(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    for _ in range(2):
        process_event_ledger.append_event(
            path, {"request_id": "r-1", "state": "starting"}
        )
    assert len(_rows(path)) == 2


def _launcher_reservation_row(**overrides: object) -> dict:
    """The field set process_launcher.py writes for a ``starting`` reservation."""

    row: dict = {
        "request_id": "r-real",
        "task_id": "task-real",
        "runner": "claude",
        "topic": "quality_review",
        "adapter_id": "claude-cli",
        "model": "claude-opus-5-5",
        "state": "starting",
        "reservation_expires_at_epoch": 1700000120.0,
        "owner_pid": 1234,
        "owner_pid_start_ticks": 987654,
        "timeout_seconds": 600,
        "claim_epoch": 3,
        "pid": 41,
    }
    row.update(overrides)
    return row


def test_starting_dedupe_keys_on_real_writer_spawn_fields(tmp_path: Path) -> None:
    assert process_event_ledger._SPAWN_IDENTITY_FIELDS == (
        "owner_pid",
        "owner_pid_start_ticks",
        "pid",
        "supervisor_pid",
        "supervisor_pid_start_ticks",
    )
    path = tmp_path / "process_events.jsonl"
    process_event_ledger.append_event(path, _launcher_reservation_row())
    # Same request_id and claim_epoch, different pid: a distinct spawn.
    process_event_ledger.append_event(path, _launcher_reservation_row(pid=42))
    assert [row["pid"] for row in _rows(path)] == [41, 42]
    # A re-announcement of the same spawn (only the expiry moved) is a repeat.
    process_event_ledger.append_event(
        path, _launcher_reservation_row(reservation_expires_at_epoch=1700000240.0)
    )
    assert len(_rows(path)) == 2
    # A reused owner pid with a new start tick is a different process.
    process_event_ledger.append_event(
        path, _launcher_reservation_row(owner_pid_start_ticks=111)
    )
    assert len(_rows(path)) == 3


def test_starting_row_without_spawn_identity_is_always_written(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    base = {"request_id": "r-1", "state": "starting", "claim_epoch": 1}
    process_event_ledger.append_event(path, {**base, "adapter_id": "a"})
    process_event_ledger.append_event(path, {**base, "adapter_id": "b"})
    assert [row["adapter_id"] for row in _rows(path)] == ["a", "b"]


def test_starting_keys_are_kept_apart_from_request_digests(tmp_path: Path) -> None:
    process_event_ledger.reset_append_digest_cache()
    path = tmp_path / "process_events.jsonl"
    row = {"request_id": "r-1", "state": "starting", "claim_epoch": 1, "pid": 7}
    process_event_ledger.append_event(path, row)
    process_event_ledger.append_event(path, {**row, "timestamp": "again"})
    assert len(_rows(path)) == 1
    index = process_event_ledger._APPEND_DIGESTS[str(path.resolve(strict=False))]
    key = process_event_ledger._starting_identity_key(row)
    assert key is not None
    assert key in index.starting_keys
    assert key not in index.digests
    assert set(index.digests) == {"r-1"}


# --- NF-2026-00240: registered-dispatcher health fails closed ---------------


class _FakeDispatcher:
    def __init__(
        self,
        provider: str,
        health: dict,
        adapter: object = None,
        *,
        repo_id: str = "repo-x",
        window_id: str = "window-x",
    ) -> None:
        self.provider = provider
        self.repo_id = repo_id
        self.window_id = window_id
        self._health = health
        self._adapter = adapter

    def health(self) -> dict:
        return dict(self._health)

    def claude_adapter(self) -> object:
        return self._adapter


def test_registered_health_reports_push_only_on_positive_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    fake = _FakeDispatcher("codex", {"dispatcher_running": True})
    monkeypatch.setattr(callback_bridge, "get_dispatcher", lambda _root: fake)
    passive = callback_bridge.PASSIVE_MAILBOX_DELIVERY
    # A missing provider_supported flag is not support.
    assert callback_bridge.dispatcher_health(tmp_path, "codex")["delivery_mode"] == passive
    fake._health["provider_supported"] = True
    assert (
        callback_bridge.dispatcher_health(tmp_path, "codex")["delivery_mode"]
        == callback_bridge.PUSH_DELIVERY
    )
    # The dispatcher must serve the asked-about route.
    assert callback_bridge.dispatcher_health(tmp_path, "claude")["delivery_mode"] == passive
    fake._health["dispatcher_running"] = False
    assert callback_bridge.dispatcher_health(tmp_path, "codex")["delivery_mode"] == passive
    # Unscoped readout carries no delivery_mode, as on the unregistered branch.
    assert "delivery_mode" not in callback_bridge.dispatcher_health(tmp_path)


class _AckingCliClient:
    """A resume client whose session acknowledges, or fails as configured."""

    def __init__(self) -> None:
        self.error: Exception | None = None

    def deliver_callback_batch(self, session_id, members, *, client_user_message_id=None):
        if self.error is not None:
            raise self.error
        return {"ok": True}


def _route(repo_id: str = "repo-x", window_id: str = "window-x") -> CoordinatorRouteKey:
    return CoordinatorRouteKey(
        repo_id=repo_id,
        provider="claude",
        window_id=window_id,
        task_id="task-1",
        session_id="session-1",
    )


def _claude_health(tmp_path: Path) -> dict:
    return callback_bridge.dispatcher_health(tmp_path, "claude")


def test_claude_health_with_cli_client_but_no_live_session_is_passive(
    tmp_path: Path, monkeypatch
) -> None:
    running = {"dispatcher_running": True, "provider_supported": True}
    adapter = callback_bridge.ClaudeCallbackAdapter(cli_client=_AckingCliClient())
    fake = _FakeDispatcher("claude", running, adapter)
    monkeypatch.setattr(callback_bridge, "get_dispatcher", lambda _root: fake)
    health = _claude_health(tmp_path)
    assert health["delivery_mode"] == callback_bridge.PASSIVE_MAILBOX_DELIVERY
    assert health["delivery_mode_reason"] == "claude_no_live_session"
    # No adapter at all, and an adapter without any transport, are passive too.
    fake._adapter = None
    assert _claude_health(tmp_path)["delivery_mode_reason"] == "claude_adapter_not_built"
    fake._adapter = callback_bridge.ClaudeCallbackAdapter()
    assert (
        _claude_health(tmp_path)["delivery_mode_reason"]
        == "claude_wake_transport_unavailable"
    )


def test_claude_health_pushes_only_for_live_session_on_matching_route(
    tmp_path: Path, monkeypatch
) -> None:
    running = {"dispatcher_running": True, "provider_supported": True}
    client = _AckingCliClient()
    adapter = callback_bridge.ClaudeCallbackAdapter(cli_client=client)
    adapter.deliver_or_defer(_route(), payload={"members": [{"task_id": "task-1"}]})
    assert adapter.delivery_mode == callback_bridge.PUSH_DELIVERY
    fake = _FakeDispatcher("claude", running, adapter)
    monkeypatch.setattr(callback_bridge, "get_dispatcher", lambda _root: fake)
    health = _claude_health(tmp_path)
    assert health["delivery_mode"] == callback_bridge.PUSH_DELIVERY
    assert health["delivery_mode_reason"] == ""

    # Mismatched repo_id: the same route check delivery performs fails closed.
    fake.repo_id = "repo-other"
    health = _claude_health(tmp_path)
    assert health["delivery_mode"] == callback_bridge.PASSIVE_MAILBOX_DELIVERY
    assert health["delivery_mode_reason"] == "claude_route_repo_mismatch"
    fake.repo_id = ""
    assert _claude_health(tmp_path)["delivery_mode_reason"] == "claude_route_repo_mismatch"
    fake.repo_id = "repo-x"
    fake.window_id = "window-other"
    assert _claude_health(tmp_path)["delivery_mode_reason"] == "claude_route_window_mismatch"
    fake.window_id = "window-x"

    # A transport failure withdraws the live-session evidence.
    client.error = callback_bridge.ClaudeCliUnavailableError("gone")
    adapter.deliver_or_defer(_route(), payload={"members": [{"task_id": "task-1"}]})
    assert adapter.live_route is None
    assert _claude_health(tmp_path)["delivery_mode_reason"] == "claude_no_live_session"


def test_claude_health_does_not_construct_an_adapter(tmp_path: Path) -> None:
    try:
        callback_bridge.ensure_claude_delivery(
            tmp_path,
            repo_id="repo-x",
            window_id="window-x",
            bridge_kwargs={
                "db_path": tmp_path / "task_queue.sqlite",
                "state_path": tmp_path / "state.json",
            },
        )
        dispatcher = callback_bridge.get_dispatcher(tmp_path)
        assert dispatcher is not None
        for _ in range(2):
            health = _claude_health(tmp_path)
            assert health["delivery_mode"] == callback_bridge.PASSIVE_MAILBOX_DELIVERY
            assert health["delivery_mode_reason"] == "claude_adapter_not_built"
        assert dispatcher.claude_adapter() is None
        assert dispatcher._bridge.existing_claude_adapter() is None
        # Delivery's lazy build is single-instance under concurrency.
        bridge = dispatcher._bridge
        built: list[object] = []
        threads = [
            threading.Thread(target=lambda: built.append(bridge.claude_adapter()))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len({id(adapter) for adapter in built}) == 1
        assert dispatcher.claude_adapter() is built[0]
    finally:
        callback_bridge.stop_dispatcher(tmp_path)


# --- NF-2026-00855 rework: placeholder spawn fields are not identity --------


def test_starting_rows_with_only_placeholder_spawn_fields_are_all_written(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    base = {"request_id": "r-1", "state": "starting", "claim_epoch": 1}
    placeholders = {name: None for name in process_event_ledger._SPAWN_IDENTITY_FIELDS}
    process_event_ledger.append_event(path, {**base, **placeholders, "timestamp": "t1"})
    process_event_ledger.append_event(path, {**base, **placeholders, "timestamp": "t2"})
    assert len(_rows(path)) == 2
    assert process_event_ledger._starting_identity_key({**base, **placeholders}) is None
    blanks = {"owner_pid": 0, "pid": "", "supervisor_pid": None}
    assert process_event_ledger._starting_identity_key({**base, **blanks}) is None


def test_starting_rows_with_identical_real_pid_and_ticks_are_deduped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    row = {
        "request_id": "r-1",
        "state": "starting",
        "claim_epoch": 1,
        "owner_pid": None,
        "pid": 41,
        "supervisor_pid_start_ticks": 5150,
    }
    process_event_ledger.append_event(path, {**row, "timestamp": "t1"})
    process_event_ledger.append_event(path, {**row, "timestamp": "t2"})
    assert len(_rows(path)) == 1
