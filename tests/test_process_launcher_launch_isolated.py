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
import builtins
import inspect
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
