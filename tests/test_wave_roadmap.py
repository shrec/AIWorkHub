from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import wave_roadmap  # noqa: E402

project = wave_roadmap.project_current_wave


def _task(task_id: str, status: str) -> dict[str, str]:
    return {"task_id": task_id, "status": status}


def _goal(goal_id: str, *task_ids: str) -> dict[str, Any]:
    return {"id": goal_id, "label": f"Goal {goal_id}", "task_ids": list(task_ids)}


def _wave(
    roadmap_id: str,
    milestone: Any,
    goals: Any,
    tasks: list[dict[str, str]] | None = None,
    *,
    status: str = "in_progress",
) -> dict[str, Any]:
    return {
        "id": roadmap_id,
        "status": status,
        "milestone": milestone,
        "provenance": {"wave_goals": goals},
        "task_ids": [task["task_id"] for task in tasks or []],
        "tasks": list(tasks or []),
    }


def _observed_wave() -> dict[str, Any]:
    """The 0.11.51 wave seen while 0.11.53 was installed: every goal unfinished."""
    return _wave(
        "RM-2026-00066",
        "0.11.51",
        [
            _goal("playbook", "PLAYBOOK_V1"),
            _goal("lsp", "LSP_INDEX_V3"),
            _goal("delta-review", "DELTA_REVIEW_V2"),
        ],
        [
            _task("PLAYBOOK_V1", "processing"),
            _task("LSP_INDEX_V3", "pending"),
            _task("DELTA_REVIEW_V2", "blocked"),
        ],
    )


def _states(result: dict[str, Any]) -> dict[str, str]:
    return {goal["id"]: goal["state"] for goal in result["goals"]}


def _single_goal_wave(goal: dict[str, Any], tasks: list[Any] | None) -> dict[str, Any]:
    return _wave("RM-2026-00066", "0.11.51", [goal], tasks)


def test_missed_target_keeps_installed_and_target_apart_with_goals_open() -> None:
    rows = [_observed_wave()]
    before = copy.deepcopy(rows)

    result = project(rows, "0.11.53")

    assert result["state"] == "ready"
    assert result["selection_reason"] == wave_roadmap.REASON_SELECTED
    assert result["wave_id"] == "RM-2026-00066"
    assert result["installed_version"] == "0.11.53"
    assert result["target_milestone"] == "0.11.51"
    assert result["overdue"] is True
    assert _states(result) == {"playbook": "open", "lsp": "open", "delta-review": "open"}
    assert result["goals"][0] == {
        "id": "playbook",
        "label": "Goal playbook",
        "state": "open",
        "tasks": [{"task_id": "PLAYBOOK_V1", "status": "processing"}],
    }
    assert rows == before
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize("installed", ["0.11.53", "0.11.54", "0.12.0", "1.0.0"])
def test_a_newer_release_never_moves_the_target_or_checks_a_goal(installed: str) -> None:
    result = project([_observed_wave()], installed)

    assert result["installed_version"] == installed
    assert result["target_milestone"] == "0.11.51"
    assert result["overdue"] is True
    assert set(_states(result).values()) == {"open"}


@pytest.mark.parametrize(
    ("installed", "overdue"),
    [
        ("0.11.49", False),
        ("0.11.51", False),
        ("v0.11.51", False),
        ("0.11.52", True),
        ("0.11.100", True),
        ("0.12.0", True),
        ("1.0.0", True),
        (" 0.11.53 ", True),
    ],
)
def test_overdue_only_once_the_installed_version_passes_the_target(
    installed: str, overdue: bool
) -> None:
    assert project([_observed_wave()], installed)["overdue"] is overdue


def test_a_finished_wave_is_not_overdue_after_its_target() -> None:
    wave = _observed_wave()
    wave["tasks"] = [_task(task["task_id"], "finished") for task in wave["tasks"]]

    result = project([wave], "0.11.53")

    assert _states(result) == {"playbook": "checked", "lsp": "checked", "delta-review": "checked"}
    assert result["overdue"] is False
    assert result["target_milestone"] == "0.11.51"


def test_highest_active_wave_is_chosen_by_version_number_not_text() -> None:
    rows = [
        _wave("RM-2026-00060", "0.11.9", [_goal("a", "T")], [_task("T", "pending")]),
        _wave("RM-2026-00061", "0.11.10", [_goal("b", "T")], [_task("T", "pending")]),
        _wave("RM-2026-00062", "0.10.99", [_goal("c", "T")], [_task("T", "pending")]),
    ]

    for ordered in (rows, list(reversed(rows))):
        result = project(ordered, "0.11.53")
        assert result["wave_id"] == "RM-2026-00061"
        assert result["target_milestone"] == "0.11.10"


