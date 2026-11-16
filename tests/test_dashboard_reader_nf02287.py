"""NF-2026-02287: the dashboard-only process serves the dashboard views itself.

``python -m aiworkhub.dashboard_reader`` must answer exactly like the
existing view callables, refuse anything outside the dashboard tool maps,
keep write-view authority checks unchanged, and never load the MCP server or
dispatcher nor leave a background thread running.
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import dashboard_mcp_app, dashboard_reader, needfix_store, shared_router, task_store

SRC_ROOT = Path(dashboard_reader.__file__).parent.parent

# The MCP server and its stdio host. Neither may be imported by the dashboard
# process. View modules import helper modules such as task_reconciler; what
# must never happen is that one of them starts work, which the one-thread
# check and the unregistered dispatcher below prove.
FORBIDDEN_MODULES = (
    "aiworkhub.server",
    "aiworkhub.stdio_fastmcp",
)
PROBE_MARKER = "NF02287_PROBE "


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.delenv("AIWORKHUB_WINDOW_ID", raising=False)
    # ``known_repositories`` in the snapshot reads the host-wide shared repo
    # route registry (``~/.aiworkhub/router/repos``), which other windows and
    # tests mutate between the two reads being compared. Point it at an empty
    # per-test directory so both reads see the same registry.
    registry = tmp_path / "router_registry"
    registry.mkdir()
    monkeypatch.setattr(shared_router, "registry_dir", lambda home=None: registry)
    dashboard_mcp_app._SNAPSHOT_BUILD_CACHE.clear()
    return root


@pytest.fixture
def stable_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the inputs both calls read, so only the transport differs.

    One real ``dashboard.build_snapshot`` result per mode is captured and
    replayed: the builder carries process-wide cache hit/miss counters
    (``read_bounds``, ``cost_usage.cache``) that move on every call regardless
    of which process or transport asks. The storage telemetry sample taken
    after the build is frozen for the same reason.
    """
    from aiworkhub import dashboard, storage_observability

    frozen = {
        "scan_status": "ready",
        "scanned_at": "2026-11-16T00:00:00+00:00",
        "repo_data_bytes": 1,
        "repo_data_files": 1,
        "worker_tree_bytes": 0,
        "worker_tree_count": 0,
        "quarantine_bytes": 0,
        "safe_reclaimable_bytes": 0,
        "managed_total_bytes": 1,
        "errors": [],
    }
    monkeypatch.setattr(storage_observability, "snapshot", lambda _root: dict(frozen))
    real_build = dashboard.build_snapshot
    captured: dict[bool, Any] = {}

    def replayed_build(**kwargs: Any) -> Any:
        mode = bool(kwargs.get("summary_only"))
        if mode not in captured:
            captured[mode] = real_build(**kwargs)
        return copy.deepcopy(captured[mode])

    monkeypatch.setattr(dashboard, "build_snapshot", replayed_build)


def _serve(lines: list[Any]) -> list[dict[str, Any]]:
    """Drive the real line protocol over in-memory streams."""
    payload = b"".join(
        (line if isinstance(line, bytes) else json.dumps(line).encode("utf-8")) + b"\n"
        for line in lines
    )
    stdout = io.BytesIO()
    assert dashboard_reader.serve(io.BytesIO(payload), stdout, dashboard_reader.dashboard_tools()) == 0
    return [json.loads(raw) for raw in stdout.getvalue().splitlines()]


def _wire(value: Any) -> Any:
    """The value as the extension receives it from either transport."""
    return json.loads(json.dumps(value, default=str))


def _digests(payload: dict[str, Any]) -> dict[str, str]:
    return {key: dashboard_mcp_app._snapshot_section_digest(value) for key, value in payload.items()}


def test_served_tools_are_exactly_the_named_dashboard_maps() -> None:
    tools = dashboard_reader.dashboard_tools()
    expected: dict[str, Any] = {}
    for map_name in dashboard_reader.DASHBOARD_TOOL_MAP_NAMES:
        expected.update(getattr(dashboard_mcp_app, map_name))
    assert tools == expected
    assert tools["aiworkhub_dashboard_snapshot"] is dashboard_mcp_app.snapshot_view
    assert not set(tools) & set(dashboard_mcp_app.TASK_RETENTION_WRITE_TOOLS)


@pytest.mark.parametrize("full", [True, False])
def test_snapshot_reply_equals_snapshot_view(repo: Path, stable_storage: None, full: bool) -> None:
    direct = _wire(dashboard_mcp_app.snapshot_view(full=full))
    (reply,) = _serve([{"id": 7, "tool": "aiworkhub_dashboard_snapshot", "arguments": {"full": full}}])
    assert reply["id"] == 7
    assert "error" not in reply, reply
    served, expected = _digests(reply["result"]), _digests(direct)
    differing = sorted(key for key in set(served) | set(expected) if served.get(key) != expected.get(key))
    assert differing == [], {
        key: (str(direct.get(key))[:600], str(reply["result"].get(key))[:600]) for key in differing
    }
    assert reply["result"] == direct
    assert _digests(reply["result"]) == _digests(direct)
    assert reply["result"].get("snapshot_mode") == ("full" if full else direct.get("snapshot_mode"))


