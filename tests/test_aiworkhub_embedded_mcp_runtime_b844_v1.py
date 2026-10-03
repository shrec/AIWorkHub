"""B844 V2: extension-local bundled MCP runtime + bounded stdlib fallback.

Proves the two halves of the "install anywhere" release gap this task
closes:

  1. The packaged VSIX bundles the canonical ``aiworkhub`` Python package
     (including ``dashboard_static`` assets) under an extension-local
     ``runtime/`` directory, and the extension never derives its
     ``import aiworkhub`` path from a repository checkout, an editable
     install, or a fixed host-absolute path.
  2. When the optional ``mcp`` PyPI package is not importable -- exactly
     what a bundled runtime with user/site-packages disabled looks like --
     ``aiworkhub.server`` falls back to a bounded, dependency-free stdio
     JSON-RPC/MCP server that still serves the canonical dashboard tool
     registry, with structured (never crashing) protocol errors.

The end-to-end smoke spawns the *actual* VSIX-extracted runtime with
``-S`` (skip the ``site`` module -- no global or user site-packages) against
a *fresh* temporary "repository" containing no source checkout and no
virtualenv, so it cannot silently succeed by falling back to an ambient
``mcp`` install or an ambient AIWorkHub checkout.
"""

from __future__ import annotations

import io
import json
import os
import queue
import selectors
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]  # tools/geoai-task-mcp
SRC = REPO / "src"
EXT_DIR = REPO / "vscode-extension"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _vsix_path(output_dir: Path) -> Path:
    """Always (re)build the VSIX from the CURRENT source into ``output_dir``
    and return exactly that freshly-built artifact.

    An earlier version globbed ``dist/aiworkhub-*.vsix`` and returned
    ``sorted(...)[-1]``. That sort is lexicographic, so a dist/ holding many
    historical builds returned e.g. ``aiworkhub-0.6.9.vsix`` ("...-0.6.9" sorts
    after "...-0.6.31") -- a stale bundle. The test then validated OLD bundled
    output and silently passed local source changes, while CI (with an empty
    dist/) built fresh and failed. Building fresh every time makes this test
    validate the current extension.js/runtime deterministically on every host,
    now routed through ``AIWORKHUB_VALIDATION_EXEC_SCRATCH_ROOT`` into
    ``output_dir`` so the build never touches the release ``dist/`` directory.
    """
    version = json.loads((EXT_DIR / "package.json").read_text(encoding="utf-8"))["version"]
    subprocess.run(
        ["node", "test/package-vsix.js"],
        cwd=str(EXT_DIR),
        check=True,
        timeout=120,
        env={**os.environ, "AIWORKHUB_VALIDATION_EXEC_SCRATCH_ROOT": str(output_dir)},
    )
    built = output_dir / f"aiworkhub-{version}.vsix"
    assert built.is_file(), f"packaging step did not produce {built.name}"
    return built


def _dist_vsix_snapshot(dist: Path) -> dict[str, tuple[int, int]]:
    if not dist.is_dir():
        return {}
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in dist.glob("*.vsix")}


@pytest.fixture(scope="module")
def built_vsix(tmp_path_factory):
    dist = EXT_DIR / "dist"
    dist_before = _dist_vsix_snapshot(dist)
    output_dir = tmp_path_factory.mktemp("vsix_build")
    vsix = _vsix_path(output_dir)
    return {"path": vsix, "dist_before": dist_before}


@pytest.fixture(scope="module")
def extracted_vsix(tmp_path_factory, built_vsix):
    dest = tmp_path_factory.mktemp("vsix_extract")
    with zipfile.ZipFile(built_vsix["path"]) as zf:
        zf.extractall(dest)
    return dest


def test_packaging_fixture_leaves_release_dist_untouched(built_vsix):
    dist = EXT_DIR / "dist"
    dist_after = _dist_vsix_snapshot(dist)
    assert dist_after == built_vsix["dist_before"]

    built = built_vsix["path"].resolve()
    dist_resolved = dist.resolve()
    assert dist_resolved != built.parent
    assert dist_resolved not in built.parents


# ---------------------------------------------------------------------------
# 1. VSIX contents: canonical package + dashboard assets, extension-local.
# ---------------------------------------------------------------------------


