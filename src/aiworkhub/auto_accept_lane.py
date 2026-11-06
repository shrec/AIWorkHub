"""Auto-accept lane: a clean low/medium candidate is accepted without a manager turn.

NF-2026-02103 measured 145 cards: reviewers-done -> accepted was 83% of
review_ready -> accepted time (median 26.2 min, p90 77.8 min, 108 h in total)
while the accept itself takes about 3 minutes. The candidates waiting were
mostly ones nothing objected to. This lane accepts exactly those, by
measurement, and leaves every other candidate to the manager.

Four pieces, each testable on its own:

* :func:`load_config` -- the opt-in switch. Anything but a readable JSON object
  whose ``enabled`` is literally ``true`` is disabled (fail closed); the code
  default is disabled.
* :func:`eligibility` -- PURE over the review packet ``aiworkhub_review_packet``
  builds (:func:`completion_inbox.review_packet`), never a re-derivation of the
  card. It returns ``(eligible, reasons)`` with one named reason per failing
  condition.
* :func:`run_attempt` -- the SAME accept path ``aiworkhub_agent_accept_review``
  runs (``accept_review``: gates, combined tree, neighbour tests; then
  ``accept_commit`` with its Resolves trailer, then the learning disposition
  ``mechanical_change_only``) under the explicit system actor
  ``auto_accept_lane``. It never raises.
* :class:`AutoAcceptRunner` plus the per-chain attempt ledger -- the attempt
  runs OFF the review drain loop, and one durable row per ``chain_id`` makes it
  idempotent: a replayed chain finds its row and never accepts twice.

The review orchestrator owns every SQLite transaction; :func:`settle` only
executes statements on the connection it is handed, so the outcome, the
``auto_accepted`` / ``auto_accept_declined`` task event and the manager-ready
projection commit together or not at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

ACTOR = "auto_accept_lane"
CONFIG_REL = (".aiworkhub", "config", "auto_accept.json")
CONFIG_MAX_BYTES = 4096
ELIGIBLE_RISK_TIERS = ("low", "medium")
NO_NEW_LESSON_REASON = "mechanical_change_only"
PACKET_MAX_BYTES = 90_000
OUTCOME_SCHEMA_ID = "aiworkhub.auto_accept_outcome.v1"
ACCEPT_RECEIPT_SCHEMA_ID = "aiworkhub.accept_review_receipt.v1"
MAX_REASONS = 20
MAX_OUTCOME_BYTES = 16 * 1024

# Task events this lane writes; ``sdlc_outcome_metrics`` counts the first.
EVENT_AUTO_ACCEPTED = "auto_accepted"
EVENT_DECLINED = "auto_accept_declined"

STATE_RUNNING = "running"
STATE_ACCEPTED = "accepted"
# The accept landed but its auto-commit did not: not auto-accepted; manager_ready.
STATE_ACCEPTED_UNCOMMITTED = "accepted_uncommitted"
STATE_REFUSED = "refused"
STATE_FAILED = "failed"
TERMINAL_STATES = frozenset(
    {STATE_ACCEPTED, STATE_ACCEPTED_UNCOMMITTED, STATE_REFUSED, STATE_FAILED}
)

# What the orchestrator does with a chain after :func:`settle`.
ACTION_DEFER = "defer"
ACTION_SUBMIT = "submit"
ACTION_AUTO_ACCEPTED = "auto_accepted"
ACTION_MANAGER_READY = "manager_ready"

# The closed set of eligibility reasons; a reason may carry ``:<detail>``.
REASON_PACKET_UNAVAILABLE = "packet_unavailable"
REASON_PACKET_TRUNCATED = "packet_truncated"
REASON_QUALITY_GATE = "quality_gate_not_passed"
REASON_MCP_GATE_ABSENT = "worker_mcp_gate_absent"
REASON_MCP_GATE = "worker_mcp_gate_not_satisfied"
REASON_REPORTS_UNAVAILABLE = "reviewer_reports_unavailable"
REASON_NO_REVIEWER = "no_required_reviewer_lens"
REASON_LENS_NOT_USABLE = "required_lens_not_usable"
REASON_DEFECTS = "reviewer_defect_findings"
REASON_REFINEMENT = "refinement_required"
REASON_PREVIEW_NOT_EVALUATED = "accept_preview_not_evaluated"
REASON_PREVIEW_BLOCKED = "accept_preview_blocked"
REASON_DESTRUCTIVE = "destructive_diff_blocker"
REASON_RISK_TIER = "risk_tier_not_low_or_medium"
ELIGIBILITY_REASONS = (
    REASON_PACKET_UNAVAILABLE,
    REASON_PACKET_TRUNCATED,
    REASON_QUALITY_GATE,
    REASON_MCP_GATE_ABSENT,
    REASON_MCP_GATE,
    REASON_REPORTS_UNAVAILABLE,
    REASON_NO_REVIEWER,
    REASON_LENS_NOT_USABLE,
    REASON_DEFECTS,
    REASON_REFINEMENT,
    REASON_PREVIEW_NOT_EVALUATED,
    REASON_PREVIEW_BLOCKED,
    REASON_DESTRUCTIVE,
    REASON_RISK_TIER,
)
# Attempt failures (never eligibility reasons).
REASON_ACCEPT_REFUSED = "accept_refused"
REASON_INTERNAL_ERROR = "auto_accept_internal_error"
REASON_INTERRUPTED = "auto_accept_interrupted"
# ``auto_commit_<commit state>:<commit reason>`` for an accept whose commit did not land.
REASON_AUTO_COMMIT = "auto_commit"
# The one commit state ``accept_commit.commit_promoted`` reports for a landed commit.
COMMIT_STATE_COMMITTED = "committed"

# ``owner_liveness`` verdicts; only ``dead`` lets a foreign running row be settled.
OWNER_ALIVE = "alive"
OWNER_DEAD = "dead"
OWNER_UNKNOWN = "unknown"
# The accept itself measured ~3 min; an unknown owner's claim older than this 10x headroom is stale.
ATTEMPT_STALE_SECONDS = 1800

# The manager wake an auto-accepted chain enqueues: the one the manager already consumes.
CALLBACK_TRANSITION = "review_ready"

# Accept-fold blocker kinds that have their own named eligibility reason.
_REFINEMENT_BLOCKER = "refinement_required"
_DESTRUCTIVE_BLOCKER = "destructive_diff_requires_manager_confirmation"

ATTEMPT_TABLE = (
    "CREATE TABLE IF NOT EXISTS auto_accept_lane_attempts ("
    "chain_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, request_id TEXT NOT NULL, "
    "state TEXT NOT NULL, owner TEXT NOT NULL, owner_pid INTEGER NOT NULL DEFAULT 0, "
    "owner_start_ticks TEXT NOT NULL DEFAULT '', outcome_json TEXT NOT NULL DEFAULT '{}', "
    "declined_recorded INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, "
    "finished_at TEXT NOT NULL DEFAULT '')"
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def config_path(repo: str | Path) -> Path:
    return Path(repo).joinpath(*CONFIG_REL)


def load_config(repo: str | Path) -> dict[str, Any]:
    """``{"enabled": bool, "reason": str}``; missing or unreadable means disabled."""

    path = config_path(repo)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"enabled": False, "reason": "config_missing"}
    except OSError as exc:
        return {"enabled": False, "reason": f"config_unreadable:{type(exc).__name__}"}
    if len(raw) > CONFIG_MAX_BYTES:
        return {"enabled": False, "reason": "config_too_large"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"enabled": False, "reason": "config_invalid_json"}
    if not isinstance(data, dict):
        return {"enabled": False, "reason": "config_not_an_object"}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return {"enabled": False, "reason": "config_enabled_not_boolean"}
    return {"enabled": enabled, "reason": "config_enabled" if enabled else "config_disabled"}


# ---------------------------------------------------------------------------
# Eligibility (pure)
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _defect_count(value: Any) -> int:
    """A count the packet recorded; a missing or unreadable count is never zero."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 1


