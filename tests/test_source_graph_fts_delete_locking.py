"""Regression coverage for the chunked/set-based FTS5 delete (NF-2026-00635).

Covers: exact removal of requested entity_ids spanning more than one delete
chunk with unrelated rows preserved, empty input as a no-op, a statement-count
and wall-time proof that the previous per-entity ``executemany`` (one full
``entities_fts`` virtual-table scan per deleted entity) is gone, concurrent
read-only retrieval surviving a multi-chunk incremental delete+republish, and
that the read-only retrieval connection and its busy-timeout stay unchanged.

Run: python3 -m pytest -q tests/test_source_graph_fts_delete_locking.py
"""

from __future__ import annotations

import math
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _new_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _seed_synthetic_file(conn: sqlite3.Connection, file_path: str, count: int) -> list[int]:
    """Insert ``count`` bare entity + entities_fts rows for ``file_path``.

    Bypasses real AST extraction so the fixture is exact, fast and
    deterministic: these tests exercise ``_invalidate_file`` deletion
    semantics, not parsing.
    """
    conn.execute(
        "INSERT INTO files(file_path, language, status, source_hash, indexed_at, build_revision) "
        "VALUES (?,?,?,?,?,?)",
        (file_path, "python", "ok", "0" * 64, sg._now_iso(), sg.BUILD_REVISION),
    )
    ids: list[int] = []
    for i in range(count):
        name = f"sym_{i}"
        qualname = f"{file_path}::{name}"
        cur = conn.execute(
            "INSERT INTO entities(file_path, kind, name, qualname, line_start, line_end, "
            "signature, evidence_label, extractor, confidence, source_hash, build_revision) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_path, "function", name, qualname, i + 1, i + 1, "", "EXTRACTED",
             "test", 1.0, "0" * 64, sg.BUILD_REVISION),
        )
        entity_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO entities_fts(entity_id, name, qualname, signature, file_path) "
            "VALUES (?,?,?,?,?)",
            (entity_id, name, qualname, "", file_path),
        )
        ids.append(entity_id)
    return ids


def _fts_delete_statements(statements: list[str]) -> list[str]:
    return [
        s for s in statements
        if "entities_fts" in s and s.strip().upper().startswith("DELETE")
    ]


# ---------------------------------------------------------------------------
# 1. Multi-chunk exactness + unrelated-row preservation + empty no-op
# ---------------------------------------------------------------------------

def test_invalidate_file_removes_exact_ids_across_multiple_chunks(tmp_path, monkeypatch):
    root = _new_repo(tmp_path, "multi_chunk")
    monkeypatch.setattr(sg, "_FTS_DELETE_CHUNK_SIZE", 50)
    db_path = sg.resolve_db_path(root)
    conn = sg.connect(db_path)
    try:
        target_ids = _seed_synthetic_file(conn, "target.py", 120)
        keep_ids = _seed_synthetic_file(conn, "keep.py", 30)
        conn.commit()

        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        sg._invalidate_file(conn, "target.py")
        conn.set_trace_callback(None)
        conn.commit()

        fts_deletes = _fts_delete_statements(statements)
        assert len(fts_deletes) == math.ceil(120 / 50) == 3

        target_rows = conn.execute(
            "SELECT COUNT(*) FROM entities_fts WHERE entity_id IN ({})".format(
                ",".join("?" * len(target_ids))
            ),
            target_ids,
        ).fetchone()[0]
        assert target_rows == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM entities WHERE file_path='target.py'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_path='target.py'"
        ).fetchone()[0] == 0

        keep_rows = conn.execute(
            "SELECT COUNT(*) FROM entities_fts WHERE entity_id IN ({})".format(
                ",".join("?" * len(keep_ids))
            ),
            keep_ids,
        ).fetchone()[0]
        assert keep_rows == len(keep_ids)
        assert conn.execute(
            "SELECT COUNT(*) FROM entities WHERE file_path='keep.py'"
        ).fetchone()[0] == len(keep_ids)
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_path='keep.py'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_invalidate_file_with_no_entities_executes_no_fts_delete(tmp_path):
    root = _new_repo(tmp_path, "empty_noop")
    db_path = sg.resolve_db_path(root)
    conn = sg.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO files(file_path, language, status, source_hash, indexed_at, build_revision) "
            "VALUES (?,?,?,?,?,?)",
            ("empty.py", "python", "ok", "0" * 64, sg._now_iso(), sg.BUILD_REVISION),
        )
        conn.commit()

        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        sg._invalidate_file(conn, "empty.py")
        conn.set_trace_callback(None)
        conn.commit()

        assert _fts_delete_statements(statements) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_path='empty.py'"
        ).fetchone()[0] == 0

        # A file that was never indexed at all is the same no-op shape.
        statements.clear()
        conn.set_trace_callback(statements.append)
        sg._invalidate_file(conn, "never_indexed.py")
        conn.set_trace_callback(None)
        assert _fts_delete_statements(statements) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Statement-count and wall-time proof: no full scan per deleted entity