def test_vsix_bundles_canonical_aiworkhub_package_with_dashboard_assets(extracted_vsix):
    runtime = extracted_vsix / "extension" / "runtime" / "aiworkhub"
    assert (runtime / "__init__.py").is_file()
    assert (runtime / "server.py").is_file()
    assert (runtime / "core.py").is_file()
    assert (runtime / "dashboard_mcp_app.py").is_file()

    static = runtime / "dashboard_static"
    assert (static / "index.html").is_file()
    assert (static / "dashboard.js").is_file()
    assert (static / "dashboard.css").is_file()

    for cache_dir in runtime.rglob("__pycache__"):
        pytest.fail(f"bundled runtime must never ship bytecode caches: {cache_dir}")
    for pyc in runtime.rglob("*.pyc"):
        pytest.fail(f"bundled runtime must never ship compiled bytecode: {pyc}")


def test_extension_js_never_derives_import_from_repo_or_fixed_host_path(extracted_vsix):
    ext_js = (extracted_vsix / "extension" / "extension.js").read_text(encoding="utf-8")
    # The runtime dir is derived from context.extensionUri via
    # resolveExtensionRuntimeDir, which prefers the packaged runtime/ and only
    # falls back to the sibling repo src/ in a development checkout -- never a
    # repository path or a fixed host-absolute path. (A non-existent runtime/
    # PYTHONPATH is what broke Codex's `python -m aiworkhub.server`.)
    assert "resolveExtensionRuntimeDir(context.extensionUri.fsPath)" in ext_js
    assert 'path.join(extensionFsPath, "runtime")' in ext_js
    assert "env.PYTHONPATH =" in ext_js
    assert "cwd: runtimeDir || root" in ext_js
    for forbidden in ("pip install", "pip3 install", "--editable", "site-packages"):
        assert forbidden not in ext_js, f"forbidden pattern found in bundled extension.js: {forbidden!r}"


# ---------------------------------------------------------------------------
# 2. In-process unit coverage of the bounded stdlib fallback dispatcher.
# ---------------------------------------------------------------------------


@pytest.fixture()
def fallback_server_module():
    """Import aiworkhub.server with any externally-installed ``mcp`` hidden.

    Mirrors the exact bundled-runtime condition this task closes: no
    optional ``mcp`` PyPI package importable, so ``server.FastMCP`` must be
    this module's own bounded stdlib fallback, not the real package's class.
    """
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    package = sys.modules.get("aiworkhub")
    saved_server_module = sys.modules.pop("aiworkhub.server", None)
    sentinel = object()
    saved_server_attr = (
        package.__dict__.pop("server", sentinel) if package is not None else sentinel
    )

    builtins.__import__ = blocked_import
    try:
        from aiworkhub import server as server_module
    finally:
        builtins.__import__ = real_import

    yield server_module

    # Only server.py needs a fresh import to exercise its stdlib fallback.
    # Replacing the full package graph creates duplicate exception classes
    # and module singletons in already-collected tests.
    sys.modules.pop("aiworkhub.server", None)
    if saved_server_module is not None:
        sys.modules["aiworkhub.server"] = saved_server_module
    if package is not None:
        if saved_server_attr is sentinel:
            package.__dict__.pop("server", None)
        else:
            package.__dict__["server"] = saved_server_attr


def test_stdlib_fallback_engages_and_registers_canonical_dashboard_tools(fallback_server_module):
    server_module = fallback_server_module
    assert server_module.FastMCP.__module__ == "aiworkhub.server"
    names = set(server_module.mcp.registered_tools)
    for expected in (
        "aiworkhub_dashboard_snapshot",
        "aiworkhub_dashboard_task_detail",
        "aiworkhub_dashboard_health",
    ):
        assert expected in names


def test_stdlib_fallback_schema_generation_covers_every_registered_tool(fallback_server_module):
    server_module = fallback_server_module

    def assert_array_items(fragment):
        if isinstance(fragment, dict):
            if fragment.get("type") == "array":
                assert "items" in fragment
            for value in fragment.values():
                assert_array_items(value)
        elif isinstance(fragment, list):
            for value in fragment:
                assert_array_items(value)

    for name, func in server_module.mcp._tools.items():
        schema = server_module._stdio_schema_for(func)
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert_array_items(schema)