def _count_or_none(value: Any) -> int | None:
    """A reported count for the callback facts; None when it was not recorded."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _required_lenses(packet: Mapping[str, Any], lenses: list[Mapping[str, Any]]) -> list[str]:
    """The risk profile's required lenses, plus any lens row marked required.

    A profile that names none (a low tier) still needs reviewer evidence before
    anything is accepted without a manager: every selected lens then counts as
    required, and with no selected lens at all the candidate is ineligible.
    """
    risk = _mapping(packet.get("risk"))
    required = [str(lens) for lens in (risk.get("required_reviewer_lenses") or []) if str(lens)]
    required.extend(str(row.get("lens") or "") for row in lenses if row.get("required") is True)
    if not required:
        required = [str(row.get("lens") or "") for row in lenses if row.get("selected") is True]
    return sorted({lens for lens in required if lens})


def eligibility(packet: Mapping[str, Any] | None) -> tuple[bool, list[str]]:
    """``(eligible, reasons)`` over one review packet; no I/O, no clock.

    Eligible only when every condition holds: the quality gate passed, the
    worker MCP gate is satisfied, every required reviewer lens is selected and
    usable, no reviewer reported a defect (observations are allowed), nothing
    requires refinement, the accept preview was evaluated and is not blocked,
    no destructive-diff check failed, and the effective risk tier is low or
    medium. Each failing condition contributes one named reason.
    """
    from . import quality_evidence

    if not isinstance(packet, Mapping) or packet.get("ok") is not True:
        return False, [REASON_PACKET_UNAVAILABLE]
    reasons: list[str] = []
    if _mapping(packet.get("truncation")).get("dropped_sections"):
        reasons.append(REASON_PACKET_TRUNCATED)
    gates = _mapping(packet.get("gates"))
    if gates.get("quality_gate_passed") is not True:
        reasons.append(REASON_QUALITY_GATE)
    mcp = _mapping(gates.get("worker_mcp_gate"))
    if mcp.get("present") is not True:
        reasons.append(REASON_MCP_GATE_ABSENT)
    elif mcp.get("satisfied") is not True or mcp.get("receipt_conformance_blocking"):
        # Fail closed: a present gate is cleared only by an explicit
        # ``satisfied: true``, whatever ``gated`` says or omits.
        reasons.append(REASON_MCP_GATE)

    review = _mapping(packet.get("review"))
    lenses = _rows(review.get("lenses"))
    if packet.get("reviewer_reports_unavailable"):
        reasons.append(REASON_REPORTS_UNAVAILABLE)
    required = _required_lenses(packet, lenses)
    if not required:
        reasons.append(REASON_NO_REVIEWER)
    for lens in required:
        if not any(
            str(row.get("lens") or "") == lens
            and row.get("selected") is True
            and row.get("usable") is True
            for row in lenses
        ):
            reasons.append(f"{REASON_LENS_NOT_USABLE}:{lens}")
    defect_lenses = sorted(
        {str(row.get("lens") or "") for row in lenses if _defect_count(row.get("finding_count"))}
    )
    findings = _rows(review.get("findings"))
    if defect_lenses or findings:
        detail = ",".join(lens for lens in defect_lenses if lens) or str(len(findings))
        reasons.append(f"{REASON_DEFECTS}:{detail}")

    preview = _mapping(packet.get("accept_preview"))
    blockers = _rows(preview.get("blockers"))
    kinds = [str(row.get("kind") or "") for row in blockers]
    if review.get("refine_required") or _REFINEMENT_BLOCKER in kinds:
        reasons.append(REASON_REFINEMENT)
    if preview.get("evaluated") is not True:
        reasons.append(REASON_PREVIEW_NOT_EVALUATED)
    else:
        named = [kind for kind in kinds if kind not in (_REFINEMENT_BLOCKER, _DESTRUCTIVE_BLOCKER)]
        reasons.extend(f"{REASON_PREVIEW_BLOCKED}:{kind or 'unnamed'}" for kind in named)
        if preview.get("blocked") is not False and not blockers:
            # Blocked with nothing named, or an unknown ``blocked``: never clear.
            reasons.append(REASON_PREVIEW_BLOCKED)

    for row in _rows(gates.get("destructive_diff_checks")):
        if str(row.get("status") or "") == quality_evidence.STATUS_FAILED:
            reasons.append(f"{REASON_DESTRUCTIVE}:{row.get('check_id') or 'unnamed'}")
    for row in blockers:
        if str(row.get("kind") or "") == _DESTRUCTIVE_BLOCKER:
            reasons.append(f"{REASON_DESTRUCTIVE}:{row.get('detail') or 'unnamed'}")

    tier = str(_mapping(packet.get("risk")).get("effective_tier") or "").strip().lower()
    if tier not in ELIGIBLE_RISK_TIERS:
        reasons.append(f"{REASON_RISK_TIER}:{tier or 'unknown'}")

    unique = list(dict.fromkeys(reason[:300] for reason in reasons))[:MAX_REASONS]
    return not unique, unique


def eligibility_facts(packet: Mapping[str, Any] | None) -> dict[str, Any]:
    """The bounded facts an eligibility verdict was decided on, for the callback."""
    packet = _mapping(packet)
    gates = _mapping(packet.get("gates"))
    mcp = _mapping(gates.get("worker_mcp_gate"))
    review = _mapping(packet.get("review"))
    lenses = _rows(review.get("lenses"))
    preview = _mapping(packet.get("accept_preview"))
    return {
        "effective_tier": str(_mapping(packet.get("risk")).get("effective_tier") or "")[:20],
        "quality_gate_passed": gates.get("quality_gate_passed") is True,
        "worker_mcp_gate_satisfied": mcp.get("satisfied"),
        "accept_preview_evaluated": preview.get("evaluated") is True,
        "accept_preview_blocked": preview.get("blocked"),
        "lenses": [
            {
                "lens": str(row.get("lens") or "")[:40],
                "usable": row.get("usable") is True,
                "finding_count": _defect_count(row.get("finding_count")),
                "observation_count": _count_or_none(row.get("observation_count")),
            }
            for row in lenses[:10]
        ],
        "changed_path_count": _mapping(packet.get("diff")).get("changed_path_count"),
    }


# ---------------------------------------------------------------------------
# The accept attempt
# ---------------------------------------------------------------------------


def build_review_packet(manager: Any, *, request_id: str, task_id: str) -> dict[str, Any]:
    """The packet ``aiworkhub_review_packet`` returns for this request.

    The same three calls in the same order as ``server._review_packet_one``:
    the read-only accept preview, the selected reviewers' sealed reports, and
    :func:`completion_inbox.review_packet` over both. Nothing is re-derived.
    """
    from . import completion_inbox, process_launcher_accept_review

    preview = manager.accept_preview(request_id, task_id)
    preview = preview if isinstance(preview, dict) else None
    reviewer_ids = preview.get("reviewer_request_ids") if preview is not None else None
    reports = (
        process_launcher_accept_review.preview_reviewer_reports(
            manager, task_id, request_id, reviewer_ids
        )
        if isinstance(reviewer_ids, list) and reviewer_ids
        else []
    )
    packet = completion_inbox.review_packet(
        request_id,
        task_id=task_id,
        accept_preview=preview,
        max_bytes=PACKET_MAX_BYTES,
        **({"reviewer_reports": reports} if reports else {}),
    )
    if isinstance(packet, dict) and isinstance(reviewer_ids, list) and reviewer_ids and not reports:
        packet["reviewer_reports_unavailable"] = True
    return packet


def _lifecycle_lock() -> Any:
    """The server's accept lock when the MCP server is loaded in this process.

    ``aiworkhub_agent_accept_review`` holds ``server._TASK_LIFECYCLE_WRITE_LOCK``
    across accept and commit; a lane accept in the same process takes the same
    lock so the two never interleave promotion and ``git commit`` on the one
    canonical tree. It is looked up, never imported: importing the server
    module would start nothing useful here and pull the whole MCP surface in.
    """
    server = sys.modules.get(f"{__package__}.server")
    lock = getattr(server, "_TASK_LIFECYCLE_WRITE_LOCK", None)
    if lock is not None and hasattr(lock, "__enter__") and hasattr(lock, "__exit__"):
        return lock
    return contextlib.nullcontext()


def run_accept(manager: Any, *, repo: str | Path, task_id: str, request_id: str) -> dict[str, Any]:
    """``aiworkhub_agent_accept_review`` with its defaults, as the lane actor.

    ``accept_review`` with every override at its default (measured tier,
    every verified bound reviewer, no destructive or high-risk confirmation),
    then ``auto_commit`` of exactly the promoted paths with the Resolves
    trailer, then ``no_new_lesson_reason=mechanical_change_only``. A commit or
    disposition failure never undoes an accept that landed.
    """
    from . import accept_commit, learning_commit_store, task_store

    with _lifecycle_lock():
        snapshot = accept_commit.snapshot_before_accept(repo, auto_commit=True)
        result = manager.accept_review(request_id, task_id)
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = result.get("error") if isinstance(result, Mapping) else None
            return {"ok": False, "error": str(error or "accept_review_invalid_reply")[:400]}
        # accept_review takes no actor argument; the lane's receipt names it.
        receipt: dict[str, Any] = {
            "schema_id": ACCEPT_RECEIPT_SCHEMA_ID, **result, "accepted_by": ACTOR,
        }
        try:
            card = task_store.get_task(repo, task_id)
        except Exception:  # noqa: BLE001 -- the accept landed; the card read is best-effort
            card = None
        commit = accept_commit.commit_promoted(
            snapshot,
            receipt,
            card if isinstance(card, dict) else None,
            task_id=task_id,
            request_id=request_id,
        )
    try:
        disposition: Any = learning_commit_store.record_disposition(
            repo,
            task_id=task_id,
            request_id=request_id,
            disposition=learning_commit_store.DISPOSITION_NO_NEW_LESSON,
            reason=NO_NEW_LESSON_REASON,
            recorded_by=ACTOR,
        )
    except Exception as exc:  # noqa: BLE001 -- the accept already landed
        disposition = {"ok": False, "error": f"learning_closeout_failed:{type(exc).__name__}"}
    paths = result.get("promoted_paths")
    return {
        "ok": True,
        "accepted_by": ACTOR,
        "commit": dict(commit) if isinstance(commit, Mapping) else {},
        "promoted_path_count": len(paths) if isinstance(paths, list) else 0,
        "learning_disposition": disposition,
    }


def _commit_facts(commit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "state": str(commit.get("state") or "")[:40],
        "sha": str(commit.get("sha") or "")[:64],
        "reason": str(commit.get("reason") or "")[:200],
    }


def _disposition_facts(value: Any) -> dict[str, Any]:
    value = _mapping(value)
    return {
        key: str(value.get(key) or "")[:200]
        for key in ("disposition", "reason", "recorded_by", "error")
        if value.get(key)
    }


def run_attempt(
    manager: Any,
    *,
    repo: str | Path,
    task_id: str,
    request_id: str,
    packet_fn: Callable[..., Any] = build_review_packet,
    accept_fn: Callable[..., Any] = run_accept,
) -> dict[str, Any]:
    """Decide eligibility, then accept. Returns the outcome; never raises."""
    outcome: dict[str, Any] = {
        "schema_id": OUTCOME_SCHEMA_ID,
        "actor": ACTOR,
        "task_id": str(task_id),
        "request_id": str(request_id),
    }
    try:
        packet = packet_fn(manager, request_id=request_id, task_id=task_id)
        eligible, reasons = eligibility(packet)
        outcome["eligibility"] = {
            "eligible": eligible,
            "reasons": reasons,
            "facts": eligibility_facts(packet),
        }
        if not eligible:
            return {**outcome, "state": STATE_REFUSED, "reasons": reasons}
        accepted = accept_fn(manager, repo=repo, task_id=task_id, request_id=request_id)
    except Exception as exc:  # noqa: BLE001 -- every failure returns to the manager
        return {
            **outcome,
            "state": STATE_FAILED,
            "reasons": [f"{REASON_INTERNAL_ERROR}:{type(exc).__name__}"],
        }
    if not isinstance(accepted, Mapping) or accepted.get("ok") is not True:
        error = accepted.get("error") if isinstance(accepted, Mapping) else None
        return {
            **outcome,
            "state": STATE_FAILED,
            "reasons": [f"{REASON_ACCEPT_REFUSED}:{error or 'unknown'}"[:400]],
        }
    commit = _commit_facts(_mapping(accepted.get("commit")))
    landed = {
        **outcome,
        "accepted_by": ACTOR,
        "commit": commit,
        "promoted_path_count": accepted.get("promoted_path_count"),
        "learning_disposition": _disposition_facts(accepted.get("learning_disposition")),
    }
    if commit["state"] != COMMIT_STATE_COMMITTED:
        # The accept landed but its commit did not: never report it as
        # auto-accepted; the manager is called to commit it by hand.
        return {
            **landed,
            "state": STATE_ACCEPTED_UNCOMMITTED,
            "reasons": [
                f"{REASON_AUTO_COMMIT}_{commit['state'] or 'unknown'}:"
                f"{commit['reason'] or 'unnamed'}"[:400]
            ],
        }
    return {**landed, "state": STATE_ACCEPTED, "reasons": []}


def interrupted_outcome(task_id: str, request_id: str) -> dict[str, Any]:
    """The outcome of a running row no live runner in this process still owns."""
    return {
        "schema_id": OUTCOME_SCHEMA_ID,
        "actor": ACTOR,
        "task_id": str(task_id),
        "request_id": str(request_id),
        "state": STATE_FAILED,
        "reasons": [REASON_INTERRUPTED],
    }


# ---------------------------------------------------------------------------
# Off-loop runner
# ---------------------------------------------------------------------------


class AutoAcceptRunner:
    """Run attempts off the review drain loop; results are collected, not pushed.

    Sequential by design (one worker): every accept promotes into the one
    canonical tree and commits through the one git index, and the in-process
    accept lock serializes them anyway, so a second worker could only wait.
    The accept's own validation already fans out across cores. Parallelism
    here would change nothing that is measured and could only add index.lock
    contention.
    """

    def __init__(
        self,
        *,
        packet_fn: Callable[..., Any] = build_review_packet,
        accept_fn: Callable[..., Any] = run_accept,
    ) -> None:
        self.owner = f"{ACTOR}:{uuid.uuid4().hex}"
        # This process's identity, persisted with every row it claims, so a
        # runner in another process can tell a live owner from a dead one.
        self.owner_pid = os.getpid()
        self.owner_start_ticks = _own_start_ticks(self.owner_pid)
        self.packet_fn = packet_fn
        self.accept_fn = accept_fn
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[int, Future[dict[str, Any]]] = {}
        self._results: dict[int, dict[str, Any]] = {}
        # Claimed (inside the settle transaction) but not yet submitted.
        self._reserved: set[int] = set()

    def _pool(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="aiworkhub-auto-accept"
            )
        return self._executor

    def submit(
        self, chain_id: int, manager: Any, *, repo: str | Path, task_id: str, request_id: str
    ) -> bool:
        """Start the attempt :meth:`reserve` claimed for ``chain_id``.

        False when ``chain_id`` was never reserved (its claim did not commit,
        or it was released) or its attempt already started.
        """
        chain = int(chain_id)
        with self._lock:
            if chain not in self._reserved or chain in self._futures or chain in self._results:
                return False
            future = self._pool().submit(
                run_attempt,
                manager,
                repo=repo,
                task_id=task_id,
                request_id=request_id,
                packet_fn=self.packet_fn,
                accept_fn=self.accept_fn,
            )
            self._reserved.discard(chain)
            self._futures[chain] = future

        def _collect(done: Future[dict[str, Any]]) -> None:
            try:
                result = done.result()
            except BaseException as exc:  # noqa: BLE001 -- run_attempt never raises; belt only
                result = {
                    **interrupted_outcome(task_id, request_id),
                    "reasons": [f"{REASON_INTERNAL_ERROR}:{type(exc).__name__}"],
                }
            with self._lock:
                self._futures.pop(chain, None)
                self._results[chain] = result

        future.add_done_callback(_collect)
        return True

    def reserve(self, chain_id: int) -> None:
        """Mark ``chain_id`` tracked, with no result, from its claim until submit."""
        with self._lock:
            self._reserved.add(int(chain_id))

    def release(self, chain_id: int) -> None:
        """Drop a reservation whose claim did not commit; a started attempt is kept."""
        with self._lock:
            self._reserved.discard(int(chain_id))

    def tracking(self, chain_id: int) -> bool:
        chain = int(chain_id)
        with self._lock:
            return chain in self._reserved or chain in self._futures or chain in self._results

    def result(self, chain_id: int) -> dict[str, Any] | None:
        """The finished outcome, or None while the attempt still runs."""
        with self._lock:
            return self._results.get(int(chain_id))

    def forget(self, chain_id: int) -> None:
        """Drop a result only after its outcome was durably persisted."""
        with self._lock:
            self._results.pop(int(chain_id), None)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until every submitted attempt has a result (tests, shutdown)."""
        with self._lock:
            futures = list(self._futures.values())
        for future in futures:
            try:
                future.result(timeout=timeout)
            except BaseException:  # noqa: BLE001 -- collected by the done callback
                pass
        with self._lock:
            return not self._futures

    def shutdown(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)


