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
        self.stdin_texts: list[str | None] = []
        self.children: list[Any] = []

    def plan_builder(self, backend_id: str, prompt: str, repo: Any, *, model: str = "") -> Any:
        self.prompts.append(prompt)
        return SimplePlan(_ADAPTER_SHAPES[backend_id](prompt, model), str(repo))

    def spawn(self, argv: Any, cwd: str | None, stdin_text: str | None = None) -> Any:
        self.argv_calls.append(list(argv))
        self.stdin_texts.append(stdin_text)
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

    def __init__(self, argv: list[str], cwd: str, stdin_text: str | None = None) -> None:
        self.argv = argv
        self.cwd = cwd
        self.stdin_text = stdin_text
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


def test_a_claude_cli_turns_system_init_event_records_the_aliass_resolution(tmp_path: Path, monkeypatch):
    fake = FakeCli(
        tmp_path,
        [[
            {"type": "system", "subtype": "init", "session_id": "conv-1", "model": "claude-opus-5"},
            {"type": "result", "usage": {}},
        ]],
    )
    cli = mlb.CliManagerBackend(
        "claude_cli", "opus", fake.root, plan_builder=fake.plan_builder, spawn=fake.spawn,
    )
    recorded: list[tuple[Any, str, str]] = []
    monkeypatch.setattr(
        mlb.cli_model_discovery,
        "record_claude_resolution",
        lambda repo, alias, resolved: recorded.append((repo, alias, resolved)),
    )
    cli.start("brief")
    drain(cli.send("go"))

    assert recorded == [(fake.root, "opus", "claude-opus-5")]
    cli.close()


def test_a_record_claude_resolution_failure_never_fails_the_turn(tmp_path: Path, monkeypatch):
    fake = FakeCli(
        tmp_path,
        [[
            {"type": "system", "subtype": "init", "session_id": "conv-2", "model": "claude-opus-5"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "result", "usage": {}},
        ]],
    )
    cli = mlb.CliManagerBackend(
        "claude_cli", "opus", fake.root, plan_builder=fake.plan_builder, spawn=fake.spawn,
    )

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(mlb.cli_model_discovery, "record_claude_resolution", _boom)
    cli.start("brief")
    events = drain(cli.send("go"))

    assert kinds(events) == ["assistant_text", "turn_end"]
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


def test_model_reasoning_is_captured_not_dropped():
    claude = {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": "considering two options"}]},
    }
    assert mlb.translate("claude_cli", claude) == [
        {"type": "reasoning", "payload": {"text": "considering two options"}}
    ]
    assert mlb.translate("claude_cli", {
        "type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "  "}]},
    }) == [], "blank thinking records nothing"
    opencode = {"type": "reasoning", "part": {"text": "weighing the trade-off"}}
    assert mlb.translate("opencode_cli", opencode) == [
        {"type": "reasoning", "payload": {"text": "weighing the trade-off"}}
    ]


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


def test_cli_manager_backend_feeds_stdin_text_to_spawn(tmp_path: Path):
    """NF-2026-00042: a plan's stdin_text reaches the spawn seam, not the argv."""
    fake = FakeCli(tmp_path, [[{"type": "result"}]])

    def plan_builder(backend_id: str, prompt: str, repo: Any, *, model: str = "") -> Any:
        return SimplePlan(["claude", "-p"], str(repo), stdin_text=prompt)

    cli = mlb.CliManagerBackend(
        "claude_cli",
        "fixture-model",
        fake.root,
        plan_builder=plan_builder,
        spawn=fake.spawn,
    )
    cli.start("brief")
    drain(cli.send("go"))
    cli.close()

    assert fake.stdin_texts == ["brief\n\ngo"]

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
    assert signature.parameters["plan_builder"].default is runtime_adapters.build_manager_command
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


def test_the_factory_accepts_a_model_the_cli_itself_discovers(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        mlb.cli_model_discovery,
        "codex_models",
        lambda: [{"model": "gpt-6-astra", "label": "GPT-6-Astra", "priority": 1}],
    )
    build = mlb.manager_backend_factory(tmp_path, declares_route=lambda *_a: False)

    assert build("codex_cli", "gpt-6-astra").model == "gpt-6-astra"
    assert build("claude_cli", "fable").model == "fable"
    with pytest.raises(mlb.ManagerLoopError, match="manager_backend_unavailable:codex_cli:gpt-0"):
        build("codex_cli", "gpt-0")


def test_the_factory_accepts_an_opencode_model_the_cli_itself_lists(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        mlb, "_opencode_discovered_models", lambda: ["opencode-go/muse-spark-1.3-contributor"]
    )
    build = mlb.manager_backend_factory(tmp_path, declares_route=lambda *_a: False)

    assert build("opencode_cli", "opencode-go/muse-spark-1.3-contributor").model == \
        "opencode-go/muse-spark-1.3-contributor"
    with pytest.raises(mlb.ManagerLoopError, match="manager_backend_unavailable:opencode_cli:x/y"):
        build("opencode_cli", "x/y")


def test_opencode_discovery_failure_refuses_by_name_not_silently(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mlb, "_opencode_discovered_models", lambda: [])
    build = mlb.manager_backend_factory(tmp_path, declares_route=lambda *_a: False)

    with pytest.raises(mlb.ManagerLoopError, match="manager_backend_unavailable:opencode_cli"):
        build("opencode_cli", "opencode-go/muse-spark-1.3-contributor")

def test_codex_resume_argv_uses_only_the_flags_exec_resume_accepts():
    first = ["codex", "exec", "--json", "-s", "workspace-write", "-C", "D:\repo", "--model", "m", "-"]

    assert mlb.resume_argv("codex_cli", first, "") == first
    assert mlb.resume_argv("codex_cli", first, "tid-1") == [
        "codex", "exec", "resume", "tid-1", "--json",
        "-c", 'sandbox_mode="workspace-write"', "--model", "m", "-",
    ]


