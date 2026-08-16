"""Boundary tests for the shared read-only SQLite connection helper.

``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` is *not* a read-only
open: in SQLite URI syntax ``#`` begins a fragment, so a path containing
``#`` swallows ``?mode=ro``, opens READ-WRITE with create-if-missing, and
targets a different file than the caller named. These tests pin the fix: one
shared helper that percent-encodes the path AND issues ``PRAGMA query_only``,
plus a regression ledger that makes every helper user (and any future raw
f-string site) visible.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub.sqlite_readonly import connect_readonly  # noqa: E402
from aiworkhub import task_store  # noqa: E402

# In-scope modules whose read-only opens must all route through the helper.
SCOPE_FILES = (
    "aiworkhub/task_store.py",
    "aiworkhub/task_retention.py",
    "aiworkhub/source_graph.py",
    "aiworkhub/context_importer.py",
    "aiworkhub/context_write_intents.py",
    "aiworkhub/worker_ai_tools_mcp.py",
)

# Static regression ledger: every read-only open that must route through the
# shared helper, with its before/after timeout.
EXPECTED_USERS = (
    ("aiworkhub/task_store.py", "connect_readonly(path, timeout=5.0)"),
    ("aiworkhub/task_store.py", "connect_readonly(source)"),
    ("aiworkhub/task_store.py", "connect_readonly(canonical)"),
    ("aiworkhub/task_retention.py", "connect_readonly(path)"),
    ("aiworkhub/source_graph.py", "connect_readonly(db_path, timeout=30.0)"),
    ("aiworkhub/context_importer.py", "connect_readonly(path, timeout=5)"),
    ("aiworkhub/context_write_intents.py", "connect_readonly(path, timeout=5)"),
    (
        "aiworkhub/worker_ai_tools_mcp.py",
        "connect_readonly(path, timeout=SQLITE_QUERY_TIMEOUT_SECONDS)",
    ),
)


def _hash_path(tmp_path: Path) -> Path:
    return tmp_path / "db#frag.db"


def _seed(path: Path, values: tuple[str, ...] = ()) -> None:
    """Create a real SQLite database at ``path`` (via a plain, non-URI open)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE seed(v INTEGER)")
        for value in values:
            conn.execute("INSERT INTO seed(v) VALUES (?)", (int(value),))
        conn.commit()
    finally:
        conn.close()


def test_query_only_flag_is_on(tmp_path: Path) -> None:
    path = tmp_path / "plain.db"
    _seed(path)
    conn = connect_readonly(path)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()


def test_normal_path_write_refused(tmp_path: Path) -> None:
    path = tmp_path / "plain.db"
    _seed(path)
    conn = connect_readonly(path)
    try:
        with pytest.raises(sqlite3.DatabaseError) as exc_info:
            conn.execute("CREATE TABLE injected(x INTEGER)")
        assert "readonly" in str(exc_info.value).lower()
    finally:
        conn.close()


def test_hash_path_refuses_write_and_creates_no_stray_file(tmp_path: Path) -> None:
    db_path = _hash_path(tmp_path)
    _seed(db_path, ("1",))
    conn = connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError) as exc_info:
            conn.execute("CREATE TABLE injected(x INTEGER)")
        assert "readonly" in str(exc_info.value).lower()
    finally:
        conn.close()
    # Filesystem truth, not only the raised exception: the literal '#' path
    # still exists and no stripped 'db' file was materialised.
    assert db_path.exists()
    assert not (tmp_path / "db").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["db#frag.db"]


def test_hash_path_missing_never_creates_file(tmp_path: Path) -> None:
    db_path = _hash_path(tmp_path)
    with pytest.raises(sqlite3.Error):
        connect_readonly(db_path)
    assert not db_path.exists()
    assert not (tmp_path / "db").exists()


def test_hash_path_query_survives_uri_encoding(tmp_path: Path) -> None:
    db_path = _hash_path(tmp_path)
    _seed(db_path, ("7",))
    conn = connect_readonly(db_path)
    try:
        assert conn.execute("SELECT v FROM seed").fetchone()[0] == 7
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()
    assert not (tmp_path / "db").exists()


