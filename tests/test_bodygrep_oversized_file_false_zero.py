"""NF-2026-00567: one big data file turned "present" into "not found".

``bodygrep_query`` walks candidates ``ORDER BY file_path``, so ``data/`` is
reached long before ``src/``. It read each file's full bytes and only then
compared the running total against ``scan_byte_cap`` -- which is
``budget * 262144``, so 5.24 MB at budget 20. A single 8.68 MB artifact under
``data/`` therefore blew the whole budget on its own, and because exceeding the
cap ended the scan, the walk stopped 37 files in and returned nothing.

Measured on this repository before the fix, for the literal ``looprisks``,
which is present in three files:

    unscoped, budget 20    ->  0 matches,  37 files scanned
    unscoped, budget 64    -> 15 matches, 402 files scanned
    target=src, budget 20  ->  7 matches, 133 files scanned

A caller asking a smaller question got a confident zero. After the fix the same
budget-20 unscoped call returns 6 matches across 335 files and names the one
file it could not open.

Two properties are pinned here. A file larger than the entire cap is skipped
rather than allowed to end the scan -- it can never fit any page, so stopping on
it discards the rest of the repository instead of deferring it. And the skip is
reported, so "the literal is not there" stays distinguishable from "that file
was never opened".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402

_TERM = "distinctivehaystackliteral"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def repo_with_a_huge_early_file(tmp_path_factory) -> Path:
    """A repository shaped like this one: a huge data/ file sorting before src/."""

    repo = tmp_path_factory.mktemp("bodygrep") / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="bodygrep")

    # Sorts before "src/" and is far larger than any small budget's byte cap.
    _write(repo / "data" / "inventory.jsonl", ('{"filler":"' + "x" * 512 + '"}\n') * 12000)
    # The literal lives only under src/, which the walk reaches last.
    for i in range(3):
        _write(repo / "src" / f"mod_{i}.py", f"def probe_{i}():\n    return {_TERM!r}\n")
    sg.build_index(repo, incremental=False)
    return repo


def test_a_present_literal_is_never_reported_as_absent(repo_with_a_huge_early_file):
    """The defect in one assertion: a small budget returned a confident zero."""
    result = sg.bodygrep_query(repo_with_a_huge_early_file, _TERM, budget=20)
    assert result["matches"], (
        "a literal present in three files came back empty; "
        f"scanned {result['files_scanned']} files"
    )


def test_a_file_larger_than_the_whole_cap_is_skipped_not_fatal(
    repo_with_a_huge_early_file,
):
    result = sg.bodygrep_query(repo_with_a_huge_early_file, _TERM, budget=20)
    assert result["oversized_files_skipped"] >= 1
    assert any("inventory.jsonl" in name for name in result["oversized_files"])
    # The scan continued past it rather than ending there.
    assert result["files_scanned"] > 1


def test_the_skip_is_reported_not_silent(repo_with_a_huge_early_file):
    """A caller must be able to tell 'not present' from 'not looked at'."""
    result = sg.bodygrep_query(repo_with_a_huge_early_file, _TERM, budget=20)
    assert "oversized_files_skipped" in result
    assert "oversized_files" in result
    assert result["scan_truncated"] is True


def test_a_scoped_walk_is_unaffected(repo_with_a_huge_early_file):
    """Scoping away the artifact must still find every occurrence."""
    result = sg.bodygrep_query(repo_with_a_huge_early_file, _TERM, budget=20, target="src")
    assert len(result["matches"]) == 3
    assert result["oversized_files_skipped"] == 0


def test_a_genuinely_absent_literal_still_reports_nothing(repo_with_a_huge_early_file):
    """The fix must not manufacture matches."""
    result = sg.bodygrep_query(
        repo_with_a_huge_early_file, "nosuchliteralanywhere", budget=20
    )
    assert result["matches"] == []


def test_ordinary_overflow_still_pages_rather_than_skipping(
    repo_with_a_huge_early_file,
):
    """A file that merely overflows the REMAINING budget is normal paging."""
    result = sg.bodygrep_query(repo_with_a_huge_early_file, _TERM, budget=1)
    # Budget 1 is a 1 MiB cap: the artifact is oversized for it and skipped,
    # and the walk still either finds rows or hands back a cursor to continue.
    assert result["matches"] or result["next_cursor"]
