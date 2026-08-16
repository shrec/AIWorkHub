"""Duplicate-qualname resilience for the canonical AIWorkHub Source Graph.

Regression coverage for NF-2026-00269-SOURCE-GRAPH-PHP. A live PHP repository
took the whole Source Graph down: two same-named class-like declarations inside
ONE file both built the identical ``(file_path, qualname)`` key, and the entity
insert aborted with ``UNIQUE constraint failed`` -- permanently, with no partial
index and no recovery, because the entire multi-file write ran in one
transaction.

The collision is between two entities INSIDE one file, never between two files
(the constraint is on the pair, so different directories can never collide).
These tests pin three guarantees:

  1. Extractor discipline -- a genuine duplicate PHP declaration indexes and
     both entities survive with distinct, stable qualnames.
  2. Blast radius -- one file that cannot be written is skipped and counted
     while every other file in the same run still indexes.
  3. Idempotency -- re-indexing unchanged duplicate source neither churns the
     qualnames nor aborts.

Each test isolates state under its own ``tmp_path`` bootstrapped repository.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _new_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _class_rows(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT file_path, qualname, line_start FROM entities "
        "WHERE kind='class' AND name=? ORDER BY line_start",
        (name,),
    ).fetchall()


def test_duplicate_php_class_declarations_both_survive_and_are_distinct(tmp_path):
    """Two same-named class-like declarations in one file index successfully;
    both entities exist, are distinguishable, and no IntegrityError escapes."""

    repo = _new_repo(tmp_path, "php_dup_class")
    _write(
        repo / "src" / "Widget.php",
        "<?php\n"
        "namespace App;\n"
        "class Widget { public function first() { return 1; } }\n"
        "class Widget { public function second() { return 2; } }\n",
    )

    # A duplicate declaration must not abort the build.
    report = sg.build_index(repo, incremental=True)
    assert report.errors == []

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        rows = _class_rows(conn, "Widget")
        # Both real declarations remain findable -- neither is silently dropped.
        assert len(rows) == 2
        qualnames = [row["qualname"] for row in rows]
        # Distinguishable: distinct qualnames on the same natural-key family.
        assert len(set(qualnames)) == 2
        # First occurrence keeps the plain namespaced name (backward compatible);
        # the second carries a stable ``~N`` suffix, never a hash/counter that
        # changes between runs.
        assert qualnames[0] == "App\\Widget"
        assert qualnames[1] == "App\\Widget~2"
        # Both methods survive under their owning (distinct) class.
        methods = {
            row["qualname"]
            for row in conn.execute(
                "SELECT qualname FROM entities WHERE kind='method' AND name IN ('first','second')"
            )
        }
        assert methods == {"App\\Widget::first", "App\\Widget~2::second"}
    finally:
        conn.close()


def test_duplicate_php_qualname_is_stable_across_real_reextraction(tmp_path):
    """The disambiguated qualname is derived identically every time the
    extractor runs, so re-indexing never churns the graph.

    An incremental re-index of unchanged source would be hash-skipped and never
    re-run the extractor, so it proves nothing about stability. This forces a
    real second extraction (``incremental=False`` routes every file back through
    the extractor -- see the ``incremental and ...`` gate in the build) and
    compares the two independently derived qualname sets."""

    repo = _new_repo(tmp_path, "php_dup_stable")
    _write(
        repo / "lib" / "Shape.php",
        "<?php\n"
        "interface Shape { }\n"
        "interface Shape { }\n",
    )

    first = sg.build_index(repo, incremental=True)
    assert first.errors == []
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        before = {row["qualname"] for row in _class_rows(conn, "Shape")}
    finally:
        conn.close()

    # Full rebuild: extraction actually runs a SECOND time over identical
    # content (not a hash-skip). Still no error, and the identities re-derive
    # byte-for-byte -- no counter/hash that drifts between runs.
    second = sg.build_index(repo, incremental=False)
    assert second.errors == []
    assert second.incremental is False
    # The file was genuinely re-extracted, not skipped as unchanged.
    assert second.files_changed >= 1
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        after = {row["qualname"] for row in _class_rows(conn, "Shape")}
    finally:
        conn.close()

    assert before == {"lib/Shape.php::Shape", "lib/Shape.php::Shape~2"}
    assert before == after


def test_duplicate_php_inheritance_edge_binds_the_primary_declaration(tmp_path):
    """When a file declares a class-like name twice, a same-file inheritance
    edge must resolve to the FIRST (primary) declaration, never the ``~N``
    duplicate. Fixing the crash must not quietly mis-wire the graph for exactly
    the files that triggered it."""

    repo = _new_repo(tmp_path, "php_dup_inherit")
    _write(
        repo / "src" / "Model.php",
        "<?php\n"
        "namespace App;\n"
        "class Base { }\n"
        "class Base { }\n"
        "class Derived extends Base { }\n",
    )

    report = sg.build_index(repo, incremental=True)
    assert report.errors == []

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        # Both declarations survive with distinct, ordered qualnames.
        assert [row["qualname"] for row in _class_rows(conn, "Base")] == [
            "App\\Base", "App\\Base~2",
        ]
        edge = conn.execute(
            "SELECT dst_qualname, evidence_label FROM edges "
            "WHERE kind='inherits' AND src_qualname='App\\Derived'"
        ).fetchone()
        assert edge is not None
        # First-wins: the bare name ``Base`` owns the primary declaration, so
        # the edge points at ``App\\Base`` and not the ``App\\Base~2`` duplicate.
        assert edge["dst_qualname"] == "App\\Base"
    finally:
        conn.close()


def test_one_unindexable_file_is_skipped_and_counted_others_still_index(
    tmp_path, monkeypatch,
):
    """One bad file among good ones is contained: it is skipped and counted while
    every other file in the same run still indexes. Assert the surviving rows,
    not merely the absence of an exception."""

    repo = _new_repo(tmp_path, "one_bad_among_good")
    _write(repo / "pkg" / "good_one.py", "def survivor_one():\n    return 1\n")
    _write(repo / "pkg" / "good_two.py", "def survivor_two():\n    return 2\n")
    _write(repo / "pkg" / "bad.py", "def doomed_symbol():\n    return 3\n")

    original_write = sg._write_extraction

    def flaky_write(conn, extraction, **kwargs):
        # Fail closed for exactly one file at write time, exercising per-file
        # containment. Every other extraction writes normally.
        if extraction.file_path.endswith("bad.py"):
            raise sqlite3.IntegrityError("synthetic per-file write failure")
        return original_write(conn, extraction, **kwargs)

    monkeypatch.setattr(sg, "_write_extraction", flaky_write)

    # The run completes; one bad row never takes the whole repository down.
    report = sg.build_index(repo, incremental=True)

    assert report.files_skipped == 1
    skipped = [e for e in report.errors if e.get("status") == "index_write_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["file"].endswith("bad.py")

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        # Every good file still indexed -- surviving rows, asserted directly.
        assert sg.find(conn, "survivor_one")
        assert sg.find(conn, "survivor_two")
        # The skipped file left no partial rows behind.
        assert not sg.find(conn, "doomed_symbol")
        assert conn.execute(
            "SELECT COUNT(*) FROM entities WHERE file_path LIKE '%bad.py'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_build_fails_loudly_when_every_write_is_contained(tmp_path, monkeypatch):
    """The containment floor. Per-file skips keep one bad file from taking a
    repository down -- they must NEVER let a build that indexed nothing report
    success. When every write is contained, the build raises instead of
    returning a clean report over an empty index, and the skip outcome is still
    durably recorded in the meta ``last_build`` row for after-the-fact triage.

    This pins the exact defect the previous round reintroduced: a broad per-file
    catch that swallowed a total write failure and returned ``files_changed==0,
    files_skipped==N`` as if healthy."""

    repo = _new_repo(tmp_path, "all_writes_contained")
    _write(repo / "pkg" / "one.py", "def one():\n    return 1\n")
    _write(repo / "pkg" / "two.py", "def two():\n    return 2\n")

    def always_fail(conn, extraction, **kwargs):
        # Forward-signature-compatible stub: the write contract is still
        # ``(conn, extraction, *, file_size, mtime_ns)``, so this fails on real
        # write intent, not on an unexpected keyword.
        raise sqlite3.IntegrityError("synthetic total write failure")

    monkeypatch.setattr(sg, "_write_extraction", always_fail)

    # Every write is contained -> nothing indexed -> the build FAILS, loudly.
    with pytest.raises(sg.SourceGraphBuildFailedError):
        sg.build_index(repo, incremental=True)

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        last_build = json.loads(
            conn.execute("SELECT value FROM meta WHERE key='last_build'").fetchone()[0]
        )
        # The degradation is durable and diagnosable, never a clean-looking row.
        assert last_build["files_changed"] == 0
        assert last_build["files_skipped"] == 2
        assert len(last_build["skipped_files"]) == 2
        # No success costume: the empty index is empty, not silently blessed.
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    finally:
        conn.close()
