"""NF-2026-01970: the executable bit survives semantic edit apply and promotion.

``replace_prepared_range`` writes through a 0600 ``mkstemp`` file; a chmod
failure used to be swallowed, silently turning a 0755 script into a 0600 file
that git records as 100644.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import semantic_edit, semantic_edit_applier, worker_workspace  # noqa: E402

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits only")

_SCRIPT = "tools/run.sh"


def _edit_line_two(root: Path, relative: str = _SCRIPT) -> dict:
    target = semantic_edit.prepare_line_target(
        root, path=relative, start_line=2, end_line=2, allowed_writes=[relative]
    )
    _text, metrics = semantic_edit_applier.replace_prepared_range(
        root, target, "echo edited\n", allowed_writes=[relative]
    )
    return metrics


def _write_script(root: Path, mode: int) -> Path:
    path = root / _SCRIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\necho original\n")
    os.chmod(path, mode)
    return path


@posix_only
def test_semantic_edit_keeps_executable_mode(tmp_path: Path) -> None:
    path = _write_script(tmp_path, 0o755)

    metrics = _edit_line_two(tmp_path)

    assert path.read_text() == "#!/bin/sh\necho edited\n"
    assert path.stat().st_mode & 0o7777 == 0o755
    assert metrics["mode_restored"] is True
    assert metrics["original_mode"] == 0o755


@posix_only
def test_semantic_edit_exec_bit_survives_path_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_script(tmp_path, 0o755)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "chmod forbidden")

    monkeypatch.setattr(os, "chmod", refuse)

    metrics = _edit_line_two(tmp_path)

    assert path.stat().st_mode & 0o7777 == 0o755
    assert metrics["mode_restored"] is True


def test_chmod_failure_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_script(tmp_path, 0o755 if os.name != "nt" else 0o666)
    original_mode = path.stat().st_mode & 0o7777

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "chmod forbidden")

    monkeypatch.setattr(os, "chmod", refuse)
    monkeypatch.setattr(os, "fchmod", refuse, raising=False)

    metrics = _edit_line_two(tmp_path)

    assert path.read_text() == "#!/bin/sh\necho edited\n"
    assert metrics["mode_restored"] is False
    assert metrics["original_mode"] == original_mode


@posix_only
def test_promoted_candidate_keeps_exec_bit_in_git_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "parent"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "NF01970 Test"], cwd=repo, check=True)
    _write_script(repo, 0o755)
    subprocess.run(["git", "add", _SCRIPT], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo, "nf01970-exec", {"allowed_writes": [_SCRIPT], "read_first": []}, "validation"
    )
    _edit_line_two(workspace.path)
    assert (workspace.path / _SCRIPT).stat().st_mode & 0o7777 == 0o755

    assert worker_workspace.promote(workspace, [_SCRIPT]) == [_SCRIPT]

    assert (repo / _SCRIPT).read_text() == "#!/bin/sh\necho edited\n"
    assert (repo / _SCRIPT).stat().st_mode & 0o777 == 0o755
    subprocess.run(["git", "add", _SCRIPT], cwd=repo, check=True)
    staged = subprocess.run(
        ["git", "ls-files", "-s", _SCRIPT], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert staged.startswith("100755 ")