_DEFAULT_RUNNER: AutoAcceptRunner | None = None
_DEFAULT_RUNNER_LOCK = threading.Lock()


def default_runner() -> AutoAcceptRunner:
    """One runner per process, so its owner token is stable across drain passes."""
    global _DEFAULT_RUNNER
    with _DEFAULT_RUNNER_LOCK:
        if _DEFAULT_RUNNER is None:
            _DEFAULT_RUNNER = AutoAcceptRunner()
        return _DEFAULT_RUNNER


# ---------------------------------------------------------------------------
# Attempt ledger (executed on a connection the orchestrator owns)
# ---------------------------------------------------------------------------


class Attempt(NamedTuple):
    chain_id: int
    task_id: str
    request_id: str
    state: str
    owner: str
    outcome: dict[str, Any]
    declined_recorded: bool
    owner_pid: int = 0
    owner_start_ticks: str = ""
    # The ``now`` claim_attempt stored; bounds how long an unknown owner defers.
    started_at: str = ""


class Decision(NamedTuple):
    action: str
    outcome: dict[str, Any] | None = None
    persisted_result: bool = False


def _bounded_json(value: Mapping[str, Any]) -> str:
    text = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
    if len(text.encode("utf-8")) <= MAX_OUTCOME_BYTES:
        return text
    # Keep the verdict; drop the facts that pushed it over the bound.
    slim = {
        key: value[key]
        for key in (
            "schema_id", "actor", "accepted_by", "task_id", "request_id", "state", "reasons",
            "commit", "promoted_path_count",
        )
        if key in value
    }
    slim["truncated"] = True
    return json.dumps(slim, ensure_ascii=False, sort_keys=True, default=str)[:MAX_OUTCOME_BYTES]


