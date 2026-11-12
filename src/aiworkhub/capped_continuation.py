"""Continue a reject-depth-capped card as a successor that inherits its sealed rework delta."""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from . import core, launch_replay_guard, rework_delta_artifact, task_store, task_templates

FEEDBACK_SCHEMA_ID = "aiworkhub.rework_feedback_delta.v1"
MAX_REASON_BYTES = 8000
MAX_CHANGED_PATHS = 256
NOT_CAPPED = "continuation_not_capped"
PREDECESSOR_UNVERIFIED = "continuation_predecessor_unverified"
# Manager-only card key: written by ``task_store.inherit_capped_rework`` in the
# same transaction as ``rework_predecessor``; ``create_task`` has no parameter
# that could set it.
MARKER_KEY = "capped_continuation"


def continuation_marker(predecessor: Mapping[str, Any]) -> dict[str, str]:
    """The binding a successor carries to the capped card whose delta it inherits."""
    delta = predecessor.get("rework_delta")
    return {
        "predecessor_task_id": str(predecessor.get("task_id") or "").strip(),
        "predecessor_request_id": str(predecessor.get("request_id") or "").strip(),
        "delta_digest": str(
            (delta if isinstance(delta, Mapping) else {}).get("artifact_sha256") or ""
        ),
    }


def consumable_predecessor_task_id(card: Mapping[str, Any], task_id: str) -> str:
    """The predecessor task id ``card`` (stored as ``task_id``) may consume.

    The card's own id, or the capped card it continues -- but only while the
    marker names the request of the card's current ``rework_predecessor``.  A
    later rejection pins a new predecessor, so the marker stops matching and
    the card's own id applies again.  The predecessor id itself never changes:
    the sealed delta and the predecessor's process metadata are bound to it.
    """
    marker = card.get(MARKER_KEY)
    predecessor = card.get("rework_predecessor")
    if isinstance(marker, Mapping) and isinstance(predecessor, Mapping):
        request_id = str(marker.get("predecessor_request_id") or "").strip()
        bound = str(marker.get("predecessor_task_id") or "").strip()
        if bound and request_id and request_id == str(
            predecessor.get("request_id") or ""
        ).strip():
            return bound
    return task_id


def _superseded_into(
    task_id: str, predecessor: Mapping[str, Any], authority_repo: Path
) -> str:
    """The capped card ``predecessor`` is bound to, iff it was superseded into ``task_id``.

    The canonical archive of the capped card is the durable half of the
    binding: ``core.supersede_task`` records ``superseded_by`` on it, and it
    still pins the very request and sealed delta the successor inherited.
    Any other card -- unrelated, superseded elsewhere, or pinning another
    request or delta -- is not proven, and an unreadable store proves
    nothing.
    """
    owner = str(predecessor.get("task_id") or "").strip()
    request_id = str(predecessor.get("request_id") or "").strip()
    digest = continuation_marker(predecessor)["delta_digest"]
    if not owner or owner == task_id or not request_id or not digest:
        return ""
    try:
        capped = task_store.get_task(authority_repo, owner)
    except (task_store.TaskStoreError, OSError, sqlite3.Error):
        return ""
    pinned = capped.get("rework_predecessor") if isinstance(capped, Mapping) else None
    if (
        not isinstance(capped, Mapping)
        or not isinstance(pinned, Mapping)
        or str(capped.get("superseded_by") or "").strip() != task_id
        or continuation_marker(pinned)
        != {"predecessor_task_id": owner, "predecessor_request_id": request_id, "delta_digest": digest}
    ):
        return ""
    return owner


def launch_predecessor_task_id(
    card: Mapping[str, Any], task_id: str, *, authority_repo: Path
) -> str:
    """The single predecessor owner every launch-time packet builder binds to.

    :func:`consumable_predecessor_task_id` when the card's continuation marker
    vouches for its predecessor.  A successor whose marker is absent or names
    another request -- one continued before the marker existed, which
    ``inherit_capped_rework`` can never re-mark -- falls back to the canonical
    supersede record of the capped card (NF-2026-01961); otherwise the card's
    own id, so a cross-task predecessor stays refused.
    """
    owner = consumable_predecessor_task_id(card, task_id)
    predecessor = card.get("rework_predecessor")
    if owner != task_id or not isinstance(predecessor, Mapping):
        return owner
    return _superseded_into(task_id, predecessor, authority_repo) or task_id


