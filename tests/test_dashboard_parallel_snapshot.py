from __future__ import annotations

import threading

from aiworkhub import dashboard


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
