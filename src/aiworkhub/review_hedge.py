"""One bounded hedged successor for a quiet automatic reviewer (NF-2026-00823).

A read-only quality reviewer can hold its lens for the whole launch timeout
while it emits only reasoning deltas and heartbeats: it never calls a tool and
never submits, and the card waits a full cycle before the ordinary
one-distinct-route retry even starts. This module owns the policy that buys
exactly ONE hedge for such an attempt, beside the incumbent, on a distinct
route chosen by the orchestrator's own route selection.

* The incumbent is never cancelled or terminalized from wall-clock here
  (owner contract NF-2026-00176). Quiet time only buys the hedge.
* Quiet time is measured from launch or from the last AUTHENTICATED
  ACTIONABLE PROGRESS -- the supervisor's ``last_actionable_progress_epoch``,
  which only a trusted adapter's meaningful progress phase (Source Graph or
  context receipts, tool execution, quality_review_submit) moves. Raw text,
  reasoning deltas, usage growth and heartbeats never reset it.
* The first valid sealed submission wins. The loser is cancelled by its exact
  request id and retired, so a late loser submission is never read.
* The hedge IS the lens's one distinct-route retry: it takes attempt index 2,
  so the ``_MAX_ROUTE_ATTEMPTS`` ceiling still refuses any third route.
* Mutating workers are never hedged.

``review_orchestrator`` keeps only small hooks; every decision lives here.
"""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .worker_supervisor import TRUSTED_PROGRESS_ADAPTERS

# Conservative: at least 600 s, and strictly below the 1800 s reviewer wall so a
# hedge still has time to report before the incumbent's launch timeout.
HEDGE_QUIET_SECONDS = 900.0

KIND_READ_ONLY_REVIEWER = "read_only_reviewer"
KIND_MUTATING = "mutating"

HEDGE_STATE_NONE = "none"
HEDGE_STATE_RACING = "racing"
HEDGE_STATE_DECIDED = "decided"
HEDGE_STATE_SKIPPED = "skipped"

RACE_RUNNING = "running"
RACE_SUBMITTED = "submitted"
RACE_FAILED = "failed"
RACE_UNSUBMITTED = "unsubmitted"
_RACE_STATES = frozenset({RACE_RUNNING, RACE_SUBMITTED, RACE_FAILED, RACE_UNSUBMITTED})

ROLE_INCUMBENT = "incumbent"
ROLE_HEDGE = "hedge"

EVENT_LAUNCHED = "hedge_launched"
EVENT_WON = "hedge_won"
EVENT_LOSER_CANCELLED = "hedge_loser_cancelled"
EVENT_SKIPPED = "hedge_skipped"

SKIP_NO_DISTINCT_ROUTE = "hedge_skipped:no_distinct_route"
SKIP_PROGRESS_SIGNAL_UNAVAILABLE = "hedge_skipped:progress_signal_unavailable"
SKIP_CANCEL_UNAVAILABLE = "hedge_skipped:cancel_unavailable"
SKIP_ROUTE_SELECTION_REFUSED = "hedge_skipped:route_selection_refused:"
SKIP_HEDGE_LAUNCH_FAILED = "hedge_skipped:hedge_launch_failed:"
SKIP_HEDGE_REUSED_INCUMBENT = "hedge_skipped:hedge_reused_incumbent"

# The retirement reason (and cancel reason) written for a race loser.
LOSER_RETIREMENT = "hedge_loser_cancelled"

_INCUMBENT_INDEX = 1
_HEDGE_INDEX = 2
_MAX_DETAIL_CHARS = 500

HEDGE_TABLE = (
    "CREATE TABLE IF NOT EXISTS review_orchestrator_hedges ("
    "chain_id INTEGER NOT NULL, lens TEXT NOT NULL, state TEXT NOT NULL, "
    "incumbent_attempt_index INTEGER NOT NULL, "
    "hedge_attempt_index INTEGER NOT NULL, "
    "incumbent_request_id TEXT NOT NULL, "
    "hedge_request_id TEXT NOT NULL DEFAULT '', quiet_seconds REAL, "
    "winner_attempt_index INTEGER NOT NULL DEFAULT 0, "
    "outcome TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
    "PRIMARY KEY(chain_id, lens))"
)
HEDGE_EVENT_TABLE = (
    "CREATE TABLE IF NOT EXISTS review_orchestrator_hedge_events ("
    "event_id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id INTEGER NOT NULL, "
    "lens TEXT NOT NULL, event TEXT NOT NULL, "
    "incumbent_request_id TEXT NOT NULL DEFAULT '', "
    "hedge_request_id TEXT NOT NULL DEFAULT '', quiet_seconds REAL, "
    "detail TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL)"
)

