"""Current-wave projection, completion verdict and wave authoring validation: pure functions, no store access, no writes."""

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


def select_current_wave(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, str]:
    """The unique highest-milestone active wave row, or ``None`` with its typed reason.

    The one selection rule behind :func:`project_current_wave` and the
    rollover's choice of predecessor; it never reads an installed version.
    """
    invalid_version = False
    versioned: list[tuple[tuple[int, ...], Mapping[str, Any]]] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            return None, REASON_MALFORMED_ROW
        provenance = row.get("provenance")
        if (
            row.get("status") != "in_progress"
            or not isinstance(provenance, Mapping)
            or "wave_goals" not in provenance
        ):
            continue
        if not isinstance(row.get("id"), str) or not row["id"]:
            return None, REASON_MALFORMED_ROW
        version = _version(row.get("milestone"))
        if version is None:
            invalid_version = True
        else:
            versioned.append((version, row))
    # One unversioned active wave makes "highest target" unknowable, so never rank the rest.
    if invalid_version:
        return None, REASON_INVALID_WAVE_VERSION
    if not versioned:
        return None, REASON_NO_ACTIVE_WAVE
    target = max(version for version, _ in versioned)
    winners = [row for version, row in versioned if version == target]
    if len(winners) != 1:
        return None, REASON_AMBIGUOUS_ACTIVE_WAVE
    return winners[0], REASON_SELECTED


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
    wave, reason = select_current_wave(rows)
    if wave is None:
        return _unknown(reason, installed_text)
    target = _version(wave["milestone"])
    if target is None:  # select_current_wave ranks versioned rows only
        return _unknown(REASON_INVALID_WAVE_VERSION, installed_text)
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


# --- Evidence-gated completion verdict ---------------------------------------
#
# A wave may complete only when every numbered acceptance criterion is mapped to
# a goal (``acceptance_indices``, 1-based) and every exact current task of every
# goal is canonically accepted with its verifier receipt. Nothing here reads a
# version, so a release alone can never close or retarget a wave.

COMPLETION_COMPLETED = "completed"
COMPLETION_PENDING = "pending_evidence"
COMPLETION_UNKNOWN = "unknown"
TASK_ACCEPTED = "accepted"
TASK_UNFINISHED = "unfinished"
# Canonical states the ordinary lifecycle still moves forward; any other token
# is not evidence of work in flight.
_UNFINISHED_STATUSES = ("pending", "processing", "review")
# The identity ``task_engine.accept_review`` binds into the receipt it stores.
_RECEIPT_SCHEMA = "aiworkhub.accepted_outcome_receipt.v1"
_RECEIPT_ID = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_REQUEST_ID_CHARS = 128


def accepted_receipt(task_id: str, card: object) -> dict[str, str] | None:
    """This exact card's own verifier receipt identity, or ``None``.

    ``task_engine.accept_review`` stamps ``accepted_request_id``, ``accepted_by``
    and ``accepted_at`` and stores the receipt it verified for the same task and
    request. A receipt of another task or request, of a foreign schema, or
    without its sealed id is not this card's verification.
    """
    if not isinstance(card, Mapping):
        return None
    evidence = card.get("accept_evidence")
    receipt = evidence.get("accepted_outcome_receipt") if isinstance(evidence, Mapping) else None
    request_id = card.get("accepted_request_id")
    if (
        not isinstance(receipt, Mapping)
        or not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > _MAX_REQUEST_ID_CHARS
        or receipt.get("schema_id") != _RECEIPT_SCHEMA
        or receipt.get("task_id") != task_id
        or receipt.get("request_id") != request_id
        or not isinstance(receipt.get("receipt_id"), str)
        or not _RECEIPT_ID.fullmatch(receipt["receipt_id"])
        or not all(
            isinstance(card.get(field), str) and card[field].strip()
            for field in ("accepted_by", "accepted_at")
        )
    ):
        return None
    return {"request_id": request_id, "receipt_id": receipt["receipt_id"]}


def task_evidence(task_id: str, card: object, status: object) -> str:
    """Classify one exact task card, given its canonical status, for completion.

    Only ``accepted`` proves a goal and only ``unfinished`` can still become
    proof by waiting; every other token (missing, ambiguous, archived, blocked,
    superseded, unverified) is unresolved evidence.
    """
    if not isinstance(card, Mapping):
        return "missing"
    if card.get("task_id", task_id) != task_id:
        return "ambiguous"
    if status in ("archived", "blocked", "superseded"):
        return str(status)
    if status in _UNFINISHED_STATUSES:
        return TASK_UNFINISHED
    if status != "finished":
        return "ambiguous"
    # A finished card without the accept transaction's own receipt was never
    # verified, and no later wait can produce one for it.
    return TASK_ACCEPTED if accepted_receipt(task_id, card) else "unverified"


