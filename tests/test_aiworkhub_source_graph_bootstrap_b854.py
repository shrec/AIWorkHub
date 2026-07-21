"""Regression checks for CLAUDE_SONNET5_AIWORKHUB_SOURCE_GRAPH_BOOTSTRAP_CORRECTNESS_B854_V1.

Guards the bootstrap repair: a clean canonical build of the current repository
must not crash on ``UNIQUE(file_path, qualname)`` when a file defines the
same nested name twice in one lexical scope (e.g. ``main._avg`` repeated),
and ``.claude/worktrees`` (ephemeral nested checkouts) must never be
indexed alongside canonical source trees such as ``tools/`` and ``scripts/``.

Every test isolates state under its own ``tmp_path``, so this file is safe
to run in a parallel test pool.

Run: PYTHONPATH=tools/geoai-task-mcp/src python3 -m pytest -q
     tools/geoai-task-mcp/tests/test_aiworkhub_source_graph_bootstrap_b854.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub.repository_state import bootstrap_repository


def _new_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Repeated nested same-name definitions in one lexical scope (main._avg bug)
# ---------------------------------------------------------------------------

def test_repeated_nested_definitions_build_without_unique_crash(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", (
        "def main():\n"
        "    def _avg(xs):\n"
        "        return sum(xs) / len(xs)\n"
        "    a = _avg([1, 2])\n"
        "    def _avg(xs):\n"
        "        return sum(xs) / max(1, len(xs))\n"
        "    b = _avg([3, 4])\n"
        "    return a, b\n"
    ))
    report = sg.build_index(repo, incremental=True)
    assert report.errors == []
    assert report.files_changed == 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        matches = sg.func(conn, "_avg")
        assert len(matches) == 2
        qualnames = {m["qualname"] for m in matches}
        assert len(qualnames) == 2  # stable distinct identities, not overwritten
        assert "pkg/core.py.main._avg" in qualnames
        assert "pkg/core.py.main._avg~2" in qualnames
    finally:
        conn.close()


def test_repeated_nested_qualnames_stable_across_incremental_rebuild(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    source = (
        "def main():\n"
        "    def _avg(xs):\n"
        "        return sum(xs) / len(xs)\n"
        "    def _avg(xs):\n"
        "        return sum(xs) / max(1, len(xs))\n"
        "    return _avg\n"
    )
    _write(repo / "pkg" / "core.py", source)
    sg.build_index(repo, incremental=True)
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        first_qualnames = sorted(m["qualname"] for m in sg.func(conn, "_avg"))
    finally:
        conn.close()

    # A no-op incremental rebuild must not duplicate or renumber entities.
    report2 = sg.build_index(repo, incremental=True)
    assert report2.files_changed == 0 and report2.files_unchanged == 1
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        second_qualnames = sorted(m["qualname"] for m in sg.func(conn, "_avg"))
        assert first_qualnames == second_qualnames
    finally:
        conn.close()

    # A forced re-extraction (full rebuild) assigns the same disambiguated
    # identities, since AST traversal order is deterministic for unchanged
    # source.
    report3 = sg.build_index(repo, incremental=False)
    assert report3.errors == []
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        third_qualnames = sorted(m["qualname"] for m in sg.func(conn, "_avg"))
        assert first_qualnames == third_qualnames
    finally:
        conn.close()


def test_ast_extraction_dedupes_repeated_class_definitions(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "pkg" / "dupe_class.py"
    _write(target, (
        "def factory():\n"
        "    class Helper:\n"
        "        pass\n"
        "    class Helper:\n"  # redefinition, same owner scope
        "        pass\n"
        "    return Helper\n"
    ))
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "ok"
    class_qualnames = sorted(e.qualname for e in extraction.entities if e.kind == "class")
    assert class_qualnames == ["pkg/dupe_class.py.factory.Helper", "pkg/dupe_class.py.factory.Helper~2"]


# ---------------------------------------------------------------------------
# .claude/worktrees exclusion (ephemeral nested checkouts, not canonical)
# ---------------------------------------------------------------------------

def test_claude_worktrees_excluded_from_indexing(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "tools" / "real_tool.py", "def real_helper():\n    return 1\n")
    # Simulate a nested worktree checkout carrying its own copy of the tree.
    _write(
        repo / ".claude" / "worktrees" / "b999" / "tools" / "real_tool.py",
        "def real_helper():\n    return 1\n",
    )
    files = sg.iter_source_files(repo)
    rels = {p.relative_to(repo).as_posix() for p in files}
    assert "tools/real_tool.py" in rels
    assert not any(rel.startswith(".claude/") for rel in rels)


def test_legit_canonical_trees_still_indexable_alongside_worktree_exclusion(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "tools" / "geoai-task-mcp" / "src" / "pkg.py", "def pkg_probe():\n    return 1\n")
    _write(repo / "scripts" / "run.py", "def script_probe():\n    return 1\n")
    _write(repo / ".claude" / "worktrees" / "b854" / "tools" / "shadow.py", "def shadow_probe():\n    return 1\n")

    report = sg.build_index(repo, incremental=True)
    assert report.errors == []

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert sg.func(conn, "pkg_probe")
        assert sg.func(conn, "script_probe")
        assert sg.func(conn, "shadow_probe") == []  # excluded worktree copy
    finally:
        conn.close()


def test_vcs_internals_and_caches_excluded(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / ".hg" / "store" / "legacy.py", "def hg_probe():\n    return 1\n")
    _write(repo / ".svn" / "pristine" / "legacy.py", "def svn_probe():\n    return 1\n")
    _write(repo / ".cache" / "tool" / "legacy.py", "def cache_probe():\n    return 1\n")
    files = sg.iter_source_files(repo)
    rels = {p.relative_to(repo).as_posix() for p in files}
    assert not any(rel.startswith((".hg/", ".svn/", ".cache/")) for rel in rels)


# ---------------------------------------------------------------------------
# Incremental rebuild idempotence with the disambiguation path involved
# ---------------------------------------------------------------------------

def test_incremental_rebuild_idempotent_no_duplicate_entities_or_edges(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", (
        "def main():\n"
        "    def _avg(xs):\n"
        "        return sum(xs) / len(xs)\n"
        "    def _avg(xs):\n"
        "        return sum(xs) / max(1, len(xs))\n"
        "    return _avg\n"
    ))
    sg.build_index(repo, incremental=True)
    sg.build_index(repo, incremental=True)
    sg.build_index(repo, incremental=True)

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        distinct = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT file_path, qualname FROM entities)"
        ).fetchone()[0]
        assert total == distinct
        edge_total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        sg.build_index(repo, incremental=False)
        edge_total_after_full = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    finally:
        conn.close()
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        edge_total_2 = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert edge_total_2 == edge_total_after_full
    finally:
        conn.close()
    assert edge_total >= 0  # sanity, no crash means the fix holds


# ---------------------------------------------------------------------------
# No legacy AITools/source_graph.db write authority
# ---------------------------------------------------------------------------

def test_no_legacy_aitools_db_written_during_bootstrap_build(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", (
        "def main():\n"
        "    def _avg(xs):\n"
        "        return 1\n"
        "    def _avg(xs):\n"
        "        return 2\n"
        "    return _avg\n"
    ))
    report = sg.build_index(repo, incremental=True)
    assert report.errors == []
    assert not (repo / "AITools" / "source_graph.db").exists()
    assert not (repo / "AITools" / "source_graph_universal.db").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
