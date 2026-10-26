#!/usr/bin/env python3
"""Every blocked card, its last failure reason classified, and what to do next.

Triage for the blocked queue. Each blocked card (newest first, up to
``--limit``) has its last recorded failure or blocked reason read from the card
-- ``terminal_failure.evidence`` first, then ``blocker_reason`` and the terminal
substatus, then the card's newest ``terminal_failure`` event when the card
itself names nothing -- and put into exactly one class:

    route_unavailable  validation_failed  scope_violation  quota  sandbox  unknown

A reason is split on ``:``; the earliest segment that names a known cause
decides, so free text after the reason code (a command, a path) can never
override it. Inside one segment the first matching class in precedence order
wins: route_unavailable, sandbox, quota, scope_violation, validation_failed --
an availability or environment cause outranks the generic finalizer outcome it
surfaced as. A reason naming no known cause is ``unknown``, never guessed, and
a ``cause_undetermined`` reason (the queue's "no cause was established" marker)
is never classified: the path it carries is not a cause.

Read-only: an ``immutable=1`` connection that neither locks nor replays the WAL.

    python -m aiworkhub.recipes.blocked_queue_triage [--limit N] [--topic NAME]

``--topic all`` (the default) applies no topic filter. ``next_actions`` holds the
one recommended action per class; a card's ``class`` selects it.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from typing import Any

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.blocked_queue_triage.v1"

CLASS_ROUTE_UNAVAILABLE = "route_unavailable"
CLASS_VALIDATION_FAILED = "validation_failed"
CLASS_SCOPE_VIOLATION = "scope_violation"
CLASS_QUOTA = "quota"
CLASS_SANDBOX = "sandbox"
CLASS_UNKNOWN = "unknown"
CLASSES = (
    CLASS_ROUTE_UNAVAILABLE,
    CLASS_VALIDATION_FAILED,
    CLASS_SCOPE_VIOLATION,
    CLASS_QUOTA,
    CLASS_SANDBOX,
    CLASS_UNKNOWN,
)

NEXT_ACTIONS = {
    CLASS_ROUTE_UNAVAILABLE: (
        "No usable route: wait for or restore the provider route, then requeue; "
        "retrying before then fails the same way."
    ),
    CLASS_VALIDATION_FAILED: (
        "A declared validation or required output failed: read the failing "
        "receipt in the card's terminal_failure evidence, fix the code or the "
        "validation contract, then requeue."
    ),
    CLASS_SCOPE_VIOLATION: (
        "The attempt wrote outside its allowed_writes: compare the reported "
        "paths with the card's scope, widen it only if the extra path is "
        "legitimate, then requeue."
    ),
    CLASS_QUOTA: (
        "Provider quota or rate limit: wait for the reported reset window or "
        "move the card to another provider route, then requeue."
    ),
    CLASS_SANDBOX: (
        "The sandbox or validation environment refused the attempt: fix the "
        "environment (backend, permissions, runtime) before requeueing; the "
        "card's own brief is not at fault."
    ),
    CLASS_UNKNOWN: (
        "No recognised cause: inspect the card's terminal_failure and its "
        "task_events before acting; do not requeue it unchanged."
    ),
}

STATUS_BLOCKED = "blocked"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_REASON_CHARS = 2_000
DISPLAY_REASON_CHARS = 160

_ROUTE_TOKENS = ("route_unavailable",)
_SANDBOX_TOKENS = ("sandbox", "appcontainer", "snap_confine")
_QUOTA_TOKENS = (
    "quota",
    "rate_limit",
    "rate_limited",
    "balance_exhausted",
    "session_limit",
)
_SCOPE_TOKENS = ("scope_violation", "scope_rejected", "symlink_output", "out_of_scope")
_VALIDATION_TOKENS = (
    "validation_failed",
    "required_output",
    "quality_gate",
    "behavioral_gate",
    "residual_contract",
    "research_result",
    "invalid_validation_command",
    "validation_toolchain_authority",
    "missing_output_artifact",
)

# Where a reason can live, most specific first. The same paths read a
# ``terminal_failure`` event once wrapped as {"terminal_failure": payload}.
_REASON_PATHS: tuple[tuple[str, ...], ...] = (
    ("terminal_failure", "evidence", "error"),
    ("terminal_failure", "evidence", "failure_kind"),
    ("terminal_failure", "evidence", "failure_class"),
    ("blocker_reason",),
    ("terminal_failure", "substatus"),
    ("terminal_substatus",),
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

Rules = tuple[tuple[str, tuple[str, ...]], ...]


def normalise(text: str) -> str:
    """Lower-case ``text`` with every run of other characters as one ``_``."""
    return _NON_ALNUM.sub("_", text.lower()).strip("_")


def build_rules() -> Rules:
    """Class tokens in precedence order; the sandbox set extends the owner's own.

    ``VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES`` is the launcher's list of
    validation-environment refusals, so it is read from there, not restated.
    """
    restrictions = common.owner_attr(
        "process_launcher_validation", "VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES"
    )
    restriction_tokens = tuple(
        token
        for token in (normalise(p) for p in restrictions if isinstance(p, str))
        if token
    )
    if not restriction_tokens:
        raise common.OwnerUnavailable(
            "process_launcher_validation.VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES:empty"
        )
    return (
        (CLASS_ROUTE_UNAVAILABLE, _ROUTE_TOKENS),
        (CLASS_SANDBOX, _SANDBOX_TOKENS + restriction_tokens),
        (CLASS_QUOTA, _QUOTA_TOKENS),
        (CLASS_SCOPE_VIOLATION, _SCOPE_TOKENS),
        (CLASS_VALIDATION_FAILED, _VALIDATION_TOKENS),
    )


def classify(text: str, rules: Rules) -> tuple[str, str | None]:
    """``(class, matched token)`` for one reason; ``unknown`` when none matches."""
    for segment in text.split(":"):
        padded = f"_{normalise(segment)}_"
        for class_name, tokens in rules:
            for token in tokens:
                if f"_{token}_" in padded:
                    return class_name, token
    return CLASS_UNKNOWN, None


def _dig(source: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(source, dict):
            return None
        source = source.get(key)
    return source


def reason_candidates(
    source: dict[str, Any], undetermined: str, prefix: str = ""
) -> list[tuple[str, str]]:
    """``(source path, text)`` for each usable reason in ``source``, in order.

    Only strings count, and a ``cause_undetermined`` text is dropped: it records
    that no cause was established, so it cannot be one.
    """
    marker = normalise(undetermined)
    found: list[tuple[str, str]] = []
    for path in _REASON_PATHS:
        value = _dig(source, path)
        text = value.strip()[:MAX_REASON_CHARS] if isinstance(value, str) else ""
        if not text or normalise(text.split(":")[0]) == marker:
            continue
        found.append((prefix + ".".join(path), text))
    return found


def latest_terminal_failure(
    conn: sqlite3.Connection, task_id: str
) -> dict[str, Any] | None:
    """The newest ``terminal_failure`` event's failure record, if it has one."""
    row = conn.execute(
        "SELECT payload_json FROM task_events WHERE task_id = ? AND event = ? "
        "ORDER BY event_id DESC LIMIT 1",
        (task_id, "terminal_failure"),
    ).fetchone()
    payload = common.parse_card(row[0]) if row else None
    if payload is None:
        return None
    nested = payload.get("terminal_failure")
    return nested if isinstance(nested, dict) else payload