def _task_exists(task_id: str) -> bool:
    return task_store.get_task(core.repo_root(), task_id) is not None


def successor_task_id(task_id: str, by: str = "") -> str:
    """``by`` when given, else ``<task_id>-c<N>`` with the smallest free N >= 1."""
    explicit = str(by or "").strip()
    if explicit:
        return explicit
    number = 1
    while _task_exists(f"{task_id}-c{number}"):
        number += 1
    return f"{task_id}-c{number}"


def _bounded_reason(text: str) -> str:
    return str(text or "").strip().encode("utf-8")[:MAX_REASON_BYTES].decode("utf-8", errors="ignore")


def _last_reject_reason(card: Mapping[str, Any]) -> str:
    """The predecessor's latest reject reason: ``review_feedback.instruction`` of the pending card.

    ``reject_review --to pending`` writes it verbatim (bounded at the write), so the
    successor can carry the exact fix list rather than only the supersede reason.
    """
    feedback = card.get("review_feedback")
    if not isinstance(feedback, Mapping):
        return ""
    return str(feedback.get("instruction") or "")


def _feedback(predecessor: dict[str, Any], reason: str, prior_reject_reason: str = "") -> dict[str, Any]:
    changed = predecessor.get("changed_path_hashes")
    supersede = _bounded_reason(reason)
    prior = _bounded_reason(prior_reject_reason)
    if not prior:
        instruction = supersede
    else:
        # The prior reject reason is carried verbatim; the supersede reason follows
        # it.  With no prior reason neither header is emitted.
        instruction = f"Previous reject reason:\n{prior}"
        if supersede:
            instruction += f"\n\nSupersede reason:\n{supersede}"
        # The combined text keeps the same MAX_REASON_BYTES bound as a single reason.
        instruction = _bounded_reason(instruction)
    return {
        "schema_id": FEEDBACK_SCHEMA_ID,
        "instruction": instruction,
        "predecessor_request_id": str(predecessor.get("request_id") or ""),
        "predecessor_changed_paths": sorted(
            str(path) for path in (changed if isinstance(changed, dict) else {})
        )[:MAX_CHANGED_PATHS],
        "residual_identities": [],
    }


def continue_capped(task_id: str, *, reason: str = "", by: str = "") -> dict[str, Any]:
    """Create the successor, pin the sealed delta on it, then supersede ``task_id``.

    Both refusals are raised before anything is written.
    """
    card, error = core._live_card(task_id)
    if error:
        return error
    assert card is not None
    root = core.repo_root()
    if card.get("status") != "pending" or not launch_replay_guard.reject_depth_refusal(
        card, repo=root
    ):
        raise ValueError(NOT_CAPPED)
    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, dict) or not (
        rework_delta_artifact.has_verified_rework_delta(predecessor, authority_repo=root)
    ):
        raise ValueError(PREDECESSOR_UNVERIFIED)
    successor = successor_task_id(task_id, by)
    payload = core._create_payload_from_card(card)
    provenance = payload["template_provenance"]
    if isinstance(provenance, dict) and provenance.get("template_name") == task_templates.CUSTOM_TEMPLATE_NAME:
        # create_task re-derives the audited escape from the copied contract fields.
        payload["template_provenance"] = None
        payload["custom_template_escape"] = task_templates.AUDITED_CUSTOM_ESCAPE
    created = core.create_task(task_id=successor, **payload)
    if not created.get("ok"):
        raise ValueError(f"continuation_create_failed:{successor}:{created.get('stderr', '')}")
    ok, state = task_store.inherit_capped_rework(
        root,
        successor,
        rework_predecessor=copy.deepcopy(predecessor),
        review_feedback=_feedback(predecessor, reason, _last_reject_reason(card)),
        actor=core._verified_manager_actor(),
    )
    if not ok:
        raise ValueError(f"continuation_inherit_failed:{successor}:{state}")
    result = core.supersede_task(task_id, reason=reason, by=successor)
    result["continuation"] = {
        "successor_task_id": successor,
        "predecessor_request_id": str(predecessor.get("request_id") or ""),
        "delta_digest": str((predecessor.get("rework_delta") or {}).get("artifact_sha256") or ""),
    }
    return result
