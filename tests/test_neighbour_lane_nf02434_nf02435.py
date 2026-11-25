"""NF-2026-02435 / NF-2026-02434: neighbour refusals name failures; width is memory-bound."""

from __future__ import annotations

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import parallelism, platform_io
from aiworkhub import validation_runner as vr

GIB = 1 << 30
ENV = "AIWORKHUB_XDIST_WORKER_MEMORY_BYTES"


def _host(monkeypatch: pytest.MonkeyPatch, cores: int, memory: int | None) -> None:
    monkeypatch.setattr(neighbours.os, "cpu_count", lambda: cores)
    monkeypatch.setattr(parallelism, "get_cpu_capacity", lambda: cores)
    monkeypatch.setattr(platform_io, "available_memory_bytes", lambda *a, **k: memory)
    monkeypatch.delenv(ENV, raising=False)


def _files(count: int) -> list[str]:
    return [f"tests/test_n{index:02d}.py" for index in range(count)]


# --- NF-2026-02435: failed names come from the full-output digest -------------------------


def _digest(*names: str) -> dict:
    return {
        "schema": vr.PYTEST_FAILURE_DIGEST_SCHEMA,
        "failures": [{"test": name, "message": "boom"} for name in names],
        "totals": f"{len(names)} failed",
        "omitted": 0,
    }


def test_digest_only_row_names_the_failed_tests() -> None:
    row = {
        "returncode": 1,
        "stdout_tail": "...lots of progress output with no summary lines...",
        "pytest_failure_digest": _digest("tests/a.py::t1", "tests/b.py::t2", "tests/a.py::t1"),
    }
    failed, first = neighbours._failed_tests([row])
    assert failed == ["tests/a.py::t1", "tests/b.py::t2"]
    assert first.startswith("FAILED tests/a.py::t1")


def test_digest_names_keep_order_and_dedup_across_rows() -> None:
    rows = [
        {"pytest_failure_digest": _digest("tests/z.py::z", "tests/a.py::a")},
        {"pytest_failure_digest": _digest("tests/a.py::a", "tests/m.py::m")},
    ]
    failed, _first = neighbours._failed_tests(rows)
    assert failed == ["tests/z.py::z", "tests/a.py::a", "tests/m.py::m"]


def test_regex_remains_the_fallback_without_a_usable_digest() -> None:
    rows = [
        {"stdout_tail": "FAILED tests/a.py::t - boom\n"},
        {"stdout_tail": "FAILED tests/b.py::u\n", "pytest_failure_digest": None},
        {"stdout_tail": "FAILED tests/c.py::v\n", "pytest_failure_digest": {"failures": []}},
        {"stdout_tail": "ERROR tests/d.py::w\n", "pytest_failure_digest": {"failures": "x"}},
    ]
    failed, first = neighbours._failed_tests(rows)
    assert failed == ["tests/a.py::t", "tests/b.py::u", "tests/c.py::v", "tests/d.py::w"]
    assert first == "FAILED tests/a.py::t - boom"


def test_refusal_names_digest_failures() -> None:
    rows = [{"returncode": 1, "stdout_tail": "x", "pytest_failure_digest": _digest("tests/a.py::t")}]
    text = str(neighbours._refusal(["tests/a.py"], rows, ""))
    assert "tests/a.py::t" in text


def test_digest_branch_honours_the_limit() -> None:
    count = neighbours.MAX_REPORTED_FAILURES + 5
    names = [f"tests/a.py::t{index:03d}" for index in range(count)]
    failed, _first = neighbours._failed_tests([{"pytest_failure_digest": _digest(*names)}])
    assert failed == names[: neighbours.MAX_REPORTED_FAILURES]
    failed, _first = neighbours._failed_tests([{"pytest_failure_digest": _digest(*names)}], limit=2)
    assert failed == names[:2]


def test_digest_error_entry_keeps_kind_and_message() -> None:
    digest = {
        "failures": [{"kind": "error", "test": "tests/a.py::t", "message": "ImportError: x"}],
        "omitted": 0,
    }
    failed, first = neighbours._failed_tests([{"pytest_failure_digest": digest}])
    assert failed == ["tests/a.py::t"]
    assert first == "ERROR tests/a.py::t - ImportError: x"