def test_stdlib_fallback_accept_review_arrays_have_copilot_compatible_items(
    fallback_server_module,
):
    server_module = fallback_server_module
    func = server_module.mcp._tools["aiworkhub_agent_accept_review"]
    properties = server_module._stdio_schema_for(func)["properties"]
    assert properties["risk_signals"]["type"] == "array"
    assert properties["risk_signals"]["items"] == {
        "type": "string",
        "enum": [
            "public_api", "combined_change", "authority_boundary", "concurrency",
            "destructive_change", "schema_migration", "security_sensitive", "release",
        ],
    }
    assert properties["reviewer_request_ids"] == {
        "type": "array", "items": {"type": "string"},
    }
    assert properties["reviewer_reports"] == {
        "type": "array",
        "items": {"type": "object", "additionalProperties": {}},
    }


def test_stdlib_fallback_source_graph_schema_is_self_describing(fallback_server_module):
    server_module = fallback_server_module
    func = server_module.mcp._tools["aiworkhub_manager_source_graph_query"]
    schema = server_module._stdio_schema_for(func)
    assert schema["properties"]["mode"]["enum"] == [
        "focus", "slice", "context", "file", "function", "class", "body", "bodygrep",
        "impact", "trace", "deps", "bundle",
        "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership",
        "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity",
        "stats", "summarize", "pipeline",
        "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes",
        "looprisks", "deadmethods", "duplicates", "gaps",
    ]
    assert schema["properties"]["bundle_type"]["enum"] == [
        "bugfix", "feature", "refactor", "audit", "optimize", "explore",
    ]


def test_stdlib_fallback_exposes_manager_semantic_edit_pair(
    fallback_server_module, monkeypatch,
):
    server_module = fallback_server_module
    tools = server_module.mcp._tools
    assert "aiworkhub_manager_semantic_edit_prepare" in tools
    assert "aiworkhub_manager_semantic_edit_apply" in tools

    monkeypatch.setattr(
        server_module.manager_ai_tools,
        "semantic_edit_prepare",
        lambda **kwargs: {"ok": True, "arguments": kwargs},
    )
    prepared = server_module._stdio_dispatch(
        "AIWorkHub MCP",
        tools,
        "tools/call",
        {
            "name": "aiworkhub_manager_semantic_edit_prepare",
            "arguments": {
                "file_path": "src/aiworkhub/core.py",
                "start_line": 1,
                "end_line": 2,
                "include_fragment": False,
            },
        },
    )["structuredContent"]
    assert prepared["arguments"]["file_path"] == "src/aiworkhub/core.py"

    monkeypatch.setattr(
        server_module.manager_ai_tools,
        "semantic_edit_apply",
        lambda **kwargs: {"ok": True, "arguments": kwargs},
    )
    applied = server_module._stdio_dispatch(
        "AIWorkHub MCP",
        tools,
        "tools/call",
        {
            "name": "aiworkhub_manager_semantic_edit_apply",
            "arguments": {
                "target_id": "target-1",
                "new": "replacement",
                "idempotency_key": "manager-edit-1",
            },
        },
    )["structuredContent"]
    assert applied["arguments"] == {
        "target_id": "target-1",
        "new": "replacement",
        "idempotency_key": "manager-edit-1",
    }


def test_stdlib_fallback_rejects_unknown_tool_and_unknown_method(fallback_server_module):
    server_module = fallback_server_module
    tools = server_module.mcp._tools
    with pytest.raises(server_module._StdioProtocolError) as unknown:
        server_module._stdio_dispatch(
            "AIWorkHub MCP", tools, "tools/call",
            {"name": "aiworkhub_dashboard_healt", "arguments": {}},
        )
    payload = json.loads(unknown.value.message)
    assert payload["retryable"] is True
    assert payload["suggestions"][0] == "aiworkhub_dashboard_health"
    assert len(payload["suggestions"]) <= 3
    with pytest.raises(server_module._StdioProtocolError):
        server_module._stdio_dispatch("AIWorkHub MCP", tools, "not/a/real/method", {})


