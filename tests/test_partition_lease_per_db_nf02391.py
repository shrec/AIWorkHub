"""The Source Graph writer lease is keyed by the database it protects.

NF-2026-02391: ``index_write_lease`` derived its lock file from the database
directory alone, so every partition DB built under ``database_path_override``
in one overlay directory shared ONE ``index.lock``. A second concurrent
reviewer's ``index_file`` then raised ``SourceGraphBuildInProgressError``
(immediately on POSIX ``flock``; after the 1 s ``msvcrt`` retry on Windows),
``build_partition`` recorded it in ``failed_files`` and that overlay carried
base rows but no delta rows. These tests hold one partition's lease in the
test thread and build a sibling partition in the SAME directory.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from aiworkhub import repository_state
from aiworkhub import source_graph as sg
from aiworkhub import source_graph_partition as sgp
from aiworkhub import storage_registry


def _build_base(repo: Path) -> Path:
    files = {
        "keep.py": "def keep_symbol():\n    return 1\n",
        "change.py": "def original_symbol():\n    return 2\n",
    }
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    repository_state.bootstrap_repository(repo)
    sg.build_index(repo, incremental=False)
    registry = storage_registry.load_storage_registry(repo.resolve())
    base_db = storage_registry.resolve_database_path(registry, "source_graph")
    (repo / "change.py").write_text(
        "def changed_symbol():\n    return 3\n", encoding="utf-8"
    )
    return base_db


def _changed(repo: Path) -> list[dict[str, str]]:
    digest = hashlib.sha256((repo / "change.py").read_bytes()).hexdigest()
    return [{"path": "change.py", "sha256": digest}]


def _entity_names(partition: Path) -> set[str]:
    conn = sg.connect(partition, read_only=True)
    try:
        return {str(row[0]) for row in conn.execute("SELECT name FROM entities")}
    finally:
        conn.close()


def test_canonical_lease_path_keeps_the_index_lock_name(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_base(repo)

    lease = sg._index_write_lease_path(repo)
    assert lease == sg.resolve_db_path(repo).with_name("index.lock")
    assert sg._lease_holder_path(lease).name == "index.lock.holder.json"


def test_sibling_partition_builds_while_another_partition_holds_its_lease(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(repo)
    overlay = tmp_path / "runtime"
    overlay.mkdir()
    holder_db = overlay / ".a.tmp"
    partition = overlay / ".b.tmp"

    with sg.database_path_override(holder_db):
        with sg.index_write_lease(repo) as acquired:
            assert acquired
            held = sg._index_write_lease_path(repo)
            assert held.name == ".a.tmp.index.lock"
            holder = sg.read_index_write_lease_holder(repo)
            assert holder.get("pid") == os.getpid()

            report = sgp.build_partition(
                repo, _changed(repo), partition, base_db_path=base_db,
            )

            assert report.failed_files == ()
            assert report.files_indexed == 1
            # Only the lease this thread still holds remains; the partition's
            # own lock file was removed and the shared name was never used.
            locks = sorted(p.name for p in overlay.glob("*.index.lock"))
            assert locks == [".a.tmp.index.lock"]
            assert not (overlay / "index.lock").exists()
            assert list(overlay.glob(".b.tmp.index.lock*")) == []

    names = _entity_names(partition)
    assert "changed_symbol" in names
    assert "original_symbol" not in names
