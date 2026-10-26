#!/usr/bin/env python3
"""Whether one pending card can win a launch, and every concrete blocker if not.

A read-only preflight: nothing is launched, claimed or written. For the card
named by ``--task-id`` it reports

* the ``required_outputs`` its ``allowed_writes`` do not cover -- the worker
  would be forbidden to write a file the card demands (a directory entry covers
  nothing beneath it; only a glob does, exactly as the worker's scope check
  treats it);
* the repository paths a ``validation`` command reads that are absent from the
  working tree and that the card does not authorise the worker to create, or
  that git does not track so the worker's worktree cannot have them;
* the validation roles the card's ``work_kind`` requires against the roles it
  declares, when the card stores them;
* every ``processing`` and ``review`` card whose ``allowed_writes`` overlap this
  card's, with the task ids and the overlapping path pairs.

``winnable`` is true only when ``blockers`` is empty. A fact that cannot be
established is itself a blocker -- an unreadable card, a malformed field, a peer
card whose scope cannot be read, a check that crashed -- never a pass. A card
that is not ``pending`` is evaluated all the same and also carries
``task_not_pending:<status>``.

Read-only: an ``immutable=1`` connection that neither locks nor replays the WAL.

    python -m aiworkhub.recipes.preflight_card_winnability --task-id ID

Blockers are ``<kind>:<detail>`` strings: task_not_pending, card_unreadable,
allowed_writes_malformed, required_outputs_malformed, validation_malformed,
allowed_write_invalid, required_output_not_covered, validation_input_missing,
validation_input_untracked, validation_input_unavailable,
validation_inputs_unverifiable, validation_roles_invalid, write_overlap,
peer_write_scope_unknown.

Output keys: ``schema_id task_id topic status winnable blockers allowed_writes
required_outputs required_outputs_not_covered validation_inputs_missing
validation_roles write_overlaps write_overlap_count peers_unreadable
peers_unreadable_count``. Unknown id, absent queue and owner failures exit 2.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.preflight_card_winnability.v1"

STATUS_PENDING = "pending"
OVERLAP_STATUSES = ("processing", "review")
MAX_LISTED_PEERS = 50
MAX_PAIRS_PER_PEER = 20


def missing_validation_inputs(
    card: dict[str, Any],
) -> tuple[list[dict[str, str]], str | None]:
    """``(findings, error)``: inputs no worker can have, or why that was undecidable."""
    check = common.owner_attr("toolchain_authority", "card_contract_missing_inputs")
    try:
        findings = check(common.REPO_ROOT, card)
    except Exception as exc:  # noqa: BLE001 - the owner parses arbitrary command text
        return [], type(exc).__name__
    return [{"path": path, "reason": reason} for path, reason in findings], None


def input_blocker_kinds() -> dict[str, str]:
    """The owner's reason strings, each mapped to the blocker kind that names it."""
    return {
        common.owner_attr(
            "toolchain_authority", "CARD_CONTRACT_INPUT_MISSING"
        ): "validation_input_missing",
        common.owner_attr(
            "toolchain_authority", "WORKER_WORKTREE_ABSENT"
        ): "validation_input_untracked",
    }


def role_contract(card: dict[str, Any], commands: list[str]) -> dict[str, Any]:
    """Declared against required validation roles for the card's ``work_kind``.

    ``normalize_behavioral_contract`` gives the verdict (``error``); the other
    fields only show its working.
    """
    normalize = common.owner_attr("quality_evidence", "normalize_behavioral_contract")
    required_by_kind = common.owner_attr("quality_evidence", "_REQUIRED_BEHAVIORAL_ROLES")
    generic = common.owner_attr("quality_evidence", "WORK_KIND_GENERIC")
    work_kind, roles = card.get("work_kind"), card.get("validation_roles")
    kind = str(work_kind or generic).strip().lower()
    declared = (
        [str(role).strip().lower()[:80] for role in roles]
        if isinstance(roles, list)
        else []
    )
    required = list(required_by_kind.get(kind, ()))
    try:
        normalize(work_kind, commands, roles)
        error = None
    except (ValueError, TypeError) as exc:
        error = str(exc)[:200]
    return {
        "stored": work_kind is not None or roles is not None,
        "work_kind": kind[:80],
        "declared": declared,
        "required": required,
        "missing": [role for role in required if role not in declared],
        "error": error,
    }


