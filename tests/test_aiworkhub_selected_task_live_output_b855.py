"""B855: bounded, single-task Live Output read.

Covers ``aiworkhub.process_launcher.read_live_output_for_task`` (the
low-level implementation -- deliberately kept in process_launcher.py, not
dashboard.py, to avoid a circular import since dashboard.py already imports
process_launcher) and the ``aiworkhub_dashboard_task_live_output`` MCP tool
in ``aiworkhub.dashboard_mcp_app``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Import directly -- a plain `import aiworkhub.dashboard_mcp_app` already
# succeeds in this worktree (every sibling module is present); the sibling-
# stub bridge from test_aiworkhub_vscode_release_b853.py is only needed when
# that import genuinely fails, which it does not here.
from aiworkhub import dashboard_mcp_app, process_launcher  # noqa: E402


def _write_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _seed_task(
    tmp_path: Path,
    task_id: str,
    *,
    request_id: str = "req-1",
    stdout_text: str = "",
    stderr_text: str = "",
    state: str = "running",
    include_paths: bool = True,
) -> Path:
    log_path = tmp_path / "logs" / "process_events.jsonl"
    stdout_path = tmp_path / f"{request_id}.stdout.log"
    stderr_path = tmp_path / f"{request_id}.stderr.log"
    if include_paths:
        stdout_path.write_bytes(stdout_text.encode("utf-8"))
        stderr_path.write_bytes(stderr_text.encode("utf-8"))
    event = {
        "request_id": request_id,
        "task_id": task_id,
        "state": state,
        "started_at": "2026-01-01T00:00:00+00:00",
        "timestamp": "2026-01-01T00:00:01+00:00",
    }
    if include_paths:
        event["stdout_path"] = str(stdout_path)
        event["stderr_path"] = str(stderr_path)
    _write_event(log_path, event)
    return log_path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv(process_launcher.PROCESS_LOG_ENV, str(tmp_path / "logs" / "process_events.jsonl"))
    return tmp_path


def test_normal_tail_read(repo) -> None:
    _seed_task(repo, "TASK_B855_NORMAL", stdout_text="hello world\n")
    result = process_launcher.read_live_output_for_task("TASK_B855_NORMAL", repo=repo, cursor=0)
    assert result["ok"] is True
    assert "hello world" in result["output"]
    assert result["next_cursor"] == len("hello world\n")
    assert result["truncated"] is False


def test_cursor_incremental_refresh(repo) -> None:
    _seed_task(repo, "TASK_B855_INCR", stdout_text="first-chunk\n")
    first = process_launcher.read_live_output_for_task("TASK_B855_INCR", repo=repo, cursor=0)
    assert first["output"].strip() == "first-chunk"
    cursor = first["next_cursor"]

    # Append more bytes to the same stdout log, as a live worker would.
    stdout_path = repo / "req-1.stdout.log"
    with stdout_path.open("ab") as fh:
        fh.write(b"second-chunk\n")

    second = process_launcher.read_live_output_for_task("TASK_B855_INCR", repo=repo, cursor=cursor)
    assert second["ok"] is True
    assert "second-chunk" in second["output"]
    assert "first-chunk" not in second["output"]
    assert second["next_cursor"] > cursor


def test_ansi_stripping(repo) -> None:
    ansi_text = "\x1b[31mred text\x1b[0m plain\n"
    _seed_task(repo, "TASK_B855_ANSI", stdout_text=ansi_text)
    result = process_launcher.read_live_output_for_task("TASK_B855_ANSI", repo=repo, cursor=0)
    assert "\x1b[" not in result["output"]
    assert "red text" in result["output"]
    assert "plain" in result["output"]


def test_html_escaping() -> None:
    escaped = process_launcher._sanitize_live_output_text("<script>alert(1)</script>")
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


def test_secret_redaction(repo) -> None:
    secret = "abcd1234efgh5678ijkl9012mnop"  # 28 chars, alnum-only
    _seed_task(repo, "TASK_B855_SECRET", stdout_text=f"token={secret} end\n")
    result = process_launcher.read_live_output_for_task("TASK_B855_SECRET", repo=repo, cursor=0)
    assert secret not in result["output"]
    assert "redacted" in result["output"]


def test_bound_and_truncation(repo) -> None:
    big_text = "x" * 5000
    _seed_task(repo, "TASK_B855_BIG", stdout_text=big_text)
    result = process_launcher.read_live_output_for_task(
        "TASK_B855_BIG", repo=repo, cursor=0, max_bytes=1024
    )
    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["next_cursor"] == 1024


def test_output_unavailable_unknown_task(repo) -> None:
    result = process_launcher.read_live_output_for_task("TASK_B855_UNKNOWN", repo=repo, cursor=0)
    assert result["ok"] is False
    assert result["error"] == "output_unavailable"
    assert result["reason"] == "no_process_log_record_for_task"


def test_output_unavailable_no_log_path(repo) -> None:
    _seed_task(repo, "TASK_B855_NOPATH", include_paths=False)
    result = process_launcher.read_live_output_for_task("TASK_B855_NOPATH", repo=repo, cursor=0)
    assert result["ok"] is False
    assert result["reason"] == "no_log_path_recorded"


def test_output_unavailable_deleted_log_file(repo) -> None:
    _seed_task(repo, "TASK_B855_DELETED", stdout_text="will be deleted\n")
    (repo / "req-1.stdout.log").unlink()
    (repo / "req-1.stderr.log").unlink()
    result = process_launcher.read_live_output_for_task("TASK_B855_DELETED", repo=repo, cursor=0)
    assert result["ok"] is False
    assert result["reason"] == "log_file_missing"


def test_single_task_only_no_fanout(repo, monkeypatch) -> None:
    """Seeding a SECOND unrelated task's log must never be tailed when
    reading the first task's live output."""
    _seed_task(repo, "TASK_B855_TARGET", request_id="req-target", stdout_text="target output\n")
    _seed_task(repo, "TASK_B855_OTHER", request_id="req-other", stdout_text="other output\n")

    read_paths: list[str] = []
    real_read_byte_range = process_launcher._read_byte_range

    def _tracking_read_byte_range(path, offset, length):
        read_paths.append(str(path))
        return real_read_byte_range(path, offset, length)

    monkeypatch.setattr(process_launcher, "_read_byte_range", _tracking_read_byte_range)

    result = process_launcher.read_live_output_for_task("TASK_B855_TARGET", repo=repo, cursor=0)
    assert result["ok"] is True
    assert "target output" in result["output"]
    assert all("req-other" not in path for path in read_paths)
    assert all("req-target" in path for path in read_paths)


