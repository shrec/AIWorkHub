"""Concurrent reviewer overlays must not lose their composed base pin.

NF-2026-02385: two quality reviewers of one candidate went blind while the
canonical Source Graph was healthy, and a reviewer launch was refused with
``composed_base_pin_raced``. These tests reproduce both failure shapes with
real files and real threads, synchronised only by barriers and events:

  (a) a canonical republish between the generation snapshot and the link is
      re-snapshotted and pinned, and a base that keeps moving past the
      wall-clock budget still fails closed with the measured reason;
  (b) concurrent pinners of the same generation both succeed and no caller
      ever unlinks a pin another partition created, verified and references.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import source_graph_partition as sgp
from aiworkhub import storage_registry

_WAIT_SECONDS = 60.0


def _build_base(repo: Path) -> Path:
    files = {
        "keep.py": "def keep_symbol():\n    return 1\n",
        "change.py": "def original_symbol():\n    return 2\n",
    }
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    repository_state.bootstrap_repository(repo)
    source_graph.build_index(repo, incremental=False)
    registry = storage_registry.load_storage_registry(repo.resolve())
    base_db = storage_registry.resolve_database_path(registry, "source_graph")
    (repo / "change.py").write_text(
        "def changed_symbol():\n    return 3\n", encoding="utf-8"
    )
    return base_db


def _changed(repo: Path) -> list[dict[str, str]]:
    digest = hashlib.sha256((repo / "change.py").read_bytes()).hexdigest()
    return [{"path": "change.py", "sha256": digest}]


def _pins(directory: Path) -> list[Path]:
    return sorted(directory.glob(".sg-base-pin.*.sqlite"))


def _entity_names(partition: Path) -> set[str]:
    conn = source_graph.connect(partition, read_only=True)
    try:
        return {str(row[0]) for row in conn.execute("SELECT name FROM entities")}
    finally:
        conn.close()


def _assert_answers_overlay(partition: Path) -> None:
    names = _entity_names(partition)
    assert "keep_symbol" in names, "base rows missing: overlay is blind"
    assert "changed_symbol" in names
    assert "original_symbol" not in names


@pytest.fixture()
def candidate(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = _build_base(repo)
    overlay = tmp_path / "runtime"
    overlay.mkdir()
    return repo, base_db, overlay


def _build_overlay(repo: Path, base_db: Path, final: Path) -> Any:
    """Build like the reviewer prewarm: a private ``.tmp``, then publish it.

    Publishing goes through the same owner the prewarm uses, so a concurrent
    reviewer's prune briefly reading this partition's marker (a Windows
    sharing violation) is retried there instead of blinding this reviewer.
    """

    temporary = final.parent / f".{final.name}.{secrets.token_hex(8)}.tmp"
    try:
        report = sgp.build_partition(
            repo, _changed(repo), temporary, base_db_path=base_db,
        )
        sgp.publish_partition(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    return report


def _run_reviewers(
    roles: dict[str, Callable[[], Any]],
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}

    def run(role: str, action: Callable[[], Any]) -> None:
        try:
            results[role] = action()
        except BaseException as exc:  # surfaced below with its role
            errors[role] = exc

    threads = [
        threading.Thread(target=run, args=(role, action), name=role)
        for role, action in roles.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_WAIT_SECONDS)
        assert not thread.is_alive(), f"{thread.name} did not finish"
    assert errors == {}, {role: repr(exc) for role, exc in errors.items()}
    return results


def _first_call_hook(
    hooks: dict[str, Callable[[], None]],
) -> Callable[..., Any]:
    """An ``os.link`` that runs one hook per thread on that thread's first call."""

    real_link = os.link
    pending = dict(hooks)
    lock = threading.Lock()

    def link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        with lock:
            hook = pending.pop(threading.current_thread().name, None)
        if hook is not None:
            hook()
        return real_link(src, dst, *args, **kwargs)

    return link


# ---------------------------------------------------------------------------
# (b) concurrent pinners of the same generation
# ---------------------------------------------------------------------------

