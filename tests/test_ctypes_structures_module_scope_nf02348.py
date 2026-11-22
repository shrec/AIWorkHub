"""NF-2026-02348: ctypes Structure/Union types live at module scope, never per call.

A ``ctypes.Structure`` subclass defined inside a function body is a new ctypes
type on every call. Passing ``ctypes.POINTER(cls)`` to ``argtypes`` or
``ctypes.cast`` makes ctypes cache that POINTER type for the process lifetime,
so a long-lived Windows MCP server retains one Structure type plus one pointer
type per call. The static rule below keeps every Structure/Union out of
function bodies. The one exception is a function decorated with
``functools.lru_cache`` or ``functools.cache``, which runs once per process.

The AST rule runs on every platform (CI is Linux-only). The live-census check
is Windows-only because the leak only exists where the helpers take the Win32
path.
"""

from __future__ import annotations

import ast
import ctypes
import gc
import os
import sys
from pathlib import Path

import pytest

from aiworkhub import core, doctor, platform_io, runtime_temp

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "aiworkhub"

_STRUCTURE_BASES = frozenset(
    {
        "Structure",
        "Union",
        "BigEndianStructure",
        "LittleEndianStructure",
        "BigEndianUnion",
        "LittleEndianUnion",
    }
)
_CACHE_DECORATORS = frozenset({"lru_cache", "cache"})
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _tail_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_cache_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return _tail_name(target) in _CACHE_DECORATORS


def _structure_classes_in_function_bodies(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, name)`` for each Structure/Union defined inside a function.

    A class nested under a cached function (``lru_cache``/``cache``) is allowed:
    the function runs once, so the class is created once.
    """
    found: list[tuple[int, str]] = []

    def walk(node: ast.AST, in_function: bool, cached: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                is_struct = any(_tail_name(base) in _STRUCTURE_BASES for base in child.bases)
                if is_struct and in_function and not cached:
                    found.append((child.lineno, child.name))
                walk(child, in_function, cached)
            elif isinstance(child, _FUNCTION_NODES):
                is_cached = cached or any(_is_cache_decorator(d) for d in child.decorator_list)
                walk(child, True, is_cached)
            else:
                walk(child, in_function, cached)

    walk(tree, False, False)
    return found


def test_rule_flags_per_call_structure_and_allows_cached_function() -> None:
    """The checker itself: it must catch the leak pattern and accept the cached exception."""
    leaking = ast.parse(
        "import ctypes\n"
        "def helper():\n"
        "    class S(ctypes.Structure):\n"
        "        _fields_ = []\n"
        "    return S\n"
    )
    cached = ast.parse(
        "import ctypes\n"
        "import functools\n"
        "@functools.lru_cache(maxsize=None)\n"
        "def kernel():\n"
        "    class S(ctypes.Structure):\n"
        "        _fields_ = []\n"
        "    return S\n"
    )
    module_scope = ast.parse("import ctypes\nclass S(ctypes.Structure):\n    _fields_ = []\n")
    assert _structure_classes_in_function_bodies(leaking) == [(3, "S")]
    assert _structure_classes_in_function_bodies(cached) == []
    assert _structure_classes_in_function_bodies(module_scope) == []


def test_no_structure_or_union_defined_inside_a_function_body() -> None:
    offenders: list[str] = []
    files = sorted(SRC_ROOT.rglob("*.py"))
    assert files, f"no source files found under {SRC_ROOT}"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, name in _structure_classes_in_function_bodies(tree):
            offenders.append(f"{rel}:{lineno} ({name})")
    assert not offenders, (
        "ctypes Structure/Union defined inside a function body (hoist it to module scope; "
        "only functions decorated with lru_cache/cache are exempt): " + ", ".join(offenders)
    )


def _structure_type_count() -> int:
    metaclass = type(ctypes.Structure)
    return sum(1 for obj in gc.get_objects() if type(obj) is metaclass)


@pytest.mark.skipif(sys.platform != "win32", reason="the per-call leak is Windows-only")
def test_windows_helpers_do_not_grow_live_structure_types(tmp_path: Path) -> None:
    import msvcrt

    probe = tmp_path / "handle_probe.txt"
    probe.write_text("probe", encoding="utf-8")
    with probe.open("rb") as handle_file:
        handle = msvcrt.get_osfhandle(handle_file.fileno())

        def call_all() -> None:
            platform_io.process_memory_bytes()
            platform_io.windows_process_tree()
            core._windows_process_owner_sid(os.getpid())
            doctor._windows_memory_counters()
            runtime_temp._windows_handle_attributes(handle)

        call_all()  # warm-up: the first call may create module-level POINTER types once
        gc.collect()
        before = _structure_type_count()
        for _ in range(50):
            call_all()
        gc.collect()
        after = _structure_type_count()

    assert after - before <= 2, (
        f"live ctypes Structure types grew by {after - before} over 50 calls "
        f"(before={before}, after={after})"
    )
