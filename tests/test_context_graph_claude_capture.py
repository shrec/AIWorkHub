"""Proof that the Claude manager route is actually captured by the Context Graph.

NF-2026-00256: on a Claude-only installation the Context Graph previously
reported ``claude: not_configured`` and recorded nothing from the seat that
holds the manager role.  These tests feed real Claude transcript records
through :mod:`aiworkhub.context_capture` and assert the resulting events land
in the ledger, that the status surface no longer reports a blanket
``not_configured``, and that non-final records (tool output, sidechain and
meta) are never captured -- the same ``final_items`` contract as Codex.
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import context_capture, context_graph, feature_settings, task_store


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _enable(repo: Path) -> None:
    result = feature_settings.update(
        repo,
        changes={"context_graph": True},
        expected_revision=feature_settings.load(repo)["revision"],
    )
    assert result["features"]["context_graph"] is True


def _records() -> list[dict]:
    return [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "sess-1",
            "timestamp": "2026-08-18T00:00:00Z",
            "message": {"role": "user", "content": "useralpha please summarize"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "sess-1",
            "timestamp": "2026-08-18T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "assistantalpha decision recorded"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": "sess-1",
            "timestamp": "2026-08-18T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "toolonly blob"}
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "s1",
            "sessionId": "sess-1",
            "timestamp": "2026-08-18T00:00:03Z",
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "sidechainalpha subagent"}],
            },
        },
        {
            "type": "user",
            "uuid": "m1",
            "sessionId": "sess-1",
            "timestamp": "2026-08-18T00:00:04Z",
            "isMeta": True,
            "message": {"role": "user", "content": "metaalpha caveat"},
        },
    ]


def test_claude_route_records_only_final_user_and_assistant_messages(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _enable(repo)

    summary = context_capture.capture_claude_records(repo, _records())

    # Two final messages captured; tool-only, sidechain and meta records skipped.
    # No record is rejected: every sessionId here passes the strict token rule.
    assert summary == {
        "ok": True,
        "provider": "claude",
        "state": "final_items",
        "seen": 5,
        "captured": 2,
        "idempotent": 0,
        "skipped": 3,
        "rejected": 0,
        "rejected_records": [],
    }

    user_hit = context_graph.search(repo, "useralpha")
    assert user_hit["count"] == 1
    assert user_hit["results"][0]["content"] == "useralpha please summarize"
    assert user_hit["results"][0]["source_ref"] == "claude_transcript:u1"

    # The assistant text is captured with tool_use stripped out (final_items only).
    assistant_hit = context_graph.search(repo, "assistantalpha")
    assert assistant_hit["count"] == 1
    assert assistant_hit["results"][0]["content"] == "assistantalpha decision recorded"

    # Non-final records never enter the ledger.
    assert context_graph.search(repo, "toolonly")["count"] == 0
    assert context_graph.search(repo, "sidechainalpha")["count"] == 0
    assert context_graph.search(repo, "metaalpha")["count"] == 0

    runtime = context_graph.status(repo)
    assert runtime["ready"] is True
    assert runtime["events"] == 2
    # The blanket not_configured is gone; codex parity is preserved.
    assert runtime["capture_adapters"]["claude"] == "final_items"
    assert runtime["capture_adapters"]["claude"] != "not_configured"
    assert runtime["capture_adapters"]["codex"] == "final_items"


def test_claude_capture_is_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _enable(repo)

    first = context_capture.capture_claude_records(repo, _records())
    second = context_capture.capture_claude_records(repo, _records())

    assert first["captured"] == 2 and first["idempotent"] == 0
    assert second["captured"] == 0 and second["idempotent"] == 2
    assert context_graph.status(repo)["events"] == 2


def test_claude_capture_skips_malformed_session_id_and_keeps_the_batch(tmp_path: Path) -> None:
    # A sessionId that passes the loose local token check but violates the
    # Context Graph's strict token regex must not abort the whole batch: it is
    # skipped by name and the well-formed records around it are still captured.
    repo = _repo(tmp_path)
    _enable(repo)

    records = [
        {
            "type": "user",
            "uuid": "bad1",
            "sessionId": "sess bad",  # space is rejected by the strict token rule
            "timestamp": "2026-08-18T00:00:00Z",
            "message": {"role": "user", "content": "rejectedcharset content"},
        },
        {
            "type": "user",
            "uuid": "ok1",
            "sessionId": "sess-ok",
            "timestamp": "2026-08-18T00:00:01Z",
            "message": {"role": "user", "content": "keptone content"},
        },
        {
            "type": "assistant",
            "uuid": "ok2",
            "sessionId": "sess-ok",
            "timestamp": "2026-08-18T00:00:02Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "kepttwo body"}]},
        },
    ]

    summary = context_capture.capture_claude_records(repo, records)

    # Two well-formed records captured; the malformed one is reported by name.
    assert summary["captured"] == 2
    assert summary["rejected"] == 1
    assert summary["rejected_records"] == [
        {"item_id": "bad1", "session_id": "sess bad", "reason": "invalid_session_id"}
    ]

    # The good records really landed -- the bad one did not throw the batch away.
    assert context_graph.search(repo, "keptone")["count"] == 1
    assert context_graph.search(repo, "kepttwo")["count"] == 1
    # The malformed record never entered the ledger.
    assert context_graph.search(repo, "rejectedcharset")["count"] == 0
    assert context_graph.status(repo)["events"] == 2


def test_claude_capture_fails_closed_when_context_graph_disabled(tmp_path: Path) -> None:
    repo = _repo(tmp_path)  # context_graph feature left disabled

    summary = context_capture.capture_claude_records(repo, _records())

    assert summary["captured"] == 0
    assert summary["skipped"] == 5
    assert context_graph.status(repo) == feature_settings.disabled_result("context_graph")


def test_extract_ignores_non_final_records() -> None:
    tool_only = {
        "type": "user",
        "uuid": "u2",
        "sessionId": "sess-1",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
    }
    sidechain = {
        "type": "assistant",
        "uuid": "s1",
        "sessionId": "sess-1",
        "isSidechain": True,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }
    assert context_capture.extract_claude_completed_message(tool_only) is None
    assert context_capture.extract_claude_completed_message(sidechain) is None
    assert context_capture.extract_claude_completed_message({}) is None

    good = {
        "type": "assistant",
        "uuid": "a1",
        "sessionId": "sess-1",
        "timestamp": "2026-08-18T00:00:01Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "kept"},
                {"type": "tool_use", "name": "Bash"},
            ],
        },
    }
    message = context_capture.extract_claude_completed_message(good)
    assert message is not None
    assert message.role == "assistant"
    assert message.content == "kept"
    assert message.item_id == "a1"