def test_two_threads_pinning_one_generation_both_succeed(
    candidate: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_db, overlay = candidate
    generation = os.stat(base_db)
    real_link = os.link
    # Two rendezvous: both callers snapshot and privately link the SAME
    # generation, then both publish the canonical pin name at once, so one of
    # them meets the other's freshly created pin (FileExistsError).
    snapshot = threading.Barrier(2, timeout=_WAIT_SECONDS)
    publish = threading.Barrier(2, timeout=_WAIT_SECONDS)

    def link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if threading.current_thread().name.startswith("reviewer-"):
            if Path(dst).name.startswith(".sg-base-link."):
                snapshot.wait()
            elif sgp._BASE_PIN_NAME.fullmatch(Path(dst).name):
                publish.wait()
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(sgp.os, "link", link)
    finals = {
        "reviewer-correctness": overlay / "candidate_source_graph_a.sqlite",
        "reviewer-security": overlay / "candidate_source_graph_b.sqlite",
    }
    reports = _run_reviewers(
        {
            role: (lambda final=final: _build_overlay(repo, base_db, final))
            for role, final in finals.items()
        }
    )
    monkeypatch.undo()

    assert {report.base_pin for report in reports.values()} == {"pinned"}
    (pin,) = _pins(overlay)
    assert os.stat(pin).st_ino == generation.st_ino
    assert list(overlay.glob(".sg-base-link.*")) == []
    assert all(r.failed_files == () for r in reports.values())
    for final in finals.values():
        marker = sgp.read_composed_marker(final)
        assert marker is not None and marker["base"]["pin"]["name"] == pin.name
        _assert_answers_overlay(final)


def test_failed_verification_never_unlinks_a_pin_another_partition_uses(
    candidate: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_db, overlay = candidate
    first = overlay / "candidate_source_graph_a.sqlite"
    assert _build_overlay(repo, base_db, first).base_pin == "pinned"
    (pin,) = _pins(overlay)

    def unreadable(path: Path) -> None:
        raise sgp.PartitionBasePinError(f"composed_base_pin_unreadable:{path.name}")

    monkeypatch.setattr(sgp, "_verify_pinned_generation", unreadable)
    with pytest.raises(sgp.PartitionBasePinError, match="composed_base_pin_unreadable"):
        _build_overlay(repo, base_db, overlay / "candidate_source_graph_b.sqlite")
    monkeypatch.undo()

    assert _pins(overlay) == [pin], "a valid pin another overlay uses was removed"
    _assert_answers_overlay(first)


def test_prune_keeps_pins_while_a_marker_is_transiently_unreadable(
    candidate: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_db, overlay = candidate
    first = overlay / "candidate_source_graph_a.sqlite"
    _build_overlay(repo, base_db, first)
    (pin,) = _pins(overlay)
    real_select = sgp._select_marker_row

    def locked(path: Path) -> Any:
        if Path(path).name == first.name:
            raise sqlite3.OperationalError("database is locked")
        return real_select(path)

    monkeypatch.setattr(sgp, "_select_marker_row", locked)
    # Another reviewer's marker write prunes with its own (different) pin.
    assert sgp._prune_base_pins(overlay, keep=".sg-base-pin.0.0.0.0.sqlite") == 0
    monkeypatch.undo()
    assert _pins(overlay) == [pin]
    assert sgp._prune_base_pins(overlay, keep=".sg-base-pin.0.0.0.0.sqlite") == 0
    assert _pins(overlay) == [pin]
    _assert_answers_overlay(first)


# ---------------------------------------------------------------------------
# (a) a canonical republish between snapshot and link
# ---------------------------------------------------------------------------

def _publish_generation(fresh: Path, base_db: Path) -> None:
    """Atomically republish ``fresh`` as the canonical base.

    Windows may briefly deny replacing a just-written file (a scanner holding
    it open); that is the test's publisher retrying, bounded, never the code
    under test, so it cannot masquerade as an ``EACCES`` link failure.
    """

    for attempt in range(100):
        try:
            os.replace(fresh, base_db)
            return
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.01)

def test_concurrent_reviewer_builds_survive_a_republish_and_both_answer(
    candidate: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both reviewers snapshot generation 1; the base is republished before
    either links. Both must re-snapshot generation 2, pin it concurrently and
    answer overlay queries -- the measured 0.13.11 failure had both blind."""

    repo, base_db, overlay = candidate
    before = os.stat(base_db)
    newer = overlay.parent / "republished.sqlite"
    newer.write_bytes(base_db.read_bytes())
    both_snapshotted = threading.Barrier(2, timeout=_WAIT_SECONDS)
    republished = threading.Event()

    def republish() -> None:
        both_snapshotted.wait()
        _publish_generation(newer, base_db)
        republished.set()

    def after_republish() -> None:
        both_snapshotted.wait()
        assert republished.wait(_WAIT_SECONDS)

    monkeypatch.setattr(
        sgp.os,
        "link",
        _first_call_hook(
            {
                "reviewer-correctness": republish,
                "reviewer-security": after_republish,
            }
        ),
    )
    finals = {
        "reviewer-correctness": overlay / "candidate_source_graph_a.sqlite",
        "reviewer-security": overlay / "candidate_source_graph_b.sqlite",
    }
    reports = _run_reviewers(
        {
            role: (lambda final=final: _build_overlay(repo, base_db, final))
            for role, final in finals.items()
        }
    )
    monkeypatch.undo()

    current = os.stat(base_db)
    assert current.st_ino != before.st_ino
    assert {report.base_pin for report in reports.values()} == {"pinned"}
    (pin,) = _pins(overlay)
    assert os.stat(pin).st_ino == current.st_ino
    assert list(overlay.glob(".sg-base-link.*")) == []
    assert all(r.failed_files == () for r in reports.values())
    for final in finals.values():
        marker = sgp.read_composed_marker(final)
        assert marker is not None
        assert marker["base"]["ino"] == current.st_ino
        _assert_answers_overlay(final)


def test_base_that_keeps_moving_fails_closed_with_measured_reason(
    candidate: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_db, overlay = candidate
    original = base_db.read_bytes()
    counter = itertools.count()
    real_link = os.link

    def always_republished(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(dst).name.startswith(".sg-base-link."):
            fresh = overlay.parent / f"generation-{next(counter)}.sqlite"
            fresh.write_bytes(original)
            _publish_generation(fresh, base_db)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(sgp, "_BASE_PIN_RACE_BUDGET_SECONDS", 0.25)
    monkeypatch.setattr(sgp.os, "link", always_republished)
    with pytest.raises(sgp.PartitionBasePinError) as raised:
        _build_overlay(repo, base_db, overlay / "candidate_source_graph_a.sqlite")
    monkeypatch.undo()

    reason = str(raised.value)
    match = re.fullmatch(
        r"composed_base_pin_raced:\.sg-base-pin\.[0-9.]+sqlite"
        r"(:no_progress)?:attempts=(\d+):elapsed_ms=(\d+):budget_ms=250",
        reason,
    )
    assert match is not None, reason
    if match.group(1) is None:
        assert int(match.group(3)) >= 250
    assert int(match.group(2)) >= 1
    assert _pins(overlay) == []
    assert list(overlay.glob(".sg-base-link.*")) == []
