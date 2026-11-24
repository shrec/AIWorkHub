"""NF-2026-02134: the in-process server memory profiler produces a complete report."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tracemalloc
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "server_memory_profile.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("server_memory_profile_nf02134", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def profiler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    return _load()


def test_tiny_synthetic_profile_reports_every_step_and_allocators(
    profiler: ModuleType, tmp_path: Path
) -> None:
    # Hermetic and bounded: installed provider CLIs (which completion_inbox's
    # route preflight would run as child processes) are kept off PATH, and the
    # cold server import runs untraced.  Allocator attribution of a traced step
    # is covered by test_traced_step_names_its_allocators_by_file_and_line.
    path_before = os.environ.get("PATH")
    rc = profiler.main(
        [
            "--repo", str(tmp_path),
            "--synthetic-cards", "20",
            "--card-kb", "4",
            "--events-per-card", "2",
            "--event-kb", "1",
            "--repeats", "1",
            "--exclude-provider-clis",
            "--no-trace-startup",
        ]
    )
    assert os.environ.get("PATH") == path_before
    assert not tracemalloc.is_tracing()

    reports = sorted((tmp_path / ".scratch" / "memory_profile").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["step_errors"] == []
    assert rc == 0
    assert report["schema_id"] == profiler.SCHEMA_ID
    assert report["synthetic"]["built"] is True
    assert report["db_before"]["tasks"] == 20
    assert report["db_before"]["task_events"] == 40

    rounds = ["initial", "repeat_1"]
    seen = [(s["round"], s["step"]) for s in report["steps"]]
    assert seen == [(r, name) for r in rounds for name in profiler.STEP_NAMES]
    for step in report["steps"]:
        missing = [key for key in profiler.STEP_METRIC_KEYS if key not in step]
        assert not missing, (step["round"], step["step"], missing)
        assert isinstance(step["gc_objects"], int) and step["gc_objects"] > 0
        assert step["tracemalloc_peak_bytes"] >= step["tracemalloc_step_peak_bytes"] >= 0
        if sys.platform.startswith(("win", "linux")):
            assert isinstance(step["rss_bytes"], int) and step["rss_bytes"] > 0
            assert step["peak_rss_bytes"] >= step["rss_bytes"]
        if sys.platform.startswith("linux") and step["trim"].get("available"):
            assert isinstance(step["rss_after_trim_bytes"], int)

    heaviest = report["heaviest_step"]
    assert heaviest["step"] in profiler.OPERATION_STEPS
    assert heaviest["top_allocators"]
    assert len(heaviest["top_allocators"]) <= 25
    for row in heaviest["top_allocators"]:
        filename, _, lineno = row["location"].rpartition(":")
        assert filename and lineno.isdigit() and row["size_diff_bytes"] > 0

    growth = report["repeat_growth"]
    assert (growth["from"], growth["to"]) == ("initial", "repeat_1")
    assert growth["top_allocators"]
    # Both bounding flags are visible in the report, never silent.
    assert report["startup"]["traced"] is False
    assert report["config"]["trace_startup"] is False
    assert report["config"]["exclude_provider_clis"] is True
    provider_clis = report["provider_clis"]
    assert provider_clis["enabled"] is True and provider_clis["binaries"]
    assert isinstance(provider_clis["excluded_dirs"], list)


def test_probe_reports_named_basis(profiler: ModuleType) -> None:
    sample = profiler.probe_process_memory()
    assert {"rss_bytes", "peak_rss_bytes", "private_bytes", "memory_basis"} <= set(sample)
    if sys.platform == "win32":
        assert sample["memory_basis"] == "windows:GetProcessMemoryInfo"
        assert sample["private_bytes"] and sample["private_bytes"] > 0
    elif sys.platform.startswith("linux"):
        assert sample["memory_basis"] == "linux:/proc/self/status"
        assert sample["peak_rss_bytes"] >= sample["rss_bytes"] > 0


def test_synthetic_build_refuses_a_repo_that_already_has_tasks(
    profiler: ModuleType, tmp_path: Path
) -> None:
    profiler.build_synthetic_db(tmp_path, cards=2, card_kb=1, events_per_card=0, event_kb=1)
    db_path = profiler.task_db_path(tmp_path)
    rc = profiler.main(["--repo", str(tmp_path), "--synthetic-cards", "2", "--card-kb", "1"])
    assert rc == 2
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    finally:
        conn.close()
    assert not (tmp_path / ".scratch" / "memory_profile").exists()


def test_traced_step_names_its_allocators_by_file_and_line(profiler: ModuleType) -> None:
    """A traced step's retained growth is attributed to filename:lineno."""

    external = tracemalloc.is_tracing()
    if not external:
        tracemalloc.start(1)
    try:
        excluded = profiler.excluded_filenames()
        previous = profiler.line_statistics(excluded)

        def allocate() -> list[bytearray]:
            return [bytearray(4096) for _ in range(256)]

        record, snapshot = profiler._run_step(
            "startup",
            allocate,
            round_label="initial",
            previous=previous,
            excluded=excluded,
            top=5,
        )
    finally:
        if not external:
            tracemalloc.stop()

    assert snapshot is not None and record["error"] is None
    missing = [key for key in profiler.STEP_METRIC_KEYS if key not in record]
    assert not missing
    rows = record["top_allocators"]
    assert rows and len(rows) <= 5
    for row in rows:
        filename, _, lineno = row["location"].rpartition(":")
        assert filename and lineno.isdigit() and row["size_diff_bytes"] > 0
    top_file = Path(rows[0]["location"].rpartition(":")[0]).name
    assert top_file == Path(__file__).name, rows[0]
    assert rows[0]["size_diff_bytes"] >= 256 * 4096
