"""Manager callback-lane health and the verified owner's lane claim (NF-2026-01802).

A peer saw the manager route flip to Codex inside a Manager Chat session: new
cards recorded ``coordinator_provider=codex`` and ``route_pending``, task_create
and claude_callback_wait refused, yet the doctor reported ok and no manager tool
could repair the lane. :func:`route_health` names that split for the doctor;
:func:`claim_callback_lane` lets the verified manager take the lane back.

Every route fact is read from ``core`` at call time; this module never edits
``core`` state other than the coordinator-target file the extension also writes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import core, task_store
from .route_identity import MANAGER_PROVIDERS

CLAIM_TOOL = "aiworkhub_manager_callback_lane_claim"
TARGETS_SCHEMA_ID = "aiworkhub.coordinator_targets.v1"
OPEN_CARD_STATUSES = ("pending", "processing", "review")
CARD_IDS_MAX = 20
CARD_SCAN_LIMIT = 500  # per status; a larger open queue is reported as truncated

REASON_UNVERIFIED = "manager_route_unverified"
REASON_LANE_OWNER = "manager_route_lane_owner_mismatch"
REASON_SELECTED_TARGET = "manager_route_selected_target_mismatch"
REASON_PENDING_CARDS = "manager_route_pending_cards"
REASON_CARD_PROVIDER = "manager_route_card_provider_mismatch"
REASON_CARDS_UNREADABLE = "manager_route_cards_unreadable"

CLAIM_OK = "callback_lane_claimed"
CLAIM_UNVERIFIED = "callback_lane_claim_unverified_caller"
CLAIM_CODEX_NOT_LIVE = "callback_lane_claim_codex_route_not_live"
CLAIM_HELD_BY_MANAGER_CHAT = "callback_lane_held_by_manager_chat"
CLAIM_HELD_BY_CODEX = "callback_lane_held_by_codex"
CLAIM_TARGETS_UNREADABLE = "callback_lane_claim_targets_unreadable"
CLAIM_WRITE_FAILED = "callback_lane_claim_write_failed"

_CARD_READ_ERRORS = (OSError, RuntimeError, sqlite3.Error, task_store.TaskStoreError)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _verified_identity() -> dict[str, str]:
    """The caller's stamped manager route, ``{}`` unless provider and session are verified."""
    identity = core.stamped_manager_identity() or {}
    provider = _text(identity.get("provider")).lower()
    session_id = _text(identity.get("session_id"))
    if provider not in MANAGER_PROVIDERS or not session_id:
        return {}
    return {
        "provider": provider,
        "session_id": session_id,
        "thread_id": _text(identity.get("thread_id")),
        "window_id": _text(identity.get("window_id")),
        "route_state": _text(identity.get("route_state")).lower(),
        "callback_supported": _text(identity.get("callback_supported")).lower(),
    }


def _codex_route_live(identity: Mapping[str, Any]) -> bool:
    """A Codex route can take wakes: a real thread UUID that is not route_pending."""
    if not core._UUID_RE.fullmatch(_text(identity.get("thread_id"))):
        return False
    if (_text(identity.get("route_state")).lower() or "ready") == "route_pending":
        return False
    return (_text(identity.get("callback_supported")).lower() or "true") == "true"


def _lane_owner(provider: str) -> str:
    """The provider owning the callback lane, as claude_callback_wait would decide it."""
    if provider == "claude":
        return "claude"
    owning = core._claude_lane_refusal().get("owning_route")
    return _text(owning.get("provider")).lower() if isinstance(owning, Mapping) else ""


def _open_card_findings(provider: str) -> tuple[list[str], list[str], bool]:
    """Non-terminal cards still route_pending, and those recorded for another provider."""
    root = core.repo_root()
    readiness = task_store.storage_readiness(root)
    pending: list[str] = []
    mismatched: list[str] = []
    truncated = False
    for status in OPEN_CARD_STATUSES:
        rows = task_store.list_tasks(root, status=status, limit=CARD_SCAN_LIMIT)
        truncated = truncated or len(rows) >= CARD_SCAN_LIMIT
        for row in rows:
            task_id = _text(row.get("task_id"))
            card = task_store.get_task(root, task_id, readiness=readiness) if task_id else None
            if not isinstance(card, Mapping):
                continue
            if _text(card.get("manager_route_state")).lower() == "route_pending":
                pending.append(task_id)
            card_provider = _text(card.get("coordinator_provider")).lower()
            if card_provider and card_provider != provider:
                mismatched.append(task_id)
    return sorted(pending), sorted(mismatched), truncated


