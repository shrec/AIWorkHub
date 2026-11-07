"""NF-2026-02163: git check-ignore must not answer for a repo above the guarded root.

The guarded root is not a repository, but it sits inside an outer repository whose
.gitignore ignores it. Without GIT_CEILING_DIRECTORIES, git walks up and reports the
outer ignore rule, so the guard wrongly treats the root's own files as ignored.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parents[1] / "src" / "aiworkhub" / "tool_guard_hook.py"


def _load_hook():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "_tool_guard_hook_git_ceiling_under_test", _HOOK_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def test_outer_ignoring_repo_does_not_exempt_non_repo_root(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    subprocess.run(["git", "init", "-q", str(outer)], check=True, capture_output=True)
    (outer / ".gitignore").write_text("cells/\n", encoding="utf-8")
    root = outer / "cells" / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".aiworkhub").mkdir()  # decide() allows everything for a root without .aiworkhub/
    (root / "src" / "x.py").write_text("print(1)\n", encoding="utf-8")

    # Fixture proof: without the ceiling, git reports the file ignored via the outer repo.
    probe = subprocess.run(
        ["git", "-C", str(root / "src"), "check-ignore", "-q", "--", "x.py"],
        capture_output=True,
    )
    assert probe.returncode == 0

    assert hook.decide("Bash", {"command": "grep -rn foo src"}, root) is not None
    assert hook._git_ignored(root / "src" / "x.py", root) is False
