"""NF-2026-00980 bounded Windows AppContainer LSP nested child-spawn probe.

One focused diagnostic canary for Task MCP's canonical Windows AppContainer
validation.  It records and asserts, in order:

* ``Path(sys.executable).is_file()`` -- the validation interpreter is a
  visible file inside the container.
* ``source_graph_lsp.server_command_available((sys.executable,))`` -- the
  production LSP launcher accepts this exact command.
* a nested child Python script under ``tmp_path``, started with the same
  ``Popen`` shape as ``source_graph_lsp._start_session`` (``list(argv)``,
  stdin/stdout pipes, ``cwd=str(tmp_path)``, ``env=os.environ.copy()``,
  ``start_new_session=True``; stderr is a PIPE here instead of production
  DEVNULL so a failed child leaves bounded evidence), can read a fixture in
  ``tmp_path``, write a marker, emit a stdout marker and exit 0.

This is a canary, not a tolerance test: a real sandbox defect must surface as
a red test that preserves exact evidence (OSError type/errno/winerror,
bounded stderr, returncode, file states).  No skips, no production changes,
no sandbox permission changes, no secrets.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from aiworkhub import source_graph_lsp

FIXTURE_NAME = "nf980_child_fixture.txt"
MARKER_NAME = "nf980_child_marker.txt"
SCRIPT_NAME = "nf980_child_script.py"
FIXTURE_PAYLOAD = "nf980-fixture-v1"
STDOUT_MARKER = "NF980_CHILD_STDOUT_OK:"
CHILD_TIMEOUT_S = 30.0
EVIDENCE_CHAR_LIMIT = 2000

# Tiny child: read one fixture relative to its cwd (tmp_path), write one
# marker file, print one stdout marker, exit 0.  On OSError it reports
# type/errno/winerror/cwd to stderr and exits 3.
CHILD_SCRIPT = """\
import sys
from pathlib import Path

try:
    cwd = Path.cwd()
    payload = (cwd / "%(fixture_name)s").read_text(encoding="utf-8").strip()
    (cwd / "%(marker_name)s").write_text("read:" + payload + "\\n", encoding="utf-8")
except OSError as exc:
    sys.stderr.write(
        "child OSError type="
        + type(exc).__name__
        + " errno="
        + str(exc.errno)
        + " winerror="
        + str(getattr(exc, "winerror", None))
        + " cwd="
        + str(Path.cwd())
        + "\\n"
    )
    sys.exit(3)
sys.stdout.write("%(stdout_marker)s" + payload + "\\n")
""" % {
    "fixture_name": FIXTURE_NAME,
    "marker_name": MARKER_NAME,
    "stdout_marker": STDOUT_MARKER,
}


def _bounded(data: bytes | str) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    if len(text) > EVIDENCE_CHAR_LIMIT:
        extra = len(text) - EVIDENCE_CHAR_LIMIT
        return text[:EVIDENCE_CHAR_LIMIT] + f"...[truncated {extra} chars]"
    return text


def _file_states(base: Path) -> dict[str, str]:
    states: dict[str, str] = {}
    for name in (FIXTURE_NAME, SCRIPT_NAME, MARKER_NAME):
        candidate = base / name
        if candidate.is_file():
            states[name] = f"file size={candidate.stat().st_size}"
        else:
            states[name] = "missing"
    return states


def _evidence(
    returncode: int | None, stdout_b: bytes, stderr_b: bytes, base: Path
) -> str:
    return " ".join(
        [
            f"returncode={returncode}",
            f"executable={sys.executable}",
            f"cwd={base}",
            f"stdout={_bounded(stdout_b)!r}",
            f"stderr={_bounded(stderr_b)!r}",
            f"files={_file_states(base)}",
        ]
    )


def test_nf980_nested_child_spawn_probe_inside_canonical_appcontainer(tmp_path):
    interpreter = Path(sys.executable)
    print(f"nf980 executable={sys.executable}")
    print(f"nf980 cwd={tmp_path}")
    assert interpreter.is_file(), (
        f"sys.executable is not a visible file inside the container: {sys.executable}"
    )

    command = (sys.executable,)
    available = source_graph_lsp.server_command_available(command)
    print(f"nf980 server_command_available({command!r}) -> {available}")
    assert available, (
        f"server_command_available rejected the canonical interpreter: {sys.executable}"
    )

    fixture = tmp_path / FIXTURE_NAME
    fixture.write_text(FIXTURE_PAYLOAD + "\n", encoding="utf-8")
    script = tmp_path / SCRIPT_NAME
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    marker = tmp_path / MARKER_NAME

    # Same Popen shape as source_graph_lsp._start_session; stderr is a PIPE
    # here (production uses DEVNULL) so a failed child leaves evidence.
    argv = [sys.executable, str(script)]
    try:
        child = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        print(
            "nf980 spawn OSError type="
            + type(exc).__name__
            + " errno="
            + str(exc.errno)
            + " winerror="
            + str(winerror)
            + " executable="
            + sys.executable
            + " cwd="
            + str(tmp_path)
        )
        raise

    try:
        stdout_b, stderr_b = child.communicate(timeout=CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        child.kill()
        stdout_b, stderr_b = child.communicate()
        evidence = _evidence(child.returncode, stdout_b, stderr_b, tmp_path)
        print("nf980 timeout evidence: " + evidence)
        raise AssertionError(
            f"child timed out after {CHILD_TIMEOUT_S}s: " + evidence
        )

    evidence = _evidence(child.returncode, stdout_b, stderr_b, tmp_path)
    print("nf980 evidence: " + evidence)
    assert child.returncode == 0, "child exited nonzero: " + evidence
    stdout_text = stdout_b.decode("utf-8", "replace")
    assert STDOUT_MARKER + FIXTURE_PAYLOAD in stdout_text, (
        "child stdout marker missing: " + evidence
    )
    assert marker.is_file(), "child marker file missing: " + evidence
    assert marker.read_text(encoding="utf-8") == f"read:{FIXTURE_PAYLOAD}\n", (
        "child marker payload mismatch: " + evidence
    )
