"""NF-2026-01729: the GC and vscode_lm extractions keep every monkeypatch seam.

``ProcessManager._gc_finalized_workspace`` and
``ProcessManager._invoke_vscode_lm_worker_tool_bound`` moved into sibling
modules to give ``process_launcher.py`` ratchet headroom.  The moved bodies
re-bind each ``process_launcher`` module-level name on entry; these tests pin
that the seam tuples are complete, that every seam still exists on
``process_launcher``, and that a patch of ``process_launcher`` reaches the moved
code.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import threading
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_vscode_lm_tool as vscode_lm_tool
from aiworkhub import process_launcher_workspace_gc as workspace_gc

PROCESS_LAUNCHER_MAX_LINES = 13600

# (module, function, seam tuple attribute, ProcessManager method)
EXTRACTIONS: tuple[tuple[ModuleType, str, str, str], ...] = (
    (
        workspace_gc,
        "gc_finalized_workspace",
        "GC_FINALIZED_WORKSPACE_SEAM_NAMES",
        "_gc_finalized_workspace",
    ),
    (
        vscode_lm_tool,
        "invoke_vscode_lm_worker_tool_bound",
        "VSCODE_LM_WORKER_TOOL_SEAM_NAMES",
        "_invoke_vscode_lm_worker_tool_bound",
    ),
)
EXTRACTION_IDS = [entry[1] for entry in EXTRACTIONS]


def _module_tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _module_globals(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return names


def _seam_rebinds(fn: ast.FunctionDef) -> list[str]:
    """Names re-bound as ``name = _pl.name`` in the function's top-level body."""
    rebinds: list[str] = []
    for stmt in fn.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Attribute)
            and isinstance(stmt.value.value, ast.Name)
            and stmt.value.value.id == "_pl"
        ):
            assert stmt.value.attr == stmt.targets[0].id, ast.dump(stmt)
            rebinds.append(stmt.targets[0].id)
    return rebinds


def _annotation_node_ids(fn: ast.FunctionDef) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(fn):
        for attr in ("annotation", "returns"):
            value = getattr(node, attr, None)
            if isinstance(value, ast.AST):
                ids.update(id(sub) for sub in ast.walk(value))
    return ids


