#!/usr/bin/env python3
"""Prove that a stdio MCP server command answers ``initialize`` and ``tools/list``.

Usage: python scripts/mcp_stdio_smoke.py [--timeout S] -- <server command...>

The command is started with stdin/stdout pipes and driven with newline-delimited
JSON-RPC: ``initialize``, ``notifications/initialized``, ``tools/list``. The server
must identify itself as "AIWorkHub MCP" and list at least one tool. One JSON summary
line is printed on stdout; a failure also names its reason on stderr.

Exit status: 0 pass, 1 smoke failure, 2 usage error. The child is always reaped: stdin
is closed first so a well-behaved server exits by itself, then the child (with its
process group or tree) is terminated and finally killed, each after a grace period.
Standard library only, so it runs before anything is installed, for example against
``docker run -i --rm <image>`` in CI.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from typing import IO, Any

EXPECTED_SERVER_NAME = "AIWorkHub MCP"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 90.0
MAX_TIMEOUT = 3600.0
DEFAULT_GRACE = 8.0
EXIT_PROBE_SECONDS = 2.0
DRAIN_SECONDS = 2.0
MAX_LINE_BYTES = 64 * 1024 * 1024
STDERR_TAIL_LINES = 20
STDERR_LINE_BYTES = 300

INITIALIZE_ID = 1
TOOLS_LIST_ID = 2
AWAITED_IDS = (INITIALIZE_ID, TOOLS_LIST_ID)
INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": INITIALIZE_ID,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "aiworkhub-smoke", "version": "1"},
    },
}
INITIALIZED: dict[str, Any] = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS_LIST: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": TOOLS_LIST_ID,
    "method": "tools/list",
    "params": {},
}


class SmokeFailure(Exception):
    """The smoke did not pass; ``reason`` is a stable machine-readable name."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.summary: dict[str, Any] = {}
        self.stderr_tail: list[str] = []


