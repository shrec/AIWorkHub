"""NF-2026-02396: per-finding diff context from the retained worktree.

Real temp git repos, no mocks of git except where a test injects a failing or
counting runner. The worktree is never written: the index bytes and
``git status --porcelain`` are compared before and after every call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import completion_inbox, core, review_finding_context  # noqa: E402

from test_review_packet_bound_nf02361 import _card, _preview, _reports  # noqa: E402

_REAL_RUN = subprocess.run


def _git(repo: Path, *args: str) -> bytes:
    return _REAL_RUN(
        ["git", "--no-optional-locks", "-C", str(repo), "-c", "core.autocrlf=false", *args],
        capture_output=True, check=True, timeout=60, stdin=subprocess.DEVNULL,
    ).stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", message)


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _make_repo(root: Path) -> Path:
    """Tracked file changed in two far-apart places, plus one untracked file."""
    repo = root / "wt"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = [f"line {i}" for i in range(1, 61)]
    _write(repo / "tracked.py", base)
    _git(repo, "add", "tracked.py")
    _commit(repo, "base")
    changed = list(base)
    changed[4] = "EDIT-FIVE"
    changed[49] = "EDIT-FIFTY"
    _write(repo / "tracked.py", changed)
    _write(repo / "new.py", [f"new {i}" for i in range(1, 41)])
    return repo


def _size(item: dict[str, Any]) -> int:
    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_the_hunk_containing_the_finding_line_is_chosen(tmp_path):
    repo = _make_repo(tmp_path)
    section = review_finding_context.build_section(
        [{"id": "F-1", "path": "tracked.py", "line": 50}], repo)
    item = section["items"][0]
    assert item["kind"] == "diff_hunk"
    assert item["finding_id"] == "F-1" and item["line"] == 50
    assert "EDIT-FIFTY" in item["hunk"]
    assert "EDIT-FIVE" not in item["hunk"]
    assert item["hunk_header"].startswith("@@ -47,7 +47,7 @@")


def test_the_nearest_hunk_is_chosen_when_the_line_is_outside_every_hunk(tmp_path):
    repo = _make_repo(tmp_path)
    # Hunks cover new lines 2..8 and 47..53; line 20 is 12 from the first, 27 from the second.
    section = review_finding_context.build_section(
        [{"id": "F-2", "path": "tracked.py", "line": 20}], repo)
    item = section["items"][0]
    assert item["kind"] == "diff_hunk"
    assert "EDIT-FIVE" in item["hunk"]
    assert "EDIT-FIFTY" not in item["hunk"]


def test_an_untracked_file_gets_a_new_file_window_around_the_line(tmp_path):
    repo = _make_repo(tmp_path)
    section = review_finding_context.build_section(
        [{"id": "F-3", "path": "new.py", "line": 20}], repo)
    item = section["items"][0]
    assert item["kind"] == "new_file_window"
    lines = item["hunk"].split("\n")
    assert lines[0] == "5: new 5"
    assert lines[-1] == "35: new 35"
    assert len(lines) == 31


def test_a_path_outside_the_worktree_is_refused_for_relative_and_absolute_paths(tmp_path):
    repo = _make_repo(tmp_path)
    section = review_finding_context.build_section(
        [
            {"id": "R", "path": "../x", "line": 1},
            {"id": "A", "path": str(tmp_path / "outside.py"), "line": 1},
        ],
        repo,
    )
    reasons = [item["unavailable_reason"] for item in section["items"]]
    assert reasons == ["path_outside_worktree", "path_outside_worktree"]


def test_a_path_absent_from_the_worktree_is_named(tmp_path):
    repo = _make_repo(tmp_path)
    section = review_finding_context.build_section(
        [{"id": "M", "path": "missing.py", "line": 3}], repo)
    assert section["items"][0]["unavailable_reason"] == "path_not_in_worktree"


def test_an_absent_worktree_is_named_and_never_raises(tmp_path):
    section = review_finding_context.build_section(
        [{"id": "F", "path": "tracked.py", "line": 1}], tmp_path / "absent")
    assert section == {"items": [], "unavailable_reason": "worktree_absent"}


def test_a_hunk_is_cut_to_1500_bytes_and_marked_truncated(tmp_path):
    repo = _make_repo(tmp_path)
    _write(repo / "wide.py", ["x" * 4000])
    section = review_finding_context.build_section(
        [{"id": "W", "path": "wide.py", "line": 1}], repo)
    item = section["items"][0]
    assert item["truncated"] is True
    assert len(item["hunk"].encode("utf-8")) <= 1500


def test_the_section_stops_at_8000_bytes_and_records_items_omitted(tmp_path):
    repo = _make_repo(tmp_path)
    rows = []
    for i in range(12):
        _write(repo / f"big{i:02d}.py", ["y" * 2000])
        rows.append({"id": f"B-{i:02d}", "path": f"big{i:02d}.py", "line": 1})
    section = review_finding_context.build_section(rows, repo)
    assert section["truncated"] is True
    assert section["items_omitted"] >= 1
    assert len(section["items"]) + section["items_omitted"] == 12
    assert sum(_size(item) for item in section["items"]) <= 8000
    # Items are kept in input order; once one is omitted, all later ones are.
    assert [item["finding_id"] for item in section["items"]] == [
        f"B-{i:02d}" for i in range(len(section["items"]))
    ]


def test_two_findings_on_one_path_run_git_once(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    calls: list[list[str]] = []

    def counting_run(argv, **kwargs):
        if "diff" in argv:
            calls.append(list(argv))
        return _REAL_RUN(argv, **kwargs)

    monkeypatch.setattr(review_finding_context.subprocess, "run", counting_run)
    section = review_finding_context.build_section(
        [
            {"id": "A", "path": "tracked.py", "line": 50},
            {"id": "B", "path": "tracked.py", "line": 5},
            {"id": "C", "path": "new.py", "line": 3},
        ],
        repo,
    )
    assert len(calls) == 2
    assert [item["finding_id"] for item in section["items"]] == ["A", "B", "C"]


def test_the_worktree_index_and_porcelain_status_are_unchanged(tmp_path):
    repo = _make_repo(tmp_path)
    before_status = _git(repo, "status", "--porcelain")
    before_index = (repo / ".git" / "index").read_bytes()
    assert b"tracked.py" in before_status and b"new.py" in before_status
    review_finding_context.build_section(
        [
            {"id": "A", "path": "tracked.py", "line": 50},
            {"id": "C", "path": "new.py", "line": 3},
        ],
        repo,
    )
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert _git(repo, "status", "--porcelain") == before_status


def test_a_git_failure_is_a_per_item_reason_and_never_raises(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)

    def failing_run(argv, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(review_finding_context.subprocess, "run", failing_run)
    section = review_finding_context.build_section(
        [{"id": "A", "path": "tracked.py", "line": 50}], repo)
    assert section["items"][0]["unavailable_reason"] == "git_diff_failed:FileNotFoundError"


def test_a_non_dict_row_is_a_per_item_reason_and_the_rest_still_build(tmp_path):
    repo = _make_repo(tmp_path)
    section = review_finding_context.build_section(
        ["not-a-row", {"id": "A", "path": "tracked.py", "line": 50}], repo)
    assert section["items"][0]["unavailable_reason"] == "invalid_row"
    assert section["items"][1]["kind"] == "diff_hunk"


@pytest.fixture
def review_repo(tmp_path, monkeypatch):
    """The request's worktree at the canonical path, with core.repo_root pointed at tmp_path."""
    repo = tmp_path / ".aiworkhub" / "runtime" / "worktrees" / "R-1" / "worktree"
    (repo / "src" / "aiworkhub").mkdir(parents=True)
    _git(repo, "init", "-q")
    for name in ("mod_0.py", "mod_1.py"):
        _write(repo / "src" / "aiworkhub" / name, [f"code {i}" for i in range(1, 21)])
    _git(repo, "add", ".")
    _commit(repo, "base")
    lines = [f"code {i}" for i in range(1, 21)]
    lines[9] = "EDIT-TEN"
    _write(repo / "src" / "aiworkhub" / "mod_0.py", lines)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return repo