def test_stdlib_fallback_rejects_unexpected_tool_arguments_no_dynamic_eval(fallback_server_module):
    server_module = fallback_server_module
    tools = server_module.mcp._tools
    with pytest.raises(server_module._StdioProtocolError):
        server_module._stdio_dispatch(
            "AIWorkHub MCP", tools, "tools/call",
            {
                "name": "aiworkhub_dashboard_health",
                "arguments": {"__import__": "os", "__eval__": "os.system('id')"},
            },
        )


def test_stdlib_fallback_initialize_and_tools_call_shape(fallback_server_module):
    server_module = fallback_server_module
    tools = server_module.mcp._tools
    init = server_module._stdio_dispatch("AIWorkHub MCP", tools, "initialize", {})
    assert init["protocolVersion"] == "2024-11-05"
    assert init["capabilities"]["resources"] == {}
    with_banner = server_module._stdio_dispatch(
        "AIWorkHub MCP",
        tools,
        "initialize",
        {},
        server_module.core.MCP_MANAGER_CONTRACT_BANNER,
    )
    assert "AIWORKHUB MANAGER CONTRACT" in with_banner["instructions"]
    assert "Creating a task leaves it pending" in with_banner["instructions"]
    assert "aiworkhub_manager_source_graph_query before built-in filesystem discovery" in with_banner["instructions"]
    assert server_module.mcp.instructions == server_module.core.MCP_MANAGER_CONTRACT_BANNER
    assert server_module._stdio_dispatch(
        "AIWorkHub MCP", tools, "resources/list", {}
    ) == {"resources": []}
    assert server_module._stdio_dispatch(
        "AIWorkHub MCP", tools, "resources/templates/list", {}
    ) == {"resourceTemplates": []}
    result = server_module._stdio_dispatch(
        "AIWorkHub MCP", tools, "tools/call",
        {"name": "aiworkhub_dashboard_health", "arguments": {}},
    )
    assert result.get("isError") is not True
    assert result["structuredContent"]["server_tool"] == "aiworkhub_dashboard_health"


def test_stdlib_fallback_broken_stdout_exits_cleanly_with_stderr_event(
    fallback_server_module, monkeypatch
):
    server_module = fallback_server_module

    class ClosedPipe:
        def write(self, _text):
            raise BrokenPipeError("client closed")

        def flush(self):
            raise AssertionError("flush must not run after failed write")

    stderr = io.StringIO()
    monkeypatch.setattr(server_module.sys, "stderr", stderr)
    with pytest.raises(server_module._StdioTransportClosed):
        server_module._stdio_write_message(
            ClosedPipe(), {"jsonrpc": "2.0", "id": 7, "result": {}}
        )
    event = json.loads(stderr.getvalue())
    assert event["event"] == "transport_closed"
    assert event["request_id"] == 7


def test_stdlib_fallback_task_create_then_show_uses_binary_utf8_for_georgian(
    fallback_server_module, monkeypatch
):
    """Regression: Windows text stdout may be cp1251, not UTF-8.

    Exercise the real fallback JSON-RPC dispatcher and the canonical public
    task-create/task-show tool wrappers.  The fake text wrapper deliberately
    rejects every text write, exactly as a Windows locale wrapper rejects
    Georgian.  A valid server must exclusively use its binary buffer and keep
    both responses on the same live transport.
    """

    server_module = fallback_server_module
    task_id = "CODEX_UNICODE_STDIO_REGRESSION_V1"
    title = "ქართული სათაური"
    objective = "ქართული ამოცანის შექმნა და იმავე ტრანსპორტზე წაკითხვა"
    card: dict[str, object] = {}
    card_created = threading.Event()

    def fake_create_task(**kwargs):
        card.update(kwargs)
        card_created.set()
        return {"ok": True, "created": True, "task_id": kwargs["task_id"], **kwargs}

    def fake_show_task(requested_task_id, *, full=False):
        assert card_created.wait(timeout=5), "create request did not complete"
        assert requested_task_id == task_id
        assert full is False
        return {"ok": True, "task_id": requested_task_id, "card": dict(card)}

    monkeypatch.setattr(server_module.core, "create_task", fake_create_task)
    monkeypatch.setattr(server_module.core, "show_task", fake_show_task)

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "aiworkhub_task_create",
                "arguments": {
                    "task_id": task_id,
                    "title": title,
                    "runner": "codex_unicode_test",
                    "topic": "task_mcp",
                    "objective": objective,
                    "acceptance": ["ქართული პასუხი უცვლელია"],
                    "allowed_writes": [],
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "aiworkhub_task_show",
                "arguments": {"task_id": task_id},
            },
        },
    ]
    stdin_bytes = io.BytesIO(
        b"".join(
            json.dumps(request, ensure_ascii=True).encode("ascii") + b"\n"
            for request in requests
        )
    )
    stdout_bytes = io.BytesIO()

    class LocaleBoundTextStream:
        def __init__(self, buffer):
            self.buffer = buffer

        def write(self, _text):
            raise UnicodeEncodeError("charmap", "ქართული", 0, 1, "unsupported")

        def flush(self):
            raise AssertionError("text flush must not be used")

    monkeypatch.setattr(server_module.sys, "stdin", LocaleBoundTextStream(stdin_bytes))
    monkeypatch.setattr(server_module.sys, "stdout", LocaleBoundTextStream(stdout_bytes))

    server_module._run_stdio_fallback_server(
        "AIWorkHub MCP", server_module.mcp._tools,
    )

    responses = [
        json.loads(line.decode("utf-8"))
        for line in stdout_bytes.getvalue().splitlines()
    ]
    responses_by_id = {response["id"]: response for response in responses}
    assert sorted(responses_by_id) == [1, 2]
    assert responses_by_id[1]["result"]["structuredContent"]["title"] == title
    shown = responses_by_id[2]["result"]["structuredContent"]
    assert shown["card"]["objective"] == objective


