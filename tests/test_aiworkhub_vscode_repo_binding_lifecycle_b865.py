"""B865: close VS Code repository binding and child-process lifecycle.

Covers the acceptance surface owned by this card, layered on top of the
already-shipped B850 (activation-time no-bootstrap) / B855 (Live Output) /
B857 (dispatcher lifecycle) work:

1. MCP health after activation/reload reports the exact active repo root,
   repo_id, storage readiness and server version 0.6.2 -- never
   ``repo_root_not_selected`` once a repository is actually bound.
2. Editor-tab deserialization reconnects automatically and converges the ONE
   lifecycle-owned dispatcher for the bound repository (static contract on
   ``reviveDashboardPanel``/``_handshake`` in extension.js; functional
   contract on ``core.dispatcher_ensure_started`` here).
3. Deactivate, reload and repository switch stop the old dispatcher and
   terminate the exact child process before starting a new one -- no orphan
   0.5-era runtime survives (static contract on ``deactivate``/
   ``getMcpClient``/``selectRepositoryCommand``/``stopDispatcherThenTerminate``
   in extension.js; functional contract on the dispatcher registry here).
4. Only ``.aiworkhub`` is ever read/created; ``.aiworkinghub`` never appears
   anywhere in the extension host or the Python backend.
5. Init Repo is gated to a genuinely uninitialized repository
   (``is_not_initialized_reason``), never a corrupt/mismatched one.
6. ``register()`` keeps binding the full read-only + live-output +
   initialize tool surface the Webview depends on.

Every dispatcher/repo fixture below uses its own ``tmp_path`` so this file
stays parallel-safe (see the Parallel-Tests-First rule): no test shares a
canonical DB, outbox, or dispatcher registry key with another.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from aiworkhub import callback_bridge, core, dashboard_mcp_app, task_store  # noqa: E402

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_EXTENSION_JS_PATH = _TOOL_ROOT / "vscode-extension" / "extension.js"
_EXTENSION_JS = _EXTENSION_JS_PATH.read_text(encoding="utf-8")
_DASHBOARD_MCP_APP_SRC = (_SRC / "aiworkhub" / "dashboard_mcp_app.py").read_text(encoding="utf-8")
_CALLBACK_BRIDGE_SRC = (_SRC / "aiworkhub" / "callback_bridge.py").read_text(encoding="utf-8")

_REPO_ID_RE = re.compile(r"^repo_[a-f0-9]{32}$")


def _slice(source: str, marker: str, span: int) -> str:
    start = source.index(marker)
    return source[start : start + span]


# ---------------------------------------------------------------------------
# 1. MCP health reports exact active repo root, repo_id, storage readiness,
#    server version 0.6.2 -- never repo_root_not_selected once bound.
# ---------------------------------------------------------------------------

def test_health_view_reports_full_identity_after_repo_bind(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    init = task_store.initialize_repository(root)
    assert init["ok"], init
    # The extension always spawns the child with both env vars pointing at
    # the same bound repository (see McpStdioClient._start in extension.js).
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "window_b865_test")

    result = dashboard_mcp_app.health_view()

    assert result["ok"] is True
    assert result.get("error") != "repo_root_not_selected"
    assert result["repo"] == str(root)
    assert result["storage"]["ready"] is True
    assert _REPO_ID_RE.match(result["storage"]["repo_id"])
    assert result["server_version"] == "0.6.8"


def test_health_view_accepts_manager_mux_repo_binding(tmp_path, monkeypatch):
    root = tmp_path / "repo_manager_mux"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))

    result = dashboard_mcp_app.health_view()

    assert result["ok"] is True
    assert result["repo"] == str(root)
    assert result["storage"]["ready"] is True
    assert result["server_tool"] == "aiworkhub_dashboard_health"
    assert result["dispatcher"]["ok"] is True


def test_health_view_reports_repo_root_not_selected_before_any_bind(monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    result = dashboard_mcp_app.health_view()
    assert result["ok"] is False
    assert result["error"] == "repo_root_not_selected"


# ---------------------------------------------------------------------------
# 2 & 3. Dispatcher convergence per repository + clean stop on
#    switch/deactivate -- exercises the exact core/callback_bridge path the
#    extension calls after every MCP handshake and on repo switch/teardown.
# ---------------------------------------------------------------------------

def test_dispatcher_ensure_started_converges_then_stop_unregisters(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    init = task_store.initialize_repository(root)
    assert init["ok"], init
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "window_b865_dispatcher")
    try:
        started = core.dispatcher_ensure_started()
        assert started["ok"] is True
        assert started["dispatcher_started"] is True

        # A second "handshake" (tab-deserialize/reload) converges -- never a
        # second thread for the same repository.
        started_again = core.dispatcher_ensure_started()
        assert started_again["dispatcher_started"] is True
        assert callback_bridge.get_dispatcher(root) is not None

        stopped = core.dispatcher_stop()
        assert stopped["ok"] is True
        assert stopped["stopped"] is True
        assert callback_bridge.get_dispatcher(root) is None
    finally:
        callback_bridge.stop_dispatcher(root)


def test_repository_switch_stops_old_dispatcher_never_shares_with_new(tmp_path):
    root_a = tmp_path / "repo_a"
    root_b = tmp_path / "repo_b"
    for root in (root_a, root_b):
        root.mkdir()
        init = task_store.initialize_repository(root)
        assert init["ok"], init
    try:
        dispatcher_a = callback_bridge.ensure_dispatcher(root_a, "codex")
        dispatcher_b = callback_bridge.ensure_dispatcher(root_b, "codex")
        assert dispatcher_a is not dispatcher_b
        assert dispatcher_a.is_running()
        assert dispatcher_b.is_running()

        # selectRepositoryCommand()/getMcpClient() stop the OLD repo's
        # dispatcher explicitly before/while binding the new one -- never
        # leaves it running once the window has moved on.
        assert callback_bridge.stop_dispatcher(root_a) is True
        assert dispatcher_a.is_running() is False
        assert dispatcher_b.is_running() is True
        assert callback_bridge.get_dispatcher(root_a) is None
        assert callback_bridge.get_dispatcher(root_b) is dispatcher_b
    finally:
        callback_bridge.stop_dispatcher(root_a)
        callback_bridge.stop_dispatcher(root_b)


def test_extension_reload_restore_disposes_stale_controller_before_adopting_new_panel():
    body = _slice(_EXTENSION_JS, "function reviveDashboardPanel(", 1700)
    assert "panel.__aiworkhubViewState.dispose()" in body
    assert "getMcpClient(context)" in body
    assert "pushSnapshot(view)" in body


def test_extension_handshake_calls_dispatcher_ensure_started():
    body = _slice(_EXTENSION_JS, "async _handshake()", 1600)
    assert "DISPATCHER_TOOLS.ensureStarted" in body


def test_extension_deactivate_stops_dispatcher_before_terminating_child():
    body = _slice(_EXTENSION_JS, "async function deactivate()", 500)
    assert "stopDispatcherThenTerminate" in body


def test_extension_stop_dispatcher_then_terminate_orders_dispatcher_stop_before_kill():
    body = _slice(_EXTENSION_JS, "async stopDispatcherThenTerminate(", 900)
    stop_tool_index = body.index("DISPATCHER_TOOLS.stop")
    kill_index = body.index("this.stop(")
    assert stop_tool_index < kill_index, (
        "the dispatcher must be told to stop (freeing any nested app-server "
        "subprocess) before the outer MCP child is killed, or the nested "
        "process is orphaned"
    )


def test_extension_repo_switch_stops_old_client_dispatcher_before_rebinding():
    get_client_body = _slice(_EXTENSION_JS, "function getMcpClient(context)", 900)
    assert "stopDispatcherThenTerminate" in get_client_body

    select_repo_body = _slice(_EXTENSION_JS, "async function selectRepositoryCommand()", 1700)
    assert "stopDispatcherThenTerminate" in select_repo_body


def test_extension_never_spawns_a_second_child_while_one_is_live():
    body = _slice(_EXTENSION_JS, "ensureStarted() {", 500)
    assert "this.running && this.initialized" in body
    assert "this.startingPromise" in body


# ---------------------------------------------------------------------------
# 4. Only .aiworkhub is ever read/created -- .aiworkinghub never appears.
# ---------------------------------------------------------------------------

def test_extension_only_reads_aiworkhub_never_the_legacy_aiworkinghub_path():
    assert ".aiworkinghub" not in _EXTENSION_JS
    assert '".aiworkhub"' in _EXTENSION_JS


def test_backend_never_references_the_legacy_aiworkinghub_path():
    assert ".aiworkinghub" not in _DASHBOARD_MCP_APP_SRC
    assert ".aiworkinghub" not in _CALLBACK_BRIDGE_SRC


def test_initialize_view_bootstraps_in_place_never_a_legacy_or_cross_repo_import():
    marker = "def initialize_view("
    start = _DASHBOARD_MCP_APP_SRC.index(marker)
    end = _DASHBOARD_MCP_APP_SRC.index("\n\n\n", start)
    body = _DASHBOARD_MCP_APP_SRC[start:end]
    assert "repository_bootstrap.initialize_repository_full" in body
    for forbidden in ("import_legacy", "migrate_legacy", "copy_from_other_repo", ".aiworkinghub"):
        assert forbidden not in body


# ---------------------------------------------------------------------------
# 5. Init Repo stays gated to a genuinely uninitialized repository.
# ---------------------------------------------------------------------------

def test_is_not_initialized_reason_true_only_for_never_initialized():
    assert dashboard_mcp_app.is_not_initialized_reason("manifest_invalid:manifest_missing:/x/.aiworkhub/project.json") is True
    for corrupt_reason in (
        "manifest_invalid:repo_id_mismatch",
        "manifest_invalid:repository_root_unavailable:/x",
        "registry_repo_id_mismatch",
        "canonical_db_missing",
        "canonical_db_corrupt",
        "quick_check_failed:corrupt",
        "",
    ):
        assert dashboard_mcp_app.is_not_initialized_reason(corrupt_reason) is False


def test_health_view_flags_not_initialized_only_for_a_genuinely_never_initialized_repo(tmp_path, monkeypatch):
    never_initialized = tmp_path / "never_initialized"
    never_initialized.mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(never_initialized))
    result = dashboard_mcp_app.health_view()
    assert result["ok"] is False
    assert result["storage"]["not_initialized"] is True


def test_health_view_never_flags_not_initialized_for_a_directory_that_does_not_exist(tmp_path, monkeypatch):
    # A missing/unresolvable root is a different, real failure -- never the
    # "click Init Repo" recommendation a genuinely fresh repository gets.
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path / "does_not_exist"))
    result = dashboard_mcp_app.health_view()
    assert result["ok"] is False
    assert result["storage"]["not_initialized"] is False


def test_extension_init_repo_button_stays_inside_the_hidden_uninitialized_alert():
    marker = 'id="uninitialized-alert"'
    idx = _EXTENSION_JS.index(marker)
    section_start = _EXTENSION_JS.rindex("<section", 0, idx)
    section_end = _EXTENSION_JS.index("</section>", idx)
    section = _EXTENSION_JS[section_start:section_end]
    assert "hidden" in section
    assert 'id="initialize-button"' in section


# ---------------------------------------------------------------------------
# 6. register() keeps binding the full tool surface the Webview depends on.
# ---------------------------------------------------------------------------

def test_register_still_binds_dispatcher_dependent_readonly_and_initialize_tools():
    assert dashboard_mcp_app.READONLY_TOOL_NAMES == (
        "aiworkhub_dashboard_snapshot",
        "aiworkhub_dashboard_task_detail",
        "aiworkhub_dashboard_health",
    )
    assert dashboard_mcp_app.LIVE_OUTPUT_TOOL_NAME == "aiworkhub_dashboard_task_live_output"
    assert dashboard_mcp_app.INITIALIZE_TOOL_NAME == "aiworkhub_dashboard_initialize"


def test_extension_dispatcher_tools_match_the_python_server_contract():
    assert 'ensureStarted: "aiworkhub_dispatcher_ensure_started"' in _EXTENSION_JS
    assert 'health: "aiworkhub_dispatcher_health"' in _EXTENSION_JS
    assert 'stop: "aiworkhub_dispatcher_stop"' in _EXTENSION_JS
