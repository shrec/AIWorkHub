"""RM-2026-00001 phase 1: the provider-neutral manager loop core.

Every backend here is the deterministic FakeBackend below, and every brief
source and writer is a fake, so no CLI, model, MCP route or task store is
touched -- except by the one test that pins the production defaults to the
existing functions they must call.
"""

from __future__ import annotations

import json
import os
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
        self.id_gate: threading.Event | None = None
        self.id_entered = threading.Event()
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

    @property
    def started(self) -> list[str]:
        """Every brief a provider session was started from; empty while none was touched."""
        return [brief for backend in self.backends for brief in backend.briefs]

    def next_id(self) -> str:
        self.issued += 1
        if self.id_gate is not None:
            self.id_entered.set()
            assert self.id_gate.wait(timeout=10)
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

def test_event_types_gained_command_and_file_change() -> None:
    assert {"command", "file_change"} <= ml.EVENT_TYPES


def test_new_records_carry_v3_and_an_unversioned_record_still_reads(make, tmp_path: Path) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")
    harness.orch.send("hello")

    events = harness.store.events(session.session_id)
    assert events and all(event["v"] == 3 for event in events)

    log = tmp_path / "manager_loop" / "events" / f"{session.session_id}.jsonl"
    legacy = json.dumps({
        "at": "2026-01-01T00:00:00+00:00", "turn": 0, "seq": 0,
        "type": "assistant_text", "payload": {"text": "legacy"},
    })
    log.write_text(legacy + "\n" + log.read_text(encoding="utf-8"), encoding="utf-8")

    reread = harness.store.events(session.session_id)
    assert reread[0]["payload"] == {"text": "legacy"}
    assert "v" not in reread[0]


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


def test_bounded_fields_are_cut_per_field_not_per_payload(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID)
    payload = {"output_tail": "a" * 20_000, "output": "short", "diff": "d" * 100, "exit_code": 1}

    record = store.append_event("session-0001", {"type": "tool_result", "payload": payload})

    kept = record["payload"]
    assert kept["output_tail"].startswith("a" * 100)
    assert len(kept["output_tail"].encode("utf-8")) <= ml.FIELD_BOUNDS["output_tail"]
    assert (kept["output"], kept["diff"], kept["exit_code"]) == ("short", "d" * 100, 1)
    assert kept["truncated"] is True and kept["original_bytes"] == 20_000
    assert "preview" not in kept
    assert store.events("session-0001")[-1] == record


def test_non_string_output_is_serialized_before_it_is_bounded(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID)
    lines = ["line"] * 10_000
    for value in ("text", lines, {"key": "value"}, None):
        store.append_event("session-0001", {"type": "tool_result", "payload": {"output": value}})

    stored = [event["payload"] for event in store.events("session-0001")]

    assert stored[0] == {"output": "text"}
    assert isinstance(stored[1]["output"], str) and stored[1]["output"].startswith('["line","line"')
    assert len(stored[1]["output"].encode("utf-8")) <= ml.FIELD_BOUNDS["output"]
    assert stored[1]["truncated"] is True
    assert stored[1]["original_bytes"] == len(json.dumps(lines, separators=(",", ":")))
    assert stored[2] == {"output": {"key": "value"}}
    assert stored[3] == {"output": None}


def test_the_log_is_appended_between_compactions(tmp_path: Path, monkeypatch) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID, max_events=4)
    published: list[Path] = []
    real_publish = ml._publish
    monkeypatch.setattr(ml, "_publish", lambda path, text: (published.append(path), real_publish(path, text)))
    log = tmp_path / "events" / "session-0001.jsonl"

    for index in range(3):
        store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": str(index)}})
    assert published == [] and len(log.read_text(encoding="utf-8").splitlines()) == 3

    store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": "3"}})
    assert published == [log] and len(log.read_text(encoding="utf-8").splitlines()) == 4

    for index in range(4, 7):
        store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": str(index)}})
    assert published == [log] and len(log.read_text(encoding="utf-8").splitlines()) == 7
    assert [event["seq"] for event in store.events("session-0001")] == [4, 5, 6, 7]

    store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": "7"}})
    assert published == [log, log]
    assert [json.loads(line)["seq"] for line in log.read_text(encoding="utf-8").splitlines()] == [5, 6, 7, 8]


