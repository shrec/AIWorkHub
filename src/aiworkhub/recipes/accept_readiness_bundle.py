#!/usr/bin/env python3
"""Whether one review card has the evidence an accept needs, and every gap if not.

A read-only pre-check: nothing is accepted, rejected, launched or written. For
the ``review`` card named by ``--task-id`` it reports

* the validation receipts of the card's latest attempt -- each command and the
  exit code it recorded -- read from that attempt's ``validation.json`` artifact
  by ``attempt_validation``'s own reader when that module can be imported, else
  by a local reader that reads the same fields;
* the ``quality_review`` lens reviewer cards bound to that exact attempt, each
  with its status and whether it is usable, against the lenses the card's sealed
  risk profile requires;
* the paths the attempt changed (``diff.json``) against the card's
  ``allowed_writes``, under the worker's own scope rule (a directory entry
  covers nothing beneath it; only a glob does).

``ready`` is true only when ``blockers`` is empty, and absent evidence is never a
pass: an unknown attempt, a missing or unreadable artifact, no receipts at all
(unless the card declares no validation and records a ``validation_exemption``),
a receipt without an integer exit code, or an unknown required-lens set each
block. ``ready`` is a pre-check over what the queue and the attempt artifacts
record, not the accept verdict: ``accept_review`` also reads the launcher's
reviewer ledger, pending context writes and the combined tree, none of which
this recipe can see. Artifacts are read as stored; their manifest digests are
not re-verified.

Read-only: an ``immutable=1`` connection that neither locks nor replays the WAL.

    python -m aiworkhub.recipes.accept_readiness_bundle --task-id ID

Blockers are ``<kind>:<detail>`` strings. Named as ``accept_review`` names them:
terminal_substatus_not_review_ready, required_reviewer_missing,
quality_reviewer_not_review_ready. This recipe's own: task_not_in_review,
card_unreadable, launch_request_id_mismatch, latest_attempt_unknown,
validation_artifact_missing, validation_artifact_unreadable,
validation_receipts_missing, validation_check_failed,
validation_receipts_incomplete, validation_receipts_short, validation_checks_truncated,
diff_artifact_missing, diff_artifact_unreadable, allowed_writes_malformed,
changed_path_outside_allowed_writes,
changed_paths_outside_allowed_writes_unlisted, required_reviewer_lenses_unknown,
explicit_human_approval_required.

Output keys: ``schema_id task_id topic status ready blockers latest_request_id
terminal_substatus validation required_lenses required_lenses_known
lens_reviewers lens_reviewer_count lens_reviewers_truncated allowed_writes
diff_artifact changed_paths changed_path_count changed_paths_truncated
changed_paths_outside_allowed_writes out_of_scope_count risk_tier
explicit_human_approval_required``. Unknown id, absent queue and owner failures
exit 2.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from typing import Any, Callable

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.accept_readiness_bundle.v1"

STATUS_REVIEW = "review"
SUBSTATUS_REVIEW_READY = "review_ready"
REVIEW_TOPIC = "quality_review"
# Restated from process_launcher_accept_review._REVIEWER_USABLE_STATES: that
# module resolves the home directory when imported, which a recipe subprocess
# must not depend on. The recipe tests pin the two together.
REVIEWER_USABLE = ("review_ready", "accepted")

ARTIFACT_OK = "ok"
ARTIFACT_MISSING = "missing"
ARTIFACT_UNREADABLE = "unreadable"
ARTIFACT_UNKNOWN_ATTEMPT = "unknown_attempt"

READER_SIBLING = "attempt_validation"
READER_LOCAL = "local"
RECEIPT_KEYS = frozenset({"command", "returncode", "timed_out", "failure_class"})

MAX_CHECKS = 100
MAX_REVIEWERS = 50
MAX_LENSES = 32
MAX_LISTED_PATHS = 200
MAX_BLOCKING_PATHS = 50
MAX_TEXT_CHARS = 200

Fold = Callable[[dict[str, Any]], dict[str, Any]]


def dict_of(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def clip(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


def latest_request_id(card: dict[str, Any], terminal: dict[str, Any]) -> tuple[str, str | None]:
    """``(request id, blocker)``: the attempt the card's review evidence belongs to.

    The terminal review records the request it was sealed for and the card's
    ``launch_request_id`` the reservation it holds. Two ids that disagree mean the
    evidence may belong to another attempt, so neither is trusted. The id also
    becomes a directory name, so only the launcher's 32-hex shape is accepted.
    """
    sealed = str(terminal.get("request_id") or "").strip()
    reserved = str(card.get("launch_request_id") or "").strip()
    if sealed and reserved and sealed != reserved:
        return "", "launch_request_id_mismatch"
    request_id = sealed or reserved
    if not common.is_request_id(request_id):
        return "", "latest_attempt_unknown"
    return request_id, None


def read_artifact(request_id: str, role: str) -> tuple[dict[str, Any] | None, str]:
    """``(payload, outcome)`` for one attempt artifact, read as stored.

    Nothing is followed through a link and nothing over the owner's size ceiling
    is read; ``outcome`` is ``ok``, ``missing`` or ``unreadable``.
    """
    name = common.owner_attr("attempt_artifacts", "ROLE_FILENAMES")[role]
    ceiling = common.owner_attr("attempt_artifacts", "MAX_ARTIFACT_BYTES")
    bundle = common.ATTEMPT_ARTIFACTS_DIR / request_id
    path = bundle / name
    try:
        if bundle.is_symlink() or path.is_symlink():
            return None, ARTIFACT_UNREADABLE
        if not path.exists():
            return None, ARTIFACT_MISSING
        if not path.is_file() or path.stat().st_size > ceiling:
            return None, ARTIFACT_UNREADABLE
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None, ARTIFACT_UNREADABLE
    if not isinstance(payload, dict):
        return None, ARTIFACT_UNREADABLE
    return payload, ARTIFACT_OK


def local_fold(check: dict[str, Any]) -> dict[str, Any]:
    """The receipt fields of one check, read exactly as ``attempt_validation`` reads them."""
    receipt = dict_of(check.get("failure_receipt"))
    argv = check.get("argv")
    declared = check.get("declared_argv")
    return {
        "command": check.get("command"),
        "argv": argv if isinstance(argv, list) else None,
        "declared_argv": declared if isinstance(declared, list) else None,
        "returncode": check.get("returncode"),
        "timed_out": receipt.get("timed_out"),
        "failure_class": receipt.get("failure_class"),
    }


def check_reader() -> tuple[str, Fold]:
    """``(name, fold)``: ``attempt_validation``'s check reader when importable, else local.

    The sibling is probed once on an empty check; a missing module, a missing
    name, a changed signature or a result without the receipt fields all mean
    the local reader. ``1`` is the tail length asked for: only the receipt
    fields are used here, and ``bounded_text`` reads ``0`` as "unbounded".
    """
    try:
        reader = importlib.import_module(f"{__package__}.attempt_validation")._fold_check
        probe = reader({}, 1)
    except (ImportError, AttributeError, TypeError):
        return READER_LOCAL, local_fold
    if not isinstance(probe, dict) or not RECEIPT_KEYS.issubset(probe):
        return READER_LOCAL, local_fold
    return READER_SIBLING, lambda check: reader(check, 1)


def display_command(folded: dict[str, Any]) -> str:
    command = folded.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    for key in ("declared_argv", "argv"):
        argv = folded.get(key)
        if isinstance(argv, list) and argv and all(isinstance(part, str) for part in argv):
            return " ".join(argv)
    return ""


def receipt_view(check: Any, fold: Fold, passes: Callable[[Any], bool]) -> dict[str, Any]:
    """One check as ``{command, exit_code, passed, timed_out, failure_class}``.

    ``passed`` is True for an integer exit code the owner's own rule
    (``quality_evidence``) accepts, False for a non-zero exit or a timeout, and
    None when the receipt carries no integer exit code. A boolean is not an exit
    code: ``False == 0`` in Python, so it would otherwise read as a clean run.
    """
    folded = fold(check) if isinstance(check, dict) else {}
    returncode = folded.get("returncode")
    exit_code = (
        returncode if isinstance(returncode, int) and not isinstance(returncode, bool) else None
    )
    timed_out = folded.get("timed_out") is True
    passed: bool | None
    if timed_out:
        passed = False
    elif exit_code is None:
        passed = None
    else:
        passed = bool(passes(folded))
    failure_class = folded.get("failure_class")
    return {
        "command": clip(display_command(folded)),
        "exit_code": exit_code,
        "passed": passed,
        "timed_out": timed_out,
        "failure_class": clip(failure_class) if isinstance(failure_class, str) else None,
    }


def validation_report(card: dict[str, Any], request_id: str) -> tuple[dict[str, Any], list[str]]:
    """The attempt's validation receipts, and what in them blocks an accept."""
    commands, commands_ok = common.string_list(card.get("validation"))
    exemption = card.get("validation_exemption")
    exempt = (
        commands_ok and not commands and isinstance(exemption, str) and bool(exemption.strip())
    )
    section: dict[str, Any] = {
        "artifact": ARTIFACT_UNKNOWN_ATTEMPT,
        "reader": None,
        "exempt": exempt,
        "declared_command_count": len(commands),
        "check_count": 0,
        "counts": {"passed": 0, "failed": 0, "unproven": 0},
        "checks": [],
        "checks_truncated": False,
    }
    if not request_id:
        return section, []

    payload, outcome = read_artifact(request_id, "validation")
    checks = (payload or {}).get("checks")
    if outcome == ARTIFACT_OK and not isinstance(checks, list):
        outcome = ARTIFACT_UNREADABLE
    section["artifact"] = outcome
    if outcome != ARTIFACT_OK:
        return section, [f"validation_artifact_{outcome}"]

    reader, fold = check_reader()
    passes = common.owner_attr("quality_evidence", "_validation_receipt_passed")
    entries = [receipt_view(check, fold, passes) for check in checks[:MAX_CHECKS]]
    blockers: list[str] = []
    if not checks and not exempt:
        blockers.append("validation_receipts_missing")
    elif len(checks) < len(commands):
        # A declared command without its receipt is absent evidence, never a pass.
        blockers.append(f"validation_receipts_short:{len(checks)}/{len(commands)}")
    for index, entry in enumerate(entries):
        if entry["passed"] is False:
            label = entry["command"] or f"check_{index}"
            cause = "timed_out" if entry["timed_out"] else f"exit_{entry['exit_code']}"
            blockers.append(f"validation_check_failed:{label}:{cause}")
    unproven = sum(1 for entry in entries if entry["passed"] is None)
    if unproven:
        blockers.append(f"validation_receipts_incomplete:{unproven}/{len(entries)}")
    if len(checks) > MAX_CHECKS:
        blockers.append(f"validation_checks_truncated:{len(checks)}")

    section.update(
        reader=reader,
        check_count=len(checks),
        counts={
            "passed": sum(1 for entry in entries if entry["passed"] is True),
            "failed": sum(1 for entry in entries if entry["passed"] is False),
            "unproven": unproven,
        },
        checks=entries,
        checks_truncated=len(checks) > MAX_CHECKS,
    )
    return section, blockers