def read_attempt(conn: sqlite3.Connection, chain_id: int) -> Attempt | None:
    conn.execute(ATTEMPT_TABLE)
    row = conn.execute(
        "SELECT chain_id, task_id, request_id, state, owner, outcome_json, declined_recorded, "
        "owner_pid, owner_start_ticks, started_at FROM auto_accept_lane_attempts "
        "WHERE chain_id=?",
        (int(chain_id),),
    ).fetchone()
    if row is None:
        return None
    try:
        outcome = json.loads(str(row[5] or "{}"))
    except ValueError:
        outcome = {}
    try:
        owner_pid = int(row[7] or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    return Attempt(
        int(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]),
        outcome if isinstance(outcome, dict) else {}, bool(row[6]),
        owner_pid, str(row[8] or ""), str(row[9] or ""),
    )


def claim_attempt(
    conn: sqlite3.Connection,
    *,
    chain_id: int,
    task_id: str,
    request_id: str,
    owner: str,
    now: str,
    owner_pid: int = 0,
    owner_start_ticks: Any = None,
) -> bool:
    """Insert the chain's one attempt row; False when any row already exists.

    The owner's pid and process start identity are stored with the row, so a
    runner in another process can tell a live owner from a dead one
    (:func:`owner_liveness`) instead of guessing from the wall clock.
    """
    conn.execute(ATTEMPT_TABLE)
    ticks = "" if owner_start_ticks in (None, "") else str(owner_start_ticks)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO auto_accept_lane_attempts"
        "(chain_id, task_id, request_id, state, owner, owner_pid, owner_start_ticks, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(chain_id), str(task_id), str(request_id), STATE_RUNNING, str(owner),
            int(owner_pid or 0), ticks, now,
        ),
    )
    return cursor.rowcount == 1