def test_omitted_digest_also_scans_the_tail_for_extra_names() -> None:
    digest = _digest("tests/a.py::t1")
    digest["omitted"] = 3
    row = {
        "pytest_failure_digest": digest,
        "stdout_tail": "FAILED tests/a.py::t1 - boom\nFAILED tests/z.py::tail_only - bang\n",
    }
    failed, first = neighbours._failed_tests([row])
    assert failed == ["tests/a.py::t1", "tests/z.py::tail_only"]
    assert first.startswith("FAILED tests/a.py::t1")
    capped, _first = neighbours._failed_tests([row], limit=1)
    assert capped == ["tests/a.py::t1"]


def test_neighbour_command_carries_rfe_and_one_width() -> None:
    tests = _files(3)
    for serial in (False, True):
        command = neighbours.neighbour_command(tests, serial=serial)
        assert " -rfE " in command
        assert command.count(" -n ") == 1
        assert command.endswith(" -q")


# --- NF-2026-02434: width is bounded by memory --------------------------------------------


def test_budget_default_is_one_and_a_half_gib() -> None:
    assert vr.XDIST_WORKER_MEMORY_BUDGET_BYTES == 3 * GIB // 2
    assert vr.XDIST_MEMORY_RESERVE_FRACTION == 0.25


def test_width_is_capped_by_low_available_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # 16 cores and 12 files would be -n 12; 6 GiB free leaves 4.5 GiB = 3 workers.
    _host(monkeypatch, 16, 6 * GIB)
    tests = _files(12)
    assert neighbours.neighbour_width(tests) == 3
    assert " -n 3 " in neighbours.neighbour_command(tests)
    _host(monkeypatch, 16, 64 * GIB)
    assert neighbours.neighbour_width(tests) == 12


def test_width_never_below_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    _host(monkeypatch, 16, 100 * 1024 * 1024)
    assert vr.bounded_xdist_workers() == 1
    assert neighbours.neighbour_width(_files(12)) == 1


@pytest.mark.parametrize(
    ("cores", "memory", "files"),
    [(16, 6 * GIB, 12), (16, None, 20), (4, 64 * GIB, 12), (8, 3 * GIB, 5), (2, 64 * GIB, 12)],
)
def test_width_never_exceeds_bounded_xdist_workers(
    monkeypatch: pytest.MonkeyPatch, cores: int, memory: int | None, files: int
) -> None:
    _host(monkeypatch, cores, memory)
    assert neighbours.neighbour_width(_files(files)) <= vr.bounded_xdist_workers()


def test_memory_reserve_is_subtracted_before_dividing(monkeypatch: pytest.MonkeyPatch) -> None:
    # 6 GiB / 1.5 GiB would be 4 workers; the 25% reserve leaves 4.5 GiB = 3.
    _host(monkeypatch, 16, 6 * GIB)
    assert vr.bounded_xdist_workers() == 3
    # Exactly on the boundary: 6 GiB usable needs 8 GiB available.
    _host(monkeypatch, 16, 8 * GIB)
    assert vr.bounded_xdist_workers() == 4
    _host(monkeypatch, 16, 8 * GIB - 1)
    assert vr.bounded_xdist_workers() == 3


def test_env_override_sets_the_per_worker_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _host(monkeypatch, 16, 4 * GIB)
    assert vr.bounded_xdist_workers() == 2
    monkeypatch.setenv(ENV, str(GIB))
    assert vr.xdist_worker_memory_budget_bytes() == GIB
    assert vr.bounded_xdist_workers() == 3
    monkeypatch.setenv(ENV, str(3 * GIB))
    assert vr.bounded_xdist_workers() == 1
    assert neighbours.neighbour_width(_files(12)) == 1


@pytest.mark.parametrize("value", ["", "abc", "0", "-5", "1.5", " "])
def test_invalid_env_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _host(monkeypatch, 16, 4 * GIB)
    monkeypatch.setenv(ENV, value)
    assert vr.xdist_worker_memory_budget_bytes() == vr.XDIST_WORKER_MEMORY_BUDGET_BYTES
    assert vr.bounded_xdist_workers() == 2