def _clean_task_ids(value: object) -> list[str] | None:
    if not isinstance(value, (list, tuple)) or not 0 < len(value) <= MAX_GOAL_TASKS:
        return None
    ids = [
        item
        for item in value
        if isinstance(item, str) and item and item.strip() == item and len(item) <= _MAX_TASK_ID_CHARS
    ]
    return ids if len(ids) == len(value) == len(set(ids)) else None


def _clean_indices(value: object, criteria: int) -> list[int] | str:
    if not isinstance(value, (list, tuple)) or not value:
        return "acceptance_unmapped_goal"
    if (
        not all(isinstance(index, int) and not isinstance(index, bool) for index in value)
        or len(set(value)) != len(value)
    ):
        return "goals_malformed"
    if not all(1 <= index <= criteria for index in value):
        return "acceptance_index_invalid"
    return sorted(value)


def completion_goals(wave: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Normalize a wave's criterion-mapped goals, or name why they cannot prove it."""
    acceptance = wave.get("acceptance")
    if (
        not isinstance(acceptance, (list, tuple))
        or not acceptance
        or not all(isinstance(item, str) and item.strip() for item in acceptance)
    ):
        return [], "acceptance_missing"
    provenance = wave.get("provenance")
    declared = provenance.get("wave_goals") if isinstance(provenance, Mapping) else None
    if not isinstance(declared, (list, tuple)) or not declared:
        return [], "goals_missing"
    if len(declared) > MAX_GOALS:
        return [], "goals_malformed"
    goals: list[dict[str, Any]] = []
    seen: set[str] = set()
    covered: set[int] = set()
    for goal in declared:
        if not isinstance(goal, Mapping):
            return [], "goals_malformed"
        goal_id = goal.get("id")
        task_ids = _clean_task_ids(goal.get("task_ids"))
        if (
            not isinstance(goal_id, str)
            or not goal_id.strip()
            or len(goal_id) > _MAX_GOAL_ID_CHARS
            or goal_id.strip() in seen
            or task_ids is None
        ):
            return [], "goals_malformed"
        indices = _clean_indices(goal.get("acceptance_indices"), len(acceptance))
        if isinstance(indices, str):
            return [], indices
        seen.add(goal_id.strip())
        covered.update(indices)
        goals.append({"id": goal_id, "acceptance_indices": indices, "task_ids": task_ids})
    if covered != set(range(1, len(acceptance) + 1)):
        return [], "acceptance_unmapped"
    return goals, ""


def wave_completion_verdict(
    wave: Mapping[str, Any], evidence: Mapping[str, str]
) -> dict[str, Any]:
    """Decide completion from mapped goals and per-task evidence; never writes.

    ``evidence`` maps each exact current task id to :func:`task_evidence`.
    Unresolved evidence outranks unfinished work, so an archived predecessor
    beside a pending task is UNKNOWN rather than merely pending.
    """
    acceptance = wave.get("acceptance")
    base: dict[str, Any] = {
        "wave_id": str(wave.get("id") or "")[:32],
        "criteria": len(acceptance) if isinstance(acceptance, (list, tuple)) else 0,
        "goals": [],
        "task_ids": [],
    }
    status = wave.get("status")
    if status == "completed":
        return {**base, "state": COMPLETION_COMPLETED, "reason": "already_completed"}
    if status != "in_progress":
        return {**base, "state": COMPLETION_UNKNOWN, "reason": "wave_not_active"}
    goals, reason = completion_goals(wave)
    if reason:
        return {**base, "state": COMPLETION_UNKNOWN, "reason": reason}
    unresolved: set[str] = set()
    unfinished: set[str] = set()
    for goal in goals:
        tasks = []
        for task_id in goal["task_ids"]:
            observed = evidence.get(task_id, "missing")
            observed = observed if isinstance(observed, str) else "ambiguous"
            tasks.append({"task_id": task_id, "evidence": observed[:_MAX_STATUS_CHARS]})
            if observed == TASK_UNFINISHED:
                unfinished.add(task_id)
            elif observed != TASK_ACCEPTED:
                unresolved.add(task_id)
        base["goals"].append(
            {"id": goal["id"], "acceptance_indices": goal["acceptance_indices"], "tasks": tasks}
        )
    if unresolved:
        return {
            **base,
            "state": COMPLETION_UNKNOWN,
            "reason": "task_evidence_unresolved",
            "task_ids": sorted(unresolved),
        }
    if unfinished:
        return {
            **base,
            "state": COMPLETION_PENDING,
            "reason": "task_evidence_pending",
            "task_ids": sorted(unfinished),
        }
    return {**base, "state": COMPLETION_COMPLETED, "reason": "all_mapped_goals_accepted"}


# --- Wave authoring validation (NF-2026-00987) --------------------------------
#
# Pure checks behind the wave authoring tools; ``roadmap_store`` performs every
# write. Each refusal is a stable token naming its cause. Nothing here can
# complete a wave: completion stays ``wave_completion_verdict`` evidence only.

GOAL_DEFINITION_KEYS = frozenset({"id", "label", "acceptance_indices"})


def milestone_refusal(milestone: object) -> str:
    """``""`` for a target version the projection can rank, else the refusal."""
    return "" if _version(milestone) is not None else "milestone_invalid"


def same_milestone(left: object, right: object) -> bool:
    """True when both name the same ``X.Y.Z`` version (``v`` prefix ignored)."""
    version = _version(left)
    return version is not None and version == _version(right)


def milestone_after(candidate: object, current: object) -> bool:
    """True when ``candidate`` is a strictly later ``X.Y.Z`` version than ``current``."""
    later, earlier = _version(candidate), _version(current)
    return later is not None and earlier is not None and later > earlier


def goal_id_refusal(goal_id: object) -> str:
    from .roadmap_store import WAVE_GOAL_ID_RE

    if (
        not isinstance(goal_id, str)
        or len(goal_id) > _MAX_GOAL_ID_CHARS
        or not WAVE_GOAL_ID_RE.fullmatch(goal_id)
    ):
        return "goal_id_invalid"
    return ""


def goal_label_refusal(label: object) -> str:
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > _MAX_LABEL_CHARS:
        return "goal_label_invalid"
    return ""


def acceptance_indices_refusal(indices: object, criteria: int) -> str:
    """``""`` when ``indices`` is omitted (None) or valid 1-based criterion indices."""
    if indices is None:
        return ""
    cleaned = _clean_indices(indices, criteria)
    return f"acceptance_indices_invalid:{cleaned}" if isinstance(cleaned, str) else ""


def _goal_record(goal: Mapping[str, Any], criteria: int) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(goal, Mapping) or not set(goal) <= GOAL_DEFINITION_KEYS:
        return None, "goals_malformed"
    indices = goal.get("acceptance_indices")
    reason = (
        goal_id_refusal(goal.get("id"))
        or goal_label_refusal(goal.get("label"))
        or acceptance_indices_refusal(indices, criteria)
    )
    if reason:
        return None, reason
    record: dict[str, Any] = {"id": goal["id"], "label": goal["label"].strip(), "task_ids": []}
    if indices is not None:
        record["acceptance_indices"] = sorted(indices)
    return record, ""


def goal_definitions(goals: object, criteria: int) -> tuple[list[dict[str, Any]], str]:
    """Normalize new ``[{id, label, acceptance_indices?}]`` goals; each starts with no task."""
    if goals is None:
        return [], ""
    if not isinstance(goals, (list, tuple)):
        return [], "goals_malformed"
    if len(goals) > MAX_GOALS:
        return [], "goal_limit"
    records: list[dict[str, Any]] = []
    for goal in goals:
        record, reason = _goal_record(goal, criteria)
        if record is None:
            return [], reason
        records.append(record)
    if len({record["id"] for record in records}) != len(records):
        return [], "goal_duplicate"
    return records, ""


def _declared_goal_task_ids(goal: object) -> list[str] | None:
    if not isinstance(goal, Mapping) or goal_id_refusal(goal.get("id")) or goal_label_refusal(
        goal.get("label")
    ):
        return None
    task_ids = goal.get("task_ids")
    if (
        not isinstance(task_ids, list)
        or len(task_ids) > MAX_GOAL_TASKS
        or not all(isinstance(task_id, str) and task_id for task_id in task_ids)
        or len(set(task_ids)) != len(task_ids)
    ):
        return None
    return list(task_ids)


def declared_goals(wave: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """The wave's stored goals when every one is well formed, else the refusal."""
    provenance = wave.get("provenance")
    declared = provenance.get("wave_goals") if isinstance(provenance, Mapping) else None
    if not isinstance(declared, list) or len(declared) > MAX_GOALS:
        return [], "goals_malformed"
    if any(_declared_goal_task_ids(goal) is None for goal in declared):
        return [], "goals_malformed"
    if len({goal["id"] for goal in declared}) != len(declared):
        return [], "goals_malformed"
    return [dict(goal) for goal in declared], ""


def goal_task_ids(wave: Mapping[str, Any]) -> list[str]:
    """Every current task id of a well-formed wave's goals, in goal order."""
    goals, _reason = declared_goals(wave)
    return list(dict.fromkeys(task_id for goal in goals for task_id in goal["task_ids"]))


def unaccepted_task_ids(task_ids: Sequence[str], evidence: Mapping[str, str]) -> list[str]:
    """Tasks whose evidence is anything but ``accepted`` (missing counts as unaccepted)."""
    return [task_id for task_id in task_ids if evidence.get(task_id) != TASK_ACCEPTED]


def carried_goals(
    predecessor: Mapping[str, Any],
    evidence: Mapping[str, str],
    acceptance: Sequence[str],
) -> tuple[list[dict[str, Any]], str]:
    """The predecessor's goals not yet proven by accepted tasks, with their task ids.

    A goal is left behind only when it has tasks and every one is ``accepted``.
    A carried goal keeps an acceptance index only where the same criterion text
    appears in the successor's ``acceptance``; it is never mapped by position.
    """
    goals, reason = declared_goals(predecessor)
    if reason:
        return [], f"predecessor_{reason}"
    old = predecessor.get("acceptance")
    old_acceptance = list(old) if isinstance(old, (list, tuple)) else []
    position: dict[str, int] = {}
    for index, text in enumerate(acceptance, 1):
        position.setdefault(text, index)
    carried: list[dict[str, Any]] = []
    for goal in goals:
        task_ids = goal["task_ids"]
        if task_ids and not unaccepted_task_ids(task_ids, evidence):
            continue
        record: dict[str, Any] = {
            "id": goal["id"],
            "label": goal["label"].strip(),
            "task_ids": list(task_ids),
            "carried_from": str(predecessor.get("id") or "")[:32],
        }
        indices = goal.get("acceptance_indices")
        remapped = sorted(
            {
                position[old_acceptance[index - 1]]
                for index in (indices if isinstance(indices, list) else [])
                if isinstance(index, int)
                and not isinstance(index, bool)
                and 1 <= index <= len(old_acceptance)
                and old_acceptance[index - 1] in position
            }
        )
        if remapped:
            record["acceptance_indices"] = remapped
        carried.append(record)
    return carried, ""


def composed_goals(
    carried: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], str]:
    """Carried goals first, then new ones: unique ids, 1..MAX_GOALS goals."""
    goals = [dict(goal) for goal in (*carried, *new)]
    if not goals:
        return [], "goals_missing"
    if len(goals) > MAX_GOALS:
        return [], "goal_limit"
    if len({goal["id"] for goal in goals}) != len(goals):
        return [], "goal_duplicate"
    return goals, ""


def upserted_goals(
    wave: Mapping[str, Any],
    goal_id: object,
    label: object,
    acceptance_indices: object,
) -> tuple[list[dict[str, Any]], str, str]:
    """``(goals, outcome, reason)``; outcome is created, updated or unchanged.

    ``acceptance_indices=None`` keeps an existing goal's mapping; a new goal
    starts with no task.
    """
    acceptance = wave.get("acceptance")
    criteria = len(acceptance) if isinstance(acceptance, (list, tuple)) else 0
    reason = (
        goal_id_refusal(goal_id)
        or goal_label_refusal(label)
        or acceptance_indices_refusal(acceptance_indices, criteria)
    )
    if reason:
        return [], "", reason
    goals, reason = declared_goals(wave)
    if reason:
        return [], "", reason
    clean_label = str(label).strip()
    indices = sorted(acceptance_indices) if isinstance(acceptance_indices, (list, tuple)) else None
    for goal in goals:
        if goal["id"] != goal_id:
            continue
        updated = {**goal, "label": clean_label}
        if indices is not None:
            updated["acceptance_indices"] = indices
        if updated == goal:
            return goals, "unchanged", ""
        return [updated if item is goal else item for item in goals], "updated", ""
    if len(goals) >= MAX_GOALS:
        return [], "", "goal_limit"
    created: dict[str, Any] = {"id": goal_id, "label": clean_label, "task_ids": []}
    if indices is not None:
        created["acceptance_indices"] = indices
    return [*goals, created], "created", ""


def removed_goals(
    wave: Mapping[str, Any], goal_id: object
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str]:
    """``(remaining goals, removed goal, reason)``; a wave keeps at least one goal."""
    reason = goal_id_refusal(goal_id)
    if reason:
        return [], None, reason
    goals, reason = declared_goals(wave)
    if reason:
        return [], None, reason
    matching = [goal for goal in goals if goal["id"] == goal_id]
    if not matching:
        return [], None, "goal_missing"
    if len(goals) == 1:
        return [], None, "wave_requires_one_goal"
    return [goal for goal in goals if goal["id"] != goal_id], matching[0], ""
