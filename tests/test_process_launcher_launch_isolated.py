"""``_launch_isolated`` was moved out of ``process_launcher``; the seams must survive.

``ProcessManager._launch_isolated`` was 1043 lines inside ``process_launcher``
-- the largest single method left in the repository's highest-churn file, and
the last open structural item of the 2026-09-07 chain audit.  It now lives in
:mod:`aiworkhub.process_launcher_launch_isolated` and the method is a
delegation.

The move is only safe because the body still resolves its module-level
collaborators through the ``process_launcher`` module object.  The suite
monkeypatches that module across dozens of attributes; a relocation that
captured those names at import time instead would sever the seam **silently** --
the patch would rebind ``process_launcher.X`` while the moved code kept calling
the original, and every one of those tests would still pass while testing
nothing.

These tests are the guard.  They assert the binding list is exactly the moved
function's free-variable set under real scope analysis (so a future edit that
introduces a new global cannot quietly escape the mechanism), and they drive
two of the seams through the real call path to prove a patch still lands and
that changing the patched value changes the answer.
"""

from __future__ import annotations

import ast
import base64
import builtins
import contextlib
import hashlib
import inspect
import json
import symtable
from pathlib import Path

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_launch_isolated


SOURCE = Path(process_launcher_launch_isolated.__file__)
MOVED_NAME = "launch_isolated"


def _module_ast() -> ast.Module:
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def _moved_function_node() -> ast.FunctionDef:
    return next(
        node
        for node in _module_ast().body
        if isinstance(node, ast.FunctionDef) and node.name == MOVED_NAME
    )


def _preamble_bindings(fn: ast.FunctionDef) -> dict[str, ast.Attribute]:
    """The ``name = _pl.name`` statements that re-bind the seams."""
    return {
        stmt.targets[0].id: stmt.value
        for stmt in fn.body
        if isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.Attribute)
        and isinstance(stmt.value.value, ast.Name)
        and stmt.value.value.id == "_pl"
        and stmt.value.attr == stmt.targets[0].id
    }


def _free_names_of_moved_function() -> set[str]:
    """The names the body resolves AT RUNTIME without the seam preamble.

    The preamble binds every one of these as a local, so a free-variable pass
    over the current source returns nothing.  Deleting the preamble (and the
    ``_pl`` import that feeds it) and re-running real scope analysis recovers
    the set the body actually depends on -- which is precisely the set that has
    to be re-bound.

    ``symtable`` is used rather than a hand-rolled AST walk because the body
    contains a nested function: only real scope analysis distinguishes a name
    that nested scope binds locally from one it reads from the module.
    Annotation-only names are excluded for free -- the module sets
    ``from __future__ import annotations``, so annotations are strings that are
    never evaluated and cannot carry a monkeypatch.
    """
    fn = _moved_function_node()
    preamble = _preamble_bindings(fn)
    fn.body = [
        stmt
        for stmt in fn.body
        if not (
            isinstance(stmt, ast.Assign)
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id in preamble
        )
        and not (
            isinstance(stmt, ast.ImportFrom)
            and any(alias.asname == "_pl" for alias in stmt.names)
        )
    ]
    stripped = "from __future__ import annotations\n" + ast.unparse(fn)
    table = symtable.symtable(stripped, "stripped.py", "exec")
    scope = table.lookup(MOVED_NAME).get_namespace()

    free: set[str] = set()

    def _walk(node: symtable.SymbolTable) -> None:
        for symbol in node.get_symbols():
            name = symbol.get_name()
            if symbol.is_global() and not hasattr(builtins, name):
                free.add(name)
        for child in node.get_children():
            _walk(child)

    _walk(scope)
    return free


