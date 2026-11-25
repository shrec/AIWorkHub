"""NF-2026-02369: pytest-xdist ``-n`` is capped by observed cores and memory."""

from __future__ import annotations

import pytest

from aiworkhub import host_validation_stage, parallelism, platform_io
from aiworkhub import validation_runner as vr
from aiworkhub import worker_workspace as ww

GIB = 1 << 30
PY = ["python", "-m", "pytest", "-q"]


def _host(monkeypatch, cores: int, memory: int | None) -> None:
    monkeypatch.setattr(parallelism, "get_cpu_capacity", lambda: cores)
    monkeypatch.setattr(platform_io, "available_memory_bytes", lambda *a, **k: memory)


def test_headroom_matches_host_stage_and_budget_is_one_and_a_half_gib():
    assert vr.XDIST_HEADROOM_FRACTION == host_validation_stage.HEADROOM_FRACTION
    assert vr.XDIST_WORKER_MEMORY_BUDGET_BYTES == GIB * 3 // 2


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (["-n", "12"], ["-n", "12"][:1] + ["12"]),
        (["-n12"], ["-n12"]),
        (["-n=12"], ["-n=12"]),
        (["--numprocesses", "12"], ["--numprocesses", "12"]),
        (["--numprocesses=12"], ["--numprocesses=12"]),
    ],
)
def test_each_spelling_capped_to_core_bound(monkeypatch, tail, expected):
    # 16 cores, 25% headroom -> 12; ample memory. Request 12 stays 12 ...
    _host(monkeypatch, 16, 64 * GIB)
    assert ww._normalize_pytest_validation_argv(PY + tail) == PY + expected
    # ... while 20 is capped to 12 in the same spelling.
    big = [token.replace("12", "20") for token in tail]
    capped = [token.replace("20", "12") for token in big]
    assert ww._normalize_pytest_validation_argv(PY + big) == PY + capped


@pytest.mark.parametrize("value", ["auto", "logical"])
@pytest.mark.parametrize(
    "spelling", [lambda v: ["-n", v], lambda v: [f"-n{v}"], lambda v: [f"--numprocesses={v}"]]
)
def test_auto_and_logical_become_bound(monkeypatch, value, spelling):
    _host(monkeypatch, 8, None)
    out = ww._normalize_pytest_validation_argv(PY + spelling(value) + ["tests/x.py"])
    assert out == PY + spelling("6") + ["tests/x.py"]


def test_zero_absent_and_small_untouched(monkeypatch):
    _host(monkeypatch, 16, 64 * GIB)
    for argv in (
        PY + ["-n", "0", "tests/x.py"],
        PY + ["-n0"],
        PY + ["tests/x.py", "-k", "-n"],
        PY + ["-n", "3"],
        PY + ["--numprocesses=2"],
        ["ruff", "check", "-n", "99"],
    ):
        assert ww._normalize_pytest_validation_argv(argv) == argv
    assert "-n" not in ww._normalize_pytest_validation_argv(PY + ["tests/x.py"])


def test_console_script_spelling_is_also_capped(monkeypatch):
    _host(monkeypatch, 4, None)
    out = ww._normalize_pytest_validation_argv(["pytest", "-n", "16"])
    assert out[1:] == ["-m", "pytest", "-n", "3"]


def test_low_memory_host_caps_to_memory_bound(monkeypatch):
    # 16 cores but only 4 GiB free: 25% reserve leaves 3 GiB = 2 workers of 1.5 GiB.
    _host(monkeypatch, 16, 4 * GIB)
    assert ww._normalize_pytest_validation_argv(PY + ["-n", "12"]) == PY + ["-n", "2"]
    assert vr.bounded_xdist_workers() == 2
    # 370 MB free: still at least one worker, never zero.
    _host(monkeypatch, 16, 370 * 1024 * 1024)
    assert ww._normalize_pytest_validation_argv(PY + ["-nauto"]) == PY + ["-n1"]


def test_unmeasured_memory_falls_back_to_core_bound(monkeypatch):
    _host(monkeypatch, 16, None)
    assert vr.bounded_xdist_workers() == 12
    assert vr.bounded_xdist_workers(5) == 5
    assert vr.bounded_xdist_workers(40) == 12
    _host(monkeypatch, 1, None)
    assert vr.bounded_xdist_workers() == 1


def test_bound_is_sampled_at_call_time(monkeypatch):
    _host(monkeypatch, 16, 64 * GIB)
    assert vr.bounded_xdist_workers() == 12
    _host(monkeypatch, 16, 6 * GIB)
    assert vr.bounded_xdist_workers() == 3


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (["-qn20"], ["-qn12"]),
        (["-xn", "20"], ["-xn", "12"]),
        (["-vsn32"], ["-vsn12"]),
        (["-qnauto", "tests/x.py"], ["-qn12", "tests/x.py"]),
        (["-qn=20"], ["-qn=12"]),
    ],
)
def test_bundled_short_flags_are_capped(monkeypatch, tail, expected):
    _host(monkeypatch, 16, 64 * GIB)
    assert ww._normalize_pytest_validation_argv(PY + tail) == PY + expected


def test_bundled_zero_and_unrelated_bundles_untouched(monkeypatch):
    _host(monkeypatch, 16, 64 * GIB)
    for argv in (
        PY + ["-qn0"],
        PY + ["-xn", "0", "tests/x.py"],
        PY + ["-xvs", "tests/x.py"],
        PY + ["-qn4"],
    ):
        assert ww._normalize_pytest_validation_argv(argv) == argv


def test_xdist_env_equals_bound(monkeypatch):
    _host(monkeypatch, 16, 64 * GIB)
    assert vr.xdist_env() == {"PYTEST_XDIST_AUTO_NUM_WORKERS": "12"}
    _host(monkeypatch, 16, 4 * GIB)
    assert vr.xdist_env() == {
        "PYTEST_XDIST_AUTO_NUM_WORKERS": str(vr.bounded_xdist_workers())
    }
    assert vr.xdist_env({"PYTEST_XDIST_AUTO_NUM_WORKERS": "1"}) == {}


def test_validation_env_builders_carry_xdist_bound(monkeypatch):
    _host(monkeypatch, 8, None)
    child = vr.validation_child_env({"PATH": "x"})
    assert child["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "6"
    assert child["PATH"] == "x"
    kept = vr.validation_child_env({"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"})
    assert kept["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "2"
    safe = ww.sanitized_env("validation")
    assert safe["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "6"
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" not in ww.sanitized_env("codex")
