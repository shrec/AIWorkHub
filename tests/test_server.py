from __future__ import annotations

import builtins
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import server  # noqa: E402


def test_stdlib_backend_can_be_selected_even_when_sdk_is_installed() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    env["AIWORKHUB_MCP_STDIO_BACKEND"] = "stdlib"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from aiworkhub import server; print(server._MCP_SDK_AVAILABLE)",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert completed.stdout.strip() == "False"


def test_manager_archive_and_supersede_tools_are_write_gated(monkeypatch) -> None:
    monkeypatch.setattr(server.core, "writes_allowed", lambda: False)

    archive = server.aiworkhub_manager_task_archive("TASK_B891", reason="done")
    supersede = server.aiworkhub_manager_task_supersede("TASK_B891", reason="orphan")

    assert archive == {"ok": False, "error": "write_gate_closed", "task_id": "TASK_B891"}
    assert supersede == {"ok": False, "error": "write_gate_closed", "task_id": "TASK_B891"}


def test_manager_archive_and_supersede_dispatch_to_repo_bound_engine(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(server.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(server.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(server.core, "CODEX_RUNNER", "codex")

    def fake_archive(repo, task_id, *, actor, reason="", supersede=False):
        calls.append({
            "repo": repo,
            "task_id": task_id,
            "actor": actor,
            "reason": reason,
            "supersede": supersede,
        })
        return {"ok": True, "returncode": 0}

    monkeypatch.setattr(server.task_engine, "archive_task", fake_archive)

    assert server.aiworkhub_manager_task_archive("TASK_ARCHIVE", reason="done")["ok"] is True
    assert server.aiworkhub_manager_task_supersede("TASK_SUPERSEDE", reason="orphan")["ok"] is True
    assert calls == [
        {
            "repo": tmp_path,
            "task_id": "TASK_ARCHIVE",
            "actor": "codex",
            "reason": "done",
            "supersede": False,
        },
        {
            "repo": tmp_path,
            "task_id": "TASK_SUPERSEDE",
            "actor": "codex",
            "reason": "orphan",
            "supersede": True,
        },
    ]


def test_fallback_stdio_writer_is_binary_utf8_and_transport_safe() -> None:
    """Exercise the real fallback writer without replacing live modules."""
    import uuid

    package_path = _SRC / "aiworkhub"
    server_path = package_path / "server.py"
    package_name = f"_test_aiworkhub_{uuid.uuid4().hex}"
    module_name = f"{package_name}.server"
    original_import = builtins.__import__
    saved_stderr = sys.stderr

    def selective_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mcp.server.fastmcp" or name.startswith("mcp.server.fastmcp."):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return original_import(name, globals, locals, fromlist, level)

    try:
        builtins.__import__ = selective_import
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            package_path / "__init__.py",
            submodule_search_locations=[str(package_path)],
        )
        assert package_spec is not None and package_spec.loader is not None
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        package_spec.loader.exec_module(package)

        spec = importlib.util.spec_from_file_location(module_name, server_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        georgian_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"text": "გამარჯობა →"},
        }
        stream = io.BytesIO()
        module._stdio_write_message(stream, georgian_message)
        emitted = stream.getvalue()
        assert emitted.endswith(b"\n")
        assert emitted.count(b"\n") == 1
        assert json.loads(emitted.decode("utf-8")) == georgian_message

        ascii_message = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"text": "hello world"},
        }
        expected_ascii = (
            json.dumps(ascii_message, ensure_ascii=False, default=str).encode("utf-8")
            + b"\n"
        )
        ascii_stream = io.BytesIO()
        module._stdio_write_message(ascii_stream, ascii_message)
        assert ascii_stream.getvalue() == expected_ascii

        class BrokenStream:
            def write(self, _data):
                raise BrokenPipeError()

            def flush(self):
                return None

        stderr = io.StringIO()
        sys.stderr = stderr
        try:
            module._stdio_write_message(
                BrokenStream(),
                {"jsonrpc": "2.0", "id": 3, "result": {}},
            )
            raise AssertionError("expected _StdioTransportClosed")
        except module._StdioTransportClosed as exc:
            assert exc.code == 0

        record = json.loads(stderr.getvalue().strip())
        assert record == {
            "component": "aiworkhub.mcp_stdio",
            "event": "transport_closed",
            "request_id": 3,
            "error_type": "BrokenPipeError",
        }
    finally:
        builtins.__import__ = original_import
        sys.stderr = saved_stderr
        for name in list(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)
