"""NF-2026-01178: ``_run`` decodes its pipes as UTF-8 with replacement.

Before the fix the pipes were decoded with the locale codec in strict mode,
so one unmapped byte made the reader thread raise and the whole stream came
back as ``None``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aiworkhub import worker_workspace

pytestmark = pytest.mark.filterwarnings(
    "error::pytest.PytestUnhandledThreadExceptionWarning"
)

_GEORGIAN = "ქართული"

_WRITE_BOTH_PIPES = (
    "import sys\n"
    f"data = {_GEORGIAN!a}.encode('utf-8')\n"
    "sys.stdout.buffer.write(data)\n"
    "sys.stdout.buffer.flush()\n"
    "sys.stderr.buffer.write(data)\n"
    "sys.stderr.buffer.flush()\n"
)


def test_run_decodes_utf8_on_both_pipes(tmp_path: Path) -> None:
    result = worker_workspace._run(
        [sys.executable, "-c", _WRITE_BOTH_PIPES], cwd=tmp_path
    )

    assert result.returncode == 0
    assert result.stdout == _GEORGIAN
    assert result.stderr == _GEORGIAN


def test_run_replaces_an_invalid_byte_instead_of_dropping_the_stream(
    tmp_path: Path,
) -> None:
    child = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xffok')\n"
        "sys.stdout.buffer.flush()\n"
    )

    result = worker_workspace._run([sys.executable, "-c", child], cwd=tmp_path)

    assert result.returncode == 0
    assert isinstance(result.stdout, str)
    assert result.stdout.endswith("ok")


def test_run_bytes_mode_is_unchanged(tmp_path: Path) -> None:
    result = worker_workspace._run(
        [sys.executable, "-c", _WRITE_BOTH_PIPES], cwd=tmp_path, text=False
    )

    assert result.returncode == 0
    assert result.stdout == _GEORGIAN.encode("utf-8")
    assert result.stderr == _GEORGIAN.encode("utf-8")


def test_run_writes_input_text_as_utf8(tmp_path: Path) -> None:
    child = (
        "import sys\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        "sys.stdout.buffer.flush()\n"
    )

    result = worker_workspace._run(
        [sys.executable, "-c", child], cwd=tmp_path, input_text=_GEORGIAN
    )

    assert result.returncode == 0
    assert result.stdout == _GEORGIAN
