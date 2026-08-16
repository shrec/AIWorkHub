"""Read-only contract tests for ``cli_adapter_readonly_tool`` (B107 recovery).

Covers the pieces the recovered collision preflight relies on that the packaged
regression file does not: the fail-closed read-only SQLite open, malformed-row
tolerance that skips-and-counts instead of disabling the whole diagnostic, and
the path-free ``unresolved_reason`` on the canonical-store branch.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import cli_adapter_readonly_tool as tool  # noqa: E402
from aiworkhub import core  # noqa: E402
from aiworkhub import sqlite_readonly  # noqa: E402
from aiworkhub import task_store  # noqa: E402

_CARDS_ENV = "BITNN_TASK_CARDS_PATH"
_DEDICATED_ENV = "AIWORKHUB_COLLISION_CARDS_PATH"


@pytest.fixture(autouse=True)
def _clear_card_env(monkeypatch) -> None:
    monkeypatch.delenv(_CARDS_ENV, raising=False)
    monkeypatch.delenv(_DEDICATED_ENV, raising=False)


def test_readonly_open_of_hash_path_creates_no_file() -> None:
    """A read-only open of a path containing ``#`` must create NO file.

    Built by raw f-string URI (``file:{path}?mode=ro``) the ``#`` begins a
    fragment and swallows ``?mode=ro``; the DB would then open read-write with
    create-if-missing at a DIFFERENT, fragment-truncated path. The hardened
    ``connect_readonly`` percent-encodes the path via ``Path.as_uri`` so
    ``?mode=ro`` survives, and a missing file cannot be created -- asserted on
    the filesystem.
    """
    tmp = Path(tempfile.mkdtemp(prefix="aiworkhub_ro_hash_"))
    try:
        db_path = tmp / "store#1.sqlite3"
        assert not db_path.exists()
        with pytest.raises(sqlite3.OperationalError):
            sqlite_readonly.connect_readonly(db_path)
        # Neither the hash path nor the buggy fragment-truncated sibling exists.
        assert not db_path.exists()
        assert not (tmp / "store").exists()
        assert list(tmp.iterdir()) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_readonly_open_refuses_writes(tmp_path) -> None:
    """``PRAGMA query_only=ON`` refuses a write even on an existing DB."""
    db = tmp_path / "real.sqlite3"
    writer = sqlite3.connect(db)
    writer.execute("CREATE TABLE t(x INTEGER)")
    writer.commit()
    writer.close()

    conn = sqlite_readonly.connect_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")
    finally:
        conn.close()


def test_preflight_skips_and_counts_malformed_rows(tmp_path, monkeypatch) -> None:
    """One malformed row does not disable the whole diagnostic; it is skipped
    AND counted, and the valid active cards are still classified."""
    cards = tmp_path / "cards.jsonl"
    cards.write_text(
        "\n".join(
            [
                json.dumps({"task_id": "T1", "runner": "r", "status": "processing"}),
                "{ broken json",                   # malformed -> skipped + counted
                json.dumps(["not", "a", "dict"]),  # non-object -> skipped + counted
                "   ",                              # blank -> skipped, not counted
                json.dumps({"task_id": "T2", "runner": "r", "status": "review"}),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_CARDS_ENV, str(cards))
    result = tool.collision_preflight(task_id="T9", runner="r2", topic="x")
    assert result["cards_source_resolved"] is True
    assert result["active_card_count"] == 2
    assert result["skipped_malformed_rows"] == 2


def _assert_unresolved_no_path_leak(result: dict, leaked: Path) -> None:
    """The WHOLE unresolved structure must be path-free: neither the reason NOR
    the ``cards_source`` beside it may carry the operator's absolute path (or its
    basename). Redacting the reason while printing the path next to it is not
    redaction."""
    reason = result["unresolved_reason"]
    source = result["cards_source"]
    assert reason and source
    for field in (reason, source):
        assert str(leaked) not in field
        assert leaked.name not in field
        assert "/" not in field and "\\" not in field


def test_preflight_explicit_directory_source_is_unresolved(tmp_path, monkeypatch) -> None:
    """A configured source that EXISTS but is a DIRECTORY (the misconfiguration a
    reviewer reproduced) is UNRESOLVED, never a confident zero. 'Missing' and
    'unreadable' are the same epistemic state: no cards were observed."""
    a_dir = tmp_path / "cards_where_a_file_was_expected"
    a_dir.mkdir()
    monkeypatch.setenv(_CARDS_ENV, str(a_dir))
    result = tool.collision_preflight(task_id="T1", runner="r", topic="x")
    assert result["cards_source_resolved"] is False
    assert result["active_card_count"] is None
    assert result["would_collide"] is None
    assert result["read_only"] is True and result["mutated_state"] is False
    _assert_unresolved_no_path_leak(result, a_dir)


def test_preflight_explicit_undecodable_source_is_unresolved(tmp_path, monkeypatch) -> None:
    """A configured source whose bytes are not valid UTF-8 raises a
    UnicodeDecodeError (NOT an OSError); it too is UNRESOLVED, not a zero."""
    bad = tmp_path / "cards.jsonl"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \x00\x01")
    monkeypatch.setenv(_DEDICATED_ENV, str(bad))
    result = tool.collision_preflight(task_id="T1", runner="r", topic="x")
    assert result["cards_source_resolved"] is False
    assert result["active_card_count"] is None
    assert result["would_collide"] is None
    _assert_unresolved_no_path_leak(result, bad)


def test_preflight_configured_source_read_raising_permissionerror_is_unresolved(
    tmp_path, monkeypatch
) -> None:
    """When the READ of a configured source raises ``PermissionError`` the result
    is the UNRESOLVED shape -- proven by FORCING the raise, not by chmod-ing a
    real file.

    A mode-000 filesystem test is not portable: under an effective user that
    bypasses mode bits (root, or any process holding ``CAP_DAC_OVERRIDE`` -- what
    several of this repository's validation and CI environments run as) the file
    stays readable after the chmod and the premise evaporates, while a sandbox
    that denies ``chmod`` outright raises before the assertion is even reached.
    So we patch the read helper to raise ``PermissionError`` and assert THIS
    code's behaviour: the configured-source read failure fails closed. The raised
    error carries the absolute path (as a real ``PermissionError`` would), which
    also proves the whole unresolved structure stays path-free."""
    cards = tmp_path / "cards.jsonl"
    cards.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(_CARDS_ENV, str(cards))

    def _raise_permission_error(_path):
        raise PermissionError(13, "Permission denied", str(cards))

    monkeypatch.setattr(tool, "_load_cards", _raise_permission_error)
    result = tool.collision_preflight(task_id="T1", runner="r", topic="x")
    assert result["cards_source_resolved"] is False
    assert result["active_card_count"] is None
    assert result["would_collide"] is None
    assert result["read_only"] is True and result["mutated_state"] is False
    _assert_unresolved_no_path_leak(result, cards)


def test_preflight_canonical_non_readiness_sqlite_failure_is_unresolved(monkeypatch) -> None:
    """On the canonical (no-override) branch a deleted/locked DB after the
    readiness check raises ``sqlite3.OperationalError`` -- NOT a TaskStoreError --
    inside the scan. That must still yield the UNRESOLVED shape, never an escaped
    exception and never a confident zero."""
    from aiworkhub import core

    def _boom() -> list:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(core, "_active_cards_for_collision_guard", _boom)
    result = tool.collision_preflight(task_id="T1", runner="r", topic="x")
    assert result["cards_source_resolved"] is False
    assert result["active_card_count"] is None
    assert result["would_collide"] is None
    assert result["cards_source"] == "canonical_task_store"
    reason = result["unresolved_reason"]
    assert reason and "/" not in reason and "\\" not in reason


def test_preflight_resolved_explicit_source_is_path_free(tmp_path, monkeypatch) -> None:
    """The RESOLVED explicit-source branch must be path-free too. Redaction that
    applies only to the failure path is not redaction: the success path is the
    one that runs constantly, and it previously emitted the operator's raw
    absolute filesystem path in ``cards_source``. ``topic`` is omitted here to
    prove it is no longer required."""
    cards = tmp_path / "operator_cards.jsonl"
    cards.write_text(
        json.dumps({"task_id": "T1", "runner": "r", "status": "processing"}),
        encoding="utf-8",
    )
    monkeypatch.setenv(_CARDS_ENV, str(cards))
    result = tool.collision_preflight(task_id="T9", runner="r2")
    assert result["cards_source_resolved"] is True
    assert result["active_card_count"] == 1
    source = result["cards_source"]
    assert str(cards) not in source
    assert cards.name not in source
    assert "/" not in source and "\\" not in source
    assert source == f"configured:{_CARDS_ENV}"


def _build_tasks_store(db_path: Path, rows: list[tuple]) -> None:
    """Create a minimal canonical ``tasks`` store with the columns the scan reads."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT, runner TEXT, topic TEXT, status TEXT,"
            " worker_status TEXT, card_json TEXT, archived_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status,"
            " card_json, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_preflight_repo_arg_scopes_scan_to_that_repository(tmp_path, monkeypatch) -> None:
    """A supplied ``repo`` scopes the canonical scan to THAT repository's store,
    not the ambient process-bound one. The active count must reflect the named
    repo (2 active of 3 rows), never the ambient guard's count."""
    # Ambient canonical scan would report a DIFFERENT count (1). If ``repo`` were
    # silently dropped the preflight would answer about the wrong repository.
    monkeypatch.setattr(
        core,
        "_active_cards_for_collision_guard",
        lambda: [{"task_id": "AMBIENT", "runner": "r", "status": "processing"}],
    )
    db = tmp_path / "named_repo_store.sqlite3"
    _build_tasks_store(
        db,
        [
            ("R1", "r", "t", "processing", None, "{}", None),  # active
            ("R2", "r", "t", "review", None, "{}", None),      # active
            ("R3", "r", "t", "finished", None, "{}", None),    # terminal -> excluded
        ],
    )
    named_repo = tmp_path / "named_repo"
    named_repo.mkdir()

    def _readiness(root):
        assert Path(root) == named_repo
        return task_store.StorageReadiness(True, "ready", "repo_named", str(db))

    monkeypatch.setattr(task_store, "storage_readiness", _readiness)
    result = tool.collision_preflight(task_id="R9", runner="r2", repo=named_repo)
    assert result["cards_source_resolved"] is True
    assert result["cards_source"] == "canonical_task_store"  # path-free
    assert result["active_card_count"] == 2  # the named repo, not the ambient 1
    assert result["read_only"] is True and result["mutated_state"] is False


def test_preflight_repo_arg_unreadable_store_is_unresolved(tmp_path, monkeypatch) -> None:
    """When the NAMED repository's store is not ready the preflight fails closed
    with the UNRESOLVED shape -- a scoped answer it cannot read is never a
    confident zero, and the reason leaks no absolute path."""
    named_repo = tmp_path / "unready_repo"
    named_repo.mkdir()
    monkeypatch.setattr(
        task_store,
        "storage_readiness",
        lambda root: task_store.StorageReadiness(False, "canonical_db_missing", "repo_x", ""),
    )
    result = tool.collision_preflight(task_id="T1", runner="r", repo=named_repo)
    assert result["cards_source_resolved"] is False
    assert result["active_card_count"] is None
    assert result["would_collide"] is None
    reason = result["unresolved_reason"]
    assert reason and "/" not in reason and "\\" not in reason


def test_default_canonical_unresolved_reason_has_no_absolute_path() -> None:
    """With no override the preflight resolves from the canonical task store.
    Where the store is not provisioned (an isolated worktree) that is UNRESOLVED
    with a path-free reason; where it is provisioned it resolves cleanly."""
    result = tool.collision_preflight(task_id="T1", runner="r", topic="x")
    if result["cards_source_resolved"]:
        assert result["cards_source"] == "canonical_task_store"
        assert isinstance(result["active_card_count"], int)
        assert result["unresolved_reason"] is None
        return
    assert result["active_card_count"] is None
    assert result["would_collide"] is None
    reason = result["unresolved_reason"]
    assert reason and "/" not in reason and "\\" not in reason