def scope_report(card: dict[str, Any], request_id: str) -> tuple[dict[str, Any], list[str]]:
    """The attempt's changed paths against the card's ``allowed_writes``."""
    allowed, allowed_ok = common.string_list(card.get("allowed_writes"))
    scope, _invalid = common.split_write_scope(allowed)
    section: dict[str, Any] = {
        "allowed_writes": allowed,
        "diff_artifact": ARTIFACT_UNKNOWN_ATTEMPT,
        "changed_paths": [],
        "changed_path_count": 0,
        "changed_paths_truncated": False,
        "changed_paths_outside_allowed_writes": [],
        "out_of_scope_count": 0,
    }
    blockers = [] if allowed_ok else ["allowed_writes_malformed"]
    if not request_id:
        return section, blockers

    payload, outcome = read_artifact(request_id, "diff")
    changed = (payload or {}).get("changed_paths")
    if outcome == ARTIFACT_OK and not (
        isinstance(changed, list) and all(isinstance(path, str) for path in changed)
    ):
        outcome = ARTIFACT_UNREADABLE
    section["diff_artifact"] = outcome
    if outcome != ARTIFACT_OK:
        return section, [*blockers, f"diff_artifact_{outcome}"]

    outside = common.uncovered_paths(changed, scope) if allowed_ok else []
    blockers.extend(
        f"changed_path_outside_allowed_writes:{clip(path)}" for path in outside[:MAX_BLOCKING_PATHS]
    )
    if len(outside) > MAX_BLOCKING_PATHS:
        unlisted = len(outside) - MAX_BLOCKING_PATHS
        blockers.append(f"changed_paths_outside_allowed_writes_unlisted:{unlisted}")
    section.update(
        changed_paths=[clip(path) for path in changed[:MAX_LISTED_PATHS]],
        changed_path_count=len(changed),
        changed_paths_truncated=len(changed) > MAX_LISTED_PATHS,
        changed_paths_outside_allowed_writes=[clip(path) for path in outside[:MAX_LISTED_PATHS]],
        out_of_scope_count=len(outside),
    )
    return section, blockers