def write_overlaps(
    conn: sqlite3.Connection, task_id: str, scope: list[str]
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]]]:
    """``(overlaps, overlapping peer count, peers whose scope is unreadable)``.

    Peers are every other ``processing`` or ``review`` card, whatever its topic.
    A peer whose scope cannot be read is reported, never assumed disjoint.
    """
    conflicts = common.owner_attr("task_plan", "paths_conflict")
    overlaps: list[dict[str, Any]] = []
    overlap_count = 0
    unreadable: list[dict[str, str]] = []
    for peer in common.select_tasks(conn, statuses=OVERLAP_STATUSES):
        peer_id = str(peer["task_id"])
        if peer_id == task_id:
            continue
        peer_card = common.load_card(conn, peer_id)
        entries, well_formed = common.string_list((peer_card or {}).get("allowed_writes"))
        if peer_card is None or not well_formed:
            unreadable.append(
                {
                    "task_id": peer_id,
                    "reason": "card_unreadable"
                    if peer_card is None
                    else "allowed_writes_malformed",
                }
            )
            continue
        peer_scope, _invalid = common.split_write_scope(entries)
        pairs = [
            {"this": mine, "other": theirs}
            for mine in scope
            for theirs in peer_scope
            if conflicts(mine, theirs)
        ]
        if not pairs:
            continue
        overlap_count += 1
        overlaps.append(
            {
                "task_id": peer_id,
                "topic": peer["topic"],
                "status": common.canonical_status(peer),
                "paths": pairs[:MAX_PAIRS_PER_PEER],
                "paths_truncated": len(pairs) > MAX_PAIRS_PER_PEER,
            }
        )
    return overlaps[:MAX_LISTED_PEERS], overlap_count, unreadable


def preflight(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    task_id = str(row["task_id"])
    status = common.canonical_status(row)
    blockers: list[str] = []
    if status != STATUS_PENDING:
        blockers.append(f"task_not_pending:{status}")

    card = common.load_card(conn, task_id)
    if card is None:
        blockers.append("card_unreadable")
        card = {}

    allowed, allowed_ok = common.string_list(card.get("allowed_writes"))
    outputs, outputs_ok = common.string_list(card.get("required_outputs"))
    commands, commands_ok = common.string_list(card.get("validation"))
    for field, well_formed in (
        ("allowed_writes", allowed_ok),
        ("required_outputs", outputs_ok),
        ("validation", commands_ok),
    ):
        if not well_formed:
            blockers.append(f"{field}_malformed")

    scope, invalid_scope = common.split_write_scope(allowed)
    blockers.extend(f"allowed_write_invalid:{entry}" for entry in invalid_scope)

    uncovered = common.uncovered_paths(outputs, scope) if allowed_ok and outputs_ok else []
    blockers.extend(f"required_output_not_covered:{path}" for path in uncovered)

    inputs: list[dict[str, str]] = []
    if commands_ok:
        inputs, inputs_error = missing_validation_inputs(card)
        kinds = input_blocker_kinds()
        blockers.extend(
            f"{kinds.get(item['reason'], 'validation_input_unavailable')}:{item['path']}"
            for item in inputs
        )
        if inputs_error is not None:
            blockers.append(f"validation_inputs_unverifiable:{inputs_error}")

    roles = role_contract(card, commands)
    if commands_ok and roles["error"] is not None:
        blockers.append(f"validation_roles_invalid:{roles['error']}")

    overlaps: list[dict[str, Any]] = []
    overlap_count = 0
    unreadable: list[dict[str, str]] = []
    if scope:
        overlaps, overlap_count, unreadable = write_overlaps(conn, task_id, scope)
    blockers.extend(f"write_overlap:{item['task_id']}" for item in overlaps)
    blockers.extend(
        f"peer_write_scope_unknown:{item['task_id']}"
        for item in unreadable[:MAX_LISTED_PEERS]
    )

    blockers = list(dict.fromkeys(blockers))
    return {
        "schema_id": SCHEMA_ID,
        "task_id": task_id,
        "topic": row["topic"],
        "status": status,
        "winnable": not blockers,
        "blockers": blockers,
        "allowed_writes": allowed,
        "required_outputs": outputs,
        "required_outputs_not_covered": uncovered,
        "validation_inputs_missing": inputs,
        "validation_roles": roles,
        "write_overlaps": overlaps,
        "write_overlap_count": overlap_count,
        "peers_unreadable": unreadable[:MAX_LISTED_PEERS],
        "peers_unreadable_count": len(unreadable),
    }


def main(argv: list[str] | None = None) -> int:
    return common.run_for_task(argv, __doc__, SCHEMA_ID, "exact canonical task id", preflight)


if __name__ == "__main__":
    sys.exit(main())
