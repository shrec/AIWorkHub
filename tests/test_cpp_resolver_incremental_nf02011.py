"""NF-2026-02011: the lexical cross-file resolver is incremental.

``_resolve_cpp_cross_file_edges`` used to NULL and re-resolve every
cpp/polyglot/tree-sitter lexical edge repo-wide on every build. It now resets
only edges owned by a changed file or aimed at a name this build added,
removed or renamed. These tests pin (1) that a Python-only change resets zero
lexical edges, measured by SQLite row-change counts rather than wall-clock,
and (2)/(3) that a sequence of incremental builds -- and of single-file index
calls -- leaves every lexical edge's ``dst_qualname`` identical to a fresh
full build of the same tree.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub.repository_state import bootstrap_repository

LEXICAL_EXTRACTORS = (
    sgast.CPP_LEXICAL_EXTRACTOR_ID,
    sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID,
    sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID,
)

INITIAL_TREE = {
    "native/target.cpp": "int unique_target(int x) { return x + 1; }\n",
    "native/util.cpp": "int rename_me(int x) { return x * 2; }\n",
    "native/solo.cpp": "int solo(int x) { return x - 1; }\n",
    "native/lib_a.hpp": "int dup(int x) { return 1; }\n",
    "native/lib_b.hpp": "int dup(int x) { return 2; }\n",
    "native/main.cpp": (
        "int main_fn() { return unique_target(1) + rename_me(2) + solo(3); }\n"
    ),
    "native/other.cpp": "int other_fn() { return renamed_fn(4); }\n",
    "native/user.cpp": (
        '#include "lib_a.hpp"\n'
        "int user_fn() { return dup(5); }\n"
    ),
    "py/app.py": "def py_entry():\n    return 1\n",
}


def _new_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _source_tree(repo: Path) -> dict[str, str]:
    return {
        path.relative_to(repo).as_posix(): path.read_text(encoding="utf-8")
        for top in ("native", "py")
        if (repo / top).is_dir()
        for path in sorted((repo / top).rglob("*"))
        if path.is_file()
    }


def _lexical_edges(repo: Path) -> list[tuple[str, str, str, str, str]]:
    placeholders = ",".join("?" for _ in LEXICAL_EXTRACTORS)
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        rows = conn.execute(
            "SELECT file_path, extractor, kind, dst_name, dst_qualname FROM edges "
            f"WHERE extractor IN ({placeholders}) AND kind IN ('calls','inherits')",
            LEXICAL_EXTRACTORS,
        ).fetchall()
    finally:
        conn.close()
    return sorted(
        (str(r[0]), str(r[1]), str(r[2]), str(r[3]), "" if r[4] is None else str(r[4]))
        for r in rows
    )


def _full_build_edges(tmp_path: Path, repo: Path, label: str) -> list[tuple[str, ...]]:
    fresh = _new_repo(tmp_path, f"fresh_{label}")
    for rel, text in _source_tree(repo).items():
        _write(fresh, rel, text)
    sg.build_index(fresh, incremental=False)
    edges = _lexical_edges(fresh)
    shutil.rmtree(fresh, ignore_errors=True)
    return edges


def _dst(edges: list[tuple[str, ...]], file_path: str, dst_name: str) -> str:
    matches = [e[4] for e in edges if e[0] == file_path and e[3] == dst_name and e[2] == "calls"]
    assert matches, f"no lexical call edge {file_path} -> {dst_name}: {edges}"
    return matches[0]


@pytest.fixture()
def resolver_calls(monkeypatch) -> list[dict[str, Any]]:
    """Record each resolver call with its scope and SQLite row-change count."""

    calls: list[dict[str, Any]] = []
    original = sg._resolve_cpp_cross_file_edges

    def recording(conn, **kwargs):
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        before = conn.total_changes
        try:
            result = original(conn, **kwargs)
        finally:
            conn.set_trace_callback(None)
        calls.append({
            "changed_files": kwargs.get("changed_files"),
            "affected_names": kwargs.get("affected_names"),
            "rows_changed": conn.total_changes - before,
            "reset_statements": [
                s for s in statements if "SET dst_qualname=NULL" in s
            ],
            "resolved": result,
        })
        return result

    recording.__wrapped__ = original  # type: ignore[attr-defined]
    monkeypatch.setattr(sg, "_resolve_cpp_cross_file_edges", recording)
    return calls


def _seed(tmp_path: Path) -> Path:
    repo = _new_repo(tmp_path, "repo")
    for rel, text in INITIAL_TREE.items():
        _write(repo, rel, text)
    return repo


def test_python_only_change_resets_zero_lexical_edges(tmp_path, resolver_calls):
    repo = _seed(tmp_path)
    sg.build_index(repo, incremental=True)
    baseline = _lexical_edges(repo)
    assert any(edge[4] for edge in baseline), "fixture must resolve lexical edges"

    # A body-only edit plus a brand-new Python function whose name no lexical
    # edge targets: neither can change any C++ edge's candidate set.
    resolver_calls.clear()
    _write(
        repo, "py/app.py",
        "def py_entry():\n    return 2\n\n\ndef py_only_new_helper():\n    return 3\n",
    )
    result = sg.build_index(repo, incremental=True)
    assert result.files_changed == 1
    assert len(resolver_calls) == 1
    call = resolver_calls[0]
    assert call["changed_files"] == {"py/app.py"}
    assert "py_only_new_helper" in call["affected_names"]
    assert call["rows_changed"] == 0
    assert call["resolved"] == 0
    assert _lexical_edges(repo) == baseline

    # A body-only edit alone: no entity name changes, zero lexical rows touched.
    resolver_calls.clear()
    _write(
        repo, "py/app.py",
        "def py_entry():\n    return 7\n\n\ndef py_only_new_helper():\n    return 3\n",
    )
    sg.build_index(repo, incremental=True)
    assert len(resolver_calls) == 1
    call = resolver_calls[0]
    assert call["affected_names"] == set()
    assert call["rows_changed"] == 0
    assert _lexical_edges(repo) == baseline

    # Both scope sets empty -> the resolver issues no UPDATE at all.
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        before = conn.total_changes
        assert sg._resolve_cpp_cross_file_edges.__wrapped__(
            conn, changed_files=set(), affected_names=set(),
        ) == 0
        conn.set_trace_callback(None)
        assert conn.total_changes == before
        assert not [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
    finally:
        conn.close()


def _parity_steps() -> list[tuple[str, dict[str, str | None]]]:
    """Each step maps rel path -> new text, or None to delete the file."""

    return [
        ("rename", {"native/util.cpp": "int renamed_fn(int x) { return x * 2; }\n"}),
        ("delete_unique_target", {"native/target.cpp": None}),
        ("introduce_ambiguity", {"native/solo2.cpp": "int solo(int y) { return y; }\n"}),
        ("remove_ambiguity", {"native/solo2.cpp": None}),
        ("include_disambiguation", {
            "native/user.cpp": '#include "lib_b.hpp"\nint user_fn() { return dup(5); }\n',
        }),
    ]


def _assert_step_semantics(label: str, edges: list[tuple[str, ...]]) -> None:
    # Guard against a vacuous parity: each step must move the edge it targets.
    if label == "rename":
        assert _dst(edges, "native/main.cpp", "rename_me") == ""
        assert _dst(edges, "native/other.cpp", "renamed_fn") != ""
    elif label == "delete_unique_target":
        assert _dst(edges, "native/main.cpp", "unique_target") == ""
    elif label == "introduce_ambiguity":
        assert _dst(edges, "native/main.cpp", "solo") == ""
    elif label == "remove_ambiguity":
        assert _dst(edges, "native/main.cpp", "solo") != ""


def test_incremental_build_parity_with_full_build(tmp_path, resolver_calls):
    repo = _seed(tmp_path)
    sg.build_index(repo, incremental=True)
    initial = _lexical_edges(repo)
    assert initial == _full_build_edges(tmp_path, repo, "initial")
    assert _dst(initial, "native/main.cpp", "unique_target") != ""
    assert _dst(initial, "native/main.cpp", "solo") != ""
    assert _dst(initial, "native/other.cpp", "renamed_fn") == ""
    user_dup_before = _dst(initial, "native/user.cpp", "dup")
    assert user_dup_before != ""

    for label, mutations in _parity_steps():
        for rel, text in mutations.items():
            if text is None:
                (repo / rel).unlink()
            else:
                _write(repo, rel, text)
        resolver_calls.clear()
        sg.build_index(repo, incremental=True)
        assert resolver_calls and all(
            call["changed_files"] is not None for call in resolver_calls
        ), label
        incremental_edges = _lexical_edges(repo)
        assert incremental_edges == _full_build_edges(tmp_path, repo, label), label
        _assert_step_semantics(label, incremental_edges)
    user_dup_after = _dst(incremental_edges, "native/user.cpp", "dup")
    assert user_dup_after not in {"", user_dup_before}


def test_single_file_index_parity_with_full_build(tmp_path, resolver_calls):
    repo = _seed(tmp_path)
    sg.build_index(repo, incremental=True)
    initial = _lexical_edges(repo)
    user_dup_before = _dst(initial, "native/user.cpp", "dup")

    # Deletion is expressed as an edit that drops the definition, since only
    # ``index_file`` re-runs lexical resolution on the single-file path.
    steps: list[tuple[str, str, str]] = [
        ("rename", "native/util.cpp", "int renamed_fn(int x) { return x * 2; }\n"),
        ("delete_unique_target", "native/target.cpp", "int gone_fn_nf02011;\n"),
        ("introduce_ambiguity", "native/solo2.cpp", "int solo(int y) { return y; }\n"),
        ("remove_ambiguity", "native/solo2.cpp", "int not_solo(int y) { return y; }\n"),
        ("include_disambiguation", "native/user.cpp",
         '#include "lib_b.hpp"\nint user_fn() { return dup(5); }\n'),
    ]
    for label, rel, text in steps:
        _write(repo, rel, text)
        resolver_calls.clear()
        expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        result = sg.index_file(repo, rel, expected_hash)
        assert result["ok"] is True, label
        assert len(resolver_calls) == 1
        assert resolver_calls[0]["changed_files"] == {rel}, label
        edges = _lexical_edges(repo)
        assert edges == _full_build_edges(tmp_path, repo, f"single_{label}"), label
        _assert_step_semantics(label, edges)
    user_dup_after = _dst(edges, "native/user.cpp", "dup")
    assert user_dup_after not in {"", user_dup_before}


def _seed_lsp_binding(repo: Path, caller: str, target: str, name: str) -> str:
    """Bind ``caller``'s call to ``name`` onto ``target`` through the LSP writers.

    Uses the production ``_lsp_bind_edge``/``_lsp_write_receipt`` so the rows
    have exactly the shape a language-server answer leaves behind.
    """

    spec = SimpleNamespace(command=("fake-lsp-nf02011",), version="1")
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        entity = conn.execute(
            "SELECT qualname, line_start FROM entities WHERE file_path=? AND name=?",
            (target, name),
        ).fetchone()
        edge = conn.execute(
            "SELECT id, line, max(source_col, 0) AS col FROM edges "
            "WHERE file_path=? AND dst_name=? AND kind='calls'",
            (caller, name),
        ).fetchone()
        # A server answers only edges lexical resolution left unresolved.
        conn.execute("UPDATE edges SET dst_qualname=NULL WHERE id=?", (int(edge["id"]),))
        binding = sg._LspBinding(
            source_path=caller,
            source_line=int(edge["line"]),
            source_column=int(edge["col"]),
            source_hash=sg._lsp_indexed_hash(conn, caller),
            target_path=target,
            target_hash=sg._lsp_indexed_hash(conn, target),
            target_qualname=str(entity["qualname"]),
            target_name=name,
            target_line_start=int(entity["line_start"]),
            target_range=(int(entity["line_start"]), 0),
            classification="verified",
            result_config_digest="digest",
        )
        assert sg._lsp_bind_edge(conn, binding, spec, "digest", 0) == 1
        sg._lsp_write_receipt(
            conn, caller, spec, "digest", 1, 0, binding.source_hash, True
        )
        conn.commit()
    finally:
        conn.close()
    return str(entity["qualname"])


@pytest.mark.parametrize("path", ["build", "single_file"])
def test_lsp_revoked_caller_in_unchanged_file_is_re_resolved(tmp_path, path):
    # Reviewer defect: a body-only edit of the target revokes the caller's LSP
    # binding (NULL) in an unchanged file; incremental scope must include it.
    repo = _new_repo(tmp_path, "repo")
    _write(repo, "native/caller.cpp", "int caller_fn() { return lsp_target(1); }\n")
    target_text = "int lsp_target(int x) { return x + 1; }\n"
    _write(repo, "native/def.cpp", target_text)
    sg.build_index(repo, incremental=True)
    target_qualname = _seed_lsp_binding(
        repo, "native/caller.cpp", "native/def.cpp", "lsp_target"
    )
    assert _dst(_lexical_edges(repo), "native/caller.cpp", "lsp_target") == target_qualname

    edited = "int lsp_target(int x) { return x + 2; }\n"
    _write(repo, "native/def.cpp", edited)
    if path == "build":
        sg.build_index(repo, incremental=True)
    else:
        result = sg.index_file(
            repo, "native/def.cpp", hashlib.sha256(edited.encode("utf-8")).hexdigest()
        )
        assert result["ok"] is True
    edges = _lexical_edges(repo)
    assert edges == _full_build_edges(tmp_path, repo, f"lsp_{path}")
    assert _dst(edges, "native/caller.cpp", "lsp_target") == target_qualname
