from __future__ import annotations

import threading
import time

from aiworkhub import dashboard


def test_dashboard_provider_singleflights_shared_snapshot_inputs(monkeypatch, tmp_path):
    provider = dashboard.DashboardProvider(repo_root=tmp_path)
    calls = {"cards": 0, "ledger": 0, "preflight": 0}

    def load_cards(*_args, **_kwargs):
        calls["cards"] += 1
        time.sleep(0.05)
        return [{"task_id": "T-1", "allowed_writes": ["src/a.py"]}]

    def load_ledger(*_args, **_kwargs):
        calls["ledger"] += 1
        time.sleep(0.05)
        return {
            "tasks": [{"task_id": "T-1", "total_tokens": 7}],
            "cost_per_accepted_outcome": {"accepted": 1},
        }

    def load_preflight(*_args, **_kwargs):
        calls["preflight"] += 1
        time.sleep(0.05)
        return {"providers": []}

    monkeypatch.setattr(dashboard.task_store, "list_task_cards", load_cards)
    monkeypatch.setattr(dashboard.cost_ledger, "build_cost_ledger", load_ledger)
    monkeypatch.setattr(dashboard.repo_policy, "build_preflight", load_preflight)
    monkeypatch.setattr(dashboard.task_store, "list_tasks", lambda *_a, **_k: [])
    monkeypatch.setattr(
        dashboard, "read_process_runs", lambda **_kwargs: {"processes": []}
    )
    monkeypatch.setattr(
        dashboard.workforce_catalog,
        "build_catalog",
        lambda _repo, **kwargs: {
            "cards": len(kwargs["cards"]),
            "usage": len(kwargs["usage_rows"]),
        },
    )
    monkeypatch.setattr(
        dashboard.task_plan,
        "build_snapshot",
        lambda cards: {"card_count": len(cards)},
    )
    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: 8)

    errors: list[dict[str, str]] = []
    with provider.snapshot_read_scope():
        result = dashboard._parallel_snapshot_reads(  # noqa: SLF001
            {
                "cost": ("cost", provider.get_cost_ledger, {}),
                "workforce": ("workforce", provider.get_workforce_catalog, {}),
                "preflight": ("preflight", provider.get_environment_preflight, {}),
                "needfix": ("needfix", provider.get_needfix_snapshot, {}),
                "plan": ("plan", provider.get_task_plan, {}),
                "collision": ("collision", provider.get_collision_report, {}),
            },
            errors,
        )

    assert errors == []
    assert calls == {"cards": 1, "ledger": 1, "preflight": 1}
    assert result["cost"]["tasks"] == []
    assert result["workforce"] == {"cards": 1, "usage": 1}
    assert result["plan"]["card_count"] == 1

    # The cache never survives its read set and direct provider calls preserve
    # the historical fresh-read behavior.
    provider.get_task_plan()
    provider.get_task_plan()
    provider.get_environment_preflight()
    provider.get_environment_preflight()
    provider.get_needfix_snapshot()
    provider.get_needfix_snapshot()
    assert calls["cards"] == 5
    assert calls["preflight"] == 3


def test_parallel_snapshot_reads_use_core_derived_concurrency(monkeypatch):
    second_started = threading.Event()
    first_observed_parallelism: list[bool] = []

    def first() -> str:
        first_observed_parallelism.append(second_started.wait(timeout=1.0))
        return "first"

    def second() -> str:
        second_started.set()
        return "second"

    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: 4)
    errors: list[dict[str, str]] = []

    result = dashboard._parallel_snapshot_reads(  # noqa: SLF001
        {
            "first": ("first", first, "fallback-first"),
            "second": ("second", second, "fallback-second"),
        },
        errors,
    )

    assert first_observed_parallelism == [True]
    assert result == {"first": "first", "second": "second"}
    assert errors == []


def test_parallel_snapshot_read_errors_keep_declaration_order(monkeypatch):
    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: 8)

    def fail(message: str):
        raise RuntimeError(message)

    errors: list[dict[str, str]] = []
    result = dashboard._parallel_snapshot_reads(  # noqa: SLF001
        {
            "slow_name": ("source.a", lambda: fail("a"), "fallback-a"),
            "fast_name": ("source.b", lambda: fail("b"), "fallback-b"),
        },
        errors,
    )

    assert result == {"slow_name": "fallback-a", "fast_name": "fallback-b"}
    assert [error["source"] for error in errors] == ["source.a", "source.b"]
