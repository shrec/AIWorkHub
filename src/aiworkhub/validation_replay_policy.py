"""Policy for provider-free validation-only replays (NF-2026-02253/02254).

One owner for two decisions the reject handler and the recovery transaction
share:

* ``replay_key`` / ``same_failure``: an infrastructure reject with
  ``failure_category=validation_environment`` earns at most ONE automatic
  replay per (task, predecessor request, failure signature).  A replay keeps
  the exact candidate bytes, so the candidate's changed-path digest identifies
  the same predecessor even after a replay request has been pinned in its
  place.  The failure signature is a digest of the terminal failure with its
  volatile identifiers (request ids, digests, long numbers) folded, so the same
  sandbox launch error measured twice yields the same signature.
* ``replay_feedback``: the recovery feedback a validation-only replay records
  when the manager typed none and the card carries no residual reviewer
  feedback.  A replay reruns the declared validation on unchanged bytes, so
  the retained predecessor itself is the actionable instruction.

Only the standard library is imported: ``task_store`` reads this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

KEY_SCHEMA_ID = "aiworkhub.automatic_validation_replay_key.v1"
# Categories whose automatic replay is capped per failure signature.  base_drift
# is not: each drift is a new canonical HEAD, never the same failure.
CAPPED_CATEGORIES: frozenset[str] = frozenset({"validation_environment"})
REPEATED_REFUSAL = "infra_reject_replay_environment_failure_repeated"
SKIP_REASON = "environment_failure_repeated"
# Bounds: the history scan, the failing rows read, and each folded text.
MAX_HISTORY_EVENTS = 256
_MAX_ROWS = 32
_MAX_TEXT_CHARS = 500
_VOLATILE = re.compile(r"[0-9A-Fa-f]{8,}")
_SPACE = re.compile(r"\s+")


def _fold(value: Any) -> str:
    """Bounded text with volatile identifiers and whitespace runs folded."""
    if value is None:
        return ""
    text = _VOLATILE.sub("#", str(value)[: _MAX_TEXT_CHARS * 4])
    return _SPACE.sub(" ", text).strip()[:_MAX_TEXT_CHARS]


def _digest(material: Any) -> str:
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def failure_signature(terminal: Mapping[str, Any] | None) -> str:
    """Digest of one terminal failure, stable across identical reruns.

    An absent or unreadable terminal folds to one constant signature, so an
    unknown failure never earns a second replay of the same candidate.
    """
    terminal = terminal if isinstance(terminal, Mapping) else {}
    evidence = terminal.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    rows = evidence.get("validation")
    failing: list[list[Any]] = []
    for row in rows[:_MAX_ROWS] if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        if row.get("timed_out") or row.get("returncode") not in (0, None):
            failing.append(
                [
                    _fold(row.get("declared_command") or row.get("command")),
                    _fold(row.get("returncode")),
                    bool(row.get("timed_out")),
                    _fold(row.get("failure_class")),
                ]
            )
    return _digest(
        {
            "substatus": _fold(terminal.get("substatus")),
            "error": _fold(evidence.get("error") or terminal.get("error")),
            "failing": failing,
        }
    )


def candidate_digest(changed_path_hashes: Any) -> str:
    """Digest of a candidate's changed-path hashes; empty when there are none."""
    if not isinstance(changed_path_hashes, Mapping) or not changed_path_hashes:
        return ""
    return _digest({str(path): str(sha) for path, sha in changed_path_hashes.items()})


def replay_key(
    task_id: str,
    predecessor: Mapping[str, Any] | None,
    terminal: Mapping[str, Any] | None,
    failure_category: str,
) -> dict[str, Any]:
    """The identity one automatic replay is recorded under."""
    predecessor = predecessor if isinstance(predecessor, Mapping) else {}
    return {
        "schema_id": KEY_SCHEMA_ID,
        "task_id": str(task_id),
        "failure_category": str(failure_category),
        "predecessor_request_id": str(predecessor.get("request_id") or "").strip(),
        "candidate_sha256": candidate_digest(predecessor.get("changed_path_hashes")),
        "failure_signature": failure_signature(terminal),
    }


def same_failure(prior: Any, key: Mapping[str, Any]) -> bool:
    """True when ``prior`` recorded a replay of this candidate for this failure."""
    if not isinstance(prior, Mapping) or prior.get("schema_id") != KEY_SCHEMA_ID:
        return False
    if any(
        prior.get(field) != key.get(field)
        for field in ("task_id", "failure_category", "failure_signature")
    ):
        return False
    return any(
        bool(key.get(field)) and prior.get(field) == key.get(field)
        for field in ("predecessor_request_id", "candidate_sha256")
    )


def replay_feedback(predecessor_request_id: str) -> str:
    """Recovery feedback for a validation-only replay that was given none."""
    return (
        "Validation-only replay of the retained predecessor "
        f"{str(predecessor_request_id).strip()}: rerun the card's declared "
        "validation on its unchanged candidate."
    )
