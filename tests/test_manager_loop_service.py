from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import manager_loop as ml  # noqa: E402
from aiworkhub import manager_loop_service  # noqa: E402
from aiworkhub import server  # noqa: E402


class FakeManagerBackend:
    """A minimal scripted ManagerBackend: an optional gate holds a turn open."""

    def __init__(self, backend_id: str, model: str) -> None:
        self.backend_id = backend_id
        self.model = model
        self.briefs: list[str] = []
        self.messages: list[str] = []
        self.closed = False
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def start(self, brief: str) -> str:
        self.briefs.append(brief)
        return f"{self.backend_id}:{self.model}:{len(self.briefs)}"

    def send(self, message: str) -> Iterator[dict[str, Any]]:
        self.messages.append(message)
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=10)
        return iter([
            {"type": "assistant_text", "payload": {"text": f"ack: {message}"}},
            {"type": "turn_end", "payload": {}},
        ])

    def close(self) -> None:
        self.closed = True


class _FakeOrchestratorFactory:
    """Stands in for ``ManagerOrchestrator`` so tests never touch a real repo.

    ``for_repository`` mirrors what the real classmethod does in production
    (build a store, a brief builder, an orchestrator) but with the same
    fully-local fakes ``tests/test_manager_loop.py``'s own ``Harness`` uses,
    so ``manager_loop_service`` is exercised against a real
    ``ManagerOrchestrator``/``SessionStore`` without depending on
    ``repository_state`` or the real Context Graph writers.
    """

    @staticmethod
    def for_repository(
        repo: Any, backend_factory: Callable[[str, str], Any], **_options: Any
    ) -> ml.ManagerOrchestrator:
        root = Path(repo)
        store = ml.SessionStore(root / "manager_loop", f"repo:{root.name}")
        builder = ml.BriefBuilder(
            open_cards=lambda: "",
            state=lambda: {},
            context=lambda: "",
            rules=lambda: "",
            max_bytes=4096,
        )
        issued = {"n": 0}

        def new_id() -> str:
            issued["n"] += 1
            return f"sess-{issued['n']:04d}"

        return ml.ManagerOrchestrator(
            store,
            backend_factory=backend_factory,
            brief_builder=builder,
            event_writer=lambda **fields: {"ok": True},
            session_writer=lambda **fields: {"ok": True},
            new_id=new_id,
        )


