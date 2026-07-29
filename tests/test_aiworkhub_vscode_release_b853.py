"""B853: AIWorkHub VS Code release -- HTTP/8765 removal.

Verifies (a) ``aiworkhub.dashboard`` no longer contains any HTTP listener /
browser / fixed-port runtime while its canonical snapshot/detail builders
keep working, and (b) the bounded MCP tool surface in
``aiworkhub.dashboard_mcp_app`` is unchanged and still functional.

Some isolated Task MCP worktrees only seed the exact files a given task's
``read_first``/``allowed_writes`` names -- this worktree has
``aiworkhub/dashboard.py`` and ``aiworkhub/dashboard_mcp_app.py`` but not
their sibling modules (``core``, ``task_store``, ``completion_inbox``,
``cost_ledger``, ``deepseek_credentials``, ``process_launcher``,
``repository_state``), which a fuller environment already provides. This
mirrors the existing bridge in ``tests/test_dashboard.py``
(``_ensure_deepseek_credentials_stub``): install a minimal stub ONLY when the
real sibling module is genuinely unimportable, so a host with the full
package exercises the real modules unchanged.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_DASHBOARD_PY_PATH = _SRC / "aiworkhub" / "dashboard.py"
_DASHBOARD_MCP_APP_PY_PATH = _SRC / "aiworkhub" / "dashboard_mcp_app.py"
_DASHBOARD_SOURCE = _DASHBOARD_PY_PATH.read_text(encoding="utf-8")


def _load_module_from_this_worktree(dotted_name: str, path: Path) -> types.ModuleType:
    """Load THIS worktree's exact file at ``path`` under ``dotted_name``,
    bypassing normal package resolution.

    Some hosts run several isolated Task MCP worktrees side by side against
    one shared Python environment whose ``aiworkhub`` package is resolved via
    an editable-install meta path finder pointed at a *different* checkout.
    That finder takes priority over ``sys.path``/``PYTHONPATH`` ordering, so
    a plain ``import aiworkhub.dashboard`` can silently load a sibling
    worktree's copy instead of the file this test is meant to verify. Loading
    by explicit file path guarantees this test always exercises exactly the
    file at ``path``, regardless of what else is installed on the host.
    """
    spec = importlib.util.spec_from_file_location(dotted_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted_name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_aiworkhub_sibling_stubs() -> None:
    """Install minimal stand-ins for aiworkhub's sibling modules ONLY when
    they are genuinely absent from this worktree (see module docstring)."""
    import aiworkhub

    if not hasattr(aiworkhub, "__version__"):
        aiworkhub.__version__ = "0.2.0"

    def _install(name: str, build) -> None:
        full_name = f"aiworkhub.{name}"
        try:
            importlib.import_module(full_name)
            return
        except ImportError:
            pass
        module = types.ModuleType(full_name)
        build(module)
        sys.modules[full_name] = module
        setattr(aiworkhub, name, module)

    def _build_core(module: types.ModuleType) -> None:
        def repo_root():
            return _TOOL_ROOT.parent.parent

        def collision_guard(print_json: bool = True):  # noqa: ARG001
            return {
                "returncode": 0,
                "stdout": '{"collision_free": true, "active_cards": 0, "collision_count": 0, "file_collisions": []}',
            }

        def health():
            return {"ok": True}

        module.repo_root = repo_root
        module.collision_guard = collision_guard
        module.health = health

    def _build_completion_inbox(module: types.ModuleType) -> None:
        module.DEFAULT_STALE_PROCESSING_HOURS = 24.0
        module.MAX_LIMIT = 2000

        def build_completion_inbox(limit=500, stale_processing_hours=24.0):  # noqa: ARG001
            return {"review_queue": [], "stale_processing": [], "runner_mismatch_warnings": [], "read_errors": []}

        module.build_completion_inbox = build_completion_inbox

    def _build_cost_ledger(module: types.ModuleType) -> None:
        def build_cost_ledger(include_tasks: bool = False):  # noqa: ARG001
            return {"aggregates": {"by_runner": {}}, "source_status": {}}

        module.build_cost_ledger = build_cost_ledger

    def _build_deepseek_credentials(module: types.ModuleType) -> None:
        def adapter_readiness(repo=None):  # noqa: ARG001
            return {"ok": True, "readonly": True, "adapters": []}

        module.adapter_readiness = adapter_readiness

    def _build_process_launcher(module: types.ModuleType) -> None:
        module.PROCESS_LOG_ENV = "AIWORKHUB_PROCESS_LOG"
        module.ACTIVE_PROCESS_STATES = ("started", "running")
        module.LAUNCH_IMPLEMENTED = False

        def launch_gates_open():
            return False

        def read_supervisor_status(path):  # noqa: ARG001
            return {}

        def _pid_matches(pid, ticks):  # noqa: ARG001
            return False

        def derive_liveness_state(**kwargs):  # noqa: ARG001
            return {"liveness_state": "unknown", "heartbeat_age_seconds": None, "activity_age_seconds": None}

        module.launch_gates_open = launch_gates_open
        module.read_supervisor_status = read_supervisor_status
        module._pid_matches = _pid_matches
        module.derive_liveness_state = derive_liveness_state

    def _build_repository_state(module: types.ModuleType) -> None:
        def resolve_repository_root(require_manifest: bool = False):  # noqa: ARG001
            return _TOOL_ROOT.parent.parent

        module.resolve_repository_root = resolve_repository_root

    def _build_task_store(module: types.ModuleType) -> None:
        class StorageReadiness:
            def __init__(self, ready=True, reason="ok", repo_id="repo_stub"):
                self.ready = ready
                self.reason = reason
                self.repo_id = repo_id

        class StorageNotReadyError(RuntimeError):
            pass

        class TaskStoreError(RuntimeError):
            pass

        class InitializationRefusedError(TaskStoreError):
            pass

        def storage_readiness(repo_root):  # noqa: ARG001
            return StorageReadiness()

        def list_tasks(repo_root, status, limit=500):  # noqa: ARG001
            return []

        def get_task(repo_root, task_id):  # noqa: ARG001
            return None

        def exact_status_counts(repo_root):  # noqa: ARG001
            return {}

        def callback_bridge_health(repo_root):  # noqa: ARG001
            return {}

        def initialize_repository(root, expected_repo_id=None):  # noqa: ARG001
            return {"ok": True, "repo_id": "repo_" + "0" * 32}

        module.StorageReadiness = StorageReadiness
        module.StorageNotReadyError = StorageNotReadyError
        module.TaskStoreError = TaskStoreError
        module.InitializationRefusedError = InitializationRefusedError
        module.storage_readiness = storage_readiness
        module.list_tasks = list_tasks
        module.get_task = get_task
        module.exact_status_counts = exact_status_counts
        module.callback_bridge_health = callback_bridge_health
        module.initialize_repository = initialize_repository

    _install("core", _build_core)
    _install("completion_inbox", _build_completion_inbox)
    _install("cost_ledger", _build_cost_ledger)
    _install("deepseek_credentials", _build_deepseek_credentials)
    _install("process_launcher", _build_process_launcher)
    _install("repository_state", _build_repository_state)
    _install("task_store", _build_task_store)


_ensure_aiworkhub_sibling_stubs()

from aiworkhub import task_store

dashboard = _load_module_from_this_worktree("aiworkhub.dashboard", _DASHBOARD_PY_PATH)
dashboard_mcp_app = _load_module_from_this_worktree("aiworkhub.dashboard_mcp_app", _DASHBOARD_MCP_APP_PY_PATH)


# ── 1. Static source checks: the canonical runtime carries no HTTP/browser/
#      fixed-port surface. ─────────────────────────────────────────────────
FORBIDDEN_PATTERNS = (
    "import http.server",
    "from http.server",
    "from http import HTTPStatus",
    "HTTPServer",
    "BaseHTTPRequestHandler",
    "ThreadingHTTPServer",
    "serve_forever",
    "import webbrowser",
    "webbrowser.open",
    "import argparse",
    "import socket",
    "8765",
    "def create_server(",
    "def main(",
    "--host",
    "--port",
)


@pytest.mark.parametrize("pattern", FORBIDDEN_PATTERNS)
def test_dashboard_has_no_http_runtime(pattern: str) -> None:
    assert pattern not in _DASHBOARD_SOURCE, f"forbidden HTTP/browser/port pattern present: {pattern!r}"


def test_dashboard_module_exposes_no_http_server_symbols() -> None:
    for name in ("create_server", "main", "DashboardHTTPServer", "DashboardRequestHandler", "is_loopback_host"):
        assert not hasattr(dashboard, name), f"dashboard.{name} must not exist after HTTP removal"


# ── 2. Functional checks: snapshot/detail builders keep working. ───────────
class _FakeProvider:
    def get_storage_readiness(self):
        # dashboard.build_snapshot only ever reads ready/reason/repo_id via
        # getattr -- a plain duck-typed object avoids depending on whichever
        # concrete task_store.StorageReadiness constructor signature happens
        # to be resolved on this host (real module vs. this file's stub).
        return types.SimpleNamespace(ready=True, reason="ok", repo_id="repo_" + "a" * 32)

    def list_tasks(self, status: str):
        if status == "pending":
            return [{"task_id": "TASK_B853_PENDING_V1", "status": "pending", "topic": "release", "runner": "claude_sonnet5"}]
        return []

    def get_task(self, task_id: str):
        if task_id == "TASK_B853_PENDING_V1":
            return {
                "task_id": task_id,
                "status": "pending",
                "topic": "release",
                "runner": "claude_sonnet5",
                "objective": "Ship the aiworkhub vscode release",
            }
        return None

    def get_completion_inbox(self):
        return {"review_queue": [], "stale_processing": [], "runner_mismatch_warnings": [], "read_errors": []}

    def get_cost_ledger(self):
        return {"aggregates": {"by_runner": {}}, "source_status": {}}

    def get_collision_report(self):
        return {"collision_free": True, "active_cards": 1, "collision_count": 0, "file_collisions": []}

    def get_agent_processes(self):
        return {"ok": True, "processes": [], "total_requests": 0}

    def get_exact_status_counts(self):
        return {"pending": 1, "processing": 0, "review": 0, "blocked": 0, "finished": 0, "archived": 0}

    def get_adapter_readiness(self):
        return {"ok": True, "readonly": True, "adapters": []}

    def get_callback_bridge_health(self):
        return {"bound_task_count": 0, "unbound_task_count": 0, "by_state": {}}


def test_build_snapshot_is_functional() -> None:
    snapshot = dashboard.build_snapshot(_FakeProvider())
    assert snapshot["schema_version"] == 1
    assert snapshot["readonly"] is True
    assert snapshot["storage"]["ready"] is True
    assert snapshot["status_counts"]["pending"] == 1
    assert [row["task_id"] for row in snapshot["tasks"]["pending"]] == ["TASK_B853_PENDING_V1"]
    assert snapshot["errors"] == []


def test_build_task_detail_is_functional() -> None:
    detail = dashboard.build_task_detail("TASK_B853_PENDING_V1", _FakeProvider())
    assert detail is not None
    assert detail["task"]["task_id"] == "TASK_B853_PENDING_V1"
    assert detail["task"]["objective"] == "Ship the aiworkhub vscode release"


def test_build_snapshot_fails_closed_when_storage_not_ready() -> None:
    class _NotReadyProvider(_FakeProvider):
        def get_storage_readiness(self):
            return types.SimpleNamespace(ready=False, reason="uninitialized", repo_id="")

    snapshot = dashboard.build_snapshot(_NotReadyProvider())
    assert snapshot["storage"]["ready"] is False
    assert snapshot["status_counts"]["pending"] == 0
    assert snapshot["tasks"]["pending"] == []


# ── 3. dashboard_mcp_app's bounded MCP tool surface stays wired. ───────────
def test_dashboard_mcp_app_readonly_tools_unchanged() -> None:
    assert dashboard_mcp_app.READONLY_TOOL_NAMES == (
        "aiworkhub_dashboard_snapshot",
        "aiworkhub_dashboard_task_detail",
        "aiworkhub_dashboard_health",
    )
    assert dashboard_mcp_app.INITIALIZE_TOOL_NAME == "aiworkhub_dashboard_initialize"


def test_dashboard_mcp_app_health_view_is_functional(tmp_path, monkeypatch) -> None:
    task_store.initialize_repository(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    result = dashboard_mcp_app.health_view()
    assert result["ok"] is True
    assert result["storage"]["ready"] is True
    assert result["server_tool"] == "aiworkhub_dashboard_health"
    assert result["authority_flags"]["readonly"] is True


# ── 4. Extension/eval artifacts required by this task exist and are valid.
def test_glm_release_eval_json_is_valid() -> None:
    eval_path = _TOOL_ROOT / "eval" / "aiworkhub_vscode_release_glm_b853_v1.json"
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    assert payload["task_id"] == "CLAUDE_SONNET5_AIWORKHUB_VSCODE_RELEASE_GLM_B853_V1"


def test_extension_js_has_no_model_diagnostics_or_glm_canary_surface() -> None:
    """B880: the obsolete dashboard diagnostics/canary UI stays removed.

    The production GLM worker bridge intentionally uses ``vscode.lm`` in the
    extension host because editor-contributed custom models are unavailable to
    the standalone Copilot CLI.  That is runtime orchestration, not a canary UI.
    """
    ext_path = _TOOL_ROOT / "vscode-extension" / "extension.js"
    ext = ext_path.read_text(encoding="utf-8")
    for forbidden in (
        "runGlmCanary",
        "refreshModelCapabilities",
        "cancelGlmCanary",
        "GLM_CANARY_FIXED_PROMPT",
        "MODEL_CAPABILITY_STATES",
    ):
        assert forbidden not in ext, f"obsolete model-diagnostics symbol still present: {forbidden}"


@pytest.mark.parametrize("media_file", ("app.js", "app.css"))
def test_media_files_have_no_model_diagnostics_or_glm_canary_surface(media_file: str) -> None:
    """B880: the webview media bundle (JS + CSS) carries no dead Model
    capabilities / GLM canary DOM bindings, render/status logic, click
    handlers, or styling -- these were left over from the extension.js-only
    B880 v1 pass and are removed here."""
    media_path = _TOOL_ROOT / "vscode-extension" / "media" / media_file
    source = media_path.read_text(encoding="utf-8")
    for forbidden in (
        "refresh-model-capabilities-button",
        "refreshModelCapabilities",
        "model-capability-state",
        "model-capability-list",
        "modelCapabilityState",
        "modelCapabilityList",
        "modelCapabilities",
        "renderModelCapabilities",
        "glm-canary",
        "glmCanary",
        "runGlmCanary",
        "cancelGlmCanary",
        "renderGlmCanary",
    ):
        assert forbidden not in source, f"obsolete model-diagnostics symbol still present in {media_file}: {forbidden}"
