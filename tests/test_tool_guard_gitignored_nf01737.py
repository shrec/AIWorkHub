"""Tool guard lets Read through for files git ignores (NF-2026-01737)."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "aiworkhub" / "tool_guard_hook.py"
)


def _load_hook():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "_tool_guard_hook_gitignored_under_test", _HOOK_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def _make_repo(base: Path) -> Path:
    root = base / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (root / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    (root / "plain.py").write_text("y = 2\n", encoding="utf-8")
    (root / "ignored.md").write_text("# ignored\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "tracked.py", ".gitignore"], check=True
    )
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return root


def test_gitignored_md_is_not_indexed(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    ctx = hook._Context(root, root, "bash")
    assert ctx.indexed_file(str(root / "ignored.md")) is None


def test_tracked_and_untracked_py_stay_indexed(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    ctx = hook._Context(root, root, "bash")
    assert ctx.indexed_file(str(root / "tracked.py")) == "tracked.py"
    assert ctx.indexed_file(str(root / "plain.py")) == "plain.py"


def test_git_failure_fails_closed_to_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_repo(tmp_path)
    ctx = hook._Context(root, root, "bash")

    def _boom(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("git missing")

    monkeypatch.setattr(hook.subprocess, "run", _boom)
    assert ctx.indexed_file(str(root / "tracked.py")) == "tracked.py"
    assert ctx.indexed_file(str(root / "ignored.md")) == "ignored.md"
