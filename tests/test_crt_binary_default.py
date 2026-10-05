"""NF-2026-01365: importing aiworkhub makes plain os.open I/O binary on Windows."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="MSVC CRT text mode is Windows-only")

_SRC = Path(__file__).resolve().parents[1] / "src"

_PROBE = r"""
import os
import sys

import aiworkhub  # noqa: F401  (sets the CRT default mode at package init)

root = sys.argv[1]

# (1) an O_RDWR open must not strip a trailing 0x1A
rdwr = os.path.join(root, "rdwr.bin")
with open(rdwr, "wb") as handle:
    handle.write(b"A" * 4095 + b"\x1a")
fd = os.open(rdwr, os.O_RDWR)
try:
    os.fsync(fd)
finally:
    os.close(fd)
assert os.path.getsize(rdwr) == 4096, os.path.getsize(rdwr)

# (2) os.write must not translate LF to CRLF
written = os.path.join(root, "write.bin")
fd = os.open(written, os.O_WRONLY | os.O_CREAT)
try:
    os.write(fd, b"a\nb")
finally:
    os.close(fd)
with open(written, "rb") as handle:
    raw = handle.read()
assert raw == b"a\nb", raw

# (3) os.read must keep CR and must not stop at 0x1A
payload = b"a\r\nb\x1ac"
readable = os.path.join(root, "read.bin")
with open(readable, "wb") as handle:
    handle.write(payload)
fd = os.open(readable, os.O_RDONLY)
try:
    data = os.read(fd, 64)
finally:
    os.close(fd)
assert data == payload, data
print("ok")
"""


def test_import_makes_plain_os_open_binary(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