def test_only_in_progress_outcomes_declaring_wave_goals_compete() -> None:
    goals = [_goal("a", "T")]
    tasks = [_task("T", "pending")]
    rows = [
        _wave("RM-2026-00050", "0.11.40", goals, tasks),
        _wave("RM-2026-00051", "0.11.90", goals, tasks, status="completed"),
        _wave("RM-2026-00052", "0.11.91", goals, tasks, status="blocked"),
        _wave("RM-2026-00053", "0.11.92", goals, tasks, status="proposed"),
        _wave("RM-2026-00054", "0.11.93", goals, tasks, status="archived"),
        {**_wave("RM-2026-00055", "0.11.94", goals, tasks), "provenance": {}},
        {**_wave("RM-2026-00056", "0.11.95", goals, tasks), "provenance": {"source": "manual"}},
        {**_wave("RM-2026-00057", "0.11.96", goals, tasks), "provenance": "wave_goals"},
    ]

    result = project(rows, "0.11.99")

    assert result["state"] == "ready"
    assert result["wave_id"] == "RM-2026-00050"


def test_duplicates_below_the_highest_target_do_not_block_selection() -> None:
    goals = [_goal("a", "T")]
    tasks = [_task("T", "pending")]
    rows = [
        _wave("RM-2026-00060", "0.11.50", goals, tasks),
        _wave("RM-2026-00061", "0.11.50", goals, tasks),
        _wave("RM-2026-00062", "0.11.51", goals, tasks),
    ]

    assert project(rows, "0.11.53")["wave_id"] == "RM-2026-00062"


@pytest.mark.parametrize("second_milestone", ["0.11.51", "v0.11.51", " 0.11.51 "])
def test_two_active_waves_at_one_target_are_unknown_not_an_arbitrary_winner(
    second_milestone: str,
) -> None:
    first = _observed_wave()
    second = _wave("RM-2026-00067", second_milestone, [_goal("x", "T")], [_task("T", "pending")])

    for rows in ([first, second], [second, first]):
        result = project(rows, "0.11.53")
        assert result["state"] == "UNKNOWN"
        assert result["selection_reason"] == wave_roadmap.REASON_AMBIGUOUS_ACTIVE_WAVE
        assert result["wave_id"] is None
        assert result["target_milestone"] is None
        assert result["overdue"] is None
        assert result["goals"] == []
        assert result["installed_version"] == "0.11.53"


@pytest.mark.parametrize(
    "installed",
    ["", "  ", "dev", "0.11", "0.11.x", "0.11.53.1", "0.11.53-rc1", "٠.١١.٥٣", "9" * 100 + ".1.1", None, 11353],
)
def test_invalid_installed_version_is_unknown(installed: Any) -> None:
    result = project([_observed_wave()], installed)

    assert result["state"] == "UNKNOWN"
    assert result["selection_reason"] == wave_roadmap.REASON_INVALID_INSTALLED_VERSION
    assert result["installed_version"] is None
    assert result["overdue"] is None


@pytest.mark.parametrize(
    "milestone", ["", "soon", "0.11", "0.11.x", "0.11.51-rc1", None, 51, ["0.11.51"]]
)
def test_an_active_wave_without_a_valid_version_is_unknown(milestone: Any) -> None:
    wave = _wave("RM-2026-00066", milestone, [_goal("a", "T")], [_task("T", "pending")])

    result = project([wave], "0.11.53")

    assert result["state"] == "UNKNOWN"
    assert result["selection_reason"] == wave_roadmap.REASON_INVALID_WAVE_VERSION
    assert result["installed_version"] == "0.11.53"


@pytest.mark.parametrize(
    "milestone",
    ["", "soon", "not-a-version", "0.11", "0.11.x", "0.11.51-rc1", None, 51, ["0.11.51"]],
)
@pytest.mark.parametrize("valid_milestone", ["0.10.99", "0.11.54", "0.99.0"])
def test_any_active_wave_without_a_valid_version_makes_the_selection_unknown(
    milestone: Any, valid_milestone: str
) -> None:
    unversioned = _wave("RM-2026-00067", milestone, [_goal("a", "T")], [_task("T", "pending")])
    versioned = _wave(
        "RM-2026-00066", valid_milestone, [_goal("b", "T")], [_task("T", "finished")]
    )
    rows = [unversioned, versioned]
    before = copy.deepcopy(rows)

    for ordered in (rows, list(reversed(rows))):
        result = project(ordered, "0.11.53")

        assert result["state"] == "UNKNOWN"
        assert result["selection_reason"] == wave_roadmap.REASON_INVALID_WAVE_VERSION
        assert result["wave_id"] is None
        assert result["target_milestone"] is None
        assert result["overdue"] is None
        assert result["goals"] == []
        assert result["installed_version"] == "0.11.53"
    assert rows == before


