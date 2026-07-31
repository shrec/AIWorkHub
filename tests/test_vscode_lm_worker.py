from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import vscode_lm_worker  # noqa: E402


def test_root_output_uses_declared_file_without_sibling_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("old\n", encoding="utf-8")
    original_mode = stat.S_IMODE(target.stat().st_mode)

    def unexpected_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("root output must not create a sibling temporary file")

    monkeypatch.setattr(vscode_lm_worker.tempfile, "mkstemp", unexpected_mkstemp)
    vscode_lm_worker._write_atomic(tmp_path, "AGENTS.md", "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == original_mode
    assert sorted(path.name for path in tmp_path.iterdir()) == ["AGENTS.md"]


def test_nested_output_keeps_atomic_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "docs" / "result.md"
    target.parent.mkdir()
    target.write_text("old\n", encoding="utf-8")
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def observed_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(vscode_lm_worker.os, "replace", observed_replace)
    vscode_lm_worker._write_atomic(tmp_path, "docs/result.md", "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert len(replacements) == 1
    assert replacements[0][0].parent == target.parent
    assert replacements[0][1] == target


def test_output_rejects_symlink_even_when_it_points_inside_workspace(tmp_path: Path) -> None:
    target = tmp_path / "real.md"
    target.write_text("real\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(target)

    with pytest.raises(RuntimeError, match="bridge_output_symlink:AGENTS.md"):
        vscode_lm_worker._write_atomic(tmp_path, "AGENTS.md", "forbidden\n")

    assert target.read_text(encoding="utf-8") == "real\n"
