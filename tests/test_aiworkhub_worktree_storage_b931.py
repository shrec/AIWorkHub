"""B931 (Phase 1): retained-worktree storage visibility + data-loss-proof cleanup.

AIWorkHub retains a task's git worktree while the task is in review and only GCs
it once the task is finished/archived, so worktrees whose tasks reach review and
are never finalized accumulate without bound (a real 245 GB disk-full incident).
``worktree_storage`` adds visibility and an explicit, dry-run-first cleanup that
removes a worktree ONLY when nothing can be lost: it is git-clean AND fully
pushed. A worktree with uncommitted or unpushed work -- which every live/review
worktree is -- is never removed. These tests build real git worktrees in each
state and prove the classification and the cleanup guarantees.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worktree_storage as ws  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def worktrees(tmp_path):
    """A base dir holding one worktree of each safety class, plus a pushed
    parent repo (a local bare 'remote' makes 'pushed' verifiable offline)."""
    remote = tmp_path / "remote.git"
    parent = tmp_path / "parent"
    base = tmp_path / "wt-base"
    base.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(parent))
    (parent / "file.txt").write_text("v1\n", encoding="utf-8")
    _git(parent, "add", "file.txt")
    _git(parent, "commit", "-m", "base")
    _git(parent, "push", "origin", "HEAD:refs/heads/main")
    _git(parent, "fetch", "origin")  # populate refs/remotes/origin/*

    def add_worktree(name: str) -> Path:
        wt = base / name / "worktree"
        wt.parent.mkdir(parents=True)
        _git(parent, "worktree", "add", "--detach", str(wt), "HEAD")
        return wt

    # 1) removable_safe: clean checkout at the pushed HEAD.
    add_worktree("W_SAFE")

    # 2) dirty: uncommitted edit to a tracked file.
    dirty = add_worktree("W_DIRTY")
    (dirty / "file.txt").write_text("uncommitted change\n", encoding="utf-8")

    # 3) unpushed: a new commit not on any remote.
    unpushed = add_worktree("W_UNPUSHED")
    (unpushed / "new.txt").write_text("local work\n", encoding="utf-8")
    _git(unpushed, "add", "new.txt")
    _git(unpushed, "commit", "-m", "unpushed local work")

    # 4) orphaned: a worktree dir whose .git pointer is broken (parent gone).
    orph = base / "W_ORPHAN" / "worktree"
    orph.mkdir(parents=True)
    (orph / ".git").write_text("gitdir: /nonexistent/gitdir\n", encoding="utf-8")

    return {"base": base, "parent": parent}


def _by_id(scan):
    return {wt["id"]: wt for wt in scan["worktrees"]}


def test_scan_classifies_every_safety_state(worktrees) -> None:
    scan = ws.scan_worktrees(worktrees["base"])
    cls = {i: wt["class"] for i, wt in _by_id(scan).items()}
    assert cls["W_SAFE"] == ws.CLASS_REMOVABLE_SAFE
    assert cls["W_DIRTY"] == ws.CLASS_RETAINED_UNSAVED
    assert cls["W_UNPUSHED"] == ws.CLASS_RETAINED_UNSAVED
    assert cls["W_ORPHAN"] == ws.CLASS_ORPHANED
    # summary totals cover all four and bytes are non-negative.
    assert scan["summary"]["count"] == 4
    assert scan["summary"]["total_bytes"] >= 0


def test_plan_removes_only_safe_and_keeps_all_work(worktrees) -> None:
    plan = ws.plan_cleanup(ws.scan_worktrees(worktrees["base"]))
    remove_ids = {wt["id"] for wt in plan["would_remove"]}
    keep_ids = {wt["id"] for wt in plan["would_keep"]}
    assert remove_ids == {"W_SAFE"}
    assert keep_ids == {"W_DIRTY", "W_UNPUSHED", "W_ORPHAN"}


def test_plan_include_orphaned_adds_only_orphans(worktrees) -> None:
    plan = ws.plan_cleanup(ws.scan_worktrees(worktrees["base"]), include_orphaned=True)
    remove_ids = {wt["id"] for wt in plan["would_remove"]}
    assert remove_ids == {"W_SAFE", "W_ORPHAN"}
    # dirty/unpushed WORK is still never removable, even in orphaned mode.
    assert {wt["id"] for wt in plan["would_keep"]} == {"W_DIRTY", "W_UNPUSHED"}


def test_execute_dry_run_deletes_nothing(worktrees) -> None:
    base = worktrees["base"]
    result = ws.execute_cleanup(base=base, confirm=False)
    assert result["dry_run"] is True and result["confirmed"] is False
    assert result["reclaimed_bytes"] == 0 and result["removed"] == []
    # everything still on disk
    assert (base / "W_SAFE").exists()
    assert {wt["id"] for wt in result["would_remove"]} == {"W_SAFE"}


def test_execute_confirm_removes_only_safe(worktrees) -> None:
    base = worktrees["base"]
    result = ws.execute_cleanup(base=base, confirm=True)
    assert result["confirmed"] is True and result["ok"] is True
    assert result["removed"] == ["W_SAFE"]
    assert not (base / "W_SAFE").exists()          # removed
    assert (base / "W_DIRTY").exists()             # uncommitted work kept
    assert (base / "W_UNPUSHED").exists()          # unpushed commit kept
    assert (base / "W_ORPHAN").exists()            # orphan kept (opt-in only)
    # parent worktree registration for the removed one is pruned.
    listed = subprocess.run(
        ["git", "-C", str(worktrees["parent"]), "worktree", "list"],
        capture_output=True, text=True,
    ).stdout
    assert "W_SAFE" not in listed


def test_execute_confirm_include_orphaned_also_removes_orphan(worktrees) -> None:
    base = worktrees["base"]
    result = ws.execute_cleanup(base=base, confirm=True, include_orphaned=True)
    assert set(result["removed"]) == {"W_SAFE", "W_ORPHAN"}
    assert not (base / "W_ORPHAN").exists()
    assert (base / "W_DIRTY").exists() and (base / "W_UNPUSHED").exists()


def test_scan_missing_base_is_empty_not_error(tmp_path) -> None:
    scan = ws.scan_worktrees(tmp_path / "does-not-exist")
    assert scan["exists"] is False
    assert scan["worktrees"] == []
    assert scan["summary"]["total_bytes"] == 0
    # cleanup on a missing base is a clean no-op.
    result = ws.execute_cleanup(base=tmp_path / "does-not-exist", confirm=True)
    assert result["removed"] == [] and result["ok"] is True


def test_format_report_names_classes_and_repo(worktrees) -> None:
    text = ws.format_report(ws.scan_worktrees(worktrees["base"]))
    assert "retained worktrees" in text
    assert "4 worktrees" in text
    assert ws.CLASS_REMOVABLE_SAFE in text and ws.CLASS_ORPHANED in text


def test_cli_report_then_dry_run_then_confirm(worktrees, capsys) -> None:
    base = str(worktrees["base"])
    # report mode: read-only, exit 0
    assert ws.main(["--base", base]) == 0
    assert "retained worktrees" in capsys.readouterr().out

    # dry run: deletes nothing
    assert ws.main(["--base", base, "--cleanup"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert (worktrees["base"] / "W_SAFE").exists()

    # confirm: removes only the safe worktree
    assert ws.main(["--base", base, "--cleanup", "--confirm"]) == 0
    assert "removed 1" in capsys.readouterr().out
    assert not (worktrees["base"] / "W_SAFE").exists()
    assert (worktrees["base"] / "W_DIRTY").exists()
    assert (worktrees["base"] / "W_UNPUSHED").exists()