def test_inactive_outcomes_with_invalid_versions_do_not_block_selection() -> None:
    goals = [_goal("a", "T")]
    tasks = [_task("T", "pending")]
    rows = [
        _observed_wave(),
        _wave("RM-2026-00070", "soon", goals, tasks, status="completed"),
        _wave("RM-2026-00071", None, goals, tasks, status="blocked"),
        _wave("RM-2026-00072", "", goals, tasks, status="proposed"),
        _wave("RM-2026-00073", "0.11", goals, tasks, status="archived"),
        {**_wave("RM-2026-00074", "soon", goals, tasks), "provenance": {}},
        {**_wave("RM-2026-00075", "soon", goals, tasks), "provenance": {"source": "manual"}},
        {**_wave("RM-2026-00076", "soon", goals, tasks), "provenance": "wave_goals"},
    ]

    for ordered in (rows, list(reversed(rows))):
        result = project(ordered, "0.11.53")

        assert result["state"] == "ready"
        assert result["wave_id"] == "RM-2026-00066"


@pytest.mark.parametrize(
    "malformed", [None, 42, "RM-2026-00068", _wave("", "0.11.52", [_goal("a", "T")])]
)
def test_a_malformed_row_outranks_an_invalid_wave_version_in_any_order(malformed: Any) -> None:
    unversioned = _wave("RM-2026-00067", "soon", [_goal("a", "T")], [_task("T", "pending")])

    for rows in ([unversioned, malformed], [malformed, unversioned]):
        result = project(rows, "0.11.53")

        assert result["state"] == "UNKNOWN"
        assert result["selection_reason"] == wave_roadmap.REASON_MALFORMED_ROW


def test_an_invalid_wave_version_outranks_ambiguity_and_missing_goal_data() -> None:
    unversioned = _wave("RM-2026-00067", "soon", [_goal("a", "T")], [_task("T", "pending")])
    tied = [_observed_wave(), _wave("RM-2026-00068", "0.11.51", [_goal("x", "T")])]
    goalless = [_wave("RM-2026-00069", "0.11.52", [])]

    for others in (tied, goalless):
        for rows in ([unversioned, *others], [*others, unversioned]):
            result = project(rows, "0.11.53")

            assert result["state"] == "UNKNOWN"
            assert result["selection_reason"] == wave_roadmap.REASON_INVALID_WAVE_VERSION


@pytest.mark.parametrize(
    "rows",
    [
        [],
        (),
        None,
        [_wave("RM-2026-00066", "0.11.51", [_goal("a", "T")], status="completed")],
        [{"id": "RM-2026-00066", "status": "in_progress", "milestone": "0.11.51", "provenance": {}}],
        [{"id": "RM-2026-00066", "status": "in_progress", "milestone": "0.11.51"}],
    ],
)
def test_no_active_wave_is_unknown(rows: Any) -> None:
    result = project(rows, "0.11.53")

    assert result["state"] == "UNKNOWN"
    assert result["selection_reason"] == wave_roadmap.REASON_NO_ACTIVE_WAVE
    assert result["installed_version"] == "0.11.53"


def test_a_truncated_list_is_unknown_even_when_one_wave_is_visible() -> None:
    result = project([_observed_wave()], "0.11.53", truncated=True)

    assert result["state"] == "UNKNOWN"
    assert result["selection_reason"] == wave_roadmap.REASON_TRUNCATED
    assert result["wave_id"] is None


@pytest.mark.parametrize(
    "rows",
    [
        [None],
        ["RM-2026-00066"],
        [42],
        [_observed_wave(), None],
        [{**_observed_wave(), "id": ""}],
        [{**_observed_wave(), "id": None}],
    ],
)
def test_malformed_rows_fail_closed(rows: Any) -> None:
    result = project(rows, "0.11.53")

    assert result["state"] == "UNKNOWN"
    assert result["selection_reason"] == wave_roadmap.REASON_MALFORMED_ROW


