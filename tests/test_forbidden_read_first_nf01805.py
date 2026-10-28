from __future__ import annotations

from aiworkhub import core

FORBIDDEN = ["docs/legacy/**"]


def _fields(card: dict) -> set[str]:
    return {row["field"] for row in core.task_card_path_conflicts(card)}


def test_read_first_inside_forbidden_glob_is_not_a_conflict():
    card = {"forbidden": FORBIDDEN, "read_first": ["docs/legacy/a.md"]}
    assert core.task_card_path_conflicts(card) == []


def test_immutable_inputs_inside_forbidden_glob_is_not_a_conflict():
    card = {"forbidden": FORBIDDEN, "immutable_inputs": ["docs/legacy/a.md"]}
    assert core.task_card_path_conflicts(card) == []


def test_allowed_writes_inside_forbidden_glob_is_still_a_conflict():
    card = {"forbidden": FORBIDDEN, "allowed_writes": ["docs/legacy/a.md"]}
    assert core.task_card_path_conflicts(card) == [
        {"field": "allowed_writes", "path": "docs/legacy/a.md", "forbidden": "docs/legacy/**"}
    ]


def test_required_outputs_inside_forbidden_glob_is_still_a_conflict():
    card = {"forbidden": FORBIDDEN, "required_outputs": ["docs/legacy/a.md"]}
    assert core.task_card_path_conflicts(card) == [
        {"field": "required_outputs", "path": "docs/legacy/a.md", "forbidden": "docs/legacy/**"}
    ]


def test_mixed_card_reports_only_write_fields():
    card = {
        "forbidden": FORBIDDEN,
        "read_first": ["docs/legacy/a.md"],
        "immutable_inputs": ["docs/legacy/b.md"],
        "allowed_writes": ["src/x.py", "docs/legacy/c.md"],
        "required_outputs": ["src/x.py"],
    }
    assert _fields(card) == {"allowed_writes"}