# ---------------------------------------------------------------------------
# 3. Isolated end-to-end smoke: the actual extracted VSIX runtime, -S (no
#    site-packages), a fresh repository with no checkout and no venv.
# ---------------------------------------------------------------------------


class _StdioSession:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._stdout_pending = bytearray()
        self._stderr_tail = ""
        self._stderr_lock = threading.Lock()
        self._stderr_done = threading.Event()
        if proc.stderr is None:
            self._stderr_done.set()
        else:
            # Drain concurrently: a chatty child must not block on a full pipe.
            threading.Thread(target=self._collect_stderr, daemon=True).start()

    def _collect_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            while chunk := self.proc.stderr.readline(2048):
                with self._stderr_lock:
                    self._stderr_tail = (self._stderr_tail + chunk)[-2000:]
        except Exception as exc:
            with self._stderr_lock:
                self._stderr_tail = (
                    self._stderr_tail + f"[stderr reader {type(exc).__name__}: {exc}]"
                )[-2000:]
        finally:
            self._stderr_done.set()

    def _diagnostics(self, started: float, first_byte_at: float | None) -> str:
        first_byte = (
            "None" if first_byte_at is None else f"{first_byte_at - started:.3f}s"
        )
        with self._stderr_lock:
            stderr_tail = self._stderr_tail
        return (
            f"pid={self.proc.pid} returncode={self.proc.poll()} "
            f"elapsed={time.monotonic() - started:.3f}s "
            f"first_byte={first_byte} stderr_tail={stderr_tail!r}"
        )

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = 20.0) -> dict:
        assert self.proc.stdout is not None
        started = time.monotonic()
        first_byte_at: list[float | None] = [None]
        if os.name == "nt":
            # Windows ``select`` accepts sockets only, not subprocess pipes.
            # The reader reports its exception instead of silently timing out.
            received: queue.Queue[tuple[str | None, BaseException | None]] = queue.Queue(
                maxsize=1
            )

            def read_line() -> None:
                try:
                    first = self.proc.stdout.read(1)
                    if first:
                        first_byte_at[0] = time.monotonic()
                    received.put((first + self.proc.stdout.readline() if first else "", None))
                except BaseException as exc:
                    received.put((None, exc))

            threading.Thread(target=read_line, daemon=True).start()
            try:
                line, read_error = received.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError(
                    "bundled MCP fallback runtime did not respond in time: "
                    + self._diagnostics(started, first_byte_at[0])
                ) from exc
            if read_error is not None:
                raise read_error
        else:
            # DefaultSelector handles pipe fds above FD_SETSIZE. Read bytes
            # under one deadline: TextIOWrapper.readline() can block forever
            # after select reports only the first byte of a partial line.
            deadline = started + timeout
            if self._stdout_pending:
                first_byte_at[0] = started
            with selectors.DefaultSelector() as selector:
                selector.register(self.proc.stdout, selectors.EVENT_READ)
                while b"\n" not in self._stdout_pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError(
                            "bundled MCP fallback runtime did not respond in time: "
                            + self._diagnostics(started, first_byte_at[0])
                        )
                    chunk = os.read(self.proc.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    if first_byte_at[0] is None:
                        first_byte_at[0] = time.monotonic()
                    self._stdout_pending.extend(chunk)
            end = self._stdout_pending.find(b"\n")
            if end < 0:
                end = len(self._stdout_pending) - 1
            raw_line = bytes(self._stdout_pending[:end + 1])
            del self._stdout_pending[:end + 1]
            line = raw_line.decode(
                self.proc.stdout.encoding or "utf-8",
                errors=self.proc.stdout.errors or "strict",
            )
        if line == "":
            # The child may still own stderr; never wait unboundedly for it.
            self._stderr_done.wait(timeout=0.05)
            raise EOFError(
                "bundled MCP fallback runtime exited unexpectedly: "
                + self._diagnostics(started, first_byte_at[0])
            )
        return json.loads(line)



def _diagnostic_child(code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-S", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def test_stdio_session_eof_reports_child_state_and_bounded_stderr():
    proc = _diagnostic_child(
        "import sys; sys.stderr.write('x' * 4096 + 'stderr-marker\\n'); sys.exit(7)"
    )
    try:
        session = _StdioSession(proc)
        assert proc.wait(timeout=5) == 7
        assert session._stderr_done.wait(timeout=2)
        with pytest.raises(EOFError) as raised:
            session.recv(timeout=1)
        message = str(raised.value)
        assert f"pid={proc.pid}" in message
        assert "returncode=7" in message
        assert "first_byte=None" in message
        assert "stderr-marker" in message
        assert len(message) < 3000
    finally:
        _terminate(proc)


def test_stdio_session_timeout_reports_live_child_without_output():
    proc = _diagnostic_child("import time; time.sleep(5)")
    try:
        session = _StdioSession(proc)
        with pytest.raises(TimeoutError) as raised:
            session.recv(timeout=0.05)
        message = str(raised.value)
        assert f"pid={proc.pid}" in message
        assert "returncode=None" in message
        assert "elapsed=" in message
        assert "first_byte=None" in message
    finally:
        _terminate(proc)


def test_stdio_session_timeout_reports_partial_first_byte():
    proc = _diagnostic_child(
        "import sys, time; sys.stderr.write('ready\\n'); sys.stderr.flush(); "
        "sys.stdin.readline(); sys.stdout.write('{'); sys.stdout.flush(); time.sleep(5)"
    )
    try:
        ready: queue.Queue[str] = queue.Queue(maxsize=1)
        threading.Thread(
            target=lambda: ready.put(proc.stderr.readline()), daemon=True
        ).start()
        assert ready.get(timeout=5) == "ready\n"
        session = _StdioSession(proc)
        proc.stdin.write("\n")
        proc.stdin.flush()
        with pytest.raises(TimeoutError) as raised:
            session.recv(timeout=1)
        message = str(raised.value)
        assert f"pid={proc.pid}" in message
        assert "first_byte=" in message
        assert "first_byte=None" not in message
    finally:
        _terminate(proc)


def test_stdio_session_propagates_reader_error():
    proc = _diagnostic_child("import time; time.sleep(5)")
    try:
        session = _StdioSession(proc)
        proc.stdout.close()
        with pytest.raises(ValueError):
            session.recv(timeout=1)
    finally:
        _terminate(proc)


def _spawn_bundled_runtime(runtime_dir: Path, fresh_repo: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(runtime_dir),
        "AIWORKHUB_REPO_ROOT": str(fresh_repo),
        "AIWORKHUB_REPO": str(fresh_repo),
        "AIWORKHUB_REPO_ID": "repo_" + "0" * 32,
        "AIWORKHUB_WINDOW_ID": "window_test_b844",
        "AIWORKHUB_CLAIM_EPISODE": "episode_test_b844",
    })
    env.pop("AIWORKHUB_ALLOW_WRITES", None)
    env.pop("AIWORKHUB_ALLOW_LAUNCH", None)
    # -S: skip the `site` module entirely -- neither global nor user
    # site-packages are on sys.path, so this can only succeed via the
    # extension-local bundled runtime (PYTHONPATH + cwd), and can only speak
    # MCP via the bounded stdlib fallback if `mcp` happens to be installed
    # on this machine's interpreter.
    return subprocess.Popen(
        [sys.executable, "-S", "-m", "aiworkhub.server"],
        cwd=str(runtime_dir),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def test_isolated_vsix_runtime_smoke_no_site_packages_fresh_repo(extracted_vsix, tmp_path):
    runtime_dir = extracted_vsix / "extension" / "runtime"
    assert (runtime_dir / "aiworkhub" / "server.py").is_file()

    fresh_repo = tmp_path / "fresh_repo"
    fresh_repo.mkdir()
    assert not (fresh_repo / ".git").exists()
    assert not (fresh_repo / ".venv").exists()
    assert not (fresh_repo / "AITools").exists()

    proc = _spawn_bundled_runtime(runtime_dir, fresh_repo)
    session = _StdioSession(proc)
    try:
        session.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest-isolated-smoke", "version": "1"},
            },
        })
        init = session.recv()
        assert "error" not in init, init.get("error")
        assert init["result"]["protocolVersion"] == "2024-11-05"

        session.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        session.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_list = session.recv()
        assert "error" not in tools_list, tools_list.get("error")
        names = {t["name"] for t in tools_list["result"]["tools"]}
        assert "aiworkhub_dashboard_snapshot" in names

        session.send({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "aiworkhub_dashboard_snapshot", "arguments": {}},
        })
        snapshot = session.recv(30)
        assert "error" not in snapshot, snapshot.get("error")
        result = snapshot["result"]
        assert result.get("isError") is not True
        structured = result["structuredContent"]
        assert structured["server_tool"] == "aiworkhub_dashboard_snapshot"
        assert "status_counts" in structured
        assert "row_counts" in structured
    finally:
        _terminate(proc)


