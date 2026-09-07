"""``accept_review`` was moved out of ``process_launcher``; the seams must survive.

``ProcessManager.accept_review`` used to be 1183 lines inside
``process_launcher`` -- the single largest method in the repository's
highest-churn file.  It now lives in
:mod:`aiworkhub.process_launcher_accept_review` and the method is a delegation.

The move is only safe because the body still resolves its module-level
collaborators through the ``process_launcher`` module object.  27 test files
monkeypatch that module across 48 attributes; a relocation that captured those
names at import time instead would sever the seam **silently** -- the patch
would rebind ``process_launcher.X`` while the moved code kept calling the
original, and every one of those tests would still pass while testing nothing.

These tests are the guard.  They assert the binding list is exactly the moved
function's free-variable set (so a future edit that introduces a new global
cannot quietly escape the mechanism), and they drive two of the seams through
the real call path to prove a patch still lands.
"""

from __future__ import annotations

import ast
import builtins
import inspect
from pathlib import Path

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_accept_review


SOURCE = Path(process_launcher_accept_review.__file__)


class _SeamReached(Exception):
    """Sentinel raised from a patched seam.

    Deliberately not a subclass of anything in the moved body's
    ``except (LaunchRejected, AttributeError, TypeError)`` pre-read guard, so
    reaching the seam is observable rather than swallowed.
    """


class _StubManager:
    """The moved function takes ``self`` as an ordinary parameter."""

    def _request_events(self, request_id: str) -> list[dict[str, object]]:
        return [{"task_id": "T-1", "request_id": request_id}]

    def _show_task(self, task_id: str) -> dict[str, object]:
        return {"task_id": task_id}


def _moved_function_node() -> ast.FunctionDef:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "accept_review"
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


def _annotation_node_ids(fn: ast.FunctionDef) -> set[int]:
    """Every node reachable from an annotation.

    ``process_launcher_accept_review`` sets ``from __future__ import
    annotations``, so annotations are strings that are never evaluated.  A name
    appearing only there is not resolved at runtime and therefore cannot carry
    a monkeypatch -- it must NOT be re-bound, or it would be hidden from the
    type checker for no benefit.
    """
    ids: set[int] = set()
    for node in ast.walk(fn):
        annotation = getattr(node, "annotation", None)
        if annotation is not None:
            ids.update(id(child) for child in ast.walk(annotation))
    for arg in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs:
        if arg.annotation is not None:
            ids.update(id(child) for child in ast.walk(arg.annotation))
    if fn.returns is not None:
        ids.update(id(child) for child in ast.walk(fn.returns))
    return ids


def _free_names_of_moved_function() -> set[str]:
    """The names the body resolves AT RUNTIME without the seam preamble.

    The preamble binds every one of these as a local, so an ordinary
    free-variable pass over the current source returns nothing.  Ignoring the
    preamble's own assignments recovers the set the body actually depends on --
    which is precisely the set that has to be re-bound.  Annotation-only names
    are excluded because they are never evaluated.
    """
    fn = _moved_function_node()
    preamble = _preamble_bindings(fn)
    annotation_ids = _annotation_node_ids(fn)

    bound = {arg.arg for arg in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
    if fn.args.vararg:
        bound.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        bound.add(fn.args.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id not in preamble:
                bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not fn:
                bound.add(node.name)

    used = {
        node.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and id(node) not in annotation_ids
    }
    return {name for name in used - bound if not hasattr(builtins, name)}


def test_declared_seam_names_are_exactly_the_functions_free_variables():
    """A new global in the body must be added to the preamble, or this fails.

    The preamble is what keeps the seams connected.  A runtime free name that is
    not in it would raise ``NameError``; a name in the preamble that the body no
    longer uses at runtime is dead weight that also hides the name from the type
    checker.  Both are drift, and both are caught here.
    """
    declared = set(process_launcher_accept_review.ACCEPT_REVIEW_SEAM_NAMES)
    assert declared == _free_names_of_moved_function()


def test_every_declared_seam_is_still_an_attribute_of_process_launcher():
    """Each name must resolve on the module the tests actually patch."""
    missing = [
        name
        for name in process_launcher_accept_review.ACCEPT_REVIEW_SEAM_NAMES
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

    # The module is imported inside the function, so the binding below reads the
    # live module object rather than a name captured when this module loaded.
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
        for name in process_launcher_accept_review.ACCEPT_REVIEW_SEAM_NAMES
        if name not in bindings
    ]
    assert missing == []


def test_delegating_method_keeps_the_original_signature():
    """Callers and the 16 test files that reference the name see no change."""
    assert str(inspect.signature(process_launcher.ProcessManager.accept_review)) == str(
        inspect.signature(process_launcher_accept_review.accept_review)
    )


def test_process_launcher_method_is_a_delegation_and_holds_no_logic():
    """The point of the move: the body is gone from process_launcher."""
    source = inspect.getsource(process_launcher.ProcessManager.accept_review)
    assert len(source.splitlines()) < 40
    assert "_accept_review_impl(" in source


@pytest.mark.parametrize("seam", ["_parse_card", "_canonical_task_status"])
def test_patching_process_launcher_still_reaches_the_moved_body(monkeypatch, seam):
    """Drive two real seams through the real call path.

    ``_parse_card`` and ``_canonical_task_status`` are both consumed by the
    pre-read at the top of the moved body.  Patching them on
    ``process_launcher`` must change what the moved code executes; if the
    relocation had captured either name at import time the sentinel would never
    be raised and this test would fail.
    """
    if seam == "_canonical_task_status":
        # _parse_card runs first; let it succeed so the later seam is reached.
        monkeypatch.setattr(process_launcher, "_parse_card", lambda *a, **k: {})

    def _sentinel(*args, **kwargs):
        raise _SeamReached(seam)

    monkeypatch.setattr(process_launcher, seam, _sentinel)

    with pytest.raises(_SeamReached) as excinfo:
        process_launcher.ProcessManager.accept_review(
            _StubManager(), "R-1", "T-1"
        )
    assert str(excinfo.value) == seam
