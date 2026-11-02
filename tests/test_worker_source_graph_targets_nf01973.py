"""NF-2026-01973: Source Graph targets strip line ranges, add objective tokens.

Covers:
- a read_first entry with a trailing ``:START-END`` suffix becomes a bare path
  target;
- a tracked repo path named only in the objective becomes a target, while an
  untracked / absolute / '..' / URL-embedded path does not;
- declared read_first/immutable_inputs/allowed_writes entries keep priority
  under the MAX_TARGETS cap;
- the worker-side target check normalizes both sides.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import project_context  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as mcp  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _make_tracked_repo(tmp_path: Path, tracked: tuple[str, ...]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "tester")
    for rel in tracked:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_derive_targets_strips_trailing_line_range_suffix() -> None:
    card = {"read_first": ["src/x.py:120-180", "src/y.py:10"]}
    targets = project_context._derive_targets(card)
    assert "src/x.py" in targets
    assert "src/y.py" in targets
    assert "src/x.py:120-180" not in targets
    assert "src/y.py:10" not in targets


def test_derive_targets_dedupes_after_stripping_suffix() -> None:
    card = {"read_first": ["src/x.py:120-180"], "allowed_writes": ["src/x.py"]}
    assert project_context._derive_targets(card).count("src/x.py") == 1


def test_objective_tracked_path_token_is_appended_as_target(tmp_path) -> None:
    repo = _make_tracked_repo(tmp_path, ("src/named.py", "src/ranged.py"))
    (repo / "src" / "untracked.py").write_text("y = 2\n", encoding="utf-8")
    card = {
        "read_first": [],
        "objective": (
            "Fix the bug in src/named.py and src/ranged.py:10-20 "
            "but not src/untracked.py."
        ),
    }
    targets = project_context._derive_targets(card, repo_root=repo)
    assert "src/named.py" in targets
    assert "src/ranged.py" in targets
    assert "src/untracked.py" not in targets


def test_objective_scan_is_skipped_without_repo_root(tmp_path) -> None:
    card = {"read_first": [], "objective": "Fix src/named.py"}
    assert project_context._derive_targets(card) == []


def test_objective_unsafe_path_tokens_are_not_targets(tmp_path) -> None:
    repo = _make_tracked_repo(
        tmp_path,
        ("src/named.py", "abs/path.py", "esc/ape.py", "host/a/b.py", "a/b.py"),
    )
    card = {
        "read_first": [],
        "objective": (
            "See /abs/path.py and ../esc/ape.py and https://host/a/b.py "
            "and a/../a/b.py for details."
        ),
    }
    targets = project_context._derive_targets(card, repo_root=repo)
    assert targets == []


def test_objective_dot_slash_path_is_a_target(tmp_path) -> None:
    repo = _make_tracked_repo(tmp_path, ("src/named.py",))
    card = {"objective": "Look at ./src/named.py now."}
    assert project_context._derive_targets(card, repo_root=repo) == ["src/named.py"]


def test_declared_entries_keep_priority_under_target_cap(tmp_path) -> None:
    repo = _make_tracked_repo(tmp_path, ("src/named.py",))
    declared = [f"src/declared_{i}.py" for i in range(project_context.MAX_TARGETS)]
    card = {"read_first": declared, "objective": "Also touch src/named.py please."}
    targets = project_context._derive_targets(card, repo_root=repo)
    assert targets == declared


def test_validate_contract_forwards_repo_root_to_target_derivation(tmp_path) -> None:
    repo = _make_tracked_repo(tmp_path, ("src/named.py",))
    card = {
        "objective": "Fix src/named.py",
        "project_context": {
            "required": False,
            "source_graph": {"mode": "focus", "query": "named"},
            "session": {"topic": "t"},
        },
    }
    contract = project_context._validate_contract(card, repo)
    assert contract is not None
    assert contract["source_graph"]["targets"] == ["src/named.py"]


@pytest.mark.parametrize(
    "value",
    ["src/a.py", "src/a.py:12", "src/a.py:3-9", "src\\a.py", " src/a.py:3-9 "],
)
def test_normalize_source_graph_target(value: str) -> None:
    assert mcp._normalize_source_graph_target(value) == "src/a.py"
