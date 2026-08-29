"""Reviewer prewarm cost after the full-index clone was deleted (NF-302).

The old prewarm cloned the entire canonical database (measured at ~107 MB /
759 files for a candidate that changed six) and re-indexed only the changed
files. These tests pin the replacement: prewarm now builds a PARTITION over the
packet's changed paths alone, records the base for composition, and never makes
a full copy. The deliverable numbers -- before/after bytes and wall time for 6
changed files, for 3 lenses, and for 50 changed files -- are printed by
:func:`test_prewarm_cost_report` and asserted below.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from aiworkhub import quality_reviewer
from aiworkhub import process_launcher
from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import storage_registry
from aiworkhub import worker_ai_tools_mcp as worker_tools


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base_db(repo: Path) -> Path:
    registry = storage_registry.load_storage_registry(repo.resolve())
    return storage_registry.resolve_database_path(registry, "source_graph")


def _module_source(index: int) -> str:
    return (
        f"def symbol_{index:04d}():\n    return {index}\n\n"
        f"def helper_{index:04d}(value):\n    return value + {index}\n"
    )


def _setup(
    tmp_path: Path,
    *,
    changed: int,
    base_filler: int,
) -> tuple[Path, Path, Path, int]:
    """Build a canonical base and a candidate worktree changing ``changed`` files.

    Returns ``(canonical, candidate, packet_path, changed)``. The base holds
    ``base_filler`` extra modules so a clone would be obviously large.
    """

    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    runtime = tmp_path / "runtime"
    canonical.mkdir(parents=True)
    candidate.mkdir(parents=True)
    runtime.mkdir(parents=True)

    for i in range(base_filler):
        (canonical / f"filler_{i:04d}.py").write_text(
            _module_source(10_000 + i), encoding="utf-8"
        )
    for i in range(changed):
        (canonical / f"target_{i:04d}.py").write_text(
            _module_source(i), encoding="utf-8"
        )
    repository_state.bootstrap_repository(canonical)
    source_graph.build_index(canonical, incremental=False)

    changed_hashes: dict[str, str] = {}
    for i in range(changed):
        rel = f"target_{i:04d}.py"
        candidate_file = candidate / rel
        candidate_file.write_text(
            _module_source(i) + f"\ndef added_{i:04d}():\n    return {i}\n",
            encoding="utf-8",
        )
        changed_hashes[rel] = _sha(candidate_file)

    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes=changed_hashes,
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return canonical, candidate, packet_path, changed


def _prewarm_once(
    packet_path: Path, candidate: Path, canonical: Path
) -> tuple[dict, float]:
    started = time.monotonic()
    result = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical,
    )
    return result, time.monotonic() - started


def _partition_file_rows(db_path: Path) -> int:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    finally:
        conn.close()


def _bare_process_manager(monkeypatch):
    manager = process_launcher.ProcessManager.__new__(process_launcher.ProcessManager)
    manager._quality_review_prepared = {}
    manager._quality_review_flights = {}
    monkeypatch.setattr(
        process_launcher.ProcessManager, "_QUALITY_REVIEW_PREP_ACTIVE_BUILDERS", 0
    )
    return manager


def test_quality_review_active_builder_and_flight_capacity_bound(
    monkeypatch,
) -> None:
    manager = _bare_process_manager(monkeypatch)
    assert manager._QUALITY_REVIEW_PREP_MAX == 8
    monkeypatch.setattr(manager, "_QUALITY_REVIEW_PREP_WAIT_SECONDS", 0.15)
    monkeypatch.setattr(manager, "_QUALITY_REVIEW_PREP_OWNER_SECONDS", 0.15)
    release = threading.Event()
    builders_started = threading.Condition()
    active = 0
    peak_active = 0
    peak_flights = 0

    def blocked_build(request_id, task_id, progress=None):
        nonlocal active, peak_active, peak_flights
        with builders_started:
            active += 1
            peak_active = max(peak_active, active)
            peak_flights = max(peak_flights, len(manager._quality_review_flights))
            builders_started.notify_all()
        release.wait(timeout=2)
        with builders_started:
            active -= 1
        return {"ok": True, "prepared": {"request_id": request_id, "task_id": task_id}}

    manager._build_quality_review_packet = blocked_build
    barrier = threading.Barrier(13)
    results = []

    def call(index):
        barrier.wait()
        results.append(manager._prepared_quality_review(f"request-{index}", f"task-{index}"))

    callers = [threading.Thread(target=call, args=(index,)) for index in range(12)]
    for caller in callers:
        caller.start()
    barrier.wait()
    with builders_started:
        assert builders_started.wait_for(lambda: active == 8, timeout=1)
    for caller in callers:
        caller.join(timeout=1)
        assert not caller.is_alive()

    assert peak_active == 8
    assert peak_flights <= 8
    assert len(manager._quality_review_flights) == 8
    assert manager._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS == 8
    assert len(results) == 12
    assert {result["error"] for result in results} == {
        "quality_review_preparation_timeout"
    }

    release.set()
    with manager._QUALITY_REVIEW_PREP_CAPACITY:
        assert manager._QUALITY_REVIEW_PREP_CAPACITY.wait_for(
            lambda: manager._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS == 0, timeout=2
        )
    assert manager._quality_review_flights == {}
    assert len(manager._quality_review_prepared) == 8


def test_quality_review_single_flight_survives_caller_timeout(monkeypatch) -> None:
    manager = _bare_process_manager(monkeypatch)
    monkeypatch.setattr(manager, "_QUALITY_REVIEW_PREP_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(manager, "_QUALITY_REVIEW_PREP_OWNER_SECONDS", 0.05)
    release = threading.Event()
    started = threading.Event()
    calls = 0

    def blocked_build(request_id, task_id, progress=None):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return {"ok": True, "prepared": {"packet": "authoritative"}}

    manager._build_quality_review_packet = blocked_build
    first = manager._prepared_quality_review("same-request", "same-task")
    assert started.is_set()
    assert first == {"ok": False, "error": "quality_review_preparation_timeout"}
    second = manager._prepared_quality_review("same-request", "same-task")
    assert second == first
    assert calls == 1
    assert len(manager._quality_review_flights) == 1

    release.set()
    with manager._QUALITY_REVIEW_PREP_CAPACITY:
        assert manager._QUALITY_REVIEW_PREP_CAPACITY.wait_for(
            lambda: manager._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS == 0, timeout=2
        )
    assert manager._quality_review_flights == {}
    cached = manager._prepared_quality_review("same-request", "same-task")
    assert cached == {"ok": True, "prepared": {"packet": "authoritative"}}
    assert calls == 1


def test_quality_review_single_flight_exception_notifies_and_cleans_up(
    monkeypatch,
) -> None:
    manager = _bare_process_manager(monkeypatch)

    def failed_build(request_id, task_id, progress=None):
        raise RuntimeError("packet exploded")

    manager._build_quality_review_packet = failed_build
    result = manager._prepared_quality_review("failed-request", "failed-task")
    assert result == {
        "ok": False,
        "error": "quality_review_target_invalid:packet exploded",
    }
    assert manager._quality_review_flights == {}
    assert manager._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS == 0


def test_quality_review_single_flight_thread_start_exception_cleans_up(
    monkeypatch,
) -> None:
    manager = _bare_process_manager(monkeypatch)

    def unexpected_build(request_id, task_id, progress=None):
        raise AssertionError("builder body must not run when Thread.start fails")

    def failed_start(_thread):
        raise RuntimeError("thread refused")

    manager._build_quality_review_packet = unexpected_build
    monkeypatch.setattr(process_launcher.threading.Thread, "start", failed_start)

    started = time.monotonic()
    result = manager._prepared_quality_review("failed-start-request", "failed-start-task")
    elapsed = time.monotonic() - started

    assert result == {
        "ok": False,
        "error": "quality_review_target_invalid:thread refused",
    }
    assert elapsed < manager._QUALITY_REVIEW_PREP_WAIT_SECONDS
    assert manager._quality_review_flights == {}
    assert manager._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS == 0
    assert manager._quality_review_prepared == {}


def test_prewarm_six_changed_files_makes_no_full_copy(
    tmp_path: Path, monkeypatch
) -> None:
    canonical, candidate, packet_path, changed = _setup(
        tmp_path, changed=6, base_filler=200
    )

    def fail_build(*_a, **_k):
        raise AssertionError("prewarm must never call build_index")

    monkeypatch.setattr(source_graph, "build_index", fail_build)

    result, _wall = _prewarm_once(packet_path, candidate, canonical)
    assert result["built"] is True
    partition = Path(result["db_path"])
    base_db = _base_db(canonical)

    # The partition indexes only the changed files -- not the whole repository a
    # clone would have copied.
    assert _partition_file_rows(partition) == changed
    # And it is a small fraction of the base a clone would have written.
    assert partition.stat().st_size < base_db.stat().st_size // 2


def test_prewarm_three_lenses_share_one_partition_build(
    tmp_path: Path, monkeypatch
) -> None:
    canonical, candidate, packet_path, changed = _setup(
        tmp_path, changed=4, base_filler=40
    )

    def fail_build(*_a, **_k):
        raise AssertionError("prewarm must never call build_index")

    monkeypatch.setattr(source_graph, "build_index", fail_build)

    real_index_file = source_graph.index_file
    calls = {"n": 0}

    def counting_index_file(repo_root, path, expected_hash):
        calls["n"] += 1
        return real_index_file(repo_root, path, expected_hash)

    monkeypatch.setattr(source_graph, "index_file", counting_index_file)

    # Three lenses of ONE candidate reuse a single packet-bound overlay.
    builts = []
    for _lens in range(3):
        result, _wall = _prewarm_once(packet_path, candidate, canonical)
        builts.append(result["built"])

    # Exactly one lens builds; the other two reuse it. The partition is indexed
    # exactly once -- one index_file call per changed file, no rebuild per lens.
    assert sorted(builts) == [False, False, True]
    assert calls["n"] == changed


def test_prewarm_fifty_changed_files_scales_with_changes_not_base(
    tmp_path: Path, monkeypatch
) -> None:
    small = _setup(tmp_path / "a", changed=6, base_filler=200)
    big = _setup(tmp_path / "b", changed=50, base_filler=200)

    # Guard installed AFTER the base fixtures are built, so it polices only the
    # prewarm calls below -- never the legitimate _setup base build.
    def fail_build(*_a, **_k):
        raise AssertionError("prewarm must never call build_index")

    monkeypatch.setattr(source_graph, "build_index", fail_build)

    small_result, _ = _prewarm_once(small[2], small[1], small[0])
    big_result, _ = _prewarm_once(big[2], big[1], big[0])

    small_part = Path(small_result["db_path"])
    big_part = Path(big_result["db_path"])
    assert _partition_file_rows(small_part) == 6
    assert _partition_file_rows(big_part) == 50
    # Cost scales with the changed set: 50 changed files cost more than 6.
    assert big_part.stat().st_size > small_part.stat().st_size
    # But never with the base: both bases are the same size, and each partition
    # stays far below it -- no clone was made in either case.
    assert big_part.stat().st_size < _base_db(big[0]).stat().st_size


def test_prewarm_cost_report(tmp_path: Path, monkeypatch, capsys) -> None:
    """Emit the mandated before/after numbers for 6 / 3-lens / 50 changed."""

    # Build every base fixture BEFORE installing the guard, so the guard polices
    # only the prewarm calls -- never the legitimate _setup base builds.
    six = _setup(tmp_path / "six", changed=6, base_filler=200)
    lenses = _setup(tmp_path / "lens", changed=6, base_filler=200)
    fifty = _setup(tmp_path / "fifty", changed=50, base_filler=200)

    def fail_build(*_a, **_k):
        raise AssertionError("prewarm must never call build_index")

    monkeypatch.setattr(source_graph, "build_index", fail_build)

    report: dict[str, dict[str, float]] = {}

    # 6 changed files.
    result, wall = _prewarm_once(six[2], six[1], six[0])
    before = _base_db(six[0]).stat().st_size
    after = Path(result["db_path"]).stat().st_size
    report["6_changed_files"] = {
        "before_bytes_clone_would_copy": before,
        "after_bytes_partition": after,
        "wall_seconds": round(wall, 4),
    }
    assert after < before

    # 3 lenses on one candidate: total prepare cost is one build + two reuses.
    lens_before = _base_db(lenses[0]).stat().st_size
    lens_after = 0
    lens_wall = 0.0
    for _lens in range(3):
        result, wall = _prewarm_once(lenses[2], lenses[1], lenses[0])
        lens_after = Path(result["db_path"]).stat().st_size
        lens_wall += wall
    report["3_lenses_one_candidate"] = {
        "before_bytes_clone_would_copy": lens_before * 3,
        "after_bytes_partition_shared": lens_after,
        "wall_seconds_total": round(lens_wall, 4),
    }
    # Three clones would have copied the base three times; the composed view
    # builds one partition and reuses it.
    assert lens_after < lens_before

    # 50 changed files.
    result, wall = _prewarm_once(fifty[2], fifty[1], fifty[0])
    fifty_before = _base_db(fifty[0]).stat().st_size
    fifty_after = Path(result["db_path"]).stat().st_size
    report["50_changed_files"] = {
        "before_bytes_clone_would_copy": fifty_before,
        "after_bytes_partition": fifty_after,
        "wall_seconds": round(wall, 4),
    }
    assert fifty_after < fifty_before

    print("PREWARM_COST_REPORT " + json.dumps(report, indent=2))
    captured = capsys.readouterr()
    assert "PREWARM_COST_REPORT" in captured.out
