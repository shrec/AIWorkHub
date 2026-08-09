"""Completion-inbox operational-failure enrichment from process rows.

Pure helper coverage plus one integration test proving the post-``compact_processes``
call site inside ``aiworkhub_completion_inbox`` invokes the helper exactly once
without altering adapter readiness or the compact field list.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    completion_inbox,
    core,
    deepseek_credentials,
    process_launcher,
    repo_policy,
)
from aiworkhub.server import (  # noqa: E402
    _enrich_operational_failures_from_processes,
    aiworkhub_completion_inbox,
)

import aiworkhub.server as server  # noqa: E402


def _enrich(failures: Any, processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _enrich_operational_failures_from_processes(failures, processes)


def test_exact_match_copies_process_error_and_workspace_retained() -> None:
    failures = [{"request_id": "req-1", "task_id": "T1"}]
    processes = [
        {
            "request_id": "req-1",
            "task_id": "T1",
            "error": "boom",
            "workspace_retained": True,
        }
    ]
    enriched = _enrich(failures, processes)
    assert enriched[0]["operational_error"] == "boom"
    assert enriched[0]["workspace_retained"] is True


def test_workspace_retained_false_is_preserved() -> None:
    failures = [{"request_id": "req-2"}]
    processes = [{"request_id": "req-2", "error": "x", "workspace_retained": False}]
    enriched = _enrich(failures, processes)
    assert enriched[0]["workspace_retained"] is False
    assert enriched[0]["operational_error"] == "x"


def test_duplicate_exact_request_resolves_latest_wins() -> None:
    failures = [{"request_id": "req-3"}]
    processes = [
        {"request_id": "req-3", "error": "old", "workspace_retained": False},
        {"request_id": "req-3", "error": "new", "workspace_retained": True},
    ]
    enriched = _enrich(failures, processes)
    assert enriched[0]["operational_error"] == "new"
    assert enriched[0]["workspace_retained"] is True


def test_older_same_task_different_request_is_rejected() -> None:
    failures = [{"request_id": "req-4", "task_id": "T4"}]
    processes = [{"request_id": "req-other", "task_id": "T4", "error": "boom"}]
    enriched = _enrich(failures, processes)
    assert "operational_error" not in enriched[0]
    assert "workspace_retained" not in enriched[0]


def test_unrelated_request_is_rejected() -> None:
    failures = [{"request_id": "req-5"}]
    processes = [{"request_id": "req-unrelated", "error": "boom"}]
    enriched = _enrich(failures, processes)
    assert "operational_error" not in enriched[0]


def test_existing_operational_error_is_not_overwritten() -> None:
    failures = [{"request_id": "req-6", "operational_error": "kept"}]
    processes = [
        {
            "request_id": "req-6",
            "error": "would-overwrite",
            "workspace_retained": True,
        }
    ]
    enriched = _enrich(failures, processes)
    assert enriched[0]["operational_error"] == "kept"
    assert enriched[0]["workspace_retained"] is True


def test_input_failure_dicts_are_not_mutated() -> None:
    failures = [{"request_id": "req-7", "task_id": "T7"}]
    processes = [{"request_id": "req-7", "error": "x", "workspace_retained": False}]
    enriched = _enrich(failures, processes)
    assert failures == [{"request_id": "req-7", "task_id": "T7"}]
    assert enriched[0] is not failures[0]
    assert "operational_error" not in failures[0]
    assert "workspace_retained" not in failures[0]


def test_empty_or_non_list_returns_input_unchanged() -> None:
    assert _enrich([], [{"request_id": "x"}]) == []
    assert _enrich(None, []) is None


def test_integration_completion_inbox_invokes_helper_after_compact(
    monkeypatch, tmp_path
) -> None:
    captured: list[tuple[Any, list[dict[str, Any]]]] = []
    original = _enrich_operational_failures_from_processes

    def spy(
        operational_failures: Any,
        compact_processes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        captured.append((operational_failures, list(compact_processes)))
        return original(operational_failures, compact_processes)

    monkeypatch.setattr(server, "_enrich_operational_failures_from_processes", spy)

    def fake_build(topic, limit, stale_processing_hours):
        return {
            "operational_failures": [{"request_id": "req-int", "task_id": "TINT"}],
            "review_queue": [],
            "stale_processing": [],
            "runner_mismatch_warnings": [],
            "latest_validation_facts": [],
        }

    monkeypatch.setattr(completion_inbox, "build_completion_inbox", fake_build)

    class _FakeManager:
        def list_processes(self, limit=200):
            return {
                "processes": [
                    {
                        "request_id": "req-int",
                        "error": "kaboom",
                        "workspace_retained": False,
                    }
                ]
            }

    monkeypatch.setattr(
        process_launcher, "default_manager", lambda: _FakeManager()
    )
    monkeypatch.setattr(core, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        deepseek_credentials,
        "adapter_readiness",
        lambda repo: {"adapters": []},
    )
    monkeypatch.setattr(
        repo_policy,
        "build_preflight",
        lambda repo_root: {"providers": []},
    )

    result = aiworkhub_completion_inbox(
        topic=None, limit=5, stale_processing_hours=1.0
    )

    assert len(captured) == 1
    failures_arg, processes_arg = captured[0]
    assert failures_arg == [{"request_id": "req-int", "task_id": "TINT"}]
    assert len(processes_arg) == 1
    assert processes_arg[0]["request_id"] == "req-int"
    assert "workspace_retained" in processes_arg[0]
    assert result["operational_failures"][0]["operational_error"] == "kaboom"
    assert result["operational_failures"][0]["workspace_retained"] is False
    assert result["adapter_readiness"] == {
        "adapters": [],
        "preflight_schema_id": None,
    }
    assert (
        result["agent_processes"]["schema_id"]
        == "aiworkhub.completion_process_summary.v1"
    )
