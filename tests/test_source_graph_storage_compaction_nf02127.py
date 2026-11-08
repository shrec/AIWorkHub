"""NF-2026-02127: the published Source Graph generation stays compact.

A generation that deletes most of its rows must not be published (or copied
forward by the next incremental build) as a mostly-free-pages file, and leaked
``..build-result.*.json.*.tmp`` files must be reaped without ever touching one
a live build is still writing.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository

THRESHOLD = sg.SOURCE_GRAPH_STAGED_COMPACT_FREELIST_RATIO


def _new_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write_modules(root: Path, count: int) -> list[Path]:
    paths = []
    for index in range(count):
        path = root / f"module_{index:03d}.py"
        body = "".join(
            f"def symbol_{index}_{fn}(value):\n    return value + {fn}\n\n"
            for fn in range(40)
        )
        path.write_text(body, encoding="utf-8")
        paths.append(path)
    return paths


def test_published_generation_is_compact_after_deleting_most_rows(tmp_path):
    repo = _new_repo(tmp_path)
    modules = _write_modules(repo, 40)
    sg.build_index(repo, incremental=False)
    canonical = sg.resolve_db_path(repo)
    assert sg._generation_freelist_ratio(canonical) < THRESHOLD

    for path in modules[2:]:
        path.unlink()
    report = sg.build_index(repo, incremental=True)

    assert report.files_removed == 38
    # The deletes really did leave the candidate bloated ...
    assert report.compaction_performed is True
    assert report.freelist_ratio_before_compaction >= THRESHOLD
    # ... and the file that readers see is compact.
    assert sg._generation_freelist_ratio(canonical) < THRESHOLD
    assert report.database_bytes_after_compaction == canonical.stat().st_size
    assert report.database_bytes_after_compaction < report.database_bytes_before_compaction
    conn = sqlite3.connect(f"{canonical.as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert not [p for p in canonical.parent.iterdir() if p.name.startswith(sg._staging_prefix(canonical))]


def test_incremental_copy_of_a_bloated_generation_is_made_compact(tmp_path):
    repo = _new_repo(tmp_path)
    _write_modules(repo, 3)
    sg.build_index(repo, incremental=False)
    canonical = sg.resolve_db_path(repo)

    conn = sqlite3.connect(str(canonical))
    try:
        conn.execute("CREATE TABLE junk(b BLOB)")
        conn.executemany(
            "INSERT INTO junk VALUES(randomblob(4096))", [() for _ in range(2000)]
        )
        conn.commit()
        conn.execute("DROP TABLE junk")
        conn.commit()
    finally:
        conn.close()
    assert sg._generation_freelist_ratio(canonical) >= THRESHOLD
    bloated_bytes = canonical.stat().st_size

    with sg._staged_generation(canonical, copy_existing=True) as staging:
        assert sg._generation_freelist_ratio(staging) < THRESHOLD
        assert staging.stat().st_size < bloated_bytes
        check = sqlite3.connect(f"{staging.as_uri()}?mode=ro", uri=True)
        try:
            assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert check.execute("SELECT COUNT(*) FROM entities").fetchone()[0] > 0
        finally:
            check.close()
    # The live generation was only read: still readable and untouched.
    assert canonical.stat().st_size == bloated_bytes


def test_stale_build_result_tmp_is_reaped_and_fresh_one_kept(tmp_path):
    repo = _new_repo(tmp_path)
    canonical = sg.resolve_db_path(repo)

    stale = canonical.parent / "..build-result.aaaa.json.bbbb.tmp"
    unrelated_old = canonical.parent / "unrelated.tmp"
    stale.write_bytes(b"")
    unrelated_old.write_bytes(b"")
    old = time.time() - sg.SOURCE_GRAPH_BUILD_RESULT_TMP_MAX_AGE_SECONDS - 60
    os.utime(stale, (old, old))
    os.utime(unrelated_old, (old, old))
    fresh = canonical.parent / "..build-result.cccc.json.dddd.tmp"
    fresh.write_bytes(b"{")

    sg._cleanup_abandoned_staging(canonical)

    assert not stale.exists()
    assert fresh.exists()
    assert unrelated_old.exists()


def test_default_grace_period_is_one_hour():
    assert sg.SOURCE_GRAPH_BUILD_RESULT_TMP_MAX_AGE_SECONDS == 3600.0
