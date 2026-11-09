"""NF-2026-02197: Stop ends the running Manager Chat turn, wherever that turn is."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest

from aiworkhub import manager_loop_backends as mlb
from aiworkhub import manager_loop_service
from test_manager_loop import FakeBackend, Harness, _terminals, make  # noqa: F401
from test_manager_loop_service import _install_fakes, _run_turn

_WAIT = 10
_KILLED = {"type": "error", "payload": {"source": "backend", "error": "killed"}}
_OVER_THRESHOLD = "x" * 2000
_ROTATES_EARLY = {"brief_bytes": 1024, "context_window_bytes": 2200, "rotate_fraction": 0.5}


def _event_types(harness: Harness, session_id: str) -> list[str]:
    return [event["type"] for event in harness.store.events(session_id)]


def test_a_stopped_turn_with_a_partial_reply_starts_no_handoff_and_a_later_turn_still_rotates(make) -> None:
    harness = make(**_ROTATES_EARLY)
    session = harness.orch.start("fake", "model-a")
    backend = harness.backends[-1]
    backend.gate = threading.Event()
    backend.script.append([{"type": "assistant_text", "payload": {"text": "partial"}}, _KILLED])
    done: list[dict[str, Any]] = []
    runner = threading.Thread(target=lambda: done.append(harness.orch.send(_OVER_THRESHOLD)), daemon=True)
    runner.start()
    try:
        assert backend.entered.wait(timeout=_WAIT)
        assert harness.orch.interrupt() is True
    finally:
        backend.gate.set()
        runner.join(timeout=_WAIT)
    backend.gate = None
    assert not runner.is_alive() and len(done) == 1

    assert done[0]["reply"] == "partial"
    assert done[0]["context_estimate_bytes"] >= harness.orch.rotate_at_bytes
    assert done[0]["rotation"] is None
    assert _terminals(harness, session.session_id, 1) == ["turn_stopped"]
    assert "handoff_request" not in _event_types(harness, session.session_id)
    assert backend.messages == [_OVER_THRESHOLD]

    later = harness.orch.send("again")

    assert later["rotation"] is not None
    assert "handoff_request" in _event_types(harness, session.session_id)
    assert len(backend.messages) == 3


def _hold_bind(
    monkeypatch: Any, result: tuple[Any, Any] | None = None
) -> tuple[threading.Event, threading.Event, Callable[..., Any]]:
    """Block ``_bind_turn_session`` on an event; return (entered, release, the real bind)."""
    entered, release = threading.Event(), threading.Event()
    real = manager_loop_service._bind_turn_session

    def held(entry: Any, repo: Any, **options: Any) -> Any:
        entered.set()
        assert release.wait(timeout=_WAIT)
        return result if result is not None else real(entry, repo, **options)

    monkeypatch.setattr(manager_loop_service, "_bind_turn_session", held)
    return entered, release, real


def _send_in_background(repo: Path, text: str) -> tuple[threading.Thread, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    sender = threading.Thread(
        target=lambda: sent.append(manager_loop_service.send(repo, text)), daemon=True
    )
    sender.start()
    return sender, sent


def test_stop_during_the_bind_ends_the_turn_without_a_backend_send(monkeypatch: Any, tmp_path: Path) -> None:
    backends = _install_fakes(monkeypatch)
    manager_loop_service.start(tmp_path, "fake", "model-a")
    entry = manager_loop_service._entry_for(tmp_path)
    session_id = entry.orchestrator.session.session_id
    entered, release, _ = _hold_bind(monkeypatch)
    sender, sent = _send_in_background(tmp_path, "one")
    try:
        assert entered.wait(timeout=_WAIT)
        stopped = manager_loop_service.stop(tmp_path)
    finally:
        release.set()
        sender.join(timeout=_WAIT)
    try:
        assert not sender.is_alive() and sent[0]["ok"] is True
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=_WAIT) is True

        assert stopped == {"ok": True, "stopped": False, "armed": True, "dropped_queued": 0}
        assert backends[0].messages == []
        kinds = [event["type"] for event in entry.orchestrator.store.events(session_id)]
        assert "turn_stopped" in kinds and "turn_end" not in kinds
    finally:
        manager_loop_service.close(tmp_path)


def test_a_failed_bind_clears_the_running_turn_and_the_armed_stop(monkeypatch: Any, tmp_path: Path) -> None:
    backends = _install_fakes(monkeypatch)
    manager_loop_service.start(tmp_path, "fake", "model-a")
    entry = manager_loop_service._entry_for(tmp_path)
    session_id = entry.orchestrator.session.session_id
    _run_turn(tmp_path, "first")
    refusal = {"ok": False, "error": "bind_failed"}
    entered, release, real_bind = _hold_bind(monkeypatch, result=(None, refusal))
    sender, sent = _send_in_background(tmp_path, "two")
    try:
        assert entered.wait(timeout=_WAIT)
        assert entry.turn_running is True
        # No turn number exists until the bind, so the previous turn's must not linger.
        assert entry.running_turn is None
        # A message queued here would be stranded when the bind fails.
        assert manager_loop_service.send(tmp_path, "three") == {"ok": False, "error": "manager_turn_in_progress"}
        assert list(entry.send_queue) == []
        assert manager_loop_service.stop(tmp_path)["armed"] is True
    finally:
        release.set()
        sender.join(timeout=_WAIT)
    try:
        assert not sender.is_alive() and sent == [refusal]
        assert entry.turn_running is False
        assert entry.turn_lock.locked() is False
        assert entry.orchestrator._stop_armed is False

        monkeypatch.setattr(manager_loop_service, "_bind_turn_session", real_bind)
        _run_turn(tmp_path, "next")

        assert backends[0].messages == ["first", "next"]
        last = [
            event["type"] for event in entry.orchestrator.store.events(session_id)
            if event["type"] in {"turn_end", "turn_stopped", "error"}
        ]
        assert last == ["turn_end", "turn_end"]
    finally:
        manager_loop_service.close(tmp_path)


class _LateSpawnBackend(FakeBackend):
    """No process exists until ``spawn_allowed``: a ``close`` before it ends nothing."""

    first_event: dict[str, Any] = {}

    def __init__(self, backend_id: str, model: str) -> None:
        super().__init__(backend_id, model)
        self.spawn_allowed = threading.Event()
        self.spawned = False
        self.killed = False

    def send(self, message: str) -> Iterator[dict[str, Any]]:
        self.messages.append(message)
        return self._run()

    def _run(self) -> Iterator[dict[str, Any]]:
        self.entered.set()
        assert self.spawn_allowed.wait(timeout=_WAIT)
        self.spawned = True
        yield self.first_event
        yield _KILLED if self.killed else {"type": "turn_end", "payload": {}}

    def close(self) -> None:
        if self.spawned:
            self.killed = True


class _LateSpawnHarness(Harness):
    first_event: dict[str, Any] = {}

    def factory(self, backend_id: str, model: str) -> FakeBackend:
        backend = _LateSpawnBackend(backend_id, model)
        backend.first_event = self.first_event
        self.backends.append(backend)
        return backend


@pytest.mark.parametrize(
    "first_event",
    [
        {"type": "delta", "payload": {"kind": "assistant_text", "text": "par"}},
        {"type": "assistant_text", "payload": {"text": "partial"}},
    ],
    ids=["delta", "final_event"],
)
def test_a_stop_before_the_process_exists_ends_the_turn_at_its_first_event(
    tmp_path: Path, first_event: dict[str, Any]
) -> None:
    harness = _LateSpawnHarness(tmp_path)
    harness.first_event = first_event
    session = harness.orch.start("fake", "model-a")
    backend = harness.backends[-1]
    done: list[dict[str, Any]] = []
    runner = threading.Thread(target=lambda: done.append(harness.orch.send("work")), daemon=True)
    runner.start()
    try:
        assert backend.entered.wait(timeout=_WAIT)
        assert harness.orch.interrupt() is True
        assert backend.spawned is False
    finally:
        backend.spawn_allowed.set()
        runner.join(timeout=_WAIT)
    try:
        assert not runner.is_alive() and len(done) == 1
        assert backend.killed is True
        assert _terminals(harness, session.session_id, 1) == ["turn_stopped"]
        assert done[0]["ok"] is False
    finally:
        harness.orch.close()


class _HungProcess:
    """A CLI child that prints nothing until the platform facade ends it."""

    pid = 4321
    stderr = None

    def __init__(self) -> None:
        self.ended = threading.Event()
        self.stdout = self._lines()

    def _lines(self) -> Iterator[str]:
        assert self.ended.wait(timeout=_WAIT)
        yield from ()

    def poll(self) -> int | None:
        return 1 if self.ended.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        assert self.ended.wait(timeout)
        return 1

    def kill(self) -> None:
        self.ended.set()


def _hung_cli(repo: Path, process: _HungProcess, **options: Any) -> mlb.CliManagerBackend:
    plan = SimpleNamespace(
        argv=["codex", "exec", "-"], cwd=str(repo), launchable=True, stdin_text="go", validation_reason=""
    )
    return mlb.CliManagerBackend(
        "codex_cli",
        "fixture-model",
        repo,
        plan_builder=lambda *_args, **_kwargs: plan,
        spawn=lambda *_args: process,
        **options,
    )


def test_close_ends_the_tree_within_a_second_and_the_watchdog_keeps_the_default(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: list[tuple[int, dict[str, Any]]] = []
    live: list[_HungProcess] = []

    def terminate(pid: int, **options: Any) -> bool:
        calls.append((pid, options))
        live[-1].ended.set()
        return True

    monkeypatch.setattr(mlb.platform_io, "terminate_process_tree", terminate)

    live.append(_HungProcess())
    cli = _hung_cli(tmp_path, live[-1])
    runner = threading.Thread(target=lambda: list(cli.send("go")), daemon=True)
    runner.start()
    deadline = time.monotonic() + _WAIT
    while not cli.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cli.is_running()
    cli.close()
    runner.join(timeout=_WAIT)
    assert not runner.is_alive()
    assert len(calls) == 1 and calls[0][0] == _HungProcess.pid
    assert 0 < calls[0][1]["timeout"] <= 1

    calls.clear()
    live.append(_HungProcess())
    watched = _hung_cli(tmp_path, live[-1], timeout_seconds=0.05)
    events = list(watched.send("go"))

    assert calls == [(_HungProcess.pid, {})]
    assert [event["type"] for event in events] == ["error"]
    assert "manager_turn_timeout_seconds" in str(events[0]["payload"])
