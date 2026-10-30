"""NF-2026-01851: a truncated empty bodygrep page continues instead of reading as zero."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository

NEEDLE = "NF01851_LATE_NEEDLE"
PAGE_FILES = 64  # scan_file_cap for budget=1


def _repo(tmp_path: Path, filler: int) -> Path:
    root = tmp_path / "grep"
    root.mkdir()
    bootstrap_repository(root, repo_name="grep")
    for index in range(filler):
        path = root / "pkg" / f"f{index:04d}.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"def f{index}():\n    return {index}\n", encoding="utf-8")
    late = root / "zz_late" / "hit.py"
    late.parent.mkdir()
    late.write_text(f"VALUE = '{NEEDLE}'\n", encoding="utf-8")
    sg.build_index(root, incremental=False)
    return root


def _manual_first_hit(repo: Path, cursor: str | None = None, limit: int = 20):
    for _ in range(limit):
        page = sg._bodygrep_query_page(repo, NEEDLE, 1, cursor=cursor)
        if page["matches"] or not page["next_cursor"]:
            return page
        cursor = page["next_cursor"]
    raise AssertionError("manual paging did not end")


def test_hit_beyond_first_page_is_returned_in_one_call(tmp_path):
    repo = _repo(tmp_path, PAGE_FILES + 6)
    first_page = sg._bodygrep_query_page(repo, NEEDLE, 1)
    assert first_page["matches"] == [] and first_page["scan_truncated"] is True

    result = sg.bodygrep_query(repo, NEEDLE, 1)
    assert [row["file_path"] for row in result["matches"]] == ["zz_late/hit.py"]
    assert result["pages_scanned"] > 1
    assert result["files_scanned"] > PAGE_FILES
    assert "hint" not in result
    assert result["cursor"] is None

    manual = _manual_first_hit(repo)
    assert result["matches"] == manual["matches"]
    assert result["next_cursor"] == manual["next_cursor"]


def test_bound_reached_keeps_cursor_and_hint_and_paging_matches(tmp_path):
    repo = _repo(tmp_path, PAGE_FILES * 6)
    result = sg.bodygrep_query(repo, NEEDLE, 1)
    assert result["matches"] == []
    assert result["pages_scanned"] == 1 + sg.BODYGREP_EXTRA_PAGES
    assert result["files_scanned"] == PAGE_FILES * result["pages_scanned"]
    assert result["scan_truncated"] is True
    assert result["next_cursor"]
    assert result["hint"].startswith(
        f"0 hits in the first {result['files_scanned']} of "
    )
    assert result["hint"].endswith("indexed files; pass next_cursor or a target prefix")

    resumed = sg.bodygrep_query(repo, NEEDLE, 1, cursor=result["next_cursor"])
    assert [row["file_path"] for row in resumed["matches"]] == ["zz_late/hit.py"]
    assert resumed["cursor"] == result["next_cursor"]
    manual = _manual_first_hit(repo)
    assert resumed["matches"] == manual["matches"]
    assert resumed["next_cursor"] == manual["next_cursor"]


def test_time_bound_stops_continuation(tmp_path, monkeypatch):
    repo = _repo(tmp_path, PAGE_FILES * 3)
    monkeypatch.setattr(sg, "BODYGREP_EXTRA_PAGES_SECONDS", 0.0)
    result = sg.bodygrep_query(repo, NEEDLE, 1)
    assert result["pages_scanned"] == 1
    assert result["next_cursor"] and result["hint"]


def test_complete_scan_with_no_hit_is_a_real_zero(tmp_path):
    repo = _repo(tmp_path, 3)
    result = sg.bodygrep_query(repo, "ABSENT_NF01851_TERM", 1)
    assert result["matches"] == []
    assert result["scan_truncated"] is False
    assert result["next_cursor"] is None
    assert result["pages_scanned"] == 1
    assert "hint" not in result


@pytest.mark.parametrize("budget", [1, 8])
def test_page_with_hits_is_not_continued(tmp_path, budget):
    repo = _repo(tmp_path, 3)
    result = sg.bodygrep_query(repo, NEEDLE, budget)
    assert len(result["matches"]) == 1
    assert result["pages_scanned"] == 1
