"""RM-2026-00001 phase 1: the provider-neutral manager loop core.

Every backend here is the deterministic FakeBackend below, and every brief
source and writer is a fake, so no CLI, model, MCP route or task store is
touched -- except by the one test that pins the production defaults to the
existing functions they must call.
"""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from aiworkhub import manager_loop as ml

REPO_ID = "repo_manager_loop_fixture"
_EPOCH = datetime(2026, 9, 22, tzinfo=timezone.utc)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}$")


class Ticker:
    """A strictly increasing clock, shareable between orchestrators."""

    def __init__(self) -> None:
        self.ticks = 0

    def __call__(self) -> str:
        self.ticks += 1
        return (_EPOCH + timedelta(seconds=self.ticks)).isoformat()


class FakeBackend:
    """A deterministic, scripted ManagerBackend.

    Each ``send`` consumes one script step: an exception is raised at the call,
    a list is streamed (an exception inside it is raised mid-stream), and an
    empty script acknowledges the message. ``gate`` holds a turn open.
    """

    def __init__(self, backend_id: str, model: str) -> None:
        self.backend_id = backend_id
        self.model = model
        self.briefs: list[str] = []
        self.messages: list[str] = []
        self.script: deque[Any] = deque()
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.closed = False

    def start(self, brief: str) -> str:
        self.briefs.append(brief)
        return f"{self.backend_id}:{self.model}:{len(self.briefs)}"

    def send(self, message: str) -> Iterator[dict[str, Any]]:
        self.messages.append(message)
        step = self.script.popleft() if self.script else None
        if isinstance(step, Exception):
            raise step
        return self._stream(message, step)

    def _stream(self, message: str, step: list[Any] | None) -> Iterator[dict[str, Any]]:
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=10)
        if step is None:
            step = [
                {"type": "assistant_text", "payload": {"text": f"ack: {message}"}},
                {"type": "turn_end", "payload": {}},
            ]
        for event in step:
            if isinstance(event, Exception):
                raise event
            yield event

    def close(self) -> None:
        self.closed = True


class Harness:
    """One orchestrator over a store in ``root``, with recording fakes."""

    def __init__(
        self,
        root: Path,
        *,
        prefix: str = "a",
        ticker: Ticker | None = None,
        brief_bytes: int = 4096,
        **options: Any,
    ) -> None:
        self.prefix = prefix
        self.issued = 0
        self.backends: list[FakeBackend] = []
        self.graph_events: list[dict[str, Any]] = []
        self.session_writes: list[dict[str, Any]] = []
        self.graph_result: Any = None
        self.session_result: Any = None
        self.store = ml.SessionStore(root / "manager_loop", REPO_ID)
        self.builder = ml.BriefBuilder(
            open_cards=lambda: "- [pending] CARD_A (release, fake)",
            state=lambda: {"phase": "fixture"},
            context=lambda: "no earlier turns",
            rules=lambda: "Manager role:\n- fixture rule",
            max_bytes=brief_bytes,
        )
        self.orch = ml.ManagerOrchestrator(
            self.store,
            backend_factory=self.factory,
            brief_builder=self.builder,
            event_writer=self.graph_write,
            session_writer=self.session_write,
            clock=ticker or Ticker(),
            new_id=self.next_id,
            **options,
        )

    def factory(self, backend_id: str, model: str) -> FakeBackend:
        self.backends.append(FakeBackend(backend_id, model))
        return self.backends[-1]

    def next_id(self) -> str:
        self.issued += 1
        return f"{self.prefix}-session-{self.issued:04d}"

    def graph_write(self, **fields: Any) -> Any:
        self.graph_events.append(fields)
        return self._answer(self.graph_result, {"ok": True, "event_id": len(self.graph_events)})

    def session_write(self, **fields: Any) -> Any:
        self.session_writes.append(fields)
        return self._answer(self.session_result, {"ok": True, "document_id": len(self.session_writes)})

    @staticmethod
    def _answer(override: Any, default: dict[str, Any]) -> Any:
        if isinstance(override, Exception):
            raise override
        return default if override is None else override


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    """Build harnesses over one repository state dir and release their locks."""
    made: list[Harness] = []

    def build(**options: Any) -> Harness:
        made.append(Harness(tmp_path, **options))
        return made[-1]

    yield build
    for harness in made:
        harness.orch.close()