def _decode_message(raw: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None
    return message if isinstance(message, dict) else None


def _close_quietly(stream: IO[bytes] | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _signal_tree(proc: subprocess.Popen[bytes], *, force: bool) -> None:
    """Stop the child together with everything it spawned."""
    try:
        if os.name == "nt":
            # A console child has no graceful signal on Windows; /T takes the tree.
            # Absolute path: a bare "taskkill" is resolved via the cwd and PATH (CWE-427).
            subprocess.run(
                [
                    os.path.join(
                        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"
                    ),
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        else:
            # The child leads its own session, so its pid is its process group id.
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    (proc.kill if force else proc.terminate)()


class ServerProcess:
    """A spawned server command; leaving the ``with`` block always reaps it."""

    def __init__(
        self, command: Sequence[str], timeout: float, grace: float = DEFAULT_GRACE
    ) -> None:
        self.command = list(command)
        self.timeout = timeout
        self.grace = grace
        self.proc: subprocess.Popen[bytes] | None = None
        self.exit_code: int | None = None
        self._deadline = 0.0
        self._cond = threading.Condition()
        self._responses: dict[int, dict[str, Any]] = {}
        self._stdout_closed = False
        self._oversize = False
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._pumps: list[tuple[threading.Thread, IO[bytes]]] = []

    def __enter__(self) -> ServerProcess:
        self._deadline = time.monotonic() + self.timeout
        extra: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
        try:
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **extra,
            )
        except (OSError, ValueError) as error:
            raise SmokeFailure(
                "spawn_failed", f"cannot start {self.command[0]!r}: {error}"
            ) from error
        try:
            for target, stream in (
                (self._pump_stdout, self.proc.stdout),
                (self._pump_stderr, self.proc.stderr),
            ):
                thread = threading.Thread(target=target, args=(stream,), daemon=True)
                thread.start()
                self._pumps.append((thread, stream))
        except BaseException:
            self._reap()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._reap()

    def stderr_tail(self) -> list[str]:
        return list(self._stderr)

    def send(self, message: dict[str, Any], what: str) -> None:
        data = json.dumps(message, separators=(",", ":")).encode("ascii") + b"\n"
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise self._gone(what) from None

    def await_response(self, request_id: int, what: str) -> dict[str, Any]:
        """Return the response to ``request_id``; every other stdout line is skipped."""
        with self._cond:
            while request_id not in self._responses and not self._stdout_closed:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise SmokeFailure("timeout", f"no {what} response within {self.timeout:g}s")
                self._cond.wait(remaining)
            message = self._responses.get(request_id)
        if message is None:
            raise self._gone(what)
        return message

    def _gone(self, what: str) -> SmokeFailure:
        if self._oversize:
            return SmokeFailure(
                "stdout_line_too_long",
                f"stdout line over {MAX_LINE_BYTES} bytes before the {what} response",
            )
        try:
            code = self.proc.wait(timeout=EXIT_PROBE_SECONDS)
        except subprocess.TimeoutExpired:
            return SmokeFailure(
                "server_closed_stdio",
                f"server closed its stdio before the {what} response but is still running",
            )
        return SmokeFailure(
            "server_exited", f"server exited with code {code} before the {what} response"
        )

    def _pump_stdout(self, stream: IO[bytes]) -> None:
        # Keeps only the first response per awaited id, so a chatty server cannot grow memory.
        try:
            while True:
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                    self._oversize = True
                    return
                message = _decode_message(line)
                if message is None or "method" in message:
                    continue
                request_id = message.get("id")
                if type(request_id) is int and request_id in AWAITED_IDS:
                    with self._cond:
                        self._responses.setdefault(request_id, message)
                        self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._stdout_closed = True
                self._cond.notify_all()

    def _pump_stderr(self, stream: IO[bytes]) -> None:
        try:
            for raw in iter(lambda: stream.readline(STDERR_LINE_BYTES), b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr.append(text)
        except (OSError, ValueError):
            pass

    def _wait_exit(self, proc: subprocess.Popen[bytes]) -> bool:
        try:
            proc.wait(timeout=self.grace)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _reap(self) -> None:
        proc = self.proc
        if proc is None:
            return
        _close_quietly(proc.stdin)
        if not self._wait_exit(proc):
            for force in (False, True):
                _signal_tree(proc, force=force)
                if self._wait_exit(proc):
                    break
        self.exit_code = proc.returncode
        for thread, stream in self._pumps:
            thread.join(timeout=DRAIN_SECONDS)
            if not thread.is_alive():
                _close_quietly(stream)


def _result_of(message: dict[str, Any], what: str) -> dict[str, Any]:
    if "error" in message:
        error = json.dumps(message["error"])[:300]
        raise SmokeFailure("rpc_error", f"{what} returned an error: {error}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise SmokeFailure("bad_response", f"{what} response has no result object")
    return result


def _handshake(server: ServerProcess, summary: dict[str, Any]) -> None:
    server.send(INITIALIZE, "initialize")
    result = _result_of(server.await_response(INITIALIZE_ID, "initialize"), "initialize")
    info = result.get("serverInfo")
    if not isinstance(info, dict):
        raise SmokeFailure("bad_initialize", "initialize result has no serverInfo object")
    version = info.get("version")
    summary["server_name"] = info.get("name")
    summary["server_version"] = version if isinstance(version, str) else None
    if summary["server_name"] != EXPECTED_SERVER_NAME:
        raise SmokeFailure(
            "wrong_server",
            f"serverInfo.name is {summary['server_name']!r}, expected {EXPECTED_SERVER_NAME!r}",
        )
    server.send(INITIALIZED, "notifications/initialized")
    server.send(TOOLS_LIST, "tools/list")
    result = _result_of(server.await_response(TOOLS_LIST_ID, "tools/list"), "tools/list")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise SmokeFailure("bad_response", "tools/list result has no tools array")
    summary["tool_count"] = len(tools)
    if not tools:
        raise SmokeFailure("no_tools", "tools/list returned an empty tools array")


def run_smoke(
    command: Sequence[str], timeout: float = DEFAULT_TIMEOUT, grace: float = DEFAULT_GRACE
) -> dict[str, Any]:
    """Drive the handshake against ``command``; return the summary or raise SmokeFailure."""
    summary: dict[str, Any] = {
        "ok": False,
        "server_name": None,
        "server_version": None,
        "tool_count": 0,
    }
    server = ServerProcess(command, timeout, grace)
    try:
        with server:
            _handshake(server, summary)
    except SmokeFailure as failure:
        failure.summary = dict(summary)
        failure.stderr_tail = server.stderr_tail()
        raise
    summary["ok"] = True
    return summary


def _complain(text: str) -> None:
    safe = text.encode("unicode_escape").decode("ascii")
    print(f"mcp_stdio_smoke: {safe}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cut = raw.index("--") if "--" in raw else len(raw)
    options, command = raw[:cut], raw[cut + 1 :]
    parser = argparse.ArgumentParser(
        prog="mcp_stdio_smoke.py",
        usage="%(prog)s [--timeout S] -- <server command...>",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="S",
        help=f"seconds to wait for both responses (default {DEFAULT_TIMEOUT:g})",
    )
    args = parser.parse_args(options)
    if not command:
        parser.error("a server command is required after '--'")
    if not (math.isfinite(args.timeout) and 0 < args.timeout <= MAX_TIMEOUT):
        parser.error(f"--timeout must be greater than 0 and at most {MAX_TIMEOUT:g} seconds")
    try:
        summary = run_smoke(command, args.timeout)
    except SmokeFailure as failure:
        print(json.dumps({**failure.summary, "ok": False, "reason": failure.reason}))
        _complain(f"FAIL {failure}")
        for line in failure.stderr_tail:
            _complain(f"  server stderr: {line}")
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
