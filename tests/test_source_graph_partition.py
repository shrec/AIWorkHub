"""Partitioned indexing + composed views (card NF-302-SG-PARTITION).

These tests pin the capability the owner mandated: a partition indexed over a
bounded scope, a composed view that answers as one index with exact precedence,
cross-boundary edge resolution in both directions with its limitations NAMED,
an immutable identity-bound view, and -- the whole point -- preparation whose
cost scales with the changed files and NEVER with the base index size, with no
full-index copy ever made.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import source_graph_partition as sgp
from aiworkhub import storage_registry


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base_db_path(repo: Path) -> Path:
    registry = storage_registry.load_storage_registry(repo.resolve())
    return storage_registry.resolve_database_path(registry, "source_graph")


def _build_base(repo: Path, files: dict[str, str]) -> Path:
    """Write ``files``, bootstrap identity, build a real base index, return db."""

    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    repository_state.bootstrap_repository(repo)
    source_graph.build_index(repo, incremental=False)
    return _base_db_path(repo)


def _changed(repo: Path, rels: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for rel in rels:
        candidate = repo / rel
        if candidate.exists():
            rows.append({"path": rel, "sha256": _sha256(candidate)})
        else:
            rows.append({"path": rel, "sha256": ""})
    return rows


# ---------------------------------------------------------------------------
# ONE: a partition is an index over a bounded scope, built independently
# ---------------------------------------------------------------------------

def test_partition_is_a_capability_built_over_scope_alone(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(
        repo,
        {
            "keep.py": "def keep_symbol():\n    return 1\n",
            "change.py": "def original_symbol():\n    return 2\n",
        },
    )
    # Change exactly one file on disk; the base index still holds the old row.
    (repo / "change.py").write_text(
        "def changed_symbol():\n    return 3\n", encoding="utf-8"
    )
    partition = tmp_path / "partition.sqlite"

    report = sgp.build_partition(
        repo, _changed(repo, ["change.py"]), partition, base_db_path=base_db,
    )

    assert report.files_indexed == 1
    assert report.scope == ("change.py",)
    assert report.entities >= 1
    assert partition.is_file()
    # The partition indexes the changed content, not the base's old row.
    view = sgp.ComposedView.bind(base_db, [partition])
    assert view.func("changed_symbol"), "partition symbol missing from composed view"


# ---------------------------------------------------------------------------
# TWO: preparing a view never copies the base; cost scales with changed files
# ---------------------------------------------------------------------------

def _make_large_base(repo: Path, file_count: int) -> Path:
    files = {
        f"pkg/mod_{i:04d}.py": (
            f"def symbol_{i:04d}():\n    return {i}\n\n"
            f"def helper_{i:04d}(x):\n    return x + {i}\n"
        )
        for i in range(file_count)
    }
    files["target.py"] = "def target_symbol():\n    return 0\n"
    return _build_base(repo, files)


def test_partition_cost_scales_with_changed_files_not_base_size(
    tmp_path: Path,
) -> None:
    small = tmp_path / "small"
    large = tmp_path / "large"
    small.mkdir()
    large.mkdir()
    small_db = _make_large_base(small, 20)
    large_db = _make_large_base(large, 200)
    # The base a clone would have copied is far bigger in the large repo.
    assert large_db.stat().st_size > small_db.stat().st_size * 3

    def prepare(repo: Path, base_db: Path) -> sgp.PartitionBuildReport:
        (repo / "target.py").write_text(
            "def target_symbol():\n    return 1\n", encoding="utf-8"
        )
        return sgp.build_partition(
            repo, _changed(repo, ["target.py"]),
            repo.parent / f"{repo.name}_part.sqlite", base_db_path=base_db,
        )

    small_report = prepare(small, small_db)
    large_report = prepare(large, large_db)

    # No full-index copy: the partition is smaller than the base a clone would
    # have written, in BOTH repos.
    assert small_report.bytes_written < small_db.stat().st_size
    assert large_report.bytes_written < large_db.stat().st_size
    # The SAME one-file change costs ~the same bytes regardless of base size:
    # the base grew >3x (asserted above) while the partition stayed <2x, so
    # preparation scales with the changed set, not the base index.
    assert large_report.bytes_written < small_report.bytes_written * 2
    # And the composed view answers a base-only symbol without the base ever
    # being copied into the partition.
    view = sgp.ComposedView.bind(large_db, [Path(large_report.partition_db_path)])
    assert view.func("symbol_0000"), "unchanged base symbol not visible via view"


# ---------------------------------------------------------------------------
# THREE (precedence): partition wins / absent -> base / deleted -> nothing
# ---------------------------------------------------------------------------

@pytest.fixture()
def precedence_view(tmp_path: Path) -> sgp.ComposedView:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(
        repo,
        {
            "keep.py": "def keep_symbol():\n    return 1\n",
            "change.py": "def original_symbol():\n    return 2\n",
            "gone.py": "def doomed_symbol():\n    return 3\n",
        },
    )
    (repo / "change.py").write_text(
        "def replacement_symbol():\n    return 9\n", encoding="utf-8"
    )
    (repo / "gone.py").unlink()
    partition = tmp_path / "partition.sqlite"
    sgp.build_partition(
        repo,
        _changed(repo, ["change.py", "gone.py"]),
        partition,
        base_db_path=base_db,
    )
    return sgp.ComposedView.bind(base_db, [partition])


def test_precedence_partition_wins_per_file(precedence_view: sgp.ComposedView) -> None:
    assert precedence_view.func("replacement_symbol"), "partition definition lost"
    # The base's old row for the changed file is invisible.
    assert not precedence_view.func("original_symbol"), "hidden base row leaked"


def test_precedence_absent_file_resolves_from_base(
    precedence_view: sgp.ComposedView,
) -> None:
    assert precedence_view.func("keep_symbol"), "unchanged base symbol missing"


def test_precedence_deleted_file_resolves_to_nothing(
    precedence_view: sgp.ComposedView,
) -> None:
    assert not precedence_view.func("doomed_symbol"), "deleted file still resolves"
    with precedence_view.open() as conn:
        gone = sgp.sg.context(conn, "gone.py")
    assert gone["found"] is False


# ---------------------------------------------------------------------------
# FOUR (cross-boundary), both directions + a NAMED limitation
# ---------------------------------------------------------------------------

def test_cross_boundary_changed_file_into_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(
        repo,
        {
            "lib.py": "def base_helper():\n    return 1\n",
            "feature.py": "def feature():\n    return 0\n",
        },
    )
    (repo / "feature.py").write_text(
        "from lib import base_helper\n\n"
        "def feature():\n    return base_helper()\n",
        encoding="utf-8",
    )
    partition = tmp_path / "partition.sqlite"
    sgp.build_partition(
        repo, _changed(repo, ["feature.py"]), partition, base_db_path=base_db,
    )
    view = sgp.ComposedView.bind(base_db, [partition])

    report = view.resolve_cross_boundary()
    directions = {
        (row["dst_name"], row["direction"]) for row in report["resolved"]
    }
    assert ("base_helper", "partition_to_base") in directions


def test_cross_boundary_unchanged_file_into_moved_definition(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(
        repo,
        {
            "oldhome.py": "def moved_fn():\n    return 1\n",
            "newhome.py": "def unrelated():\n    return 0\n",
            "caller.py": (
                "from oldhome import moved_fn\n\n"
                "def call_site():\n    return moved_fn()\n"
            ),
        },
    )
    # The definition MOVES from oldhome into newhome; caller.py is unchanged.
    (repo / "oldhome.py").write_text(
        "def something_else():\n    return 2\n", encoding="utf-8"
    )
    (repo / "newhome.py").write_text(
        "def unrelated():\n    return 0\n\n"
        "def moved_fn():\n    return 1\n",
        encoding="utf-8",
    )
    partition = tmp_path / "partition.sqlite"
    sgp.build_partition(
        repo,
        _changed(repo, ["oldhome.py", "newhome.py"]),
        partition,
        base_db_path=base_db,
    )
    view = sgp.ComposedView.bind(base_db, [partition])

    report = view.resolve_cross_boundary()
    resolved = {
        (row["dst_name"], row["direction"]) for row in report["resolved"]
    }
    # The unchanged caller's edge re-resolves to the partition definition, not
    # the stale base one.
    assert ("moved_fn", "base_to_partition") in resolved


def test_cross_boundary_ambiguous_case_is_named_not_guessed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(
        repo,
        {
            "dup_a.py": "def dup():\n    return 1\n",
            "dup_b.py": "def dup():\n    return 2\n",
            "feature.py": "def feature():\n    return 0\n",
        },
    )
    (repo / "feature.py").write_text(
        "def feature():\n    return dup()\n", encoding="utf-8"
    )
    partition = tmp_path / "partition.sqlite"
    sgp.build_partition(
        repo, _changed(repo, ["feature.py"]), partition, base_db_path=base_db,
    )
    view = sgp.ComposedView.bind(base_db, [partition])

    report = view.resolve_cross_boundary()
    ambiguous = {
        row["dst_name"]
        for row in report["unresolved"]
        if row["reason"] == "ambiguous"
    }
    assert "dup" in ambiguous, "ambiguous cross-boundary target must be named"
    # It must never be silently resolved to one of the two candidates.
    assert not any(
        row["dst_name"] == "dup" for row in report["resolved"]
    )


# ---------------------------------------------------------------------------
# FIVE: immutable, identity-bound, base cannot shift underneath an open view
# ---------------------------------------------------------------------------

def _identity_view(tmp_path: Path) -> tuple[sgp.ComposedView, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(repo, {"m.py": "def base_symbol():\n    return 1\n"})
    (repo / "m.py").write_text(
        "def changed_symbol():\n    return 2\n", encoding="utf-8"
    )
    partition = tmp_path / "partition.sqlite"
    sgp.build_partition(
        repo, _changed(repo, ["m.py"]), partition, base_db_path=base_db,
    )
    view = sgp.ComposedView.bind(
        base_db,
        [partition],
        packet_sha256="packet-a",
        target_request_id="req-a",
        target_task_id="task-a",
    )
    return view, base_db


def test_view_is_frozen_and_binding_is_verifiable(tmp_path: Path) -> None:
    view, _base = _identity_view(tmp_path)
    # Immutable value.
    with pytest.raises((AttributeError, TypeError)):
        view.packet_sha256 = "other"  # type: ignore[misc]
    # Exact-match binding passes.
    view.verify_binding("packet-a", "req-a", "task-a")


def test_reviewer_refuses_view_whose_binding_mismatches_packet(
    tmp_path: Path,
) -> None:
    view, _base = _identity_view(tmp_path)
    with pytest.raises(sgp.PartitionBindingError):
        view.verify_binding("packet-b", "req-a", "task-a")


def test_base_cannot_shift_underneath_an_open_view(tmp_path: Path) -> None:
    view, base_db = _identity_view(tmp_path)
    # A valid open works.
    with view.open() as conn:
        assert sgp.is_composed(conn)
    # Mutating the base file changes its fingerprint; the view fails closed.
    with base_db.open("ab") as handle:
        handle.write(b"\x00")
    with pytest.raises(sgp.PartitionBaseShiftError):
        view.assert_base_unshifted()
    with pytest.raises(sgp.PartitionBaseShiftError):
        with view.open():
            pass


# ---------------------------------------------------------------------------
# The composed view answers as ONE index (find spans both sides)
# ---------------------------------------------------------------------------

def test_composed_find_spans_partition_and_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(
        repo,
        {
            "keep.py": "def unchanged_beacon():\n    return 1\n",
            "change.py": "def old_beacon():\n    return 2\n",
        },
    )
    (repo / "change.py").write_text(
        "def new_beacon():\n    return 3\n", encoding="utf-8"
    )
    partition = tmp_path / "partition.sqlite"
    sgp.build_partition(
        repo, _changed(repo, ["change.py"]), partition, base_db_path=base_db,
    )
    view = sgp.ComposedView.bind(base_db, [partition])

    with view.open() as conn:
        unchanged = {row["name"] for row in source_graph.find(conn, "unchanged_beacon")}
        new = {row["name"] for row in source_graph.find(conn, "new_beacon")}
        old = {row["name"] for row in source_graph.find(conn, "old_beacon")}
    assert "unchanged_beacon" in unchanged  # from base
    assert "new_beacon" in new  # from partition
    assert "old_beacon" not in old  # hidden base row for the changed file


# ---------------------------------------------------------------------------
# Path safety: a symlink changed path is refused (guard fires BEFORE resolve)
# ---------------------------------------------------------------------------

def test_build_partition_refuses_symlink_changed_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(repo, {"real.py": "def real_symbol():\n    return 1\n"})
    # An in-repo symlink named like a source file. ``resolve()`` canonicalizes
    # it to its (non-link) target, so the guard MUST test the unresolved path;
    # a post-resolve ``is_symlink`` would silently index the link's target.
    link = repo / "link.py"
    link.symlink_to(repo / "real.py")
    partition = tmp_path / "partition.sqlite"
    with pytest.raises(sgp.PartitionError) as excinfo:
        sgp.build_partition(
            repo,
            [{"path": "link.py", "sha256": _sha256(repo / "real.py")}],
            partition,
            base_db_path=base_db,
        )
    assert "quality_review_candidate_path_symlink" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Admission is routed through the SAME repository policy as the base index
# ---------------------------------------------------------------------------

def test_admission_routes_through_repository_policy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(
        repo,
        {
            "keep.py": "def keep_symbol():\n    return 1\n",
            "generated/gen.py": "def generated_symbol():\n    return 2\n",
        },
    )
    # Teach the repository policy to exclude ``generated/**`` -- the SAME rule
    # the base's ``iter_source_files`` obeys. A changed path under it must be
    # skipped by the partition, not admitted just because the packet named it.
    config = source_graph.ignore_config_path(repo)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "schema_id": source_graph.POLICY_SCHEMA_ID,
                "revision": 1,
                "exclude_dirs": [],
                "exclude_globs": ["generated/**"],
                "disabled_languages": [],
            }
        ),
        encoding="utf-8",
    )
    (repo / "generated" / "gen.py").write_text(
        "def changed_generated():\n    return 9\n", encoding="utf-8"
    )
    partition = tmp_path / "partition.sqlite"
    report = sgp.build_partition(
        repo,
        _changed(repo, ["generated/gen.py"]),
        partition,
        base_db_path=base_db,
    )
    assert report.files_indexed == 0, "excluded changed path was indexed"
    assert report.files_skipped == 1
    # It stays in scope (so any base row for it is hidden) but carries no row.
    assert report.scope == ("generated/gen.py",)
    view = sgp.ComposedView.bind(base_db, [partition])
    assert not view.func("changed_generated"), "excluded path leaked into view"


def test_disjoint_scope_required_across_partitions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(repo, {"m.py": "def s():\n    return 1\n"})
    (repo / "m.py").write_text("def s2():\n    return 2\n", encoding="utf-8")
    p1 = tmp_path / "p1.sqlite"
    p2 = tmp_path / "p2.sqlite"
    sgp.build_partition(repo, _changed(repo, ["m.py"]), p1, base_db_path=base_db)
    sgp.build_partition(repo, _changed(repo, ["m.py"]), p2, base_db_path=base_db)
    with pytest.raises(sgp.PartitionError):
        sgp.ComposedView.bind(base_db, [p1, p2])


# ---------------------------------------------------------------------------
# NF-2026-00946: the base generation is pinned for the overlay's lifetime
# ---------------------------------------------------------------------------

def _publish(repo: Path, files: dict[str, str]) -> None:
    """Ordinary canonical publication: a real full rebuild of the base."""

    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    source_graph.build_index(repo, incremental=False)


def _pins(directory: Path) -> list[Path]:
    return sorted(directory.glob(".sg-base-pin.*.sqlite"))


def _pinned_partition(tmp_path: Path) -> tuple[Path, Path, Path, os.stat_result]:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(
        repo,
        {
            "keep.py": "def keep_symbol():\n    return 1\n",
            "change.py": "def original_symbol():\n    return 2\n",
        },
    )
    before = os.stat(base_db)
    (repo / "change.py").write_text(
        "def changed_symbol():\n    return 3\n", encoding="utf-8"
    )
    partition_dir = tmp_path / "overlay"
    partition_dir.mkdir()
    partition = partition_dir / "partition.sqlite"
    report = sgp.build_partition(
        repo, _changed(repo, ["change.py"]), partition, base_db_path=base_db,
    )
    assert report.base_pin == "pinned"
    return repo, base_db, partition, before


def _names(partition: Path, table: str, column: str) -> set[str]:
    conn = source_graph.connect(partition, read_only=True)
    try:
        return {str(row[0]) for row in conn.execute(f"SELECT {column} FROM {table}")}
    finally:
        conn.close()


def test_pinned_base_survives_atomic_canonical_publication(tmp_path: Path) -> None:
    repo, base_db, partition, before = _pinned_partition(tmp_path)
    (pin,) = _pins(partition.parent)
    # The pin IS the base generation's inode: a hard link, never a copy.
    assert (os.stat(pin).st_ino, os.stat(pin).st_dev) == (before.st_ino, before.st_dev)

    _publish(repo, {"keep.py": "def keep_symbol():\n    return 1\n\n"
                               "def published_later():\n    return 4\n"})
    after = os.stat(base_db)
    assert after.st_ino != before.st_ino, "publication did not replace the base"

    names = _names(partition, "entities", "name")
    assert "changed_symbol" in names  # from the partition
    assert "keep_symbol" in names  # unchanged base, from the pinned generation
    assert "original_symbol" not in names  # hidden base row for the changed file
    assert "published_later" not in names  # newer generation never leaks in


@pytest.mark.parametrize(
    "tamper, reason",
    [
        ("pin_missing", "composed_base_pin_missing"),
        ("pin_replaced", "composed_base_pin_shifted"),
        ("pin_mutated", "composed_base_pin_shifted"),
        ("pin_symlink", "composed_base_pin_not_file"),
        ("marker_escape", "composed_base_pin_invalid"),
        ("marker_forged_generation", "composed_base_pin_invalid"),
        ("marker_forged_schema", "composed_base_pin_invalid:schema"),
    ],
)
def test_pinned_base_tampering_fails_closed(
    tmp_path: Path, tamper: str, reason: str
) -> None:
    _repo, _base_db, partition, _before = _pinned_partition(tmp_path)
    (pin,) = _pins(partition.parent)
    if tamper == "pin_missing":
        pin.unlink()
    elif tamper == "pin_replaced":
        replacement = pin.with_name("replacement.bin")
        replacement.write_bytes(pin.read_bytes())
        os.replace(replacement, pin)
    elif tamper == "pin_mutated":
        with pin.open("ab") as handle:
            handle.write(b"\x00")
    elif tamper == "pin_symlink":
        target = pin.with_name("elsewhere.bin")
        pin.rename(target)
        pin.symlink_to(target)
    else:
        marker = sgp.read_composed_marker(partition)
        assert marker is not None
        if tamper == "marker_escape":
            marker["base"]["pin"]["name"] = "../" + pin.name
        elif tamper == "marker_forged_generation":
            marker["base"]["ino"] = int(marker["base"]["ino"]) + 1
        else:
            marker["base"]["pin"]["schema_id"] = "forged"
        conn = sqlite3.connect(partition)
        try:
            conn.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (json.dumps(marker), sgp.COMPOSED_VIEW_META_KEY),
            )
            conn.commit()
        finally:
            conn.close()
    with pytest.raises(sgp.PartitionBasePinError, match=reason):
        source_graph.connect(partition, read_only=True)


def test_pin_refuses_symlinked_missing_or_unverified_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(repo, {"m.py": "def s():\n    return 1\n"})
    changed = _changed(repo, ["m.py"])
    link = tmp_path / "base_link.sqlite"
    link.symlink_to(base_db)
    with pytest.raises(sgp.PartitionBasePinError, match="composed_base_symlink"):
        sgp.build_partition(repo, changed, tmp_path / "a.sqlite", base_db_path=link)
    with pytest.raises(sgp.PartitionBaseShiftError, match="composed_base_missing"):
        sgp.build_partition(
            repo, changed, tmp_path / "b.sqlite",
            base_db_path=tmp_path / "absent.sqlite",
        )
    conn = sqlite3.connect(base_db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='last_build'").fetchone()
        payload = json.loads(row[0])
        payload["build_revision"] = "aiworkhub.source_graph.forged.v0"
        conn.execute(
            "UPDATE meta SET value=? WHERE key='last_build'", (json.dumps(payload),)
        )
        conn.commit()
    finally:
        conn.close()
    forged_dir = tmp_path / "forged"
    forged_dir.mkdir()
    with pytest.raises(sgp.PartitionBasePinError, match="composed_base_pin_wrong_revision"):
        sgp.build_partition(
            repo, changed, forged_dir / "c.sqlite", base_db_path=base_db,
        )
    assert _pins(forged_dir) == [], "an unverified generation stayed pinned"


def test_pin_refuses_generation_republished_during_link(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(repo, {"m.py": "def s():\n    return 1\n"})
    (repo / "m.py").write_text("def s2():\n    return 2\n", encoding="utf-8")
    real_link = os.link
    newer = tmp_path / "newer.sqlite"
    newer.write_bytes(base_db.read_bytes() + b"\x00" * 512)

    def racing_link(src, dst, *args, **kwargs):
        # The canonical path is atomically republished between the generation
        # stat and the link: the link now names an unverified generation.
        os.replace(newer, base_db)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(sgp.os, "link", racing_link)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    with pytest.raises(sgp.PartitionBasePinError, match="composed_base_pin_raced"):
        sgp.build_partition(
            repo, _changed(repo, ["m.py"]), overlay / "p.sqlite", base_db_path=base_db,
        )
    assert _pins(overlay) == []


def test_pin_unsupported_filesystem_is_recorded_and_fails_explicitly(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(repo, {"keep.py": "def keep_symbol():\n    return 1\n",
                                 "m.py": "def s():\n    return 1\n"})
    (repo / "m.py").write_text("def s2():\n    return 2\n", encoding="utf-8")

    def cross_device_link(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(sgp.os, "link", cross_device_link)
    partition = tmp_path / "p.sqlite"
    report = sgp.build_partition(
        repo, _changed(repo, ["m.py"]), partition, base_db_path=base_db,
    )
    assert report.base_pin == "unsupported:EXDEV"
    assert _pins(tmp_path) == []
    # Still composes while the canonical generation is unchanged ...
    assert "keep_symbol" in _names(partition, "entities", "name")
    # ... and names the missing pin when publication shifts the base.
    _publish(repo, {"keep.py": "def keep_symbol():\n    return 5\n"})
    with pytest.raises(
        sgp.PartitionBasePinError, match="composed_base_shifted_unpinned:EXDEV"
    ):
        source_graph.connect(partition, read_only=True)


def test_pin_retention_is_bounded_by_live_partitions(tmp_path: Path) -> None:
    repo, base_db, partition, _before = _pinned_partition(tmp_path)
    overlay = partition.parent
    sibling = overlay / "sibling.sqlite"
    sgp.build_partition(
        repo, _changed(repo, ["change.py"]), sibling, base_db_path=base_db,
    )
    (first_pin,) = _pins(overlay)  # one shared pin per generation

    _publish(repo, {"keep.py": "def keep_symbol():\n    return 6\n"})
    # A rebuild publishes through temp + atomic replace, exactly as callers do.
    temporary = overlay / ".partition.sqlite.abc.tmp"
    sgp.build_partition(
        repo, _changed(repo, ["change.py"]), temporary, base_db_path=base_db,
    )
    os.replace(temporary, partition)
    pins = _pins(overlay)
    # The sibling still references the first generation, so it is retained.
    assert first_pin in pins and len(pins) == 2

    sibling.unlink()
    _publish(repo, {"keep.py": "def keep_symbol():\n    return 7\n"})
    temporary = overlay / ".partition.sqlite.def.tmp"
    sgp.build_partition(
        repo, _changed(repo, ["change.py"]), temporary, base_db_path=base_db,
    )
    os.replace(temporary, partition)
    pins = _pins(overlay)
    # The unreferenced first generation is gone; the one the partition named
    # until its replace is retained at most until the next marker write.
    assert first_pin not in pins and len(pins) == 2
    sgp.build_partition(
        repo, _changed(repo, ["change.py"]), sibling, base_db_path=base_db,
    )
    # Only the generation a live partition references survives.
    (only_pin,) = _pins(overlay)
    assert os.stat(only_pin).st_ino == os.stat(base_db).st_ino
    assert "keep_symbol" in _names(partition, "entities", "name")


def test_pin_setup_cost_is_constant_not_base_sized(tmp_path: Path, capsys) -> None:
    measurements: dict[str, dict[str, float]] = {}
    for label, count in (("small", 20), ("large", 200)):
        repo = tmp_path / label
        repo.mkdir()
        base_db = _make_large_base(repo, count)
        (repo / "target.py").write_text(
            "def target_symbol():\n    return 1\n", encoding="utf-8"
        )
        overlay = tmp_path / f"{label}_overlay"
        overlay.mkdir()
        report = sgp.build_partition(
            repo, _changed(repo, ["target.py"]), overlay / "p.sqlite",
            base_db_path=base_db,
        )
        (pin,) = _pins(overlay)
        # No copy: the pin shares the base inode, so it adds no bytes.
        assert os.stat(pin).st_ino == os.stat(base_db).st_ino
        assert os.stat(base_db).st_nlink >= 2
        measurements[label] = {
            "base_bytes": os.stat(base_db).st_size,
            "partition_bytes": report.bytes_written,
            "pin_seconds": round(report.pin_seconds, 5),
        }
    assert measurements["large"]["base_bytes"] > measurements["small"]["base_bytes"] * 3
    # Generous bound: a link + stat + metadata check, never proportional work.
    assert measurements["large"]["pin_seconds"] < 1.0
    print("BASE_PIN_COST " + json.dumps(measurements, sort_keys=True))
    assert "BASE_PIN_COST" in capsys.readouterr().out