def test_a_codex_manager_turn_keeps_its_session_for_resume(tmp_path: Path):
    backend = mlb.CliManagerBackend("codex_cli", "m", tmp_path)
    plan = type("Plan", (), {"argv": ["codex", "exec", "--json", "--ephemeral", "-"]})()

    assert "--ephemeral" not in backend.argv_for(plan)


def test_provision_manager_seat_env_writes_a_0600_codex_home(tmp_path: Path):
    env = mlb.provision_manager_seat_env(
        tmp_path, "codex_cli",
        python_executable=sys.executable,
        package_import_root=tmp_path,
    )

    assert set(env) == {"CODEX_HOME"}
    config_path = Path(env["CODEX_HOME"]) / "config.toml"
    assert config_path.is_file()
    if sys.platform != "win32":
        assert (config_path.stat().st_mode & 0o777) == 0o600
    text = config_path.read_text(encoding="utf-8")
    assert "[mcp_servers.AIWorkHub]" in text
    assert "aiworkhub_task_create" in text
    assert "aiworkhub_agent_launch_task" not in text


def test_provision_manager_seat_env_binds_opencode_through_child_env(tmp_path: Path):
    from aiworkhub import runtime_adapters as ra

    env = mlb.provision_manager_seat_env(
        tmp_path, "opencode_cli",
        python_executable=sys.executable,
        package_import_root=tmp_path,
    )

    assert env[ra.OPENCODE_DISABLE_PROJECT_CONFIG_ENV] == "1"
    config = json.loads(env[ra.OPENCODE_WORKER_CONFIG_ENV])
    assert ra.validate_opencode_manager_config(config) is config


def test_provision_manager_seat_env_needs_nothing_for_claude(tmp_path: Path):
    assert mlb.provision_manager_seat_env(
        tmp_path, "claude_cli",
        python_executable=sys.executable,
        package_import_root=tmp_path,
    ) == {}
    with pytest.raises(mlb.ManagerLoopError, match="manager_backend_unsupported"):
        mlb.provision_manager_seat_env(
            tmp_path, "nope_cli",
            python_executable=sys.executable,
            package_import_root=tmp_path,
        )


def test_factory_provisions_seat_env_per_backend_at_build_time(tmp_path: Path):
    seen: list[tuple[str, str]] = []

    def provider(backend_id: str, model: str) -> dict[str, str]:
        seen.append((backend_id, model))
        return {"SEAT_BINDING": f"{backend_id}/{model}"}

    build = mlb.manager_backend_factory(
        tmp_path,
        declares_route=lambda *_a: True,
        seat_env_provider=provider,
    )
    backend = build("codex_cli", "m")

    assert seen == [("codex_cli", "m")]
    assert backend._extra_env == {"SEAT_BINDING": "codex_cli/m"}


def test_turn_without_seat_env_spawns_exactly_as_before(tmp_path: Path):
    fake = FakeCli(
        tmp_path,
        [[
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "result", "usage": {}},
        ]],
    )
    backend = mlb.CliManagerBackend(
        "claude_cli", "m", tmp_path, plan_builder=fake.plan_builder, spawn=fake.spawn
    )
    fake.open_gate()

    assert backend._extra_env is None
    assert [event["type"] for event in backend.send("hi")] == ["assistant_text", "turn_end"]
    assert fake.argv_calls and fake.stdin_texts == [None]


def test_empty_seat_env_inherits_instead_of_spawning_blank(tmp_path: Path):
    # Regression: provision returns {} for claude (no bindings needed); a bare
    # env kills the child in milliseconds with no provider signal, recorded as
    # worker_process_failure_no_provider_refusal_signal.
    backend = mlb.CliManagerBackend("claude_cli", "m", tmp_path, extra_env={})
    assert backend._extra_env is None

    build = mlb.manager_backend_factory(
        tmp_path,
        declares_route=lambda *_a: True,
        seat_env_provider=lambda _b, _m: {},
    )
    assert build("claude_cli", "m")._extra_env is None


def test_seat_env_merges_over_inherited_environment(tmp_path: Path, monkeypatch: Any) -> None:
    # Regression: a wholesale env replace drops SystemRoot/PATH and Bun dies
    # in ~0.1s with no provider signal (worker_process_failure). Seat bindings
    # must merge over inheritance.
    monkeypatch.setenv("SEAT_INHERIT_PROBE", "kept")
    process = mlb._spawn_cli(
        [sys.executable, "-c", "import os,sys;sys.exit(0 if os.environ.get('SEAT_X') == '1' and os.environ.get('SEAT_INHERIT_PROBE') == 'kept' else 3)"],
        str(tmp_path),
        None,
        {"SEAT_X": "1"},
    )
    try:
        assert process.wait(timeout=30) == 0
    finally:
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass


def test_seat_turn_passes_merged_env_to_spawn(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def spawn(argv: Any, cwd: Any, stdin_text: Any = None, env: Any = None) -> Any:
        seen.update(env=env)
        raise OSError("stop-here")

    backend = mlb.CliManagerBackend(
        "codex_cli", "m", tmp_path,
        plan_builder=lambda *a, **k: SimplePlan(["codex"], str(tmp_path)),
        spawn=spawn,
        extra_env={"SEAT_BINDING": "x"},
    )
    assert [event["type"] for event in backend.send("hi")] == ["error"]
    # The backend forwards seat bindings untouched; _spawn_cli merges them
    # over the inherited environment (SystemRoot/PATH) at spawn time.
    assert seen["env"] == {"SEAT_BINDING": "x"}
