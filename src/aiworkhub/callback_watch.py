"""NF-2026-01769: a read-only watch for a caller whose callback lane another route owns.

Measured 2026-10-27/28: ``claude_callback_wait`` refused a manager on the Codex
route with ``callback_lane_owned_by_codex`` before it reached the watch loop, so
the manager polled ``agent_task_status`` (about 10 KB a reply) while three cards
ran.  Refusing DELIVERY is correct -- the owning route claims, leases and
acknowledges its batches -- but waiting for a named request's ledger row to
change is a pure read.  This module is that read: it never opens the callback
store, never claims, leases or acknowledges a batch and never changes lane
ownership.  It only blocks until a watched request's ``_watch_signature``
(state, preparation_phase, exit_code, timestamp) changes or the capped timeout
passes, and replies with one compact row per watched request.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping, Sequence

READ_ONLY_WATCH_MODE = "read_only_watch"
# Only a lane another verified route owns may be watched; any other refusal stands.
WATCHABLE_LANE_OWNERS = frozenset({"callback_lane_owned_by_codex", "callback_lane_owned_by_manager_chat"})
_DEFAULT_TIMEOUT_SECONDS = 240


def callback_wait_timing(timeout_seconds: Any) -> tuple[int, dict[str, Any]]:
    """The effective hold for one callback wait and the timing fields every
    reply carries; the single owner for both the delivering and the read-only
    wait."""
    from . import core  # local import: cycle-safe

    limits = core.callback_wait_limits()
    try:
        requested = int(timeout_seconds)
    except (TypeError, ValueError):
        requested = _DEFAULT_TIMEOUT_SECONDS
    timeout = max(1, min(requested, int(limits["max_timeout_seconds"])))
    return timeout, {
        "requested_timeout_seconds": requested,
        "effective_timeout_seconds": timeout,
        "max_timeout_seconds": int(limits["max_timeout_seconds"]),
        "timeout_clamped": requested > timeout,
        "timeout_clamp_source": limits["timeout_clamp_source"],
    }


def normalize_watch_ids(watch_request_ids: Sequence[str] | None) -> list[str]:
    """Non-blank, stripped request ids, capped at ``CALLBACK_WAIT_WATCH_LIMIT``;
    the single owner for both the delivering and the read-only wait."""
    from . import core  # local import: cycle-safe

    return [
        str(item).strip() for item in (watch_request_ids or []) if str(item or "").strip()
    ][:core.CALLBACK_WAIT_WATCH_LIMIT]


def _watched_rows(watch: Sequence[str]) -> list[dict[str, Any]] | None:
    """The watched requests' ledger rows; ``None`` when the ledger is unreadable."""
    from . import core  # local import: cycle-safe

    watched = set(watch)
    try:
        # An empty session id: only the watched requests are kept below.
        rows = core._claude_session_process_rows("", watch)
    except Exception:  # noqa: BLE001 -- a watch is evidence, never a failure
        return None
    return [row for row in rows if str(row.get("request_id") or "") in watched]


def _card_risk_tiers(task_ids: Sequence[str]) -> dict[str, Any]:
    """``risk_tier`` per task card, read-only; an unreadable store reports none."""
    from . import core  # local import: cycle-safe

    wanted = sorted({task_id for task_id in task_ids if task_id})
    if not wanted:
        return {}
    tiers: dict[str, Any] = {}
    try:
        conn = core._canonical_connect(readonly=True)
        try:
            marks = ",".join("?" for _ in wanted)
            for row in conn.execute(
                f"SELECT task_id, card_json FROM tasks WHERE task_id IN ({marks})", tuple(wanted),
            ).fetchall():
                try:
                    card = json.loads(row["card_json"] or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(card, dict):
                    tiers[str(row["task_id"])] = card.get("risk_tier")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- risk DESCRIPTION never fails a watch
        return {}
    return tiers


def _compact_rows(
    rows: Sequence[Mapping[str, Any]],
    watch: Sequence[str],
    baseline: Mapping[str, tuple],
    current: Mapping[str, tuple],
) -> list[dict[str, Any]]:
    by_request = {str(row.get("request_id") or ""): row for row in rows}
    review_ready = {
        request_id: row for request_id, row in by_request.items()
        if str(row.get("state") or "") == "review_ready"
    }
    tiers = _card_risk_tiers(
        [str(row.get("task_id") or "") for row in review_ready.values() if not row.get("risk_tier")]
    )
    compact: list[dict[str, Any]] = []
    for request_id in watch:
        row = by_request.get(request_id) or {}
        signature = current.get(request_id) or ("absent", None, None, None)
        entry: dict[str, Any] = {
            "request_id": request_id,
            "task_id": str(row.get("task_id") or ""),
            "state": signature[0],
            "changed": signature != baseline.get(request_id),
            "exit_code": signature[2],
            "risk_tier": None,
            "changed_paths_count": None,
        }
        if request_id in review_ready:
            entry["risk_tier"] = row.get("risk_tier") or tiers.get(entry["task_id"])
            changed_paths = row.get("changed_paths")
            if isinstance(changed_paths, list):
                entry["changed_paths_count"] = len(changed_paths)
        compact.append(entry)
    return compact


def read_only_watch(
    refusal: Mapping[str, Any],
    watch_request_ids: Sequence[str] | None,
    timeout_seconds: Any,
) -> dict[str, Any]:
    """Block on the watched requests' ledger rows without touching delivery.

    ``refusal`` is ``_claude_lane_refusal()``; its ``reason``/``owning_route``
    stay in the reply so the caller still learns who delivers.  An empty
    (after normalisation) watch list returns the refusal unchanged."""
    from . import core  # local import: cycle-safe

    watch = normalize_watch_ids(watch_request_ids)
    if not watch:
        return {"ok": False, **refusal}
    timeout, timing = callback_wait_timing(timeout_seconds)
    # An unreadable ledger is no observation, never a change: the first
    # readable snapshot is the baseline and the last one is reported.
    rows: list[dict[str, Any]] = []
    baseline: dict[str, tuple] | None = None
    current = core._watch_signature(rows, watch)
    deadline = time.monotonic() + timeout
    while True:
        observed = _watched_rows(watch)
        if observed is not None:
            rows, current = observed, core._watch_signature(observed, watch)
            if baseline is None:
                baseline = current
        changed = baseline is not None and current != baseline
        if changed or time.monotonic() >= deadline:
            return {
                "ok": True,
                "mode": READ_ONLY_WATCH_MODE,
                "status": "watch_changed" if changed else "timeout_no_change",
                **refusal,
                "requests": _compact_rows(rows, watch, baseline or current, current),
                **timing,
            }
        remaining = deadline - time.monotonic()
        time.sleep(max(0.0, min(core.CALLBACK_WAIT_WATCH_POLL_SECONDS, remaining)))
