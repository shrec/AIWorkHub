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

import json
import json
import os
import select
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]  # tools/geoai-task-mcp
SRC = REPO / "src"
EXT_DIR = REPO / "vscode-extension"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _vsix_path() -> Path:
    """Always (re)build the VSIX from the CURRENT source and return exactly that
    freshly-built artifact.

    An earlier version globbed ``dist/aiworkhub-*.vsix`` and returned
    ``sorted(...)[-1]``. That sort is lexicographic, so a dist/ holding many
    historical builds returned e.g. ``aiworkhub-0.6.9.vsix`` ("...-0.6.9" sorts
    after "...-0.6.31") -- a stale bundle. The test then validated OLD bundled
    output and silently passed local source changes, while CI (with an empty
    dist/) built fresh and failed. Building fresh every time makes this test
    validate the current extension.js/runtime deterministically on every host.
    """
    dist = EXT_DIR / "dist"
    version = json.loads((EXT_DIR / "package.json").read_text(encoding="utf-8"))["version"]
    subprocess.run(
        ["node", "test/package-vsix.js"], cwd=str(EXT_DIR), check=True, timeout=120
    )
    built = dist / f"aiworkhub-{version}.vsix"
    assert built.is_file(), f"packaging step did not produce {built.name}"
    return built


@pytest.fixture(scope="module")
def extracted_vsix(tmp_path_factory):
    vsix = _vsix_path()
    dest = tmp_path_factory.mktemp("vsix_extract")
    with zipfile.ZipFile(vsix) as zf:
        zf.extractall(dest)
    return dest


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
    for name, func in server_module.mcp._tools.items():
        schema = server_module._stdio_schema_for(func)
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)


def test_stdlib_fallback_source_graph_schema_is_self_describing(fallback_server_module):
    server_module = fallback_server_module
    func = server_module.mcp._tools["aiworkhub_manager_source_graph_query"]
    schema = server_module._stdio_schema_for(func)
    assert schema["properties"]["mode"]["enum"] == ["focus", "slice", "bundle"]
    assert schema["properties"]["bundle_type"]["enum"] == [
        "bugfix", "feature", "refactor", "audit", "optimize", "explore",
    ]


def test_stdlib_fallback_rejects_unknown_tool_and_unknown_method(fallback_server_module):
    server_module = fallback_server_module
    tools = server_module.mcp._tools
    with pytest.raises(server_module._StdioProtocolError):
        server_module._stdio_dispatch(
            "AIWorkHub MCP", tools, "tools/call",
            {"name": "definitely_not_a_real_tool", "arguments": {}},
        )
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
    result = server_module._stdio_dispatch(
        "AIWorkHub MCP", tools, "tools/call",
        {"name": "aiworkhub_dashboard_health", "arguments": {}},
    )
    assert result.get("isError") is not True
    assert result["structuredContent"]["server_tool"] == "aiworkhub_dashboard_health"


# ---------------------------------------------------------------------------
# 3. Isolated end-to-end smoke: the actual extracted VSIX runtime, -S (no
#    site-packages), a fresh repository with no checkout and no venv.
# ---------------------------------------------------------------------------


class _StdioSession:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = 20.0) -> dict:
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("bundled MCP fallback runtime did not respond in time")
        line = self.proc.stdout.readline()
        if line == "":
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise EOFError(f"bundled MCP fallback runtime exited unexpectedly: {stderr[-2000:]}")
        return json.loads(line)


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
