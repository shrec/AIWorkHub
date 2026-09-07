"""NF-2026-00639: the suite must run distributed, and CI must never pass ``-n``
at a job that has not installed pytest-xdist.

The measured defect was that ``[tool.pytest.ini_options]`` declared no
``addopts`` and CI invoked pytest with no ``-n`` while pytest-xdist sat
installed and unused: a full serial run of 763.36 s where the same tree runs in
149.21 s distributed on the same host.

Two contracts are asserted here because breaking either one is silent:

* the worker count is DERIVED from the observed core count, with headroom --
  the repository rule forbids a pinned worker constant;
* every CI step that installs pytest also installs pytest-xdist. ``addopts``
  is global, so the moment ``-n`` lives in ``pyproject.toml`` a job without
  xdist fails on ``unrecognized arguments: -n`` before it runs a single test.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

import conftest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _addopts() -> list[str]:
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    raw = data["tool"]["pytest"]["ini_options"].get("addopts", "")
    return shlex.split(raw if isinstance(raw, str) else " ".join(raw))


def test_addopts_distributes_the_suite_without_pinning_a_worker_count() -> None:
    opts = _addopts()
    assert "-n" in opts, f"the suite must run distributed by default: addopts={opts}"
    value = opts[opts.index("-n") + 1]
    assert value == "auto", (
        f"the worker count must be derived at run time, not pinned: -n {value}"
    )


def test_distribution_keeps_a_test_file_on_one_worker() -> None:
    # loadfile is what makes the parallel run equivalent to the serial one for
    # the tests here that monkeypatch module-level state or drive daemons and
    # child processes: they interfere within a file, which loadfile keeps
    # together on one worker in file order.
    opts = _addopts()
    assert "--dist" in opts, f"the distribution mode must be explicit: addopts={opts}"
    assert opts[opts.index("--dist") + 1] == "loadfile"


def test_worker_count_is_derived_from_the_observed_cores_and_leaves_headroom(
    monkeypatch,
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    seen: list[int] = []
    for cores, expected in ((1, 1), (2, 2), (3, 3), (4, 3), (8, 7), (16, 14), (64, 56)):
        monkeypatch.setattr(conftest, "observed_cores", lambda cores=cores: cores)
        workers = conftest.pytest_xdist_auto_num_workers(None)
        assert workers == expected, f"{cores} cores -> {workers}, expected {expected}"
        seen.append(workers)
    # Derived, not constant: the same function must answer differently for
    # different hosts, and must never claim more workers than there are cores.
    assert len(set(seen)) > 1, "a worker count that never changes is a pinned constant"
    for cores, workers in zip((1, 2, 3, 4, 8, 16, 64), seen, strict=True):
        assert workers <= cores, f"{workers} workers on {cores} cores oversubscribes"
    for cores, workers in ((4, 3), (8, 7), (16, 14), (64, 56)):
        assert workers < cores, (
            f"{cores} cores must reserve headroom for the interactive MCP server"
        )


def test_an_explicit_operator_override_is_deferred_to_xdist(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "3")
    assert conftest.pytest_xdist_auto_num_workers(None) is None


def test_observed_cores_is_positive_and_matches_this_host() -> None:
    import os

    cores = conftest.observed_cores()
    assert cores >= 1
    if hasattr(os, "sched_getaffinity"):
        assert cores == len(os.sched_getaffinity(0))


def test_every_ci_step_that_installs_pytest_also_installs_xdist() -> None:
    # addopts is global, so a job that installs pytest without xdist dies on
    # `unrecognized arguments: -n` before running a test. This is the ordering
    # the NeedFix required: xdist in CI BEFORE anything passes -n.
    if "-n" not in _addopts():
        return
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    installs = [
        line for line in lines
        if "pip install" in line and "pytest" in line and not line.lstrip().startswith("#")
    ]
    assert installs, "no pytest install step found in the CI workflow"
    for line in installs:
        assert "pytest-xdist" in line, (
            f"CI installs pytest without pytest-xdist while addopts passes -n: {line.strip()}"
        )


def test_a_deselected_file_is_still_run_somewhere_in_ci() -> None:
    # Scoping parallelism around a file must never quietly drop its coverage:
    # anything deselected from the distributed pass must have its own serial
    # invocation in the same job.
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    runs = [
        line.strip() for line in lines
        if "python -m pytest" in line
        and "--collect-only" not in line
        and not line.lstrip().startswith("#")
    ]
    deselected = {
        part for line in runs
        for flag, part in zip(shlex.split(line), shlex.split(line)[1:])
        if flag == "--deselect"
    }
    for target in deselected:
        assert (REPO_ROOT / target).exists(), f"--deselect names a path that does not exist: {target}"
        serial = [
            line for line in runs
            if target in line and "--deselect" not in line and " -n 0 " in line
        ]
        assert serial, (
            f"{target} is deselected from the distributed run but never run serially: "
            "its coverage would be silently lost"
        )


def test_platform_qualification_manifests_stay_serial() -> None:
    # The measured win is the full suite and the local loop. The platform-owned
    # lock/process/temp manifests are fast serially and are the last place to
    # introduce a scheduling variable that cannot be reproduced off the runner.
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    runs = [
        line for line in lines
        if "python -m pytest" in line
        and "--collect-only" not in line
        and not line.lstrip().startswith("#")
    ]
    assert runs, "no pytest invocation found in the CI workflow"
    qualification = [line for line in runs if "junitxml=qualification" in line]
    assert len(qualification) == 3, f"expected 3 qualification runs, found {len(qualification)}"
    for line in qualification:
        assert " -n 0 " in line, f"platform qualification must stay serial: {line.strip()}"
