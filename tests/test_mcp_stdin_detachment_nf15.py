"""NF-2026-00015: children must not inherit the MCP server's request pipe.

Measured on Windows: a child that inherited this server's stdin handle hung
before it executed its own first statement -- ``git --version`` timed out at
15 s and a bare ``python -c`` never wrote its marker file. Every coordinator
child that does not redirect stdin was affected, so ``git ls-files -z`` burned
its full 120 s budget inside ``toolchain_authority.repository_tracked_paths``
and ``aiworkhub_task_create`` appeared to stall for the client's whole idle
timeout, while the create path itself answers in 0.16 s.

POSIX never had the hang, which is why the same code stayed healthy on Linux.
The other half of the contract is platform-independent and is asserted here on
every host: a child holding the request pipe can consume JSON-RPC bytes
addressed to the server, and after detachment it cannot.

Run: python3 -m pytest -q tests/test_mcp_stdin_detachment_nf15.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"

# The child runs inside a process whose stdin is a pipe this test holds open and
# never writes to -- exactly the shape the MCP server runs in.
_CHILD = r"""
import json, os, subprocess, sys, time
sys.path.insert(0, {src!r})
from aiworkhub import server

marker = {marker!r}
report = {{}}

stdin_before = os.fstat(0)
server._detach_inheritable_stdin()
report["stdin_changed"] = os.fstat(0)[:2] != stdin_before[:2] or os.name != "nt"
report["stdin_readable_object"] = hasattr(sys.stdin, "buffer")

started = time.time()
try:
    subprocess.run(
        [sys.executable, "-c", "open(%r, 'a').write('ok')" % marker],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    report["child_exited"] = True
except subprocess.TimeoutExpired:
    report["child_exited"] = False
report["child_seconds"] = round(time.time() - started, 2)
report["marker_written"] = os.path.exists(marker)
sys.stderr.write(json.dumps(report) + "\n")
sys.stderr.flush()
"""


def _run_child(marker: Path) -> dict:
    source = _CHILD.format(src=str(_SRC), marker=str(marker))
    proc = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.PIPE,  # a pipe nobody writes to: the server's shape
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(_ROOT),
    )
    try:
        _out, err = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:  # pragma: no cover - the defect itself
        proc.kill()
        pytest.fail("the instrumented server child never reported")
    lines = [line for line in err.decode("utf-8", "replace").splitlines() if line.strip()]
    assert lines, "child produced no report"
    import json

    return json.loads(lines[-1])


def test_a_child_does_not_inherit_the_request_pipe(tmp_path: Path) -> None:
    marker = tmp_path / "child-marker.txt"

    report = _run_child(marker)

    assert report["child_exited"] is True, report
    assert report["marker_written"] is True, report
    # The defect took the child's full 30 s budget; a detached child is instant.
    assert report["child_seconds"] < 15, report
    assert report["stdin_readable_object"] is True, report


def test_detachment_keeps_the_protocol_stream_readable(tmp_path: Path) -> None:
    """The reader keeps a private duplicate: requests still arrive."""

    source = (
        f"import sys; sys.path.insert(0, {str(_SRC)!r});"
        "from aiworkhub import server;"
        "server._detach_inheritable_stdin();"
        "line = sys.stdin.buffer.readline();"
        "sys.stderr.write('READ:' + line.decode('utf-8').strip() + '\\n');"
        "sys.stderr.flush()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(_ROOT),
    )
    try:
        _out, err = proc.communicate(input=b'{"jsonrpc":"2.0"}\n', timeout=180)
    except subprocess.TimeoutExpired:  # pragma: no cover - the defect itself
        proc.kill()
        pytest.fail("the detached reader never saw the request line")
    assert 'READ:{"jsonrpc":"2.0"}' in err.decode("utf-8", "replace")


def test_descriptor_zero_no_longer_names_the_request_pipe() -> None:
    """Descriptor 0 is the null device afterwards, on every host."""

    source = (
        f"import os, sys; sys.path.insert(0, {str(_SRC)!r});"
        "from aiworkhub import server;"
        "server._detach_inheritable_stdin();"
        "sys.stderr.write('READ0:' + repr(os.read(0, 16)) + '\\n');"
        "sys.stderr.flush()"
    )
    with tempfile.TemporaryDirectory() as scratch:
        proc = subprocess.Popen(
            [sys.executable, "-c", source],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=scratch,
        )
        try:
            _out, err = proc.communicate(input=b"not-for-the-child\n", timeout=180)
        except subprocess.TimeoutExpired:  # pragma: no cover - the defect itself
            proc.kill()
            pytest.fail("reading detached descriptor 0 blocked")
    # The null device is always at EOF: descriptor 0 can no longer consume a
    # single byte of the protocol stream.
    assert "READ0:b''" in err.decode("utf-8", "replace")