# ---------------------------------------------------------------------------

def test_chunked_delete_beats_legacy_per_entity_delete_on_statements_and_time(
    tmp_path, monkeypatch
):
    root = _new_repo(tmp_path, "perf_proof")
    source_conn = sg.connect(sg.resolve_db_path(root))
    try:
        entity_ids = _seed_synthetic_file(source_conn, "haystack.py", 3000)
        keep_ids = _seed_synthetic_file(source_conn, "keep.py", 5)
        source_conn.commit()

        def legacy_delete(conn, ids):
            conn.executemany(
                "DELETE FROM entities_fts WHERE entity_id=?",
                [(entity_id,) for entity_id in ids],
            )

        def run_case(method, label, *, trace=False):
            # Every arm starts from the same committed SQLite backup and uses
            # the same production connection settings and invalidation path.
            case_conn = sg.connect(tmp_path / f"{method}_{label}.sqlite")
            try:
                source_conn.backup(case_conn)
                statements: list[str] = []
                if trace:
                    case_conn.set_trace_callback(statements.append)
                with monkeypatch.context() as patcher:
                    if method == "legacy":
                        patcher.setattr(sg, "_delete_entities_fts_rows", legacy_delete)
                    if not trace:
                        started = time.perf_counter()
                    sg._invalidate_file(case_conn, "haystack.py")
                    case_conn.commit()
                    elapsed = None if trace else time.perf_counter() - started
                case_conn.set_trace_callback(None)

                # Check both the deleted target and unrelated rows after commit;
                # the MATCH query also verifies the surviving FTS index is usable.
                state = (
                    case_conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                    case_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                    case_conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0],
                    case_conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                    case_conn.execute(
                        "SELECT COUNT(*) FROM entities_fts WHERE file_path='haystack.py'"
                    ).fetchone()[0],
                    case_conn.execute(
                        "SELECT COUNT(*) FROM entities_fts WHERE entities_fts MATCH 'sym_0'"
                    ).fetchone()[0],
                )
                assert state == (1, len(keep_ids), len(keep_ids), 0, 0, 1)
                assert case_conn.execute(
                    "SELECT file_path FROM files"
                ).fetchone()[0] == "keep.py"
                return elapsed, _fts_delete_statements(statements)
            finally:
                case_conn.close()

        # Trace callbacks distort timing, so count SQL on separate untimed clones.
        _, legacy_deletes = run_case("legacy", "statements", trace=True)
        _, chunked_deletes = run_case("chunked", "statements", trace=True)
        assert len(legacy_deletes) == len(entity_ids) == 3000
        assert len(chunked_deletes) == math.ceil(
            len(entity_ids) / sg._FTS_DELETE_CHUNK_SIZE
        ) == 6
        assert len(chunked_deletes) < len(legacy_deletes)

        elapsed_by_method = {"legacy": [], "chunked": []}
        for trial in range(8):  # One warmup pair, then seven alternating pairs.
            order = ("legacy", "chunked") if trial % 2 == 0 else ("chunked", "legacy")
            for method in order:
                elapsed, _ = run_case(method, f"timed_{trial}")
                if trial:
                    elapsed_by_method[method].append(elapsed)
        paired_advantages = [
            legacy - chunked
            for legacy, chunked in zip(
                elapsed_by_method["legacy"], elapsed_by_method["chunked"]
            )
        ]
        assert statistics.median(paired_advantages) > 0, (
            f"chunked delete did not beat per-entity delete across paired runs: "
            f"{elapsed_by_method}"
        )
    finally:
        source_conn.close()


