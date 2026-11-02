"""NF-2026-01977: thinking-display rejection scope and the reasoning_redacted counter.

A rejection of ``--thinking-display`` by one claude binary (resolved path plus
size and mtime) must not disable the flag for a different binary, and a binary
that changed since it refused the flag is probed again. A thinking block that
arrives without text is counted on the turn's ``turn_end`` instead of vanishing.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import manager_loop_backends as mlb
from aiworkhub import manager_loop_service
from aiworkhub.manager_loop import ManagerOrchestrator

_REJECT = "error: unknown option '--thinking-display'\n"
_RESULT = '{"type": "result", "usage": {}}\n'


@pytest.fixture(autouse=True)
def _fresh_rejections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mlb, "_THINKING_DISPLAY_REJECTIONS", {})


@dataclasses.dataclass
class _Plan:
    argv: list[str]
    cwd: str
    launchable: bool = True
    validation_reason: str = ""
    stdin_text: str | None = None


class _FakeProcess:
    """A finished CLI: canned stdout/stderr lines and an exit code; nothing is spawned."""

    pid = None

    def __init__(self, stdout: list[str], stderr: list[str], code: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = code

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self) -> int:
        return self.returncode


def _binary(tmp_path: Path, name: str, body: str = "v1") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _backend(tmp_path: Path, executable: Path, calls: list[list[str]], rejects: set[str]) -> mlb.CliManagerBackend:
    """A claude backend whose binary refuses the flag while its current bytes are in ``rejects``."""

    def spawn(argv: Any, cwd: Any, stdin_text: Any = None) -> _FakeProcess:
        calls.append(list(argv))
        refused = "--thinking-display" in argv and executable.read_text(encoding="utf-8") in rejects
        return _FakeProcess([], [_REJECT], 1) if refused else _FakeProcess([_RESULT], [], 0)

    backend = mlb.CliManagerBackend(
        "claude_cli", "claude-test-model", tmp_path,
        plan_builder=lambda backend_id, prompt, repo, *, model="": _Plan([str(executable), "-p", prompt], str(repo)),
        spawn=spawn,
    )
    backend.start("brief")
    return backend


def test_a_rejection_by_one_binary_keeps_the_flag_for_another(tmp_path: Path) -> None:
    first = mlb.cli_identity(str(_binary(tmp_path, "claude-a")))
    second = mlb.cli_identity(str(_binary(tmp_path, "claude-b")))
    assert first != second

    mlb.record_thinking_display_rejection(first)

    assert "--thinking-display" not in mlb.manager_stream_tokens("claude_cli", identity=first)
    assert "--thinking-display" in mlb.manager_stream_tokens("claude_cli", identity=second)
    assert "--thinking-display" in mlb.manager_stream_tokens("claude_cli")
    assert mlb.thinking_display_state(first)["state"] == "rejected"
    assert mlb.thinking_display_state(second)["state"] == "enabled"


def test_a_changed_binary_is_probed_again(tmp_path: Path) -> None:
    path = _binary(tmp_path, "claude", "old build")
    before = mlb.cli_identity(str(path))
    mlb.record_thinking_display_rejection(before)

    path.write_text("a newer, longer build", encoding="utf-8")
    after = mlb.cli_identity(str(path))

    assert after[0] == before[0] and after != before
    assert "--thinking-display" in mlb.manager_stream_tokens("claude_cli", identity=after)


def test_an_unreadable_binary_has_a_stable_identity(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-claude")
    assert mlb.cli_identity(missing)[1:] == (-1, -1)
    assert mlb.cli_identity(missing) == mlb.cli_identity(missing)


def test_the_rejection_memo_is_bounded(tmp_path: Path) -> None:
    for index in range(mlb._THINKING_DISPLAY_REJECTIONS_MAX + 5):
        mlb.record_thinking_display_rejection((str(tmp_path / f"c{index}"), index, index))
    assert len(mlb._THINKING_DISPLAY_REJECTIONS) == mlb._THINKING_DISPLAY_REJECTIONS_MAX
    assert not mlb.thinking_display_rejected((str(tmp_path / "c0"), 0, 0))


def test_backend_turns_scope_the_rejection_and_report_it_on_turn_end(tmp_path: Path) -> None:
    path_a, path_b = _binary(tmp_path, "claude-a", "build-a"), _binary(tmp_path, "claude-b", "build-b")
    calls_a: list[list[str]] = []
    calls_b: list[list[str]] = []
    backend_a = _backend(tmp_path, path_a, calls_a, {"build-a"})
    backend_b = _backend(tmp_path, path_b, calls_b, {"build-a"})

    events_a = list(backend_a.send("go"))
    events_b = list(backend_b.send("go"))

    assert [("--thinking-display" in argv) for argv in calls_a] == [True, False]
    assert all(event["type"] != "error" for event in events_a)
    state_a = events_a[-1]["payload"]["thinking_display"]
    assert events_a[-1]["type"] == "turn_end"
    assert state_a["state"] == "rejected"
    assert state_a["executable"] == mlb.cli_identity(str(path_a))[0]
    assert backend_a.thinking_display == state_a

    assert len(calls_b) == 1 and "--thinking-display" in calls_b[0]
    # An enabled flag stays implicit on turn_end; the backend holds the full state.
    assert events_b[-1] == {"type": "turn_end", "payload": {}}
    assert backend_b.thinking_display is not None
    assert backend_b.thinking_display["state"] == "enabled"
    assert backend_b.thinking_display["executable"] == mlb.cli_identity(str(path_b))[0]


def test_a_backend_reprobes_its_binary_after_it_changes(tmp_path: Path) -> None:
    path = _binary(tmp_path, "claude", "old")
    calls: list[list[str]] = []
    backend = _backend(tmp_path, path, calls, {"old"})
    list(backend.send("one"))
    assert [("--thinking-display" in argv) for argv in calls] == [True, False]

    path.write_text("upgraded build", encoding="utf-8")
    events = list(backend.send("two"))

    assert "--thinking-display" in calls[-1] and len(calls) == 3
    assert events[-1] == {"type": "turn_end", "payload": {}}
    assert backend.thinking_display is not None and backend.thinking_display["state"] == "enabled"


def test_an_unrelated_failure_records_no_rejection(tmp_path: Path) -> None:
    path = _binary(tmp_path, "claude")
    calls: list[list[str]] = []

    def spawn(argv: Any, cwd: Any, stdin_text: Any = None) -> _FakeProcess:
        calls.append(list(argv))
        return _FakeProcess([], ["some unrelated provider failure\n"], 1)

    backend = mlb.CliManagerBackend(
        "claude_cli", "claude-test-model", tmp_path,
        plan_builder=lambda backend_id, prompt, repo, *, model="": _Plan([str(path), "-p", prompt], str(repo)),
        spawn=spawn,
    )
    events = list(backend.send("go"))

    assert len(calls) == 1
    assert [event["type"] for event in events] == ["error"]
    assert mlb._THINKING_DISPLAY_REJECTIONS == {}


def test_an_empty_claude_thinking_block_is_counted_not_dropped() -> None:
    context = mlb.TurnContext()
    message = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": ""},
        {"type": "thinking", "thinking": "   "},
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "thinking", "thinking": "visible step"},
    ]}}

    events = mlb.translate("claude_cli", message, context)
    end = mlb.translate("claude_cli", {"type": "result", "usage": {}}, context)

    assert [event["payload"]["text"] for event in events if event["type"] == "reasoning"] == ["visible step"]
    assert end == [{"type": "turn_end", "payload": {"reasoning_redacted": 3}}]


def test_a_turn_with_only_visible_thinking_reports_no_redaction() -> None:
    context = mlb.TurnContext()
    mlb.translate("claude_cli", {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "all shown"},
    ]}}, context)
    assert mlb.translate("claude_cli", {"type": "result", "usage": {}}, context) == [
        {"type": "turn_end", "payload": {}},
    ]


def test_an_empty_opencode_reasoning_part_is_counted_on_the_flushed_turn_end() -> None:
    context = mlb.TurnContext()
    assert mlb.translate("opencode_cli", {"type": "reasoning", "part": {"type": "reasoning", "text": ""}}, context) == []
    assert mlb.flush("opencode_cli", context) == [{"type": "turn_end", "payload": {"reasoning_redacted": 1}}]


def _status_with_backend(monkeypatch: pytest.MonkeyPatch, backend: Any) -> dict[str, Any]:
    """manager_loop_service.status() over a fake entry whose orchestrator holds ``backend``."""
    orchestrator = SimpleNamespace(session=None, store=None, _backend=backend)
    orchestrator.thinking_display = ManagerOrchestrator.thinking_display.fget(orchestrator)
    entry = SimpleNamespace(
        orchestrator=orchestrator, queue_lock=threading.Lock(), send_queue=[],
        turn_lock=threading.Lock(), last_turn=None,
    )
    monkeypatch.setattr(manager_loop_service, "_entry_for", lambda repo: entry)
    monkeypatch.setattr(manager_loop_service, "_session_catalog", lambda store: [])
    monkeypatch.setattr(manager_loop_service, "_wake_status", lambda entry: None)
    return manager_loop_service.status("unused-repo")


def test_status_exposes_the_live_backends_thinking_display_state(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"state": "rejected", "executable": "/opt/claude", "identity": ["/opt/claude", 1, 2]}
    payload = _status_with_backend(monkeypatch, SimpleNamespace(thinking_display=state))
    assert payload["ok"] is True
    assert payload["thinking_display"] == state


def test_status_reports_no_thinking_display_without_a_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _status_with_backend(monkeypatch, None)["thinking_display"] is None