_RECORD_KEYS = (
    "state", "incumbent_attempt_index", "hedge_attempt_index",
    "incumbent_request_id", "hedge_request_id", "quiet_seconds",
    "winner_attempt_index", "outcome", "created_at",
)
_EVENT_KEYS = (
    "event", "incumbent_request_id", "hedge_request_id", "quiet_seconds", "detail",
)


def _orchestrator_module() -> Any:
    """``review_orchestrator``, imported lazily: it imports this module."""
    from . import review_orchestrator

    return review_orchestrator


@dataclass(frozen=True)
class HedgeDecision:
    hedge: bool
    reason: str
    quiet_seconds: float | None = None


def _epoch(value: Any) -> float | None:
    """A finite numeric epoch, else ``None``; a bool is never a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def decide_hedge(
    *,
    launched_at: Any,
    last_actionable_progress_at: Any,
    now: Any,
    hedge_state: str,
    kind: str,
    quiet_threshold: float = HEDGE_QUIET_SECONDS,
) -> HedgeDecision:
    """Pure: whether one incumbent attempt has earned its single hedge.

    Quiet time runs from the later of launch and the last actionable progress.
    Anything this function cannot measure is a refusal, never a hedge.
    """
    if kind != KIND_READ_ONLY_REVIEWER:
        return HedgeDecision(
            False,
            "mutating_worker_never_hedged" if kind == KIND_MUTATING
            else "attempt_kind_unknown",
        )
    if hedge_state != HEDGE_STATE_NONE:
        return HedgeDecision(False, "hedge_already_spent")
    launched = _epoch(launched_at)
    current = _epoch(now)
    if launched is None or current is None:
        return HedgeDecision(False, "quiet_clock_unknown")
    progress = _epoch(last_actionable_progress_at)
    anchor = launched if progress is None else max(launched, progress)
    quiet = max(0.0, current - anchor)
    if quiet < quiet_threshold:
        return HedgeDecision(False, "actionable_progress_recent", quiet)
    return HedgeDecision(True, "no_actionable_progress", quiet)


def attempt_kind(status: Mapping[str, Any]) -> str:
    """Mutating when the process or its card says so, else a read-only reviewer."""
    card = status.get("task_card")
    if status.get("read_only") is False or (
        isinstance(card, Mapping) and card.get("read_only") is False
    ):
        return KIND_MUTATING
    return KIND_READ_ONLY_REVIEWER


def quiet_clock(
    status: Mapping[str, Any], now: float
) -> tuple[float | None, float | None]:
    """``(launched_at, last_actionable_progress_at)`` from a status's liveness."""
    liveness = status.get("liveness")
    if not isinstance(liveness, Mapping):
        return None, None
    runtime = _epoch(liveness.get("runtime_seconds"))
    launched = None if runtime is None or runtime < 0 else now - runtime
    return launched, _epoch(liveness.get("last_actionable_progress_epoch"))