def test_mcp_tool_invalid_task_id() -> None:
    result = dashboard_mcp_app.task_live_output_view("not a valid id!!")
    assert result["ok"] is False
    assert result["error"] == "invalid_task_id"
    assert result["server_tool"] == "aiworkhub_dashboard_task_live_output"


def test_mcp_tool_registered() -> None:
    assert dashboard_mcp_app.LIVE_OUTPUT_TOOL_NAME == "aiworkhub_dashboard_task_live_output"
    assert dashboard_mcp_app.LIVE_OUTPUT_TOOLS["aiworkhub_dashboard_task_live_output"] is (
        dashboard_mcp_app.task_live_output_view
    )
    registered = dashboard_mcp_app.register(_FakeMcp())
    assert "aiworkhub_dashboard_task_live_output" in registered


class _FakeMcp:
    def tool(self, name: str):  # noqa: ARG002
        def _decorator(fn):
            return fn

        return _decorator


def test_mcp_tool_functional(repo, monkeypatch) -> None:
    _seed_task(repo, "TASK_B855_MCP", stdout_text="mcp path output\n")
    monkeypatch.setattr(dashboard_mcp_app.dashboard, "_default_repo_root", lambda: repo)
    result = dashboard_mcp_app.task_live_output_view("TASK_B855_MCP", cursor=0)
    assert result["ok"] is True
    assert "mcp path output" in result["output"]
    assert result["authority_flags"]["readonly"] is True


def test_mcp_tool_uses_small_incremental_chunk(repo, monkeypatch) -> None:
    payload = "x" * (dashboard_mcp_app.MAX_LIVE_OUTPUT_BYTES + 100)
    _seed_task(repo, "TASK_B855_MCP_BOUND", stdout_text=payload)
    monkeypatch.setattr(dashboard_mcp_app.dashboard, "_default_repo_root", lambda: repo)

    result = dashboard_mcp_app.task_live_output_view(
        "TASK_B855_MCP_BOUND", cursor=0
    )

    assert result["ok"] is True
    assert len(result["output"]) <= dashboard_mcp_app.MAX_LIVE_OUTPUT_BYTES
    assert result["next_cursor"] == dashboard_mcp_app.MAX_LIVE_OUTPUT_BYTES
    assert result["truncated"] is True


def test_webview_normalizer_handles_nested_provider_json_without_visible_raw_dump() -> None:
    """Regression guard for Claude/Codex/DeepSeek/GLM stringified envelopes.

    The browser behavior itself is covered by the extension's JS syntax
    check; these source assertions ensure the formatter keeps the recursive
    JSON parser, readable unknown-event fallback, and newest-first ordering.
    """
    app_source = (
        _TOOL_ROOT / "vscode-extension" / "media" / "app.js"
    ).read_text(encoding="utf-8")
    assert "function parseNestedJson(" in app_source
    assert "const parsed = parseNestedJson(line);" in app_source
    assert "Provider emitted an unsupported event shape" in app_source
    assert "for (const event of events.slice(-200).reverse())" in app_source
    assert "raw: safeRawEvent(" in app_source
