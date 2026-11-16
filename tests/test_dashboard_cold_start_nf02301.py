"""NF-2026-02301: the first full dashboard snapshot comes from the reader.

Owner report (0.13.3): the dashboard sat on "Awaiting the full snapshot" and
then showed ``mcp_request_timeout``. The snapshot functions already route to
``getDashboardClient()`` (``aiworkhub.dashboard_reader``), so the defect was
the label: a reader timeout reused the MCP reason code. These tests pin that

* the snapshot functions use the dashboard client, never the MCP client,
* a reader timeout carries its own reason code,
* both codes classify as a delayed (not offline) snapshot, and
* a cold reader process serves a first full snapshot well inside the
  extension's snapshot budget without importing the MCP server or its stdio
  host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aiworkhub import dashboard_reader, task_store

SRC_ROOT = Path(dashboard_reader.__file__).resolve().parent.parent
EXTENSION_JS = Path(__file__).resolve().parent.parent / "vscode-extension" / "extension.js"
FORBIDDEN_MODULES = ("aiworkhub.server", "aiworkhub.stdio_fastmcp")
PROBE_MARKER = "NF02301_PROBE "

# Runs the reader exactly as ``python -m aiworkhub.dashboard_reader`` would,
# then reports (on stderr) which forbidden modules ended up imported.
_PROBE_SCRIPT = (
    "import sys, json\n"
    "from aiworkhub import dashboard_reader\n"
    "code = dashboard_reader.main(['--repo', sys.argv[1]])\n"
    "loaded = [m for m in " + repr(FORBIDDEN_MODULES) + " if m in sys.modules]\n"
    "sys.stderr.write(" + repr(PROBE_MARKER) + " + json.dumps(loaded) + '\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(code)\n"
)


def _extension_source() -> str:
    return EXTENSION_JS.read_text(encoding="utf-8")


def _function_body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _snapshot_bodies(source: str) -> tuple[str, str]:
    once = _function_body(source, "async function pushSnapshotOnce(view)", "async function pushSnapshotNoRetry")
    no_retry = _function_body(source, "async function pushSnapshotNoRetry", "async function pushTaskDetail")
    return once, no_retry


def _snapshot_budget_ms(source: str) -> int:
    match = re.search(r"const MCP_DASHBOARD_SNAPSHOT_TIMEOUT_MS = (\d+);", source)
    assert match, "extension.js no longer declares MCP_DASHBOARD_SNAPSHOT_TIMEOUT_MS"
    return int(match.group(1))


def test_snapshot_functions_use_the_dashboard_client_not_the_mcp_client() -> None:
    once, no_retry = _snapshot_bodies(_extension_source())
    for name, body in (("pushSnapshotOnce", once), ("pushSnapshotNoRetry", no_retry)):
        assert "getDashboardClient()" in body, f"{name} must obtain the dashboard reader client"
        assert "getMcpClient" not in body, f"{name} must never wait on the MCP client"


def test_reader_timeout_has_its_own_reason_code() -> None:
    source = _extension_source()
    assert 'const DASHBOARD_REQUEST_TIMEOUT_REASON = "dashboard_reader_timeout";' in source
    assert 'const DASHBOARD_REQUEST_TIMEOUT_REASON = "mcp_request_timeout";' not in source
    assert "new Error(DASHBOARD_REQUEST_TIMEOUT_REASON)" in source


def test_both_timeout_codes_classify_as_delayed() -> None:
    source = _extension_source()
    assert (
        'const SNAPSHOT_TIMEOUT_REASONS = Object.freeze(["mcp_request_timeout", DASHBOARD_REQUEST_TIMEOUT_REASON]);'
        in source
    )
    assert "function isSnapshotTimeoutReason(reason)" in source
    once, no_retry = _snapshot_bodies(source)
    # No literal compare may survive: every classification goes through the
    # single predicate so a new timeout code cannot be missed in one path.
    for body in (once, no_retry):
        assert '=== "mcp_request_timeout"' not in body
    assert once.count("isSnapshotTimeoutReason(") == 3
    assert no_retry.count("isSnapshotTimeoutReason(") == 1


@pytest.fixture
def cold_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    previous = {key: os.environ.get(key) for key in ("AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO")}
    os.environ["AIWORKHUB_REPO_ROOT"] = str(root)
    os.environ["AIWORKHUB_REPO"] = str(root)
    try:
        assert task_store.initialize_repository(root)["ok"]
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return root


def test_cold_reader_serves_first_full_snapshot_within_the_extension_budget(
    cold_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    budget_ms = _snapshot_budget_ms(_extension_source())
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AIWORKHUB_") and key != "PYTHONPATH"
    }
    env.update(
        {
            "PYTHONPATH": str(SRC_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(home),
            "USERPROFILE": str(home),
        }
    )
    request = json.dumps({"id": 1, "tool": "aiworkhub_dashboard_snapshot", "arguments": {"full": True}}) + "\n"

    started = time.perf_counter()
    with subprocess.Popen(
        [sys.executable, "-c", _PROBE_SCRIPT, str(cold_repo)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
        text=True,
        encoding="utf-8",
    ) as child:
        try:
            stdout, stderr = child.communicate(request, timeout=budget_ms / 1000)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            pytest.fail(f"cold dashboard reader exceeded the {budget_ms} ms extension snapshot budget")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert child.returncode == 0, stderr[-2000:]
    replies = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert len(replies) == 1, stdout[:500]
    reply = replies[0]
    assert reply["id"] == 1
    assert "error" not in reply, reply
    result = reply["result"]
    assert isinstance(result, dict) and result, "the reader returned an empty snapshot"

    probes = [line for line in stderr.splitlines() if line.startswith(PROBE_MARKER)]
    assert len(probes) == 1, stderr[-2000:]
    assert json.loads(probes[0][len(PROBE_MARKER):]) == [], "the reader imported the MCP server or its stdio host"

    assert elapsed_ms < budget_ms, f"cold first snapshot took {elapsed_ms:.0f} ms of a {budget_ms} ms budget"
    with capsys.disabled():
        print(f"\nNF02301_COLD_FIRST_FULL_SNAPSHOT_MS={elapsed_ms:.0f} budget_ms={budget_ms}")
