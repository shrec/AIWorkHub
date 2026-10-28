"""NF-2026-01216: the package gate carries the MCP tool-surface contract tests.

Twice a card turned main red because the server-appended gate ran only the
size ratchet and declared invariants: a 168-word dashboard_mcp_app tool
docstring passed its own card and was caught only by combined-tree
revalidation at accept. A card writing an MCP surface module now carries the
surface gate on both the derived validation and a caller override, and a
drift guard keeps MCP_SURFACE_MODULES in step with the modules that register
tools.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_templates  # noqa: E402
from aiworkhub.task_templates import expand_template, with_package_gate  # noqa: E402

_PACKAGE_DIR = _SRC / "aiworkhub"


def _pytest(tests: tuple[str, ...]) -> str:
    return " ".join([task_templates.COMMAND_PYTHON, "-m", "pytest", "-q", *tests])


_PACKAGE_GATE = _pytest(task_templates.PACKAGE_GATE_TESTS)
_SURFACE_GATE = _pytest(task_templates.MCP_SURFACE_GATE_TESTS)
_OVERRIDE = ["python3 -m pytest -q tests/test_a.py", "git diff --check"]
_ROLES = ["reproduction", "regression"]


def _bugfix(production: str) -> dict:
    return expand_template(
        "bugfix_with_regression",
        production_paths=[production],
        test_paths=["tests/test_a.py"],
    )


def _receiver_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _registers_mcp_tools(source: str) -> bool:
    """The registration signal MCP_SURFACE_MODULES was built from.

    Any ``<mcp>.tool`` / ``<mcp>.add_tool`` reference -- the ``@mcp.tool()``
    decorator, ``mcp.tool(name=name)(fn)`` or an alias of either -- on a
    receiver whose name contains ``mcp``.
    """
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"tool", "add_tool"}
            and "mcp" in _receiver_name(node.value).lower()
        ):
            return True
    return False


def _tool_registering_modules() -> set[str]:
    return {
        f"src/aiworkhub/{path.name}"
        for path in sorted(_PACKAGE_DIR.glob("*.py"))
        if _registers_mcp_tools(path.read_text(encoding="utf-8"))
    }


@pytest.mark.parametrize(
    "production", ["src/aiworkhub/dashboard_mcp_app.py", "src/aiworkhub/server.py"]
)
def test_a_surface_card_derives_the_surface_gate_after_the_package_gate(production):
    card = _bugfix(production)
    assert card["validation"][-2:] == [_PACKAGE_GATE, _SURFACE_GATE]
    assert card["validation"].count(_SURFACE_GATE) == 1
    assert len(card["validation_roles"]) == len(card["validation"])
    assert card["validation_roles"][-1] == "generic"


@pytest.mark.parametrize(
    "production", ["src/aiworkhub/dashboard_mcp_app.py", "src/aiworkhub/server.py"]
)
def test_a_surface_card_keeps_the_surface_gate_on_an_override(production):
    card = _bugfix(production)
    validation, roles = with_package_gate(card, _OVERRIDE, _ROLES)
    assert validation == [*_OVERRIDE, _PACKAGE_GATE, _SURFACE_GATE]
    assert roles == [*_ROLES, "generic", "generic"]
    # Already named -- even under the bare ``python`` spelling -- is not doubled.
    legacy = [*_OVERRIDE, _SURFACE_GATE.replace("python3 ", "python ", 1)]
    validation, roles = with_package_gate(card, legacy, [*_ROLES, "generic"])
    assert validation == [*legacy, _PACKAGE_GATE]
    assert roles == [*_ROLES, "generic", "generic"]
    assert with_package_gate(card, validation, roles) == (validation, roles)


def test_a_non_surface_package_card_gets_exactly_the_package_gate():
    card = _bugfix("src/aiworkhub/skill_registry.py")
    assert card["validation"][-1] == _PACKAGE_GATE
    assert card["validation"].count(_PACKAGE_GATE) == 1
    assert _SURFACE_GATE not in card["validation"]
    assert with_package_gate(card, _OVERRIDE, _ROLES) == (
        [*_OVERRIDE, _PACKAGE_GATE],
        [*_ROLES, "generic"],
    )


_RATCHET = "tests/test_module_size_ratchet.py"
_INVARIANTS = "tests/test_declared_invariants.py"
_DENIAL_NODE = (
    "tests/test_aiworkhub_dependency_autolaunch_b905_v7.py"
    "::test_every_launch_denial_reason_in_the_codebase_is_classified"
)


@pytest.mark.parametrize(
    "production", ["src/aiworkhub/skill_registry.py", "src/aiworkhub/server.py"]
)
def test_a_card_carrying_the_legacy_gate_runs_each_gate_test_once(production):
    """A stored or override legacy gate is kept and only the new node appended."""
    card = _bugfix(production)
    legacy_gate = _pytest((_RATCHET, _INVARIANTS))
    stored = [*_OVERRIDE, legacy_gate]
    validation, roles = with_package_gate(card, stored, [*_ROLES, "generic"])

    assert validation[: len(stored)] == stored
    assert _PACKAGE_GATE not in validation
    tokens = [token for command in validation for token in command.split()]
    assert tokens.count(_RATCHET) == 1
    assert tokens.count(_INVARIANTS) == 1
    assert tokens.count(_DENIAL_NODE) == 1
    assert len(roles) == len(validation)
    # Re-applying the gate to its own output is byte for byte stable.
    assert with_package_gate(card, validation, roles) == (validation, roles)


def test_a_read_only_surface_card_carries_no_gate():
    card = expand_template(
        "read_only_analysis", production_paths=["src/aiworkhub/server.py"]
    )
    assert _PACKAGE_GATE not in card["validation"]
    assert _SURFACE_GATE not in card["validation"]
    forged = {"allowed_writes": ["src/aiworkhub/server.py"], "read_only": True}
    assert with_package_gate(forged, _OVERRIDE, _ROLES) == (_OVERRIDE, _ROLES)


def test_the_registration_signal_flags_a_new_tool_module():
    """A module the guard cannot see would skip the gate silently."""
    assert _registers_mcp_tools("@mcp.tool()\ndef aiworkhub_x() -> dict:\n    return {}\n")
    assert _registers_mcp_tools("def register(mcp):\n    mcp.tool(name='x')(print)\n")
    assert _registers_mcp_tools("def register(self):\n    self._mcp.add_tool(print)\n")
    assert not _registers_mcp_tools("def tool(x):\n    return x.tool\n")


def test_every_tool_registering_module_is_an_mcp_surface_module():
    found = _tool_registering_modules()
    # Guard against a vacuous pass: a renamed decorator must not empty the scan.
    assert {"src/aiworkhub/server.py", "src/aiworkhub/dashboard_mcp_app.py"} <= found
    missing = sorted(found - task_templates.MCP_SURFACE_MODULES)
    assert missing == [], (
        "modules register MCP tools but are absent from "
        f"task_templates.MCP_SURFACE_MODULES, so their cards skip the surface gate: {missing}"
    )


def test_every_mcp_surface_module_exists():
    stale = sorted(
        name for name in task_templates.MCP_SURFACE_MODULES
        if not (_ROOT / name).is_file()
    )
    assert stale == []


def test_the_surface_gate_tests_need_nothing_a_worktree_lacks():
    """A worker worktree is a sparse checkout with no scripts/ directory."""
    for name in task_templates.MCP_SURFACE_GATE_TESTS:
        tree = ast.parse((_ROOT / name).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and not node.level
        }
        assert "scripts" not in imported, name
