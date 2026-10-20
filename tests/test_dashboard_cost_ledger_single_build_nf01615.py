"""NF-2026-01615: build_cost_ledger resolves the repository root once per build."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiworkhub import cost_ledger, task_store


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _usage_events(count: int) -> list[dict[str, Any]]:
    return [
        {
            "task_id": f"T-{index}",
            "request_id": f"req-{index}",
            "runner": "runner_a",
            "model": "model-a",
            "provider": "provider-a",
            "topic": "dashboard-performance",
            "role": "worker",
            "total_tokens": 10 + index,
            "cost_usd": 0.01,
            "cost_observed": True,
            "usage_observed": True,
            "created_at": f"2026-10-20T00:00:{index:02d}+00:00",
        }
        for index in range(count)
    ]


def test_repository_identity_fallback_resolves_repo_root_once(monkeypatch, tmp_path):
    root = _init_repo(tmp_path)
    monkeypatch.setattr(
        task_store, "list_usage_events", lambda *_a, **_k: _usage_events(25)
    )
    resolves = {"count": 0}

    class CountingPath(type(Path())):  # type: ignore[misc]
        def resolve(self, *args: Any, **kwargs: Any):
            resolves["count"] += 1
            return super().resolve(*args, **kwargs)

    monkeypatch.setattr(cost_ledger, "Path", CountingPath)

    ledger = cost_ledger.build_cost_ledger(repo_root=root, include_tasks=True)

    assert len(ledger["tasks"]) == 25
    assert resolves["count"] == 1


def test_repository_identity_precedence_is_unchanged(tmp_path):
    root = tmp_path
    fallback = str(Path(root).resolve())
    identity = cost_ledger._repository_identity  # noqa: SLF001

    assert identity({"repository_id": "a", "repo_id": "b"}, root) == "a"
    assert identity({"repo_id": "b", "repository": "c"}, root) == "b"
    assert identity({"repository": "c", "repo_root": "d"}, root) == "c"
    assert identity({"repo_root": "d"}, root) == "d"
    assert identity({}, root) == fallback
    assert identity({}, root, resolved_root="pre-resolved") == "pre-resolved"
    assert identity({"repo_id": "b"}, root, resolved_root="pre-resolved") == "b"
