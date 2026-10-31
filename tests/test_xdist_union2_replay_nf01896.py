"""NF-2026-01896: a union2 sibling-worktree path is mapped onto the workspace root."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher_validation as validation
from aiworkhub import validation_runner

_PYTEST = "python -m pytest -q tests/test_a.py -p no:cacheprovider"
_UNION2 = "Z:\\union2_abc_0123456789abcdef\\worktree\\"
_TAIL = "src\\aiworkhub\\worktree_storage.py"


def _workspace(tmp_path):
    root = tmp_path / "worktree"
    (root / "src" / "aiworkhub").mkdir(parents=True)
    (root / "src" / "aiworkhub" / "worktree_storage.py").write_text("x = 1\n", "utf-8")
    return SimpleNamespace(request_id="req-nf01896", path=root, repo=tmp_path / "repo")


def _not_found(path: str) -> str:
    doubled = path.replace("\\", "\\\\")
    return "\n".join(
        (
            "[gw2] win32 -- Python 3.12.7",
            "INTERNALERROR> FileNotFoundError: [Errno 2] No such file or directory: "
            f"'{doubled}'",
        )
    )


def test_union2_path_is_confirmed_and_replayed(tmp_path) -> None:
    ws = _workspace(tmp_path)
    recorded = _UNION2 + _TAIL
    assert validation._workspace_path_exists(ws, recorded) is True
    row = {
        "command": _PYTEST,
        "declared_command": _PYTEST,
        "returncode": 1,
        "timed_out": False,
        "stdout_tail": _not_found(recorded),
        "stderr_tail": "",
    }
    assert (
        validation_runner.xdist_collection_replay_signature(
            _PYTEST,
            row,
            path_exists=lambda value: validation._workspace_path_exists(ws, value),
        )
        == validation_runner.XDIST_WORKER_FILE_NOT_FOUND
    )


@pytest.mark.parametrize(
    "recorded",
    [
        _UNION2 + "src\\aiworkhub\\missing.py",
        _UNION2 + "..\\x",
        _UNION2 + "src\\..\\..\\x",
        _UNION2.rstrip("\\"),
        "Z:\\union2_abc_0123456789abcdef\\other\\" + _TAIL,
        "Z:\\elsewhere\\" + _TAIL,
        "Z:\\elsewhere\\worktree\\" + _TAIL,
        _UNION2 + "C:foo\\" + _TAIL,
        _UNION2 + "C:\\" + _TAIL,
    ],
)
def test_union2_negatives_are_refused(tmp_path, recorded: str) -> None:
    assert validation._workspace_path_exists(_workspace(tmp_path), recorded) is False