def _packet(max_bytes: int) -> dict[str, Any]:
    return completion_inbox.review_packet(
        "R-1", card=_card(), task_id="T-1", accept_preview=_preview(),
        reviewer_reports=_reports(2), max_bytes=max_bytes,
    )


def test_the_packet_carries_finding_diff_context_when_it_fits(review_repo):
    packet = _packet(60000)
    assert packet["truncation"]["within_bound"] is True
    assert "finding_diff_context" in packet
    assert "finding_diff_context" not in packet["truncation"]["dropped_sections"]
    by_id = {item["finding_id"]: item for item in packet["finding_diff_context"]["items"]}
    assert by_id["reviewer:correctness:F-00"]["kind"] == "diff_hunk"
    assert "EDIT-TEN" in by_id["reviewer:correctness:F-00"]["hunk"]
    assert set(by_id) == {"reviewer:correctness:F-00", "reviewer:correctness:F-01"}


def test_a_tight_bound_drops_finding_diff_context_first(review_repo):
    packet = _packet(1000)
    assert packet["truncation"]["dropped_sections"][0] == "finding_diff_context"
    assert "finding_diff_context" not in packet


def test_one_byte_over_the_bound_drops_only_finding_diff_context(review_repo):
    full = _packet(60000)
    packet = _packet(full["encoded_bytes"] - 1)
    assert packet["truncation"]["dropped_sections"] == ["finding_diff_context"]
    assert packet["truncation"]["within_bound"] is True
    assert "finding_diff_context" not in packet