def test_start_and_send_persist_every_event(make, tmp_path: Path) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")

    assert isinstance(harness.backends[0], ml.ManagerBackend)
    assert (session.status, session.turn_count, session.repo_id) == ("active", 0, REPO_ID)
    assert session.context_estimate_bytes == len(harness.backends[0].briefs[0].encode("utf-8"))

    result = harness.orch.send("hello")

    assert (result["ok"], result["turn"], result["reply"]) == (True, 1, "ack: hello")
    events = harness.store.events(session.session_id)
    assert [event["type"] for event in events] == [
        "session_start", "user_message", "assistant_text", "turn_end",
    ]
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert events[1]["payload"] == {"text": "hello"}
    assert events[2]["payload"] == {"text": "ack: hello"}
    (saved,) = harness.store.sessions()
    assert saved == harness.orch.session and saved.turn_count == 1
    assert saved.context_estimate_bytes > session.context_estimate_bytes
    assert (tmp_path / "manager_loop" / "sessions" / f"{session.session_id}.json").is_file()
    (turn_event,) = harness.graph_events
    assert (turn_event["role"], turn_event["event_type"]) == ("manager", ml.TURN_EVENT_TYPE)
    assert _IDEMPOTENCY_RE.fullmatch(turn_event["idempotency_key"])
    assert "hello" in turn_event["content"] and "ack: hello" in turn_event["content"]


def test_one_active_session_per_repository_through_the_lock(make) -> None:
    ticker = Ticker()
    first = make(prefix="a", ticker=ticker)
    second = make(prefix="b", ticker=ticker)
    first.orch.start("fake", "model-a")

    with pytest.raises(ml.ManagerLoopError, match="manager_session_already_active"):
        second.orch.start("fake", "model-a")
    with pytest.raises(ml.ManagerLoopError, match="manager_session_already_active"):
        first.orch.start("fake", "model-a")

    assert [item.status for item in first.store.sessions()] == ["active"]
    assert second.backends == []

    first.orch.rotate("operator_request")
    successor = second.orch.start("fake", "model-b")

    assert [(item.session_id, item.status) for item in second.store.sessions()] == [
        ("a-session-0001", "closed"), (successor.session_id, "active"),
    ]


def test_a_second_operation_is_refused_while_a_turn_runs(make) -> None:
    harness = make()
    harness.orch.start("fake", "model-a")
    backend = harness.backends[0]
    backend.gate = threading.Event()
    outcome: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: outcome.update(result=harness.orch.send("slow")), daemon=True
    )
    worker.start()
    try:
        assert backend.entered.wait(timeout=10)
        refusals = (
            lambda: harness.orch.send("second"),
            lambda: harness.orch.wake({"task_id": "CARD_A", "state": "review"}),
            lambda: harness.orch.rotate("operator_request"),
            lambda: harness.orch.start("fake", "model-b"),
        )
        for refused in refusals:
            with pytest.raises(ml.ManagerLoopBusy, match="manager_turn_in_progress"):
                refused()
    finally:
        backend.gate.set()
        worker.join(timeout=10)

    assert not worker.is_alive() and outcome["result"]["reply"] == "ack: slow"
    assert backend.messages == ["slow"]
    assert harness.orch.send("after")["ok"] and harness.orch.session.turn_count == 2


def test_the_brief_is_bounded_prioritised_and_deterministic() -> None:
    calls: list[str] = []

    def source(name: str) -> Callable[[], str]:
        def read() -> str:
            calls.append(name)
            return f"{name.upper()}:" + "x" * 300

        return read

    builder = ml.BriefBuilder(
        open_cards=source("cards"),
        state=source("state"),
        context=source("context"),
        rules=source("rules"),
        max_bytes=1024,
    )
    handoff = "HANDOFF " + "h" * 200

    brief = builder.build(handoff)

    assert len(brief.encode("utf-8")) == 1024
    assert [line for line in brief.splitlines() if line.startswith("## ")] == [
        "## handoff", "## open cards", "## state", "## context",
    ]
    assert brief.startswith(f"## handoff\n{handoff}\n")
    assert "CARDS:" + "x" * 300 in brief and "STATE:" + "x" * 300 in brief
    assert brief.split("## context\n")[1].endswith("[truncated]\n")
    assert calls == ["cards", "state", "context"]
    assert builder.build(handoff) == brief

    cut = builder.build("H" * 5000)

    assert len(cut.encode("utf-8")) <= 1024 and cut.startswith("## handoff\nHHH")
    assert cut.endswith("[truncated]\n") and "## open cards" not in cut
    with pytest.raises(ValueError, match="max_bytes"):
        ml.BriefBuilder(
            open_cards=str, state=str, context=str, rules=str, max_bytes=ml.MIN_BRIEF_BYTES - 1
        )