def test_isolated_vsix_runtime_bounded_protocol_errors_never_crash(extracted_vsix, tmp_path):
    runtime_dir = extracted_vsix / "extension" / "runtime"
    fresh_repo = tmp_path / "fresh_repo_bounded"
    fresh_repo.mkdir()

    proc = _spawn_bundled_runtime(runtime_dir, fresh_repo)
    session = _StdioSession(proc)
    try:
        session.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        init = session.recv()
        assert "error" not in init
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        # Malformed JSON line -> structured parse error, server stays alive.
        assert proc.stdin is not None
        proc.stdin.write("{this is not json\n")
        proc.stdin.flush()
        parse_err = session.recv()
        assert parse_err.get("error", {}).get("code") == -32700

        # Oversized single line -> structured request_too_large, still alive.
        oversized = "x" * (9 * 1024 * 1024)
        proc.stdin.write(oversized + "\n")
        proc.stdin.flush()
        oversized_err = session.recv()
        assert oversized_err.get("error", {}).get("code") == -32600

        # Invalid UTF-8 is a bounded parse error, not a decoder crash.
        assert proc.stdin is not None
        proc.stdin.buffer.write(b"\xff\xfe\n")
        proc.stdin.buffer.flush()
        utf8_err = session.recv()
        assert utf8_err.get("error", {}).get("code") == -32700

        # Unknown tool name -> structured invalid_params, still alive.
        session.send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "not_a_real_tool_at_all", "arguments": {}},
        })
        unknown = session.recv()
        assert unknown.get("error", {}).get("code") == -32602

        # A real call after all of the above still succeeds -- one bad
        # request never tears down the bounded fallback's read loop. Uses
        # aiworkhub_dashboard_snapshot (not aiworkhub_dashboard_health):
        # build_snapshot() isolates every provider failure via _safe_read,
        # so it stays MCP-successful even in a fresh repo with no
        # AITools/taskctl.py, unlike the raw health check.
        session.send({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "aiworkhub_dashboard_snapshot", "arguments": {}},
        })
        snapshot = session.recv(30)
        assert "error" not in snapshot, snapshot.get("error")
        assert snapshot["result"].get("isError") is not True
    finally:
        _terminate(proc)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
