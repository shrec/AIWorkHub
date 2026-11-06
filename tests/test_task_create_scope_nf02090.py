"""NF-2026-02090 (field reports NF-31/NF-19/NF-40): task-creation scope contract.

1. A read_first/allowed_writes/required_outputs path inside a nested git
   repository is refused at create, naming the field and the path, instead of
   surfacing one launch turn later as ``task_repo_scope_ambiguous``.
2. ``forbidden`` and ``allowed_writes`` share one anchored grammar: a bare path
   is that exact repo-root path, only an explicit ``**/`` crosses directories,
   and text fields match whole path tokens, never substrings.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, needfix_store, task_scope_contract, task_store, task_templates

NESTED = "references/AIWorkhub"
NESTED_FILE = f"{NESTED}/src/mod.py"
TARGET = "third_party/CMakeLists.txt"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    needfix_store.initialize_repository(tmp_path)
    task_store.initialize_repository(tmp_path)
    for rel in (TARGET, "CMakeLists.txt", "src/near.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-A", "route_state": "verified"},
    )
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "ok"))
    yield tmp_path
    core.previewed_plan_cache_invalidate()


def _nested_git_repo(root: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git executable not available to create a nested repository")
    nested = root / NESTED
    nested.mkdir(parents=True)
    subprocess.run([git, "init", "-q", str(nested)], check=True, capture_output=True)
    target = root / NESTED_FILE
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    pass\n", encoding="utf-8")


def _create(task_id: str, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        task_id=task_id,
        title="NF-02090 scope contract card",
        runner="claude-worker",
        topic="coding",
        objective="Adjust the vendored build file.",
        acceptance=["builds"],
        allowed_writes=[TARGET],
        required_outputs=[TARGET],
        validation=["python -m pytest -q tests/test_build.py"],
        custom_template_escape=task_templates.AUDITED_CUSTOM_ESCAPE,
        callback_required=False,
    )
    kwargs.update(overrides)
    return core.create_task(**kwargs)


# --- 1. nested git repository ------------------------------------------------


def test_read_first_inside_nested_git_repository_is_refused_at_create(repo: Path) -> None:
    _nested_git_repo(repo)

    result = _create("NF02090-NESTED", read_first=[NESTED_FILE])

    assert result["ok"] is False, result
    assert result["stderr"] == f"task_path_in_nested_repository:read_first:{NESTED_FILE}"
    assert result["field"] == "read_first"
    assert result["path"] == NESTED_FILE
    assert result["nested_repository"] == NESTED


def test_nested_repository_refusal_names_each_scope_field(repo: Path) -> None:
    _nested_git_repo(repo)
    for field in task_scope_contract.NESTED_REPOSITORY_FIELDS:
        refusal = task_scope_contract.nested_repository_refusal(repo, {field: [NESTED_FILE]})
        assert refusal is not None
        assert refusal["stderr"] == f"task_path_in_nested_repository:{field}:{NESTED_FILE}"
    assert task_scope_contract.nested_repository_refusal(repo, {"read_first": ["src/near.py"]}) is None
    reason = task_scope_contract.creation_scope_reason(repo, {"allowed_writes": [NESTED_FILE]})
    assert reason == f"task_path_in_nested_repository:allowed_writes:{NESTED_FILE}"


def test_read_first_line_range_inside_nested_repository_is_refused(repo: Path) -> None:
    _nested_git_repo(repo)
    refusal = task_scope_contract.nested_repository_refusal(
        repo, {"read_first": [f"{NESTED_FILE}:1-2"]}
    )
    assert refusal is not None
    assert refusal["path"] == NESTED_FILE


# --- 2. anchored forbidden grammar ---------------------------------------------


def test_bare_forbidden_root_file_does_not_collide_with_nested_same_name(repo: Path) -> None:
    result = _create("NF02090-BARE", forbidden=["CMakeLists.txt"])

    assert result.get("created") is True, result


def test_explicit_double_star_forbidden_collides_and_names_both_fields(repo: Path) -> None:
    result = _create("NF02090-STAR", forbidden=["**/CMakeLists.txt"])

    assert result["ok"] is False, result
    assert result["stderr"] == "contradictory_task_path_contract"
    fields = {row["field"] for row in result["conflicts"]}
    assert fields == {"allowed_writes", "required_outputs"}
    assert f"forbidden=**/CMakeLists.txt~allowed_writes={TARGET}" in result["conflict_detail"]
    assert all(row["path"] == TARGET for row in result["conflicts"])
    assert all("objective" not in label for label in result["conflict_detail"])


def test_objective_mentioning_nested_path_is_not_a_bare_forbidden_match(repo: Path) -> None:
    result = _create(
        "NF02090-TEXT",
        objective=f"Edit {TARGET} so the vendored build links.",
        forbidden=["CMakeLists.txt"],
    )

    assert result.get("created") is True, result


def test_objective_naming_the_forbidden_root_path_is_refused_by_token(repo: Path) -> None:
    card = {
        "objective": "Do not just patch ./CMakeLists.txt, fix the module.",
        "forbidden": ["CMakeLists.txt"],
    }
    assert task_scope_contract.task_card_path_conflicts(card) == [
        {"field": "objective", "path": "CMakeLists.txt", "forbidden": "CMakeLists.txt"}
    ]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("CMakeLists.txt", TARGET, False),
        ("**/CMakeLists.txt", TARGET, True),
        ("**/CMakeLists.txt", "CMakeLists.txt", True),
        ("*.txt", "third_party/*.txt", False),
        ("third_party/*.txt", "third_party/C*.txt", True),
        ("src/*.py", "src/*.txt", False),
    ],
)
def test_forbidden_and_allowed_writes_share_one_anchored_grammar(
    left: str, right: str, expected: bool
) -> None:
    assert task_scope_contract.anchored_paths_overlap(left, right) is expected
    assert task_scope_contract.anchored_paths_overlap(right, left) is expected


def test_preview_reason_matches_create_labels(repo: Path) -> None:
    card = {"allowed_writes": [TARGET], "forbidden": ["**/CMakeLists.txt"]}
    reason = task_scope_contract.creation_scope_reason(repo, card)
    assert reason == f"contradictory_task_path_contract:forbidden=**/CMakeLists.txt~allowed_writes={TARGET}"


def test_read_first_entry_with_interior_newline_does_not_crash(repo: Path) -> None:
    assert task_scope_contract.nested_repository_refusal(repo, {"read_first": ["src/near.py\nx"]}) is None
