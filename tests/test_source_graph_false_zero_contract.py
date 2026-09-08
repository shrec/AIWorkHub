"""NF-2026-00641: a bounded scan must never answer with a confident zero.

Two false-zero paths are pinned here.

1. Query normalization. ``find`` ran the all-tokens AND pass for every
   identifier-shaped query and, when that missed, only a raw ``LIKE`` substring
   fallback. A long camelCase/qualified focus query -- e.g. the six-token
   ``getCostLedgerInDashboardProvider`` -- normalizes into extra context tokens
   (``in``, ``dashboard``, ``provider``) that never co-occur in a single FTS
   row, so the AND returned zero while a shorter core of the same query
   (``getCostLedger``) still matched. Above a token threshold the OR broadening
   is now allowed, so a long focus query surfaces the same indexed matches as
   its short equivalent instead of a silent zero -- while a short, genuinely
   absent identifier still returns nothing.

2. bodygrep. A byte/file-capped partial scan must report its incompleteness
   (``scan_truncated`` / ``next_cursor``) so a zero count stays distinguishable
   from a genuinely-absent literal. Only an exhaustive scan may report an
   authoritative zero.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402

_TERM = "distinctivehaystackliteral"


def _find_graph() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(sg.SCHEMA)
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Query normalization: a long focus query must agree with its short core.
# ---------------------------------------------------------------------------

def test_long_identifier_query_matches_its_short_core() -> None:
    """The measured defect: six tokens came back zero, three tokens matched."""
    conn = _find_graph()
    try:
        short = sg.find(conn, "getCostLedger", limit=5)
        long_query = sg.find(conn, "getCostLedgerInDashboardProvider", limit=5)
    finally:
        conn.close()

    assert [row["name"] for row in short] == ["get_cost_ledger"]
    assert [row["name"] for row in long_query] == ["get_cost_ledger"]


def test_short_absent_identifier_does_not_fall_back_to_partial_or() -> None:
    """The broadening must not manufacture a match for a short exact miss."""
    conn = _find_graph()
    try:
        rows = sg.find(conn, "get_cost_missing", limit=5)
    finally:
        conn.close()

    assert rows == []


def test_focus_long_query_surfaces_the_indexed_match(tmp_path: Path) -> None:
    """End-to-end: ``focus`` reaches ``find`` and must not drop the symbol."""
    repo = tmp_path / "focusrepo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="focusrepo")
    _write(
        repo / "dashboard.py",
        "class DashboardProvider:\n    def get_cost_ledger(self):\n        return 1\n",
    )
    sg.build_index(repo, incremental=False)

    short = sg.focus(repo, "getCostLedger", budget=8)
    long_query = sg.focus(repo, "getCostLedgerInDashboardProvider", budget=8)

    short_names = [match["name"] for match in short["matches"]]
    long_names = [match["name"] for match in long_query["matches"]]
    # The short core is present in both; broadening may only add neighbours.
    assert "get_cost_ledger" in short_names
    assert "get_cost_ledger" in long_names


# ---------------------------------------------------------------------------
# bodygrep: only an exhaustive scan may report an authoritative zero.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def capped_repo(tmp_path_factory) -> Path:
    """A repository where the byte cap cannot cover the whole tree at budget 20."""
    repo = tmp_path_factory.mktemp("falsezero") / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="falsezero")
    # Sorts before src/ and exceeds the whole byte cap, so it is skipped.
    _write(repo / "data" / "inventory.jsonl", ('{"filler":"' + "x" * 512 + '"}\n') * 12000)
    for i in range(3):
        _write(repo / "src" / f"mod_{i}.py", f"def probe_{i}():\n    return {_TERM!r}\n")
    sg.build_index(repo, incremental=False)
    return repo


def test_capped_zero_is_explicitly_incomplete(capped_repo: Path) -> None:
    """Zero matches under a capped scan must carry the incompleteness flag."""
    result = sg.bodygrep_query(capped_repo, "nosuchliteralanywhere", budget=20)
    assert result["matches"] == []
    # The scan could not cover the whole tree, so the zero cannot be read as
    # full-repository proof of absence.
    assert result["scan_truncated"] is True


def test_exhaustive_zero_is_authoritative(tmp_path: Path) -> None:
    """Only a scan that finished may report a bare zero."""
    repo = tmp_path / "complete"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="complete")
    _write(repo / "a.py", "def a():\n    return 0\n")
    sg.build_index(repo, incremental=False)

    result = sg.bodygrep_query(repo, "nosuchliteralanywhere", budget=16)
    assert result["matches"] == []
    assert result["scan_truncated"] is False
    assert result["next_cursor"] is None
