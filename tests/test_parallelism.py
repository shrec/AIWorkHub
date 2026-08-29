from __future__ import annotations

import os
from unittest import mock

from aiworkhub import parallelism


def test_cpu_capacity_prefers_process_affinity() -> None:
    with mock.patch.object(os, "sched_getaffinity", return_value={0, 1, 2}):
        with mock.patch.object(os, "cpu_count", return_value=64):
            assert parallelism.get_cpu_capacity() == 3


def test_cpu_capacity_has_safe_fallback() -> None:
    with mock.patch.object(os, "sched_getaffinity", side_effect=OSError):
        with mock.patch.object(os, "cpu_count", return_value=None):
            assert parallelism.get_cpu_capacity() == 1


def test_single_cpu_is_serial() -> None:
    with mock.patch.object(parallelism, "get_cpu_capacity", return_value=1):
        workers, receipt = parallelism.compute_worker_count(candidate_count=100)
    assert workers == 1
    assert receipt.reason == "single_cpu_serial"


def test_omitted_ceiling_uses_adaptive_capacity() -> None:
    with mock.patch.object(parallelism, "get_cpu_capacity", return_value=64):
        workers, receipt = parallelism.compute_worker_count(
            candidate_count=100, reserve=1,
        )
    assert workers == 63
    assert receipt.to_dict() == {
        "available_cpus": 64,
        "selected_workers": 63,
        "reserve": 1,
        "ceiling": 63,
        "nested": False,
        "reason": "capacity_based",
    }


def test_zero_reserve_keeps_one_interactive_core() -> None:
    with mock.patch.object(parallelism, "get_cpu_capacity", return_value=64):
        workers, receipt = parallelism.compute_worker_count(
            candidate_count=100, reserve=0,
        )
    assert workers == 63
    assert receipt.reserve == 1
    assert receipt.ceiling == 63


def test_explicit_ceiling_remains_authoritative() -> None:
    with mock.patch.object(parallelism, "get_cpu_capacity", return_value=64):
        workers, receipt = parallelism.compute_worker_count(
            candidate_count=100, reserve=1, ceiling=8,
        )
    assert workers == 8
    assert receipt.ceiling == 8


def test_explicit_ceiling_reports_applied_capacity_bound() -> None:
    with mock.patch.object(parallelism, "get_cpu_capacity", return_value=4):
        workers, receipt = parallelism.compute_worker_count(
            candidate_count=100, reserve=1, ceiling=100,
        )
    assert workers == 3
    assert receipt.ceiling == 3


def test_below_candidate_threshold_is_serial() -> None:
    with mock.patch.object(parallelism, "get_cpu_capacity", return_value=16):
        workers, receipt = parallelism.compute_worker_count(
            candidate_count=2, min_candidates=4,
        )
    assert workers == 1
    assert receipt.reason == "below_min_candidates"


def test_nested_worker_pool_is_serial_and_scope_resets() -> None:
    assert parallelism.pool_is_nested() is False
    with parallelism.worker_pool_scope():
        assert parallelism.pool_is_nested() is True
        with mock.patch.object(parallelism, "get_cpu_capacity", return_value=32):
            workers, receipt = parallelism.compute_worker_count(candidate_count=100)
        assert workers == 1
        assert receipt.nested is True
        assert receipt.reason == "nested_invocation_serial"
    assert parallelism.pool_is_nested() is False


def test_worker_selection_is_deterministic() -> None:
    with mock.patch.object(parallelism, "get_cpu_capacity", return_value=8):
        first = parallelism.compute_worker_count(
            candidate_count=20, reserve=1, ceiling=4,
        )
        second = parallelism.compute_worker_count(
            candidate_count=20, reserve=1, ceiling=4,
        )
    assert first == second