def _body_locals(fn: ast.FunctionDef, seams: set[str]) -> set[str]:
    """Names the original body binds itself -- the seam re-binds excluded."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.arguments):
            for arg in (*node.posonlyargs, *node.args, *node.kwonlyargs):
                names.add(arg.arg)
            for extra in (node.vararg, node.kwarg):
                if extra is not None:
                    names.add(extra.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not fn:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    return names - seams


def test_process_launcher_has_ratchet_headroom() -> None:
    source = Path(str(process_launcher.__file__)).read_text(encoding="utf-8")
    assert len(source.splitlines()) <= PROCESS_LAUNCHER_MAX_LINES


@pytest.mark.parametrize(("module", "func_name", "seam_attr", "method"), EXTRACTIONS, ids=EXTRACTION_IDS)
def test_every_seam_exists_on_process_launcher_and_is_not_captured_at_import(
    module: ModuleType, func_name: str, seam_attr: str, method: str
) -> None:
    seams: tuple[str, ...] = getattr(module, seam_attr)
    assert seams, seam_attr
    assert list(seams) == sorted(set(seams)), "seam tuple must be sorted and unique"
    missing = [name for name in seams if not hasattr(process_launcher, name)]
    assert missing == []
    # A module attribute of the same name would be an import-time capture.
    captured = [name for name in seams if name in vars(module)]
    assert captured == []


@pytest.mark.parametrize(("module", "func_name", "seam_attr", "method"), EXTRACTIONS, ids=EXTRACTION_IDS)
def test_seam_tuple_is_the_complete_free_name_set_of_the_moved_body(
    module: ModuleType, func_name: str, seam_attr: str, method: str
) -> None:
    seams = set(getattr(module, seam_attr))
    tree = _module_tree(module)
    fn = _function_node(tree, func_name)

    # The preamble re-binds exactly the declared tuple, once each.
    rebinds = _seam_rebinds(fn)
    assert sorted(rebinds) == sorted(seams)

    builtin_names = set(dir(builtins))
    local_names = _body_locals(fn, seams)
    loads = {
        node.id for node in ast.walk(fn)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    free = loads - local_names - _module_globals(tree) - builtin_names
    assert free <= seams, sorted(free - seams)

    # Stronger: every runtime (non-annotation) load that is not a body local
    # or a builtin must be a seam -- a module-level import of a
    # ``process_launcher`` name in the new module would otherwise escape.
    annotation_ids = _annotation_node_ids(fn)
    runtime_free = {
        node.id for node in ast.walk(fn)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and id(node) not in annotation_ids
    } - local_names - builtin_names
    assert runtime_free <= seams, sorted(runtime_free - seams)
    # No stale seam: every declared name is actually used by the body.
    assert seams <= loads | {"_pl"}, sorted(seams - loads)
    # A builtin shadowed on ``process_launcher`` must be a seam too.
    shadowed = {n for n in (loads & builtin_names) - local_names if n in vars(process_launcher)}
    assert shadowed <= seams, sorted(shadowed - seams)


@pytest.mark.parametrize(("module", "func_name", "seam_attr", "method"), EXTRACTIONS, ids=EXTRACTION_IDS)
def test_process_manager_method_is_a_thin_delegation_with_the_same_signature(
    module: ModuleType, func_name: str, seam_attr: str, method: str
) -> None:
    impl = getattr(module, func_name)
    bound = getattr(process_launcher.ProcessManager, method)
    assert inspect.signature(bound) == inspect.signature(impl)
    method_node = next(
        node
        for cls in _module_tree(process_launcher).body
        if isinstance(cls, ast.ClassDef) and cls.name == "ProcessManager"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == method
    )
    statements = [
        stmt for stmt in method_node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    assert len(statements) == 1 and isinstance(statements[0], ast.Return)


class _GcSelf:
    """The ``self`` collaborators the GC body touches before ``superseded_reason``."""

    def __init__(self, latest: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self._latest = latest

    def _active_request_ids(self) -> set[str]:
        return set()

    def _request_lock(self, request_id: str) -> Any:
        return nullcontext()

    def _latest_by_request(self) -> dict[str, dict[str, Any]]:
        return {"req-1": self._latest}


def test_gc_moved_body_observes_a_process_launcher_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = sorted(process_launcher.GC_CANDIDATE_PROCESS_STATES)[0]
    event = {"state": state, "workspace_retained": True, "retention_rule_version": "v-event"}
    seen: list[tuple[Any, ...]] = []

    def _patched_superseded_reason(*args: Any) -> str:
        seen.append(args)
        return "patched_superseded_reason"

    monkeypatch.setattr(process_launcher, "superseded_reason", _patched_superseded_reason)
    fake = _GcSelf({"state": state, "workspace_retained": True, "retention_rule_version": "v-latest"})

    expected = {"request_id": "req-1", "gc": False, "reason": "patched_superseded_reason"}
    assert workspace_gc.gc_finalized_workspace(fake, "req-1", event) == expected
    # The ProcessManager delegation reaches the same patched object.
    assert process_launcher.ProcessManager._gc_finalized_workspace(fake, "req-1", event) == expected
    assert seen == [(process_launcher.WORKTREE_RETENTION_RULE_VERSION, "v-event", "v-latest")] * 2


class _BridgeSelf:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def _request_events(self, request_id: str) -> list[dict[str, Any]]:
        return self._events


def test_vscode_lm_moved_body_observes_a_process_launcher_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [{"marker": "nf01729"}]
    seen: list[list[dict[str, Any]]] = []

    def _patched_split_lifecycle_tail(received: list[dict[str, Any]]) -> tuple[Any, ...]:
        seen.append(received)
        return ({"adapter_id": "patched-not-a-vscode-adapter"}, [])

    monkeypatch.setattr(process_launcher, "split_lifecycle_tail", _patched_split_lifecycle_tail)
    fake = _BridgeSelf(events)
    request_id = "a" * 32

    expected = {"ok": False, "reason": "worker_bridge_adapter_mismatch"}
    assert vscode_lm_tool.invoke_vscode_lm_worker_tool_bound(
        fake, request_id, "aiworkhub_worker_kb_search", {}
    ) == expected
    assert process_launcher.ProcessManager._invoke_vscode_lm_worker_tool_bound(
        fake, request_id, "aiworkhub_worker_kb_search", {}
    ) == expected
    assert seen == [events, events]