def test_the_log_is_compacted_by_bytes_as_well_as_by_count(tmp_path: Path) -> None:
    # NF-2026-01233: a few large lines outgrow the byte budget long before max_events.
    store = ml.SessionStore(tmp_path, REPO_ID, max_events=500, max_bytes=8 * 1024)
    log = tmp_path / "events" / "session-0001.jsonl"

    for index in range(40):
        store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": f"{index}:" + "x" * 900}})
        assert log.stat().st_size <= store.max_bytes

    seqs = [event["seq"] for event in store.events("session-0001")]
    assert seqs[-1] == 40 and seqs == list(range(seqs[0], 41)) and len(seqs) >= 4


def test_events_after_seq_reads_only_the_tail_of_the_log(tmp_path: Path, monkeypatch) -> None:
    # NF-2026-01233: a poll for the newest events never parses the whole log.
    store = ml.SessionStore(tmp_path, REPO_ID)
    for index in range(300):
        store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": str(index)}})
    monkeypatch.setattr(ml, "_TAIL_READ_BYTES", 1024)
    parsed: list[int] = []
    real_parsed = ml._parsed

    def counting(lines: Any) -> list[dict[str, Any]]:
        lines = list(lines)
        parsed.append(len(lines))
        return real_parsed(lines)

    monkeypatch.setattr(ml, "_parsed", counting)

    newer = store.events("session-0001", after_seq=295)

    assert [event["seq"] for event in newer] == [296, 297, 298, 299, 300]
    assert sum(parsed) < 30
    assert [event["seq"] for event in store.events("session-0001")] == list(range(1, 301))
    assert store.events("session-0001", after_seq=300) == []


