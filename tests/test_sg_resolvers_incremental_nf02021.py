"""NF-2026-02021: the import/annotation/JS resolvers are incremental.

``_resolve_python_annotation_edges``, ``_resolve_python_import_edges`` and the
four JavaScript passes used to re-resolve the whole repository on every
incremental build and on every ``index_file``. They now take
``changed_files``/``affected_names`` like ``_resolve_cpp_cross_file_edges``
(NF-2026-02011). These tests pin (1) that a pass whose language has no changed
file and no affected name issues no UPDATE, measured by SQLite statement traces
and row-change counts, and (2)/(3) that a sequence of incremental builds -- and
of single-file index calls -- leaves the edges table identical to a fresh full
build of the same tree.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import source_graph as sg
from test_cpp_resolver_incremental_nf02011 import _new_repo, _write

SCOPED_RESOLVERS = (
    "_resolve_javascript_import_bindings",
    "_resolve_python_import_edges",
    "_resolve_python_annotation_edges",
    "_resolve_javascript_same_file_calls",
    "_resolve_javascript_imported_calls",
    "_resolve_javascript_mcp_tool_calls",
)
PYTHON_RESOLVERS = ("_resolve_python_import_edges", "_resolve_python_annotation_edges")
JS_RESOLVERS = (
    "_resolve_javascript_import_bindings",
    "_resolve_javascript_same_file_calls",
    "_resolve_javascript_imported_calls",
)
_WRITES = ("UPDATE", "DELETE", "INSERT")
SOURCE_TOPS = ("pkg", "vendor", "web")

USE_PY = (
    "from pkg.models import Foo, Keep\n"
    "from pkg.gone import gone_fn\n"
    "from pkg.extra import Thing\n"
    "import pkg.gone\n"
    "\n"
    "\n"
    "def use(x: Foo, y: Keep, z: Thing) -> Foo:\n"
    "    return x\n"
)

INITIAL_TREE = {
    "pkg/__init__.py": "",
    "pkg/models.py": "class Foo:\n    pass\n\n\nclass Keep:\n    pass\n",
    "pkg/gone.py": "def gone_fn():\n    return 1\n",
    "pkg/use.py": USE_PY,
    "web/lib.js": (
        "export function util(a) { return a; }\n"
        "export function other(b) { return b; }\n"
    ),
    "web/main.js": (
        "import { util, other } from './lib.js';\n"
        "function local() { return 1; }\n"
        "export function m() { return util(1) + other(2) + local(); }\n"
    ),
}

RENAMED_MODELS = "class Bar:\n    pass\n\n\nclass Keep:\n    pass\n"
VENDOR_MODELS = "class Keep:\n    pass\n"
EXTRA_PY = "class Thing:\n    pass\n"
RENAMED_LIB_JS = (
    "export function util2(a) { return a; }\n"
    "export function other(b) { return b; }\n"
)


def _source_tree(repo: Path) -> dict[str, str]:
    return {
        path.relative_to(repo).as_posix(): path.read_text(encoding="utf-8")
        for top in SOURCE_TOPS
        if (repo / top).is_dir()
        for path in sorted((repo / top).rglob("*"))
        if path.is_file()
    }


def _edges(repo: Path) -> list[tuple[str, ...]]:
    """Every edge with every column a resolver may write (no row ids)."""

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        rows = conn.execute(
            "SELECT file_path, kind, src_qualname, dst_name, dst_qualname, line, "
            "evidence_label, extractor, confidence, source_col, receiver_name "
            "FROM edges"
        ).fetchall()
    finally:
        conn.close()
    return sorted(tuple("" if value is None else str(value) for value in row) for row in rows)


def _full_build_edges(tmp_path: Path, repo: Path, label: str) -> list[tuple[str, ...]]:
    fresh = _new_repo(tmp_path, f"fresh_{label}")
    for rel, text in _source_tree(repo).items():
        _write(fresh, rel, text)
    sg.build_index(fresh, incremental=False)
    edges = _edges(fresh)
    shutil.rmtree(fresh, ignore_errors=True)
    return edges


def _dst(edges: list[tuple[str, ...]], file_path: str, kind: str, dst_name: str) -> str:
    matches = [e[4] for e in edges if e[0] == file_path and e[1] == kind and e[3] == dst_name]
    assert matches, f"no {kind} edge {file_path} -> {dst_name}"
    return matches[0]


@pytest.fixture()
def resolver_calls(monkeypatch) -> list[dict[str, Any]]:
    """Record each scoped resolver call with its scope and write statements."""

    calls: list[dict[str, Any]] = []
    for name in SCOPED_RESOLVERS:
        original = getattr(sg, name)

        def recording(conn, *args, _name=name, _original=original, **kwargs):
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            before = conn.total_changes
            try:
                result = _original(conn, *args, **kwargs)
            finally:
                conn.set_trace_callback(None)
            calls.append({
                "name": _name,
                "changed_files": kwargs.get("changed_files"),
                "affected_names": kwargs.get("affected_names"),
                "rows_changed": conn.total_changes - before,
                "writes": [
                    s for s in statements if s.lstrip().upper().startswith(_WRITES)
                ],
                "resolved": result,
            })
            return result

        recording.__wrapped__ = original  # type: ignore[attr-defined]
        monkeypatch.setattr(sg, name, recording)
    return calls


def _seed(tmp_path: Path) -> Path:
    repo = _new_repo(tmp_path, "repo")
    for rel, text in INITIAL_TREE.items():
        _write(repo, rel, text)
    return repo


def _by_name(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    named = {call["name"]: call for call in calls}
    assert set(named) == set(SCOPED_RESOLVERS), sorted(named)
    return named


def test_untouched_language_issues_no_update(tmp_path, resolver_calls):
    repo = _seed(tmp_path)
    sg.build_index(repo, incremental=True)

    # A Python-only edit: every JS pass gets empty scope sets and writes nothing.
    resolver_calls.clear()
    _write(repo, "pkg/gone.py", "def gone_fn():\n    return 2\n")
    result = sg.build_index(repo, incremental=True)
    assert result.files_changed == 1
    named = _by_name(resolver_calls)
    for name in JS_RESOLVERS:
        call = named[name]
        assert call["changed_files"] == set(), name
        assert call["affected_names"] == set(), name
        assert call["writes"] == [], name
        assert call["rows_changed"] == 0, name
        assert call["resolved"] == 0, name
    assert named["_resolve_python_import_edges"]["changed_files"] == {"pkg/gone.py"}
    assert "pkg.gone" in named["_resolve_python_import_edges"]["affected_names"]
    # No *_TOOLS registry and no JS file changed: the MCP pass stays silent too.
    assert named["_resolve_javascript_mcp_tool_calls"]["writes"] == []

    # A JS-only edit: neither Python pass writes anything.
    resolver_calls.clear()
    _write(repo, "web/main.js", INITIAL_TREE["web/main.js"] + "// touched\n")
    sg.build_index(repo, incremental=True)
    named = _by_name(resolver_calls)
    for name in PYTHON_RESOLVERS:
        call = named[name]
        assert call["changed_files"] == set(), name
        assert call["affected_names"] == set(), name
        assert call["writes"] == [], name
        assert call["rows_changed"] == 0, name
    assert named["_resolve_javascript_same_file_calls"]["changed_files"] == {"web/main.js"}

    # Both scope sets empty -> no UPDATE/DELETE/INSERT at all, for every pass.
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        for name in SCOPED_RESOLVERS:
            original = getattr(sg, name).__wrapped__
            args = () if name in PYTHON_RESOLVERS or name == SCOPED_RESOLVERS[0] else (repo,)
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            before = conn.total_changes
            assert original(conn, *args, changed_files=set(), affected_names=set()) == 0
            conn.set_trace_callback(None)
            assert conn.total_changes == before, name
            assert not [s for s in statements if s.lstrip().upper().startswith(_WRITES)], name
    finally:
        conn.close()


def _parity_steps() -> list[tuple[str, dict[str, str | None]]]:
    """Each step maps rel path -> new text, or None to delete the file."""

    return [
        ("rename_class", {"pkg/models.py": RENAMED_MODELS}),
        ("delete_module", {"pkg/gone.py": None}),
        ("introduce_ambiguity", {"vendor/pkg/models.py": VENDOR_MODELS}),
        ("new_module", {"pkg/extra.py": EXTRA_PY}),
        ("change_js_export", {"web/lib.js": RENAMED_LIB_JS}),
        ("remove_ambiguity", {"vendor/pkg/models.py": None}),
    ]


def _assert_initial(edges: list[tuple[str, ...]]) -> None:
    use = "pkg/use.py"
    assert _dst(edges, use, "annotates", "Foo") != ""
    assert _dst(edges, use, "annotates", "Keep") != ""
    assert _dst(edges, use, "annotates", "Thing") == ""
    assert _dst(edges, use, "imports", "pkg.gone.gone_fn") != ""
    assert _dst(edges, use, "imports", "pkg.gone") != ""
    assert _dst(edges, use, "imports", "pkg.extra.Thing") == ""
    assert _dst(edges, "web/main.js", "imports", "./lib.js") != ""
    assert _dst(edges, "web/main.js", "calls", "util") != ""
    assert _dst(edges, "web/main.js", "calls", "local") != ""


def _assert_step_semantics(label: str, edges: list[tuple[str, ...]]) -> None:
    # Guard against a vacuous parity: each step must move the edge it targets.
    use = "pkg/use.py"
    if label == "rename_class":
        assert _dst(edges, use, "annotates", "Foo") == ""
        assert _dst(edges, use, "imports", "pkg.models.Foo") == ""
    elif label == "delete_module":
        assert _dst(edges, use, "imports", "pkg.gone.gone_fn") == ""
    elif label == "introduce_ambiguity":
        assert _dst(edges, use, "annotates", "Keep") == ""
        assert _dst(edges, use, "imports", "pkg.models.Keep") == ""
    elif label == "new_module":
        assert _dst(edges, use, "annotates", "Thing") != ""
        assert _dst(edges, use, "imports", "pkg.extra.Thing") != ""
    elif label == "change_js_export":
        assert _dst(edges, "web/main.js", "calls", "util") == ""
        assert _dst(edges, "web/main.js", "calls", "other") != ""
    elif label == "remove_ambiguity":
        assert _dst(edges, use, "annotates", "Keep") != ""


def test_incremental_build_parity_with_full_build(tmp_path, resolver_calls):
    repo = _seed(tmp_path)
    sg.build_index(repo, incremental=True)
    initial = _edges(repo)
    assert initial == _full_build_edges(tmp_path, repo, "initial")
    _assert_initial(initial)

    for label, mutations in _parity_steps():
        for rel, text in mutations.items():
            if text is None:
                (repo / rel).unlink()
            else:
                _write(repo, rel, text)
        resolver_calls.clear()
        sg.build_index(repo, incremental=True)
        named = _by_name(resolver_calls)
        assert all(call["changed_files"] is not None for call in named.values()), label
        incremental_edges = _edges(repo)
        assert incremental_edges == _full_build_edges(tmp_path, repo, label), label
        _assert_step_semantics(label, incremental_edges)


def test_full_build_is_full_recompute(tmp_path, resolver_calls):
    repo = _seed(tmp_path)
    sg.build_index(repo, incremental=False)
    named = _by_name(resolver_calls)
    for name, call in named.items():
        assert call["changed_files"] is None, name
        assert call["affected_names"] is None, name


def test_single_file_index_parity_with_full_build(tmp_path, resolver_calls):
    repo = _seed(tmp_path)
    sg.build_index(repo, incremental=True)
    _assert_initial(_edges(repo))

    # ``index_file`` cannot delete, so deletion drops the definition instead.
    steps: list[tuple[str, str, str]] = [
        ("rename_class", "pkg/models.py", RENAMED_MODELS),
        ("delete_module", "pkg/gone.py", "GONE_NF02021 = 1\n"),
        ("introduce_ambiguity", "vendor/pkg/models.py", VENDOR_MODELS),
        ("new_module", "pkg/extra.py", EXTRA_PY),
        ("change_js_export", "web/lib.js", RENAMED_LIB_JS),
        # The vendor module still exists, so ``pkg.models`` stays ambiguous;
        # dropping its ``Keep`` must still leave every edge at full-build parity.
        ("drop_ambiguous_symbol", "vendor/pkg/models.py", "VENDOR_NF02021 = 1\n"),
    ]
    for label, rel, text in steps:
        _write(repo, rel, text)
        resolver_calls.clear()
        expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        result = sg.index_file(repo, rel, expected_hash)
        assert result["ok"] is True, label
        named = _by_name(resolver_calls)
        own = PYTHON_RESOLVERS if rel.endswith(".py") else JS_RESOLVERS
        for name in own:
            assert named[name]["changed_files"] == {rel}, (label, name)
        for name in (JS_RESOLVERS if rel.endswith(".py") else PYTHON_RESOLVERS):
            assert named[name]["changed_files"] == set(), (label, name)
            assert named[name]["writes"] == [], (label, name)
        edges = _edges(repo)
        assert edges == _full_build_edges(tmp_path, repo, f"single_{label}"), label
        _assert_step_semantics(label, edges)
