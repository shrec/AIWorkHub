"""NF-2026-00993: one import-closure run checks each path and parses each module once.

The expected lists below are the outputs of the pre-memoisation resolver for the
same tree: the per-run memo and the single AST pass must change how often the
filesystem is asked, never what is seeded or which refusal is raised.

Measured on a sparse worktree of the canonical repository (card proxy with 16
Python closure seeds, 172 modules parsed): ``_declared_workspace_seed_closure``
31.9-43.8 s before, 9.2-11.1 s after, seeded-list sha256 unchanged.  After the
memo the resolver spends ~3.3 s in ``ast.parse`` of ~8.5 s; parsing does not
dominate (and ``compile`` holds the GIL), so it stays sequential.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from aiworkhub import python_import_closure
from aiworkhub.worker_workspace import (
    WorkspaceError,
    _declared_workspace_seed_closure,
    _resolve_local_python_imports,
)

_SEED = "\n".join(
    (
        "import importlib",
        "importlib.import_module('pkg.dynamic_target')",
        "from . import sibling",
        "from .star_source import *",
        # The star import above taints importlib, so this literal is not followed.
        "importlib.import_module('pkg.after_star')",
        "import extlib",
        "",
    )
)

_CLOSURE = (
    "src/pkg/__init__.py",
    "src/pkg/dynamic_target.py",
    "src/pkg/leaf.py",
    "src/pkg/seed.py",
    "src/pkg/sibling.py",
    "src/pkg/star_source.py",
)


def _write(path: Path, source: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _package_tree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(outside / "extlib.py", "SECRET = 1\n")
    _write(repo / "src/pkg/__init__.py")
    _write(repo / "src/pkg/seed.py", _SEED)
    _write(repo / "src/pkg/sibling.py", "import extlib\nfrom .leaf import value\n")
    _write(repo / "src/pkg/leaf.py", "value = 1\n")
    _write(repo / "src/pkg/star_source.py", "from os.path import *\n")
    _write(repo / "src/pkg/dynamic_target.py")
    _write(repo / "src/pkg/after_star.py")
    _write(repo / "tests/test_seed.py", "from pkg import seed\n")
    (repo / "tools").mkdir()
    return repo, outside


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def _count_resolve_and_parse(monkeypatch: pytest.MonkeyPatch) -> tuple[Counter, Counter]:
    resolved: Counter[str] = Counter()
    parsed: Counter[str] = Counter()
    original_resolve = Path.resolve
    original_parse = ast.parse

    def counting_resolve(self: Path, strict: bool = False) -> Path:
        resolved[str(self)] += 1
        return original_resolve(self, strict=strict)

    def counting_parse(source, filename="<unknown>", *args, **kwargs):
        parsed[str(filename)] += 1
        return original_parse(source, filename, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    monkeypatch.setattr(python_import_closure.ast, "parse", counting_parse)
    return resolved, parsed


def test_symlink_escape_tree_keeps_exact_lists(tmp_path: Path) -> None:
    repo, outside = _package_tree(tmp_path)
    # ``import extlib`` reaches this escape only through a fallback directory,
    # whose refusal is swallowed -- by both modules that import it.
    _symlink_or_skip(repo / "tools/extlib.py", outside / "extlib.py")

    assert _resolve_local_python_imports(repo, ("src/pkg/seed.py",)) == _CLOSURE

    card = {
        "allowed_writes": ["src/pkg/seed.py"],
        "validation": ["python -m pytest -q tests/test_seed.py"],
    }
    live, support, seeded = _declared_workspace_seed_closure(
        repo, card, tuple(card["allowed_writes"])
    )
    assert live == ["src/pkg/seed.py", "tests/test_seed.py"]
    assert support == (
        "src/pkg/__init__.py",
        "src/pkg/dynamic_target.py",
        "src/pkg/leaf.py",
        "src/pkg/sibling.py",
        "src/pkg/star_source.py",
    )
    assert seeded == sorted(set(live) | set(support))


def test_each_candidate_resolved_and_each_module_parsed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, outside = _package_tree(tmp_path)
    _symlink_or_skip(repo / "tools/extlib.py", outside / "extlib.py")
    resolved, parsed = _count_resolve_and_parse(monkeypatch)

    assert _resolve_local_python_imports(repo, ("src/pkg/seed.py",)) == _CLOSURE

    # ``importlib`` is probed by the import walk, the stdlib-binding check and
    # the binding check; ``extlib`` by two modules and every fallback directory.
    # Every distinct path still resolves only once (the repo root included).
    assert len(resolved) >= 10
    assert max(resolved.values()) == 1, resolved.most_common(3)
    assert set(parsed) == set(_CLOSURE)
    assert max(parsed.values()) == 1


def test_fatal_symlink_escape_keeps_its_refusal(tmp_path: Path) -> None:
    repo, outside = _package_tree(tmp_path)
    _symlink_or_skip(repo / "src/pkg/escaped.py", outside / "extlib.py")
    _write(repo / "src/pkg/leaf.py", "value = 1\nfrom . import escaped\n")

    with pytest.raises(WorkspaceError) as caught:
        _resolve_local_python_imports(repo, ("src/pkg/seed.py",))

    expected = repo.resolve() / "src" / "pkg" / "escaped.py"
    assert str(caught.value) == f"symlink_path_component_forbidden:{expected}"


def test_no_verdict_survives_between_runs(tmp_path: Path) -> None:
    repo, _outside = _package_tree(tmp_path)
    assert _resolve_local_python_imports(repo, ("src/pkg/seed.py",)) == _CLOSURE

    # The tree changes between launches; a second run must see it afresh.
    _write(repo / "src/pkg/leaf.py", "value = 1\nfrom . import late\n")
    _write(repo / "src/pkg/late.py")
    (repo / "src/pkg/dynamic_target.py").unlink()

    assert _resolve_local_python_imports(repo, ("src/pkg/seed.py",)) == (
        "src/pkg/__init__.py",
        "src/pkg/late.py",
        "src/pkg/leaf.py",
        "src/pkg/seed.py",
        "src/pkg/sibling.py",
        "src/pkg/star_source.py",
    )
