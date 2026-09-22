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
import os
import symtable
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_launch_isolated
from aiworkhub import runtime_adapters
from aiworkhub import worker_ai_tools_mcp
from aiworkhub import windows_appcontainer
from aiworkhub import worker_workspace


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
    """Each name must resolve on the module the tests actually patch.

    Local seams are free variables too, but they are defined on this module
    rather than re-bound from ``process_launcher`` -- they have no
    counterpart there to check.
    """
    local = set(process_launcher_launch_isolated.LAUNCH_ISOLATED_LOCAL_SEAM_NAMES)
    missing = [
        name
        for name in process_launcher_launch_isolated.LAUNCH_ISOLATED_SEAM_NAMES
        if name not in local and not hasattr(process_launcher, name)
    ]
    assert missing == []


def test_seams_are_read_from_process_launcher_at_call_time_not_import_time():
    """The preamble must read the live module, never a captured import.

    An import-time capture is exactly the silent failure this move risks, and
    it is visible in the source: every binding is an attribute load off the
    ``process_launcher`` module imported inside the function body. Local
    seams are exempt: they are not re-bound from ``process_launcher`` at all.
    """
    fn = _moved_function_node()

    assert any(
        isinstance(stmt, ast.ImportFrom)
        and any(a.name == "process_launcher" and a.asname == "_pl" for a in stmt.names)
        for stmt in fn.body
    )

    # _preamble_bindings only matches `X = _pl.X`, so covering every declared
    # non-local seam is itself the proof that each one is a live attribute read.
    bindings = _preamble_bindings(fn)
    local = set(process_launcher_launch_isolated.LAUNCH_ISOLATED_LOCAL_SEAM_NAMES)
    missing = [
        name
        for name in process_launcher_launch_isolated.LAUNCH_ISOLATED_SEAM_NAMES
        if name not in local and name not in bindings
    ]
    assert missing == []


def test_the_extracted_module_binds_no_seam_at_import_time():
    """The module object itself must not hold a copy of any non-local seam.

    If a seam name existed at module scope here, a later edit could resolve it
    from there instead of from the preamble and the patch would stop landing
    without anything failing. Local seams are declared on this module by
    design, so they are exempt from this check.
    """
    local = set(process_launcher_launch_isolated.LAUNCH_ISOLATED_LOCAL_SEAM_NAMES)
    leaked = [
        name
        for name in process_launcher_launch_isolated.LAUNCH_ISOLATED_SEAM_NAMES
        if name not in local and hasattr(process_launcher_launch_isolated, name)
    ]
    assert leaked == []


def test_every_local_seam_resolves_on_the_extracted_module():
    """Local seams must actually exist where the moved body expects them.

    Unlike the other seams, these are read from module globals rather than
    the ``process_launcher`` preamble, so this is the only test that checks
    they resolve at all.
    """
    missing = [
        name
        for name in process_launcher_launch_isolated.LAUNCH_ISOLATED_LOCAL_SEAM_NAMES
        if not hasattr(process_launcher_launch_isolated, name)
    ]
    assert missing == []


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


def test_already_attached_launch_refusal_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A duplicate launch must not replace the live worker's claim with a blocker."""

    attached_request_id = "a" * 32

    class _TaskEngine:
        @staticmethod
        def record_launch_blocker(*_a: object, **_k: object) -> dict[str, object]:
            raise AssertionError("duplicate launch must not mutate the task card")

    manager = _StubManager(tmp_path)

    def _already_attached(*_a: object, **_k: object) -> dict[str, object]:
        raise process_launcher.LaunchRejected(
            f"task_launch_already_attached:{attached_request_id}"
        )

    manager._preflight_card = _already_attached  # type: ignore[attr-defined]
    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(process_launcher, "task_engine", _TaskEngine)
    monkeypatch.setattr(
        process_launcher,
        "_validate_adapter_identity",
        lambda *_a, **_k: None,
    )

    result = _launch(manager)

    assert result == {
        "ok": False,
        "launch_implemented": True,
        "launch_enabled": True,
        "request_id": attached_request_id,
        "existing_request_id": attached_request_id,
        "task_id": "T-1",
        "runner": "codex_cli",
        "topic": "aiworkhub",
        "adapter_id": "codex_cli",
        "state": "already_attached",
        "blocked_reason": f"task_launch_already_attached:{attached_request_id}",
        "idempotent": True,
        "shell": False,
    }
    assert manager.events == []


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
                "required_outputs": ["src/changed.py"],
                "role": "implementer",
                "risk_tier": "high",
                "work_kind": "architecture",
                "difficulty": "complex",
                "token_budget": {"cap_tokens": 2048},
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
    launched_card = dict(bridge_kwargs["card"])
    assert launched_card["role"] == "implementer"
    assert launched_card["risk_tier"] == "high"
    assert launched_card["work_kind"] == "architecture"
    assert launched_card["difficulty"] == "complex"
    assert bridge_kwargs["token_budget"] == {"cap_tokens": 2048}
    assert bridge_kwargs["required_outputs"] == ["src/changed.py"]
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


