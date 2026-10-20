"""The stdio MCP smoke script passes on the in-tree server and fails on broken ones."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPT = ROOT / "scripts" / "mcp_stdio_smoke.py"

_SPEC = importlib.util.spec_from_file_location("mcp_stdio_smoke", SCRIPT)
assert _SPEC and _SPEC.loader
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)

IN_TREE_SERVER = [sys.executable, "-c", "from aiworkhub.server import main; main()"]
IGNORES_STDIN = [sys.executable, "-c", "import time; time.sleep(600)"]
IGNORES_SIGTERM = [
    sys.executable,
    "-c",
    "import json, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}), flush=True)\n"
    "time.sleep(600)\n",
]

# Answers like an MCP server, but surrounded by the noise the script must skip: a
# non-JSON banner, a response to an unrelated id, and a notification before each reply.
FAKE_SERVER = """\
import json
import sys

mode = sys.argv[1]
print("starting up, this line is not json", flush=True)
print(json.dumps({"jsonrpc": "2.0", "id": 77, "result": {}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        name = "Other MCP" if mode == "wrong-name" else "AIWorkHub MCP"
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": name, "version": "9"},
        }
    elif method == "tools/list":
        tools = [] if mode == "no-tools" else [{"name": "alpha"}, {"name": "beta"}]
        result = {"tools": tools}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}), flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.fixture
def fake_server(tmp_path):
    path = tmp_path / "fake_mcp_server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return lambda mode: [sys.executable, str(path), mode]


def run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def summary_of(result):
    lines = result.stdout.strip().splitlines()
    assert lines, f"no summary line on stdout; stderr:\n{result.stderr}"
    return json.loads(lines[-1])


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_smoke_passes_against_the_in_tree_server(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repo)], check=True, capture_output=True, timeout=60
    )
    env = dict(os.environ)
    env.pop("AIWORKHUB_REPO", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    env.update(
        AIWORKHUB_REPO_ROOT=str(repo),
        AIWORKHUB_ALLOW_WRITES="0",
        AIWORKHUB_ALLOW_LAUNCH="0",
        PYTHONUNBUFFERED="1",
    )
    result = run_cli("--timeout", "40", "--", *IN_TREE_SERVER, env=env)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    summary = summary_of(result)
    assert set(summary) == {"ok", "server_name", "server_version", "tool_count"}
    assert summary["ok"] is True
    assert summary["server_name"] == "AIWorkHub MCP"
    assert summary["tool_count"] > 0


def test_smoke_fails_when_the_command_prints_nothing_and_exits():
    result = run_cli("--timeout", "20", "--", sys.executable, "-c", "pass")
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    summary = summary_of(result)
    assert summary["ok"] is False
    assert summary["reason"] == "server_exited"
    assert "FAIL server_exited" in result.stderr


def test_smoke_escapes_control_characters_from_the_child_stderr():
    program = (
        "import sys; "
        "sys.stderr.write('x' + chr(13) + '::warning::forged' + chr(27) + '[2J' + chr(10))"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--timeout", "20", "--", sys.executable, "-c", program],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1, f"stdout:\n{result.stdout!r}\nstderr:\n{result.stderr!r}"
    assert b"\x1b" not in result.stderr
    assert b"x\r::warning" not in result.stderr
    assert b"x\\r::warning::forged\\x1b[2J" in result.stderr


def test_smoke_times_out_on_a_command_that_stays_silent():
    result = run_cli("--timeout", "2", "--", sys.executable, "-c", "import sys; sys.stdin.read()")
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    summary = summary_of(result)
    assert summary["ok"] is False
    assert summary["reason"] == "timeout"


def test_smoke_skips_noise_and_unrelated_ids(fake_server):
    summary = smoke.run_smoke(fake_server("ok"), timeout=30, grace=2)
    assert summary == {
        "ok": True,
        "server_name": "AIWorkHub MCP",
        "server_version": "9",
        "tool_count": 2,
    }


@pytest.mark.parametrize(
    ("mode", "reason", "server_name"),
    [("wrong-name", "wrong_server", "Other MCP"), ("no-tools", "no_tools", "AIWorkHub MCP")],
)
def test_smoke_rejects_a_wrong_server_name_and_an_empty_tool_list(
    fake_server, mode, reason, server_name
):
    with pytest.raises(smoke.SmokeFailure) as failure:
        smoke.run_smoke(fake_server(mode), timeout=30, grace=2)
    assert failure.value.reason == reason
    assert failure.value.summary["ok"] is False
    assert failure.value.summary["server_name"] == server_name
    assert failure.value.summary["tool_count"] == 0


def test_smoke_reports_a_command_that_cannot_start(tmp_path):
    with pytest.raises(smoke.SmokeFailure) as failure:
        smoke.run_smoke([str(tmp_path / "no-such-server")], timeout=5)
    assert failure.value.reason == "spawn_failed"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--timeout", "5"],
        ["--timeout", "5", "--"],
        ["--timeout", "0", "--", "server"],
        ["--timeout", "nan", "--", "server"],
        ["--timeout", "99999", "--", "server"],
    ],
)
def test_usage_errors_exit_with_status_2(argv):
    with pytest.raises(SystemExit) as exit_info:
        smoke.main(argv)
    assert exit_info.value.code == 2


def test_server_process_reaps_a_child_that_ignores_stdin():
    with smoke.ServerProcess(IGNORES_STDIN, timeout=30, grace=0.5) as server:
        proc = server.proc
        assert proc.poll() is None
    assert proc.poll() is not None
    assert server.exit_code is not None


def test_server_process_reaps_the_child_when_the_block_raises():
    with pytest.raises(RuntimeError, match="boom"):
        with smoke.ServerProcess(IGNORES_STDIN, timeout=30, grace=0.5) as server:
            proc = server.proc
            raise RuntimeError("boom")
    assert proc.poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM and SIGKILL are POSIX signals")
def test_server_process_kills_a_child_that_ignores_sigterm():
    with smoke.ServerProcess(IGNORES_SIGTERM, timeout=30, grace=0.5) as server:
        server.await_response(smoke.INITIALIZE_ID, "ready")
        proc = server.proc
    assert proc.returncode == -signal.SIGKILL