def test_snapshot_delta_protocol_is_kept(repo: Path, stable_storage: None) -> None:
    (first,) = _serve([{"id": 1, "tool": "aiworkhub_dashboard_snapshot", "arguments": {"full": True}}])
    digests = _digests(first["result"])
    (delta,) = _serve([{
        "id": 2,
        "tool": "aiworkhub_dashboard_snapshot",
        "arguments": {"full": True, "previous": {dashboard_mcp_app.SNAPSHOT_SECTION_DIGESTS_FIELD: digests}},
    }])
    result = delta["result"]
    assert result["snapshot_mode"] == "delta"
    assert result["delta_schema"] == dashboard_mcp_app.SNAPSHOT_DELTA_SCHEMA
    assert set(result[dashboard_mcp_app.SNAPSHOT_SECTION_DIGESTS_FIELD]) == set(digests)
    for key, value in result["changed_sections"].items():
        assert result[dashboard_mcp_app.SNAPSHOT_SECTION_DIGESTS_FIELD][key] == (
            dashboard_mcp_app._snapshot_section_digest(value)
        )


def test_needfix_transition_write_round_trips(repo: Path) -> None:
    seed = needfix_store.add_needfix(repo, title="Reader fixture", description="Round-trip row")
    needfix_id = str(seed["id"])
    before = needfix_store.get_needfix(repo, needfix_id)["status"]
    refused, applied, detail = _serve([
        {"id": 1, "tool": "aiworkhub_dashboard_needfix_transition",
         "arguments": {"needfix_id": needfix_id, "action": "triage"}},
        {"id": 2, "tool": "aiworkhub_dashboard_needfix_transition",
         "arguments": {"needfix_id": needfix_id, "action": "triage", "confirm": True}},
        {"id": 3, "tool": "aiworkhub_dashboard_needfix_detail", "arguments": {"needfix_id": needfix_id}},
    ])
    # The view's own confirmation rule is unchanged behind the reader.
    assert refused["result"]["ok"] is False
    assert refused["result"]["error"] == "needfix_transition_confirmation_required"
    assert applied["id"] == 2 and applied["result"]["ok"] is True, applied
    after = needfix_store.get_needfix(repo, needfix_id)["status"]
    assert after != before
    assert detail["result"]["ok"] is True
    assert json.dumps(detail["result"]).count(after) >= 1


def test_unknown_or_malformed_requests_are_refused(repo: Path) -> None:
    replies = _serve([
        {"id": 1, "tool": "aiworkhub_task_create", "arguments": {}},
        {"id": 2, "tool": "aiworkhub_dashboard_task_restart", "arguments": {"task_id": "x"}},
        {"id": 3, "tool": "aiworkhub_dashboard_health", "arguments": ["not", "a", "mapping"]},
        {"id": True, "tool": "aiworkhub_dashboard_health"},
        {"id": 5, "tool": "aiworkhub_dashboard_health", "arguments": {"no_such_argument": 1}},
        b"{not json",
        [1, 2, 3],
    ])
    codes = [(reply["id"], reply["error"]["code"]) for reply in replies]
    assert codes == [
        (1, "tool_not_allowed"),
        (2, "tool_not_allowed"),
        (3, "arguments_invalid"),
        (None, "request_id_invalid"),
        (5, "arguments_invalid"),
        (None, "request_invalid_json"),
        (None, "request_not_object"),
    ]
    assert all("result" not in reply for reply in replies)


def test_oversized_request_is_refused_and_the_stream_continues(repo: Path) -> None:
    huge = b'{"id": 1, "tool": "' + b"x" * (dashboard_reader.MAX_REQUEST_LINE_BYTES + 16) + b'"}'
    oversized, health = _serve([huge, {"id": 2, "tool": "aiworkhub_dashboard_health"}])
    assert oversized["error"] == {"code": "request_too_large", "limit": dashboard_reader.MAX_REQUEST_LINE_BYTES}
    assert health["id"] == 2 and "result" in health


def test_process_loads_no_server_or_dispatcher_and_leaves_one_thread(repo: Path, tmp_path: Path) -> None:
    driver = (
        "import json, sys, threading\n"
        "from aiworkhub import dashboard_reader\n"
        "code = dashboard_reader.main(sys.argv[1:])\n"
        "from aiworkhub import callback_bridge\n"
        "import pathlib\n"
        "sys.stderr.write(" + repr(PROBE_MARKER) + " + json.dumps({\n"
        "    'code': code,\n"
        "    'threads': threading.active_count(),\n"
        "    'thread_names': [t.name for t in threading.enumerate()],\n"
        "    'modules': sorted(name for name in sys.modules if name.startswith('aiworkhub')),\n"
        "    'dispatcher_registered': callback_bridge.get_dispatcher(pathlib.Path(sys.argv[2])) is not None,\n"
        "}) + '\\n')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_ROOT), env.get("PYTHONPATH", "")]))
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("AIWORKHUB_MCP_SURFACE", "AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_ALLOW_LAUNCH"):
        env.pop(key, None)
    request = json.dumps({"id": 1, "tool": "aiworkhub_dashboard_snapshot", "arguments": {"full": True}}) + "\n"
    completed = subprocess.run(
        [sys.executable, "-c", driver, "--repo", str(repo)],
        input=request.encode("utf-8"),
        capture_output=True,
        cwd=str(tmp_path),
        env=env,
        timeout=600,
        check=False,
    )
    stderr = completed.stderr.decode("utf-8", "replace")
    assert completed.returncode == 0, stderr[-4000:]
    replies = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines() if line.strip()]
    assert len(replies) == 1 and replies[0]["id"] == 1, completed.stdout[-2000:]
    assert replies[0]["result"]["snapshot_mode"] == "full"
    probe_lines = [line for line in stderr.splitlines() if line.startswith(PROBE_MARKER)]
    assert len(probe_lines) == 1, stderr[-4000:]
    probe = json.loads(probe_lines[0][len(PROBE_MARKER):])
    assert probe["code"] == 0
    loaded = set(probe["modules"])
    assert not loaded & set(FORBIDDEN_MODULES), sorted(loaded & set(FORBIDDEN_MODULES))
    assert probe["dispatcher_registered"] is False
    assert probe["threads"] == 1, probe["thread_names"]
