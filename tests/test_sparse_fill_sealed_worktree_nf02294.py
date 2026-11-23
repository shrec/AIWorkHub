"""NF-2026-02294: the sparse toolchain fill never outlives the declared checks.

``run_declared_checks`` fills canonical scripts/src/tests files into the
validation tree so the commands can see them. That tree is the retained review
worktree, sealed by hash, so whatever the fill created must be gone when the
checks finish -- on success, failure, timeout or an exception -- while a file
(or directory) that already existed is never removed or modified.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import quality_evidence

PACKAGE = "nf02294_pkg"
CONFIG = ".aiworkhub/quality.json"
CHECK_ID = "sees-filled-inputs"
CANDIDATE_A = "VALUE = 'candidate'\n"

# Passes only while the filled canonical inputs are present.
_SEES_FILL = (
    "from pathlib import Path; "
    "assert Path('scripts/check_me.py').is_file(); "
    "assert Path('tests/test_x.py').is_file(); "
    f"assert Path('src/{PACKAGE}/__init__.py').is_file(); "
    f"assert Path('src/{PACKAGE}/a.py').read_text() == {CANDIDATE_A!r}"
)

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git executable unavailable")


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _declare(root: Path, command: list[str]) -> None:
    _write(root, CONFIG, json.dumps({"checks": [
        {"id": CHECK_ID, "kind": "test", "command": command},
    ]}))


def _git_track(root: Path) -> None:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )
    for args in (("init", "-q"), ("add", "--", *files)):
        subprocess.run(
            [str(_GIT), "-C", str(root), *args],
            check=True, capture_output=True, stdin=subprocess.DEVNULL,
            timeout=60, env=env,
        )


def _snapshot(root: Path) -> dict[str, str]:
    """Every file (sha256) and directory under ``root``; pycache is ignored."""

    seen: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if "__pycache__" in parts:
            continue
        relative = "/".join(parts)
        seen[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() else "<dir>"
        )
    return seen


def _fixture(tmp_path: Path, command: list[str]) -> tuple[Path, Path]:
    """A tracked canonical repo and a sparse tree holding only candidate bytes."""

    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    _declare(canonical, command)
    _write(canonical, "scripts/check_me.py", "VALUE = 1\n")
    _write(canonical, f"src/{PACKAGE}/__init__.py", "")
    _write(canonical, f"src/{PACKAGE}/a.py", "VALUE = 'canonical'\n")
    _write(canonical, "tests/test_x.py", "def test_x():\n    assert True\n")
    _git_track(canonical)
    _declare(union, command)
    _write(union, f"src/{PACKAGE}/a.py", CANDIDATE_A)
    return canonical, union


def _run(union: Path, canonical: Path, **kwargs: object) -> list:
    return quality_evidence.run_declared_checks(
        union, changed_paths=[CONFIG], toolchain_root=canonical, **kwargs
    )


@requires_git
def test_tree_is_byte_identical_after_passing_checks(tmp_path: Path) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])
    before = _snapshot(union)

    results = _run(union, canonical)

    row = next(item for item in results if item.check_id == CHECK_ID)
    # The command still saw the filled inputs while it ran.
    assert row.status == "passed", row.summary
    assert row.provenance.endswith(":sparse_inputs_filled=3")
    assert _snapshot(union) == before


@requires_git
def test_tree_is_restored_when_the_command_fails(tmp_path: Path) -> None:
    canonical, union = _fixture(
        tmp_path, [sys.executable, "-c", _SEES_FILL + "; raise SystemExit(3)"]
    )
    before = _snapshot(union)

    results = _run(union, canonical)

    row = next(item for item in results if item.check_id == CHECK_ID)
    assert row.status == "failed", row.summary
    assert _snapshot(union) == before


@requires_git
def test_tree_is_restored_when_the_command_times_out(tmp_path: Path) -> None:
    canonical, union = _fixture(
        tmp_path,
        [sys.executable, "-c", _SEES_FILL + "; import time; time.sleep(60)"],
    )
    before = _snapshot(union)

    results = _run(union, canonical, timeout_seconds=3)

    row = next(item for item in results if item.check_id == CHECK_ID)
    assert row.status != "passed", row.summary
    assert _snapshot(union) == before


@requires_git
def test_tree_is_restored_when_running_a_command_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])
    before = _snapshot(union)

    def _boom(*_args: object, **_kwargs: object) -> object:
        # The fill is in place while the command would run.
        assert (union / "scripts" / "check_me.py").is_file()
        raise RuntimeError("injected runner failure")

    monkeypatch.setattr(quality_evidence, "_run_command_array", _boom)
    with pytest.raises(RuntimeError, match="injected runner failure"):
        _run(union, canonical)

    assert _snapshot(union) == before


@requires_git
def test_failed_copy_midway_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])
    before = _snapshot(union)
    real_copy = quality_evidence._copy_sparse_input

    def _fail_one(source: Path, root: Path, relative: str) -> str:
        if relative == "tests/test_x.py":
            raise OSError("injected copy failure")
        return real_copy(source, root, relative)

    monkeypatch.setattr(quality_evidence, "_copy_sparse_input", _fail_one)
    with pytest.raises(OSError, match="injected copy failure"):
        _run(union, canonical)

    assert _snapshot(union) == before


@requires_git
def test_preexisting_files_and_directories_are_never_removed(tmp_path: Path) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])
    # An existing empty directory the fill will populate, and an existing file
    # that shares a directory with filled ones.
    (union / "scripts").mkdir()
    _write(union, f"src/{PACKAGE}/local_only.py", "LOCAL = True\n")
    before = _snapshot(union)

    results = _run(union, canonical)

    row = next(item for item in results if item.check_id == CHECK_ID)
    assert row.status == "passed", row.summary
    assert _snapshot(union) == before
    assert (union / "scripts").is_dir()
    assert (union / "src" / PACKAGE / "a.py").read_text(encoding="utf-8") == CANDIDATE_A


@requires_git
def test_fill_still_fills_and_reports_created_paths(tmp_path: Path) -> None:
    """The fill itself is unchanged: files appear, and the receipt names them."""

    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])

    result = quality_evidence._fill_sparse_toolchain_inputs(union, canonical, [CONFIG])

    assert result.filled == (
        "scripts/check_me.py", f"src/{PACKAGE}/__init__.py", "tests/test_x.py",
    )
    assert (union / "scripts" / "check_me.py").is_file()
    assert set(result.created_dirs) == {"scripts", "tests"}

    quality_evidence._remove_sparse_fill(union, result)

    assert not (union / "scripts").exists()
    assert not (union / "tests").exists()
    assert not (union / "src" / PACKAGE / "__init__.py").exists()
    assert (union / "src" / PACKAGE / "a.py").read_text(encoding="utf-8") == CANDIDATE_A


def test_cleanup_leaves_what_the_fill_did_not_create(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write(root, "scripts/mine.py", "keep\n")
    _write(root, "scripts/filled.py", "gone\n")
    fill = quality_evidence.SparseFillResult(
        filled=("scripts/filled.py", "scripts/never_created.py"),
        created_dirs=("scripts", "tests"),
    )
    before_mine = (root / "scripts" / "mine.py").read_bytes()

    quality_evidence._remove_sparse_fill(root, fill)

    assert not (root / "scripts" / "filled.py").exists()
    assert (root / "scripts" / "mine.py").read_bytes() == before_mine
    # A directory that still holds foreign content is kept.
    assert (root / "scripts").is_dir()


@requires_git
def test_partial_copy_residue_is_removed_when_a_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])
    before = _snapshot(union)
    real_copy = quality_evidence._copy_sparse_input

    def _partial_then_fail(source: Path, root: Path, relative: str) -> str:
        if relative == "tests/test_x.py":
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"par")
            raise OSError("injected partial copy failure")
        return real_copy(source, root, relative)

    monkeypatch.setattr(quality_evidence, "_copy_sparse_input", _partial_then_fail)
    with pytest.raises(OSError, match="injected partial copy failure"):
        _run(union, canonical)

    assert not (union / "tests" / "test_x.py").exists()
    assert not (union / "tests").exists()
    assert _snapshot(union) == before


@requires_git
def test_bytecode_cache_does_not_keep_a_created_directory_alive(
    tmp_path: Path,
) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])
    result = quality_evidence._fill_sparse_toolchain_inputs(union, canonical, [CONFIG])
    cache = union / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "x.cpython-312.pyc").write_bytes(b"\x00")

    quality_evidence._remove_sparse_fill(union, result)

    assert not (union / "scripts").exists()
    assert not (union / "tests").exists()


@requires_git
def test_non_bytecode_content_keeps_the_created_directory(tmp_path: Path) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])
    result = quality_evidence._fill_sparse_toolchain_inputs(union, canonical, [CONFIG])
    cache = union / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "x.cpython-312.pyc").write_bytes(b"\x00")
    (cache / "notes.txt").write_text("foreign\n", encoding="utf-8")

    quality_evidence._remove_sparse_fill(union, result)

    assert (cache / "notes.txt").read_text(encoding="utf-8") == "foreign\n"
    assert (cache / "x.cpython-312.pyc").is_file()


@requires_git
def test_cleanup_failure_becomes_a_failed_check_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])

    def _cleanup_fails(*_args: object, **_kwargs: object) -> None:
        raise OSError("sparse_fill_cleanup_failed:x")

    monkeypatch.setattr(quality_evidence, "_remove_sparse_fill", _cleanup_fails)
    results = _run(union, canonical)

    assert [item.check_id for item in results] == [CHECK_ID, "builtin:sparse_fill_cleanup"]
    assert results[0].status == "passed", results[0].summary
    assert results[-1].status == "failed"
    assert "sparse_fill_cleanup_failed:x" in results[-1].error


@requires_git
def test_cleanup_failure_does_not_mask_the_commands_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])

    def _cleanup_fails(*_args: object, **_kwargs: object) -> None:
        raise OSError("sparse_fill_cleanup_failed:x")

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected runner failure")

    monkeypatch.setattr(quality_evidence, "_remove_sparse_fill", _cleanup_fails)
    monkeypatch.setattr(quality_evidence, "_run_command_array", _boom)
    with pytest.raises(RuntimeError, match="injected runner failure"):
        _run(union, canonical)


class _Interrupt(BaseException):
    """Stands in for KeyboardInterrupt/SystemExit without touching pytest's own."""


@requires_git
def test_cleanup_failure_does_not_mask_a_failed_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, union = _fixture(tmp_path, [sys.executable, "-c", _SEES_FILL])

    def _interrupted_copy(*_args: object, **_kwargs: object) -> str:
        raise _Interrupt

    def _cleanup_fails(*_args: object, **_kwargs: object) -> None:
        raise OSError("sparse_fill_cleanup_failed:x")

    monkeypatch.setattr(quality_evidence, "_copy_sparse_input", _interrupted_copy)
    monkeypatch.setattr(quality_evidence, "_remove_sparse_fill", _cleanup_fails)
    with pytest.raises(_Interrupt) as raised:
        _run(union, canonical)
    assert raised.value.__notes__ == ["sparse_fill_cleanup_failed:x"]
