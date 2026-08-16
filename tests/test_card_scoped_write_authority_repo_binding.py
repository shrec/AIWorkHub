"""Card-scoped write authority binds to the launching repository, and core.py
builds no read-only SQLite URI by raw f-string.

Two guarantees are asserted:

1. ``core.repo_root()`` resolves from the launching repository (the canonical
   ``AIWORKHUB_REPO_ROOT`` binding), so two repositories stay isolated and a
   legacy ambient ``AIWORKHUB_REPO`` can never silently override it.
2. ``core._canonical_connect(readonly=True)`` routes through
   ``sqlite_readonly.connect_readonly`` rather than a raw
   ``file:{path}?mode=ro`` f-string -- proven with a repository path containing
   ``#``, the exact case a raw f-string mishandles (``#`` starts a URI fragment
   that swallows ``?mode=ro`` and opens a DIFFERENT file read-write).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aiworkhub import core, sqlite_readonly


# --- 1. Write authority resolves from the launching repository ---------------


def test_repo_root_binds_to_launching_repository(monkeypatch, tmp_path) -> None:
    launching = tmp_path / "launching_repo"
    launching.mkdir()
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(launching))
    assert core.repo_root() == launching.resolve()


def test_two_repositories_stay_isolated(monkeypatch, tmp_path) -> None:
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    repo_b = tmp_path / "b"
    repo_b.mkdir()
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo_a))
    root_a = core.repo_root()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo_b))
    root_b = core.repo_root()

    assert root_a == repo_a.resolve()
    assert root_b == repo_b.resolve()
    assert root_a != root_b


def test_launching_repo_outranks_ambient_legacy_binding(monkeypatch, tmp_path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(canonical))
    monkeypatch.setenv("AIWORKHUB_REPO", str(ambient))
    # A legacy ambient binding that disagrees with the canonical launching repo
    # is a hard error, never a silent override.
    with pytest.raises(RuntimeError):
        core.repo_root()


# --- 2. core.py builds no read-only SQLite URI by f-string --------------------


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, tag TEXT)")
        conn.execute("INSERT INTO marker (tag) VALUES ('canonical')")
        conn.commit()
    finally:
        conn.close()


def test_canonical_readonly_connect_is_fragment_safe(monkeypatch, tmp_path) -> None:
    # A repository directory containing '#' is exactly what a raw
    # ``file:{path}?mode=ro`` f-string mishandles.
    repo = tmp_path / "repo#1"
    repo.mkdir()
    db_path = repo / "task_queue.sqlite"
    _make_db(db_path)
    monkeypatch.setattr(core, "_canonical_db_path", lambda: db_path)

    conn = core._canonical_connect(readonly=True)
    try:
        # It opened the intended file (not a fresh empty database).
        rows = conn.execute("SELECT tag FROM marker").fetchall()
        assert [row[0] for row in rows] == ["canonical"]
        # And it is genuinely read-only.
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO marker (tag) VALUES ('mutation')")
    finally:
        conn.close()
    # No fragment-truncated sibling file was silently created.
    assert not (repo / "task_queue.sqlite?mode=ro").exists()


def test_sqlite_readonly_helper_rejects_writes(tmp_path) -> None:
    repo = tmp_path / "has#hash"
    repo.mkdir()
    db_path = repo / "db.sqlite"
    _make_db(db_path)
    conn = sqlite_readonly.connect_readonly(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO marker (tag) VALUES ('x')")
    finally:
        conn.close()
