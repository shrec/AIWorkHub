from __future__ import annotations

import time
from pathlib import Path

from aiworkhub import (
    context_graph,
    feature_settings,
    manager_transcript_capture,
    task_store,
)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    initialized = task_store.initialize_repository(repo)
    assert initialized["ok"]
    return repo, initialized["repo_id"]


def _completed(
    *,
    item_type: str,
    content: str,
    item_id: str = "item-1",
) -> dict:
    item = (
        {"id": item_id, "type": "userMessage", "content": [{"type": "text", "text": content}]}
        if item_type == "userMessage"
        else {"id": item_id, "type": "agentMessage", "text": content}
    )
    return {
        "method": "item/completed",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "completedAtMs": 1_785_456_000_000,
            "item": item,
        },
    }


def _wait_for(capture: manager_transcript_capture.ManagerTranscriptCapture, key: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = capture.status()
        if status[key]:
            return status
        time.sleep(0.01)
    raise AssertionError(f"capture status never reported {key}")


def test_extracts_only_authoritative_completed_user_and_agent_messages() -> None:
    user = manager_transcript_capture.extract_codex_completed_message(
        _completed(item_type="userMessage", content="hello")
    )
    agent = manager_transcript_capture.extract_codex_completed_message(
        _completed(item_type="agentMessage", content="world", item_id="item-2")
    )

    assert user is not None and user.role == "user" and user.content == "hello"
    assert agent is not None and agent.role == "assistant" and agent.content == "world"
    assert manager_transcript_capture.extract_codex_completed_message(
        {"method": "item/agentMessage/delta", "params": {"delta": "secret"}}
    ) is None
    assert manager_transcript_capture.extract_codex_completed_message(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "completedAtMs": 1_785_456_000_000,
                "item": {"id": "tool-1", "type": "commandExecution"},
            },
        }
    ) is None


def test_verified_manager_route_captures_without_duplicate_events(tmp_path: Path) -> None:
    repo, repo_id = _repo(tmp_path)
    feature_settings.update(repo, changes={"context_graph": True}, expected_revision=0)
    route = lambda **_kwargs: {  # noqa: E731
        "ok": True,
        "repo_id": repo_id,
        "repo_root": str(repo),
    }
    capture = manager_transcript_capture.ManagerTranscriptCapture(
        repo_id=repo_id,
        extension_host_pid=123,
        route_resolver=route,
        route_retry_seconds=0,
    )
    capture.start()
    message = _completed(item_type="agentMessage", content="durable answer")
    capture.offer(message)
    capture.offer(message)
    _wait_for(capture, "idempotent")
    capture.close()

    found = context_graph.search(repo, "durable answer")
    assert found["count"] == 1
    assert found["results"][0]["role"] == "assistant"
    assert found["results"][0]["thread_id"] == "thread-1"
    status = capture.status()
    assert status["captured"] == 1
    assert status["idempotent"] == 1


def test_disabled_or_unverified_route_never_captures(tmp_path: Path) -> None:
    repo, repo_id = _repo(tmp_path)
    valid_route = lambda **_kwargs: {  # noqa: E731
        "ok": True,
        "repo_id": repo_id,
        "repo_root": str(repo),
    }
    disabled = manager_transcript_capture.ManagerTranscriptCapture(
        repo_id=repo_id,
        extension_host_pid=123,
        route_resolver=valid_route,
        route_retry_seconds=0,
    )
    disabled.start()
    disabled.offer(_completed(item_type="userMessage", content="not retained"))
    _wait_for(disabled, "skipped")
    disabled.close()

    feature_settings.update(repo, changes={"context_graph": True}, expected_revision=0)
    unverified = manager_transcript_capture.ManagerTranscriptCapture(
        repo_id=repo_id,
        extension_host_pid=123,
        route_resolver=lambda **_kwargs: {"ok": False, "error": "route_not_observed"},
        route_retry_seconds=0,
    )
    unverified.start()
    unverified.offer(_completed(item_type="userMessage", content="also not retained"))
    _wait_for(unverified, "skipped")
    unverified.close()

    assert context_graph.status(repo)["events"] == 0
