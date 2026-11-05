"""NF-2026-02092: an apply receipt names the bytes it actually replaced.

Two ranges of one file are prepared, then applied in turn.  The second apply
reads a file the first one already changed, so its ``before_sha256`` must be
the first apply's ``after_sha256`` -- not the hash prepare saw.  The prepare
time hash is still reported, as ``prepared_sha256`` with ``rebased: true``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import stdio_fastmcp
from aiworkhub import worker_ai_tools_mcp as worker_tools

_PATH = "src/module.py"
_PREPARE = "aiworkhub_worker_semantic_edit_prepare"
_APPLY = "aiworkhub_worker_semantic_edit_apply"
_TEXT = b"alpha = 1\nbeta = 2\ngamma = 3\ndelta = 4\n"
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "manager_semantic_edit.py"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Client:
    def __init__(self, root: Path) -> None:
        self.server = stdio_fastmcp.FallbackFastMCP("nf02092")
        worker_tools.register_tools(
            self.server,
            worker_tools.WorkerToolContext(
                task_id="TASK", runner="runner", topic="topic", request_id="request",
                repo=root, authority_repo=root, source_graph_targets=(_PATH,),
                session_topic="topic", audit_ledger_path=None,
                audit_hmac_key_path=None, allowed_writes=("src/*.py",),
            ),
        )

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        reply = stdio_fastmcp._dispatch(
            self.server.name, self.server._tools, "tools/call",
            {"name": tool, "arguments": arguments},
        )
        assert "isError" not in reply, reply
        return json.loads(reply["content"][0]["text"])

    def prepare(self, line: int, text: str) -> dict[str, Any]:
        reply = self.call(
            _PREPARE, file_path=_PATH, start_line=line, end_line=line,
            expected_first_line=text, expected_last_line=text,
        )
        assert reply["ok"] is True, reply
        return reply


def test_second_apply_reports_the_bytes_the_first_apply_left(tmp_path: Path) -> None:
    target = tmp_path / _PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(_TEXT)
    client = _Client(tmp_path)
    prepared_a = client.prepare(2, "beta = 2")
    prepared_b = client.prepare(4, "delta = 4")
    assert prepared_a["current_sha256"] == prepared_b["current_sha256"] == _sha(_TEXT)

    first = client.call(
        _APPLY, target_id=prepared_a["target_id"], new="beta = 20", idempotency_key="a",
    )
    second = client.call(
        _APPLY, target_id=prepared_b["target_id"], new="delta = 40", idempotency_key="b",
    )

    assert first["ok"] is True and second["ok"] is True, (first, second)
    assert target.read_bytes() == b"alpha = 1\nbeta = 20\ngamma = 3\ndelta = 40\n"
    # The first apply read exactly what prepare bound: nothing was rebased.
    assert first["before_sha256"] == _sha(_TEXT)
    assert "rebased" not in first and "prepared_sha256" not in first
    # The second read the first one's output.
    assert second["before_sha256"] == first["after_sha256"]
    assert second["after_sha256"] == _sha(target.read_bytes())
    assert second["prepared_sha256"] == _sha(_TEXT)
    assert second["rebased"] is True
    assert second["file_bytes"] == len(b"alpha = 1\nbeta = 20\ngamma = 3\ndelta = 4\n")


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("manager_semantic_edit_nf02092", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manager_script_before_sha256_is_the_file_it_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "doc.txt"
    target.write_bytes(_TEXT)
    script = _load_script()
    outputs: list[dict[str, Any]] = []
    for line, new in ((2, "beta = 20"), (4, "delta = 40")):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(new.encode())))
        code = script.main(
            ["--repo", str(tmp_path), "--path", "doc.txt",
             "--start", str(line), "--end", str(line)],
        )
        assert code == 0
        outputs.append(json.loads(capsys.readouterr().out))

    first, second = outputs
    assert first["before_sha256"] == _sha(_TEXT)
    assert "rebased" not in first
    assert second["before_sha256"] == _sha(b"alpha = 1\nbeta = 20\ngamma = 3\ndelta = 4\n")
    assert "rebased" not in second
    assert target.read_bytes() == b"alpha = 1\nbeta = 20\ngamma = 3\ndelta = 40\n"


def test_manager_script_reports_the_prepared_hash_when_the_file_moved_after_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "doc.txt"
    target.write_bytes(_TEXT)
    script = _load_script()
    real = script.semantic_edit.prepare_line_target
    moved = b"alpha = 1\nbeta = 2\ngamma = 3\ndelta = 4\nextra = 5\n"

    def prepare_then_append(*args: Any, **kwargs: Any) -> Any:
        prepared = real(*args, **kwargs)
        target.write_bytes(moved)  # another writer lands after prepare
        return prepared

    monkeypatch.setattr(script.semantic_edit, "prepare_line_target", prepare_then_append)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"beta = 20")))

    assert script.main(
        ["--repo", str(tmp_path), "--path", "doc.txt", "--start", "2", "--end", "2"],
    ) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["before_sha256"] == _sha(moved)
    assert receipt["prepared_sha256"] == _sha(_TEXT)
    assert receipt["rebased"] is True