@pytest.mark.parametrize(
    "goals",
    [
        [],
        None,
        "lsp",
        {"id": "lsp", "label": "LSP", "task_ids": ["T"]},
        [None],
        ["lsp"],
        [{"label": "LSP", "task_ids": ["T"]}],
        [{"id": "", "label": "LSP", "task_ids": ["T"]}],
        [{"id": 7, "label": "LSP", "task_ids": ["T"]}],
        [{"id": "lsp", "task_ids": ["T"]}],
        [{"id": "lsp", "label": "  ", "task_ids": ["T"]}],
        [{"id": "x" * 65, "label": "LSP", "task_ids": ["T"]}],
        [_goal("lsp", "T"), _goal("lsp", "T")],
        [_goal("lsp", "T"), _goal(" lsp", "T")],
        [_goal(f"g{index}", "T") for index in range(wave_roadmap.MAX_GOALS + 1)],
        [_goal("g", *[f"T{index}" for index in range(wave_roadmap.MAX_GOAL_TASKS + 1)])],
    ],
)
def test_missing_or_malformed_goal_data_is_unknown(goals: Any) -> None:
    wave = _wave("RM-2026-00066", "0.11.51", goals, [_task("T", "pending")])

    result = project([wave], "0.11.53")

    assert result["state"] == "UNKNOWN"
    assert result["selection_reason"] == wave_roadmap.REASON_MISSING_GOAL_DATA
    assert result["installed_version"] == "0.11.53"
    assert result["goals"] == []


def test_a_goalless_highest_wave_does_not_fall_through_to_a_lower_wave() -> None:
    rows = [
        _wave("RM-2026-00060", "0.11.50", [_goal("a", "T")], [_task("T", "pending")]),
        _wave("RM-2026-00061", "0.11.51", []),
    ]

    result = project(rows, "0.11.53")

    assert result["state"] == "UNKNOWN"
    assert result["selection_reason"] == wave_roadmap.REASON_MISSING_GOAL_DATA


def test_goal_and_task_bounds_are_inclusive() -> None:
    tasks = [_task(f"T{index}", "finished") for index in range(wave_roadmap.MAX_GOAL_TASKS)]
    task_ids = [task["task_id"] for task in tasks]
    goals = [_goal(f"g{index}", *task_ids) for index in range(wave_roadmap.MAX_GOALS)]

    result = project([_wave("RM-2026-00066", "0.11.51", goals, tasks)], "0.11.53")

    assert result["state"] == "ready"
    assert len(result["goals"]) == wave_roadmap.MAX_GOALS
    assert set(_states(result).values()) == {"checked"}
    assert result["overdue"] is False


@pytest.mark.parametrize(
    ("statuses", "state"),
    [
        (["finished"], "checked"),
        (["finished", "finished"], "checked"),
        (["pending"], "open"),
        (["processing"], "open"),
        (["review"], "open"),
        (["blocked"], "open"),
        (["archived"], "open"),
        (["superseded"], "open"),
        (["finished", "blocked"], "open"),
        (["finished", "archived"], "open"),
        (["missing"], "UNKNOWN"),
        (["finished", "missing"], "UNKNOWN"),
        (["pending", "missing"], "open"),
    ],
)
def test_goal_is_checked_only_when_every_exact_task_is_finished(
    statuses: list[str], state: str
) -> None:
    tasks = [_task(f"T{index}", status) for index, status in enumerate(statuses)]
    goal = _goal("g", *[task["task_id"] for task in tasks])

    result = project([_single_goal_wave(goal, tasks)], "0.11.53")

    assert result["state"] == "ready"
    assert _states(result) == {"g": state}
    assert result["overdue"] is (state != "checked")


@pytest.mark.parametrize(
    "tasks",
    [
        [],
        [_task("task_a", "finished")],
        [_task("TASK_A ", "finished")],
        [_task("TASK_B", "finished")],
        [_task("TASK_A", "finished"), _task("TASK_A", "finished")],
        [_task("TASK_A", "finished"), _task("TASK_A", "blocked")],
        [{"task_id": "TASK_A"}],
        [{"task_id": "TASK_A", "status": ""}],
        [{"task_id": "TASK_A", "status": None}],
        ["TASK_A"],
        None,
    ],
)
def test_absent_ambiguous_or_unreadable_task_evidence_cannot_check_a_goal(tasks: Any) -> None:
    wave = _single_goal_wave(_goal("g", "TASK_A"), [])
    wave["tasks"] = tasks

    result = project([wave], "0.11.53")

    assert result["state"] == "ready"
    assert _states(result) == {"g": "UNKNOWN"}
    assert result["overdue"] is True


