"""NF-2026-02365: a semantic edit never changes the edited file's mode.

Field defect: where the sandbox forbids chmod, every apply turned a 0644 file
into 0600 -- ``mkstemp`` created the temp 0600, both chmod attempts swallowed
their failure and ``os.replace`` installed the temp anyway. The temp is now
created with the original mode; a temp whose bits still differ is never
installed (the bytes are written in place instead), and when neither path can
keep the mode the apply is refused by name with the original bytes untouched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import semantic_edit, semantic_edit_applier  # noqa: E402

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX umask and mode bits only")

_RELATIVE = "pkg/module.txt"
_ORIGINAL = b"first\nsecond\nthird\n"
_EXPECTED = "first\nSECOND\nthird\n"


def _write_fixture(root: Path, mode: int) -> Path:
    path = root / _RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_ORIGINAL)
    os.chmod(path, mode)
    return path


def _edit_line_two(root: Path) -> dict:
    target = semantic_edit.prepare_line_target(
        root, path=_RELATIVE, start_line=2, end_line=2, allowed_writes=[_RELATIVE]
    )
    _text, metrics = semantic_edit_applier.replace_prepared_range(
        root, target, "SECOND\n", allowed_writes=[_RELATIVE]
    )
    return metrics


def _forbid_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "chmod forbidden by sandbox policy")

    monkeypatch.setattr(os, "chmod", refuse)
    monkeypatch.setattr(os, "fchmod", refuse, raising=False)


def _force_temp_mode_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the temp report foreign permission bits; the original stays honest."""

    temp_inodes: set[int] = set()
    real_create = semantic_edit_applier._create_temp_with_mode
    real_matches = semantic_edit_applier._fd_mode_matches

    def create(file_path: Path, mode: int) -> tuple[Path, int]:
        temp_path, fd = real_create(file_path, mode)
        temp_inodes.add(os.fstat(fd).st_ino)
        return temp_path, fd

    def matches(fd: int, mode: int) -> bool:
        if os.fstat(fd).st_ino in temp_inodes:
            return False
        return real_matches(fd, mode)

    monkeypatch.setattr(semantic_edit_applier, "_create_temp_with_mode", create)
    monkeypatch.setattr(semantic_edit_applier, "_fd_mode_matches", matches)


@pytest.mark.parametrize("mode", [0o644, 0o755])
def test_mode_survives_where_chmod_and_fchmod_are_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    path = _write_fixture(tmp_path, mode)
    # Windows has only the read-only bit; the full st_mode is compared so the
    # same assertion holds on both platforms.
    before = os.stat(path).st_mode
    if os.name != "nt":
        assert before & 0o7777 == mode
    _forbid_chmod(monkeypatch)

    metrics = _edit_line_two(tmp_path)

    assert path.read_text(encoding="utf-8") == _EXPECTED
    assert os.stat(path).st_mode == before
    assert metrics["mode_restored"] is True
    assert sorted(os.listdir(path.parent)) == [path.name]


def test_temp_with_foreign_bits_is_written_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_fixture(tmp_path, 0o644)
    before = os.stat(path)
    _forbid_chmod(monkeypatch)
    _force_temp_mode_mismatch(monkeypatch)

    metrics = _edit_line_two(tmp_path)

    after = os.stat(path)
    assert path.read_text(encoding="utf-8") == _EXPECTED
    assert after.st_ino == before.st_ino
    assert after.st_mode == before.st_mode
    assert metrics["mode_write_path"] == "in_place"
    assert metrics["mode_restored"] is True
    assert sorted(os.listdir(path.parent)) == [path.name]


@posix_only
def test_a_narrowing_umask_with_forbidden_fchmod_keeps_the_inode_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_fixture(tmp_path, 0o644)
    before = os.stat(path)
    _forbid_chmod(monkeypatch)
    previous_umask = os.umask(0o077)
    try:
        metrics = _edit_line_two(tmp_path)
    finally:
        os.umask(previous_umask)

    after = os.stat(path)
    assert path.read_text(encoding="utf-8") == _EXPECTED
    assert after.st_mode & 0o7777 == 0o644
    assert after.st_ino == before.st_ino
    assert metrics["mode_write_path"] == "in_place"
    assert sorted(os.listdir(path.parent)) == [path.name]


def test_refused_by_name_when_in_place_write_is_impossible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_fixture(tmp_path, 0o644)
    before = os.stat(path).st_mode
    _forbid_chmod(monkeypatch)
    _force_temp_mode_mismatch(monkeypatch)

    def cannot_write(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "in-place write forbidden")

    monkeypatch.setattr(semantic_edit_applier, "_write_in_place", cannot_write)

    with pytest.raises(semantic_edit.SemanticEditError) as raised:
        _edit_line_two(tmp_path)

    assert str(raised.value) == f"semantic_edit_mode_not_preserved:{_RELATIVE}"
    assert path.read_bytes() == _ORIGINAL
    assert os.stat(path).st_mode == before
    assert sorted(os.listdir(path.parent)) == [path.name]


def test_refused_when_no_descriptor_carries_the_original_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_fixture(tmp_path, 0o644)
    before = os.stat(path).st_mode
    _forbid_chmod(monkeypatch)
    monkeypatch.setattr(semantic_edit_applier, "_fd_mode_matches", lambda _fd, _mode: False)

    with pytest.raises(semantic_edit.SemanticEditError) as raised:
        _edit_line_two(tmp_path)

    assert str(raised.value) == f"semantic_edit_mode_not_preserved:{_RELATIVE}"
    assert path.read_bytes() == _ORIGINAL
    assert os.stat(path).st_mode == before
    assert sorted(os.listdir(path.parent)) == [path.name]