def required_lenses(profile: dict[str, Any]) -> tuple[list[str], bool]:
    """``(lenses, known)``: the reviewer lenses the sealed risk profile requires.

    A missing profile, one that recorded an ``error``, or lenses that are not a
    short list of non-empty strings are unknown -- never "none required".
    """
    lenses = profile.get("required_reviewer_lenses")
    if (
        not profile
        or profile.get("error")
        or not isinstance(lenses, list)
        or len(lenses) > MAX_LENSES
        or not all(isinstance(lens, str) and lens.strip() for lens in lenses)
    ):
        return [], False
    return list(dict.fromkeys(lenses)), True


def bound_reviewers(
    conn: sqlite3.Connection, task_id: str, request_id: str
) -> list[dict[str, Any]]:
    """The ``quality_review`` cards bound to exactly this task and attempt.

    The owner's rule (``bound_reviewer_rows``): the binding is the terminal
    evidence's ``quality_review`` block, else the root one; two non-empty blocks
    that disagree are no binding; and it must name both this task and this
    request. ``state`` is the card's own terminal substatus (else its lifecycle
    status): the launcher's reviewer ledger, which the owner reads for "usable",
    is not visible from the queue.
    """
    found: list[dict[str, Any]] = []
    for row in common.select_tasks(conn, topic=REVIEW_TOPIC, contains=task_id):
        reviewer_id = str(row["task_id"])
        card = common.load_card(conn, reviewer_id)
        if card is None:
            continue
        terminal = dict_of(card.get("terminal_review"))
        evidence = dict_of(terminal.get("evidence"))
        sealed = dict_of(evidence.get("quality_review"))
        root = dict_of(card.get("quality_review"))
        if sealed and root and sealed != root:
            continue
        binding = sealed or root
        if (
            str(binding.get("target_task_id") or "") != task_id
            or str(binding.get("target_request_id") or "") != request_id
        ):
            continue
        substatus = str(terminal.get("substatus") or "")
        status = common.canonical_status(row)
        found.append(
            {
                "task_id": reviewer_id,
                "lens": str(binding.get("lens") or ""),
                "status": status,
                "substatus": clip(substatus),
                "state": clip(substatus or status or "unknown"),
                "usable": substatus in REVIEWER_USABLE,
                "receipt_sealed": isinstance(evidence.get("quality_review_receipt"), dict),
            }
        )
    return sorted(found, key=lambda entry: (entry["lens"], entry["task_id"]))