def race_winner(incumbent: str, hedge: str) -> str | None:
    """The winning role, ``""`` when neither submitted, ``None`` to keep waiting.

    The incumbent was bought first, so it wins a tie observed in one pass.
    """
    if incumbent not in _RACE_STATES or hedge not in _RACE_STATES:
        return None
    if incumbent == RACE_SUBMITTED:
        return ROLE_INCUMBENT
    if hedge == RACE_SUBMITTED:
        return ROLE_HEDGE
    if RACE_RUNNING in (incumbent, hedge):
        return None
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(orch: Any, *statements: tuple[str, tuple[Any, ...]]) -> None:
    ro = _orchestrator_module()
    with closing(ro._side_table_connection(orch.db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(ro.ROUTE_ATTEMPT_TABLE)
        conn.execute(HEDGE_TABLE)
        conn.execute(HEDGE_EVENT_TABLE)
        for sql, params in statements:
            conn.execute(sql, params)


def _read_rows(db_path: str | Path, sql: str, params: tuple[Any, ...]) -> list[Any]:
    ro = _orchestrator_module()
    with closing(ro._side_table_connection(db_path, readonly=True)) as conn:
        try:
            return list(conn.execute(sql, params).fetchall())
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise


def hedge_record(db_path: str | Path, chain_id: int, lens: str) -> dict[str, Any] | None:
    """The one durable hedge record for a chain lens, or ``None``."""
    rows = _read_rows(
        db_path,
        "SELECT " + ",".join(_RECORD_KEYS) + " FROM review_orchestrator_hedges "
        "WHERE chain_id=? AND lens=?",
        (int(chain_id), str(lens)),
    )
    return dict(zip(_RECORD_KEYS, rows[0], strict=True)) if rows else None


def hedge_events(db_path: str | Path, chain_id: int, lens: str) -> list[dict[str, Any]]:
    """Every hedge telemetry row for a chain lens, oldest first."""
    rows = _read_rows(
        db_path,
        "SELECT " + ",".join(_EVENT_KEYS) + " FROM review_orchestrator_hedge_events "
        "WHERE chain_id=? AND lens=? ORDER BY event_id",
        (int(chain_id), str(lens)),
    )
    return [dict(zip(_EVENT_KEYS, row, strict=True)) for row in rows]


def _event(
    action: Any, event: str, incumbent_request_id: str, hedge_request_id: str,
    quiet_seconds: Any, detail: str = "",
) -> tuple[str, tuple[Any, ...]]:
    return (
        "INSERT INTO review_orchestrator_hedge_events "
        "(chain_id,lens,event,incumbent_request_id,hedge_request_id,"
        "quiet_seconds,detail,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            int(action.chain_id), action.lens, event, str(incumbent_request_id),
            str(hedge_request_id), _epoch(quiet_seconds),
            str(detail)[:_MAX_DETAIL_CHARS], _now_iso(),
        ),
    )


def _record(
    action: Any, state: str, incumbent_request_id: str, quiet_seconds: Any,
    outcome: str = "",
) -> tuple[str, tuple[Any, ...]]:
    return (
        "INSERT OR IGNORE INTO review_orchestrator_hedges "
        "(chain_id,lens,state,incumbent_attempt_index,hedge_attempt_index,"
        "incumbent_request_id,quiet_seconds,outcome,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            int(action.chain_id), action.lens, state, _INCUMBENT_INDEX,
            _HEDGE_INDEX, str(incumbent_request_id), _epoch(quiet_seconds),
            str(outcome)[:_MAX_DETAIL_CHARS], _now_iso(),
        ),
    )


def _decide(
    *, launched_at: Any, progress_at: Any, now: float, kind: str, state: str,
) -> HedgeDecision:
    return decide_hedge(
        launched_at=launched_at, last_actionable_progress_at=progress_at,
        now=now, hedge_state=state, kind=kind,
    )


def maybe_launch(
    orch: Any, action: Any, attempt: Mapping[str, Any], status: Mapping[str, Any],
) -> None:
    """Buy the one hedge for a still-running incumbent that has gone quiet.

    Called from the orchestrator's still-running branch only. The incumbent is
    neither cancelled nor retired here; when no distinct route exists the
    skip is recorded and the incumbent is left exactly as it was.
    """
    ro = _orchestrator_module()
    incumbent_request = str(attempt.get("reviewer_request_id") or "")
    if (
        int(attempt.get("attempt_index") or 0) != _INCUMBENT_INDEX
        or not incumbent_request
        or str(attempt.get("state") or "") != "launched"
        or ro._MAX_ROUTE_ATTEMPTS < _HEDGE_INDEX
        or orch._is_adopted_attempt(action, attempt)
    ):
        return
    now = time.time()
    launched_at, progress_at = quiet_clock(status, now)
    adapter = str(status.get("adapter_id") or attempt.get("adapter_id") or "")
    trusted = adapter in TRUSTED_PROGRESS_ADAPTERS
    if not trusted:
        # An untrusted adapter's progress clock is not authenticated evidence.
        progress_at = None
    kind = attempt_kind(status)
    # Decide on the in-memory facts first so a reviewer that is not quiet
    # costs no database read; only a candidate hedge reads the durable state.
    decision = _decide(
        launched_at=launched_at, progress_at=progress_at, now=now, kind=kind,
        state=HEDGE_STATE_NONE,
    )
    if not decision.hedge:
        return
    record = hedge_record(orch.db_path, action.chain_id, action.lens)
    if record is not None:
        decision = _decide(
            launched_at=launched_at, progress_at=progress_at, now=now, kind=kind,
            state=str(record["state"]),
        )
        if not decision.hedge:
            return
    attempts = orch._route_attempts(action.chain_id, action.lens)
    if len(attempts) != 1:
        return
    excluded, excluded_adapters = ro._route_attempt_exclusions(attempts)
    excluded = excluded | {ro._review_route_identity(attempt)}
    reviewer_task_id = orch._reviewer_task_id(
        action.descriptor["chain_identity"], action.lens, attempt_index=_HEDGE_INDEX,
    )
    quiet = decision.quiet_seconds
    skip = ""
    route: dict[str, str] = {}
    if not trusted:
        skip = SKIP_PROGRESS_SIGNAL_UNAVAILABLE
    elif not callable(getattr(orch.manager, "cancel", None)):
        # A race whose loser cannot be cancelled would leave two providers.
        skip = SKIP_CANCEL_UNAVAILABLE
    else:
        try:
            route = orch._select_attempt_route(
                reviewer_task_id, action.lens, frozenset(excluded),
                excluded_adapters=excluded_adapters,
            )
        except Exception as exc:  # noqa: BLE001 -- a refused selection is a named skip
            text = str(exc)
            if text.startswith("review_route_unavailable:") or text.startswith(
                "review_route_identity_invalid"
            ):
                skip = SKIP_NO_DISTINCT_ROUTE
            else:
                skip = SKIP_ROUTE_SELECTION_REFUSED + type(exc).__name__
    if skip:
        _write(
            orch,
            _record(action, HEDGE_STATE_SKIPPED, incumbent_request, quiet, skip),
            _event(action, EVENT_SKIPPED, incumbent_request, "", quiet, skip),
        )
        return
    _write(
        orch,
        (
            "INSERT OR IGNORE INTO review_orchestrator_route_attempts "
            "(chain_id,lens,attempt_index,reviewer_task_id,runner,adapter_id,"
            "model,state) VALUES (?,?,?,?,?,?,?,'planned')",
            (
                int(action.chain_id), action.lens, _HEDGE_INDEX, reviewer_task_id,
                route["runner"], route["adapter_id"], route["model"],
            ),
        ),
        _record(action, HEDGE_STATE_RACING, incumbent_request, quiet),
    )
    planned = orch._route_attempts(action.chain_id, action.lens)
    record = hedge_record(orch.db_path, action.chain_id, action.lens)
    if (
        len(planned) != _HEDGE_INDEX
        or str(planned[-1]["reviewer_task_id"]) != reviewer_task_id
        or record is None
        or record["state"] != HEDGE_STATE_RACING
        or str(record["incumbent_request_id"]) != incumbent_request
    ):
        raise RuntimeError("review_hedge_plan_conflict")
    _launch_hedge(orch, action, planned[-1], record)


