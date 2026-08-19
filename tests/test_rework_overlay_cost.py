"""Rework overlay cost after the full-index clone was deleted (NF-2026-00313).

The rework overlay (``_rework_source_graph_binding``) used to clone the entire
canonical Source Graph database with the SQLite backup API and then reconcile
only the packet's retained paths back onto it -- so every rejected attempt paid
a cost proportional to repository size.  These tests pin the replacement: the
overlay now builds a PARTITION over the packet's retained paths ALONE (the same
``source_graph_partition.build_partition`` primitive the reviewer prewarm uses),
records the canonical base for read-only composition, and never copies the base.

The deliverable numbers -- before/after bytes and wall time for a six-file and a
fifty-file packet -- are printed by :func:`test_rework_overlay_cost_report`.
The other tests prove no full-index copy occurs (caught on a deliberately large
base), that cost scales with the packet rather than the base, and that the
overlay's identity, readiness, symlink and path-safety properties are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import storage_registry
from aiworkhub.worker_ai_tools_mcp import (
    WorkerToolContext,
    WorkerToolError,
    _rework_source_graph_binding,
)
from aiworkhub.worker_workspace import materialize_rework_overlay


class _Fixture(NamedTuple):
    canonical: Path
    workspace: Path
    packet: dict
    packet_path: Path
    retained: int


def _module_source(index: int) -> str:
    return (
        f"def symbol_{index:04d}():\n    return {index}\n\n"
        f"def helper_{index:04d}(value):\n    return value + {index}\n"
    )


def _base_db(repo: Path) -> Path:
    registry = storage_registry.load_storage_registry(repo.resolve())
    return storage_registry.resolve_database_path(registry, "source_graph")


def _forbid_build_index(*_a, **_k):
    raise AssertionError("rework overlay must never rebuild the whole index")


def _partition_file_rows(db_path: Path) -> int:
    # Open the overlay file DIRECTLY (raw sqlite3, never source_graph.connect),
    # so the composed-view TEMP views are NOT installed and this counts the
    # partition's OWN rows -- a clone of the base would show every base file.
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    finally:
        conn.close()


def _partition_filler_rows(db_path: Path) -> int:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM files WHERE file_path LIKE 'filler_%'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _setup(base: Path, *, retained: int, base_filler: int) -> _Fixture:
    """Build a canonical base and a workspace changing ``retained`` files.

    The canonical base carries ``base_filler`` extra modules so a clone of it
    would be obviously large; the workspace edits only the ``retained`` files
    the rework packet declares.
    """

    canonical = base / "canonical"
    workspace = base / "workspace"
    runtime = base / "runtime"
    canonical.mkdir(parents=True)
    workspace.mkdir(parents=True)
    runtime.mkdir(parents=True)

    for i in range(base_filler):
        (canonical / f"filler_{i:04d}.py").write_text(
            _module_source(10_000 + i), encoding="utf-8"
        )
    for i in range(retained):
        (canonical / f"target_{i:04d}.py").write_text(
            _module_source(i), encoding="utf-8"
        )
    repository_state.bootstrap_repository(canonical)
    source_graph.build_index(canonical, incremental=False)

    file_entries: list[tuple[str, str, bytes]] = []
    for i in range(retained):
        rel = f"target_{i:04d}.py"
        content = (
            _module_source(i) + f"\ndef retained_{i:04d}():\n    return {i}\n"
        ).encode("utf-8")
        (workspace / rel).write_bytes(content)
        file_entries.append((rel, hashlib.sha256(content).hexdigest(), content))

    packet = json.loads(
        materialize_rework_overlay(
            "successor-request",
            "rework-task",
            "predecessor-request",
            "rework-task",
            canonical,
            file_entries,
        )
    )
    packet_path = runtime / "rework_overlay.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return _Fixture(canonical, workspace, packet, packet_path, retained)


def _ctx(fx: _Fixture) -> WorkerToolContext:
    return WorkerToolContext(
        task_id=fx.packet["successor_task_id"],
        runner="runner",
        topic="task_mcp",
        request_id=fx.packet["successor_request_id"],
        repo=fx.workspace,
        authority_repo=fx.canonical,
        source_graph_targets=tuple(e["path"] for e in fx.packet["files"]),
        session_topic="task_mcp",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=fx.packet,
        rework_overlay_packet_path=fx.packet_path,
    )


def test_rework_overlay_makes_no_full_copy_on_large_base(tmp_path, monkeypatch):
    fx = _setup(tmp_path, retained=6, base_filler=200)
    monkeypatch.setattr(source_graph, "build_index", _forbid_build_index)

    binding = _rework_source_graph_binding(_ctx(fx))
    partition = binding.db_path
    base_db = _base_db(fx.canonical)

    # Only the packet's six files are indexed -- not the 200 filler modules a
    # clone of the base would have copied wholesale.
    assert _partition_file_rows(partition) == 6
    assert _partition_filler_rows(partition) == 0
    # And the overlay is a small fraction of the base a clone would have written.
    assert partition.stat().st_size < base_db.stat().st_size // 2


def test_rework_overlay_cost_scales_with_packet_not_base(tmp_path, monkeypatch):
    small = _setup(tmp_path / "small", retained=6, base_filler=200)
    big = _setup(tmp_path / "big", retained=50, base_filler=200)
    monkeypatch.setattr(source_graph, "build_index", _forbid_build_index)

    small_part = _rework_source_graph_binding(_ctx(small)).db_path
    big_part = _rework_source_graph_binding(_ctx(big)).db_path

    assert _partition_file_rows(small_part) == 6
    assert _partition_file_rows(big_part) == 50
    # Cost tracks the packet: 50 retained files cost more than 6.
    assert big_part.stat().st_size > small_part.stat().st_size
    # But never the base: both bases are equal-sized, and each overlay stays far
    # below the base -- no clone was made in either case.
    assert big_part.stat().st_size < _base_db(big.canonical).stat().st_size


def test_rework_overlay_keyed_on_canonical_digest(tmp_path):
    fx = _setup(tmp_path, retained=2, base_filler=10)
    binding = _rework_source_graph_binding(_ctx(fx))
    digest = fx.packet["canonical_digest"]
    assert binding.db_path.name == f"rework_source_graph_{digest[:16]}.sqlite"
    assert binding.authority_source == "rework_overlay"
    assert binding.authority_state == "request_scoped_predecessor"
    assert binding.target_request_id == fx.packet["predecessor_request_id"]
    assert binding.target_task_id == fx.packet["predecessor_task_id"]

    # A different canonical_digest lands in a distinct overlay file: the overlay
    # stays keyed on the digest, not on a shared or repository-wide path.
    other = {**fx.packet, "canonical_digest": "b" * 64}
    binding2 = _rework_source_graph_binding(
        replace(_ctx(fx), rework_overlay_packet=other)
    )
    assert binding2.db_path != binding.db_path
    assert binding2.db_path.name == f"rework_source_graph_{'b' * 16}.sqlite"


def test_rework_overlay_composes_base_and_retained(tmp_path):
    fx = _setup(tmp_path, retained=3, base_filler=30)
    binding = _rework_source_graph_binding(_ctx(fx))

    # A read-only open composes the packet-scoped partition with the base: the
    # retained files resolve from the partition while a base-only filler file is
    # still visible -- the base was composed, not copied.
    conn = source_graph.connect(binding.db_path, read_only=True)
    try:
        files = {str(row[0]) for row in conn.execute("SELECT file_path FROM files")}
    finally:
        conn.close()
    assert "target_0000.py" in files
    assert any(name.startswith("filler_") for name in files)


def test_rework_overlay_readiness_reuses_then_rebuilds_on_edit(tmp_path, monkeypatch):
    fx = _setup(tmp_path, retained=4, base_filler=20)
    ctx = _ctx(fx)
    monkeypatch.setattr(source_graph, "build_index", _forbid_build_index)

    real_index_file = source_graph.index_file
    calls = {"n": 0}

    def counting_index_file(repo_root, path, expected_hash):
        calls["n"] += 1
        return real_index_file(repo_root, path, expected_hash)

    monkeypatch.setattr(source_graph, "index_file", counting_index_file)

    first = _rework_source_graph_binding(ctx)
    # The first preparation indexes each retained file exactly once -- packet
    # sized, never base sized.
    assert calls["n"] == 4

    # Readiness reuse: an unchanged workspace snapshot serves the existing
    # overlay without re-indexing anything.
    second = _rework_source_graph_binding(ctx)
    assert second.db_path == first.db_path
    assert second.packet_sha256 == first.packet_sha256
    assert calls["n"] == 4

    # A later worker edit of a retained path changes the observed snapshot, so
    # the packet-scoped partition is rebuilt on the next query -- the base is
    # still never copied (index_file, not build_index, does the work).
    edited = fx.workspace / "target_0000.py"
    edited.write_text(
        _module_source(0) + "\ndef late_symbol_0000():\n    return 0\n",
        encoding="utf-8",
    )
    third = _rework_source_graph_binding(ctx)
    assert third.db_path == first.db_path
    assert third.packet_sha256 != first.packet_sha256
    assert calls["n"] == 8

    conn = source_graph.connect(third.db_path, read_only=True)
    try:
        found = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE name=?", ("late_symbol_0000",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(found) >= 1


def test_rework_overlay_refuses_symlinked_retained_path(tmp_path):
    fx = _setup(tmp_path, retained=2, base_filler=8)
    victim = fx.workspace / "target_0000.py"
    victim.unlink()
    # Point the link at a sibling INSIDE the workspace so the path-escape guard
    # (which runs first, exactly as before) passes and the symlink refusal is
    # the check under test.
    victim.symlink_to(fx.workspace / "target_0001.py")

    with pytest.raises(WorkerToolError) as exc:
        _rework_source_graph_binding(_ctx(fx))
    assert "rework_overlay_path_symlink:target_0000.py" in str(exc.value)


def test_rework_overlay_refuses_path_escaping_workspace(tmp_path):
    fx = _setup(tmp_path, retained=1, base_filler=6)
    # A hand-crafted packet whose retained path escapes the workspace. The
    # digest only needs to be well-formed here; the binding refuses the path
    # before any build side effect, exactly as before.
    packet = {
        "successor_request_id": "successor-request",
        "successor_task_id": "rework-task",
        "predecessor_request_id": "predecessor-request",
        "predecessor_task_id": "rework-task",
        "authority_repo": str(fx.canonical.resolve()),
        "files": [{"path": "../escape.py", "sha256": "0" * 64}],
        "canonical_digest": "a" * 64,
    }
    ctx = replace(_ctx(fx), rework_overlay_packet=packet)
    with pytest.raises(WorkerToolError) as exc:
        _rework_source_graph_binding(ctx)
    assert "rework_overlay_path_escapes_workspace:../escape.py" in str(exc.value)


def test_rework_overlay_cost_report(tmp_path, monkeypatch, capsys):
    """Emit the mandated before/after bytes and wall time for 6 / 50 files."""

    # Build both base fixtures BEFORE installing the guard, so it polices only
    # the overlay preparations below -- never the legitimate base builds.
    six = _setup(tmp_path / "six", retained=6, base_filler=200)
    fifty = _setup(tmp_path / "fifty", retained=50, base_filler=200)
    monkeypatch.setattr(source_graph, "build_index", _forbid_build_index)

    report: dict[str, dict[str, float]] = {}

    started = time.monotonic()
    six_part = _rework_source_graph_binding(_ctx(six)).db_path
    six_wall = time.monotonic() - started
    report["6_file_packet"] = {
        "before_bytes_clone_would_copy": _base_db(six.canonical).stat().st_size,
        "after_bytes_overlay_partition": six_part.stat().st_size,
        "wall_seconds": round(six_wall, 4),
    }

    started = time.monotonic()
    fifty_part = _rework_source_graph_binding(_ctx(fifty)).db_path
    fifty_wall = time.monotonic() - started
    report["50_file_packet"] = {
        "before_bytes_clone_would_copy": _base_db(fifty.canonical).stat().st_size,
        "after_bytes_overlay_partition": fifty_part.stat().st_size,
        "wall_seconds": round(fifty_wall, 4),
    }

    for entry in report.values():
        assert (
            entry["after_bytes_overlay_partition"]
            < entry["before_bytes_clone_would_copy"]
        )
    # Cost scales with the packet, not the base: 50 files cost more than 6 while
    # both bases are the same size.
    assert (
        report["50_file_packet"]["after_bytes_overlay_partition"]
        > report["6_file_packet"]["after_bytes_overlay_partition"]
    )

    print("REWORK_OVERLAY_COST_REPORT " + json.dumps(report, indent=2))
    captured = capsys.readouterr()
    assert "REWORK_OVERLAY_COST_REPORT" in captured.out