def test_vscode_launch_prefetch_rejects_overlay_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    packet = {
        "successor_request_id": "R-other",
        "successor_task_id": "T-1",
        "predecessor_request_id": "R-old",
        "predecessor_task_id": "T-1",
        "authority_repo": str(authority.resolve()),
        "files": [],
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

        @staticmethod
        def mark_launch_failed(
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            return {"ok": True}

    created: dict[str, object] = {}

    class _Bridge:
        @staticmethod
        def create_request(**kwargs: object) -> object:
            created["kwargs"] = kwargs
            return type("Req", (), {"request_id": str(kwargs["request_id"])})()

        @staticmethod
        def cancel_request(_request: object) -> None:
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
    monkeypatch.setattr(process_launcher, "vscode_lm_bridge", _Bridge)
    monkeypatch.setattr(process_launcher, "_touch_0600", lambda path: path.write_text(""))
    monkeypatch.setattr(process_launcher, "chmod_path", lambda *_a: None)
    monkeypatch.setattr(
        process_launcher,
        "write_json_0600",
        lambda path, data: path.write_text(json.dumps(data)),
    )
    monkeypatch.setattr(
        process_launcher, "_committed_claim_card",
        lambda claim, **_kwargs: {
            "request_id": "R-prefetch",
            "claim_epoch": int(dict(claim["card"])["claim_epoch"]),
            "allowed_writes": ["src/changed.py"],
        },
    )

    result = _launch(
        _LaunchManager(),
        runner="vscode_lm",
        adapter_id=process_launcher.runtime_adapters.VSCODE_LM_ADAPTER,
        topic="nf736",
        timeout_seconds=30,
    )

    assert result["ok"] is False
    assert "kwargs" not in created
    reason = str(result.get("blocked_reason") or "")
    assert reason.startswith("vscode_lm_initial_source_graph_prefetch_failed:")
    assert "successor_request_id_mismatch" in reason


def test_vscode_lm_create_request_passes_authenticated_card_for_worker_and_review() -> None:
    fn = _moved_function_node()
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_request"
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords if keyword.arg}
    assert "card" in keywords
    assert isinstance(keywords["card"], ast.Name) and keywords["card"].id == "card"
    assert "token_budget" in keywords
    assert "request_kind" in keywords
    request_kind = ast.unparse(keywords["request_kind"])
    assert "quality_review" in request_kind
    assert "worker" in request_kind


# ---------------------------------------------------------------------------
# OpenCode: the generated request-local ``awh`` config reaches the real launch.
#
# ``launch_isolated`` runs for real, and so do the config provisioner,
# ``worker_launch_env`` / ``sanitized_env`` and, for Landlock and bubblewrap,
# ``sandbox_argv``.  Only collaborators that need a task store, git, a live
# supervisor or ``chmod`` are replaced, and the MCP runtime is written in the
# exact shape ``generate_worker_mcp_runtime`` emits (the generator itself needs
# ``chmod``, which the sandbox denies).
# ---------------------------------------------------------------------------

_MCP = worker_ai_tools_mcp
_OPENCODE_MODEL = "opencode-go/muse-spark-1.3-contributor"
_OPENCODE_BACKENDS = ("landlock", "bubblewrap", "windows_appcontainer")
_OPENCODE_REQUEST_ID = "R-oc-launch"


class _WindowsSys:
    """``process_launcher.sys`` as a Windows host reports it."""

    platform = "win32"

    def __getattr__(self, name: str) -> object:
        return getattr(sys, name)


class _FakeThread:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs

    def start(self) -> None:
        return None


class _OpenCodeLaunchManager(_StubManager):
    def __init__(self, authority: Path, process_dir: Path) -> None:
        super().__init__(authority)
        self.process_dir = process_dir
        self._live: dict[str, object] = {}
        self._lock = contextlib.nullcontext()
        self.popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def _preflight_card(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "request_id": _OPENCODE_REQUEST_ID,
            "allowed_writes": ["src/a.py"],
            "required_outputs": ["src/a.py"],
        }

    def _with_dependency_inputs(self, card: dict[str, object]) -> dict[str, object]:
        return dict(card)

    def _resolve_provider_env(
        self, _adapter_id: str, model: str | None
    ) -> tuple[None, str | None]:
        return None, model

    def _launch_reservation(
        self, _event: dict[str, object]
    ) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def _terminal_authority_grant_path(self, request_id: str) -> Path:
        return self.process_dir / f"{request_id}.authority.json"

    def _terminal_authority_key(self) -> bytes:
        return b"test-key"

    def _build_adapter(
        self,
        *,
        adapter_id: str,
        prompt: str,
        repo: Path,
        model: str | None,
        outer_sandbox_backend: str,
        **kwargs: object,
    ) -> runtime_adapters.RuntimeAdapterPlan:
        return runtime_adapters.build_runtime_command(
            adapter_id,
            prompt,
            repo,
            model=model,
            executable_overrides={adapter_id: Path(sys.executable).resolve()},
            outer_sandbox_backend=outer_sandbox_backend,
            **kwargs,
        )

    def _popen(self, argv: list[str], **kwargs: object) -> object:
        self.popen_calls.append((list(argv), kwargs))
        return SimpleNamespace(pid=4321)

    def _monitor(self, _live: object) -> None:
        return None


def _generated_runtime(
    workspace: worker_workspace.WorkerWorkspace,
    authority: Path,
    backend: str,
    command: str,
) -> SimpleNamespace:
    runtime_dir = workspace.home / "task_mcp_worker_runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    ledger = runtime_dir / "audit_ledger.jsonl"
    key = runtime_dir / "audit_hmac.key"
    ledger.write_bytes(b"")
    key.write_bytes(b"k" * 32)
    aliased = backend == "bubblewrap"
    env = {
        _MCP.ENV_TASK_ID: "T-1",
        _MCP.ENV_RUNNER: "opencode-go",
        _MCP.ENV_TOPIC: "topic",
        _MCP.ENV_REQUEST_ID: workspace.request_id,
        _MCP.ENV_REPO: (
            worker_workspace.SANDBOX_WORKSPACE if aliased else str(workspace.path)
        ),
        _MCP.ENV_AUTHORITY_REPO: (
            worker_workspace.SANDBOX_AUTHORITY_REPO if aliased else str(authority)
        ),
        _MCP.ENV_SOURCE_GRAPH_TARGETS: json.dumps(["src/a.py"]),
        _MCP.ENV_ALLOWED_WRITES: json.dumps(["src/a.py"]),
        _MCP.ENV_SESSION_TOPIC: "topic",
        _MCP.ENV_AUDIT_LEDGER_PATH: str(ledger),
        _MCP.ENV_AUDIT_HMAC_KEY_PATH: str(key),
        _MCP.ENV_PYTHONPATH: (
            worker_workspace.SANDBOX_PACKAGE_IMPORT_ROOT
            if aliased
            else str(_MCP.resolve_host_package_import_root())
        ),
    }
    source = runtime_dir / "claude_mcp_config.json"
    source.write_text(
        json.dumps(
            {
                "mcpServers": {
                    _MCP.SERVER_NAME: {
                        "command": command,
                        "args": ["-m", _MCP.__name__],
                        "env": env,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(
        server_name=_MCP.SERVER_NAME,
        tool_names=(),
        env=env,
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
        claude_mcp_config_path=source,
        copilot_mcp_config_path=runtime_dir / "copilot_mcp_config.json",
        codex_config_toml_path=workspace.home / ".codex" / "config.toml",
        kilo_config_path=workspace.home / ".config" / "kilo" / "kilo.json",
        package_import_root=_MCP.resolve_host_package_import_root(),
    )


def _prepare_opencode_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: str,
    *,
    command: str | None = None,
) -> SimpleNamespace:
    base = tmp_path.resolve()
    authority = base / "authority"
    root = base / _OPENCODE_REQUEST_ID
    workspace = worker_workspace.WorkerWorkspace(
        request_id=_OPENCODE_REQUEST_ID,
        repo=authority,
        path=root / "worktree",
        home=root / "home",
        allowed_writes=("src/a.py",),
        parent_baseline={},
        workspace_baseline={},
    )
    process_dir = base / "processes"
    for directory in (authority, workspace.path, workspace.home, process_dir):
        directory.mkdir(parents=True)
    runtime_command = command or (
        "/usr/bin/env" if backend == "bubblewrap" else sys.executable
    )
    runtime = _generated_runtime(workspace, authority, backend, runtime_command)
    manager = _OpenCodeLaunchManager(authority, process_dir)
    writes: list[tuple[Path, dict[str, object]]] = []
    failed: list[str] = []
    env_calls: list[dict[str, object]] = []

    class _TaskEngine:
        @staticmethod
        def claim_start_exact(*_a: object, **_k: object) -> dict[str, object]:
            return {"ok": True, "card": {"claim_epoch": 1}}

        @staticmethod
        def mark_launch_failed(
            *_a: object, reason: str, **_k: object
        ) -> dict[str, object]:
            failed.append(reason)
            return {"ok": True}

    real_worker_launch_env = process_launcher.worker_launch_env

    def _spy_worker_launch_env(adapter_id: str, **kwargs: object) -> dict[str, str]:
        env_calls.append(dict(kwargs))
        return real_worker_launch_env(adapter_id, **kwargs)

    def _set(name: str, value: object) -> None:
        monkeypatch.setattr(process_launcher, name, value)

    # A portable host, and no chmod: ``sanitized_env`` chmods the request HOME,
    # which the sandbox denies and which this launch contract does not assert.
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
    monkeypatch.setattr(runtime_adapters, "_is_linux_host", lambda: True)
    monkeypatch.setattr(worker_workspace, "chmod_path", lambda *_a, **_k: None)
    _set("launch_gates_open", lambda: True)
    _set("task_engine", _TaskEngine)
    _set("_validate_adapter_identity", lambda *_a: None)
    _set("validate_workforce_identity", lambda _r, _a, model, **_k: model)
    _set("_memory_launch_admission", lambda: {"admit": True})
    _set("_external_readonly_dirs", lambda *_a: [])
    _set("_task_authority_repo", lambda *_a: authority)
    _set("_launch_project_context", lambda *_a: None)
    _set("_launch_source_graph_request", lambda *_a: None)
    _set("_sandbox_backend_for_adapter", lambda _adapter_id: backend)
    _set("create_workspace", lambda *_a: workspace)
    _set("build_residual_contract_manifest", lambda *_a: [])
    _set("_materialize_worker_rework_overlay", lambda *_a, **_k: (None, None))
    _set("_materialize_crash_retry_packet", lambda *_a, **_k: (None, None))
    _set("_provision_worker_mcp_runtime_for_authority", lambda *_a, **_k: runtime)
    _set("_worker_mcp_source_graph_targets", lambda _context: ("src/a.py",))
    _set("_worker_mcp_session_topic", lambda *_a: "topic")
    _set("build_worker_prompt", lambda **_k: "prompt-text")
    _set("worker_launch_env", _spy_worker_launch_env)
    _set(
        "worker_temp_environment",
        lambda _repo, _request_id: {
            key: str(base / "worker-tmp") for key in runtime_adapters.WORKER_TEMP_ENV_VARS
        },
    )
    _set("worker_validation_affordance_env", lambda *_a, **_k: {})
    _set("_worker_launch_cwd", lambda path: str(path))
    _set("_worker_supervisor_script", lambda: base / "supervisor.py")
    _set("_touch_0600", lambda path: path.write_text(""))
    _set("chmod_path", lambda *_a: None)
    _set("write_json_0600", lambda path, data: writes.append((Path(path), data)))
    _set("_write_terminal_authority_grant", lambda *_a, **_k: None)
    _set("_release_launch_request_resources", lambda **_k: [])
    _set("_pid_start_ticks", lambda _pid: 123)
    _set("process_group_launch_kwargs", lambda _name: {})
    _set(
        "_committed_claim_card",
        lambda _claim, **_k: {
            "request_id": _OPENCODE_REQUEST_ID,
            "claim_epoch": 1,
            "allowed_writes": ["src/a.py"],
        },
    )
    monkeypatch.setattr(process_launcher.threading, "Thread", _FakeThread)
    if backend == "windows_appcontainer":
        # The real ``sandbox_argv`` runs: only the OS boundary is faked -- the
        # host platform and the Win32 AppContainer probe -- so its measured
        # confinement gate, not a passthrough stub, decides the worker argv.
        monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: True)
        monkeypatch.setattr(
            windows_appcontainer,
            "probe",
            lambda: SimpleNamespace(available=True, detail="fake win32"),
        )
        _set("sys", _WindowsSys())
        monkeypatch.setattr(
            process_launcher.project_context.repository_state,
            "inspect_repository",
            lambda _repo: SimpleNamespace(
                manifest=SimpleNamespace(repo_id="repo-canonical")
            ),
        )
    return SimpleNamespace(
        manager=manager,
        workspace=workspace,
        runtime=runtime,
        runtime_command=runtime_command,
        authority=authority,
        writes=writes,
        failed=failed,
        env_calls=env_calls,
    )


def _launch_opencode(harness: SimpleNamespace, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "runner": "opencode-go",
        "topic": "topic",
        "adapter_id": "opencode_cli",
        "model": _OPENCODE_MODEL,
        "timeout_seconds": 60,
    }
    kwargs.update(overrides)
    return _launch(harness.manager, **kwargs)


def _has_run(argv: list[str], *tokens: str) -> bool:
    width = len(tokens)
    return any(argv[index:index + width] == list(tokens) for index in range(len(argv)))


def _supervisor_spec(harness: SimpleNamespace) -> dict[str, object] | None:
    return next(
        (
            payload
            for path, payload in harness.writes
            if path.name == f"{_OPENCODE_REQUEST_ID}.supervisor-spec.json"
        ),
        None,
    )


@pytest.mark.parametrize("backend", _OPENCODE_BACKENDS)
def test_opencode_launch_hands_the_supervisor_the_request_local_awh_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    if backend == "bubblewrap" and os.name == "nt":
        pytest.skip("bubblewrap is a Linux sandbox")
    harness = _prepare_opencode_launch(monkeypatch, tmp_path, backend)
    workspace = harness.workspace
    aliased = backend == "bubblewrap"

    result = _launch_opencode(harness)

    assert result["ok"] is True, result
    assert result["sandbox_backend"] == backend
    assert harness.failed == []
    ((_argv, spawn),) = harness.manager.popen_calls
    env = spawn["env"]
    # Only bubblewrap remounts the request HOME under an alias; Landlock and
    # AppContainer see the real directory, so HOME must be exactly that path.
    assert harness.env_calls[0]["home"] == (None if aliased else workspace.home)
    if aliased:
        home_alias = worker_workspace.bubblewrap_home_env_value()
        assert env["HOME"] == str(Path(home_alias).resolve())
        worker_home: PurePosixPath | Path = PurePosixPath(home_alias)
    else:
        assert env["HOME"] == str(workspace.home)
        worker_home = workspace.home
    config_text = env[runtime_adapters.OPENCODE_WORKER_CONFIG_ENV]
    config = json.loads(config_text)
    assert runtime_adapters.validate_opencode_worker_config(config) is config
    assert env[runtime_adapters.OPENCODE_DISABLE_PROJECT_CONFIG_ENV] == "1"
    assert not {"OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_PERMISSION"} & set(env)
    assert list(config["mcp"]) == ["awh"]
    server = config["mcp"]["awh"]
    assert server["command"] == [harness.runtime_command, "-m", _MCP.__name__]
    environment = server["environment"]
    assert environment[_MCP.ENV_REQUEST_ID] == _OPENCODE_REQUEST_ID
    assert environment[_MCP.ENV_REPO] == (
        worker_workspace.SANDBOX_WORKSPACE if aliased else str(workspace.path)
    )
    # The audit binding is spelled where this worker's sandbox shows its HOME.
    for key, name in (
        (_MCP.ENV_AUDIT_LEDGER_PATH, "audit_ledger.jsonl"),
        (_MCP.ENV_AUDIT_HMAC_KEY_PATH, "audit_hmac.key"),
    ):
        assert environment[key] == str(worker_home / "task_mcp_worker_runtime" / name)
    spec = _supervisor_spec(harness)
    assert spec is not None
    worker_argv = [str(token) for token in spec["argv"]]
    if backend == "landlock":
        assert _has_run(worker_argv, "--home", str(workspace.home))
    elif aliased:
        # The mount namespace shows the request HOME at the alias the config
        # names, and the package root at the PYTHONPATH alias.
        assert _has_run(worker_argv, "--bind", str(workspace.home), home_alias)
        assert _has_run(
            worker_argv,
            "--ro-bind",
            str(_MCP.resolve_host_package_import_root()),
            worker_workspace.SANDBOX_PACKAGE_IMPORT_ROOT,
        )
        assert environment[_MCP.ENV_PYTHONPATH] == (
            worker_workspace.SANDBOX_PACKAGE_IMPORT_ROOT
        )
    else:
        assert spec["execution_backend"] == "windows_appcontainer"
        assert spec["repo_id"] == "repo-canonical"
        assert spec["worker_kind"] == "opencode_cli"
    adapter_argv = (
        worker_argv[worker_argv.index("--") + 1 :] if "--" in worker_argv else worker_argv
    )
    # Model pin and effort tokens are still the adapter plan's, untouched.
    assert adapter_argv[1:6] == ["run", "--format", "json", "--model", _OPENCODE_MODEL]
    assert adapter_argv[-1] == "prompt-text"
    assert "model" not in config and "provider" not in config
    # Request-local: the config lives only in the child environment.
    assert not list(tmp_path.rglob("opencode*.json"))
    assert all(
        config_text not in json.dumps(payload, default=str)
        for _path, payload in harness.writes
    )
    assert all(config_text not in token for token in worker_argv)


def _tamper_missing(harness: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    harness.runtime.claude_mcp_config_path.unlink()


def _tamper_symlink(harness: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation needs a privilege on Windows")
    source = harness.runtime.claude_mcp_config_path
    real = tmp_path / "real_config.json"
    source.replace(real)
    source.symlink_to(real)


def _tamper_malformed(harness: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    harness.runtime.claude_mcp_config_path.write_text("{not json", encoding="utf-8")


def _tamper_oversized(harness: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    harness.runtime.claude_mcp_config_path.write_bytes(b" " * 300_000)


def _tamper_unreadable(harness: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    name = harness.runtime.claude_mcp_config_path.name
    real_open = os.open

    def _denied(path: object, *args: object, **kwargs: object) -> int:
        if os.path.basename(os.fspath(path)) == name:
            raise PermissionError(13, "denied", os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _denied)


@pytest.mark.parametrize(
    ("backend", "cause", "tamper"),
    [
        ("landlock", "missing", _tamper_missing),
        ("landlock", "symlink", _tamper_symlink),
        ("windows_appcontainer", "malformed", _tamper_malformed),
        ("windows_appcontainer", "missing", _tamper_missing),
        ("landlock", "oversized", _tamper_oversized),
        ("landlock", "unreadable", _tamper_unreadable),
    ],
)
def test_opencode_launch_refuses_before_spawn_with_a_typed_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str, cause: str, tamper
) -> None:
    harness = _prepare_opencode_launch(monkeypatch, tmp_path, backend)
    tamper(harness, tmp_path, monkeypatch)

    result = _launch_opencode(harness)

    typed = f"opencode_worker_mcp_config_{cause}"
    assert result["ok"] is False
    assert str(result["blocked_reason"]).startswith(typed)
    assert harness.failed and harness.failed[0].startswith(typed)
    # Refused before the supervisor spec -- the last write before a spawn.
    assert _supervisor_spec(harness) is None
    assert harness.manager.popen_calls == []


@pytest.mark.skipif(os.name == "nt", reason="bubblewrap is a Linux sandbox")
def test_opencode_launch_refuses_a_command_the_bubblewrap_mount_cannot_show(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _prepare_opencode_launch(
        monkeypatch, tmp_path, "bubblewrap", command="/home/nobody/.venv/bin/python"
    )

    result = _launch_opencode(harness)

    assert result["ok"] is False
    assert str(result["blocked_reason"]).startswith(
        "opencode_worker_mcp_config_command_not_visible"
    )
    assert _supervisor_spec(harness) is None
    assert harness.manager.popen_calls == []


def test_opencode_appcontainer_launch_uses_real_provisioning_and_sandbox_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real AppContainer route: generated runtime in, supervisor spec out.

    Neither worker-MCP provisioning nor ``sandbox_argv`` is stubbed; only the
    host platform and the Win32 probe are.  Before the AppContainer branch
    existed, this exact path raised ``unsupported_sandbox_backend``.
    """
    real_provision = process_launcher._provision_worker_mcp_runtime_for_authority
    real_sandbox_argv = process_launcher.sandbox_argv
    harness = _prepare_opencode_launch(monkeypatch, tmp_path, "windows_appcontainer")
    monkeypatch.setattr(
        process_launcher, "_provision_worker_mcp_runtime_for_authority", real_provision
    )
    assert process_launcher.sandbox_argv is real_sandbox_argv
    workspace = harness.workspace

    result = _launch_opencode(harness)

    assert result["ok"] is True, result.get("blocked_reason")
    assert harness.failed == []
    spec = _supervisor_spec(harness)
    assert spec is not None
    assert spec["execution_backend"] == "windows_appcontainer"
    assert spec["repo_id"] == "repo-canonical"
    assert spec["worker_kind"] == "opencode_cli"
    worker_argv = [str(token) for token in spec["argv"]]
    # No Linux wrapper: the supervisor applies the AppContainer to this argv.
    assert "--landlock-exec" not in worker_argv
    assert worker_argv[1:6] == ["run", "--format", "json", "--model", _OPENCODE_MODEL]
    ((_argv, spawn),) = harness.manager.popen_calls
    env = spawn["env"]
    assert env["HOME"] == str(workspace.home)
    server = json.loads(env[runtime_adapters.OPENCODE_WORKER_CONFIG_ENV])["mcp"]["awh"]
    environment = server["environment"]
    assert environment[_MCP.ENV_REQUEST_ID] == _OPENCODE_REQUEST_ID
    assert environment[_MCP.ENV_REPO] == str(workspace.path)
    assert environment[_MCP.ENV_AUTHORITY_REPO] == str(harness.authority)
    ledger = Path(environment[_MCP.ENV_AUDIT_LEDGER_PATH])
    assert ledger.is_file()
    assert workspace.home.resolve() in ledger.resolve().parents


@pytest.mark.parametrize(
    ("windows_host", "probe_available", "cause"),
    [
        (False, True, "platform_not_windows"),
        (True, False, "win32_appcontainer_unavailable"),
    ],
)
def test_opencode_appcontainer_launch_refuses_without_measured_confinement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    windows_host: bool,
    probe_available: bool,
    cause: str,
) -> None:
    """No measured AppContainer means no spawn -- never an unconfined fallback."""
    harness = _prepare_opencode_launch(monkeypatch, tmp_path, "windows_appcontainer")
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: windows_host)
    monkeypatch.setattr(
        windows_appcontainer,
        "probe",
        lambda: SimpleNamespace(available=probe_available, detail="fake win32"),
    )

    result = _launch_opencode(harness)

    typed = f"windows_appcontainer_sandbox_unavailable:{cause}"
    assert result["ok"] is False
    assert typed in str(result["blocked_reason"])
    assert harness.failed and typed in harness.failed[0]
    assert _supervisor_spec(harness) is None
    assert harness.manager.popen_calls == []


def test_appcontainer_sandbox_argv_refuses_validation_shaped_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        windows_appcontainer,
        "probe",
        lambda: SimpleNamespace(available=True, detail="fake win32"),
    )
    workspace = worker_workspace.WorkerWorkspace(
        request_id=_OPENCODE_REQUEST_ID,
        repo=tmp_path / "authority",
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=("src/a.py",),
        parent_baseline={},
        workspace_baseline={},
    )

    assert worker_workspace.sandbox_argv(
        workspace, "opencode_cli", ["opencode", "run"], backend="windows_appcontainer"
    ) == ["opencode", "run"]
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="windows_appcontainer_validation_argv_unsupported",
    ):
        worker_workspace.sandbox_argv(
            workspace,
            "opencode_cli",
            ["opencode", "run"],
            backend="windows_appcontainer",
            outer_validation_authority=True,
        )


def test_a_non_opencode_launch_never_reads_or_receives_the_opencode_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _prepare_opencode_launch(monkeypatch, tmp_path, "landlock")
    harness.runtime.claude_mcp_config_path.unlink()

    result = _launch_opencode(
        harness, runner="codex_worker", adapter_id="codex_cli", model=None
    )

    assert result["ok"] is True, result
    env = harness.manager.popen_calls[0][1]["env"]
    assert not [key for key in env if key.startswith("OPENCODE_")]
    assert harness.env_calls[0]["provider_env"] is None
