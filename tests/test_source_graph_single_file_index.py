"""NF15 single-file Source Graph maintenance API focused regression.

Covers: index_file(repo_root, path, expected_hash),
remove_file(repo_root, path), lexical symlink rejection before resolve,
POSIX/Windows absolute/traversal/out-of-repo rejection, default and
configured exclude_globs including nested paths, expected-hash fail-closed,
exactly-one-file transactional mutation, unrelated-row/generation
preservation, idempotent missing-file removal.

Run: python3 -m pytest -q tests/test_source_graph_single_file_index.py
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import worker_ai_tools_mcp as worker_tools
from aiworkhub.repository_state import bootstrap_repository


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _new_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entity_count_for_file(conn, file_path: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM entities WHERE file_path=?", (file_path,)
    ).fetchone()[0]


def _edge_count_for_file(conn, file_path: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM edges WHERE file_path=?", (file_path,)
    ).fetchone()[0]


def _open_fd_count() -> int:
    proc_fd = Path("/proc/self/fd")
    if not proc_fd.exists():
        pytest.skip("/proc/self/fd is unavailable")
    return len(os.listdir(proc_fd))


# ---------------------------------------------------------------------------
# 1. Happy-path index_file
# ---------------------------------------------------------------------------

def test_index_file_basic_python(tmp_path):
    repo = _new_repo(tmp_path, "basic")
    source = "def hello():\n    return 'world'\n"
    _write(repo / "lib" / "greet.py", source)
    expected = _sha256(source)
    result = sg.index_file(repo, "lib/greet.py", expected)
    assert result["ok"] is True
    assert result["file_path"] == "lib/greet.py"
    assert result["source_hash"] == expected
    assert result["language"] == "python"
    assert result["status"] == "ok"
    assert result["entities"] >= 1
    assert result["edges"] >= 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert _entity_count_for_file(conn, "lib/greet.py") == result["entities"]
        assert _edge_count_for_file(conn, "lib/greet.py") == result["edges"]
        funcs = sg.func(conn, "hello")
        assert len(funcs) == 1
        assert funcs[0]["file_path"] == "lib/greet.py"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Happy-path remove_file
# ---------------------------------------------------------------------------

def test_remove_file_basic(tmp_path):
    repo = _new_repo(tmp_path, "remove")
    source = "def bye():\n    return 'done'\n"
    _write(repo / "lib" / "farewell.py", source)
    expected = _sha256(source)
    sg.index_file(repo, "lib/farewell.py", expected)
    result = sg.remove_file(repo, "lib/farewell.py")
    assert result["ok"] is True
    assert result["file_path"] == "lib/farewell.py"
    assert result["removed_entities"] >= 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert _entity_count_for_file(conn, "lib/farewell.py") == 0
        assert _edge_count_for_file(conn, "lib/farewell.py") == 0
    finally:
        conn.close()


def test_private_db_override_does_not_leak_across_threads(tmp_path):
    canonical_repo = _new_repo(tmp_path, "canonical_thread")
    overlay_repo = _new_repo(tmp_path, "overlay_thread")
    canonical_source = "def canonical_thread_symbol():\n    return 1\n"
    overlay_source = "def overlay_thread_symbol():\n    return 2\n"
    _write(canonical_repo / "canonical.py", canonical_source)
    _write(overlay_repo / "overlay.py", overlay_source)
    overlay_db = tmp_path / "private" / "overlay.sqlite"
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def overlay_writer() -> None:
        try:
            with sg.database_path_override(overlay_db):
                entered.set()
                assert release.wait(timeout=5)
                sg.index_file(
                    overlay_repo, "overlay.py", _sha256(overlay_source)
                )
        except BaseException as exc:  # preserve thread failure for assertion
            errors.append(exc)

    thread = threading.Thread(target=overlay_writer)
    thread.start()
    assert entered.wait(timeout=5)
    # This call runs while another thread owns a private overlay context. It
    # must still resolve the canonical repository database.
    sg.index_file(
        canonical_repo, "canonical.py", _sha256(canonical_source)
    )
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []

    canonical_conn = sg.connect(sg.resolve_db_path(canonical_repo), read_only=True)
    overlay_conn = sg.connect(overlay_db, read_only=True)
    try:
        assert _entity_count_for_file(canonical_conn, "canonical.py") >= 1
        assert _entity_count_for_file(canonical_conn, "overlay.py") == 0
        assert _entity_count_for_file(overlay_conn, "overlay.py") >= 1
        assert _entity_count_for_file(overlay_conn, "canonical.py") == 0
    finally:
        canonical_conn.close()
        overlay_conn.close()


# ---------------------------------------------------------------------------
# 3. Absolute path rejection (POSIX)
# ---------------------------------------------------------------------------

def test_index_file_rejects_absolute_path_posix(tmp_path):
    repo = _new_repo(tmp_path, "abs_posix")
    with pytest.raises(sg.SourceGraphError, match="absolute"):
        sg.index_file(repo, "/etc/passwd", "abc123")


# ---------------------------------------------------------------------------
# 4. Absolute path rejection (Windows-style)
# ---------------------------------------------------------------------------

def test_index_file_rejects_absolute_path_windows(tmp_path):
    repo = _new_repo(tmp_path, "abs_win")
    with pytest.raises(sg.SourceGraphError, match="absolute"):
        sg.index_file(repo, "C:\\Windows\\system32\\drivers\\etc\\hosts", "abc123")


# ---------------------------------------------------------------------------
# 5. Traversal rejection
# ---------------------------------------------------------------------------

def test_index_file_rejects_traversal(tmp_path):
    repo = _new_repo(tmp_path, "traversal")
    with pytest.raises(sg.SourceGraphError, match="traversal"):
        sg.index_file(repo, "../outside.py", "abc123")


# ---------------------------------------------------------------------------
# 6. Symlink rejection (lexical, before resolve)
# ---------------------------------------------------------------------------

def test_index_file_rejects_symlink_lexically(tmp_path):
    repo = _new_repo(tmp_path, "sym_lex")
    _write(repo / "real.py", "def f():\n    return 1\n")
    symlink = repo / "link.py"
    symlink.symlink_to("real.py")
    with pytest.raises(sg.SourceGraphError, match="symlink"):
        sg.index_file(repo, "link.py", _sha256("def f():\n    return 1\n"))


# ---------------------------------------------------------------------------
# 7. Out-of-repo rejection
# ---------------------------------------------------------------------------

def test_index_file_rejects_out_of_repo_via_symlink(tmp_path):
    repo = _new_repo(tmp_path, "outside")
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "escaped.py").write_text("x = 1\n", encoding="utf-8")
    symlink = repo / "link.py"
    symlink.symlink_to((sibling / "escaped.py").resolve())
    with pytest.raises(sg.SourceGraphError, match="symlink|outside"):
        sg.index_file(repo, "link.py", _sha256("x = 1\n"))


# ---------------------------------------------------------------------------
# 8. Default exclude_glob (nested eval/** path)
# ---------------------------------------------------------------------------

def test_index_file_rejects_default_exclude_glob_nested(tmp_path):
    repo = _new_repo(tmp_path, "def_glob")
    source = '{"key": "val"}\n'
    _write(repo / "eval" / "sub" / "results.json", source)
    with pytest.raises(sg.SourceGraphError, match="excluded_glob"):
        sg.index_file(repo, "eval/sub/results.json", _sha256(source))


# ---------------------------------------------------------------------------
# 9. Configured exclude_glob (user-added pattern)
# ---------------------------------------------------------------------------

def test_index_file_rejects_configured_exclude_glob(tmp_path):
    repo = _new_repo(tmp_path, "cfg_glob")
    sg.ensure_ignore_config(repo)
    cfg = sg.ignore_config_path(repo)
    payload = json.loads(cfg.read_text("utf-8"))
    payload["exclude_globs"].append("generated/**")
    cfg.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
    source = "def generated_func():\n    pass\n"
    _write(repo / "generated" / "code" / "auto.py", source)
    with pytest.raises(sg.SourceGraphError, match="excluded_glob"):
        sg.index_file(repo, "generated/code/auto.py", _sha256(source))


# ---------------------------------------------------------------------------
# 10. Excluded dir (.venv)
# ---------------------------------------------------------------------------

def test_index_file_rejects_excluded_dir(tmp_path):
    repo = _new_repo(tmp_path, "excl_dir")
    source = "def in_venv():\n    return 1\n"
    _write(repo / ".venv" / "lib" / "hidden.py", source)
    with pytest.raises(sg.SourceGraphError, match="excluded_dir"):
        sg.index_file(repo, ".venv/lib/hidden.py", _sha256(source))


# ---------------------------------------------------------------------------
# 11. Excluded dir (.aiworkhub)
# ---------------------------------------------------------------------------

def test_index_file_rejects_dot_aiworkhub_dir(tmp_path):
    repo = _new_repo(tmp_path, "hub_dir")
    source = "def in_hub():\n    return 1\n"
    _write(repo / ".aiworkhub" / "internal.py", source)
    with pytest.raises(sg.SourceGraphError, match="excluded_dir"):
        sg.index_file(repo, ".aiworkhub/internal.py", _sha256(source))


# ---------------------------------------------------------------------------
# 12. Expected-hash mismatch (fail-closed)
# ---------------------------------------------------------------------------

def test_index_file_hash_mismatch(tmp_path):
    repo = _new_repo(tmp_path, "hash_mis")
    source = "def foo():\n    return 1\n"
    _write(repo / "pkg" / "core.py", source)
    with pytest.raises(sg.SourceGraphError, match="hash_mismatch"):
        sg.index_file(repo, "pkg/core.py", "deadbeef" * 8)


# ---------------------------------------------------------------------------
# 13. Empty expected_hash rejection
# ---------------------------------------------------------------------------

def test_index_file_empty_expected_hash(tmp_path):
    repo = _new_repo(tmp_path, "empty_hash")
    source = "def foo():\n    return 1\n"
    _write(repo / "pkg" / "core.py", source)
    with pytest.raises(sg.SourceGraphError, match="expected_hash_required"):
        sg.index_file(repo, "pkg/core.py", "")


# ---------------------------------------------------------------------------
# 14. Missing file rejection
# ---------------------------------------------------------------------------

def test_index_file_rejects_missing_file(tmp_path):
    repo = _new_repo(tmp_path, "missing")
    with pytest.raises(sg.SourceGraphError, match="unresolvable|unreadable"):
        sg.index_file(repo, "does_not_exist.py", "abc123")


# ---------------------------------------------------------------------------
# 15. Unsupported extension rejection
# ---------------------------------------------------------------------------

def test_index_file_rejects_unsupported_extension(tmp_path):
    repo = _new_repo(tmp_path, "bad_ext")
    source = "plain text content\n"
    _write(repo / "notes.txt", source)
    with pytest.raises(sg.SourceGraphError, match="unsupported_extension"):
        sg.index_file(repo, "notes.txt", _sha256(source))


# ---------------------------------------------------------------------------
# 16. Null byte in path rejection
# ---------------------------------------------------------------------------

def test_index_file_rejects_null_byte_in_path(tmp_path):
    repo = _new_repo(tmp_path, "null_byte")
    with pytest.raises(sg.SourceGraphError, match="null_byte"):
        sg.index_file(repo, "foo\0bar.py", "abc123")


# ---------------------------------------------------------------------------
# 17. Non-string path rejection
# ---------------------------------------------------------------------------

def test_index_file_rejects_non_string_path(tmp_path):
    repo = _new_repo(tmp_path, "non_str")
    with pytest.raises(sg.SourceGraphError, match="path_not_string"):
        sg.index_file(repo, 42, "abc123")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 18. Unrelated-row preservation after index_file
# ---------------------------------------------------------------------------

def test_index_file_preserves_unrelated_rows(tmp_path):
    repo = _new_repo(tmp_path, "unrelated")
    source1 = "def first():\n    return 1\n"
    source2 = "def second():\n    return 2\n"
    _write(repo / "a.py", source1)
    _write(repo / "b.py", source2)
    sg.index_file(repo, "a.py", _sha256(source1))
    sg.index_file(repo, "b.py", _sha256(source2))

    # Re-index a.py with a modified version.
    source1b = "def first():\n    return 42\n"
    _write(repo / "a.py", source1b)
    sg.index_file(repo, "a.py", _sha256(source1b))

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert _entity_count_for_file(conn, "a.py") >= 1
        assert _entity_count_for_file(conn, "b.py") >= 1
        b_rows = conn.execute(
            "SELECT * FROM entities WHERE file_path='b.py'"
        ).fetchall()
        assert len(b_rows) >= 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 19. Unrelated-row preservation after remove_file
# ---------------------------------------------------------------------------

def test_remove_file_preserves_unrelated_rows(tmp_path):
    repo = _new_repo(tmp_path, "rm_unrel")
    source1 = "def one():\n    return 1\n"
    source2 = "def two():\n    return 2\n"
    _write(repo / "one.py", source1)
    _write(repo / "two.py", source2)
    sg.index_file(repo, "one.py", _sha256(source1))
    sg.index_file(repo, "two.py", _sha256(source2))

    sg.remove_file(repo, "one.py")
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert _entity_count_for_file(conn, "one.py") == 0
        assert _entity_count_for_file(conn, "two.py") >= 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 20. Generation authority (build_revision) preservation
# ---------------------------------------------------------------------------

def test_index_file_preserves_generation_authority(tmp_path):
    repo = _new_repo(tmp_path, "gen_auth")
    source = "def preserve():\n    return 1\n"
    _write(repo / "lib" / "core.py", source)
    sg.index_file(repo, "lib/core.py", _sha256(source))

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        rows = conn.execute(
            "SELECT build_revision FROM entities WHERE file_path='lib/core.py'"
        ).fetchall()
        for row in rows:
            assert row["build_revision"] == sg.BUILD_REVISION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 21. Exactly-one-file transactional mutation
# ---------------------------------------------------------------------------

def test_index_file_exactly_one_file_mutated(tmp_path):
    repo = _new_repo(tmp_path, "one_file")
    source_a = "def a():\n    return 'a'\n"
    source_b = "def b():\n    return 'b'\n"
    source_c = "def c():\n    return 'c'\n"
    _write(repo / "a.py", source_a)
    _write(repo / "b.py", source_b)
    _write(repo / "c.py", source_c)
    sg.index_file(repo, "a.py", _sha256(source_a))
    sg.index_file(repo, "b.py", _sha256(source_b))
    sg.index_file(repo, "c.py", _sha256(source_c))

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        files_before = {
            row["file_path"]
            for row in conn.execute("SELECT DISTINCT file_path FROM files")
        }
    finally:
        conn.close()
    assert "a.py" in files_before
    assert "b.py" in files_before
    assert "c.py" in files_before

    # Rewrite only b.py, assert only b.py changes.
    source_b2 = "def b():\n    return 'b-changed'\n"
    _write(repo / "b.py", source_b2)
    sg.index_file(repo, "b.py", _sha256(source_b2))

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        files_after = {
            row["file_path"]
            for row in conn.execute("SELECT DISTINCT file_path FROM files")
        }
        assert "a.py" in files_after
        assert "b.py" in files_after
        assert "c.py" in files_after
        a_row = conn.execute(
            "SELECT source_hash FROM files WHERE file_path='a.py'"
        ).fetchone()
        assert a_row["source_hash"] == _sha256(source_a)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 22. Idempotent remove (file never indexed)
# ---------------------------------------------------------------------------

def test_remove_file_idempotent_when_not_indexed(tmp_path):
    repo = _new_repo(tmp_path, "idem_rm")
    result = sg.remove_file(repo, "never_indexed.py")
    assert result["ok"] is True
    assert result["removed_entities"] == 0


# ---------------------------------------------------------------------------
# 23. Idempotent remove (file deleted from disk but still indexed)
# ---------------------------------------------------------------------------

def test_remove_file_idempotent_when_file_deleted_from_disk(tmp_path):
    repo = _new_repo(tmp_path, "idem_del")
    source = "def temp():\n    return 1\n"
    _write(repo / "temp.py", source)
    sg.index_file(repo, "temp.py", _sha256(source))
    (repo / "temp.py").unlink()
    result = sg.remove_file(repo, "temp.py")
    assert result["ok"] is True
    assert result["removed_entities"] >= 1


# ---------------------------------------------------------------------------
# 24. Full cycle: index then remove
# ---------------------------------------------------------------------------

def test_index_remove_full_cycle(tmp_path):
    repo = _new_repo(tmp_path, "cycle")
    source = "class Widget:\n    def work(self):\n        return 42\n"
    _write(repo / "widget.py", source)
    expected = _sha256(source)
    idx = sg.index_file(repo, "widget.py", expected)
    assert idx["ok"]
    assert idx["entities"] >= 1

    rm = sg.remove_file(repo, "widget.py")
    assert rm["ok"]
    assert rm["removed_entities"] >= 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert _entity_count_for_file(conn, "widget.py") == 0
        assert _edge_count_for_file(conn, "widget.py") == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 25. File-level evidence for non-semantic language (.json)
# ---------------------------------------------------------------------------

def test_index_file_file_evidence_language(tmp_path):
    repo = _new_repo(tmp_path, "file_ev")
    source = '{"key": "val"}\n'
    _write(repo / "data.json", source)
    result = sg.index_file(repo, "data.json", _sha256(source))
    assert result["ok"]
    assert result["language"] == "json"
    assert result["status"] == "file_evidence_only"
    assert result["entities"] >= 1  # file-level entity
    assert result["edges"] == 0


# ---------------------------------------------------------------------------
# 26. remove_file rejects bad paths (same validation as index_file)
# ---------------------------------------------------------------------------

def test_remove_file_rejects_absolute_path(tmp_path):
    repo = _new_repo(tmp_path, "rm_abs")
    with pytest.raises(sg.SourceGraphError, match="absolute"):
        sg.remove_file(repo, "/etc/hosts")


def test_remove_file_rejects_traversal(tmp_path):
    repo = _new_repo(tmp_path, "rm_trav")
    with pytest.raises(sg.SourceGraphError, match="traversal"):
        sg.remove_file(repo, "../secret.py")


def test_remove_file_rejects_symlink(tmp_path):
    repo = _new_repo(tmp_path, "rm_sym")
    _write(repo / "real.py", "def f():\n    return 1\n")
    symlink = repo / "link.py"
    symlink.symlink_to("real.py")
    with pytest.raises(sg.SourceGraphError, match="symlink"):
        sg.remove_file(repo, "link.py")


# ---------------------------------------------------------------------------
# 27. index_file / remove_file exported exactly once in __all__
# ---------------------------------------------------------------------------

def test_index_file_and_remove_file_exported_exactly_once():
    assert "index_file" in sg.__all__
    assert "remove_file" in sg.__all__
    assert sg.__all__.count("index_file") == 1, (
        f"index_file appears {sg.__all__.count('index_file')} times in __all__"
    )
    assert sg.__all__.count("remove_file") == 1, (
        f"remove_file appears {sg.__all__.count('remove_file')} times in __all__"
    )


def test_single_file_mutations_advance_query_cache_identity(tmp_path):
    repo = _new_repo(tmp_path, "cache_identity")
    db_path = sg.resolve_db_path(repo)
    conn = sg.connect(db_path)
    conn.close()
    before = worker_tools._source_graph_index_identity(
        db_path, default_revision=sg.BUILD_REVISION,
    )

    source = "def cache_visible():\n    return True\n"
    _write(repo / "cache_visible.py", source)
    sg.index_file(repo, "cache_visible.py", _sha256(source))
    after_index = worker_tools._source_graph_index_identity(
        db_path, default_revision=sg.BUILD_REVISION,
    )
    assert after_index["finished_at"] > before["finished_at"]

    conn = sg.connect(db_path)
    try:
        payload = json.loads(conn.execute(
            "SELECT value FROM meta WHERE key='single_file_last_mutation'"
        ).fetchone()[0])
    finally:
        conn.close()
    assert payload["operation"] == "index"
    assert payload["file_path"] == "cache_visible.py"

    with pytest.raises(sg.SourceGraphError, match="hash_mismatch"):
        sg.index_file(repo, "cache_visible.py", "bad-hash")
    after_rejection = worker_tools._source_graph_index_identity(
        db_path, default_revision=sg.BUILD_REVISION,
    )
    assert after_rejection == after_index

    sg.remove_file(repo, "cache_visible.py")
    after_remove = worker_tools._source_graph_index_identity(
        db_path, default_revision=sg.BUILD_REVISION,
    )
    assert after_remove["finished_at"] > after_index["finished_at"]

    conn = sg.connect(db_path)
    try:
        payload = json.loads(conn.execute(
            "SELECT value FROM meta WHERE key='single_file_last_mutation'"
        ).fetchone()[0])
    finally:
        conn.close()
    assert payload["operation"] == "remove"
    assert payload["file_path"] == "cache_visible.py"


def test_single_file_mutation_identity_is_repository_scoped(tmp_path):
    repo_a = _new_repo(tmp_path, "cache_repo_a")
    repo_b = _new_repo(tmp_path, "cache_repo_b")
    db_a = sg.resolve_db_path(repo_a)
    db_b = sg.resolve_db_path(repo_b)
    for db_path in (db_a, db_b):
        conn = sg.connect(db_path)
        conn.close()

    source_b = "def only_b():\n    return 'b'\n"
    _write(repo_b / "only_b.py", source_b)
    sg.index_file(repo_b, "only_b.py", _sha256(source_b))
    identity_b = worker_tools._source_graph_index_identity(
        db_b, default_revision=sg.BUILD_REVISION,
    )

    source_a = "def only_a():\n    return 'a'\n"
    _write(repo_a / "only_a.py", source_a)
    sg.index_file(repo_a, "only_a.py", _sha256(source_a))

    assert worker_tools._source_graph_index_identity(
        db_b, default_revision=sg.BUILD_REVISION,
    ) == identity_b
    assert worker_tools._source_graph_index_identity(
        db_a, default_revision=sg.BUILD_REVISION,
    )["finished_at"]


def test_index_file_failure_rolls_back_rows_and_mutation_marker(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "cache_rollback")
    original = "def stable():\n    return 1\n"
    _write(repo / "stable.py", original)
    sg.index_file(repo, "stable.py", _sha256(original))
    db_path = sg.resolve_db_path(repo)

    conn = sg.connect(db_path)
    try:
        original_file_hash = conn.execute(
            "SELECT source_hash FROM files WHERE file_path='stable.py'"
        ).fetchone()[0]
        original_marker = conn.execute(
            "SELECT value FROM meta WHERE key='single_file_last_mutation'"
        ).fetchone()[0]
    finally:
        conn.close()

    updated = "def stable():\n    return 2\n"
    _write(repo / "stable.py", updated)

    def fail_write(*args, **kwargs):
        raise RuntimeError("synthetic_extraction_write_failure")

    monkeypatch.setattr(sg, "_write_extraction", fail_write)
    with pytest.raises(RuntimeError, match="synthetic_extraction_write_failure"):
        sg.index_file(repo, "stable.py", _sha256(updated))

    conn = sg.connect(db_path)
    try:
        assert conn.execute(
            "SELECT source_hash FROM files WHERE file_path='stable.py'"
        ).fetchone()[0] == original_file_hash
        assert conn.execute(
            "SELECT value FROM meta WHERE key='single_file_last_mutation'"
        ).fetchone()[0] == original_marker
        assert _entity_count_for_file(conn, "stable.py") >= 1
    finally:
        conn.close()


def test_extract_file_from_bytes_matches_extract_file_for_same_bytes(tmp_path):
    repo = _new_repo(tmp_path, "from_bytes_parity")
    source = "def parity():\n    return 1\n"
    target = repo / "parity.py"
    _write(target, source)

    from_path = sg.sgast.extract_file(repo, target, build_revision=sg.BUILD_REVISION)
    from_bytes = sg.sgast.extract_file_from_bytes(
        repo,
        target,
        source.encode("utf-8"),
        build_revision=sg.BUILD_REVISION,
    )

    assert from_bytes == from_path


def test_index_file_fails_closed_when_no_safe_nofollow_authority(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "no_nofollow")
    source = "def blocked():\n    return 1\n"
    _write(repo / "blocked.py", source)
    monkeypatch.delattr(sg.os, "O_NOFOLLOW", raising=False)

    with pytest.raises(sg.SourceGraphError, match="safe_open_unsupported"):
        sg.index_file(repo, "blocked.py", _sha256(source))

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert _entity_count_for_file(conn, "blocked.py") == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_path='blocked.py'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_index_file_fails_closed_when_nofollow_flag_is_zero(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "zero_nofollow")
    source = "def blocked_zero():\n    return 1\n"
    _write(repo / "blocked_zero.py", source)
    monkeypatch.setattr(sg.os, "O_NOFOLLOW", 0, raising=False)

    with pytest.raises(sg.SourceGraphError, match="safe_open_unsupported"):
        sg.index_file(repo, "blocked_zero.py", _sha256(source))

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert _entity_count_for_file(conn, "blocked_zero.py") == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_path='blocked_zero.py'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_index_file_descriptor_chain_does_not_leak_success_or_parent_failure(
    tmp_path,
):
    repo = _new_repo(tmp_path, "descriptor_chain")
    source = "def nested_descriptor_chain():\n    return 1\n"
    _write(repo / "pkg" / "nested" / "target.py", source)

    before = _open_fd_count()
    result = sg.index_file(repo, "pkg/nested/target.py", _sha256(source))
    after_success = _open_fd_count()

    assert result["ok"] is True
    assert after_success == before

    outside = tmp_path / "outside_descriptor_chain"
    _write(outside / "target.py", source)
    real_validate = sg._validate_single_file_path
    swapped = False

    def swap_parent_after_validation(repo_root: Path, path: str) -> Path:
        nonlocal swapped
        resolved = real_validate(repo_root, path)
        if not swapped:
            swapped = True
            (repo / "pkg" / "nested").rename(repo / "pkg" / "nested.original")
            (repo / "pkg" / "nested").symlink_to(outside, target_is_directory=True)
        return resolved

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            sg, "_validate_single_file_path", swap_parent_after_validation
        )
        with pytest.raises(sg.SourceGraphError, match="symlink|unreadable"):
            sg.index_file(repo, "pkg/nested/target.py", _sha256(source))

    assert swapped is True
    assert _open_fd_count() == before


def test_index_file_rejects_oversized_fstat_before_read(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "oversized_before_read")
    source = "x = 1\n"
    _write(repo / "too_big.py", source)
    monkeypatch.setattr(sg, "SOURCE_GRAPH_AUTHENTICATED_FILE_BYTE_LIMIT", 4)
    read_calls = []
    real_read = sg.os.read

    def record_read(fd: int, size: int) -> bytes:
        read_calls.append((fd, size))
        return real_read(fd, size)

    monkeypatch.setattr(sg.os, "read", record_read)
    with pytest.raises(sg.SourceGraphError, match="too_large"):
        sg.index_file(repo, "too_big.py", _sha256(source))

    assert read_calls == []
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert _entity_count_for_file(conn, "too_big.py") == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_path='too_big.py'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_index_file_rejects_cumulative_overrun_and_rolls_back(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "cumulative_overrun")
    original = "def stable():\n    return 1\n"
    _write(repo / "stable.py", original)
    sg.index_file(repo, "stable.py", _sha256(original))
    db_path = sg.resolve_db_path(repo)

    conn = sg.connect(db_path)
    try:
        original_file_row = conn.execute(
            "SELECT source_hash, file_size FROM files WHERE file_path='stable.py'"
        ).fetchone()
        original_entities = conn.execute(
            """
            SELECT kind, name, qualname, line_start, line_end, source_hash
            FROM entities
            WHERE file_path='stable.py'
            ORDER BY kind, name, qualname, line_start, line_end
            """
        ).fetchall()
    finally:
        conn.close()

    updated = "def stable():\n    return 222222\n"
    _write(repo / "stable.py", updated)
    monkeypatch.setattr(sg, "SOURCE_GRAPH_AUTHENTICATED_FILE_BYTE_LIMIT", 24)
    real_fstat = sg.os.fstat

    class FakeRegularFileStat:
        def __init__(self, original_stat):
            self.st_mode = original_stat.st_mode
            self.st_size = 24
            self.st_mtime_ns = original_stat.st_mtime_ns

    def lie_about_regular_file_size(fd: int):
        file_stat = real_fstat(fd)
        if sg.stat.S_ISREG(file_stat.st_mode):
            return FakeRegularFileStat(file_stat)
        return file_stat

    monkeypatch.setattr(sg.os, "fstat", lie_about_regular_file_size)
    with pytest.raises(sg.SourceGraphError, match="too_large"):
        sg.index_file(repo, "stable.py", _sha256(updated))
    monkeypatch.setattr(sg.os, "fstat", real_fstat)

    conn = sg.connect(db_path)
    try:
        assert conn.execute(
            "SELECT source_hash, file_size FROM files WHERE file_path='stable.py'"
        ).fetchone() == original_file_row
        assert conn.execute(
            """
            SELECT kind, name, qualname, line_start, line_end, source_hash
            FROM entities
            WHERE file_path='stable.py'
            ORDER BY kind, name, qualname, line_start, line_end
            """
        ).fetchall() == original_entities
    finally:
        conn.close()


def test_index_file_accepts_exactly_at_authenticated_byte_limit(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "at_limit")
    source = "def at_limit():\n    return 1\n"
    _write(repo / "at_limit.py", source)
    source_bytes = source.encode("utf-8")
    monkeypatch.setattr(
        sg, "SOURCE_GRAPH_AUTHENTICATED_FILE_BYTE_LIMIT", len(source_bytes)
    )

    result = sg.index_file(repo, "at_limit.py", hashlib.sha256(source_bytes).hexdigest())

    assert result["ok"] is True
    assert result["source_hash"] == hashlib.sha256(source_bytes).hexdigest()
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        file_row = conn.execute(
            "SELECT source_hash, file_size FROM files WHERE file_path='at_limit.py'"
        ).fetchone()
        assert file_row["source_hash"] == result["source_hash"]
        assert file_row["file_size"] == len(source_bytes)
        assert _entity_count_for_file(conn, "at_limit.py") >= 1
    finally:
        conn.close()


def test_index_file_uses_authenticated_bytes_after_pathname_swap(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "same_bytes_barrier")
    original = "def original_symbol():\n    return 'original'\n"
    replacement = "def swapped_symbol():\n    return 'replacement'\n"
    target = repo / "victim.py"
    _write(target, original)
    real_from_bytes = sg.sgast.extract_file_from_bytes
    swapped = False

    def swap_before_extract(repo_root, file_path, raw, *, build_revision):
        nonlocal swapped
        if not swapped:
            swapped = True
            _write(file_path, replacement)
        return real_from_bytes(
            repo_root,
            file_path,
            raw,
            build_revision=build_revision,
        )

    monkeypatch.setattr(sg.sgast, "extract_file_from_bytes", swap_before_extract)
    result = sg.index_file(repo, "victim.py", _sha256(original))

    assert swapped is True
    assert result["source_hash"] == _sha256(original)
    assert result["entities"] >= 1
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        file_row = conn.execute(
            "SELECT source_hash, file_size FROM files WHERE file_path='victim.py'"
        ).fetchone()
        assert file_row["source_hash"] == _sha256(original)
        assert file_row["file_size"] == len(original.encode("utf-8"))
        names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM entities WHERE file_path='victim.py'"
            )
        }
        assert "original_symbol" in names
        assert "swapped_symbol" not in names
    finally:
        conn.close()


def test_index_file_rejects_intermediate_parent_swap_to_symlink(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "parent_swap")
    original = "def authenticated_original():\n    return 'repo'\n"
    outside = "def outside_parent_swap():\n    return 'outside'\n"
    target_dir = repo / "nested"
    target = target_dir / "victim.py"
    _write(target, original)
    outside_dir = tmp_path / "outside_parent"
    _write(outside_dir / "victim.py", outside)

    original_hash = _sha256(original)
    baseline_result = sg.index_file(repo, "nested/victim.py", original_hash)
    assert baseline_result["source_hash"] == original_hash

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        baseline_file_row = conn.execute(
            """
            SELECT file_path, language, status, source_hash, file_size
            FROM files
            WHERE file_path='nested/victim.py'
            """
        ).fetchone()
        baseline_entities = conn.execute(
            """
            SELECT kind, name, qualname, line_start, line_end, source_hash
            FROM entities
            WHERE file_path='nested/victim.py'
            ORDER BY kind, name, qualname, line_start, line_end
            """
        ).fetchall()
        assert baseline_file_row is not None
        assert baseline_file_row[3] == original_hash
        assert baseline_entities
        assert sg.find(conn, "authenticated_original")
    finally:
        conn.close()

    real_validate = sg._validate_single_file_path
    swapped = False

    def swap_parent_after_validation(repo_root: Path, path: str) -> Path:
        nonlocal swapped
        resolved = real_validate(repo_root, path)
        if not swapped:
            swapped = True
            target_dir.rename(repo / "nested.original")
            target_dir.symlink_to(outside_dir, target_is_directory=True)
        return resolved

    monkeypatch.setattr(
        sg, "_validate_single_file_path", swap_parent_after_validation
    )
    with pytest.raises(sg.SourceGraphError, match="symlink|unreadable"):
        sg.index_file(repo, "nested/victim.py", _sha256(outside))

    assert swapped is True
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        assert conn.execute(
            """
            SELECT file_path, language, status, source_hash, file_size
            FROM files
            WHERE file_path='nested/victim.py'
            """
        ).fetchone() == baseline_file_row
        assert conn.execute(
            """
            SELECT kind, name, qualname, line_start, line_end, source_hash
            FROM entities
            WHERE file_path='nested/victim.py'
            ORDER BY kind, name, qualname, line_start, line_end
            """
        ).fetchall() == baseline_entities
        assert sg.find(conn, "authenticated_original")
        assert not sg.find(conn, "outside_parent_swap")
    finally:
        conn.close()


def test_index_file_rejects_repo_root_ancestor_swap_to_symlink(
    tmp_path, monkeypatch,
):
    root_parent = tmp_path / "stable_parent"
    repo = root_parent / "repo"
    repo.mkdir(parents=True)
    bootstrap_repository(repo, repo_name="repo")
    original = "def authenticated_ancestor_original():\n    return 'repo'\n"
    outside = "def outside_ancestor_swap():\n    return 'outside'\n"
    _write(repo / "victim.py", original)

    outside_parent = tmp_path / "outside_parent"
    outside_repo = outside_parent / "repo"
    _write(outside_repo / "victim.py", outside)

    original_hash = _sha256(original)
    baseline_result = sg.index_file(repo, "victim.py", original_hash)
    assert baseline_result["source_hash"] == original_hash

    db_path = sg.resolve_db_path(repo)
    conn = sg.connect(db_path, read_only=True)
    try:
        baseline_file_row = conn.execute(
            """
            SELECT file_path, language, status, source_hash, file_size
            FROM files
            WHERE file_path='victim.py'
            """
        ).fetchone()
        baseline_entities = conn.execute(
            """
            SELECT kind, name, qualname, line_start, line_end, source_hash
            FROM entities
            WHERE file_path='victim.py'
            ORDER BY kind, name, qualname, line_start, line_end
            """
        ).fetchall()
        assert baseline_file_row is not None
        assert baseline_file_row[3] == original_hash
        assert baseline_entities
        assert sg.find(conn, "authenticated_ancestor_original")
    finally:
        conn.close()

    moved_parent = tmp_path / "stable_parent.original"
    original_db_path = moved_parent / db_path.relative_to(root_parent)
    real_validate = sg._validate_single_file_path
    swapped = False

    def swap_ancestor_after_validation(repo_root: Path, path: str) -> Path:
        nonlocal swapped
        resolved = real_validate(repo_root, path)
        if not swapped:
            swapped = True
            root_parent.rename(moved_parent)
            root_parent.symlink_to(outside_parent, target_is_directory=True)
        return resolved

    monkeypatch.setattr(
        sg, "_validate_single_file_path", swap_ancestor_after_validation
    )
    with pytest.raises(sg.SourceGraphError, match="symlink|unreadable"):
        sg.index_file(repo, "victim.py", _sha256(outside))

    assert swapped is True
    conn = sg.connect(original_db_path, read_only=True)
    try:
        assert conn.execute(
            """
            SELECT file_path, language, status, source_hash, file_size
            FROM files
            WHERE file_path='victim.py'
            """
        ).fetchone() == baseline_file_row
        assert conn.execute(
            """
            SELECT kind, name, qualname, line_start, line_end, source_hash
            FROM entities
            WHERE file_path='victim.py'
            ORDER BY kind, name, qualname, line_start, line_end
            """
        ).fetchall() == baseline_entities
        assert sg.find(conn, "authenticated_ancestor_original")
        assert not sg.find(conn, "outside_ancestor_swap")
    finally:
        conn.close()


def test_index_file_extraction_error_fails_closed_without_partial_mutation(tmp_path):
    repo = _new_repo(tmp_path, "extract_fail_closed")
    original = "def stable():\n    return 1\n"
    target = repo / "stable.py"
    _write(target, original)
    sg.index_file(repo, "stable.py", _sha256(original))
    db_path = sg.resolve_db_path(repo)

    conn = sg.connect(db_path)
    try:
        before_hash = conn.execute(
            "SELECT source_hash FROM files WHERE file_path='stable.py'"
        ).fetchone()[0]
    finally:
        conn.close()

    broken = "def stable(:\n"
    _write(target, broken)
    with pytest.raises(sg.SourceGraphError, match="extraction_failed"):
        sg.index_file(repo, "stable.py", _sha256(broken))

    conn = sg.connect(db_path, read_only=True)
    try:
        assert conn.execute(
            "SELECT source_hash FROM files WHERE file_path='stable.py'"
        ).fetchone()[0] == before_hash
        assert _entity_count_for_file(conn, "stable.py") >= 1
    finally:
        conn.close()


@pytest.mark.parametrize("fail_on_read_call", [1, 2])
def test_index_file_read_error_fails_closed_without_partial_mutation(
    tmp_path,
    monkeypatch,
    fail_on_read_call,
):
    repo = _new_repo(tmp_path, f"read_fail_{fail_on_read_call}")
    original = "def stable_read():\n    return 1\n"
    target = repo / "stable.py"
    _write(target, original)
    sg.index_file(repo, "stable.py", _sha256(original))
    db_path = sg.resolve_db_path(repo)

    conn = sg.connect(db_path, read_only=True)
    try:
        before_file_row = conn.execute(
            """
            SELECT file_path, language, status, source_hash, file_size
            FROM files
            WHERE file_path='stable.py'
            """
        ).fetchone()
        before_entities = conn.execute(
            """
            SELECT kind, name, qualname, line_start, line_end, source_hash
            FROM entities
            WHERE file_path='stable.py'
            ORDER BY kind, name, qualname, line_start, line_end
            """
        ).fetchall()
    finally:
        conn.close()

    updated = "def stable_read():\n    return 2\n"
    _write(target, updated)
    real_read = sg.os.read
    read_calls = 0

    def failing_read(fd: int, length: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        if read_calls == fail_on_read_call:
            raise OSError(errno.EIO, "synthetic read failure")
        return real_read(fd, length)

    monkeypatch.setattr(sg.os, "read", failing_read)
    with pytest.raises(
        sg.SourceGraphError,
        match="source_graph_single_file_unreadable:stable.py",
    ) as exc_info:
        sg.index_file(repo, "stable.py", _sha256(updated))
    assert isinstance(exc_info.value.__cause__, OSError)
    assert read_calls == fail_on_read_call

    conn = sg.connect(db_path, read_only=True)
    try:
        assert conn.execute(
            """
            SELECT file_path, language, status, source_hash, file_size
            FROM files
            WHERE file_path='stable.py'
            """
        ).fetchone() == before_file_row
        assert conn.execute(
            """
            SELECT kind, name, qualname, line_start, line_end, source_hash
            FROM entities
            WHERE file_path='stable.py'
            ORDER BY kind, name, qualname, line_start, line_end
            """
        ).fetchall() == before_entities
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
