from __future__ import annotations

import sqlite3

from aiworkhub import source_graph


def _graph() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(source_graph.SCHEMA)
    cursor = conn.execute(
        "INSERT INTO entities("
        "file_path, kind, name, qualname, line_start, line_end, signature, "
        "evidence_label, extractor, confidence, source_hash, build_revision"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "src/aiworkhub/dashboard.py",
            "method",
            "get_cost_ledger",
            "src/aiworkhub/dashboard.py.DashboardProvider.get_cost_ledger",
            1268,
            1277,
            "def get_cost_ledger(self)",
            "EXTRACTED",
            "python_ast.v1",
            1.0,
            "hash",
            "revision",
        ),
    )
    entity_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO entities_fts(name, qualname, signature, file_path, entity_id) "
        "VALUES(?,?,?,?,?)",
        (
            "get_cost_ledger",
            "src/aiworkhub/dashboard.py.DashboardProvider.get_cost_ledger",
            "def get_cost_ledger(self)",
            "src/aiworkhub/dashboard.py",
            entity_id,
        ),
    )
    return conn


def test_query_tokens_normalize_identifier_and_qualified_name() -> None:
    assert source_graph._query_tokens(
        "AIWorkHub::DashboardProvider.getCostLedger"
    ) == ["ai", "work", "hub", "dashboard", "provider", "get", "cost", "ledger"]


def test_query_tokens_preserve_georgian_natural_language() -> None:
    assert source_graph._query_tokens("ხარჯის ზუსტი გაზომვა") == [
        "ხარჯის", "ზუსტი", "გაზომვა",
    ]


def test_find_uses_normalized_identifier_tokens_after_exact_pass() -> None:
    conn = _graph()
    try:
        rows = source_graph.find(conn, "getCostLedger", limit=5)
    finally:
        conn.close()

    assert [row["name"] for row in rows] == ["get_cost_ledger"]


def test_identifier_normalization_does_not_fall_back_to_partial_or_matches() -> None:
    conn = _graph()
    try:
        rows = source_graph.find(conn, "get_cost_missing", limit=5)
    finally:
        conn.close()

    assert rows == []