def test_a_torn_tail_line_is_skipped_and_seq_keeps_increasing(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID)
    for index in range(2):
        store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": str(index)}})
    log = tmp_path / "events" / "session-0001.jsonl"
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"payload": {"text": "half')

    assert [event["seq"] for event in store.events("session-0001")] == [1, 2]

    record = store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": "next"}})

    assert record["seq"] == 3
    assert [event["seq"] for event in store.events("session-0001")] == [1, 2, 3]
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[-2] == '{"payload": {"text": "half'
    assert json.loads(lines[-1]) == record


def test_a_long_text_keeps_its_text_field(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID)
    short = "s" * 20_000
    long = "h" * 70_000

    whole = store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": short}})
    cut = store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": long}})

    assert whole["payload"] == {"text": short}
    text = cut["payload"]["text"]
    assert text.startswith(long[:1024])
    assert len(text.encode("utf-8")) <= ml.FIELD_BOUNDS["text"] + len(ml._CLIP_MARK)
    assert cut["payload"]["truncated"] is True and cut["payload"]["original_bytes"] == 70_000
    assert [event["payload"] for event in store.events("session-0001")] == [whole["payload"], cut["payload"]]


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


LEGACY_ID = "legacy-pinned-0001"
LEGACY_PROVIDER_REF = "claude-conversation-7f3a91"


def _legacy_pinned_record(**changes: Any) -> dict[str, Any]:
    """A session record exactly as the v1 loop wrote it: a pinned route, no successor link."""
    return {
        "schema_id": "aiworkhub.manager_loop.v1",
        "session_id": LEGACY_ID,
        "repo_id": REPO_ID,
        "backend_id": "claude_cli",
        "model": "opus",
        "status": "active",
        "created_at": _EPOCH.isoformat(),
        "closed_at": None,
        "turn_count": 1,
        "context_estimate_bytes": 900,
        "handoff_ref": None,
        **changes,
    }


def _write_legacy_pinned_session(root: Path) -> None:
    """Leave a v1 pinned session active on disk, its provider conversation id in its log."""
    state = root / "manager_loop"
    (state / "sessions").mkdir(parents=True)
    (state / "events").mkdir()
    record = json.dumps(_legacy_pinned_record(), indent=2, sort_keys=True)
    (state / "sessions" / f"{LEGACY_ID}.json").write_text(record + "\n", encoding="utf-8")
    payloads = [
        ("session_start", {"provider_ref": LEGACY_PROVIDER_REF, "previous_session_id": None}),
        ("user_message", {"text": "triage CARD_LEGACY"}),
        ("assistant_text", {"text": "triaged CARD_LEGACY"}),
        ("turn_end", {}),
    ]
    lines = [
        json.dumps({
            "at": _EPOCH.isoformat(),
            "turn": 0 if kind == "session_start" else 1,
            "seq": seq,
            "type": kind,
            "payload": payload,
        })
        for seq, (kind, payload) in enumerate(payloads, start=1)
    ]
    (state / "events" / f"{LEGACY_ID}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _passive_record(session_id: str, created_at: str, repo_id: str = REPO_ID) -> ml.ManagerSession:
    return ml.ManagerSession(
        session_id=session_id,
        repo_id=repo_id,
        backend_id="",
        model="",
        status="active",
        created_at=created_at,
    )


def test_session_records_read_v1_and_write_v2() -> None:
    legacy = ml.ManagerSession.from_json(_legacy_pinned_record())

    assert (legacy.backend_id, legacy.model, legacy.previous_session_id) == (
        "claude_cli", "opus", None,
    )
    assert not legacy.passive and legacy.status == "active"
    written = legacy.to_json()
    assert written["schema_id"] == ml.SCHEMA_ID != "aiworkhub.manager_loop.v1"
    assert ml.ManagerSession.from_json(written) == legacy
    with pytest.raises(ml.ManagerLoopError, match="session_record_invalid:schema_id"):
        ml.ManagerSession.from_json(_legacy_pinned_record(schema_id="aiworkhub.manager_loop.v0"))


def test_ensure_persists_one_passive_conversation_without_a_provider(make, tmp_path: Path) -> None:
    harness = make()

    first = harness.orch.ensure()
    second = harness.orch.ensure()

    assert first == second == harness.orch.session
    assert (first.status, first.turn_count, first.repo_id) == ("active", 0, REPO_ID)
    assert first.passive and (first.backend_id, first.model) == ("", "")
    assert (first.previous_session_id, first.handoff_ref) == (None, None)
    assert harness.store.sessions() == [first]
    assert (tmp_path / "manager_loop" / "sessions" / f"{first.session_id}.json").is_file()
    (opened,) = harness.store.events(first.session_id)
    assert (opened["seq"], opened["type"]) == (1, "session_start")
    assert opened["payload"] == {
        "passive": True, "provider_ref": None, "previous_session_id": None, "handoff_ref": None,
    }
    assert harness.issued == 1
    assert harness.started == [] and harness.backends == []
    assert harness.graph_events == [] and harness.session_writes == []


def test_a_reconstructed_orchestrator_attaches_to_the_same_conversation(make) -> None:
    ticker = Ticker()
    first = make(prefix="a", ticker=ticker)
    created = first.orch.ensure()
    first.orch.close()

    second = make(prefix="b", ticker=ticker)
    attached = second.orch.ensure()
    assert attached == created and second.orch.session == created
    assert second.issued == 0 and second.backends == []
    assert [item.session_id for item in second.store.sessions()] == [created.session_id]
    assert [event["type"] for event in second.store.events(created.session_id)] == ["session_start"]


def test_mechanical_retire_marks_the_turn_its_dead_owner_never_closed(make) -> None:
    first = make(prefix="a")
    session = first.orch.start("fake", "model-a")
    first.orch._record(session, 1, "user_message", {"text": "hello"})
    first.orch.close()  # owner exits without rotating; the record stays active

    second = make(prefix="b")
    attached = second.orch.ensure()

    assert attached.passive and attached.session_id != session.session_id
    events = second.store.events(session.session_id)
    kinds = [(event["turn"], event["type"]) for event in events]
    assert (1, "error") in kinds
    marker = next(e for e in events if e["type"] == "error" and e["turn"] == 1)
    assert marker["payload"]["source"] == "manager_turn_interrupted"
    assert "turn_end" not in [event["type"] for event in events]
    assert kinds[-1] == (0, "session_close")

@pytest.mark.parametrize("shared", [False, True], ids=["two_orchestrators", "one_orchestrator"])
def test_concurrent_ensure_calls_share_one_conversation(make, shared: bool) -> None:
    ticker = Ticker()
    creator = make(prefix="a", ticker=ticker)
    waiter = creator if shared else make(prefix="b", ticker=ticker)
    creator.id_gate = threading.Event()
    seen: dict[str, ml.ManagerSession] = {}
    creating = threading.Thread(
        target=lambda: seen.update(creator=creator.orch.ensure()), daemon=True
    )
    waiting = threading.Thread(
        target=lambda: seen.update(waiter=waiter.orch.ensure()), daemon=True
    )
    creating.start()
    try:
        assert creator.id_entered.wait(timeout=10)
        waiting.start()
        waiting.join(timeout=0.5)
        assert waiting.is_alive()
    finally:
        creator.id_gate.set()
        for thread in (creating, waiting):
            if thread.ident is not None:
                thread.join(timeout=10)

    assert not creating.is_alive() and not waiting.is_alive()
    assert seen["creator"] == seen["waiter"]
    assert [item.session_id for item in creator.store.sessions()] == ["a-session-0001"]
    assert creator.issued + (0 if waiter is creator else waiter.issued) == 1
    assert creator.backends == [] and waiter.backends == []


def test_a_legacy_pinned_session_migrates_through_its_handoff(make, tmp_path: Path) -> None:
    _write_legacy_pinned_session(tmp_path)
    harness = make()
    (legacy,) = harness.store.sessions()
    assert (legacy.session_id, legacy.status, legacy.backend_id) == (
        LEGACY_ID, "active", "claude_cli",
    )
    assert not legacy.passive

    successor = harness.orch.ensure()

    retired, current = harness.store.sessions()
    assert (retired.session_id, retired.status) == (LEGACY_ID, "closed") and retired.closed_at
    assert (retired.backend_id, retired.model) == ("claude_cli", "opus")
    assert retired.handoff_ref == "session_document:1"
    handoff = harness.store.read_handoff(LEGACY_ID)
    assert handoff.startswith("Mechanical handoff") and "- handled: triage CARD_LEGACY" in handoff
    assert harness.session_writes[-1]["action"] == "handoff"
    assert harness.session_writes[-1]["content"] == handoff
    assert current == successor == harness.orch.session and successor.passive
    assert successor.previous_session_id == LEGACY_ID
    (opened,) = harness.store.events(successor.session_id)
    assert opened["payload"]["previous_session_id"] == LEGACY_ID
    assert opened["payload"]["handoff_ref"] == retired.handoff_ref
    assert opened["payload"]["provider_ref"] is None
    assert LEGACY_PROVIDER_REF not in json.dumps([successor.to_json(), opened]) + handoff
    assert harness.backends == [] and harness.graph_events == []


@pytest.mark.parametrize("fault", ["foreign_repository", "renamed_record"])
def test_ensure_refuses_a_record_bound_to_another_identity(make, tmp_path: Path, fault: str) -> None:
    harness = make()
    directory = tmp_path / "manager_loop" / "sessions"
    repo_id = "repo_someone_else" if fault == "foreign_repository" else REPO_ID
    harness.store.save(_passive_record("stray-session-01", _EPOCH.isoformat(), repo_id))
    if fault == "renamed_record":
        (directory / "stray-session-01.json").rename(directory / "other-session-02.json")

    with pytest.raises(ml.ManagerLoopError, match="session_record_mismatch"):
        harness.orch.ensure()

    assert harness.orch.session is None and harness.issued == 0 and harness.backends == []
    stray = list(directory.iterdir())
    assert len(stray) == 1
    stray[0].unlink()
    assert harness.orch.ensure().session_id == "a-session-0001"


def test_ensure_keeps_the_oldest_passive_conversation_when_duplicates_exist(make) -> None:
    ticker = Ticker()
    harness = make(ticker=ticker)
    older = _passive_record("older-passive-01", ticker())
    newer = _passive_record("newer-passive-02", ticker())
    harness.store.save(newer)
    harness.store.save(older)

    kept = harness.orch.ensure()

    assert kept == older
    assert [(item.session_id, item.status) for item in harness.store.sessions()] == [
        ("older-passive-01", "active"), ("newer-passive-02", "closed"),
    ]


def test_attach_continues_the_chosen_session_and_a_later_ensure_keeps_it(make) -> None:
    ticker = Ticker()
    harness = make(ticker=ticker)
    older = _passive_record("older-passive-01", ticker())
    newer = _passive_record("newer-passive-02", ticker())
    harness.store.save(older)
    harness.store.save(newer)

    attached = harness.orch.attach("newer-passive-02")

    assert attached.session_id == "newer-passive-02" and attached.status == "active"
    assert harness.orch.session == attached
    assert harness.store.read_selection() == "newer-passive-02"
    assert [(item.session_id, item.status) for item in harness.store.sessions()] == [
        ("older-passive-01", "closed"), ("newer-passive-02", "active"),
    ]
    assert "session_switch" in harness.store.read_handoff("older-passive-01")
    assert harness.backends == []

    harness.orch.close()
    other = make(prefix="b", ticker=ticker)
    assert other.orch.ensure().session_id == "newer-passive-02"
    assert other.backends == []


def test_attach_reopens_a_closed_session_without_calling_a_provider(make) -> None:
    ticker = Ticker()
    harness = make(ticker=ticker)
    created = ticker()
    closed = ml.ManagerSession(
        session_id="closed-session-01",
        repo_id=REPO_ID,
        backend_id="",
        model="",
        status="closed",
        created_at=created,
        closed_at=ticker(),
    )
    current = _passive_record("current-passive1", ticker())
    harness.store.save(closed)
    harness.store.save(current)

    reopened = harness.orch.attach("closed-session-01")

    assert (reopened.session_id, reopened.status, reopened.closed_at) == (
        "closed-session-01", "active", None,
    )
    assert harness.store.read_selection() == "closed-session-01"
    stored = {item.session_id: item for item in harness.store.sessions()}
    assert stored["current-passive1"].status == "closed"
    assert harness.backends == []
    with pytest.raises(ml.ManagerLoopError, match="session_not_found"):
        harness.orch.attach("missing-session-01")
    with pytest.raises(ml.ManagerLoopError, match="session_id_invalid"):
        harness.orch.attach("nope")


def test_begin_new_closes_the_current_conversation_and_opens_another(make) -> None:
    harness = make()
    first = harness.orch.ensure()

    second = harness.orch.begin_new()

    assert second.passive and second.session_id != first.session_id
    assert harness.orch.session == second
    assert harness.store.read_selection() is None
    stored = {item.session_id: item for item in harness.store.sessions()}
    assert stored[first.session_id].status == "closed"
    assert "new_session" in harness.store.read_handoff(first.session_id)
    assert harness.backends == []


def test_restore_loads_the_last_session_and_does_not_create_one(make) -> None:
    ticker = Ticker()
    empty = make(prefix="empty", ticker=ticker)
    assert empty.orch.restore_latest() is None
    assert empty.store.sessions() == []

    older = _passive_record("older-passive-01", ticker())
    newer = _passive_record("newer-passive-02", ticker())
    empty.store.save(older)
    empty.store.save(newer)
    restored = empty.orch.restore_latest()
    assert restored is not None and restored.session_id == "newer-passive-02"
    assert empty.store.read_selection() == "newer-passive-02"
    assert empty.backends == []


def test_rename_sets_the_display_name_without_a_provider(make) -> None:
    harness = make()
    session = harness.orch.ensure()
    renamed = harness.orch.rename(session.session_id, "  Morning   review  ")
    assert renamed.title == "Morning review"
    assert harness.orch.session is not None and harness.orch.session.title == "Morning review"
    assert harness.backends == []
    legacy = ml.ManagerSession.from_json(session.to_json())
    assert legacy.title == ""


def test_discard_removes_a_saved_session_and_its_log(make) -> None:
    harness = make()
    kept = harness.orch.ensure()
    other = _passive_record("other-session-01", "2026-09-26T00:00:00+00:00")
    harness.store.save(other)
    harness.store.save_handoff(other.session_id, "old handoff")

    harness.orch.discard(other.session_id)

    assert [item.session_id for item in harness.store.sessions()] == [kept.session_id]
    assert not (harness.store.root / "handoffs" / f"{other.session_id}.md").is_file()
    assert harness.orch.session == kept
    with pytest.raises(ml.ManagerLoopError, match="session_not_found"):
        harness.orch.discard(other.session_id)


def test_the_first_message_on_a_route_opens_one_session_and_a_loaded_one_stays(make) -> None:
    harness = make()
    opened = harness.orch.continue_on_route("fake", "model-a")
    assert opened.session_id and not opened.passive
    assert len(harness.store.sessions()) == 1
    harness.orch.close()
    again = make(prefix="b")
    loaded = again.orch.restore_latest()
    assert loaded is not None and loaded.session_id == opened.session_id
    bound = again.orch.continue_on_route("fake", "model-a")
    assert bound.session_id == opened.session_id
    switched = again.orch.continue_on_route("other", "model-b")
    assert switched.session_id == opened.session_id
    assert (switched.backend_id, switched.model) == ("other", "model-b")
    assert len(again.store.sessions()) == 1


_ASKED = "ZEBRA-owner-question-7731"
_ANSWERED = "OKAPI-manager-reply-4412"


def _record_handoffs(monkeypatch) -> list[str]:
    """Every handoff text handed to a BriefBuilder from now on."""
    seen: list[str] = []
    real = ml.BriefBuilder.build

    def build(self: ml.BriefBuilder, handoff: str = "") -> str:
        seen.append(handoff)
        return real(self, handoff)

    monkeypatch.setattr(ml.BriefBuilder, "build", build)
    return seen


def _one_turn(harness: Harness) -> ml.ManagerSession:
    session = harness.orch.continue_on_route("fake", "model-a")
    harness.backends[-1].script.append([
        {"type": "assistant_text", "payload": {"text": _ANSWERED}},
        {"type": "turn_end", "payload": {}},
    ])
    assert harness.orch.send(_ASKED)["reply"] == _ANSWERED
    return session


def _closed_with_handoff(store: ml.SessionStore, text: str) -> None:
    stamp = "2026-09-21T00:00:00+00:00"
    closed = ml.ManagerSession(
        session_id="older-closed-0001", repo_id=REPO_ID, backend_id="fake", model="model-a",
        status="closed", created_at=stamp, closed_at=stamp,
    )
    store.save(closed)
    store.save_handoff(closed.session_id, text)


def test_a_route_switch_gives_the_new_model_this_sessions_own_turns(make) -> None:
    harness = make()
    session = _one_turn(harness)

    switched = harness.orch.continue_on_route("other", "model-b")

    assert switched.session_id == session.session_id
    assert harness.backends[-1].backend_id == "other"
    (brief,) = harness.backends[-1].briefs
    assert _ASKED in brief and _ANSWERED in brief
    assert len(harness.store.sessions()) == 1


def test_rebinding_the_same_route_after_a_restart_restores_the_turns(make) -> None:
    harness = make()
    session = _one_turn(harness)
    harness.orch.close()
    again = make(prefix="b")
    loaded = again.orch.restore_latest()
    assert loaded is not None and loaded.session_id == session.session_id

    bound = again.orch.continue_on_route("fake", "model-a")

    assert bound.session_id == session.session_id
    (brief,) = again.started
    assert _ASKED in brief and _ANSWERED in brief


def test_a_bind_without_turns_keeps_the_previous_closed_handoff_only(make, monkeypatch) -> None:
    harness = make()
    session = harness.orch.continue_on_route("fake", "model-a")
    _closed_with_handoff(harness.store, "PREVIOUS-closed-handoff")
    seen = _record_handoffs(monkeypatch)

    harness.orch.continue_on_route("other", "model-b")

    assert seen == ["PREVIOUS-closed-handoff"]
    assert f"Mechanical handoff for {session.session_id}" not in harness.started[-1]


def test_own_turns_come_before_the_previous_closed_handoff(make, monkeypatch) -> None:
    harness = make()
    _one_turn(harness)
    _closed_with_handoff(harness.store, "PREVIOUS-closed-handoff")
    seen = _record_handoffs(monkeypatch)

    harness.orch.continue_on_route("other", "model-b")

    (handoff,) = seen
    previous_at = handoff.index("PREVIOUS-closed-handoff")
    assert handoff.index(_ASKED) < handoff.index(_ANSWERED) < previous_at


def test_the_combined_bind_handoff_stays_within_the_handoff_bound(make, monkeypatch) -> None:
    harness = make()
    _one_turn(harness)
    _closed_with_handoff(harness.store, "é" * (ml.MAX_HANDOFF_BYTES // 2))
    seen = _record_handoffs(monkeypatch)

    harness.orch.continue_on_route("other", "model-b")

    (handoff,) = seen
    assert len(handoff.encode("utf-8")) <= ml.MAX_HANDOFF_BYTES
    assert _ASKED in handoff and _ANSWERED in handoff
    assert "é" in handoff


def test_a_picker_selection_follows_the_route_successor(make) -> None:
    ticker = Ticker()
    harness = make(ticker=ticker)
    passive = harness.orch.ensure()
    harness.orch.attach(passive.session_id)

    successor = harness.orch.ensure_route("fake", "model-a")

    assert successor.session_id != passive.session_id
    assert harness.store.read_selection() == successor.session_id
    harness.orch.close()
    other = make(prefix="b", ticker=ticker)
    assert other.orch.ensure().session_id == successor.session_id


def test_a_stuck_ensure_holder_is_a_named_refusal_not_a_hang(make, monkeypatch) -> None:
    from aiworkhub import platform_io

    harness = make()
    holder = platform_io.open_lock_file(harness.store.ensure_lock_path)
    platform_io.lock_fd(holder, blocking=False)
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.1)
    try:
        with pytest.raises(ml.ManagerLoopError, match="manager_ensure_timeout"):
            harness.orch.ensure()

        assert harness.store.sessions() == [] and harness.issued == 0
    finally:
        platform_io.unlock_fd(holder)
        os.close(holder)

    assert harness.orch.ensure().passive


def test_start_retires_a_passive_conversation_and_pins_a_route(make) -> None:
    ticker = Ticker()
    passive_owner = make(prefix="a", ticker=ticker)
    passive = passive_owner.orch.ensure()
    pinned = make(prefix="b", ticker=ticker)

    started = pinned.orch.start("fake", "model-a")

    retired, current = pinned.store.sessions()
    assert (retired.session_id, retired.status) == (passive.session_id, "closed")
    assert current == started and not started.passive
    assert started.previous_session_id == passive.session_id
    handoff = pinned.store.read_handoff(passive.session_id)
    assert "(no route)" in handoff
    assert pinned.backends[0].briefs[0].startswith(f"## handoff\n{handoff}\n")
    assert passive_owner.backends == []


def test_start_and_ensure_agree_inside_one_orchestrator(make) -> None:
    harness = make()
    passive = harness.orch.ensure()

    started = harness.orch.start("fake", "model-a")

    assert harness.orch.session == started and not started.passive
    assert [(item.session_id, item.status) for item in harness.store.sessions()] == [
        (passive.session_id, "closed"), (started.session_id, "active"),
    ]
    assert harness.orch.ensure() == started
    assert len(harness.backends) == 1 and len(harness.backends[0].briefs) == 1
    with pytest.raises(ml.ManagerLoopError, match="manager_session_already_active"):
        harness.orch.start("fake", "model-b")


def test_ensure_route_binds_the_selected_route_from_any_state(make) -> None:
    harness = make()
    passive = harness.orch.ensure()

    bound = harness.orch.ensure_route("fake", "model-a")

    assert not bound.passive and (bound.backend_id, bound.model) == ("fake", "model-a")
    assert bound.previous_session_id == passive.session_id
    assert len(harness.backends) == 1

    same = harness.orch.ensure_route("fake", "model-a")
    assert same == bound and len(harness.backends) == 1

    switched = harness.orch.ensure_route("fake", "model-b")
    assert (switched.backend_id, switched.model) == ("fake", "model-b")
    assert switched.session_id != bound.session_id
    assert switched.previous_session_id == bound.session_id
    assert len(harness.backends) == 2
    retired = {item.session_id: item for item in harness.store.sessions()}[bound.session_id]
    assert retired.status == "closed"
    assert "stale_active_session" in harness.store.read_handoff(bound.session_id)
    with pytest.raises(ValueError, match="backend_id and model are required"):
        harness.orch.ensure_route("", "")


def test_ensure_leaves_a_live_pinned_session_alone_and_follows_its_rotation(make) -> None:
    ticker = Ticker()
    driver = make(prefix="a", ticker=ticker)
    other = make(prefix="b", ticker=ticker)
    live = driver.orch.start("fake", "model-a")

    with pytest.raises(ml.ManagerLoopError, match="manager_session_already_active"):
        other.orch.ensure()

    assert other.orch.session is None and other.issued == 0 and other.backends == []
    assert driver.store.sessions() == [live]
    assert driver.orch.ensure() == live

    rotation = driver.orch.rotate("operator_request")
    successor = other.orch.ensure()

    assert successor.passive and successor.previous_session_id == live.session_id
    (opened,) = other.store.events(successor.session_id)
    handed_over = rotation["session"].handoff_ref
    assert opened["payload"]["handoff_ref"] == handed_over == "session_document:1"
    assert len(driver.backends) == 1 and other.backends == []


def test_ensure_binds_the_verified_repository_identity(tmp_path: Path) -> None:
    from aiworkhub import repository_state

    state = repository_state.bootstrap_repository(tmp_path)
    built: list[str] = []

    def factory(backend_id: str, model: str) -> FakeBackend:
        built.append(backend_id)
        return FakeBackend(backend_id, model)

    with ml.ManagerOrchestrator.for_repository(tmp_path, factory) as first:
        session = first.ensure()
    with ml.ManagerOrchestrator.for_repository(tmp_path, factory) as reopened:
        assert reopened.ensure() == session

    assert session.repo_id == state.manifest.repo_id and session.passive
    assert built == []


def test_deltas_fill_partial_and_never_reach_the_log(make) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")
    seen: list[Any] = []

    def stream():
        yield {"type": "delta", "payload": {"kind": "text", "text": "Hel"}}
        yield {"type": "delta", "payload": {"kind": "text", "text": "lo"}}
        seen.append(harness.orch.partial)
        yield {"type": "assistant_text", "payload": {"text": "Hello"}}
        seen.append(harness.orch.partial)
        yield {"type": "turn_end", "payload": {}}

    harness.backends[0].script.append(stream())
    result = harness.orch.send("hi")

    assert seen[0]["text"] == "Hello" and seen[0]["turn"] == result["turn"]
    assert seen[0]["session_id"] == session.session_id
    assert seen[1]["text"] == ""
    assert harness.orch.partial is None
    logged = harness.store.events(session.session_id)
    assert "delta" not in {event["type"] for event in logged}
    assert [event["type"] for event in logged][-2:] == ["assistant_text", "turn_end"]


def test_a_partial_never_crosses_into_the_next_turn(make) -> None:
    harness = make()
    harness.orch.start("fake", "model-a")
    seen: list[Any] = []

    def first():
        yield {"type": "delta", "payload": {"kind": "reasoning", "text": "old"}}
        yield {"type": "turn_end", "payload": {}}

    def second():
        seen.append(harness.orch.partial)
        yield {"type": "delta", "payload": {"kind": "text", "text": "new"}}
        seen.append(harness.orch.partial)
        yield {"type": "turn_end", "payload": {}}

    harness.backends[0].script.extend([first(), second()])
    harness.orch.send("one")
    harness.orch.send("two")

    assert seen[0] is None
    assert seen[1]["reasoning"] == "" and seen[1]["text"] == "new"


def test_deltas_do_not_count_toward_the_turn_event_limit_and_a_partial_keeps_its_tail(make, monkeypatch) -> None:
    monkeypatch.setattr(ml, "MAX_TURN_EVENTS", 2)
    monkeypatch.setattr(ml, "PARTIAL_FIELD_BYTES", 4)
    harness = make()
    session = harness.orch.start("fake", "model-a")
    seen: list[Any] = []

    def stream():
        for piece in ("ab", "cd", "ef"):
            yield {"type": "delta", "payload": {"kind": "text", "text": piece}}
        seen.append(harness.orch.partial)
        yield {"type": "assistant_text", "payload": {"text": "abcdef"}}
        yield {"type": "turn_end", "payload": {}}

    harness.backends[0].script.append(stream())
    harness.orch.send("hi")

    assert seen[0]["text"] == "cdef"
    assert [event["type"] for event in harness.store.events(session.session_id)][-2:] == ["assistant_text", "turn_end"]


def test_a_failed_turn_leaves_no_partial_behind(make) -> None:
    harness = make()
    harness.orch.start("fake", "model-a")

    def stream():
        yield {"type": "delta", "payload": {"kind": "text", "text": "half"}}
        raise RuntimeError("backend died")

    harness.backends[0].script.append(stream())
    harness.orch.send("hi")

    assert harness.orch.partial is None