def test_no_raw_mode_ro_fstring_remains_in_scope_files() -> None:
    for rel in SCOPE_FILES:
        text = (_SRC / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            # A raw f-string open is ``f"file:{...}?mode=ro"``: the URI has a
            # literal ``file:{`` prefix and no ``as_uri()`` percent-encoding.
            # Prose comments that merely mention ``mode=ro`` are not flagged.
            if "file:{" in line and "?mode=ro" in line and "as_uri()" not in line:
                pytest.fail(
                    f"{rel}:{lineno}: raw read-only URI without as_uri(): {line.strip()}"
                )


def test_helper_users_enumerated() -> None:
    found: list[tuple[str, str]] = []
    for rel in SCOPE_FILES:
        text = (_SRC / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if "= connect_readonly(" in stripped:
                found.append((rel, stripped))
    for rel, snippet in EXPECTED_USERS:
        assert any(r == rel and snippet in s for r, s in found), f"missing {rel}: {snippet}"
    assert len(found) == len(EXPECTED_USERS), f"unexpected read-only users: {found}"


def test_timeout_preserved_per_site() -> None:
    # before == after: the helper forwards the exact existing timeout argument.
    report = (
        ("aiworkhub/task_store.py", "connect_readonly(path, timeout=5.0)", "5.0", "5.0"),
        ("aiworkhub/task_store.py", "connect_readonly(source)", "5.0 (default)", "5.0 (default)"),
        ("aiworkhub/task_store.py", "connect_readonly(canonical)", "5.0 (default)", "5.0 (default)"),
        ("aiworkhub/task_retention.py", "connect_readonly(path)", "5.0 (default)", "5.0 (default)"),
        ("aiworkhub/source_graph.py", "connect_readonly(db_path, timeout=30.0)", "30.0", "30.0"),
        ("aiworkhub/context_importer.py", "connect_readonly(path, timeout=5)", "5", "5"),
        ("aiworkhub/context_write_intents.py", "connect_readonly(path, timeout=5)", "5", "5"),
        (
            "aiworkhub/worker_ai_tools_mcp.py",
            "connect_readonly(path, timeout=SQLITE_QUERY_TIMEOUT_SECONDS)",
            "5 (constant)",
            "5 (constant)",
        ),
    )
    for rel, snippet, _before, _after in report:
        text = (_SRC / rel).read_text(encoding="utf-8")
        assert snippet in text, f"{rel}: timeout not preserved; expected {snippet!r}"
    # The helper never mutates row_factory; each call site keeps its own.
    helper = (_SRC / "aiworkhub/sqlite_readonly.py").read_text(encoding="utf-8")
    assert "row_factory" not in helper


def test_hash_path_refused_for_each_direct_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiworkhub import (
        context_importer,
        context_write_intents,
        source_graph,
        task_retention,
        worker_ai_tools_mcp,
    )

    monkeypatch.setattr(
        task_retention.task_store, "canonical_db_path", lambda root: _hash_path(tmp_path)
    )
    monkeypatch.setattr(
        context_write_intents.storage_registry, "load_storage_registry", lambda repo: {}
    )
    monkeypatch.setattr(
        context_write_intents.storage_registry,
        "resolve_database_path",
        lambda registry, db_id: _hash_path(tmp_path),
    )

    users = (
        ("task_store._connect", lambda: task_store._connect(_hash_path(tmp_path), readonly=True)),
        (
            "task_store._sqlite_backup",
            lambda: task_store._sqlite_backup(_hash_path(tmp_path), tmp_path / "out.db"),
        ),
        ("task_retention._connect", lambda: task_retention._connect(tmp_path, readonly=True)),
        (
            "source_graph.connect",
            lambda: source_graph.connect(_hash_path(tmp_path), read_only=True),
        ),
        (
            "context_importer._open_source",
            lambda: context_importer._open_source(_hash_path(tmp_path)),
        ),
        (
            "context_write_intents._decision_connection",
            lambda: context_write_intents._decision_connection(Path("unused"), writable=False),
        ),
        (
            "worker_ai_tools_mcp._open_readonly_db",
            lambda: worker_ai_tools_mcp._open_readonly_db(_hash_path(tmp_path), tool="test"),
        ),
    )

    for name, open_fn in users:
        assert not _hash_path(tmp_path).exists(), f"{name}: pre-existing '#' file"
        assert not (tmp_path / "db").exists(), f"{name}: pre-existing 'db' file"
        with pytest.raises((sqlite3.Error, RuntimeError)):
            open_fn()
        assert not _hash_path(tmp_path).exists(), f"{name}: materialised the '#' file"
        assert not (tmp_path / "db").exists(), f"{name}: materialised a stripped 'db' file"


def test_reconcile_auxiliary_hash_path_creates_no_stray_file(tmp_path: Path) -> None:
    """The quick-check open in ``_reconcile_auxiliary_databases`` (task_store).

    That site opens an *existing* canonical DB (not a missing one), so its
    failure mode is the swallowed-fragment one: a raw f-string would open a
    stripped ``db`` file READ-WRITE. Assert the literal '#' path is the only
    file, with no stray ``db`` materialised.
    """
    canonical = tmp_path / ".aiworkhub" / "aux" / "db#frag.db"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    _seed(canonical)
    registry = tmp_path / "storage.json"
    registry.write_text(
        json.dumps(
            {"databases": [{"id": "source_graph", "canonical_durable_path": "aux/db#frag.db"}]}
        ),
        encoding="utf-8",
    )
    repo = SimpleNamespace(root=tmp_path)
    task_store._reconcile_auxiliary_databases(repo, registry)
    assert canonical.exists()
    assert not (canonical.parent / "db").exists()
