"""Continue a reject-depth-capped card as a successor that inherits its sealed rework delta."""

from __future__ import annotations

import copy
from typing import Any

from . import core, launch_replay_guard, rework_delta_artifact, task_store, task_templates

FEEDBACK_SCHEMA_ID = "aiworkhub.rework_feedback_delta.v1"
MAX_REASON_BYTES = 8000
MAX_CHANGED_PATHS = 256
NOT_CAPPED = "continuation_not_capped"
PREDECESSOR_UNVERIFIED = "continuation_predecessor_unverified"


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


def _feedback(predecessor: dict[str, Any], reason: str) -> dict[str, Any]:
    changed = predecessor.get("changed_path_hashes")
    instruction = str(reason or "").strip().encode("utf-8")[:MAX_REASON_BYTES]
    return {
        "schema_id": FEEDBACK_SCHEMA_ID,
        "instruction": instruction.decode("utf-8", errors="ignore"),
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
        review_feedback=_feedback(predecessor, reason),
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
