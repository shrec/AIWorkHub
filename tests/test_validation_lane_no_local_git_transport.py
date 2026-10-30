"""Static guard: no test may use git's local transport (clone, push, fetch).

NF-2026-01876. The AppContainer validation lane cannot run git's local
transport (clone/push/fetch of a local bare repo exits 128), so any card whose
validation includes such a test cannot reach review_ready. The portable pattern
is in ``tests/support/retention.py:repository()``: ``git init``,
``remote add origin <path>``, commit, then
``update-ref refs/remotes/origin/main HEAD``.

This test parses every ``tests/**/*.py`` (except itself) and fails listing
``path:line`` for each call that passes "clone", "push" or "fetch" to git.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent
_TRANSPORT_VERBS = frozenset({"clone", "push", "fetch"})
_GIT_HELPER_NAMES = frozenset({"git", "_git"})


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_constants(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
    return []


def _is_git_call(call: ast.Call) -> bool:
    if _call_name(call) in _GIT_HELPER_NAMES:
        return True
    if not call.args or not isinstance(call.args[0], (ast.List, ast.Tuple)):
        return False
    first = call.args[0].elts[:1]
    return bool(first) and isinstance(first[0], ast.Constant) and first[0].value == "git"


def find_transport_calls(source: str) -> list[int]:
    """Return the line numbers of git calls that use clone, push or fetch."""
    hits: list[int] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Assert):
            return
        if isinstance(node, ast.Call) and _is_git_call(node):
            strings = [s for arg in node.args for s in _string_constants(arg)]
            if _TRANSPORT_VERBS.intersection(strings):
                hits.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return sorted(hits)


def test_scanner_flags_transport_and_ignores_prose() -> None:
    source = (
        '"""git clone in a docstring"""\n'
        "def f(tmp, repo):\n"
        '    _git(tmp, "clone", "a", "b")\n'
        '    git(repo, "push", "origin", "HEAD")\n'
        '    subprocess.run(["git", "-C", "x", "fetch", "origin"], check=True)\n'
        '    subprocess.run(["git", "status"], stderr="clone")\n'
        '    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")\n'
        '    assert helper("push")\n'
    )
    assert find_transport_calls(source) == [3, 4, 5]


def test_no_test_uses_local_git_transport() -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        for line in find_transport_calls(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_TESTS_ROOT.parent).as_posix()}:{line}")
    assert not offenders, (
        "git clone/push/fetch is unavailable in the validation lane "
        "(NF-2026-01876); use tests/support/retention.py:repository() pattern:\n"
        + "\n".join(offenders)
    )