def finish_attempt(
    conn: sqlite3.Connection, *, chain_id: int, outcome: Mapping[str, Any], now: str
) -> None:
    state = str(outcome.get("state") or "")
    if state not in TERMINAL_STATES:
        state = STATE_FAILED
    conn.execute(
        "UPDATE auto_accept_lane_attempts SET state=?, outcome_json=?, finished_at=? "
        "WHERE chain_id=? AND state=?",
        (state, _bounded_json(outcome), now, int(chain_id), STATE_RUNNING),
    )


def _own_start_ticks(pid: int) -> int | None:
    """This process's start identity, or None when the platform probe cannot read it."""
    from . import process_identity

    try:
        return process_identity._pid_start_ticks(pid)
    except Exception:  # noqa: BLE001 -- unknown identity; owner_liveness then never says dead
        return None


def owner_liveness(owner_pid: Any, owner_start_ticks: Any) -> str:
    """``dead`` only when the repo's pid identity probe proves the owner is gone.

    ``process_identity._pid_identity_evidence`` is the one pid-plus-start-time
    probe: MISMATCH means the pid no longer exists or now belongs to another
    process, so the owner is provably dead; MATCH means it still runs. A row
    with no recorded pid or start identity, or a probe that cannot decide, is
    ``unknown`` -- and unknown is never treated as dead.
    """
    from . import process_identity

    try:
        if int(owner_pid or 0) <= 0 or owner_start_ticks in (None, ""):
            return OWNER_UNKNOWN
        verdict = process_identity._pid_identity_evidence(owner_pid, owner_start_ticks).verdict
    except Exception:  # noqa: BLE001 -- an unreadable probe is unknown, never dead
        return OWNER_UNKNOWN
    if verdict is process_identity.PidIdentityVerdict.MISMATCH:
        return OWNER_DEAD
    if verdict is process_identity.PidIdentityVerdict.MATCH:
        return OWNER_ALIVE
    return OWNER_UNKNOWN