# ---------------------------------------------------------------------------
# 3. Concurrent read-only retrieval survives a multi-chunk delete+republish
# ---------------------------------------------------------------------------

def test_concurrent_reads_survive_multi_chunk_incremental_rebuild(tmp_path, monkeypatch):
    root = _new_repo(tmp_path, "concurrent_rebuild")
    (root / "app.py").write_text(
        "def committed():\n    return 'searchable marker'\n", encoding="utf-8"
    )
    sg.build_index(root, incremental=False)
    canonical = sg.resolve_db_path(root)

    # A small chunk size forces the next build's invalidation of app.py's
    # entities to span more than one delete chunk without needing hundreds
    # of real source functions.
    monkeypatch.setattr(sg, "_FTS_DELETE_CHUNK_SIZE", 3)
    many_functions = "\n".join(
        f"def gen_{i}():\n    return 'searchable marker'\n" for i in range(12)
    )
    (root / "app.py").write_text(many_functions, encoding="utf-8")

    writer_open = threading.Event()
    release_writer = threading.Event()
    original_connect = sg.connect

    def gated_connect(path, *, read_only=False):
        conn = original_connect(path, read_only=read_only)
        if not read_only and Path(path) != canonical and not writer_open.is_set():
            writer_open.set()
            assert release_writer.wait(10)
        return conn

    monkeypatch.setattr(sg, "connect", gated_connect)
    errors: list[BaseException] = []

    def refresh() -> None:
        try:
            sg.build_index(root)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=refresh)
    thread.start()
    assert writer_open.wait(10)
    for _ in range(20):
        focused = sg.focus(root, "committed", budget=8)
        assert focused["matches"]
    release_writer.set()
    thread.join(10)
    assert not thread.is_alive()
    assert errors == []

    assert sg.focus(root, "gen_0", budget=8)["matches"]
    assert sg.focus(root, "gen_11", budget=8)["matches"]


# ---------------------------------------------------------------------------
# 4. Read-only retrieval connection and its busy-timeout stay unchanged
# ---------------------------------------------------------------------------

def test_readonly_retrieval_connection_creates_no_sidecars_and_stays_readonly(tmp_path):
    root = _new_repo(tmp_path, "readonly_no_sidecars")
    (root / "app.py").write_text(
        "def marker():\n    return 'searchable marker'\n", encoding="utf-8"
    )
    sg.build_index(root, incremental=False)
    canonical = sg.resolve_db_path(root)

    conn = sg.connect(canonical, read_only=True)
    try:
        assert sg.focus(root, "marker", budget=8)["matches"]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM entities")
    finally:
        conn.close()

    sidecars = sorted(
        p.name for p in canonical.parent.iterdir()
        if p.name != canonical.name and p.name.startswith(canonical.name)
    )
    assert sidecars == []

    verify_conn = sg.connect(canonical, read_only=True)
    try:
        journal_mode = str(
            verify_conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        assert journal_mode == "delete"
    finally:
        verify_conn.close()


def test_writer_and_reader_connections_have_bounded_busy_timeout(tmp_path):
    root = _new_repo(tmp_path, "busy_timeout_bounded")
    sg.build_index(root, incremental=False)
    canonical = sg.resolve_db_path(root)

    writer = sg.connect(canonical)
    try:
        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        writer.close()

    reader = sg.connect(canonical, read_only=True)
    try:
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        reader.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