def test_a_failing_brief_source_is_stated_not_hidden() -> None:
    def offline() -> object:
        raise RuntimeError("task store offline")

    builder = ml.BriefBuilder(
        open_cards=offline, state=lambda: {"b": 2, "a": 1}, context=lambda: [], rules=lambda: "r"
    )

    brief = builder.build()

    assert brief.startswith(f"## handoff\n{ml.NO_HANDOFF}\n")
    assert "## open cards\nunavailable: RuntimeError: task store offline\n" in brief
    assert '## state\n{"a":1,"b":2}\n' in brief and "## context\n[]\n" in brief


def test_rotation_writes_the_handoff_the_next_brief_starts_from(make) -> None:
    harness = make()
    first = harness.orch.start("fake", "model-a")
    harness.orch.send("plan the release")
    handoff = "done: planned the release\nopen: CARD_A\nnext: review CARD_A"
    harness.backends[0].script.append(
        [{"type": "assistant_text", "payload": {"text": handoff}}, {"type": "turn_end"}]
    )

    rotation = harness.orch.rotate("operator_request")

    closed = rotation["session"]
    assert (closed.status, closed.turn_count) == ("closed", 2) and closed.closed_at
    assert closed.handoff_ref == "session_document:1" and not rotation["mechanical"]
    assert rotation["handoff"] == handoff
    assert harness.store.read_handoff(first.session_id) == handoff
    assert "operator_request" in harness.backends[0].messages[-1]
    assert harness.backends[0].closed and harness.orch.session is None
    (write,) = harness.session_writes
    assert (write["action"], write["topic"], write["content"]) == ("handoff", ml.SESSION_TOPIC, handoff)
    assert _IDEMPOTENCY_RE.fullmatch(write["idempotency_key"])

    harness.orch.start("fake", "model-a")

    assert harness.backends[1].briefs[0].startswith(f"## handoff\n{handoff}\n")
    assert [item.status for item in harness.store.sessions()] == ["closed", "active"]


@pytest.mark.parametrize(
    ("step", "cause"),
    [
        (RuntimeError("provider gone"), "RuntimeError: provider gone"),
        ([{"type": "turn_end"}], "empty_handoff"),
    ],
    ids=["backend_raises", "empty_reply"],
)
def test_a_failed_handoff_turn_writes_a_mechanical_handoff(make, step: Any, cause: str) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")
    harness.orch.send("triage CARD_B")
    harness.backends[0].script.append(step)

    rotation = harness.orch.rotate("context_threshold")

    text = harness.store.read_handoff(session.session_id)
    assert rotation["mechanical"] and rotation["handoff"] == text
    assert rotation["session"].status == "closed" and harness.orch.session is None
    assert text.startswith("Mechanical handoff") and cause in text
    assert "- handled: triage CARD_B" in text
    assert all(f"\n{section}:\n" in text for section in ("done", "open", "next"))
    assert harness.session_writes[-1]["content"] == text
    assert harness.store.events(session.session_id)[-1]["type"] == "session_close"

    harness.orch.start("fake", "model-b")

    assert harness.backends[1].briefs[0].startswith(f"## handoff\n{text}\n")


def test_a_backend_error_is_an_event_and_the_session_stays_usable(make) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")
    harness.backends[0].script.extend([
        RuntimeError("rate limited"),
        [{"type": "assistant_text", "payload": {"text": "partial"}}, ValueError("stream cut")],
        [{"type": "error", "payload": {"error": "model refused"}}, {"type": "bogus"}],
    ])

    failed = [harness.orch.send(f"message {index}") for index in range(3)]

    assert [outcome["ok"] for outcome in failed] == [False, False, False]
    assert failed[0]["errors"] == [{"source": "backend", "error": "RuntimeError: rate limited"}]
    assert failed[1]["reply"] == "partial"
    assert failed[1]["errors"] == [{"source": "backend", "error": "ValueError: stream cut"}]
    assert failed[2]["errors"] == [
        {"error": "model refused"}, {"source": "backend", "error": "unknown_event_type:bogus"},
    ]

    recovered = harness.orch.send("again")

    assert recovered["ok"] and recovered["reply"] == "ack: again"
    assert (harness.orch.session.status, harness.orch.session.turn_count) == ("active", 4)
    assert len(harness.graph_events) == 4
    logged = [event["type"] for event in harness.store.events(session.session_id)]
    assert logged.count("error") == 4


