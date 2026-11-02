"""A probe reproduction command is one line the validation tokenizer accepts (NF-2026-01985)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import finding_reproduction as fr
from aiworkhub import worker_workspace

_FORBIDDEN = ("\n", "\r", "|", ";", "`", ">", "<")

_TRICKY_BODY = (
    "import sys\n"
    "x = \"a; b | c\"; y = 'q`r'\n"
    "assert 1 < 2 and 3 > 2, \"<ok>\"\n"
    "print(x, y)\n"
)


def _argv(spec: fr.ReproductionSpec) -> list[str]:
    argv = worker_workspace._tokenize_validation_command(spec.command)
    assert argv[0] == "python"
    return [sys.executable, *argv[1:]]


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    spec = fr.parse_reproduction_spec(fr.PROBE_PREFIX + body)
    assert spec is not None and spec.kind == "probe"
    return subprocess.run(
        _argv(spec),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("windows", [False, True])
def test_probe_command_is_one_tokenizable_line(monkeypatch, windows: bool) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: windows)
    spec = fr.parse_reproduction_spec(fr.PROBE_PREFIX + _TRICKY_BODY)
    assert spec is not None
    assert not any(ch in spec.command for ch in _FORBIDDEN)
    argv = worker_workspace._tokenize_validation_command(spec.command)
    assert argv[:3] == ["python", "-I", "-c"] and len(argv) == 4


def test_probe_text_and_sha_hash_reviewer_text(tmp_path: Path) -> None:
    text = fr.PROBE_PREFIX + _TRICKY_BODY
    spec = fr.parse_reproduction_spec(text)
    assert spec is not None
    assert spec.text == text
    import hashlib

    assert spec.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_probe_exit_codes_and_src_import(tmp_path: Path) -> None:
    pkg = tmp_path / "src"
    pkg.mkdir()
    (pkg / "nf01985_probe_mod.py").write_text("VALUE = 42\n", encoding="utf-8")

    ok = _run(tmp_path, _TRICKY_BODY)
    assert ok.returncode == 0, ok.stderr
    assert "a; b | c q`r" in ok.stdout

    assert _run(tmp_path, "assert False").returncode == 1
    assert _run(tmp_path, "import does_not_exist_xyz").returncode == 3

    imported = _run(
        tmp_path,
        "import nf01985_probe_mod\nassert nf01985_probe_mod.VALUE == 42\n",
    )
    assert imported.returncode == 0, imported.stderr


def test_pytest_spec_command_unchanged() -> None:
    spec = fr.parse_reproduction_spec("pytest:tests/test_calc.py::test_add")
    assert spec is not None
    assert spec.command == (
        "python -m pytest -q -p no:cacheprovider tests/test_calc.py::test_add"
    )
