"""NF-2026-01823: an AIWORKHUB_METRIC line reaches stdout exactly once, with or without xdist."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).with_name("conftest.py")
_SRC = Path(__file__).resolve().parents[1] / "src"
_METRIC = 'AIWORKHUB_METRIC: {"name": "relay_probe", "value": 42}'

_GENERATED_TESTS = f"""
def test_records_metric(aiworkhub_metric):
    aiworkhub_metric({_METRIC!r})


def test_records_nothing():
    assert True


def test_also_runs_elsewhere():
    assert True
"""


def _run(tmp_path: Path, workers: int) -> subprocess.CompletedProcess[str]:
    project = tmp_path / f"project_n{workers}"
    project.mkdir()
    (project / "conftest.py").write_text(_CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "test_generated_metric.py").write_text(_GENERATED_TESTS, encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    pythonpath = [str(_SRC)] if _SRC.is_dir() else []
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    if workers:
        command += ["-n", str(workers)]
    return subprocess.run(
        command + ["-c", str(project / "pytest.ini"), "--rootdir", str(project), str(project)],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _metric_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("AIWORKHUB_METRIC:")]


@pytest.mark.parametrize("workers", [0, 2])
def test_metric_line_reaches_stdout_exactly_once(tmp_path: Path, workers: int) -> None:
    if workers and importlib.util.find_spec("xdist") is None:
        pytest.skip("pytest-xdist is not installed")
    result = _run(tmp_path, workers)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _metric_lines(result.stdout) == [_METRIC], result.stdout