def _launch_hedge(
    orch: Any, action: Any, hedge_row: Mapping[str, Any], record: Mapping[str, Any],
) -> None:
    incumbent = str(record["incumbent_request_id"])
    quiet = record.get("quiet_seconds")
    result = orch._launch_route_attempt(
        action, hedge_row, hedge_incumbent_request_id=incumbent,
    )
    if result is None:
        # Acknowledged-but-unbound or refused: the planned hedge row stays and
        # the next accept pass reconciles the same pre-bound reviewer task.
        return
    if "_route_terminal_failure" in result:
        # ``_launch_route_attempt`` already retired the hedge row; the race
        # now waits on the incumbent alone.
        _write(orch, _event(
            action, EVENT_SKIPPED, incumbent, "", quiet,
            SKIP_HEDGE_LAUNCH_FAILED + str(result["_route_terminal_failure"]),
        ))
        return
    adopted = result.get("_adopted_attempt")
    bound = adopted if isinstance(adopted, Mapping) else hedge_row
    hedge_request = str(
        (adopted or {}).get("reviewer_request_id") or result.get("request_id") or ""
    )
    statements = [(
        "UPDATE review_orchestrator_hedges SET hedge_request_id=? "
        "WHERE chain_id=? AND lens=? AND hedge_request_id=''",
        (hedge_request, int(action.chain_id), action.lens),
    )]
    if hedge_request == incumbent:
        orch._retire_route_attempt(action, hedge_row, "hedge_reused_incumbent")
        statements.append(_event(
            action, EVENT_SKIPPED, incumbent, hedge_request, quiet,
            SKIP_HEDGE_REUSED_INCUMBENT,
        ))
    else:
        statements.append(_event(
            action, EVENT_LAUNCHED, incumbent, hedge_request, quiet,
            "route=" + "/".join(
                str(bound.get(key) or "") for key in ("runner", "adapter_id", "model")
            ),
        ))
    _write(orch, *statements)


def _race_state(orch: Any, action: Any, row: Mapping[str, Any]) -> str:
    ro = _orchestrator_module()
    if str(row.get("state") or "") == "retired":
        return RACE_FAILED
    request_id = str(row.get("reviewer_request_id") or "")
    if not request_id:
        return RACE_RUNNING
    try:
        status = orch.manager.status(request_id)
    except Exception:  # noqa: BLE001 -- an unreadable status keeps waiting
        return RACE_RUNNING
    if not isinstance(status, Mapping) or (
        str(status.get("state") or "") in ro._REVIEWER_RUNNING_STATES
    ):
        return RACE_RUNNING
    if ro.mechanical_reviewer_failure_reason(status):
        return RACE_FAILED
    try:
        orch._review_receipt(
            action, status, request_id, str(row.get("reviewer_task_id") or ""),
        )
    except Exception:  # noqa: BLE001 -- an unverifiable report is no submission
        return RACE_UNSUBMITTED
    return RACE_SUBMITTED


