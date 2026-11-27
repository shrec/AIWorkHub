"""NF-2026-01014: the tool guard never dead-ends a session without AIWorkHub MCP.

A guard installed mid-session used to deny Edit/Grep while naming
``aiworkhub_*`` tools the session had not loaded. The guard now denies only when
the session's own transcript names an AIWorkHub MCP tool; otherwise it allows
the call and tells the model to restart/reload the session. Denies carry the
same hint.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import tool_guard_hook  # noqa: E402

DENIALS_REL = Path(".aiworkhub", "runtime", "tool_guard", "denials.jsonl")
MANAGER_LINE = (
    b'{"type":"assistant","message":{"content":[{"type":"tool_use",'
    b'"name":"mcp__AIWorkHub__aiworkhub_manager_source_graph_query"}]}}\n'
)
WORKER_LINE = b'{"tools":["mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_semantic_edit_apply"]}\n'
OTHER_LINE = b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit"}]}}\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub" / "runtime").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "x.py").write_bytes(b"foo = 1\n")
    return root.resolve()


def _run(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    tool_input: dict[str, Any],
    transcript: object,
) -> str:
    payload = {
        "session_id": "session-1",
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if transcript is not None:
        payload["transcript_path"] = transcript
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert tool_guard_hook.main([]) == 0
    return capsys.readouterr().out


def _edit(repo: Path) -> dict[str, Any]:
    return {"file_path": str(repo / "src" / "x.py"), "old_string": "foo = 1", "new_string": "foo = 2"}


def _grep() -> dict[str, Any]:
    return {"pattern": "foo", "path": "src"}


def _hint_only(out: str) -> None:
    lines = out.splitlines()
    assert len(lines) == 1, out
    output = json.loads(lines[0])["hookSpecificOutput"]
    assert output == {
        "hookEventName": "PreToolUse",
        "additionalContext": tool_guard_hook.MCP_ABSENT_HINT,
    }
    assert "restart/reload the session" in tool_guard_hook.MCP_ABSENT_HINT
    assert "not loaded in this session" in tool_guard_hook.MCP_ABSENT_HINT


def test_signal_absent_allows_an_edit_and_a_grep_with_the_restart_hint(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    silent = tmp_path / "silent.jsonl"
    silent.write_bytes(OTHER_LINE * 3)

    _hint_only(_run(repo, monkeypatch, capsys, "Edit", _edit(repo), str(silent)))
    _hint_only(_run(repo, monkeypatch, capsys, "Grep", _grep(), str(silent)))
    assert not (repo / DENIALS_REL).exists()


def _deny_reason(out: str) -> str:
    lines = out.splitlines()
    assert len(lines) == 1, out
    output = json.loads(lines[0])["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    reason = output["permissionDecisionReason"]
    assert isinstance(reason, str) and reason.endswith(tool_guard_hook.DENY_RESTART_HINT)
    return reason


@pytest.mark.parametrize("which", range(4))
def test_an_unknown_signal_fails_closed_on_an_indexed_read(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    which: int,
) -> None:
    transcript = [None, "", str(tmp_path / "missing.jsonl"), 7][which]
    read = {"file_path": str(repo / "src" / "x.py")}
    expected = tool_guard_hook.decide("Read", read, repo, str(repo), "session-1")
    assert expected is not None and expected[0] == "R5"

    reason = _deny_reason(_run(repo, monkeypatch, capsys, "Read", read, transcript))

    assert reason == expected[1] + " " + tool_guard_hook.DENY_RESTART_HINT
    assert tool_guard_hook.aiworkhub_mcp_loaded(transcript) is True


def test_a_foreign_scratch_image_read_still_denies_without_the_mcp(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = repo / ".scratch" / "other-session" / "shot.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    silent = tmp_path / "silent.jsonl"
    silent.write_bytes(OTHER_LINE * 3)
    read = {"file_path": str(image)}
    expected = tool_guard_hook.decide("Read", read, repo, str(repo), "session-1")
    assert expected is not None and expected[0] == "R8"
    assert tool_guard_hook.aiworkhub_mcp_loaded(str(silent)) is False

    reason = _deny_reason(_run(repo, monkeypatch, capsys, "Read", read, str(silent)))

    assert reason == expected[1] + " " + tool_guard_hook.DENY_RESTART_HINT
    assert (repo / DENIALS_REL).is_file()


@pytest.mark.parametrize("line", [MANAGER_LINE, WORKER_LINE])
def test_signal_present_denies_as_before_and_the_deny_carries_the_hint(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    line: bytes,
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(OTHER_LINE + line)
    expected = tool_guard_hook.decide("Edit", _edit(repo), repo, str(repo), "session-1")
    assert expected is not None and expected[0] == "R6"

    out = _run(repo, monkeypatch, capsys, "Edit", _edit(repo), str(transcript))

    output = json.loads(out.splitlines()[0])["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"] == (
        expected[1] + " " + tool_guard_hook.DENY_RESTART_HINT
    )
    assert "restart/reload the session" in output["permissionDecisionReason"]
    assert (repo / DENIALS_REL).is_file()
    # Grep has no deny rule: with the tools loaded it passes silently.
    assert _run(repo, monkeypatch, capsys, "Grep", _grep(), str(transcript)) == ""


def test_an_allowed_call_stays_silent_without_the_signal(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(repo, monkeypatch, capsys, "Bash", {"command": "git status"}, None) == ""


def test_a_root_without_aiworkhub_state_stays_silent_for_grep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert _run(bare.resolve(), monkeypatch, capsys, "Grep", _grep(), None) == ""


def test_a_name_split_across_scan_chunks_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool_guard_hook, "_TRANSCRIPT_CHUNK_BYTES", 64)
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 50 + MANAGER_LINE)

    assert tool_guard_hook.aiworkhub_mcp_loaded(str(transcript)) is True


def test_the_scan_is_bounded_and_a_capped_scan_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool_guard_hook, "_TRANSCRIPT_CHUNK_BYTES", 64)
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 512)
    assert tool_guard_hook.aiworkhub_mcp_loaded(str(transcript)) is False

    monkeypatch.setattr(tool_guard_hook, "_TRANSCRIPT_SCAN_MAX_BYTES", 128)
    assert tool_guard_hook.aiworkhub_mcp_loaded(str(transcript)) is True