class _StubManager:
    """The moved function takes ``self`` as an ordinary parameter."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.events: list[dict[str, object]] = []

    def _append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        event.setdefault("request_id", "R-stub")
        return event

    def _blocked(self, *args: object, **kwargs: object) -> dict[str, object]:
        return process_launcher.ProcessManager._blocked(self, *args, **kwargs)


def _launch(manager: _StubManager, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "task_id": "T-1",
        "runner": "codex_cli",
        "topic": "aiworkhub",
        "adapter_id": "codex_cli",
        "model": None,
        "owner_prompt": "prompt",
        "timeout_seconds": 600,
    }
    kwargs.update(overrides)
    return process_launcher.ProcessManager._launch_isolated(manager, **kwargs)


def test_declared_seam_names_are_exactly_the_functions_free_variables():
    """A new global in the body must be added to the preamble, or this fails.

    The preamble is what keeps the seams connected.  A runtime free name that is
    not in it would raise ``NameError``; a name in the preamble that the body no
    longer uses at runtime is dead weight that also hides the name from the type
    checker.  Both are drift, and both are caught here.
    """
    declared = set(process_launcher_launch_isolated.LAUNCH_ISOLATED_SEAM_NAMES)
    assert declared == _free_names_of_moved_function()


def test_every_declared_seam_is_still_an_attribute_of_process_launcher():
    """Each name must resolve on the module the tests actually patch."""
    missing = [
        name
        for name in process_launcher_launch_isolated.LAUNCH_ISOLATED_SEAM_NAMES
        if not hasattr(process_launcher, name)
    ]
    assert missing == []


def test_seams_are_read_from_process_launcher_at_call_time_not_import_time():
    """The preamble must read the live module, never a captured import.

    An import-time capture is exactly the silent failure this move risks, and
    it is visible in the source: every binding is an attribute load off the
    ``process_launcher`` module imported inside the function body.
    """
    fn = _moved_function_node()

    assert any(
        isinstance(stmt, ast.ImportFrom)
        and any(a.name == "process_launcher" and a.asname == "_pl" for a in stmt.names)
        for stmt in fn.body
    )

    # _preamble_bindings only matches `X = _pl.X`, so covering every declared
    # seam is itself the proof that each one is a live attribute read.
    bindings = _preamble_bindings(fn)
    missing = [
        name
        for name in process_launcher_launch_isolated.LAUNCH_ISOLATED_SEAM_NAMES
        if name not in bindings
    ]
    assert missing == []


def test_the_extracted_module_binds_no_seam_at_import_time():
    """The module object itself must not hold a copy of any seam.

    If a seam name existed at module scope here, a later edit could resolve it
    from there instead of from the preamble and the patch would stop landing
    without anything failing.
    """
    leaked = [
        name
        for name in process_launcher_launch_isolated.LAUNCH_ISOLATED_SEAM_NAMES
        if hasattr(process_launcher_launch_isolated, name)
    ]
    assert leaked == []


def test_delegating_method_keeps_the_original_signature():
    """Callers and the test files that drive the method see no change."""
    assert str(
        inspect.signature(process_launcher.ProcessManager._launch_isolated)
    ) == str(inspect.signature(process_launcher_launch_isolated.launch_isolated))


def test_process_launcher_method_is_a_delegation_and_holds_no_logic():
    """The point of the move: the body is gone from process_launcher."""
    source = inspect.getsource(process_launcher.ProcessManager._launch_isolated)
    assert len(source.splitlines()) < 40
    assert "_launch_isolated_impl(" in source


def test_patching_launch_gates_open_still_decides_the_moved_bodys_first_branch(
    monkeypatch, tmp_path
):
    """Two-sided proof on the very first seam the moved body reads.

    Closed gates must produce ``dual_gate_closed``; open gates must not.  If the
    relocation had captured ``launch_gates_open`` at import time, the patched
    value would be ignored and one of these two assertions would fail.
    """
    manager = _StubManager(tmp_path)

    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: False)
    closed = _launch(manager)
    assert closed["state"] == "blocked"
    assert "dual_gate_closed" in closed["blocked_reason"]

    # Mutate the patched value; the assertion has to move with it.
    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    opened = _launch(manager, timeout_seconds=5)
    assert opened["blocked_reason"] == "timeout_out_of_range"


@pytest.mark.parametrize("marker", ["seam_marker_alpha", "seam_marker_beta"])
def test_patching_a_seam_value_flows_through_into_the_moved_bodys_receipt(
    monkeypatch, tmp_path, marker
):
    """Drive a seam inside the moved body's main ``try`` and read the result.

    ``_validate_adapter_identity`` is a module-level collaborator called from
    deep inside the relocated code.  The value the test installs on
    ``process_launcher`` has to reach the returned receipt; parametrizing the
    marker shows the assertion tracks the patched value rather than passing on
    a constant.
    """

    class _TaskEngine:
        @staticmethod
        def record_launch_blocker(*_a: object, **_k: object) -> dict[str, object]:
            return {"ok": True}

    def _reject(*_a: object, **_k: object) -> None:
        raise ValueError(marker)

    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(process_launcher, "task_engine", _TaskEngine)
    monkeypatch.setattr(process_launcher, "_validate_adapter_identity", _reject)

    result = _launch(_StubManager(tmp_path))

    assert result["ok"] is False
    assert marker in result["blocked_reason"]


def test_vscode_launch_prefetch_accepts_parse_broken_rework_overlay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aiworkhub import source_graph
    from aiworkhub.repository_state import bootstrap_repository
    from aiworkhub.worker_workspace import WorkerWorkspace

    authority = tmp_path / "authority"
    workspace_root = tmp_path / "R-prefetch"
    workspace = workspace_root / "worktree"
    home = workspace_root / "home"
    process_dir = tmp_path / "processes"
    authority.mkdir()
    workspace.mkdir(parents=True)
    home.mkdir()
    process_dir.mkdir()
    bootstrap_repository(authority, repo_name="authority")
    bootstrap_repository(workspace, repo_name="workspace")
    (authority / "src").mkdir()
    (workspace / "src").mkdir()
    (authority / "src/changed.py").write_text(
        "def canonical_symbol():\n    return 'old'\n",
        encoding="utf-8",
    )
    source_graph.build_index(authority, incremental=False)
    broken = b"def retained_overlay(:\n    return 'repair me'\n"
    (workspace / "src/changed.py").write_bytes(broken)
    digest = hashlib.sha256(broken).hexdigest()
    packet = {
        "successor_request_id": "R-prefetch",
        "successor_task_id": "T-1",
        "predecessor_request_id": "R-old",
        "predecessor_task_id": "T-1",
        "authority_repo": str(authority.resolve()),
        "files": [
            {
                "path": "src/changed.py",
                "sha256": digest,
                "content_base64": base64.b64encode(broken).decode("ascii"),
            }
        ],
    }
    packet["canonical_digest"] = hashlib.sha256(
        json.dumps(packet, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    overlay_path = home / "task_mcp_worker_runtime" / "rework_overlay.json"
    overlay_path.parent.mkdir()
    overlay_path.write_text(json.dumps(packet), encoding="utf-8")
    worker_workspace = WorkerWorkspace(
        request_id="R-prefetch",
        repo=authority,
        path=workspace,
        home=home,
        allowed_writes=("src/changed.py",),
        parent_baseline={},
        workspace_baseline={},
    )

    class _LaunchManager(_StubManager):
        def __init__(self) -> None:
            super().__init__(authority)
            self.process_dir = process_dir
            self._live: dict[str, object] = {}
            self._lock = contextlib.nullcontext()

        def _preflight_card(
            self, *_args: object, **_kwargs: object,
        ) -> dict[str, object]:
            return {
                "request_id": "R-prefetch",
                "allowed_writes": ["src/changed.py"],
                "project_context": {
                    "source_graph": {
                        "mode": "file",
                        "query": "src/changed.py",
                        "target": "src/changed.py",
                        "budget": 16,
                        "workflow_stage": "orientation",
                    }
                },
            }

        def _with_dependency_inputs(self, card: dict[str, object]) -> dict[str, object]:
            return dict(card)

        def _resolve_provider_env(
            self,
            _adapter_id: str,
            model: str | None,
        ) -> tuple[dict[str, str], str | None]:
            return {}, model

        def _launch_reservation(
            self, _event: dict[str, object],
        ) -> contextlib.AbstractContextManager[None]:
            return contextlib.nullcontext()

        def _terminal_authority_grant_path(self, request_id: str) -> Path:
            return process_dir / f"{request_id}.authority.json"

        def _terminal_authority_key(self) -> bytes:
            return b"test-key"

        def _popen(self, *_args: object, **_kwargs: object) -> object:
            return type("FakeProcess", (), {"pid": 4321})()

        def _monitor(self, _live: object) -> None:
            return None

    class _Runtime:
        server_name = "test-worker-mcp"
        tool_names = ("aiworkhub_worker_source_graph_query",)
        audit_ledger_path = None
        audit_hmac_key_path = None
        claude_mcp_config_path = home / "claude.json"
        copilot_mcp_config_path = home / "copilot.json"
        codex_config_toml_path = home / "config.toml"
        kilo_config_path = home / "kilo.json"
        package_import_root = tmp_path

    class _TaskEngine:
        @staticmethod
        def claim_start_exact(
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            return {"ok": True, "card": {"claim_epoch": 1}}

    class _FakeThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

        def start(self) -> None:
            return None

    created: dict[str, object] = {}

    class _BridgeRequest:
        def __init__(self, request_id: str) -> None:
            self.request_id = request_id

    class _Bridge:
        @staticmethod
        def create_request(**kwargs: object) -> _BridgeRequest:
            created["kwargs"] = kwargs
            return _BridgeRequest(str(kwargs["request_id"]))

        @staticmethod
        def bridge_request_metadata(request: _BridgeRequest) -> dict[str, str]:
            return {"schema_id": "test.bridge", "request_id": request.request_id}

        @staticmethod
        def cancel_request(_request: _BridgeRequest) -> None:
            return None

    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(process_launcher, "task_engine", _TaskEngine)
    monkeypatch.setattr(
        process_launcher, "_validate_adapter_identity", lambda *_a: None,
    )
    monkeypatch.setattr(
        process_launcher,
        "validate_workforce_identity",
        lambda _runner, _adapter_id, model, **_kwargs: model or "test-model",
    )
    monkeypatch.setattr(
        process_launcher, "_memory_launch_admission", lambda: {"admit": True},
    )
    monkeypatch.setattr(
        process_launcher, "_external_readonly_dirs", lambda *_a: [],
    )
    monkeypatch.setattr(
        process_launcher, "_task_authority_repo", lambda *_a: authority,
    )
    monkeypatch.setattr(
        process_launcher, "_launch_project_context", lambda *_a: None,
    )
    monkeypatch.setattr(
        process_launcher, "create_workspace", lambda *_a: worker_workspace,
    )
    monkeypatch.setattr(
        process_launcher, "build_residual_contract_manifest", lambda *_a: [],
    )
    monkeypatch.setattr(
        process_launcher,
        "_materialize_worker_rework_overlay",
        lambda *_a, **_kwargs: (overlay_path, packet),
    )
    monkeypatch.setattr(
        process_launcher,
        "_materialize_crash_retry_packet",
        lambda *_a, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        process_launcher,
        "_provision_worker_mcp_runtime_for_authority",
        lambda *_a, **_kwargs: _Runtime(),
    )
    monkeypatch.setattr(
        process_launcher,
        "_worker_mcp_source_graph_targets",
        lambda _context: ("src/changed.py",),
    )
    monkeypatch.setattr(
        process_launcher, "_worker_mcp_session_topic", lambda *_a: "nf736",
    )
    monkeypatch.setattr(
        process_launcher, "build_worker_prompt", lambda **_kwargs: "prompt",
    )
    monkeypatch.setattr(process_launcher, "vscode_lm_bridge", _Bridge)
    monkeypatch.setattr(
        process_launcher, "_vscode_lm_worker_env", lambda env, _root: env or {},
    )
    monkeypatch.setattr(
        process_launcher, "worker_launch_env", lambda *_a, **_kwargs: {},
    )
    monkeypatch.setattr(
        process_launcher, "sandbox_argv", lambda _w, _a, argv, **_k: argv,
    )
    monkeypatch.setattr(
        process_launcher, "_worker_launch_cwd", lambda path: str(path),
    )
    monkeypatch.setattr(
        process_launcher,
        "_worker_supervisor_script",
        lambda: tmp_path / "supervisor.py",
    )
    monkeypatch.setattr(
        process_launcher, "_touch_0600", lambda path: path.write_text(""),
    )
    monkeypatch.setattr(process_launcher, "chmod_path", lambda *_a: None)
    monkeypatch.setattr(
        process_launcher,
        "write_json_0600",
        lambda path, data: path.write_text(json.dumps(data)),
    )
    monkeypatch.setattr(
        process_launcher,
        "_write_terminal_authority_grant",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(process_launcher, "_pid_start_ticks", lambda _pid: 123)
    monkeypatch.setattr(
        process_launcher, "process_group_launch_kwargs", lambda _name: {},
    )
    monkeypatch.setattr(process_launcher.threading, "Thread", _FakeThread)
    monkeypatch.setattr(
        process_launcher,
        "_committed_claim_card",
        lambda claim, **_kwargs: {
            "request_id": "R-prefetch",
            "claim_epoch": int(dict(claim["card"])["claim_epoch"]),
            "allowed_writes": ["src/changed.py"],
        },
    )

    manager = _LaunchManager()
    result = _launch(
        manager,
        runner="vscode_lm",
        adapter_id=process_launcher.runtime_adapters.VSCODE_LM_ADAPTER,
        topic="nf736",
        timeout_seconds=30,
    )

    assert result["ok"] is True
    assert result["state"] == "running"
    assert "kwargs" in created
    bridge_kwargs = dict(created["kwargs"])
    assert bridge_kwargs["source_graph_request"]["query"] == "src/changed.py"
    source_graph_result = dict(bridge_kwargs["source_graph_result"])
    assert source_graph_result["ok"] is True
    assert source_graph_result["authority_source"] == "rework_overlay"
    payload = json.loads(str(source_graph_result["content"]))
    assert payload["matches"] == [
        {
            "file_path": "src/changed.py",
            "kind": "file",
            "name": "changed.py",
            "qualname": "src/changed.py",
            "source_hash": digest,
            "pinned_sha256": digest,
            "observed_sha256": digest,
            "status": "file_evidence_only",
            "parse_status": "parse_error_fail_closed",
            "line_start": 1,
            "line_end": 1,
            "provenance": "request_scoped_rework_overlay",
        }
    ]
    assert payload["overlay"]["repair_evidence_paths"] == ["src/changed.py"]
    assert "canonical_symbol" not in str(source_graph_result["content"])
    blocked_reasons = [str(event.get("blocked_reason") or "") for event in manager.events]
    assert not any(
        reason.startswith("vscode_lm_initial_source_graph_prefetch_failed")
        for reason in blocked_reasons
    )
