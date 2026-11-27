"""Fix batch 12 regressions (NF-2026-00937, NF-2026-00938).

Review source-diff evidence: the shared byte budget is shared fairly instead
of alphabetically, and a Python hunk's leading context reaches the header of
its enclosing def/class (or names a typed omission past the bound).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from aiworkhub.process_launcher_source_evidence import (
    _ENCLOSING_SYMBOL_MAX_CONTEXT_LINES,
    quality_review_source_evidence,
)


class _Manager:
    _QUALITY_REVIEW_SOURCE_CONTEXT_LINES = 3
    _QUALITY_REVIEW_SOURCE_MAX_BYTES = 24 * 1024
    _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES = 64 * 1024

    def __init__(self, repo: Path) -> None:
        self.repo = str(repo)


def _layout(tmp_path: Path, files: dict[str, tuple[str, str]]):
    repo = tmp_path / "repo"
    work = tmp_path / "work"
    repo.mkdir()
    work.mkdir()
    hashes: dict[str, str | None] = {}
    for name, (baseline, candidate) in files.items():
        (repo / name).write_bytes(baseline.encode("utf-8"))
        (work / name).write_bytes(candidate.encode("utf-8"))
        hashes[name] = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return repo, SimpleNamespace(path=str(work)), hashes


def test_alphabetically_first_large_path_cannot_starve_a_small_later_path(
    tmp_path: Path,
) -> None:
    big = "".join(f"big line {index:04d} with filler text\n" for index in range(200))
    repo, workspace, hashes = _layout(tmp_path, {
        "a_large.txt": ("", big),
        "z_small.txt": ("keep\nold\n", "keep\nnew\n"),
    })
    manager = _Manager(repo)
    manager._QUALITY_REVIEW_SOURCE_MAX_BYTES = 4096
    manager._QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES = 1024

    evidence = quality_review_source_evidence(manager, workspace, hashes)

    assert list(evidence) == ["a_large.txt", "z_small.txt"]
    small = evidence["z_small.txt"]
    large = evidence["a_large.txt"]
    assert small["diff_complete"] is True
    assert "+new\n" in small["excerpt"]
    assert large["diff_complete"] is False
    assert large["excerpt_bytes"] + small["excerpt_bytes"] <= 1024
    # The small path's unused share rolled forward to the large one.
    assert large["excerpt_bytes"] > 1024 // 2


def _function_with_change(lead: int, *, changed: bool) -> str:
    body = "".join(f"    value_{index} = {index}\n" for index in range(lead))
    last = "    return 'changed'\n" if changed else "    return 'original'\n"
    return "import os\n\n\ndef target():\n" + body + last + "\n\nX = 1\n"


def test_python_hunk_context_reaches_enclosing_symbol_header(tmp_path: Path) -> None:
    repo, workspace, hashes = _layout(tmp_path, {
        "mod.py": (
            _function_with_change(10, changed=False),
            _function_with_change(10, changed=True),
        ),
    })

    row = quality_review_source_evidence(_Manager(repo), workspace, hashes)["mod.py"]

    assert row["diff_complete"] is True
    assert " def target():\n" in row["excerpt"]
    (segment,) = row["segments"]
    assert segment["candidate_start_line"] == 4
    assert segment["changed_start_line"] == 15
    assert "enclosing_symbol_context_omitted" not in row


def test_far_enclosing_header_is_a_typed_omission(tmp_path: Path) -> None:
    lead = _ENCLOSING_SYMBOL_MAX_CONTEXT_LINES + 5
    repo, workspace, hashes = _layout(tmp_path, {
        "mod.py": (
            _function_with_change(lead, changed=False),
            _function_with_change(lead, changed=True),
        ),
    })

    row = quality_review_source_evidence(_Manager(repo), workspace, hashes)["mod.py"]

    assert " def target():\n" not in row["excerpt"]
    assert row["enclosing_symbol_context_omitted"] == 1
    (segment,) = row["segments"]
    # Fixed context is kept when the header is past the bounded widening.
    assert segment["changed_start_line"] - segment["candidate_start_line"] == 3


def test_non_python_context_stays_fixed(tmp_path: Path) -> None:
    text_old = "".join(f"line {index}\n" for index in range(20))
    text_new = text_old.replace("line 15\n", "line fifteen\n")
    repo, workspace, hashes = _layout(tmp_path, {"notes.txt": (text_old, text_new)})

    row = quality_review_source_evidence(_Manager(repo), workspace, hashes)["notes.txt"]

    (segment,) = row["segments"]
    assert segment["changed_start_line"] - segment["candidate_start_line"] == 3


def test_budget_follows_excerpt_need_not_file_size(tmp_path: Path) -> None:
    # A large file with a one-line diff needs few excerpt bytes; a small file
    # that is entirely new needs many. Ordering by file size would give the
    # small file only half the total first and truncate it.
    big_old = "".join(f"stable line {index:05d}\n" for index in range(2000))
    big_new = big_old.replace("stable line 01000\n", "edited line 01000\n")
    small_new = "".join(f"{'x' * 30}{index:02d}\n" for index in range(30))
    files = {
        "a_large_tiny_diff.txt": (big_old, big_new),
        "b_small_large_diff.txt": ("", small_new),
    }
    repo, workspace, hashes = _layout(tmp_path, files)
    roomy = _Manager(repo)
    needs = {
        path: row["excerpt_bytes"]
        for path, row in quality_review_source_evidence(roomy, workspace, hashes).items()
    }
    assert needs["a_large_tiny_diff.txt"] < needs["b_small_large_diff.txt"]
    total = needs["a_large_tiny_diff.txt"] + needs["b_small_large_diff.txt"]
    # The small-file diff alone exceeds an even split of the total, so only
    # need-ordered water-filling lets both fit.
    assert needs["b_small_large_diff.txt"] > total // 2

    manager = _Manager(repo)
    manager._QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES = total
    evidence = quality_review_source_evidence(manager, workspace, hashes)

    for path in files:
        assert evidence[path]["diff_complete"] is True, path
        assert evidence[path]["truncated"] is False, path
        assert evidence[path]["excerpt_bytes"] == needs[path]


def test_far_header_already_shown_by_an_earlier_hunk_is_not_omitted(
    tmp_path: Path,
) -> None:
    lead = _ENCLOSING_SYMBOL_MAX_CONTEXT_LINES + 10
    body_old = "".join(f"    value_{index} = {index}\n" for index in range(lead))
    body_new = body_old.replace("    value_0 = 0\n", "    value_0 = 'first'\n")
    old = "import os\n\n\ndef target():\n" + body_old + "    return 'original'\n"
    new = "import os\n\n\ndef target():\n" + body_new + "    return 'changed'\n"
    repo, workspace, hashes = _layout(tmp_path, {"mod.py": (old, new)})

    row = quality_review_source_evidence(_Manager(repo), workspace, hashes)["mod.py"]

    first, second = row["segments"]
    # The first hunk's excerpt carries the def header ...
    assert first["candidate_start_line"] <= 4 <= first["candidate_end_line"]
    assert " def target():\n" in row["excerpt"]
    # ... so the second hunk, far past the bounded widening, omits nothing.
    assert second["changed_start_line"] - 4 > _ENCLOSING_SYMBOL_MAX_CONTEXT_LINES
    assert row.get("enclosing_symbol_context_omitted", 0) == 0


def test_pathologically_nested_python_candidate_does_not_raise(tmp_path: Path) -> None:
    deep = "x = " + "-" * 100000 + "1"
    repo, workspace, hashes = _layout(tmp_path, {"deep.py": ("x = 1\n", deep)})

    row = quality_review_source_evidence(_Manager(repo), workspace, hashes)["deep.py"]

    assert row["segments"]
    assert "enclosing_symbol_context_omitted" not in row


def test_source_graph_refresh_has_one_owner_per_repo(tmp_path: Path, monkeypatch) -> None:
    """NF-2026-00761 (partial): concurrent in-process refreshes serialize behind
    ``SourceGraphDaemon.refresh_now`` and never run a second build alongside the
    one in flight. This proves in-process serialization only; the cross-process
    single-writer owner is still open under NF-2026-00761."""
    import threading

    from aiworkhub import source_graph, source_graph_daemon, task_store

    root = tmp_path / "owner_repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]

    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = {"now": 0, "max": 0, "calls": 0}

    def fake_build_index(repo_root, *, incremental=True, db_path=None):
        with lock:
            active["now"] += 1
            active["calls"] += 1
            active["max"] = max(active["max"], active["now"])
        started.set()
        release.wait(timeout=30)
        with lock:
            active["now"] -= 1
        return source_graph.BuildReport(
            repo_root=str(repo_root), db_path="fake.sqlite", incremental=incremental,
            files_seen=0, files_changed=0, files_unchanged=0, files_removed=0,
            entities_written=0, edges_written=0, errors=[],
            build_revision="test", finished_at="t",
        )

    monkeypatch.setattr(source_graph, "build_index", fake_build_index)
    # The fake build is a local closure, so the build must run in-process.
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_THREAD,
    )
    daemon = source_graph_daemon.SourceGraphDaemon(
        root, refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()
    results: list[dict] = []
    try:
        assert started.wait(timeout=30), "background build never started"
        callers = [
            threading.Thread(target=lambda: results.append(daemon.refresh_now()))
            for _ in range(3)
        ]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=30)
        release.set()
    finally:
        release.set()
        daemon.stop()

    assert len(results) == 3
    assert all(result["triggered"] is False for result in results)
    assert all(result["reason"] == "build_in_progress" for result in results)
    assert active["max"] == 1
    # The in-flight build plus at most one coalesced follow-up.
    assert 1 <= active["calls"] <= 2
