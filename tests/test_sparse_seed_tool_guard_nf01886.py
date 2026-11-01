from __future__ import annotations

import ast
from pathlib import Path

from aiworkhub import worker_workspace

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _REPO_ROOT / "src" / "aiworkhub"
_HOOK = "src/aiworkhub/tool_guard_hook.py"


def _file_with_name_literals(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_name"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.endswith(".py")
        ):
            continue
        base = node.func.value
        if (
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Name)
            and base.func.id == "Path"
            and len(base.args) == 1
            and isinstance(base.args[0], ast.Name)
            and base.args[0].id == "__file__"
        ):
            names.add(node.args[0].value)
    return names


def test_support_tuple_names_the_tool_guard_hook_and_loaders_are_unchanged() -> None:
    assert _HOOK in worker_workspace._VALIDATION_WORKER_PACKAGE_SUPPORT
    assert "tool_guard_hook.py" not in {
        filename for _name, filename in worker_workspace.RUNTIME_LOADED_PACKAGE_SIBLINGS
    }


def test_sparse_seed_closure_includes_tool_guard_hook(tmp_path: Path) -> None:
    for relative, text in (
        ("src/aiworkhub/__init__.py", ""),
        ("src/aiworkhub/example.py", "VALUE = 1\n"),
        (_HOOK, "HOOK = 1\n"),
        ("tests/test_example.py", "def test_x():\n    assert True\n"),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    card = {
        "allowed_writes": ["src/aiworkhub/example.py"],
        "validation": ["python -m pytest tests/test_example.py -q"],
    }

    _live, _support, seeded = worker_workspace._declared_workspace_seed_closure(
        tmp_path, card, ("src/aiworkhub/example.py",)
    )

    assert _HOOK in seeded


def test_every_with_name_sibling_read_in_the_package_is_seeded() -> None:
    support = set(worker_workspace._VALIDATION_WORKER_PACKAGE_SUPPORT)
    reached = support | set(
        worker_workspace._resolve_local_python_imports(_REPO_ROOT, tuple(sorted(support)))
    )
    unseeded: dict[str, str] = {}
    scanned = 0
    for source in sorted(_PACKAGE.glob("*.py")):
        names = _file_with_name_literals(
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        )
        scanned += bool(names)
        for name in names:
            if f"src/aiworkhub/{name}" not in reached:
                unseeded[f"src/aiworkhub/{name}"] = source.name
    assert scanned, "the with_name scan found no sibling reads at all"
    assert not unseeded