def _install_fakes(monkeypatch: Any) -> list[FakeManagerBackend]:
    backends: list[FakeManagerBackend] = []

    def factory(repo: Any) -> Callable[[str, str], FakeManagerBackend]:
        def build(backend_id: str, model: str) -> FakeManagerBackend:
            backend = FakeManagerBackend(backend_id, model)
            backends.append(backend)
            return backend

        return build

    def no_wake_source(**_kwargs: Any) -> tuple[Callable[[], Any], Callable[[str, str], bool]]:
        return (lambda: None, lambda batch_id, lease_id: True)

    monkeypatch.setattr(manager_loop_service, "ManagerOrchestrator", _FakeOrchestratorFactory)
    monkeypatch.setattr(manager_loop_service, "manager_backend_factory", factory)
    # Every start() now also launches a wake consumer; keep it off the real
    # callback outbox and fast so it never touches a database in a test.
    monkeypatch.setattr(manager_loop_service, "default_callback_source", no_wake_source)
    monkeypatch.setattr(manager_loop_service, "WAKE_IDLE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(manager_loop_service, "WAKE_RETRY_POLL_SECONDS", 0.05)
    return backends


def test_send_runs_in_background_and_status_and_events_catch_up_once_finished(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    backend = backends[0]
    backend.gate = threading.Event()

    result = manager_loop_service.send(tmp_path, "hello")
    assert result["ok"] is True
    assert result["state"] == "running"
    assert result["turn"] == 1
    assert backend.entered.wait(timeout=5)

    mid_flight = manager_loop_service.status(tmp_path)
    assert mid_flight["running"] is True
    assert mid_flight["last_turn"] is None

    backend.gate.set()
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True

    settled = manager_loop_service.status(tmp_path)
    assert settled["running"] is False
    assert settled["last_turn"] == {"turn": 1, "ok": True, "errors": [], "reply": "ack: hello"}

    events = manager_loop_service.events(tmp_path, result["session_id"])
    assert events["ok"] is True
    assert [event["type"] for event in events["events"]] == [
        "session_start", "user_message", "assistant_text", "turn_end",
    ]


def test_send_while_a_turn_is_running_is_refused_and_nothing_is_queued(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    manager_loop_service.start(tmp_path, "fake", "model-a")
    backend = backends[0]
    backend.gate = threading.Event()

    manager_loop_service.send(tmp_path, "one")
    assert backend.entered.wait(timeout=5)

    refused = manager_loop_service.send(tmp_path, "two")
    assert refused == {"ok": False, "error": "manager_turn_in_progress"}

    backend.gate.set()
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    assert backend.messages == ["one"]


def test_events_after_seq_returns_only_newer_events_and_respects_limit(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_fakes(monkeypatch)
    manager_loop_service.start(tmp_path, "fake", "model-a")
    manager_loop_service.send(tmp_path, "one")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    manager_loop_service.send(tmp_path, "two")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True

    session_id = manager_loop_service.status(tmp_path)["session"]["session_id"]
    everything = manager_loop_service.events(tmp_path, session_id)["events"]
    all_seqs = [event["seq"] for event in everything]
    assert all_seqs == sorted(all_seqs)
    assert len(all_seqs) >= 4

    midpoint = all_seqs[2]
    newer = manager_loop_service.events(tmp_path, session_id, after_seq=midpoint)
    assert [event["seq"] for event in newer["events"]] == [s for s in all_seqs if s > midpoint]

    limited = manager_loop_service.events(tmp_path, session_id, after_seq=0, limit=2)
    assert [event["seq"] for event in limited["events"]] == all_seqs[:2]


def test_rotate_runs_in_the_background_and_the_next_start_sees_the_handoff(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    manager_loop_service.start(tmp_path, "fake", "model-a")
    manager_loop_service.send(tmp_path, "hello")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True

    result = manager_loop_service.rotate(tmp_path, "handing off")
    assert result["ok"] is True
    assert result["state"] == "running"
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True

    assert manager_loop_service.status(tmp_path)["session"] is None

    next_start = manager_loop_service.start(tmp_path, "fake", "model-b")
    assert next_start["ok"] is True
    assert len(backends) == 2
    assert "## handoff" in backends[1].briefs[0]
    assert "ack:" in backends[1].briefs[0]


def test_a_manager_loop_error_surfaces_as_ok_false_and_never_raises(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_fakes(monkeypatch)
    manager_loop_service.start(tmp_path, "fake", "model-a")

    result = manager_loop_service.events(tmp_path, "not a valid session id")
    assert result == {"ok": False, "error": "session_id_invalid"}


def test_one_orchestrator_per_repository_root(monkeypatch: Any, tmp_path: Path) -> None:
    _install_fakes(monkeypatch)
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()

    first = manager_loop_service.start(repo_a, "fake", "model-a")
    again = manager_loop_service.start(repo_a, "fake", "model-a")
    other = manager_loop_service.start(repo_b, "fake", "model-a")

    assert first["ok"] is True
    assert again == {"ok": False, "error": "manager_session_already_active"}
    assert other["ok"] is True


def test_a_non_manager_loop_error_during_a_turn_is_recorded_and_never_raises(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_fakes(monkeypatch)
    manager_loop_service.start(tmp_path, "fake", "model-a")

    def boom(message: str) -> Any:
        raise OSError("disk exploded")

    entry = manager_loop_service._entry_for(tmp_path)
    monkeypatch.setattr(entry.orchestrator, "send", boom)

    result = manager_loop_service.send(tmp_path, "hello")
    assert result["ok"] is True
    assert result["state"] == "running"

    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    settled = manager_loop_service.status(tmp_path)
    assert settled["running"] is False
    assert settled["last_turn"] == {
        "turn": 1, "ok": False, "errors": ["OSError: disk exploded"], "reply": "",
    }


def test_a_non_manager_loop_error_building_the_entry_surfaces_as_ok_false(
    monkeypatch: Any, tmp_path: Path
) -> None:
    class _BrokenOrchestratorFactory:
        @staticmethod
        def for_repository(repo: Any, backend_factory: Any, **_options: Any) -> Any:
            raise RuntimeError("repository inspection exploded")

    monkeypatch.setattr(manager_loop_service, "ManagerOrchestrator", _BrokenOrchestratorFactory)
    monkeypatch.setattr(
        manager_loop_service, "manager_backend_factory", lambda repo: (lambda b, m: None)
    )

    result = manager_loop_service.start(tmp_path, "fake", "model-a")

    assert result == {"ok": False, "error": "manager_loop_unavailable:RuntimeError"}


def _verified_manager(monkeypatch: Any, root: Path) -> list[str]:
    """A verified manager route and a service that records calls instead of
    spawning a CLI, so no gate test ever depends on -- or launches with --
    the ambient identity of the process running the suite."""

    monkeypatch.setattr(
        server.core,
        "manager_bootstrap",
        lambda: {"role": "manager", "repo": str(root), "manager_route": {"thread_id": "t1"}},
    )
    calls: list[str] = []
    for name in ("start", "send", "rotate"):
        monkeypatch.setattr(
            manager_loop_service, name, lambda *_a, _n=name: calls.append(_n) or {"ok": True}
        )
    return calls


def test_start_send_rotate_refuse_without_the_launch_gate(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _verified_manager(monkeypatch, tmp_path)
    monkeypatch.delenv("AIWORKHUB_ALLOW_LAUNCH", raising=False)
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")

    refusal = {"ok": False, "error": "launch_gate_closed"}
    assert server.aiworkhub_manager_loop_start("claude_cli", "opus") == refusal
    assert server.aiworkhub_manager_loop_send("hello") == refusal
    assert server.aiworkhub_manager_loop_rotate("handoff") == refusal
    assert calls == []


def test_start_send_rotate_refuse_without_the_write_gate(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _verified_manager(monkeypatch, tmp_path)
    monkeypatch.setenv("AIWORKHUB_ALLOW_LAUNCH", "1")
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)

    refusal = {"ok": False, "error": "write_gate_closed"}
    assert server.aiworkhub_manager_loop_start("claude_cli", "opus") == refusal
    assert server.aiworkhub_manager_loop_send("hello") == refusal
    assert server.aiworkhub_manager_loop_rotate("handoff") == refusal
    assert calls == []


def test_start_send_rotate_proceed_past_the_gates_once_both_are_open(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls = _verified_manager(monkeypatch, tmp_path)
    monkeypatch.setenv("AIWORKHUB_ALLOW_LAUNCH", "1")
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")

    assert server.aiworkhub_manager_loop_start("claude_cli", "opus") == {"ok": True}
    assert server.aiworkhub_manager_loop_send("hello") == {"ok": True}
    assert server.aiworkhub_manager_loop_rotate("handoff") == {"ok": True}
    assert calls == ["start", "send", "rotate"]


def test_status_events_close_do_not_require_the_launch_or_write_gate(monkeypatch: Any) -> None:
    monkeypatch.delenv("AIWORKHUB_ALLOW_LAUNCH", raising=False)
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)

    for result in (
        server.aiworkhub_manager_loop_status(),
        server.aiworkhub_manager_loop_events("mls-aaaa1111bbbb2222"),
        server.aiworkhub_manager_loop_close(),
    ):
        assert result.get("error") not in ("launch_gate_closed", "write_gate_closed")
