"""NF-2026-01059: task_template_show scope guidance must state the
per-template mandatory_changed_outputs defaults instead of claiming test_paths
are never mandatory changed outputs."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import core, server, task_store


def test_task_template_show_scope_states_mandatory_output_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo.resolve()))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo.resolve()))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        core,
        "_verify_coordinator_capability",
        lambda *args, **kwargs: (True, ""),
    )
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": "01234567-89ab-4def-8123-456789abcdef",
            "route_state": "verified",
        },
    )
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", repo.resolve())

    reply = server.aiworkhub_task_template_show("bugfix_with_regression")

    assert reply["ok"] is True
    scope = reply["creation_guidance"]["scope"]
    assert "mandatory_changed_outputs" in scope
    assert "bugfix_with_regression requires the whole write set" in scope
    assert "test_only the test paths" in scope
