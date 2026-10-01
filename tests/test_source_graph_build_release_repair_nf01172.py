"""A malformed FTS5 image anywhere in the build write path is repaired or typed.

NF-2026-01172, measured on the canonical index: quick_check reported only
``malformed inverted index for FTS5 table main.entities_fts``, yet every
incremental build died with a raw ``database disk image is malformed`` raised
by ``RELEASE sg_write_*`` -- FTS5 flushes buffered writes at RELEASE, and that
statement sat outside both the per-file containment and the FTS self-repair.
A malformed error inside the per-file write was also counted as an ordinary
``index_write_skipped``.

Covers: (a) malformed at RELEASE over FTS-only damage repairs once and
completes; (b) malformed inside ``_write_extraction`` does the same and is
never a per-file skip; (c) non-FTS damage is typed corruption with no
rebuild; (d) a failed retry stops after one rebuild; (e) an ordinary per-file
IntegrityError is still one contained skip; (f) malformed at the outer COMMIT
is never raw sqlite; (g) a malformed forward RELEASE whose cleanup RELEASE is
also malformed still repairs exactly once and completes; (h) an ordinary
per-file error whose cleanup RELEASE fails non-malformed is a typed
``source_graph_build_failed_savepoint_cleanup`` failure with no rebuild and
the canonical generation untouched.

Run: python3 -m pytest -q tests/test_source_graph_build_release_repair_nf01172.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from test_source_graph_fts_self_repair import (  # noqa: E402
    _PAGE_FINDING,
    _damage_entities_fts,
    _malformed_image,
    _quick_check,
    _refuse_rebuild,
    _repo_with_index,
    _stop_daemons,  # noqa: F401 -- autouse fixture
    sg,
)


class _Faults:
    """Deterministic malformed-image injection on every sqlite connection.

    ``release`` faults only a forward ``RELEASE sg_write_*``; the cleanup
    RELEASE that follows ``ROLLBACK TO`` is faulted only by
    ``cleanup_release``, with ``cleanup_error`` building the raised error.
    """

    def __init__(
        self, *, release: int = 0, cleanup_release: int = 0, commit: int = 0,
        cleanup_error=None,
    ) -> None:
        self.release = release
        self.cleanup_release = cleanup_release
        self.commit = commit
        self.cleanup_error = cleanup_error or _malformed_image
        self.fired: list[str] = []


def _install_faults(monkeypatch, faults: _Faults) -> None:
    real_connect = sqlite3.connect

    class _FaultConnection(sqlite3.Connection):
        _merge_seen = False
        _rolled_back = False

        def execute(self, sql, *args):
            statement = str(sql).strip()
            if statement.startswith("ROLLBACK TO sg_write_"):
                self._rolled_back = True
            elif statement.startswith("RELEASE sg_write_"):
                self._merge_seen = True
                if self._rolled_back:
                    # The cleanup RELEASE after ROLLBACK TO never spends the
                    # forward-RELEASE budget.
                    self._rolled_back = False
                    if faults.cleanup_release > 0:
                        faults.cleanup_release -= 1
                        faults.fired.append(f"cleanup:{statement}")
                        raise faults.cleanup_error()
                elif faults.release > 0:
                    faults.release -= 1
                    faults.fired.append(statement)
                    raise _malformed_image()
            return super().execute(sql, *args)

        def __exit__(self, exc_type, exc, tb):
            # ``with conn:`` commits in C without calling a Python ``commit``
            # override, so the fault is injected at the context exit. Only the
            # merge transaction's COMMIT fails, never an unrelated one, and it
            # fails as SQLite's does: the transaction is rolled back.
            if (
                exc_type is None and self._merge_seen
                and faults.commit > 0 and self.in_transaction
            ):
                faults.commit -= 1
                faults.fired.append("COMMIT")
                self.rollback()
                raise _malformed_image()
            return super().__exit__(exc_type, exc, tb)

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", _FaultConnection)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)


def _count_rebuilds(monkeypatch) -> list[str]:
    rebuilds: list[str] = []
    real_rebuild = sg.rebuild_fts_index

    def counted_rebuild(conn, table):
        rebuilds.append(table)
        return real_rebuild(conn, table)

    monkeypatch.setattr(sg, "rebuild_fts_index", counted_rebuild)
    return rebuilds


def _add_new_file(root: Path, name: str = "gamma") -> None:
    # A NEW file owns no FTS rows yet, so invalidation deletes nothing and the
    # first malformed error the build sees is the injected one.
    (root / f"{name}.py").write_text(
        f"def {name}_marker():\n    return '{name} payload'\n", encoding="utf-8"
    )


def _skips(report) -> list[dict[str, str]]:
    return [e for e in report.errors if e.get("status") == "index_write_skipped"]


def _assert_repaired_build(root: Path, canonical: Path, report, rebuilds) -> None:
    assert rebuilds == ["main.entities_fts"]
    assert report.incremental is True
    assert report.files_changed == 1, report
    assert _skips(report) == []
    assert _quick_check(canonical) == ["ok"]
    state = sg.read_integrity_state(canonical)
    assert state["status"] == sg.INTEGRITY_STATUS_REPAIRED, state
    assert state["last_repair"]["operation"] == "build_write_file"
    assert state["last_repair"]["tables"] == ["main.entities_fts"]
    assert sg.generation_marked_corrupt(canonical) is False
    assert sg.focus(root, "gamma_marker", budget=8)["matches"]
    assert sg.focus(root, "alpha_marker", budget=8)["matches"]


def test_a_malformed_release_over_fts_damage_repairs_once_and_completes(
    tmp_path, monkeypatch
):
    root = _repo_with_index(tmp_path, "release_repair")
    canonical = sg.resolve_db_path(root)
    _damage_entities_fts(canonical)
    rebuilds = _count_rebuilds(monkeypatch)
    faults = _Faults(release=1)
    _install_faults(monkeypatch, faults)
    _add_new_file(root)

    report = sg.build_index(root, incremental=True)

    assert faults.fired == ["RELEASE sg_write_0"]
    _assert_repaired_build(root, canonical, report, rebuilds)


def test_a_malformed_write_extraction_is_repaired_never_skipped(tmp_path, monkeypatch):
    root = _repo_with_index(tmp_path, "extraction_repair")
    canonical = sg.resolve_db_path(root)
    _damage_entities_fts(canonical)
    rebuilds = _count_rebuilds(monkeypatch)
    real_write = sg._write_extraction
    calls: list[str] = []

    def malformed_once(conn, extraction, **kwargs):
        calls.append(extraction.file_path)
        if len(calls) == 1:
            raise _malformed_image()
        return real_write(conn, extraction, **kwargs)

    monkeypatch.setattr(sg, "_write_extraction", malformed_once)
    _add_new_file(root)

    report = sg.build_index(root, incremental=True)

    assert calls == ["gamma.py", "gamma.py"]
    _assert_repaired_build(root, canonical, report, rebuilds)


def test_a_malformed_release_with_non_fts_damage_is_typed_corruption(
    tmp_path, monkeypatch
):
    root = _repo_with_index(tmp_path, "release_non_fts")
    canonical = sg.resolve_db_path(root)
    monkeypatch.setattr(sg, "quick_check_findings", lambda conn: [_PAGE_FINDING])
    monkeypatch.setattr(sg, "rebuild_fts_index", _refuse_rebuild)
    faults = _Faults(release=1)
    _install_faults(monkeypatch, faults)
    _add_new_file(root)

    with pytest.raises(sg.SourceGraphCorruptIndexError) as caught:
        sg.build_index(root, incremental=True)

    assert faults.fired == ["RELEASE sg_write_0"]
    assert caught.value.repair_state == sg.INTEGRITY_REPAIR_REFUSED_NON_FTS
    assert caught.value.fts_only is False
    assert sg.generation_marked_corrupt(canonical) is True


def test_a_retry_that_fails_again_stops_after_one_rebuild(tmp_path, monkeypatch):
    root = _repo_with_index(tmp_path, "release_retry_fails")
    canonical = sg.resolve_db_path(root)
    _damage_entities_fts(canonical)
    rebuilds = _count_rebuilds(monkeypatch)
    faults = _Faults(release=2)
    _install_faults(monkeypatch, faults)
    _add_new_file(root)

    with pytest.raises(sg.SourceGraphCorruptIndexError) as caught:
        sg.build_index(root, incremental=True)

    assert faults.fired == ["RELEASE sg_write_0", "RELEASE sg_write_0"]
    assert rebuilds == ["main.entities_fts"]
    assert caught.value.repair_state == sg.INTEGRITY_RETRY_FAILED
    assert sg.generation_marked_corrupt(canonical) is True


def test_an_ordinary_per_file_integrity_error_is_still_one_contained_skip(
    tmp_path, monkeypatch
):
    root = _repo_with_index(tmp_path, "contained_skip")
    canonical = sg.resolve_db_path(root)
    monkeypatch.setattr(sg, "rebuild_fts_index", _refuse_rebuild)
    real_write = sg._write_extraction

    def integrity_error_for_gamma(conn, extraction, **kwargs):
        if extraction.file_path == "gamma.py":
            raise sqlite3.IntegrityError("UNIQUE constraint failed: entities.qualname")
        return real_write(conn, extraction, **kwargs)

    monkeypatch.setattr(sg, "_write_extraction", integrity_error_for_gamma)
    _add_new_file(root, "gamma")
    _add_new_file(root, "delta")

    report = sg.build_index(root, incremental=True)

    skips = _skips(report)
    assert [entry["file"] for entry in skips] == ["gamma.py"]
    assert skips[0]["error"].startswith("IntegrityError:")
    assert report.files_skipped == 1
    assert report.files_changed == 1
    assert sg.generation_marked_corrupt(canonical) is False
    assert sg.focus(root, "delta_marker", budget=8)["matches"]


def test_a_malformed_outer_commit_never_escapes_as_raw_sqlite(tmp_path, monkeypatch):
    root = _repo_with_index(tmp_path, "commit_malformed")
    canonical = sg.resolve_db_path(root)
    faults = _Faults(commit=1)
    _install_faults(monkeypatch, faults)
    _add_new_file(root)

    try:
        report = sg.build_index(root, incremental=True)
    except sg.SourceGraphCorruptIndexError:
        assert sg.generation_marked_corrupt(canonical) is True
    else:
        assert _quick_check(canonical) == ["ok"]
        assert sg.focus(root, "gamma_marker", budget=8)["matches"]
        assert report.files_changed == 1
    assert faults.fired == ["COMMIT"]


def test_a_malformed_forward_and_cleanup_release_still_repairs_once(
    tmp_path, monkeypatch
):
    root = _repo_with_index(tmp_path, "cleanup_release_malformed")
    canonical = sg.resolve_db_path(root)
    _damage_entities_fts(canonical)
    rebuilds = _count_rebuilds(monkeypatch)
    faults = _Faults(release=1, cleanup_release=1)
    _install_faults(monkeypatch, faults)
    _add_new_file(root)

    report = sg.build_index(root, incremental=True)

    assert faults.fired == ["RELEASE sg_write_0", "cleanup:RELEASE sg_write_0"]
    _assert_repaired_build(root, canonical, report, rebuilds)


def test_a_non_malformed_cleanup_release_failure_is_a_typed_build_failure(
    tmp_path, monkeypatch
):
    root = _repo_with_index(tmp_path, "cleanup_release_failed")
    canonical = sg.resolve_db_path(root)
    rebuilds = _count_rebuilds(monkeypatch)
    real_write = sg._write_extraction

    def integrity_error_for_gamma(conn, extraction, **kwargs):
        if extraction.file_path == "gamma.py":
            raise sqlite3.IntegrityError("UNIQUE constraint failed: entities.qualname")
        return real_write(conn, extraction, **kwargs)

    monkeypatch.setattr(sg, "_write_extraction", integrity_error_for_gamma)
    faults = _Faults(
        cleanup_release=1,
        cleanup_error=lambda: sqlite3.OperationalError(
            "cannot release savepoint - SQL statements in progress"
        ),
    )
    _install_faults(monkeypatch, faults)
    _add_new_file(root)

    with pytest.raises(sg.SourceGraphBuildFailedError) as caught:
        sg.build_index(root, incremental=True)

    assert "source_graph_build_failed_savepoint_cleanup" in str(caught.value)
    assert faults.fired == ["cleanup:RELEASE sg_write_0"]
    assert rebuilds == []
    assert sg.generation_marked_corrupt(canonical) is False
    assert _quick_check(canonical) == ["ok"]
    assert sg.focus(root, "alpha_marker", budget=8)["matches"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