def _task_event(
    conn: sqlite3.Connection, task_id: str, event: str, payload: Mapping[str, Any], now: str
) -> None:
    conn.execute(
        "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(task_id), event, ACTOR, _bounded_json(payload), now),
    )


def callback_payload(chain_id: int, outcome: Mapping[str, Any]) -> dict[str, Any]:
    """The ``auto_accepted`` callback: identity, commit sha, path count, eligibility facts."""
    commit = _mapping(outcome.get("commit"))
    eligibility_section = _mapping(outcome.get("eligibility"))
    count = outcome.get("promoted_path_count")
    return {
        "schema_id": OUTCOME_SCHEMA_ID,
        "actor": ACTOR,
        # accept_review takes no actor argument, so the lane's identity is
        # recorded here and in its accept receipt instead.
        "accepted_by": ACTOR,
        "chain_id": int(chain_id),
        "task_id": str(outcome.get("task_id") or ""),
        "request_id": str(outcome.get("request_id") or ""),
        "commit_sha": str(commit.get("sha") or ""),
        "commit_state": str(commit.get("state") or ""),
        "promoted_path_count": (
            count if isinstance(count, int) and not isinstance(count, bool) else None
        ),
        "eligibility_facts": dict(_mapping(eligibility_section.get("facts"))),
    }


