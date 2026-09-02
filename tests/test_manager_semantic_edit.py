"""The manager gets the same verified editor a worker gets.

``semantic_edit_prepare``/``semantic_edit_apply`` exist only on the worker MCP
surface, so the manager -- whose charter is "small precise corrections" -- was
the one role without the instrument that makes a small correction verifiable,
and fell back to whole-string rewrites with no hash binding at all.

The applier layer is now one definition (``semantic_edit_applier``) with two
callers: the worker session and a manager CLI. These tests pin what both get --
the range and only the range changes, a file that moved underneath is refused
rather than overwritten, and the file's mode survives the atomic swap.

Run: python3 -m pytest -q tests/test_manager_semantic_edit.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from aiworkhub import semantic_edit, semantic_edit_applier  # noqa: E402

CLI = _ROOT / "scripts" / "manager_semantic_edit.py"


def _repo(tmp_path: Path, text: str = "one\ntwo\nthree\nfour\nfive\n") -> Path:
    (tmp_path / "x.py").write_text(text, encoding="utf-8")
    return tmp_path


def _prepare(root: Path, start: int, end: int):
    return semantic_edit.prepare_line_target(
        root, path="x.py", start_line=start, end_line=end, allowed_writes=["x.py"]
    )


def test_only_the_named_range_changes(tmp_path):
    root = _repo(tmp_path)
    target = _prepare(root, 2, 3)
    semantic_edit_applier.replace_prepared_range(
        root, target, "TWO\nTHREE\n", allowed_writes=["x.py"]
    )
    assert (root / "x.py").read_text(encoding="utf-8") == "one\nTWO\nTHREE\nfour\nfive\n"


def test_a_file_that_moved_underneath_the_edit_is_refused(tmp_path):
    """Prepared, then the file changes: the write must not land."""
    root = _repo(tmp_path)
    target = _prepare(root, 2, 3)
    (root / "x.py").write_text("one\ntwo\nthree\nfour\nfive\nsix\n", encoding="utf-8")

    with pytest.raises(semantic_edit.SemanticEditError, match="semantic_edit_stale"):
        semantic_edit_applier.replace_prepared_range(
            root, target, "TWO\n", allowed_writes=["x.py"]
        )
    assert (root / "x.py").read_text(encoding="utf-8").endswith("six\n"), (
        "a refused edit must leave the file exactly as it found it"
    )


def test_an_executable_file_keeps_its_mode(tmp_path):
    """mkstemp makes 0600 and os.replace carries it; the mode must survive."""
    root = _repo(tmp_path, "#!/bin/sh\necho one\necho two\n")
    os.chmod(root / "x.py", 0o755)
    target = _prepare(root, 2, 2)
    semantic_edit_applier.replace_prepared_range(
        root, target, "echo ONE\n", allowed_writes=["x.py"]
    )
    assert os.stat(root / "x.py").st_mode & 0o777 == 0o755


def test_a_path_outside_allowed_writes_is_refused(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditError):
        semantic_edit.prepare_line_target(
            root, path="x.py", start_line=1, end_line=1, allowed_writes=["other.py"]
        )


def test_the_cli_emits_a_receipt_and_never_reemits_the_file(tmp_path):
    root = _repo(tmp_path, "\n".join(f"line{n}" for n in range(1, 200)) + "\n")
    result = subprocess.run(
        [sys.executable, str(CLI), "--repo", str(root), "--path", "x.py",
         "--start", "10", "--end", "11"],
        input="LINE10\nLINE11\n", capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["ok"] is True
    assert receipt["preimage_verified"] is True
    assert receipt["model_reemitted_old_bytes"] == 0
    assert receipt["whole_file_output_required"] is False
    # The point of the instrument: bytes emitted are the range, not the file.
    assert receipt["replacement_bytes"] < receipt["file_bytes"] / 10

    lines = (root / "x.py").read_text(encoding="utf-8").splitlines()
    assert lines[9] == "LINE10" and lines[10] == "LINE11"
    assert lines[8] == "line9" and lines[11] == "line12"


def test_the_cli_fails_closed_with_a_reason(tmp_path):
    root = _repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(CLI), "--repo", str(root), "--path", "x.py",
         "--start", "99", "--end", "99"],
        input="nope\n", capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["ok"] is False
    assert (root / "x.py").read_text(encoding="utf-8") == "one\ntwo\nthree\nfour\nfive\n"
