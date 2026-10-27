"""NF-2026-01752: python check rows resolve inside the AppContainer lane.

Measured in the Windows AppContainer validation sandbox (Windows 11 26200,
CPython 3.12.4): ``_windows_native_command_argv([sys.executable, "-V"])``
returned ``(None, "windows_path_component_inspection_failed")``.
``sys.executable`` is ``D:\\Dev\\AIWorkHub\\.venv\\Scripts\\python.exe``; the
container can ``lstat`` ``Scripts`` and ``python.exe`` but every directory
above them raises ``PermissionError`` (WinError 5) -- the lane's site shim only
answers for the ancestors of the request's own directory, and ``.venv`` is
not one of them.  ``_windows_path_component_chain_error`` treated that denial
as an inspection failure, so every ``python``/``python3`` quality row came back
``not_available``.

These tests reproduce that shape by denying ``lstat`` on the ancestors of the
executable's granted directory.  A denied component is accepted only when the
strict final path names the raw path exactly; redirection and batch wrappers
still fail closed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aiworkhub import quality_evidence


def _deny_lstat_above(monkeypatch: pytest.MonkeyPatch, granted: Path) -> None:
    """Make ``lstat`` raise WinError 5 for every ancestor of ``granted``."""

    denied = {os.path.normcase(str(parent)) for parent in granted.parents}
    real_lstat = Path.lstat

    def _lstat(self: Path):
        if os.path.normcase(str(self)) in denied:
            raise PermissionError(13, "Access is denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _lstat)


def _venv_tree(root: Path, name: str) -> Path:
    scripts = root / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    executable = scripts / name
    executable.write_bytes(b"MZ")
    return executable.resolve(strict=True)


def test_denied_ancestor_lstat_resolves_native_executable(tmp_path, monkeypatch):
    executable = _venv_tree(tmp_path, "python.exe")
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(executable))
    _deny_lstat_above(monkeypatch, executable.parent)

    argv, reason = quality_evidence._windows_native_command_argv(
        [str(executable), "-V"]
    )

    assert reason == ""
    assert argv == [str(executable), "-V"]


def test_denied_ancestor_with_redirected_final_path_fails_closed(
    tmp_path, monkeypatch
):
    executable = _venv_tree(tmp_path, "python.exe")
    elsewhere = _venv_tree(tmp_path / "elsewhere", "python.exe")
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(executable))
    _deny_lstat_above(monkeypatch, executable.parent)
    real_resolve = Path.resolve

    def _redirected(self: Path, strict: bool = False) -> Path:
        if os.path.normcase(str(self)) == os.path.normcase(str(executable)):
            return elsewhere
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _redirected)

    argv, reason = quality_evidence._windows_native_command_argv(
        [str(executable), "-V"]
    )

    assert argv is None
    assert reason == "windows_path_component_inspection_failed"


def test_denied_ancestor_lstat_keeps_batch_wrapper_refusal(tmp_path, monkeypatch):
    wrapper = _venv_tree(tmp_path, "tool.cmd")
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(wrapper))
    _deny_lstat_above(monkeypatch, wrapper.parent)

    argv, reason = quality_evidence._windows_native_command_argv(["tool", "-V"])

    assert argv is None
    assert reason == "windows_batch_wrapper_forbidden"


@pytest.mark.skipif(os.name != "nt", reason="python token binding is Windows-only")
def test_python_row_runs_when_interpreter_ancestors_are_denied(
    tmp_path, monkeypatch
):
    interpreter = Path(sys.executable)
    _deny_lstat_above(monkeypatch, interpreter.parent)

    status, stdout, stderr, _ = quality_evidence._run_command_array(
        ["python", "-c", "print('nf01752')"],
        cwd=tmp_path,
        timeout_seconds=120,
    )

    assert status == quality_evidence.STATUS_PASSED, stderr
    assert stdout.strip() == "nf01752"