def triage_card(
    conn: sqlite3.Connection, row: sqlite3.Row, rules: Rules, undetermined: str
) -> dict[str, Any]:
    task_id = str(row["task_id"])
    card = common.load_card(conn, task_id)
    candidates = reason_candidates(card or {}, undetermined)
    if not candidates:
        failure = latest_terminal_failure(conn, task_id)
        if failure is not None:
            candidates = reason_candidates(
                {"terminal_failure": failure}, undetermined, "task_events."
            )

    class_name, matched = CLASS_UNKNOWN, None
    source, text = "card_unreadable" if card is None else "none", ""
    for candidate_source, candidate_text in candidates:
        candidate_class, candidate_token = classify(candidate_text, rules)
        if candidate_class != CLASS_UNKNOWN:
            class_name, matched = candidate_class, candidate_token
            source, text = candidate_source, candidate_text
            break
    else:
        if candidates:
            source, text = candidates[0]

    return {
        "task_id": task_id,
        "topic": row["topic"],
        "class": class_name,
        "matched": matched,
        "reason": text[:DISPLAY_REASON_CHARS],
        "reason_truncated": len(text) > DISPLAY_REASON_CHARS,
        "reason_source": source,
        "updated_at": row["updated_at"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"max blocked cards, newest first (default: {DEFAULT_LIMIT}, max: {MAX_LIMIT})",
    )
    parser.add_argument(
        "--topic",
        default=common.NO_FILTER,
        help="exact topic, or 'all' for no filter (default: all)",
    )
    args = parser.parse_args(argv)

    limit = max(1, min(int(args.limit), MAX_LIMIT))
    topic = str(args.topic).strip() or common.NO_FILTER

    try:
        conn = common.task_queue_readonly()
    except (FileNotFoundError, sqlite3.Error) as exc:
        return common.fail(SCHEMA_ID, "task_queue_unavailable", str(exc))
    conn.row_factory = sqlite3.Row
    try:
        rules = build_rules()
        undetermined = str(
            common.owner_attr("task_store", "_UNDETERMINED_BLOCKER_PREFIX")
        )
        rows = common.select_tasks(conn, topic=topic, statuses=(STATUS_BLOCKED,))
        cards = [triage_card(conn, row, rules, undetermined) for row in rows[:limit]]
    except common.OwnerUnavailable as exc:
        return common.fail(SCHEMA_ID, "owner_unavailable", str(exc))
    except sqlite3.Error as exc:
        return common.fail(SCHEMA_ID, "query_failed", str(exc))
    finally:
        conn.close()

    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "topic_filter": topic,
            "limit": limit,
            "total_blocked_count": len(rows),
            "returned_count": len(cards),
            "truncated": len(rows) > len(cards),
            "classes": list(CLASSES),
            "class_counts": {
                name: sum(1 for card in cards if card["class"] == name)
                for name in CLASSES
            },
            "next_actions": dict(NEXT_ACTIONS),
            "cards": cards,
        }
    )


if __name__ == "__main__":
    sys.exit(main())
