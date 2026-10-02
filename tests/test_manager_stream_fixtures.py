"""W1-P1b: recorded provider streams translate to exact v3 event lists."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import manager_loop as ml
from aiworkhub import manager_loop_backends as mlb

FIXTURES = Path(__file__).parent / "fixtures" / "manager_streams"
FIXTURE_CASES = (
    ("claude_cli", "claude_cli"),
    ("claude_cli_seat", "claude_cli"),
    ("codex_cli", "codex_cli"),
    ("opencode_cli", "opencode_cli"),
)
USAGE_KEYS = {"input", "cache_read", "cache_write", "output", "context_window", "context_fill", "raw"}


def translated(stem: str, backend_id: str) -> list[dict]:
    context = mlb.TurnContext()
    events: list[dict] = []
    for line in (FIXTURES / f"{stem}.jsonl").read_text(encoding="utf-8").splitlines():
        # Deltas (W1-P2) are display-only, never events, exactly as the orchestrator treats them.
        events.extend(e for e in mlb.translate(backend_id, json.loads(line), context) if e["type"] != "delta")
    events.extend(mlb.flush(backend_id, context))
    return events


@pytest.mark.parametrize(("stem", "backend_id"), FIXTURE_CASES)
def test_fixture_translates_to_the_expected_v3_events(stem: str, backend_id: str) -> None:
    expected = json.loads((FIXTURES / f"{stem}.expected.json").read_text(encoding="utf-8"))
    assert translated(stem, backend_id) == expected


@pytest.mark.parametrize(("stem", "backend_id"), FIXTURE_CASES)
def test_fixture_events_keep_the_v3_invariants(stem: str, backend_id: str) -> None:
    events = translated(stem, backend_id)
    kinds = {event["type"] for event in events}
    assert kinds <= ml.EVENT_TYPES
    for event in events:
        if event["type"] in ("tool_call", "tool_result", "command", "file_change"):
            assert event["payload"]["call_id"], event
    commands = [e["payload"] for e in events if e["type"] == "command"]
    assert any("git --version" in c["command"] and c["status"] == "completed" for c in commands)
    if stem == "claude_cli_seat":
        results = [e["payload"] for e in events if e["type"] == "tool_result"]
        call_ids = {e["payload"]["call_id"] for e in events if e["type"] == "tool_call"}
        denied = next(r for r in results if r["is_error"])
        assert denied["call_id"] in call_ids
        assert not any(e["type"] == "file_change" for e in events)
    else:
        assert any(e["payload"]["path"].endswith("notes.txt") for e in events if e["type"] == "file_change")
    ends = [e for e in events if e["type"] == "turn_end"]
    assert len(ends) == 1
    assert events[-1]["type"] == "turn_end"
    usage = ends[0]["payload"].get("usage")
    assert usage is None or set(usage) == USAGE_KEYS


def test_a_file_change_counts_content_lines_that_look_like_headers() -> None:
    change = mlb._file_change("e1", "notes.txt", [("alpha\n", "+++beta\n---gamma\n")], "update")
    assert change["payload"]["added"] == 2 and change["payload"]["removed"] == 1
    assert change["payload"]["diff"].splitlines()[:2] == ["--- a/notes.txt", "+++ b/notes.txt"]


def test_an_unpaired_result_without_context_still_carries_its_call_id() -> None:
    event = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
    }
    assert mlb.translate("claude_cli", event) == [
        {"type": "tool_result", "payload": {"call_id": "toolu_1", "name": "", "output": "ok", "is_error": False}}
    ]