def route_health() -> dict[str, Any]:
    """READ-ONLY: does the stamped manager own the lane, the selected target and its open cards?

    ``ok`` is False with the first stable ``reason`` (all in ``reasons``) when the
    stamped provider differs from the callback-lane owner or a non-empty selected
    coordinator target, or when an open card is route_pending or recorded for
    another provider. Card lists hold at most ``CARD_IDS_MAX`` ids plus an exact count.
    """
    identity = _verified_identity()
    if not identity:
        return {"ok": False, "reason": REASON_UNVERIFIED, "reasons": [REASON_UNVERIFIED]}
    provider = identity["provider"]
    lane_owner = _lane_owner(provider)
    selected = _text(core.read_selected_coordinator_target().get("selected_provider")).lower()
    reasons: list[str] = []
    if lane_owner != provider:
        reasons.append(REASON_LANE_OWNER)
    if selected and selected != provider:
        reasons.append(REASON_SELECTED_TARGET)
    facts: dict[str, Any] = {
        "stamped_provider": provider,
        "lane_owner": lane_owner,
        "selected_provider": selected,
    }
    try:
        pending, mismatched, truncated = _open_card_findings(provider)
    except _CARD_READ_ERRORS as exc:
        reasons.append(REASON_CARDS_UNREADABLE)
        facts["cards_error"] = type(exc).__name__
    else:
        if pending:
            reasons.append(REASON_PENDING_CARDS)
        if mismatched:
            reasons.append(REASON_CARD_PROVIDER)
        facts.update(
            route_pending_task_ids=pending[:CARD_IDS_MAX],
            route_pending_count=len(pending),
            provider_mismatch_task_ids=mismatched[:CARD_IDS_MAX],
            provider_mismatch_count=len(mismatched),
            cards_scan_truncated=truncated,
        )
    return {"ok": not reasons, "reason": reasons[0] if reasons else "", "reasons": reasons, **facts}


def _read_targets(path: Path) -> dict[str, Any]:
    """The raw coordinator-target payload; ``{}`` when absent, ValueError when unreadable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(type(exc).__name__) from exc
    if not isinstance(payload, dict):
        raise ValueError("coordinator_targets_not_object")
    return payload


def _selection(payload: Mapping[str, Any]) -> dict[str, str]:
    provider = _text(payload.get("selected_provider")).lower()
    targets = payload.get("targets")
    target = targets.get(provider) if isinstance(targets, Mapping) else None
    route = target.get("route") if isinstance(target, Mapping) else None
    session_id = _text(route.get("session_id")) if isinstance(route, Mapping) else ""
    return {"provider": provider, "session_id": session_id}


def _claim_refusal(identity: Mapping[str, str]) -> str:
    """Why the caller may not take the lane, ``""`` when it may."""
    if not identity:
        return CLAIM_UNVERIFIED
    provider, session_id = identity["provider"], identity["session_id"]
    if provider == "codex" and not _codex_route_live(identity):
        return CLAIM_CODEX_NOT_LIVE
    chat = core._active_manager_chat_record()
    if chat and not (provider == "manager_chat" and _text(chat.get("session_id")) == session_id):
        return CLAIM_HELD_BY_MANAGER_CHAT
    codex = core._codex_manager_identity()
    if codex and _codex_route_live(codex):
        if not (provider == "codex" and _text(codex.get("session_id")) == session_id):
            return CLAIM_HELD_BY_CODEX
    return ""


def _claimed_payload(current: Mapping[str, Any], identity: Mapping[str, str]) -> dict[str, Any]:
    """``current`` with the caller selected; every unrelated key is kept as it was."""
    provider = identity["provider"]
    payload = dict(current)
    payload.setdefault("schema_id", TARGETS_SCHEMA_ID)
    payload["selected_provider"] = provider
    targets = dict(current.get("targets")) if isinstance(current.get("targets"), Mapping) else {}
    target = dict(targets.get(provider)) if isinstance(targets.get(provider), Mapping) else {}
    route = dict(target.get("route")) if isinstance(target.get("route"), Mapping) else {}
    route["session_id"] = identity["session_id"]
    route["thread_id"] = identity["thread_id"]
    if identity["window_id"]:
        route["window_id"] = identity["window_id"]
    target["route"] = route
    targets[provider] = target
    payload["targets"] = targets
    return payload


def _finish(
    reason: str,
    actor: Mapping[str, str],
    previous: Mapping[str, str],
    new: Mapping[str, str],
) -> dict[str, Any]:
    claimed = reason == CLAIM_OK
    action = json.dumps(
        {
            "operation": "callback_lane_claim",
            "outcome": "claimed" if claimed else "refused",
            "reason": reason,
            "actor": dict(actor),
            "previous": dict(previous),
            "new": dict(new),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    entry = core.write_audit_entry(CLAIM_TOOL, action, "" if claimed else reason)
    return {
        "ok": claimed,
        "reason": reason,
        "actor": dict(actor),
        "previous": dict(previous),
        "selected": dict(new),
        "audited_at": entry.get("timestamp", ""),
    }


def claim_callback_lane() -> dict[str, Any]:
    """MANAGER WRITE: select the caller's verified route as the coordinator target.

    Refuses, leaving the target file unchanged, when the caller is unverified, is
    Codex without a live route, or another live Manager Chat or Codex route holds
    the lane. Every claim and refusal appends one audit row.
    """
    identity = _verified_identity()
    actor = {"provider": identity.get("provider", ""), "session_id": identity.get("session_id", "")}
    path = core._coordinator_route_state_path()
    try:
        current: dict[str, Any] | None = _read_targets(path)
    except ValueError:
        current = None
    previous = _selection(current or {})
    reason = _claim_refusal(identity)
    if reason:
        return _finish(reason, actor, previous, previous)
    if current is None:
        return _finish(CLAIM_TARGETS_UNREADABLE, actor, previous, previous)
    try:
        task_store._atomic_write_json(path, _claimed_payload(current, identity))
    except OSError:
        return _finish(CLAIM_WRITE_FAILED, actor, previous, previous)
    return _finish(CLAIM_OK, actor, previous, actor)