def enqueue_callback(
    conn: sqlite3.Connection, *, task_id: str, request_id: str, claim_epoch: str, now: str
) -> bool:
    """Wake the manager for an auto-accepted chain, inside the caller's transaction.

    The wake is the ``review_ready`` callback the manager already consumes,
    written by ``task_store._enqueue_terminal_callback_row`` -- the
    in-transaction callback authority ``publish_manager_ready`` writes its own
    wake with -- bound to the chain's claim episode and the card's provider and
    origin thread, exactly as ``publish_manager_ready`` resolves them. The card
    is then done; the ``auto_accepted`` task event carries the commit sha and
    the eligibility facts. ``auto_accepted`` itself is not a routable callback
    class (``task_fsm.TERMINAL_CALLBACK_CLASSES`` is bound to the terminal
    substatus vocabulary).

    When that insert is refused because this episode already holds a delivered
    ``review_ready`` row, the delivered rows are re-armed exactly as
    ``publish_manager_ready`` re-arms them (NF-2026-01220). False means no
    pending wake exists for the manager (no task row or origin thread, an
    unroutable provider, or nothing delivered to re-arm); the ``auto_accepted``
    task event records that verdict.
    """
    from . import task_store

    try:
        row = conn.execute(
            "SELECT card_json, origin_thread_id FROM tasks WHERE task_id=?", (str(task_id),)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None:
        return False
    try:
        card = json.loads(str(row[0] or "{}"))
    except ValueError:
        card = {}
    card = card if isinstance(card, dict) else {}
    if task_store._enqueue_terminal_callback_row(
        conn,
        task_id=str(task_id),
        provider=str(card.get("coordinator_provider") or ""),
        origin_thread_id=str(row[1] or card.get("origin_thread_id") or ""),
        transition=CALLBACK_TRANSITION,
        episode_id=str(claim_epoch),
        request_id=str(request_id),
        now=now,
    ):
        return True
    rearmed = conn.execute(
        "UPDATE callback_outbox SET state='pending', batch_id='', lease_id='', "
        "lease_expires_at='', attempts=0, last_error='', updated_at=? "
        "WHERE task_id=? AND episode_id=? AND transition=? AND state='delivered'",
        (now, str(task_id), str(claim_epoch), CALLBACK_TRANSITION),
    ).rowcount
    if not rearmed:
        return False
    conn.execute(
        "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
        "VALUES (?, 'callback_rearmed', 'system', ?, ?)",
        (
            str(task_id),
            json.dumps(
                {
                    "transition": CALLBACK_TRANSITION,
                    "episode_id": str(claim_epoch),
                    "rows": rearmed,
                },
                sort_keys=True,
            ),
            now,
        ),
    )
    return True


def attempted_chain_ids(conn: sqlite3.Connection) -> set[int]:
    """Chain ids holding an attempt row, read WITHOUT creating the lane's table.

    The orchestrator settles every such chain whatever the lane's switch says,
    while a disabled lane with no rows never creates or touches the table.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_accept_lane_attempts'"
    ).fetchone() is None:
        return set()
    return {
        int(row[0])
        for row in conn.execute("SELECT chain_id FROM auto_accept_lane_attempts").fetchall()
    }


def _age_seconds(started_at: str, now: str) -> float | None:
    """Seconds between two ISO timestamps; None when either is unreadable."""
    try:
        start = datetime.fromisoformat(str(started_at))
        return (datetime.fromisoformat(str(now)) - start).total_seconds()
    except (TypeError, ValueError):
        return None


def _foreign_owner_may_run(attempt: Attempt, now: str) -> bool:
    """True while another process's running attempt may still be accepting.

    A live owner keeps its row; a provably dead one does not. An unknown
    liveness defers only while the claim is younger than
    :data:`ATTEMPT_STALE_SECONDS`, so it can never hold a chain forever; an
    unreadable claim time has no provable age and is treated as past the cap.
    """
    liveness = owner_liveness(attempt.owner_pid, attempt.owner_start_ticks)
    if liveness == OWNER_ALIVE:
        return True
    if liveness == OWNER_DEAD:
        return False
    age = _age_seconds(attempt.started_at, now)
    return age is not None and age <= ATTEMPT_STALE_SECONDS


def settle(
    conn: sqlite3.Connection,
    runner: AutoAcceptRunner,
    *,
    chain_id: int,
    task_id: str,
    request_id: str,
    enabled: bool,
    now: str,
    claim_epoch: str = "",
) -> Decision:
    """Decide one completed chain's projection, inside the caller's transaction.

    * no row, lane disabled -> ``manager_ready`` exactly as before the lane;
    * no row, lane enabled -> claim it and ``submit`` (the caller starts the
      runner AFTER its commit, never with SQLite open);
    * running and still running here -> ``defer``: no callback yet; a row this
      runner reserved at its claim but has not started yet also defers;
    * running, owned by another runner whose process is alive -> ``defer``
      with the row left running; likewise while its liveness is unknown and
      the claim is younger than :data:`ATTEMPT_STALE_SECONDS`;
    * running, owner provably dead, unknown past that cap, or this runner no
      longer tracks its own row -> interrupted, which is a failure, so
      ``manager_ready``; an attempt is never re-run;
    * accepted -> write the one ``auto_accepted`` task event and enqueue the one
      ``review_ready`` manager wake (:func:`enqueue_callback`); the caller
      projects the chain in the same transaction, so both are written once;
    * refused/failed/accepted_uncommitted -> write one ``auto_accept_declined``
      event carrying the reasons, then ``manager_ready``.

    ``enabled`` gates only claiming a NEW attempt: an existing row is settled
    whatever it says, so an attempt started before the lane was switched off
    is finished before any manager callback.
    """
    attempt = read_attempt(conn, chain_id)
    if attempt is None:
        if not enabled:
            return Decision(ACTION_MANAGER_READY)
        claimed = claim_attempt(
            conn, chain_id=chain_id, task_id=task_id, request_id=request_id,
            owner=runner.owner, now=now,
            owner_pid=runner.owner_pid, owner_start_ticks=runner.owner_start_ticks,
        )
        if not claimed:
            return Decision(ACTION_DEFER)
        # Tracked from the claim on, inside this transaction: a settle between
        # the caller's commit and its submit defers instead of declaring the
        # row interrupted while the accept is about to start.
        runner.reserve(chain_id)
        return Decision(ACTION_SUBMIT)
    persisted = False
    if attempt.state == STATE_RUNNING:
        outcome: dict[str, Any] | None
        if attempt.owner == runner.owner and runner.tracking(chain_id):
            outcome = runner.result(chain_id)
            if outcome is None:
                return Decision(ACTION_DEFER)
            persisted = True
        elif attempt.owner != runner.owner and _foreign_owner_may_run(attempt, now):
            # Another process owns this attempt and may still be accepting.
            return Decision(ACTION_DEFER)
        else:
            outcome = interrupted_outcome(task_id, request_id)
        finish_attempt(conn, chain_id=chain_id, outcome=outcome, now=now)
        attempt = attempt._replace(
            state=str(outcome.get("state") or STATE_FAILED)
            if str(outcome.get("state") or "") in TERMINAL_STATES
            else STATE_FAILED,
            outcome=dict(outcome),
        )
    if attempt.state == STATE_ACCEPTED:
        payload = callback_payload(chain_id, attempt.outcome)
        payload["claim_epoch"] = str(claim_epoch)
        payload["callback_enqueued"] = enqueue_callback(
            conn, task_id=task_id, request_id=request_id, claim_epoch=claim_epoch, now=now
        )
        _task_event(conn, task_id, EVENT_AUTO_ACCEPTED, payload, now)
        return Decision(ACTION_AUTO_ACCEPTED, attempt.outcome, persisted)
    if not attempt.declined_recorded:
        _task_event(
            conn,
            task_id,
            EVENT_DECLINED,
            {
                "actor": ACTOR,
                "chain_id": int(chain_id),
                "request_id": str(request_id),
                "claim_epoch": str(claim_epoch),
                "state": attempt.state,
                "reasons": list(attempt.outcome.get("reasons") or [])[:MAX_REASONS],
            },
            now,
        )
        conn.execute(
            "UPDATE auto_accept_lane_attempts SET declined_recorded=1 WHERE chain_id=?",
            (int(chain_id),),
        )
    return Decision(ACTION_MANAGER_READY, attempt.outcome, persisted)
