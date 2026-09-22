"""RM-2026-00067 phase 1 card 2: the agentic-CLI backend behind ManagerBackend.

No real provider is reached. Every turn here is a real child process -- the
fake CLI script below, replaying a scripted provider stream through a pipe --
so streaming, the turn timeout, a non-zero exit and process termination are
exercised for real, while the argv the backend builds is recorded by an
injected spawn instead of being handed to ``claude``/``codex``/``opencode``.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from aiworkhub import manager_loop as ml
from aiworkhub import manager_loop_backends as mlb
from aiworkhub import runtime_adapters, workforce_catalog

REPO_ID = "repo_manager_backend_fixture"
BRIEF_SOURCES: dict[str, Callable[[], Any]] = {
    "open_cards": lambda: "- [pending] CARD_A (release, fake)",
    "state": lambda: {"phase": "fixture"},
    "context": lambda: "no earlier turns",
    "rules": lambda: "Manager role:\n- fixture rule",
}

# A child process, not a stub: it writes JSONL to a pipe, flushes each line,
# can wait on a gate file to prove streaming, and chooses its exit code.
_FAKE_CLI = textwrap.dedent(
    """
    import json
    import os
    import sys
    import time

    with open(sys.argv[1], encoding="utf-8") as handle:
        config = json.loads(handle.read())
    counter = sys.argv[1] + ".turn"
    try:
        with open(counter, encoding="utf-8") as handle:
            index = int(handle.read())
    except (OSError, ValueError):
        index = 0
    with open(counter, "w", encoding="utf-8") as handle:
        handle.write(str(index + 1))
    turns = config["turns"]
    steps = turns[index] if index < len(turns) else turns[-1]
    for step in steps:
        if step == "@gate":
            deadline = time.monotonic() + 20
            while not os.path.exists(config["gate"]) and time.monotonic() < deadline:
                time.sleep(0.01)
        elif step == "@hang":
            time.sleep(30)
        elif isinstance(step, str):
            print(step, flush=True)
        else:
            print(json.dumps(step), flush=True)
    sys.exit(int(config.get("exit", 0)))
    """
)

_ADAPTER_SHAPES: dict[str, Callable[[str, str], list[str]]] = {
    "claude_cli": lambda prompt, model: [
        "claude", "-p", prompt, "--output-format", "stream-json",
        "--no-session-persistence", "--model", model,
    ],
    "codex_cli": lambda prompt, model: ["codex", "exec", "--json", "--model", model, prompt],
    "opencode_cli": lambda prompt, model: [
        "opencode", "run", "--format", "json", "--model", model, prompt,
    ],
}


class FakeCli:
    """A scripted provider CLI: one recorded argv and one real child per turn."""

    def __init__(self, tmp_path: Path, turns: list[list[Any]], *, exit_code: int = 0) -> None:
        self.root = tmp_path
        self.script = tmp_path / "fake_cli.py"
        self.script.write_text(_FAKE_CLI, encoding="utf-8")
        self.gate = tmp_path / "gate.open"
        self.config = tmp_path / "fake_cli.json"
        self.config.write_text(
            json.dumps({"turns": turns, "exit": exit_code, "gate": str(self.gate)}),
            encoding="utf-8",
        )
        self.prompts: list[str] = []
        self.argv_calls: list[list[str]] = []
        self.children: list[Any] = []

    def plan_builder(self, backend_id: str, prompt: str, repo: Any, *, model: str = "") -> Any:
        self.prompts.append(prompt)
        return SimplePlan(_ADAPTER_SHAPES[backend_id](prompt, model), str(repo))

    def spawn(self, argv: Any, cwd: str | None) -> Any:
        self.argv_calls.append(list(argv))
        child = subprocess.Popen(
            [sys.executable, str(self.script), str(self.config)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.children.append(child)
        return child

    def open_gate(self) -> None:
        self.gate.write_text("go", encoding="utf-8")


class SimplePlan:
    """The launchable subset of RuntimeAdapterPlan this backend reads."""

    def __init__(self, argv: list[str], cwd: str) -> None:
        self.argv = argv
        self.cwd = cwd
        self.launchable = True
        self.validation_reason = ""


def backend(fake: FakeCli, *, backend_id: str = "claude_cli", **options: Any) -> Any:
    return mlb.CliManagerBackend(
        backend_id,
        "fixture-model",
        fake.root,
        plan_builder=fake.plan_builder,
        spawn=fake.spawn,
        **options,
    )


def drain(events: Iterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(events)


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def test_the_first_turn_carries_the_brief_and_the_next_resumes_the_captured_id(tmp_path: Path):
    fake = FakeCli(
        tmp_path,
        [
            [{"type": "system", "session_id": "conv-7"}, {"type": "result", "usage": {}}],
            [{"type": "result", "usage": {}}],
        ],
    )
    cli = backend(fake)
    assert cli.start("BRIEF-TEXT") == "cli:claude_cli:fixture-model"
    assert fake.argv_calls == [], "start must not spawn a process"

    drain(cli.send("first message"))
    assert fake.prompts[0].startswith("BRIEF-TEXT")
    assert "first message" in fake.prompts[0]
    assert "--resume" not in fake.argv_calls[0]
    assert mlb._CLAUDE_NO_PERSIST not in fake.argv_calls[0], "a manager turn must persist"
    assert cli.conversation_id == "conv-7"

    drain(cli.send("second message"))
    assert fake.prompts[1] == "second message", "the brief is sent once, not every turn"
    assert fake.argv_calls[1][:3] == ["claude", "--resume", "conv-7"]
    cli.close()


@pytest.mark.parametrize(
    ("backend_id", "argv", "expected"),
    [
        ("claude_cli", ["claude", "-p", "ask"], ["claude", "--resume", "c1", "-p", "ask"]),
        ("codex_cli", ["codex", "exec", "--json"], ["codex", "exec", "resume", "c1", "--json"]),
        (
            "opencode_cli",
            ["opencode", "run", "--format"],
            ["opencode", "run", "--session", "c1", "--format"],
        ),
    ],
)
def test_resume_tokens_land_where_each_cli_grammar_requires(backend_id, argv, expected):
    assert mlb.resume_argv(backend_id, argv, "c1") == expected
    assert mlb.resume_argv(backend_id, argv, "") == argv, "the first turn never resumes"


def test_every_provider_event_maps_to_its_loop_event(tmp_path: Path):
    claude = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "thinking out loud"},
            {"type": "tool_use", "name": "Read", "input": {"path": "a.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]}},
        {"type": "result", "usage": {"input_tokens": 11, "output_tokens": 2}},
    ]
    fake = FakeCli(tmp_path, [claude])
    cli = backend(fake)
    cli.start("brief")
    events = drain(cli.send("go"))
    assert kinds(events) == ["assistant_text", "tool_call", "tool_result", "turn_end"]
    assert events[0]["payload"]["text"] == "thinking out loud"
    assert events[1]["payload"] == {"name": "Read", "input": {"path": "a.py"}}
    assert events[2]["payload"] == {"name": "t1", "output": "ok"}
    assert events[3]["payload"]["usage"] == {"input_tokens": 11, "output_tokens": 2}
    cli.close()


def test_codex_and_opencode_streams_map_to_the_same_loop_events():
    codex = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "codex reply"}},
        {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "command_execution", "output": "a\nb"}},
        {"type": "turn.completed", "usage": {"input_tokens": 3}},
    ]
    translated = [event for raw in codex for event in mlb.translate("codex_cli", raw)]
    assert kinds(translated) == ["assistant_text", "tool_call", "tool_result", "turn_end"]
    assert translated[3]["payload"]["usage"] == {"input_tokens": 3}

    opencode = [
        {"type": "text", "sessionID": "ses_1", "part": {"text": "opencode reply"}},
        {"type": "tool", "part": {"tool": "bash", "state": {"status": "running", "input": {}}}},
        {"type": "tool", "part": {"tool": "bash", "state": {"status": "completed", "output": "hi"}}},
        {"type": "step_finish", "tokens": {"input": 4, "output": 1}},
    ]
    translated = [event for raw in opencode for event in mlb.translate("opencode_cli", raw)]
    assert kinds(translated) == ["assistant_text", "tool_call", "tool_result", "turn_end"]
    assert translated[3]["payload"]["usage"] == {"input": 4, "output": 1}
    assert mlb.conversation_id_of(opencode[0]) == "ses_1"


def test_a_provider_error_line_becomes_one_error_event():
    reported = {"type": "error", "error": {"name": "APIError", "data": {"statusCode": 429}}}
    assert mlb.translate("opencode_cli", reported) == [
        {"type": "error", "payload": {"source": "provider", "error": "APIError"}}
    ]
    flagged = {"type": "result", "is_error": True, "subtype": "error_during_execution"}
    assert mlb.translate("claude_cli", flagged)[0]["payload"]["error"] == "error_during_execution"


def test_an_unknown_line_is_skipped_and_never_crashes_the_turn(tmp_path: Path):
    fake = FakeCli(
        tmp_path,
        [[
            "this is not json at all",
            "[1, 2, 3]",
            {"type": "mystery", "shape": "unheard of"},
            "",
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "still here"}]}},
            {"type": "result"},
        ]],
    )
    cli = backend(fake)
    cli.start("brief")
    events = drain(cli.send("go"))
    assert kinds(events) == ["assistant_text", "turn_end"]
    assert events[1]["payload"] == {}, "no usage is reported, so none is claimed"
    cli.close()


def test_events_arrive_while_the_process_is_still_running(tmp_path: Path):
    fake = FakeCli(
        tmp_path,
        [[
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "early"}]}},
            "@gate",
            {"type": "result"},
        ]],
    )
    cli = backend(fake)
    cli.start("brief")
    stream = cli.send("go")

    first = next(stream)
    assert first["payload"]["text"] == "early"
    assert cli.is_running(), "the first event must arrive before the process exits"

    fake.open_gate()
    assert kinds(list(stream)) == ["turn_end"]
    assert not cli.is_running()
    cli.close()


def test_a_non_zero_exit_is_exactly_one_error_event_and_the_backend_still_closes(tmp_path: Path):
    fake = FakeCli(tmp_path, [[{"type": "result"}]], exit_code=3)
    cli = backend(fake)
    cli.start("brief")
    events = drain(cli.send("go"))

    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["source"] == runtime_adapters.OUTCOME_WORKER_FAILED
    assert errors[0]["payload"]["error"]
    cli.close()
    assert not cli.is_running()


def test_a_turn_timeout_kills_the_process_and_reports_exactly_one_error(tmp_path: Path):
    fake = FakeCli(
        tmp_path,
        [[
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "then stall"}]}},
            "@hang",
        ]],
    )
    cli = backend(fake, timeout_seconds=0.5)
    cli.start("brief")
    events = drain(cli.send("go"))

    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["source"] == "timeout"
    assert "manager_turn_timeout_seconds=0.5" == errors[0]["payload"]["error"]
    assert kinds(events) == ["assistant_text", "error"]
    assert not cli.is_running()
    cli.close()


def test_close_kills_a_live_process_and_is_idempotent(tmp_path: Path):
    fake = FakeCli(
        tmp_path,
        [[
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "alive"}]}},
            "@hang",
        ]],
    )
    cli = backend(fake)
    cli.start("brief")
    stream = cli.send("go")
    assert next(stream)["payload"]["text"] == "alive"
    child = fake.children[-1]
    assert cli.is_running()

    cli.close()
    assert not cli.is_running()
    cli.close()
    cli.close()
    stream.close()
    deadline = time.monotonic() + 10
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child.poll() is not None, "close must end the child, not leak it"


def test_an_unlaunchable_plan_is_one_error_event_rather_than_a_launch(tmp_path: Path):
    cli = mlb.CliManagerBackend("claude_cli", "fixture-model", tmp_path / "does-not-exist")
    cli.start("brief")
    events = drain(cli.send("go"))
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["payload"]["source"] == "launch_plan"
    assert events[0]["payload"]["error"], "the refusal must name its reason"
    cli.close()


def test_the_production_spawn_and_kill_path_runs_for_real():
    """``_spawn_cli`` and ``_end_process`` are what ships, so they are exercised here."""
    argv = [sys.executable, "-c", 'import time; print("{}", flush=True); time.sleep(30)']
    child = mlb._spawn_cli(argv, None)
    try:
        assert child.stdout.readline().strip() == "{}"
        assert child.poll() is None
        mlb._end_process(child)
        assert child.poll() is not None, "the platform kill facade must end the tree"
    finally:
        mlb._end_process(child)


def test_the_mcp_config_is_wired_only_where_this_repository_has_a_verified_flag(tmp_path: Path):
    fake = FakeCli(tmp_path, [[{"type": "result"}]])
    cli = backend(fake, mcp_config_path=tmp_path / "mcp.json")
    argv = cli.argv_for(SimplePlan(["claude", "-p", "ask"], str(tmp_path)))
    assert argv[-2:] == ["--mcp-config", str(tmp_path / "mcp.json")]

    with pytest.raises(ml.ManagerLoopError) as refused:
        mlb.CliManagerBackend("codex_cli", "m", tmp_path, mcp_config_path=tmp_path / "mcp.json")
    assert "manager_backend_mcp_config_unsupported:codex_cli" in str(refused.value)


def test_the_factory_resolves_the_three_manager_clis_through_the_registry(tmp_path: Path):
    asked: list[tuple[str, str]] = []

    def declares(root: Any, backend_id: str, model: str) -> bool:
        asked.append((backend_id, model))
        return True

    build = mlb.manager_backend_factory(tmp_path, declares_route=declares)
    for backend_id in mlb.MANAGER_BACKEND_IDS:
        made = build(backend_id, "fixture-model")
        assert isinstance(made, mlb.CliManagerBackend)
        assert isinstance(made, ml.ManagerBackend), "it must satisfy the loop's protocol"
        assert made.backend_id == backend_id
    assert asked == [(backend_id, "fixture-model") for backend_id in mlb.MANAGER_BACKEND_IDS]
    assert set(mlb.MANAGER_BACKEND_IDS) <= set(runtime_adapters.SUPPORTED_ADAPTERS)


@pytest.mark.parametrize("backend_id", ["vscode_lm", "grok_kilo_cli", "", "claude"])
def test_the_factory_refuses_an_unknown_backend_with_the_named_error(tmp_path: Path, backend_id):
    build = mlb.manager_backend_factory(tmp_path, declares_route=lambda *_: True)
    with pytest.raises(ml.ManagerLoopError) as refused:
        build(backend_id, "fixture-model")
    assert str(refused.value) == f"manager_backend_unsupported:{backend_id}"


def test_the_factory_refuses_a_backend_the_registry_does_not_declare(tmp_path: Path):
    build = mlb.manager_backend_factory(tmp_path, declares_route=lambda *_: False)
    with pytest.raises(ml.ManagerLoopError) as refused:
        build("claude_cli", "disabled-model")
    assert str(refused.value) == "manager_backend_unavailable:claude_cli:disabled-model"


def test_production_defaults_are_the_existing_repository_helpers():
    """The reuse this card is for, pinned: a respelling here would be caught."""
    signature = inspect.signature(mlb.CliManagerBackend.__init__)
    assert signature.parameters["plan_builder"].default is runtime_adapters.build_runtime_command
    assert signature.parameters["spawn"].default is mlb._spawn_cli
    factory_signature = inspect.signature(mlb.manager_backend_factory)
    assert (
        factory_signature.parameters["declares_route"].default
        is workforce_catalog.catalog_declares_route
    )


def test_the_orchestrator_drives_the_cli_backend_end_to_end(tmp_path: Path):
    reply = {"type": "assistant", "message": {"content": [{"type": "text", "text": "turn one"}]}}
    second = {"type": "assistant", "message": {"content": [{"type": "text", "text": "turn two"}]}}
    handoff = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "done: two turns\nopen: -\nnext: -"}]},
    }
    fake = FakeCli(
        tmp_path,
        [
            [{"type": "system", "session_id": "conv-e2e"}, reply, {"type": "result", "usage": {}}],
            [second, {"type": "result", "usage": {}}],
            [handoff, {"type": "result", "usage": {}}],
        ],
    )
    store = ml.SessionStore(tmp_path / "manager_loop", REPO_ID)
    writes: list[dict[str, Any]] = []

    def writer(**fields: Any) -> dict[str, Any]:
        writes.append(dict(fields))
        return {"ok": True, "document_id": "doc-1"}

    orchestrator = ml.ManagerOrchestrator(
        store,
        backend_factory=mlb.manager_backend_factory(
            tmp_path,
            declares_route=lambda *_: True,
            plan_builder=fake.plan_builder,
            spawn=fake.spawn,
            timeout_seconds=30,
        ),
        brief_builder=ml.BriefBuilder(max_bytes=4096, **BRIEF_SOURCES),
        event_writer=writer,
        session_writer=writer,
    )
    session = orchestrator.start("claude_cli", "fixture-model")
    assert session.backend_id == "claude_cli"

    first = orchestrator.send("what is open?")
    assert first["ok"] is True
    assert first["reply"] == "turn one"
    assert "turn_end" in kinds(first["events"])

    follow_up = orchestrator.send("and now?")
    assert follow_up["reply"] == "turn two"
    assert follow_up["turn"] == 2
    assert fake.argv_calls[1][:3] == ["claude", "--resume", "conv-e2e"]
    assert fake.prompts[0].startswith("## handoff"), "the first turn carries the built brief"

    rotation = orchestrator.rotate("card complete")
    assert rotation["mechanical"] is False
    assert rotation["session"].status == "closed"
    assert "done: two turns" in store.read_handoff(session.session_id)
    assert orchestrator.session is None
    assert [item.status for item in store.sessions()] == ["closed"]
    assert len(fake.argv_calls) == 3, "start, two turns and the handoff ran one CLI turn each"
    assert all(child.poll() is not None for child in fake.children), "no turn leaked a child"
    orchestrator.close()