def test_the_backend_and_model_may_differ_across_rotation(make) -> None:
    harness = make()
    harness.orch.start("claude_cli", "opus")
    harness.orch.send("hand over to codex")
    harness.orch.rotate("model_switch")
    harness.orch.start("codex_cli", "gpt-5")

    first, second = harness.store.sessions()

    assert (first.backend_id, first.model, first.status) == ("claude_cli", "opus", "closed")
    assert (second.backend_id, second.model, second.status) == ("codex_cli", "gpt-5", "active")
    assert [(item.backend_id, item.model) for item in harness.backends] == [
        ("claude_cli", "opus"), ("codex_cli", "gpt-5"),
    ]
    assert harness.store.read_handoff(first.session_id) in harness.backends[1].briefs[0]


def test_crossing_the_threshold_rotates_into_a_rehydrated_successor(make) -> None:
    harness = make(brief_bytes=1024, context_window_bytes=2200, rotate_fraction=0.5)
    first = harness.orch.start("fake", "model-a")
    assert harness.orch.rotate_at_bytes == 1100

    below = harness.orch.send("short")

    assert below["rotation"] is None and below["context_estimate_bytes"] < 1100

    crossed = harness.orch.send("x" * 1000)

    rotation = crossed["rotation"]
    assert crossed["context_estimate_bytes"] >= 1100 and rotation is not None
    assert rotation["session"].session_id == first.session_id
    assert rotation["session"].status == "closed"
    successor = rotation["successor"]
    assert successor == harness.orch.session and successor.status == "active"
    assert (successor.backend_id, successor.model) == ("fake", "model-a")
    assert "context_threshold" in harness.backends[0].messages[-1]
    assert harness.store.read_handoff(first.session_id) in harness.backends[1].briefs[0]


def test_a_threshold_inside_the_brief_budget_is_refused(make) -> None:
    with pytest.raises(ValueError, match="brief budget"):
        make(brief_bytes=4096, context_window_bytes=4096, rotate_fraction=0.5)


def test_wake_runs_the_formatted_callback_as_a_turn(make) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")

    result = harness.orch.wake({"task_id": "CARD_A", "state": "review"})

    assert harness.backends[0].messages == ["callback: CARD_A -> review"]
    assert result["ok"] and result["reply"] == "ack: callback: CARD_A -> review"
    assert harness.graph_events[-1]["task_id"] == "CARD_A"
    inbound = harness.store.events(session.session_id)[1]
    assert inbound["type"] == "callback"
    assert inbound["payload"] == {"text": "callback: CARD_A -> review", "task_id": "CARD_A"}

    harness.orch.wake({"task_id": "CARD_B", "transition": "blocked"})

    assert harness.backends[0].messages[-1] == "callback: CARD_B -> blocked"
    with pytest.raises(ValueError, match="task_id and a state"):
        harness.orch.wake({"task_id": "CARD_C"})


def test_writer_failures_are_recorded_and_returned_not_swallowed(make) -> None:
    harness = make()
    harness.graph_result = {"ok": False, "error": "write_gate_closed"}
    harness.session_result = RuntimeError("session store locked")
    session = harness.orch.start("fake", "model-a")

    result = harness.orch.send("hi")

    assert not result["ok"] and result["reply"] == "ack: hi"
    assert result["errors"] == [{"source": "context_graph_event_write", "error": "write_gate_closed"}]

    rotation = harness.orch.rotate("operator_request")

    assert rotation["writer"] == {"ok": False, "error": "RuntimeError: session store locked"}
    assert rotation["session"].handoff_ref == f"manager_loop:{session.session_id}:handoff"
    assert harness.store.read_handoff(session.session_id) == rotation["handoff"] != ""
    sources = [
        event["payload"].get("source")
        for event in harness.store.events(session.session_id) if event["type"] == "error"
    ]
    assert sources == ["context_graph_event_write", "session_write"]
    harness.orch.start("fake", "model-a")


def test_close_releases_the_lock_and_the_next_start_retires_the_record(make) -> None:
    ticker = Ticker()
    first = make(prefix="a", ticker=ticker)
    session = first.orch.start("fake", "model-a")
    first.orch.send("check CARD_C")

    first.orch.close()

    assert first.backends[0].closed and first.orch.session is None
    second = make(prefix="b", ticker=ticker)
    second.orch.start("fake", "model-a")

    retired = {item.session_id: item for item in second.store.sessions()}[session.session_id]
    text = second.store.read_handoff(session.session_id)
    assert retired.status == "closed" and retired.handoff_ref == "session_document:1"
    assert "stale_active_session" in text and "- handled: check CARD_C" in text
    assert text in second.backends[0].briefs[0]
    assert [item.status for item in second.store.sessions()].count("active") == 1


