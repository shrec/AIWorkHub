"""NF-2026-01693: ``python -m aiworkhub.*`` recipes run under the server interpreter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import recipe_runner as rr
from aiworkhub import tool_recipes as tr


def _recipe(*argv_tail: str) -> tr.Recipe:
    return tr.Recipe(
        id="fixture.interpreter",
        version="1.0.0",
        purpose="Interpreter resolution fixture",
        task_kind=tr.TaskKind.SCAN,
        argv=(tr.lit("python"), *(tr.lit(part) for part in argv_tail)),
        cache_policy=tr.CachePolicy.NEVER,
    )


@pytest.fixture
def foreign_repo(tmp_path, monkeypatch):
    """A repo whose own .venv interpreter exists but cannot import aiworkhub."""
    interpreter = tmp_path / Path(*rr._canonical_interpreter_relative().parts)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        rr, "_canonical_interpreter", lambda root: interpreter if root == tmp_path.resolve() else None
    )
    return tmp_path.resolve(), interpreter


@pytest.fixture
def spawned(monkeypatch):
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "env": dict(kwargs["env"])})
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(rr.subprocess, "run", fake_run)
    return calls


def test_aiworkhub_module_recipe_runs_under_server_interpreter(foreign_repo, spawned):
    repo, interpreter = foreign_repo
    digest = rr.run_recipe(
        repo, _recipe("-m", "aiworkhub.recipes.worktree_diff"), persist_receipt=False
    )
    package_root = Path(rr.__file__).resolve().parent.parent
    assert spawned[0]["argv"][0] == str(Path(sys.executable))
    assert spawned[0]["argv"][0] != str(interpreter)
    assert spawned[0]["argv"][1:] == ["-m", "aiworkhub.recipes.worktree_diff"]
    assert spawned[0]["env"]["PYTHONPATH"] == str(package_root)
    assert (package_root / "aiworkhub" / "__init__.py").is_file()
    assert digest["executable"] == str(Path(sys.executable))


def test_other_python_recipe_keeps_repository_interpreter(foreign_repo, spawned):
    repo, interpreter = foreign_repo
    digest = rr.run_recipe(repo, _recipe("-c", "pass"), persist_receipt=False)
    assert spawned[0]["argv"][0] == str(interpreter)
    assert "PYTHONPATH" not in spawned[0]["env"]
    assert digest["executable"] == str(interpreter)


def test_non_aiworkhub_module_recipe_keeps_repository_interpreter(foreign_repo, spawned):
    repo, interpreter = foreign_repo
    rr.run_recipe(repo, _recipe("-m", "pytest"), persist_receipt=False)
    rr.run_recipe(repo, _recipe("-m", "aiworkhubx.thing"), persist_receipt=False)
    assert [call["argv"][0] for call in spawned] == [str(interpreter)] * 2
    assert all("PYTHONPATH" not in call["env"] for call in spawned)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("python", "-m", "aiworkhub"), True),
        (("python", "-m", "aiworkhub.recipes.x", "--a"), True),
        (("python", "-m", "aiworkhubx"), False),
        (("python", "-c", "aiworkhub.x"), False),
        (("python", "aiworkhub.x"), False),
        (("python3", "-m", "aiworkhub.x"), False),
        (("python", "-m"), False),
    ],
)
def test_module_launch_predicate(argv, expected):
    assert rr._is_aiworkhub_module_launch(argv) is expected
