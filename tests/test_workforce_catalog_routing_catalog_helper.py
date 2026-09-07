"""The shared routing-catalog assembly (Finding 2).

``build_catalog`` defaults ``process_rows``, ``usage_rows`` and
``cost_per_accepted_outcome`` to empty.  ``rank_task`` falls through to a bare
``build_catalog(repo_root)`` when no ``catalog=`` is given, so any caller that
forgets the three arguments ranks every candidate on the conservative prior and
resolves the resulting total tie on the lexical ``(provider, model, worker_id)``
tie-break -- and gets back a decision that looks perfectly successful.

``build_routing_catalog`` is the single place that assembles all three, so the
mistake cannot be made one argument at a time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import workforce_catalog


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    (root / ".aiworkhub/runtime/process_logs/process_events.jsonl").write_text(
        "", encoding="utf-8"
    )
    return root


def test_build_routing_catalog_supplies_all_three_evidence_arguments(repo, monkeypatch):
    seen: dict[str, object] = {}

    def fake_build_catalog(repo_root, **kwargs):
        seen.update(kwargs)
        seen["repo_root"] = repo_root
        return {"workers": []}

    monkeypatch.setattr(workforce_catalog, "build_catalog", fake_build_catalog)
    monkeypatch.setattr(
        workforce_catalog.cost_ledger,
        "build_cost_ledger",
        lambda **_: {
            "tasks": [{"task_id": "T1", "cost_usd": 1.0}],
            "cost_per_accepted_outcome": {"gpt-5.5": {"code": {"medium": {}}}},
        },
    )

    workforce_catalog.build_routing_catalog(repo, process_rows=[{"task_id": "T1"}])

    assert seen["process_rows"] == [{"task_id": "T1"}]
    assert seen["usage_rows"] == [{"task_id": "T1", "cost_usd": 1.0}]
    assert seen["cost_per_accepted_outcome"] == {"gpt-5.5": {"code": {"medium": {}}}}


def test_rank_task_without_a_catalog_builds_one_with_no_evidence_at_all(repo, monkeypatch):
    """Pins the defect itself.

    ``rank_task(repo, task)`` with no ``catalog=`` falls through to a bare
    ``build_catalog(repo_root)``.  Every evidence argument then takes its empty
    default, which is why the reviewer route ranked 16 candidates that were tied
    through all nine substantive keys.
    """
    import inspect

    from aiworkhub import workforce_router

    defaults = inspect.signature(workforce_catalog.build_catalog).parameters
    assert defaults["process_rows"].default is None
    assert defaults["usage_rows"].default is None
    assert defaults["cost_per_accepted_outcome"].default is None

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        workforce_catalog,
        "build_catalog",
        lambda root, **kw: seen.update(kw) or {"workers": []},
    )

    task = workforce_router.TaskRequirements.build(
        task_id="T", repo_id="r", kinds={"code"}, risk="medium",
        context_tokens=0, tool_needs=set(), quality_floor=0.0,
    )
    workforce_catalog.rank_task(repo, task)

    assert seen == {}, "bare rank_task passed no evidence arguments at all"


def test_helper_reads_the_process_log_of_the_named_authority_repo(repo, monkeypatch):
    """The reader must never fall back to an ambient ProcessManager."""
    captured: dict[str, object] = {}

    class FakeDashboard:
        @staticmethod
        def read_process_runs(*, process_log_path, limit):
            captured["path"] = Path(process_log_path)
            captured["limit"] = limit
            return {"processes": [{"task_id": "T9", "model": "gpt-5.5"}, "not-a-dict"]}

    monkeypatch.setitem(
        __import__("sys").modules, "aiworkhub.dashboard", FakeDashboard
    )

    rows = workforce_catalog.default_process_rows(repo)

    assert captured["path"] == repo / ".aiworkhub/runtime/process_logs/process_events.jsonl"
    assert captured["limit"] == 1000
    assert rows == [{"task_id": "T9", "model": "gpt-5.5"}]


def test_helper_defaults_process_rows_from_the_repository_when_not_supplied(repo, monkeypatch):
    calls: list[Path] = []

    monkeypatch.setattr(
        workforce_catalog,
        "default_process_rows",
        lambda root, **_: calls.append(Path(root)) or [],
    )
    monkeypatch.setattr(
        workforce_catalog.cost_ledger,
        "build_cost_ledger",
        lambda **_: {"tasks": [], "cost_per_accepted_outcome": {}},
    )
    monkeypatch.setattr(workforce_catalog, "build_catalog", lambda root, **kw: {"workers": []})

    workforce_catalog.build_routing_catalog(repo)

    assert calls == [repo.resolve()]


def test_helper_tolerates_a_ledger_with_no_tasks_or_economics(repo, monkeypatch):
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        workforce_catalog.cost_ledger, "build_cost_ledger", lambda **_: {}
    )
    monkeypatch.setattr(
        workforce_catalog,
        "build_catalog",
        lambda root, **kw: seen.update(kw) or {"workers": []},
    )

    workforce_catalog.build_routing_catalog(repo, process_rows=[])

    assert seen["usage_rows"] == []
    assert seen["cost_per_accepted_outcome"] == {}