@pytest.mark.parametrize(
    "task_ids",
    [[], None, "TASK_A", ["TASK_A", "TASK_A"], ["TASK_A", ""], ["TASK_A", " "], ["TASK_A", 7], ["TASK_A", None]],
)
def test_goal_without_a_clean_exact_task_binding_is_unknown(task_ids: Any) -> None:
    goal = {"id": "g", "label": "G", "task_ids": task_ids}

    result = project([_single_goal_wave(goal, [_task("TASK_A", "finished")])], "0.11.53")

    assert _states(result) == {"g": "UNKNOWN"}
    assert result["overdue"] is True


def test_known_unfinished_task_keeps_a_goal_open_beside_unresolved_evidence() -> None:
    goal = {"id": "g", "label": "G", "task_ids": ["TASK_A", "TASK_B", 7]}

    result = project([_single_goal_wave(goal, [_task("TASK_A", "pending")])], "0.11.53")

    assert _states(result) == {"g": "open"}
    assert result["goals"][0]["tasks"] == [
        {"task_id": "TASK_A", "status": "pending"},
        {"task_id": "TASK_B", "status": "missing"},
    ]


def test_a_task_of_another_goal_cannot_check_this_one() -> None:
    wave = _wave(
        "RM-2026-00066",
        "0.11.51",
        [_goal("done", "T_DONE"), _goal("todo", "T_TODO")],
        [_task("T_DONE", "finished"), _task("T_TODO", "pending")],
    )

    result = project([wave], "0.11.53")

    assert _states(result) == {"done": "checked", "todo": "open"}
    assert result["overdue"] is True


def test_projection_never_mutates_or_aliases_its_input() -> None:
    rows = [_observed_wave()]
    before = copy.deepcopy(rows)

    result = project(rows, "0.11.53")
    result["goals"][0]["tasks"].append({"task_id": "X", "status": "finished"})
    result["goals"][0]["state"] = "checked"
    result["goals"].clear()

    assert rows == before


def test_labels_ids_and_statuses_stay_bounded() -> None:
    goal = {"id": "g", "label": "L" * 500, "task_ids": ["T"]}
    wave = _single_goal_wave(goal, [_task("T", "x" * 100)])
    wave["id"] = "RM-2026-00066" + "x" * 100

    result = project([wave], "0.11.53")

    assert len(result["wave_id"]) == 32
    assert len(result["goals"][0]["label"]) == 120
    assert len(result["goals"][0]["tasks"][0]["status"]) == 40


def test_selection_reasons_are_a_closed_typed_set() -> None:
    scenarios = {
        wave_roadmap.REASON_SELECTED: project([_observed_wave()], "0.11.53"),
        wave_roadmap.REASON_TRUNCATED: project([_observed_wave()], "0.11.53", truncated=True),
        wave_roadmap.REASON_INVALID_INSTALLED_VERSION: project([_observed_wave()], "dev"),
        wave_roadmap.REASON_INVALID_WAVE_VERSION: project(
            [_wave("RM-2026-00066", "soon", [_goal("a", "T")])], "0.11.53"
        ),
        wave_roadmap.REASON_NO_ACTIVE_WAVE: project([], "0.11.53"),
        wave_roadmap.REASON_AMBIGUOUS_ACTIVE_WAVE: project(
            [_observed_wave(), _wave("RM-2026-00067", "0.11.51", [_goal("a", "T")])], "0.11.53"
        ),
        wave_roadmap.REASON_MISSING_GOAL_DATA: project(
            [_wave("RM-2026-00066", "0.11.51", [])], "0.11.53"
        ),
        wave_roadmap.REASON_MALFORMED_ROW: project([None], "0.11.53"),
    }

    for reason, result in scenarios.items():
        assert result["selection_reason"] == reason
        assert (result["state"] == "ready") is (reason == wave_roadmap.REASON_SELECTED)
    assert set(scenarios) == wave_roadmap.SELECTION_REASONS
    assert len({frozenset(result) for result in scenarios.values()}) == 1


def test_snapshot_adapter_reads_items_and_the_truncated_flag() -> None:
    snapshot = {"items": [_observed_wave()], "truncated": False, "total": 1}

    assert wave_roadmap.project_snapshot_wave(snapshot, "0.11.53") == project(
        [_observed_wave()], "0.11.53"
    )
    truncated = wave_roadmap.project_snapshot_wave({**snapshot, "truncated": True}, "0.11.53")
    assert truncated["selection_reason"] == wave_roadmap.REASON_TRUNCATED
    empty = wave_roadmap.project_snapshot_wave({}, "0.11.53")
    assert empty["selection_reason"] == wave_roadmap.REASON_NO_ACTIVE_WAVE
    malformed = wave_roadmap.project_snapshot_wave({"items": {"a": 1}}, "0.11.53")
    assert malformed["selection_reason"] == wave_roadmap.REASON_MALFORMED_ROW