def lens_blockers(required: list[str], reviewers: list[dict[str, Any]]) -> list[str]:
    """One blocker per required lens without a usable bound reviewer.

    ``accept_review`` calls both cases ``required_reviewer_missing``; this tells
    "no reviewer was ever bound" from "one is bound but not ``review_ready``",
    because the operator's next action differs.
    """
    blockers: list[str] = []
    for lens in required:
        mine = [reviewer for reviewer in reviewers if reviewer["lens"] == lens]
        if any(reviewer["usable"] for reviewer in mine):
            continue
        if mine:
            blockers.append(f"quality_reviewer_not_review_ready:{lens}:{mine[0]['state']}")
        else:
            blockers.append(f"required_reviewer_missing:{lens}")
    return blockers


def readiness(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    task_id = str(row["task_id"])
    status = common.canonical_status(row)
    blockers: list[str] = []
    if status != STATUS_REVIEW:
        blockers.append(f"task_not_in_review:{status}")

    card = common.load_card(conn, task_id)
    if card is None:
        blockers.append("card_unreadable")
        card = {}
    terminal = dict_of(card.get("terminal_review"))
    substatus = clip(str(terminal.get("substatus") or ""))
    if substatus != SUBSTATUS_REVIEW_READY:
        blockers.append(
            "terminal_substatus_not_review_ready" + (f":{substatus}" if substatus else "")
        )

    request_id, request_problem = latest_request_id(card, terminal)
    if request_problem is not None:
        blockers.append(request_problem)

    validation, found = validation_report(card, request_id)
    blockers.extend(found)
    changes, found = scope_report(card, request_id)
    blockers.extend(found)

    profile = dict_of(dict_of(dict_of(terminal.get("evidence")).get("quality_gate")).get(
        "review_risk_profile"
    ))
    required, known = required_lenses(profile)
    reviewers = bound_reviewers(conn, task_id, request_id) if request_id else []
    if not known:
        blockers.append("required_reviewer_lenses_unknown")
    elif request_id:
        blockers.extend(lens_blockers(required, reviewers))

    tier = profile.get("effective_tier")
    approval = profile.get("explicit_human_approval_required")
    if approval is True:
        # The sealed profile demands an explicit sign-off before any accept.
        blockers.append("explicit_human_approval_required")
    blockers = list(dict.fromkeys(blockers))
    return {
        "schema_id": SCHEMA_ID,
        "task_id": task_id,
        "topic": row["topic"],
        "status": status,
        "ready": not blockers,
        "blockers": blockers,
        "latest_request_id": request_id,
        "terminal_substatus": substatus,
        "validation": validation,
        "required_lenses": required,
        "required_lenses_known": known,
        "lens_reviewers": [
            {**reviewer, "required": reviewer["lens"] in required}
            for reviewer in reviewers[:MAX_REVIEWERS]
        ],
        "lens_reviewer_count": len(reviewers),
        "lens_reviewers_truncated": len(reviewers) > MAX_REVIEWERS,
        **changes,
        "risk_tier": clip(tier) if isinstance(tier, str) else None,
        "explicit_human_approval_required": approval if isinstance(approval, bool) else None,
    }


def main(argv: list[str] | None = None) -> int:
    return common.run_for_task(
        argv, __doc__, SCHEMA_ID, "exact canonical task id of the review card", readiness
    )


if __name__ == "__main__":
    sys.exit(main())