def arbitrate(
    orch: Any, action: Any, attempt: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """The attempt the accept path should read, or ``None`` to keep waiting.

    Anything but a hedged lens's attempt 2 passes through unchanged, so the
    ordinary accept path is exactly what it was for every unhedged lens.
    """
    if int(attempt.get("attempt_index") or 0) != _HEDGE_INDEX:
        return attempt
    record = hedge_record(orch.db_path, action.chain_id, action.lens)
    if record is None or record["state"] == HEDGE_STATE_SKIPPED:
        return attempt
    rows = {
        int(row["attempt_index"]): row
        for row in orch._route_attempts(action.chain_id, action.lens)
    }
    incumbent = rows.get(int(record["incumbent_attempt_index"]))
    hedge = rows.get(int(record["hedge_attempt_index"]))
    if incumbent is None or hedge is None:
        raise RuntimeError("review_hedge_attempt_missing")
    state = str(record["state"])
    if state == HEDGE_STATE_DECIDED:
        # The race is settled durably: a late loser submission is never read.
        return rows.get(int(record["winner_attempt_index"]), incumbent)
    if state != HEDGE_STATE_RACING:
        raise RuntimeError("review_hedge_state_invalid:" + state)
    if str(hedge.get("state") or "") != "retired" and not str(
        hedge.get("reviewer_request_id") or ""
    ):
        _launch_hedge(orch, action, hedge, record)
        return None
    incumbent_state = _race_state(orch, action, incumbent)
    hedge_state = _race_state(orch, action, hedge)
    winner = race_winner(incumbent_state, hedge_state)
    if winner is None:
        return None
    key = (int(action.chain_id), action.lens)
    incumbent_request = str(record["incumbent_request_id"])
    hedge_request = str(
        record["hedge_request_id"] or hedge.get("reviewer_request_id") or ""
    )
    quiet = record["quiet_seconds"]
    if winner == "":
        # Neither side submitted: the ordinary path retires the incumbent and
        # meets the spent retry ceiling -- no third route.
        _write(orch, (
            "UPDATE review_orchestrator_hedges SET state='decided',"
            "winner_attempt_index=?,outcome='no_valid_submission' "
            "WHERE chain_id=? AND lens=? AND state='racing'",
            (int(incumbent["attempt_index"]), *key),
        ))
        return incumbent
    if winner == ROLE_HEDGE:
        winner_row, loser_row, loser_role, loser_state = (
            hedge, incumbent, ROLE_INCUMBENT, incumbent_state,
        )
    else:
        winner_row, loser_row, loser_role, loser_state = (
            incumbent, hedge, ROLE_HEDGE, hedge_state,
        )
    statements: list[tuple[str, tuple[Any, ...]]] = []
    if winner == ROLE_HEDGE:
        statements.append(_event(
            action, EVENT_WON, incumbent_request, hedge_request, quiet,
            "winner=" + ROLE_HEDGE,
        ))
    if loser_state == RACE_RUNNING:
        loser_request = str(loser_row.get("reviewer_request_id") or "")
        try:
            cancelled = orch.manager.cancel(loser_request, reason=LOSER_RETIREMENT)
            cancel_state = (
                str(cancelled.get("state") or cancelled.get("ok"))
                if isinstance(cancelled, Mapping) else "unknown"
            )
        except Exception as exc:  # noqa: BLE001 -- recorded, retirement still fences it
            cancel_state = "cancel_error:" + type(exc).__name__
        statements.append(_event(
            action, EVENT_LOSER_CANCELLED, incumbent_request, hedge_request, quiet,
            f"loser={loser_role};request_id={loser_request};cancel_state={cancel_state}",
        ))
    if str(loser_row.get("state") or "") != "retired" and not orch._retire_route_attempt(
        action, loser_row, LOSER_RETIREMENT
    ):
        if statements:
            _write(orch, *statements)
        return None
    statements.append((
        "UPDATE review_orchestrator_hedges SET state='decided',"
        "winner_attempt_index=?,outcome=? "
        "WHERE chain_id=? AND lens=? AND state='racing'",
        (int(winner_row["attempt_index"]), "winner=" + winner, *key),
    ))
    _write(orch, *statements)
    return winner_row
