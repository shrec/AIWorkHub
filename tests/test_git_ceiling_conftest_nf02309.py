"""NF-2026-02309: pytest fixture repos must not leak git registrations.

The session-scoped ``git_ceiling_at_basetemp`` fixture in ``tests/conftest.py``
puts the pytest basetemp into ``GIT_CEILING_DIRECTORIES``. These tests pin
both halves: the reproduction (a plain directory under tmp_path is not inside
any git repo) and the regression (the ceiling is actually set to the basetemp).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def test_plain_fixture_dir_does_not_discover_canonical_repo(tmp_path: Path) -> None:
    plain = tmp_path / "repo"
    plain.mkdir()

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=plain,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    # The ceiling stops discovery at the basetemp, so a plain directory under it
    # belongs to no repository at all -- whichever repo would have caught the leak.
    assert result.returncode != 0, (
        f"git discovery from {plain} escaped the basetemp: {result.stdout.strip()}"
    )


def test_git_ceiling_directories_contains_basetemp(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    basetemp = str(tmp_path_factory.getbasetemp().resolve())
    ceiling = os.environ.get("GIT_CEILING_DIRECTORIES", "")

    assert basetemp in ceiling.split(os.pathsep), (
        f"GIT_CEILING_DIRECTORIES={ceiling!r} does not contain basetemp {basetemp!r}"
    )
