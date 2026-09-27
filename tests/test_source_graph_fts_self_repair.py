"""Source Graph self-repairs a corrupt FTS5 inverted index (NF-2026-01017).

Measured 2026-09-26 on the 315 MB canonical index: ``PRAGMA quick_check``
returned exactly one finding, ``malformed inverted index for FTS5 table
main.entities_fts``, while the b-tree pages and all 65504 stored ``entities``
rows were intact.  Every refresh job then died with ``database disk image is
malformed`` in the incremental FTS delete, so the index went stale, untargeted
focus queries returned the raw sqlite text, and health still reported a
readable generation.

Covers: an incremental refresh over a damaged inverted index succeeds after ONE
automatic rebuild without re-parsing the repository; the publication probe
repairs the same damage when nothing changed on disk; corruption quick_check
does not localize to an FTS5 table is REFUSED rather than "repaired", marked
for the existing full-rebuild path and reported as a typed hard failure in
health; a successful self-repair is recorded as a repair event; a failed
rebuild and a failed retry each stop instead of looping; queries answer with a
typed corruption payload instead of raw sqlite text and never repair anything;
and the repair introduces no lock file and no second quarantine mechanism.

Run: python3 -m pytest -q tests/test_source_graph_fts_self_repair.py
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
from aiworkhub import source_graph_daemon as sgd  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402


# fts5 keeps the segment structure record of ``entities_fts`` at this reserved
# rowid of its ``entities_fts_data`` shadow table, and holds every segment
# b-tree page of the inverted index at a rowid above it.  The damage has to go
# in the pages, not the structure record: xConnect decodes the structure, so a
# structure that cannot be decoded makes the table unopenable and even ``PRAGMA
# quick_check`` fails to construct the vtab, short-circuiting the repair path
# under test.  Truncating the pages reproduces what NF-2026-01017 measured --
# every read of the inverted index returns SQLITE_CORRUPT, while the structure
# record, the b-tree pages, the ``entities`` rows and every
# ``entities_fts_content`` row the rebuild reads back stay intact and readable.
_FTS5_STRUCTURE_ROWID = 10
# fts5 treats any segment page shorter than its four-byte header as corrupt, so
# this is the smallest damage every page read is guaranteed to notice.
_TRUNCATED_SEGMENT_PAGE = b"\x00"
_FTS5_FINDING = "malformed inverted index for FTS5 table"
_PAGE_FINDING = "*** in database main *** Page 42: btreeInitPage() returns error code 11"


@pytest.fixture(autouse=True)
def _stop_daemons():
    yield
    sgd.stop_all_daemons()


def _repo_with_index(tmp_path: Path, name: str) -> Path:
    """A tmp_path repository with a small, real, published generation.

    Never the live canonical index: every test here damages the database it
    builds, so it must own it outright.
    """
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    (root / "alpha.py").write_text(
        "def alpha_marker():\n    return 'alpha payload'\n", encoding="utf-8"
    )
    (root / "beta.py").write_text(
        "def beta_marker():\n    return 'beta payload'\n", encoding="utf-8"
    )
    report = sg.build_index(root, incremental=False)
    assert report.files_seen >= 2, report
    return root


def _quick_check(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    finally:
        conn.close()


def _damage_entities_fts(db_path: Path) -> None:
    """Corrupt ONLY the entities_fts inverted index, and prove that is all."""
    conn = sqlite3.connect(str(db_path))
    try:
        pages = [
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM entities_fts_data WHERE id > ? ORDER BY id",
                (_FTS5_STRUCTURE_ROWID,),
            )
        ]
        assert pages, "the published generation stored no fts5 segment pages to damage"
        conn.executemany(
            "UPDATE entities_fts_data SET block=? WHERE id=?",
            [(_TRUNCATED_SEGMENT_PAGE, page) for page in pages],
        )
        conn.commit()
        # The structure record is untouched, so the table still opens and
        # quick_check still reaches the inverted index it describes.
        structure = conn.execute(
            "SELECT COUNT(*) FROM entities_fts_data WHERE id=?",
            (_FTS5_STRUCTURE_ROWID,),
        ).fetchone()[0]
        assert structure == 1, "fts5 structure record was not at its reserved rowid"
        assert conn.execute("SELECT COUNT(*) FROM entities_fts_content").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] > 0
    finally:
        conn.close()
    findings = _quick_check(db_path)
    assert len(findings) == 1, findings
    assert findings[0].startswith(_FTS5_FINDING), findings
    assert "entities_fts" in findings[0], findings


def _malformed_image() -> sqlite3.DatabaseError:
    return sqlite3.DatabaseError("database disk image is malformed")


def _refuse_rebuild(_conn, _table):
    pytest.fail("an FTS5 rebuild ran where the contract forbids one")


# ---------------------------------------------------------------------------
# 1. The measured failure: an incremental refresh repairs and completes
# ---------------------------------------------------------------------------

def test_incremental_refresh_self_repairs_the_fts_inverted_index(tmp_path, monkeypatch):
    root = _repo_with_index(tmp_path, "self_repair")
    canonical = sg.resolve_db_path(root)
    _damage_entities_fts(canonical)

    rebuilds: list[tuple[str, str]] = []
    real_rebuild = sg.rebuild_fts_index

    def counted_rebuild(conn, table):
        main = [
            str(row[2]) for row in conn.execute("PRAGMA database_list")
            if str(row[1]) == "main" and str(row[2] or "")
        ]
        rebuilds.append((table, Path(main[0]).name if main else ""))
        return real_rebuild(conn, table)

    monkeypatch.setattr(sg, "rebuild_fts_index", counted_rebuild)
    (root / "alpha.py").write_text(
        "def alpha_renamed():\n    return 'alpha payload'\n", encoding="utf-8"
    )

    report = sg.build_index(root)

    # Exactly one rebuild, of the one table quick_check named, and it ran on
    # the private staged candidate -- which exists only inside the section that
    # already owns the repository writer lease.
    assert [table for table, _ in rebuilds] == ["main.entities_fts"]
    assert rebuilds[0][1].startswith(f".{canonical.name}.building-"), rebuilds

    # A damaged inverted index never costs a whole-repository re-parse.
    assert report.incremental is True
    assert report.files_changed == 1, report
    assert report.files_unchanged >= 1, report

    assert _quick_check(canonical) == ["ok"]
    assert sg.focus(root, "alpha_renamed", budget=8)["matches"]
    assert sg.focus(root, "beta_marker", budget=8)["matches"]


def test_a_no_change_refresh_repairs_the_candidate_at_the_publication_probe(
    tmp_path, monkeypatch
):
    """The other site NF-2026-01017 named: the probe MATCH in probe_generation."""
    root = _repo_with_index(tmp_path, "probe_repair")
    canonical = sg.resolve_db_path(root)
    _damage_entities_fts(canonical)

    rebuilds: list[str] = []
    real_rebuild = sg.rebuild_fts_index

    def counted_rebuild(conn, table):
        rebuilds.append(table)
        return real_rebuild(conn, table)

    monkeypatch.setattr(sg, "rebuild_fts_index", counted_rebuild)

    # Nothing changed on disk, so no file is invalidated and the damage is
    # first seen by the publication probe -- still under the writer lease.
    report = sg.build_index(root)

    assert report.files_changed == 0, report
    assert rebuilds == ["main.entities_fts"]
    assert _quick_check(canonical) == ["ok"]
    assert sg.focus(root, "alpha_marker", budget=8)["matches"]


# ---------------------------------------------------------------------------
# 2. Health: a repair is an event, corruption is a typed hard failure
# ---------------------------------------------------------------------------

def test_health_reports_a_successful_self_repair_as_a_repair_event(tmp_path):
    root = _repo_with_index(tmp_path, "repair_event")
    canonical = sg.resolve_db_path(root)
    _damage_entities_fts(canonical)
    (root / "beta.py").write_text(
        "def beta_renamed():\n    return 'beta payload'\n", encoding="utf-8"
    )

    sg.build_index(root)

    health = sgd.daemon_health(root)
    integrity = health["integrity"]
    assert integrity["schema_id"] == sg.INTEGRITY_SCHEMA_ID
    assert integrity["status"] == sg.INTEGRITY_STATUS_REPAIRED
    assert integrity["fts_only"] is True

    repair = health["last_repair"]
    assert repair["table"] == "main.entities_fts"
    assert repair["tables"] == ["main.entities_fts"]
    assert any(_FTS5_FINDING in finding for finding in repair["findings"])
    assert isinstance(repair["duration_seconds"], float)
    assert repair["duration_seconds"] >= 0.0
    assert repair["repaired_at"]

    # A repaired index is readable again: the repair event is not a failure.
    assert health["status"] != sgd.STATUS_CORRUPT
    assert health["ok"] is True


def test_non_fts_corruption_is_refused_and_reported_as_a_typed_hard_failure(
    tmp_path, monkeypatch
):
    root = _repo_with_index(tmp_path, "non_fts")
    canonical = sg.resolve_db_path(root)
    monkeypatch.setattr(sg, "quick_check_findings", lambda conn: [_PAGE_FINDING])
    monkeypatch.setattr(sg, "rebuild_fts_index", _refuse_rebuild)
    attempts: list[str] = []

    def malformed_write():
        attempts.append("ran")
        raise _malformed_image()

    conn = sg.connect(canonical)
    try:
        with pytest.raises(sg.SourceGraphCorruptIndexError) as caught:
            sg.write_with_fts_self_repair(
                conn, malformed_write, operation_name="unit_probe"
            )
    finally:
        conn.close()

    # No retry loop: the failing operation ran exactly once.
    assert attempts == ["ran"]
    error = caught.value.to_json()
    assert error["reason"] == "source_graph_index_corrupt"
    assert error["fts_only"] is False
    assert error["fts_tables"] == []
    assert error["repair_state"] == sg.INTEGRITY_REPAIR_REFUSED_NON_FTS
    assert error["findings"] == [_PAGE_FINDING]
    assert "malformed" in error["detail"]

    # Marked for the existing full-rebuild path, and health says so.
    assert sg.generation_marked_corrupt(canonical) is True
    health = sgd.daemon_health(root)
    assert health["ok"] is False
    assert health["status"] == sgd.STATUS_CORRUPT
    assert health["readable_generation"] is False
    assert health["refreshable"] is False
    assert health["last_error"].startswith("source_graph_index_corrupt:")
    findings = health["integrity"]["findings"]
    assert 0 < len(findings) <= sg.MAX_INTEGRITY_FINDINGS
    assert all(len(finding) <= sg.MAX_INTEGRITY_FINDING_CHARS for finding in findings)


def test_health_bounds_a_long_and_repetitive_quick_check_verdict(tmp_path):
    root = _repo_with_index(tmp_path, "bounded_findings")
    canonical = sg.resolve_db_path(root)
    sg.record_generation_corrupt(
        canonical,
        operation="unit_probe",
        findings=[f"{index}:{_PAGE_FINDING * 20}" for index in range(64)],
        detail="x" * 4096,
    )

    integrity = sgd.daemon_health(root)["integrity"]
    assert len(integrity["findings"]) == sg.MAX_INTEGRITY_FINDINGS
    assert all(
        len(finding) == sg.MAX_INTEGRITY_FINDING_CHARS
        for finding in integrity["findings"]
    )
    assert len(integrity["detail"]) == sg.MAX_INTEGRITY_FINDING_CHARS


# ---------------------------------------------------------------------------
# 3. Replacement reuses the existing staged full rebuild, not a new mechanism
# ---------------------------------------------------------------------------

def test_a_generation_marked_corrupt_is_replaced_by_a_full_rebuild(tmp_path):
    root = _repo_with_index(tmp_path, "full_rebuild")
    canonical = sg.resolve_db_path(root)
    sg.record_generation_corrupt(
        canonical, operation="unit_probe", findings=[_PAGE_FINDING]
    )
    assert sg.generation_marked_corrupt(canonical) is True
    assert sgd.SourceGraphDaemon(root)._has_prior_build() is False

    # The caller still asks for an incremental refresh; the marker is what
    # routes it through the existing staged full rebuild + atomic publication.
    report = sg.build_index(root)

    assert report.incremental is False
    assert report.files_seen >= 2, report
    assert sg.generation_marked_corrupt(canonical) is False
    assert sg.read_integrity_state(canonical)["status"] == sg.INTEGRITY_STATUS_OK
    assert sg.focus(root, "alpha_marker", budget=8)["matches"]


def test_self_repair_adds_no_lock_file_and_no_second_quarantine(tmp_path):
    root = _repo_with_index(tmp_path, "no_new_locks")
    canonical = sg.resolve_db_path(root)
    before = sorted(entry.name for entry in canonical.parent.iterdir())
    assert "index.lock" in before, before

    _damage_entities_fts(canonical)
    (root / "alpha.py").write_text(
        "def alpha_again():\n    return 'alpha payload'\n", encoding="utf-8"
    )
    sg.build_index(root)

    after = sorted(entry.name for entry in canonical.parent.iterdir())
    # The only artefact a repair leaves behind is the integrity verdict health
    # reads; the writer lease file is the pre-existing one it ran under.
    assert [name for name in after if name not in before] == [
        sg.integrity_state_path(canonical).name
    ]
    assert [name for name in after if "quarantine" in name.lower()] == []


# ---------------------------------------------------------------------------
# 4. Queries: typed corruption, never raw sqlite text, never a repair
# ---------------------------------------------------------------------------

def test_queries_answer_corruption_with_a_typed_payload_and_never_repair(
    tmp_path, monkeypatch
):
    root = _repo_with_index(tmp_path, "typed_query")
    canonical = sg.resolve_db_path(root)
    _damage_entities_fts(canonical)
    monkeypatch.setattr(sg, "rebuild_fts_index", _refuse_rebuild)

    payload = sg.focus(root, "alpha_marker", budget=8)

    assert payload["ok"] is False
    assert payload["matches"] == []
    error = payload["error"]
    assert error["reason"] == "source_graph_index_corrupt"
    assert error["schema_id"] == sg.INTEGRITY_SCHEMA_ID
    assert error["fts_only"] is True
    assert error["fts_tables"] == ["main.entities_fts"]
    assert error["repair_state"] == sg.INTEGRITY_REPAIR_NOT_ATTEMPTED
    assert any(_FTS5_FINDING in finding for finding in error["findings"])
    # The raw sqlite text stays available as a bounded detail, never as the
    # whole answer.
    assert "malformed" in error["detail"]

    conn = sg.connect(canonical, read_only=True)
    try:
        with pytest.raises(sg.SourceGraphCorruptIndexError):
            sg.find(conn, "alpha_marker")
    finally:
        conn.close()

    # A reader repaired nothing: the damage is still there for the writer.
    assert _quick_check(canonical)[0].startswith(_FTS5_FINDING)


def test_an_ordinary_fts_error_still_falls_back_to_like(tmp_path):
    """The malformed-image branch must not swallow the existing LIKE fallback."""
    root = _repo_with_index(tmp_path, "like_fallback")
    real = sg.connect(sg.resolve_db_path(root), read_only=True)

    class _FtsRefusingConnection:
        """Fails only the FTS MATCH pass, with a plain non-corruption error."""

        def execute(self, statement, *args):
            if "entities_fts MATCH" in statement:
                raise sqlite3.OperationalError('fts5: syntax error near "*"')
            return real.execute(statement, *args)

        def __getattr__(self, name):
            return getattr(real, name)

    try:
        rows = sg.find(_FtsRefusingConnection(), "alpha_marker")
    finally:
        real.close()

    assert [row["name"] for row in rows] == ["alpha_marker"]


# ---------------------------------------------------------------------------
# 5. The repair is bounded: one rebuild, one retry, then stop
# ---------------------------------------------------------------------------

def test_a_failed_rebuild_is_not_retried_and_marks_the_generation(tmp_path, monkeypatch):
    root = _repo_with_index(tmp_path, "rebuild_fails")
    canonical = sg.resolve_db_path(root)
    monkeypatch.setattr(
        sg, "quick_check_findings",
        lambda conn: [f"{_FTS5_FINDING} main.entities_fts"],
    )

    def failing_rebuild(_conn, _table):
        raise _malformed_image()

    monkeypatch.setattr(sg, "rebuild_fts_index", failing_rebuild)
    attempts: list[str] = []

    def malformed_write():
        attempts.append("ran")
        raise _malformed_image()

    conn = sg.connect(canonical)
    try:
        with pytest.raises(sg.SourceGraphCorruptIndexError) as caught:
            sg.write_with_fts_self_repair(
                conn, malformed_write, operation_name="unit_probe"
            )
    finally:
        conn.close()

    assert attempts == ["ran"]
    assert caught.value.repair_state == sg.INTEGRITY_REPAIR_FAILED
    assert caught.value.fts_only is True
    assert sg.generation_marked_corrupt(canonical) is True
    assert sgd.daemon_health(root)["status"] == sgd.STATUS_CORRUPT


def test_a_retry_that_still_fails_is_not_looped(tmp_path, monkeypatch):
    root = _repo_with_index(tmp_path, "retry_fails")
    canonical = sg.resolve_db_path(root)
    monkeypatch.setattr(
        sg, "quick_check_findings",
        lambda conn: [f"{_FTS5_FINDING} main.entities_fts"],
    )
    rebuilds: list[str] = []
    monkeypatch.setattr(
        sg, "rebuild_fts_index", lambda _conn, table: rebuilds.append(table)
    )
    attempts: list[str] = []

    def always_malformed():
        attempts.append("ran")
        raise _malformed_image()

    conn = sg.connect(canonical)
    try:
        with pytest.raises(sg.SourceGraphCorruptIndexError) as caught:
            sg.write_with_fts_self_repair(
                conn, always_malformed, operation_name="unit_probe"
            )
    finally:
        conn.close()

    # One rebuild, one retry, then stop: the failure never becomes a loop.
    assert rebuilds == ["main.entities_fts"]
    assert attempts == ["ran", "ran"]
    assert caught.value.repair_state == sg.INTEGRITY_RETRY_FAILED
    assert sg.generation_marked_corrupt(canonical) is True


def test_a_quick_check_that_cannot_run_is_not_evidence_of_fts_damage(
    tmp_path, monkeypatch
):
    root = _repo_with_index(tmp_path, "quick_check_unavailable")
    canonical = sg.resolve_db_path(root)

    def exploding_quick_check(_conn):
        raise _malformed_image()

    monkeypatch.setattr(sg, "quick_check_findings", exploding_quick_check)
    monkeypatch.setattr(sg, "rebuild_fts_index", _refuse_rebuild)

    conn = sg.connect(canonical)
    try:
        with pytest.raises(sg.SourceGraphCorruptIndexError) as caught:
            sg.write_with_fts_self_repair(
                conn, _malformed_raise, operation_name="unit_probe"
            )
    finally:
        conn.close()

    assert caught.value.repair_state == sg.INTEGRITY_REPAIR_REFUSED_NON_FTS
    assert caught.value.fts_only is False
    assert any(
        finding.startswith("quick_check_unavailable:")
        for finding in caught.value.findings
    )
    assert sg.generation_marked_corrupt(canonical) is True


def _malformed_raise():
    raise _malformed_image()


# ---------------------------------------------------------------------------
# 6. Classification: what counts as corruption, and what counts as FTS-only
# ---------------------------------------------------------------------------

def test_only_corruption_is_classified_as_a_malformed_image():
    assert sg.is_malformed_database_error(_malformed_image()) is True
    assert sg.is_malformed_database_error(
        sqlite3.OperationalError("fts5: corrupt structure record")
    ) is True
    assert sg.is_malformed_database_error(
        sqlite3.DatabaseError(f"{_FTS5_FINDING} main.entities_fts")
    ) is True
    assert sg.is_malformed_database_error(
        sqlite3.OperationalError("no such table: entities_fts")
    ) is False
    assert sg.is_malformed_database_error(
        ValueError("malformed inverted index")
    ) is False


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        ([], None),
        ([f"{_FTS5_FINDING} main.entities_fts"], ("main.entities_fts",)),
        ([f"{_FTS5_FINDING} entities_fts"], ("entities_fts",)),
        (
            [f"{_FTS5_FINDING} main.entities_fts"] * 3,
            ("main.entities_fts",),
        ),
        ([f"{_FTS5_FINDING} main.entities_fts", _PAGE_FINDING], None),
        ([_PAGE_FINDING, f"{_FTS5_FINDING} main.entities_fts"], None),
        (["quick_check_unavailable:DatabaseError:database disk image is malformed"], None),
    ],
)
def test_only_a_wholly_fts_localized_verdict_is_repairable(findings, expected):
    assert sg.fts5_tables_in_findings(findings) == expected


def test_the_fts_command_target_quotes_the_table_it_was_given():
    assert sg._fts5_command_names("main.entities_fts") == (
        '"main"."entities_fts"', '"entities_fts"'
    )
    assert sg._fts5_command_names("entities_fts") == ('"entities_fts"', '"entities_fts"')
    with pytest.raises(sg.SourceGraphError, match="fts_table_name_unsafe"):
        sg._fts5_command_names('entities_fts"; DROP TABLE entities; --')


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
