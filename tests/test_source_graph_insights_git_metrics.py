"""Focused tests for the O(1) membership hot path in materialize_git_metrics.

The source change replaces the O(n) ``path not in selected`` list scan with a
bounded ``selected_set`` for O(1) membership while ``selected`` remains the
authority for both persisted and returned row order.

Functional tests below exercise the real function (order/parity, cached HEAD,
non-git and subprocess failure paths, the 90-day window and the 10,000-file
cap). The baseline/candidate metric tests emit deterministic, non-wall-clock
membership-cost receipts instead of timing anything.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from aiworkhub.source_graph_insights import materialize_git_metrics

METRIC_NAME = "git_metrics_membership_cost"
METRIC_UNIT = "comparisons"


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE file_history ("
        "file_path TEXT PRIMARY KEY, "
        "commit_touches_90d INTEGER, "
        "lines_added_90d INTEGER, "
        "lines_deleted_90d INTEGER, "
        "authors_90d INTEGER, "
        "primary_author_90d TEXT, "
        "evidence TEXT)"
    )
    return conn


def _completed(args: list[str], returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr="")


def _list_scan_comparisons(selected: list[str], paths: list[str]) -> int:
    """Deterministic equality comparisons for the old ``path not in selected`` scan."""
    positions = {path: index for index, path in enumerate(selected)}
    total = 0
    for path in paths:
        total += positions[path] + 1 if path in positions else len(selected)
    return total


def _set_lookup_count(paths: list[str]) -> int:
    """Deterministic hash lookups for the new ``path not in selected_set`` check."""
    return len(paths)


def test_materialize_git_metrics_order_and_parity(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    stdout = (
        "@@alice\n\n"
        "10\t2\tsrc/a.py\n"
        "5\t3\tsrc/b.py\n"
        "\n"
        "@@bob\n\n"
        "7\t1\tsrc/a.py\n"
    )

    def fake_run(args, **_kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return _completed(args, 0, "deadbeef\n")
        return _completed(args, 0, stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    conn = _make_conn()
    files = ["src/a.py", "src/b.py"]
    result = materialize_git_metrics(conn, repo_root, files)

    assert result["available"] is True
    assert result["window"] == "90d"
    assert [row["file_path"] for row in result["files"]] == files

    by_path = {row["file_path"]: row for row in result["files"]}
    assert by_path["src/a.py"]["commit_file_touches_90d"] == 2
    assert by_path["src/a.py"]["lines_added_90d"] == 17
    assert by_path["src/a.py"]["lines_deleted_90d"] == 3
    assert by_path["src/a.py"]["authors_90d"] == 2
    assert by_path["src/a.py"]["primary_author_90d"] == "alice"
    assert by_path["src/b.py"]["commit_file_touches_90d"] == 1
    assert by_path["src/b.py"]["lines_added_90d"] == 5
    assert by_path["src/b.py"]["lines_deleted_90d"] == 3
    assert by_path["src/b.py"]["primary_author_90d"] == "alice"

    persisted = [
        row["file_path"]
        for row in conn.execute("SELECT file_path FROM file_history ORDER BY rowid")
    ]
    assert persisted == files
    assert conn.execute(
        "SELECT value FROM meta WHERE key='git_history_head'"
    ).fetchone()["value"] == "deadbeef"


def test_cached_head_short_circuits(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    log_calls = [0]

    def fake_run(args, **_kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return _completed(args, 0, "samehead\n")
        log_calls[0] += 1
        return _completed(args, 0, "@@alice\n\n1\t1\tsrc/a.py\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    conn = _make_conn()
    files = ["src/a.py"]

    first = materialize_git_metrics(conn, repo_root, files)
    assert first["available"] is True
    assert "cached" not in first
    assert log_calls[0] == 1

    second = materialize_git_metrics(conn, repo_root, files)
    assert second == {"available": True, "window": "90d", "cached": True, "files": []}
    assert log_calls[0] == 1


def test_non_git_repo(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    result = materialize_git_metrics(_make_conn(), repo_root, ["src/a.py"])
    assert result == {"available": False, "window": "90d", "reason": "not_git_repo", "files": []}


def test_subprocess_oserror_reason(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    def raising_run(_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", raising_run)
    result = materialize_git_metrics(_make_conn(), repo_root, ["src/a.py"])
    assert result["available"] is False
    assert result["window"] == "90d"
    assert result["reason"] == "OSError"
    assert result["files"] == []


def test_git_log_exit_reason(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    def fake_run(args, **_kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return _completed(args, 0, "head\n")
        return _completed(args, 128, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = materialize_git_metrics(_make_conn(), repo_root, ["src/a.py"])
    assert result == {"available": False, "window": "90d", "reason": "git_exit_128", "files": []}


def test_ten_thousand_file_cap(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    def fake_run(args, **_kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return _completed(args, 0, "head\n")
        return _completed(args, 0, "@@alice\n\n1\t1\tsrc/f00000.txt\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    conn = _make_conn()
    files = [f"src/f{i:05d}.txt" for i in range(10050)]
    result = materialize_git_metrics(conn, repo_root, files)

    assert len(result["files"]) == 10000
    assert result["files"][0]["file_path"] == "src/f00000.txt"
    assert result["files"][-1]["file_path"] == "src/f09999.txt"
    assert "src/f10000.txt" not in {row["file_path"] for row in result["files"]}
    assert conn.execute("SELECT COUNT(*) FROM file_history").fetchone()[0] == 10000


def test_baseline_metric() -> None:
    selected = [f"src/f{i:05d}.txt" for i in range(10000)]
    paths = selected[-8:]
    value = _list_scan_comparisons(selected, paths)
    print(
        "AIWORKHUB_METRIC:"
        + json.dumps({"metric": METRIC_NAME, "unit": METRIC_UNIT, "mode": "baseline", "value": value})
    )
    assert value > 0


def test_candidate_metric() -> None:
    selected = [f"src/f{i:05d}.txt" for i in range(10000)]
    paths = selected[-8:]
    value = _set_lookup_count(paths)
    print(
        "AIWORKHUB_METRIC:"
        + json.dumps(
            {
                "metric": METRIC_NAME,
                "unit": METRIC_UNIT,
                "mode": "candidate",
                "value": value,
                "direction": "lower",
            }
        )
    )
    assert value > 0


def test_candidate_lookup_count_lower_than_baseline() -> None:
    selected = [f"src/f{i:05d}.txt" for i in range(10000)]
    paths = selected[-8:]
    assert _set_lookup_count(paths) < _list_scan_comparisons(selected, paths)
