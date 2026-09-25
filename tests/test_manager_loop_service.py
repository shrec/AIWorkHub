from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import manager_loop as ml  # noqa: E402
from aiworkhub import manager_loop_backends  # noqa: E402
from aiworkhub import manager_loop_service  # noqa: E402
from aiworkhub import model_settings  # noqa: E402
from aiworkhub import server  # noqa: E402
from aiworkhub import workforce_catalog  # noqa: E402


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

    def factory(repo: Any, **_options: Any) -> Callable[[str, str], FakeManagerBackend]:
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
        manager_loop_service, "manager_backend_factory", lambda repo, **_o: (lambda b, m: None)
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


NO_MANAGER_ROUTE = {
    "ok": False,
    "error": "no_manager_route_available",
    "hint": manager_loop_service.NO_MANAGER_ROUTE_HINT,
}


def _resolves(monkeypatch: Any, route: Any) -> list[Any]:
    """Stand in for ``resolve_manager_route``; returns every repository it was asked about."""

    asked: list[Any] = []

    def resolve(repo: Any) -> Any:
        asked.append(repo)
        if isinstance(route, Exception):
            raise route
        return route

    monkeypatch.setattr(manager_loop_service, "resolve_manager_route", resolve)
    return asked


def test_first_send_pins_the_resolved_route_and_starts_one_background_turn(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    asked = _resolves(monkeypatch, ("fake", "model-a"))
    orchestrator = manager_loop_service._entry_for(tmp_path).orchestrator
    passive = orchestrator.ensure()
    assert passive.passive and backends == []

    result = manager_loop_service.send(tmp_path, "hello")

    assert result["ok"] is True
    assert (result["state"], result["turn"]) == ("running", 1)
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    assert asked == [tmp_path]
    (backend,) = backends
    assert (backend.backend_id, backend.model, backend.messages) == ("fake", "model-a", ["hello"])
    settled = manager_loop_service.status(tmp_path)
    pinned = settled["session"]
    assert pinned["session_id"] == result["session_id"] != passive.session_id
    assert (pinned["backend_id"], pinned["model"]) == ("fake", "model-a")
    assert pinned["previous_session_id"] == passive.session_id
    assert {item.session_id: item.status for item in orchestrator.store.sessions()} == {
        passive.session_id: "closed",
        pinned["session_id"]: "active",
    }
    assert settled["last_turn"] == {"turn": 1, "ok": True, "errors": [], "reply": "ack: hello"}
    assert settled["wake"]["running"] is True
    events = manager_loop_service.events(tmp_path, pinned["session_id"])["events"]
    assert [event["type"] for event in events] == [
        "session_start", "user_message", "assistant_text", "turn_end",
    ]
    assert manager_loop_service.start(tmp_path, "fake", "model-b") == {
        "ok": False, "error": "manager_session_already_active",
    }
    assert len(backends) == 1
    assert manager_loop_service.close(tmp_path) == {"ok": True}


def test_later_sends_reuse_the_pinned_route_without_resolving_again(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    asked = _resolves(monkeypatch, ("fake", "model-a"))
    manager_loop_service._entry_for(tmp_path).orchestrator.ensure()

    first = manager_loop_service.send(tmp_path, "one")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    second = manager_loop_service.send(tmp_path, "two")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True

    assert first["ok"] is True and second["ok"] is True
    assert second["session_id"] == first["session_id"]
    assert (first["turn"], second["turn"]) == (1, 2)
    assert asked == [tmp_path]
    (backend,) = backends
    assert backend.messages == ["one", "two"]
    assert manager_loop_service.close(tmp_path) == {"ok": True}


def test_explicit_start_stays_authoritative_over_the_first_send_route(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    asked = _resolves(monkeypatch, ("fake", "auto-model"))
    manager_loop_service._entry_for(tmp_path).orchestrator.ensure()

    started = manager_loop_service.start(tmp_path, "fake", "explicit-model")
    result = manager_loop_service.send(tmp_path, "hello")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True

    assert started["ok"] is True and result["ok"] is True
    assert result["session_id"] == started["session"]["session_id"]
    assert asked == []
    (backend,) = backends
    assert (backend.model, backend.messages) == ("explicit-model", ["hello"])
    assert manager_loop_service.status(tmp_path)["session"]["model"] == "explicit-model"
    assert manager_loop_service.close(tmp_path) == {"ok": True}


def test_first_send_without_a_manager_route_is_one_stable_error_and_stays_passive(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    asked = _resolves(monkeypatch, None)
    entry = manager_loop_service._entry_for(tmp_path)
    passive = entry.orchestrator.ensure()
    sessions = entry.orchestrator.store.sessions()
    events = entry.orchestrator.store.events(passive.session_id)

    refusals = [manager_loop_service.send(tmp_path, "hello") for _ in range(2)]

    assert refusals == [NO_MANAGER_ROUTE, NO_MANAGER_ROUTE]
    assert asked == [tmp_path, tmp_path]
    assert backends == [] and entry.thread is None
    settled = manager_loop_service.status(tmp_path)
    assert settled["session"] == passive.to_json()
    assert (settled["running"], settled["last_turn"]) == (False, None)
    assert settled["wake"]["running"] is False
    assert entry.orchestrator.store.sessions() == sessions
    assert entry.orchestrator.store.events(passive.session_id) == events

    _resolves(monkeypatch, ("fake", "model-a"))
    recovered = manager_loop_service.send(tmp_path, "hello")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    assert recovered["ok"] is True and len(backends) == 1
    pinned = manager_loop_service.status(tmp_path)["session"]
    assert pinned["previous_session_id"] == passive.session_id
    assert manager_loop_service.close(tmp_path) == {"ok": True}


def test_a_route_resolution_failure_is_the_same_stable_refusal_with_its_reason(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    unreadable = model_settings.ModelSettingsError("model_settings_invalid_json:JSONDecodeError")
    _resolves(monkeypatch, unreadable)
    entry = manager_loop_service._entry_for(tmp_path)
    passive = entry.orchestrator.ensure()

    refusal = manager_loop_service.send(tmp_path, "hello")

    assert refusal == {
        **NO_MANAGER_ROUTE,
        "detail": "ModelSettingsError: model_settings_invalid_json:JSONDecodeError",
    }
    assert backends == [] and entry.orchestrator.session == passive
    assert manager_loop_service.status(tmp_path)["running"] is False


def test_a_failed_pin_is_returned_as_ok_false_and_releases_the_turn_lock(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    _resolves(monkeypatch, ("fake", "model-a"))
    entry = manager_loop_service._entry_for(tmp_path)
    entry.orchestrator.ensure()
    unavailable = "manager_backend_unavailable:fake:model-a"

    for failure, error in (
        (ml.ManagerLoopError(unavailable), unavailable),
        (OSError("disk exploded"), "manager_loop_unavailable:OSError"),
    ):

        def refuse(backend_id: str, model: str, _failure: Exception = failure) -> Any:
            raise _failure

        monkeypatch.setattr(entry.orchestrator, "start", refuse)
        assert manager_loop_service.send(tmp_path, "hello") == {"ok": False, "error": error}
        assert manager_loop_service.status(tmp_path)["running"] is False

    assert backends == [] and entry.thread is None


def test_a_blank_first_send_pins_no_route(monkeypatch: Any, tmp_path: Path) -> None:
    backends = _install_fakes(monkeypatch)
    asked = _resolves(monkeypatch, ("fake", "model-a"))
    orchestrator = manager_loop_service._entry_for(tmp_path).orchestrator
    passive = orchestrator.ensure()

    manager_loop_service.send(tmp_path, "   ")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True

    assert asked == [] and backends == []
    assert orchestrator.session == passive and passive.passive


def test_first_send_with_no_conversation_ensures_and_pins_without_start(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    asked = _resolves(monkeypatch, ("fake", "model-a"))
    orchestrator = manager_loop_service._entry_for(tmp_path).orchestrator
    assert orchestrator.session is None

    result = manager_loop_service.send(tmp_path, "hello")

    assert result["ok"] is True and result["turn"] == 1
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    assert asked == [tmp_path]
    (backend,) = backends
    assert (backend.backend_id, backend.model, backend.messages) == ("fake", "model-a", ["hello"])
    pinned = manager_loop_service.status(tmp_path)["session"]
    assert pinned["session_id"] == result["session_id"]
    assert (pinned["backend_id"], pinned["model"]) == ("fake", "model-a")
    assert manager_loop_service.status(tmp_path)["last_turn"]["ok"] is True


def test_first_send_without_a_route_is_a_stable_refusal_and_stays_passive(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    _resolves(monkeypatch, None)
    orchestrator = manager_loop_service._entry_for(tmp_path).orchestrator

    refusal = manager_loop_service.send(tmp_path, "hello")

    assert refusal == NO_MANAGER_ROUTE
    assert backends == []
    assert manager_loop_service.status(tmp_path)["running"] is False
    session = orchestrator.session
    assert session is not None and session.passive


def test_ensure_attaches_one_passive_conversation_without_any_provider(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    asked = _resolves(monkeypatch, ("fake", "model-a"))

    first = manager_loop_service.ensure(tmp_path)
    second = manager_loop_service.ensure(tmp_path)

    assert first["ok"] is True and first["running"] is False
    assert first["session"]["session_id"] == second["session"]["session_id"]
    assert backends == [] and asked == []
    assert manager_loop_service.status(tmp_path)["running"] is False


def test_send_with_a_picker_route_binds_it_instead_of_the_default(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    asked = _resolves(monkeypatch, ("fake", "default-model"))
    monkeypatch.setattr(
        manager_loop_service,
        "authorize_selected_route",
        lambda repo, backend_id, model: (backend_id, model),
    )

    result = manager_loop_service.send(tmp_path, "hello", "fake", "picked-model")

    assert result["ok"] is True
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    assert asked == [], "the policy default must not resolve when a route is picked"
    (backend,) = backends
    assert (backend.backend_id, backend.model) == ("fake", "picked-model")
    pinned = manager_loop_service.status(tmp_path)["session"]
    assert (pinned["backend_id"], pinned["model"]) == ("fake", "picked-model")


def test_send_with_an_unrunnable_route_is_refused_before_any_spawn(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    monkeypatch.setattr(manager_loop_service, "authorize_selected_route", lambda *a: None)

    result = manager_loop_service.send(tmp_path, "hello", "fake", "nope")

    assert result == {"ok": False, "error": "manager_backend_unavailable:fake:nope"}
    assert backends == []
    assert manager_loop_service.status(tmp_path)["session"] is None


def test_send_with_half_a_route_is_refused_as_incomplete(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)

    assert manager_loop_service.send(tmp_path, "hello", "fake", None) == {
        "ok": False, "error": "manager_route_selection_incomplete",
    }
    assert backends == []


def test_racing_first_sends_pin_one_route_and_every_other_call_is_refused(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    orchestrator = manager_loop_service._entry_for(tmp_path).orchestrator
    passive = orchestrator.ensure()
    entered, gate = threading.Event(), threading.Event()
    asked: list[Any] = []

    def resolve(repo: Any) -> tuple[str, str]:
        asked.append(repo)
        entered.set()
        assert gate.wait(timeout=10)
        return ("fake", "model-a")

    monkeypatch.setattr(manager_loop_service, "resolve_manager_route", resolve)
    racers = 6
    starting_line = threading.Barrier(racers)
    outcomes: list[dict[str, Any]] = []
    guard = threading.Lock()

    def race(number: int) -> None:
        starting_line.wait(timeout=10)
        outcome = manager_loop_service.send(tmp_path, f"message-{number}")
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=race, args=(n,), daemon=True) for n in range(racers)]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with guard:
                if len(outcomes) == racers - 1:
                    break
            time.sleep(0.01)
        with guard:
            refused = list(outcomes)
        assert refused == [{"ok": False, "error": "manager_turn_in_progress"}] * (racers - 1)
        assert manager_loop_service.start(tmp_path, "fake", "model-b") == {
            "ok": False, "error": "manager_turn_in_progress",
        }
        assert len(asked) == 1
    finally:
        gate.set()
        for thread in threads:
            thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    (winner,) = [outcome for outcome in outcomes if outcome["ok"]]
    assert len(outcomes) == racers and len(asked) == 1
    assert (winner["state"], winner["turn"]) == ("running", 1)
    (backend,) = backends
    assert len(backend.messages) == 1 and backend.messages[0].startswith("message-")
    assert {item.session_id: item.status for item in orchestrator.store.sessions()} == {
        passive.session_id: "closed",
        winner["session_id"]: "active",
    }
    assert manager_loop_service.status(tmp_path)["session"]["session_id"] == winner["session_id"]
    assert manager_loop_service.close(tmp_path) == {"ok": True}


def _catalog_row(adapter_id: str, model: str, **fields: Any) -> dict[str, Any]:
    return {
        "worker_id": f"{adapter_id}-{model}",
        "adapter_id": adapter_id,
        "model": model,
        "provider": "vendor-x",
        "enabled": True,
        "manager": True,
        "quality_ceiling": 0.5,
        **fields,
    }


def _route(rows: list[dict[str, Any]], **policy: Any) -> tuple[str, str] | None:
    state = {"providers": {}, "adapters": {}, "models": {}, **policy}
    return manager_loop_service.resolve_manager_route(
        "repo", load_catalog=lambda _repo: {"workers": rows}, load_policy=lambda _repo: state
    )


def test_route_resolution_offers_only_enabled_manager_capable_supported_rows() -> None:
    first, second = manager_loop_backends.MANAGER_BACKEND_IDS[:2]
    rows = [
        _catalog_row(first, "worker-only", manager=False, quality_ceiling=1.0),
        _catalog_row(first, "switched-off", enabled=False, quality_ceiling=1.0),
        _catalog_row("editor_only_adapter", "no-cli-backend", quality_ceiling=1.0),
        _catalog_row(second, "eligible", quality_ceiling=0.1),
    ]

    assert _route(rows) == (second, "eligible")
    assert _route(rows[:3]) is None
    assert _route([]) is None


def test_route_resolution_honors_the_repository_model_policy() -> None:
    first, second = manager_loop_backends.MANAGER_BACKEND_IDS[:2]
    rows = [
        _catalog_row(first, "best", quality_ceiling=0.9),
        _catalog_row(second, "next", quality_ceiling=0.5),
    ]

    assert _route(rows) == (first, "best")
    assert _route(rows, providers={"vendor-x": False}) is None
    assert _route(rows, adapters={"vendor-x": {first: False}}) == (second, "next")
    assert _route(rows, models={"vendor-x": {first: {"best": False}}}) == (second, "next")
    assert _route(rows, models={"vendor-x": {second: {"next": False}}}) == (first, "best")


def test_route_resolution_prefers_quality_and_keeps_catalog_order_on_ties() -> None:
    first, second = manager_loop_backends.MANAGER_BACKEND_IDS[:2]
    rows = [
        _catalog_row(first, "modest", quality_ceiling=0.6),
        _catalog_row(second, "tied-early", quality_ceiling=0.9),
        _catalog_row(first, "tied-late", quality_ceiling=0.9),
    ]

    assert _route(rows) == (second, "tied-early")
    assert _route(list(reversed(rows))) == (first, "tied-late")


def test_route_resolution_defaults_are_the_existing_catalog_and_policy_loaders() -> None:
    defaults = manager_loop_service.resolve_manager_route.__kwdefaults__

    assert defaults["load_catalog"] is workforce_catalog.load_catalog
    assert defaults["load_policy"] is model_settings.load


def _offered_rows(root: Path) -> list[dict[str, Any]]:
    return [
        worker
        for worker in workforce_catalog.load_catalog(root)["workers"]
        if worker["manager"]
        and worker["enabled"]
        and worker["adapter_id"] in manager_loop_backends.MANAGER_BACKEND_IDS
    ]


def _policy_owner(worker: dict[str, Any]) -> str:
    return workforce_catalog.policy_route_identity(worker["provider"], worker["adapter_id"])[0]


def _write_model_policy(root: Path, **policy: Any) -> None:
    path = model_settings.settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_id": model_settings.SCHEMA_ID, "revision": 1, **policy}
    path.write_text(json.dumps(document), encoding="utf-8")


def test_default_route_resolution_reads_the_repository_catalog_and_policy(tmp_path: Path) -> None:
    offered = _offered_rows(tmp_path)
    assert offered
    routes = {(worker["adapter_id"], worker["model"]) for worker in offered}
    assert manager_loop_service.resolve_manager_route(tmp_path) in routes

    owners = sorted({_policy_owner(worker) for worker in offered})
    _write_model_policy(tmp_path, providers={owner: False for owner in owners})
    assert manager_loop_service.resolve_manager_route(tmp_path) is None

    _write_model_policy(tmp_path, providers={owner: False for owner in owners[1:]})
    kept = {
        (worker["adapter_id"], worker["model"])
        for worker in offered
        if _policy_owner(worker) == owners[0]
    }
    assert manager_loop_service.resolve_manager_route(tmp_path) in kept


def test_first_send_through_the_default_resolver_pins_only_a_policy_authorized_route(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    entry = manager_loop_service._entry_for(tmp_path)
    passive = entry.orchestrator.ensure()
    owners = {_policy_owner(worker) for worker in _offered_rows(tmp_path)}
    _write_model_policy(tmp_path, providers={owner: False for owner in owners})

    assert manager_loop_service.send(tmp_path, "hello") == NO_MANAGER_ROUTE
    assert backends == [] and entry.orchestrator.session == passive

    model_settings.settings_path(tmp_path).unlink()
    expected = manager_loop_service.resolve_manager_route(tmp_path)
    assert expected is not None
    result = manager_loop_service.send(tmp_path, "hello")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True

    assert result["ok"] is True
    (backend,) = backends
    assert (backend.backend_id, backend.model) == expected
    assert backend.backend_id in manager_loop_backends.MANAGER_BACKEND_IDS
    assert manager_loop_service.close(tmp_path) == {"ok": True}


def test_cli_discovery_does_not_reauthorize_a_policy_disabled_catalog_route(
    monkeypatch: Any,
) -> None:
    backend = manager_loop_backends.MANAGER_BACKEND_IDS[0]
    declared = "catalog-declared"
    absent = "cli-only"
    monkeypatch.setattr(manager_loop_service, "cli_discovers_model", lambda *_args: True)
    rows = [_catalog_row(backend, declared, provider="vendor-x")]
    policy = {"providers": {"vendor-x": False}, "adapters": {}, "models": {}}

    def load_catalog(_repo: Any) -> dict[str, Any]:
        return {"workers": rows}

    def load_policy(_repo: Any) -> dict[str, Any]:
        return policy

    assert (
        manager_loop_service.authorize_selected_route(
            "repo", backend, declared, load_catalog=load_catalog, load_policy=load_policy
        )
        is None
    )
    assert manager_loop_service.authorize_selected_route(
        "repo", backend, absent, load_catalog=load_catalog, load_policy=load_policy
    ) == (backend, absent)

    owner = model_settings.policy_identity_for_adapter(backend)[0]
    blocked = {"providers": {owner: False}, "adapters": {}, "models": {}}
    assert (
        manager_loop_service.authorize_selected_route(
            "repo",
            backend,
            absent,
            load_catalog=load_catalog,
            load_policy=lambda _repo: blocked,
        )
        is None
    )


def test_non_numeric_wake_cap_returns_unavailable_and_leaves_the_turn_lock_unlocked(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_fakes(monkeypatch)
    entry = manager_loop_service._entry_for(tmp_path)

    for bad, name in (("not-a-number", "ValueError"), (None, "TypeError")):
        result = manager_loop_service.start(tmp_path, "fake", "model-a", wake_cap_per_hour=bad)
        assert result == {"ok": False, "error": f"manager_loop_unavailable:{name}"}
        assert entry.turn_lock.locked() is False

    recovered = manager_loop_service.start(tmp_path, "fake", "model-a", wake_cap_per_hour=3)
    assert recovered["ok"] is True
    assert entry.turn_lock.locked() is False
    assert entry.wake_cap_per_hour == 3
    assert manager_loop_service.close(tmp_path) == {"ok": True}