def test_closed_sessions_beyond_the_retention_bound_are_pruned(make, tmp_path: Path) -> None:
    harness = make()
    harness.store.keep_closed = 1
    rotation: dict[str, Any] = {}
    for model in ("m1", "m2", "m3"):
        harness.orch.start("fake", model)
        rotation = harness.orch.rotate("cycle")

    assert rotation["pruned"] == ["a-session-0002"]
    assert [(item.model, item.status) for item in harness.store.sessions()] == [("m3", "closed")]
    assert not (tmp_path / "manager_loop" / "events" / "a-session-0001.jsonl").exists()
    assert not (tmp_path / "manager_loop" / "handoffs" / "a-session-0002.md").exists()


def test_the_event_log_is_bounded_and_payloads_are_capped(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID, max_events=3)
    for index in range(5):
        store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": str(index)}})

    kept = store.events("session-0001")

    assert [event["seq"] for event in kept] == [3, 4, 5]
    assert [event["payload"]["text"] for event in kept] == ["2", "3", "4"]

    big = store.append_event("session-0001", {"type": "tool_result", "payload": {"blob": "y" * 10_000}})

    assert big["seq"] == 6 and big["payload"]["truncated"] is True
    assert big["payload"]["original_bytes"] > ml.MAX_EVENT_PAYLOAD_BYTES
    assert len(json.dumps(big["payload"]).encode("utf-8")) <= ml.MAX_EVENT_PAYLOAD_BYTES
    assert store.events("session-0001")[-1] == big
    with pytest.raises(ml.ManagerLoopError, match="session_id_invalid"):
        store.events("../escape")


def test_a_foreign_or_malformed_session_record_fails_closed(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID)
    foreign = ml.ManagerSession(
        session_id="foreign-session-1",
        repo_id="repo_someone_else",
        backend_id="fake",
        model="model-a",
        status="active",
        created_at=_EPOCH.isoformat(),
    )
    store.save(foreign)

    with pytest.raises(ml.ManagerLoopError, match="session_record_mismatch"):
        store.sessions()
    with pytest.raises(ml.ManagerLoopError, match="session_record_invalid:status"):
        ml.ManagerSession.from_json({**foreign.to_json(), "status": "paused"})
    with pytest.raises(ml.ManagerLoopError, match="session_record_invalid"):
        ml.ManagerSession.from_json({"session_id": "x"})


def test_production_defaults_call_the_existing_functions(tmp_path: Path, monkeypatch) -> None:
    from aiworkhub import agent_tool_instructions, manager_ai_tools, repository_state, task_store

    state = repository_state.bootstrap_repository(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

    def recorder(name: str, answer: dict[str, Any]) -> Callable[..., dict[str, Any]]:
        def call(**fields: Any) -> dict[str, Any]:
            calls.append((name, fields))
            return answer

        return call

    monkeypatch.setattr(manager_ai_tools, "session_current_state", recorder("state", {"ok": True}))
    monkeypatch.setattr(manager_ai_tools, "context_graph_search", recorder("context", {"ok": True}))
    monkeypatch.setattr(manager_ai_tools, "context_graph_event_write", recorder("event", {"ok": True}))
    monkeypatch.setattr(
        manager_ai_tools, "session_write", recorder("session", {"ok": True, "document_id": 7})
    )
    monkeypatch.setattr(
        task_store,
        "list_tasks",
        lambda root, *, status, limit: [{"task_id": f"CARD_{status}", "topic": "t", "runner": "r"}],
    )
    backends: list[FakeBackend] = []

    def factory(backend_id: str, model: str) -> FakeBackend:
        backends.append(FakeBackend(backend_id, model))
        return backends[-1]

    with ml.ManagerOrchestrator.for_repository(tmp_path, factory) as orch:
        assert orch.store.root == state.runtime_path / ml.STATE_DIRNAME
        assert orch.store.repo_id == state.manifest.repo_id
        orch.start("fake", "model-a")
        orch.send("hello")
        rotation = orch.rotate("operator_request")

    brief = backends[0].briefs[0]
    assert all(f"[{status}] CARD_{status}" in brief for status in ml.OPEN_CARD_STATUSES)
    assert "Manager role:" in brief and agent_tool_instructions.POLICY.role[0][:40] in brief
    assert [name for name, _ in calls] == ["state", "context", "event", "session"]
    assert calls[1][1] == {"query": ml.CONTEXT_QUERY, "limit": 8}
    assert calls[3][1]["action"] == "handoff"
    assert rotation["session"].handoff_ref == "session_document:7"
