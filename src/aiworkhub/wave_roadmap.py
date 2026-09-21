"""Read-only current-wave projection: pure functions, no store access, no writes."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

READY = "ready"
UNKNOWN = "UNKNOWN"

REASON_SELECTED = "unique_highest_active_wave"
REASON_TRUNCATED = "truncated_roadmap"
REASON_INVALID_INSTALLED_VERSION = "invalid_installed_version"
REASON_INVALID_WAVE_VERSION = "invalid_wave_version"
REASON_NO_ACTIVE_WAVE = "no_active_wave"
REASON_AMBIGUOUS_ACTIVE_WAVE = "ambiguous_active_wave"
REASON_MISSING_GOAL_DATA = "missing_goal_data"
REASON_MALFORMED_ROW = "malformed_roadmap_row"
SELECTION_REASONS = frozenset(
    {
        REASON_SELECTED,
        REASON_TRUNCATED,
        REASON_INVALID_INSTALLED_VERSION,
        REASON_INVALID_WAVE_VERSION,
        REASON_NO_ACTIVE_WAVE,
        REASON_AMBIGUOUS_ACTIVE_WAVE,
        REASON_MISSING_GOAL_DATA,
        REASON_MALFORMED_ROW,
    }
)

MAX_GOALS = 24
MAX_GOAL_TASKS = 16
_MAX_VERSION_CHARS = 64
_MAX_GOAL_ID_CHARS = 64
_MAX_TASK_ID_CHARS = 256
_MAX_LABEL_CHARS = 120
_MAX_STATUS_CHARS = 40
_VERSION = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")


def _version(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str) or len(value) > _MAX_VERSION_CHARS:
        return None
    match = _VERSION.fullmatch(value.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _unknown(reason: str, installed: str | None = None) -> dict[str, Any]:
    return {
        "state": UNKNOWN,
        "selection_reason": reason,
        "wave_id": None,
        "installed_version": installed,
        "target_milestone": None,
        "overdue": None,
        "goals": [],
    }


def _task_statuses(tasks: object) -> dict[str, list[object]]:
    statuses: dict[str, list[object]] = {}
    for task in tasks if isinstance(tasks, (list, tuple)) else ():
        if isinstance(task, Mapping) and isinstance(task.get("task_id"), str):
            statuses.setdefault(task["task_id"], []).append(task.get("status"))
    return statuses


def _project_goal(
    goal_id: str, label: str, task_ids: object, statuses: Mapping[str, list[object]]
) -> dict[str, Any] | None:
    listed = list(task_ids) if isinstance(task_ids, (list, tuple)) else []
    if len(listed) > MAX_GOAL_TASKS:
        return None
    tasks: list[dict[str, str]] = []
    seen: set[str] = set()
    unresolved = not listed
    unfinished = False
    for task_id in listed:
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or len(task_id) > _MAX_TASK_ID_CHARS
            or task_id in seen
        ):
            unresolved = True
            continue
        seen.add(task_id)
        observed = statuses.get(task_id, [])
        if not observed:
            status = "missing"
        elif len(observed) > 1 or not isinstance(observed[0], str) or not observed[0].strip():
            status = "ambiguous"
        else:
            status = observed[0][:_MAX_STATUS_CHARS]
        tasks.append({"task_id": task_id, "status": status})
        if status in ("missing", "ambiguous"):
            unresolved = True
        elif status != "finished":
            unfinished = True
    # A known unfinished task keeps the goal open even beside unresolved evidence.
    state = "open" if unfinished else "UNKNOWN" if unresolved else "checked"
    return {
        "id": goal_id,
        "label": label.strip()[:_MAX_LABEL_CHARS],
        "state": state,
        "tasks": tasks,
    }


def _project_goals(wave: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    declared = wave["provenance"]["wave_goals"]
    if not isinstance(declared, (list, tuple)) or not 0 < len(declared) <= MAX_GOALS:
        return None
    statuses = _task_statuses(wave.get("tasks"))
    goals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for goal in declared:
        if not isinstance(goal, Mapping):
            return None
        goal_id, label = goal.get("id"), goal.get("label")
        if (
            not isinstance(goal_id, str)
            or not goal_id.strip()
            or len(goal_id) > _MAX_GOAL_ID_CHARS
            or goal_id.strip() in seen
            or not isinstance(label, str)
            or not label.strip()
        ):
            return None
        seen.add(goal_id.strip())
        projected = _project_goal(goal_id, label, goal.get("task_ids"), statuses)
        if projected is None:
            return None
        goals.append(projected)
    return goals


def project_current_wave(
    rows: Sequence[Mapping[str, Any]],
    installed_version: str,
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    """Select the one authoritative active wave, or return a typed UNKNOWN."""
    installed = _version(installed_version)
    installed_text = installed_version.strip() if installed is not None else None
    if truncated:
        return _unknown(REASON_TRUNCATED, installed_text)
    if installed is None:
        return _unknown(REASON_INVALID_INSTALLED_VERSION)
    invalid_version = False
    versioned: list[tuple[tuple[int, ...], Mapping[str, Any]]] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            return _unknown(REASON_MALFORMED_ROW, installed_text)
        provenance = row.get("provenance")
        if (
            row.get("status") != "in_progress"
            or not isinstance(provenance, Mapping)
            or "wave_goals" not in provenance
        ):
            continue
        if not isinstance(row.get("id"), str) or not row["id"]:
            return _unknown(REASON_MALFORMED_ROW, installed_text)
        version = _version(row.get("milestone"))
        if version is None:
            invalid_version = True
        else:
            versioned.append((version, row))
    # One unversioned active wave makes "highest target" unknowable, so never rank the rest.
    if invalid_version:
        return _unknown(REASON_INVALID_WAVE_VERSION, installed_text)
    if not versioned:
        return _unknown(REASON_NO_ACTIVE_WAVE, installed_text)
    target = max(version for version, _ in versioned)
    winners = [row for version, row in versioned if version == target]
    if len(winners) != 1:
        return _unknown(REASON_AMBIGUOUS_ACTIVE_WAVE, installed_text)
    wave = winners[0]
    goals = _project_goals(wave)
    if goals is None:
        return _unknown(REASON_MISSING_GOAL_DATA, installed_text)
    return {
        "state": READY,
        "selection_reason": REASON_SELECTED,
        "wave_id": wave["id"][:32],
        "installed_version": installed_text,
        "target_milestone": wave["milestone"].strip(),
        "overdue": installed > target and any(goal["state"] != "checked" for goal in goals),
        "goals": goals,
    }


def project_snapshot_wave(snapshot: Mapping[str, Any], installed_version: str) -> dict[str, Any]:
    """Project the current wave from one ``core.roadmap_snapshot`` result."""
    return project_current_wave(
        snapshot.get("items") or (),
        installed_version,
        truncated=bool(snapshot.get("truncated")),
    )
