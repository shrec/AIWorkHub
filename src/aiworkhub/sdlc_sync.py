"""One bounded, idempotent pass that records provable SDLC stages (RM-2026-00076 C).

The reconciler scan calls ``sync_once`` so a task's SDLC case and its provable
stages appear without anyone calling a manager tool. The pass has no manager
route, so it never calls the route-bound ``core.sdlc_*`` wrappers: it calls the
same store functions they call -- ``sdlc_case_store.case_for_task`` /
``create_case`` with ``core._sdlc_task_case_id`` and ``append_stage`` -- with the
repository root and id passed explicitly.

Every stage is requested as ``ready`` with a payload derived only from the card
(objective, acceptance, coordinator_provider, risk_tier, allowed_writes,
forbidden and the current claim's request_id/claim_epoch) and, for Deploy, the
policy's first ``deploy.targets`` entry. The store proves it or refuses it; a
refusal leaves the stage unknown and its typed reason is reported, never
forced. Stage request ids digest the payload, so a replay of an unchanged card
is the store's own no-write replay.

A durable cursor over canonical ``task_events`` bounds each pass to the tasks
that changed since the last one; it lives in this module's own SQLite file
under ``.aiworkhub/runtime/``, never in a context store. A Deploy is provable
only once a release is confirmed, usually after the task's last event, so the
same file records a digest of the confirmed (version, release_commit, target)
triples; when it changes, the accepted tasks built before the newest confirmed
release whose Deploy is not ready are requested again, at most
``MAX_TASKS_PER_PASS`` per pass in task-id order with a persisted resume point.
The new digest is recorded only once that set drains without a transient
refusal. Every pass also re-requests Maintain alone, equally capped, for tasks
whose Deploy is ready and Maintain is not, so a cleared band breach or a closed
NeedFix promotes it with an unchanged ledger. After the stages, fix
cards accepted inside the pass are attributed one NeedFix at a time through
``sdlc_attribution.attribute_needfix``, and ``sdlc_control_bands.evaluate`` +
``file_breaches`` run at most once, only when a card was decided since their
last run. Each part is guarded: an exception is reported once as
``sdlc_sync:<part>:failed`` and never propagates. No model is called and no
card is launched.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from . import (
    sdlc_attribution,
    sdlc_case_store,
    sdlc_control_bands,
    sdlc_deploy_proof,
    sdlc_stage_evidence,
    task_store,
)
from .sdlc_deploy_proof import instant as _instant
from .sqlite_readonly import connect_readonly

SCHEMA_ID = "aiworkhub.sdlc_sync.v1"
STATE_DB_REL = (".aiworkhub", "runtime", "sdlc_sync.sqlite")
SYNC_STAGES = ("plan", "design", "build", "test", "deploy", "maintain")
MAX_EVENTS_PER_PASS = 500
MAX_TASKS_PER_PASS = 64
MAX_REPORTED_TASKS = 64
DECISION_EVENTS = frozenset({"accept_review", "reject_review"})
ACCEPT_EVENT = "accept_review"
REVIEWER_TOPIC = "quality_review"
REVIEWER_TASK_PREFIX = "QUALITY_REVIEW_"
UNACCEPTED_TERMINAL_STATUSES = frozenset({"superseded", "archived", "withdrawn"})
REQUEST_PREFIX = "sdlc_sync"
TASK_EVENTS_CURSOR = "task_events"
LAST_DECISION_CURSOR = "last_decision"
BAND_RUN_CURSOR = "band_run"
RELEASE_DIGEST = "confirmed_release_digest"
RELEASE_PENDING = "pending_release_digest"
RELEASE_RESUME = "release_resume_after"
MAINTAIN_RESUME = "maintain_resume_after"
_FIX_CARD_TASK_ID = re.compile(r"needfix-(NF-\d{4}-\d+)(?:-r\d+)?")


def _read_state(path: Path) -> tuple[dict[str, int], dict[str, Any], dict[str, Any]]:
    """Cursors plus the latest reported stage states, read-only; absent means empty."""

    if not path.is_file():
        return {}, {}, {}
    conn = connect_readonly(path)
    try:
        try:
            cursors = {
                str(name): int(value)
                for name, value in conn.execute("SELECT name, event_id FROM sync_cursor")
            }
            rows = conn.execute(
                "SELECT task_id, states_json, reasons_json FROM task_stage_state "
                "ORDER BY event_id DESC, task_id LIMIT ?",
                (MAX_REPORTED_TASKS,),
            ).fetchall()
        except sqlite3.OperationalError:
            # A schema-less file (interrupted first write) holds no state yet.
            return {}, {}, {}
    finally:
        conn.close()
    states = {str(task_id): json.loads(states_json) for task_id, states_json, _ in rows}
    reasons = {
        str(task_id): json.loads(reasons_json)
        for task_id, _, reasons_json in rows
        if reasons_json != "{}"
    }
    return cursors, states, reasons


def _read_pending(path: Path) -> set[str]:
    """NeedFix ids whose attribution failed and awaits a retry; absent means empty."""

    if not path.is_file():
        return set()
    conn = connect_readonly(path)
    try:
        try:
            rows = conn.execute("SELECT needfix_id FROM pending_attribution").fetchall()
        except sqlite3.OperationalError:
            return set()
    finally:
        conn.close()
    return {str(needfix_id) for (needfix_id,) in rows}


def _read_release(path: Path) -> dict[str, str]:
    """The release cursor: recorded digest, the digest being drained and resume points."""

    if not path.is_file():
        return {}
    conn = connect_readonly(path)
    try:
        try:
            rows = conn.execute("SELECT name, value FROM sync_release_cursor").fetchall()
        except sqlite3.OperationalError:
            return {}
    finally:
        conn.close()
    return {str(name): str(value) for name, value in rows}


_UNDEPLOYED_SQL = (
    "json_extract(states_json, '$.test') = 'ready' "
    "AND coalesce(json_extract(states_json, '$.deploy'), '') != 'ready'"
)
_UNMAINTAINED_SQL = (
    "json_extract(states_json, '$.deploy') = 'ready' "
    "AND coalesce(json_extract(states_json, '$.maintain'), '') != 'ready'"
)


def _read_stage_rows(
    path: Path, condition: str, after: str, limit: int, *, through: bool = False
) -> list[tuple[str, int, dict[str, str], dict[str, str]]]:
    """Up to ``limit`` recorded tasks matching ``condition``, in task-id order.

    Rows come after ``after`` or, with ``through``, from the start up to and
    including it -- the wrap-around half of a rotating scan.
    """

    if not path.is_file() or limit <= 0:
        return []
    conn = connect_readonly(path)
    try:
        try:
            rows = conn.execute(
                "SELECT task_id, event_id, states_json, reasons_json FROM task_stage_state "
                f"WHERE {condition} AND task_id {'<=' if through else '>'} ? "
                "ORDER BY task_id LIMIT ?",
                (after, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()
    return [
        (str(task_id), int(event_id), json.loads(states), json.loads(reasons))
        for task_id, event_id, states, reasons in rows
    ]


def _read_cards(root: Path, task_ids: list[str]) -> dict[str, dict[str, Any]] | None:
    """Canonical cards of ``task_ids``, read-only; None while the store is not ready."""

    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        return None
    cards: dict[str, dict[str, Any]] = {}
    if not task_ids:
        return cards
    conn = connect_readonly(readiness.canonical_db)
    try:
        for task_id in task_ids:
            row = conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None or len(row[0] or "") > sdlc_stage_evidence.MAX_TASK_CARD_CHARS:
                continue
            card = json.loads(row[0] or "{}")
            if isinstance(card, dict):
                cards[task_id] = card
    finally:
        conn.close()
    return cards


def _confirmed_release_state(root: Path) -> tuple[str, Any]:
    """Digest of the sorted confirmed (version, release_commit, target) triples, and
    the newest confirmed ``built_at``; ``("", None)`` when nothing is confirmed.

    Keyed on the triples, a version re-confirmed from a new commit is a change.
    """

    releases = sdlc_deploy_proof.confirmed_releases(sdlc_deploy_proof.load_release_ledger(root))
    if not releases:
        return "", None
    triples = sorted(
        [str(built.get(key) or "") for key in ("version", "release_commit", "target")]
        for built, _ in releases
    )
    digest = hashlib.sha256(
        json.dumps(triples, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    built_at = [when for built, _ in releases if (when := _instant(built.get("built_at")))]
    return digest, max(built_at) if built_at else None


def _transient(reason: str | None) -> bool:
    """Whether a recorded ``stage_evidence_refused:<stage>:<code>`` may clear by itself."""

    parts = (reason or "").split(":", 2)
    return len(parts) == 3 and parts[2] in sdlc_deploy_proof.TRANSIENT_REFUSALS


def _write_state(
    path: Path,
    *,
    cursors: dict[str, int] | None = None,
    task_states: dict[str, tuple[int, dict[str, str], dict[str, str]]] | None = None,
    pending_add: set[str] | None = None,
    pending_done: set[str] | None = None,
    release: dict[str, str] | None = None,
) -> None:
    """Upsert cursors, per-task stage states and attribution retries in one short write transaction."""

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sdlc_case_store.connect_writer(path)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sync_cursor ("
                "name TEXT PRIMARY KEY, event_id INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS task_stage_state ("
                "task_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL, "
                "states_json TEXT NOT NULL, reasons_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_attribution (needfix_id TEXT PRIMARY KEY)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sync_release_cursor ("
                "name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for name, value in sorted((release or {}).items()):
                conn.execute(
                    "INSERT INTO sync_release_cursor(name, value) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value "
                    "WHERE value != excluded.value",
                    (name, value),
                )
            for needfix_id in sorted(pending_add or ()):
                conn.execute(
                    "INSERT OR IGNORE INTO pending_attribution(needfix_id) VALUES (?)",
                    (needfix_id,),
                )
            for needfix_id in sorted(pending_done or ()):
                conn.execute(
                    "DELETE FROM pending_attribution WHERE needfix_id=?", (needfix_id,)
                )
            for name, event_id in (cursors or {}).items():
                conn.execute(
                    "INSERT INTO sync_cursor(name, event_id) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET event_id=excluded.event_id "
                    "WHERE event_id != excluded.event_id",
                    (name, int(event_id)),
                )
            for task_id, (event_id, states, reasons) in (task_states or {}).items():
                # An unchanged outcome keeps its row untouched, so a replay of
                # the same card rewrites nothing.
                conn.execute(
                    "INSERT INTO task_stage_state(task_id, event_id, states_json, reasons_json) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                    "event_id=excluded.event_id, states_json=excluded.states_json, "
                    "reasons_json=excluded.reasons_json "
                    "WHERE states_json != excluded.states_json "
                    "OR reasons_json != excluded.reasons_json",
                    (
                        task_id,
                        int(event_id),
                        json.dumps(states, sort_keys=True),
                        json.dumps(reasons, sort_keys=True),
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _read_window(root: Path, after: int) -> dict[str, Any]:
    """Task events after ``after``, bounded by events and by distinct tasks.

    Reads only event ids, task ids, event names and each touched card; never a
    payload. ``last`` is the id of the last event this window consumed.
    """

    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        return {"ready": False, "reason": str(readiness.reason)[:120], "tasks": {}, "last": after}
    conn = connect_readonly(readiness.canonical_db)
    try:
        rows = conn.execute(
            "SELECT event_id, task_id, event FROM task_events WHERE event_id > ? "
            "ORDER BY event_id LIMIT ?",
            (int(after), MAX_EVENTS_PER_PASS),
        ).fetchall()
        tasks: dict[str, dict[str, Any]] = {}
        last = int(after)
        for event_id, task_id, event in rows:
            if task_id not in tasks:
                if len(tasks) >= MAX_TASKS_PER_PASS:
                    break
                tasks[task_id] = {"events": [], "decided": 0, "card": None}
            entry = tasks[task_id]
            entry["events"].append(str(event))
            if event in DECISION_EVENTS:
                entry["decided"] = int(event_id)
            last = int(event_id)
        for task_id, entry in tasks.items():
            row = conn.execute(
                "SELECT card_json, status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is not None and len(row[0] or "") <= sdlc_stage_evidence.MAX_TASK_CARD_CHARS:
                card = json.loads(row[0] or "{}")
                entry["card"] = card if isinstance(card, dict) else None
                entry["status"] = str(row[1] or "")
    finally:
        conn.close()
    return {"ready": True, "tasks": tasks, "last": last}


def _skip_reason(task_id: str, entry: dict[str, Any]) -> str | None:
    """Why a window task is not SDLC work and opens no case, or None when it is.

    A quality-reviewer card is recognised by the binding the reviewer launcher
    writes (``quality_review.target_task_id``), its topic, or -- only as a
    fallback -- its task-id prefix. A superseded/archived/withdrawn task is
    skipped unless it was ever accepted, so an accepted-then-archived task
    keeps its case.
    """

    card = entry.get("card") or {}
    binding = card.get("quality_review")
    if (
        (isinstance(binding, dict) and binding.get("target_task_id"))
        or card.get("topic") == REVIEWER_TOPIC
        or task_id.startswith(REVIEWER_TASK_PREFIX)
    ):
        return "reviewer_card"
    status = entry.get("status") or str(card.get("status") or "")
    if (
        status in UNACCEPTED_TERMINAL_STATUSES
        and ACCEPT_EVENT not in entry.get("events", ())
        and not card.get("accepted_at")
    ):
        return f"never_accepted:{status}"
    return None


def _ensure_case(root: Path, repository_id: str, task_id: str) -> tuple[str, bool]:
    """The task's bound case id, creating it the way core.sdlc_case_create_for_task does."""

    from . import core

    bound = sdlc_case_store.case_for_task(root, repository_id, task_id)
    if bound.get("state") == "bound" and bound.get("case_id"):
        return str(bound["case_id"]), False
    receipt = sdlc_case_store.create_case(
        root,
        repository_id,
        core._sdlc_task_case_id(task_id),
        f"{REQUEST_PREFIX}:case:{task_id}",
        {sdlc_case_store.TASK_LINK_KEY: task_id},
    )
    return str(receipt["case_id"]), not receipt.get("idempotent", False)


def _card_items(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    items: list[str] = []
    for item in values:
        if len(items) >= sdlc_stage_evidence.MAX_CONTENT_ITEMS:
            break
        if isinstance(item, str) and item.strip():
            items.append(item.strip()[: sdlc_stage_evidence.MAX_CONTENT_CHARS])
    return items


def _claim_pointers(task_id: str, card: dict[str, Any]) -> dict[str, Any]:
    """The current claim's identity pointers; an absent one is left for the gate to name."""

    pointers: dict[str, Any] = {"task_id": task_id}
    epoch = card.get("claim_epoch")
    if isinstance(epoch, int) and not isinstance(epoch, bool):
        pointers["claim_epoch"] = epoch
    terminal = card.get("terminal_review")
    sealed = terminal.get("evidence") if isinstance(terminal, dict) else None
    identity = sealed.get("request_identity") if isinstance(sealed, dict) else None
    request_id = identity.get("request_id") if isinstance(identity, dict) else None
    if not request_id or terminal.get("claim_epoch") != epoch:
        request_id = card.get("request_id")
    if isinstance(request_id, str) and request_id:
        pointers["request_id"] = request_id
    return pointers


def _deploy_target(root: Path) -> str:
    """The policy's first deploy target; empty lets the gate refuse it by name."""

    policy = sdlc_deploy_proof.deploy_policy(root)
    return "" if isinstance(policy, str) else str(policy["targets"][0])


def _stage_payload(
    stage: str, task_id: str, card: dict[str, Any], target: str = ""
) -> dict[str, Any]:
    if stage in ("build", "test"):
        return _claim_pointers(task_id, card)
    if stage in ("deploy", "maintain"):
        # Deploy and Maintain name the accepted request, never a claim epoch.
        pointers = {
            key: value for key, value in _claim_pointers(task_id, card).items()
            if key != "claim_epoch"
        }
        return {**pointers, "target": target} if stage == "deploy" and target else pointers
    refs = {"task_id": task_id, "evidence_refs": [f"task:{task_id}"]}
    if stage == "design":
        return {
            **refs,
            "acceptance_criteria": _card_items(card.get("acceptance")),
            "affected_contracts": _card_items(card.get("allowed_writes")),
            "constraints": _card_items(card.get("forbidden")),
            "alternatives": [],
        }
    limit = sdlc_stage_evidence.MAX_CONTENT_CHARS
    objective = _card_items(card.get("objective"))
    owner = _card_items(card.get("coordinator_provider")) or ["manager"]
    fields = {
        "intent": objective[0] if objective else "",
        "problem": objective[0] if objective else "",
        "expected_outcome": "; ".join(_card_items(card.get("acceptance")))[:limit],
        "owner": owner[0],
        "risk": "".join(_card_items(card.get("risk_tier"))),
    }
    # A missing field stays missing so the gate refuses it by name.
    return {**refs, **{name: text for name, text in fields.items() if text}}


def _request_stage(
    root: Path, repository_id: str, case_id: str, stage: str, task_id: str, card: dict[str, Any]
) -> str | None:
    """Request one stage ready; the gate's typed refusal reason, or None when recorded."""

    target = _deploy_target(root) if stage == "deploy" else ""
    payload = _stage_payload(stage, task_id, card, target)
    digest = hashlib.sha256(
        json.dumps([stage, payload], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    try:
        sdlc_case_store.append_stage(
            root, repository_id, case_id, stage, "ready", payload,
            f"{REQUEST_PREFIX}:{stage}:{digest}",
        )
    except sdlc_case_store.SdlcStageEvidenceRefusal as exc:
        return exc.decision.reason
    except (sdlc_case_store.SdlcCaseConflict, sdlc_case_store.SdlcCaseValidationError) as exc:
        return f"sdlc_case_refused:{stage}:{str(exc)[:160]}"
    return None


def _record_stages(
    root: Path, repository_id: str, case_id: str, task_id: str, card: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Request each stage ready in order; the first refusal leaves it and later ones unknown."""

    states: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for stage in SYNC_STAGES:
        if reasons:
            states[stage] = "unknown"
            continue
        reason = _request_stage(root, repository_id, case_id, stage, task_id, card)
        if reason is not None:
            reasons[stage] = reason
        states[stage] = "unknown" if stage in reasons else "ready"
    return states, reasons


def _fix_needfix_id(task_id: str, card: dict[str, Any] | None) -> str:
    declared = (card or {}).get("needfix_id")
    if isinstance(declared, str) and declared:
        return declared
    match = _FIX_CARD_TASK_ID.fullmatch(task_id)
    return match.group(1) if match else ""


def _guarded(result: dict[str, Any], part: str, action: Callable[[], Any]) -> Any:
    try:
        outcome = action()
    except Exception as exc:  # noqa: BLE001 -- a sync part must never fail the scan
        marker = f"sdlc_sync:{part}:failed"
        if marker not in result["failures"]:
            result["failures"].append(marker)
        result["parts"][part] = {"state": "failed", "reason": type(exc).__name__[:80]}
        return None
    result["parts"][part] = outcome["summary"]
    return outcome


def sync_once(repo_root: Path, repository_id: str) -> dict[str, Any]:
    """Run one bounded pass; never raises, and writes nothing when nothing changed."""

    root = Path(repo_root)
    state_path = root.joinpath(*STATE_DB_REL)
    result: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "failures": [],
        "parts": {},
        "stages": {},
        "refusals": {},
    }
    loaded = _guarded(
        result, "cursor", lambda: {"summary": {"state": "read"}, "state": _read_state(state_path)}
    )
    cursors: dict[str, int] = loaded["state"][0] if loaded else {}
    after = cursors.get(TASK_EVENTS_CURSOR, 0)
    window: dict[str, Any] = {"tasks": {}, "last": after}
    cases: dict[str, str] = {}

    def run_cases() -> dict[str, Any]:
        window.update(_read_window(root, after))
        if not window.get("ready", True):
            return {"summary": {"state": "skipped", "reason": window["reason"]}}
        created, refused, skipped = 0, {}, {}
        for task_id, entry in window["tasks"].items():
            if entry["card"] is None:
                refused[task_id] = "task_card_unavailable"
                continue
            # Reviewer cards and never-accepted withdrawn tasks are not SDLC
            # work: no case is opened, an existing one is left untouched, and
            # the cursor still moves past them.
            skip = _skip_reason(task_id, entry)
            if skip:
                skipped[skip] = skipped.get(skip, 0) + 1
                continue
            try:
                case_id, was_created = _ensure_case(root, repository_id, task_id)
            except (sdlc_case_store.SdlcCaseConflict, sdlc_case_store.SdlcCaseValidationError) as exc:
                refused[task_id] = f"sdlc_case_refused:{str(exc)[:160]}"
                continue
            cases[task_id] = case_id
            created += int(was_created)
        return {"summary": {
            "state": "ok", "scanned": len(window["tasks"]), "created": created, "refused": refused,
            "skipped": skipped,
        }}

    window_transient: set[str] = set()

    def run_stages() -> dict[str, Any]:
        outcomes: dict[str, tuple[int, dict[str, str], dict[str, str]]] = {}
        for task_id, case_id in cases.items():
            states, reasons = _record_stages(
                root, repository_id, case_id, task_id, window["tasks"][task_id]["card"]
            )
            outcomes[task_id] = (window["last"], states, reasons)
            if _transient(reasons.get("deploy")):
                window_transient.add(task_id)
        if outcomes:
            _write_state(state_path, task_states=outcomes)
        ready = sum(states.get(stage) == "ready" for _, states, _ in outcomes.values() for stage in SYNC_STAGES)
        return {"summary": {"state": "ok", "tasks": len(outcomes), "ready": ready}}

    def run_attribution() -> dict[str, Any]:
        window_ids = {
            needfix_id
            for task_id, entry in window["tasks"].items()
            if ACCEPT_EVENT in entry["events"]
            for needfix_id in (_fix_needfix_id(task_id, entry["card"]),)
            if needfix_id
        }
        # Persisted retries survive the event cursor moving past the accept
        # event, so one failing NeedFix never stalls case creation for others.
        if window_ids:
            # Durable before attributing, so a failed write or attribution is retried.
            _write_state(state_path, pending_add=window_ids)
        pending = sorted(window_ids | _read_pending(state_path))
        if not pending:
            return {"summary": {"state": "idle"}}
        attributed: dict[str, str] = {}
        failed: dict[str, str] = {}
        for needfix_id in pending:
            try:
                outcome = sdlc_attribution.attribute_needfix(root, repository_id, needfix_id)
            except Exception as exc:  # noqa: BLE001 -- retried next pass
                failed[needfix_id] = type(exc).__name__[:80]
                continue
            attributed[needfix_id] = "attributed" if outcome.attributed else outcome.reason[:80]
        _write_state(state_path, pending_done=set(attributed))
        if not failed:
            return {"summary": {"state": "ok", "attributed": attributed}}
        marker = "sdlc_sync:attribution:failed"
        if marker not in result["failures"]:
            result["failures"].append(marker)
        return {"summary": {"state": "partial", "attributed": attributed, "failed": failed}}

    def run_bands() -> dict[str, Any]:
        decided = max([entry["decided"] for entry in window["tasks"].values()] or [0])
        last_decision = max(cursors.get(LAST_DECISION_CURSOR, 0), decided)
        if last_decision > cursors.get(LAST_DECISION_CURSOR, 0):
            # Durable before evaluating, so a failed band run is retried rather
            # than forgotten once the event cursor moves past the decision.
            _write_state(state_path, cursors={LAST_DECISION_CURSOR: last_decision})
        if last_decision <= cursors.get(BAND_RUN_CURSOR, 0):
            return {"summary": {"state": "idle"}}
        evaluation = sdlc_control_bands.evaluate(root, repository_id)
        filed = sdlc_control_bands.file_breaches(root, repository_id, evaluation)
        _write_state(state_path, cursors={BAND_RUN_CURSOR: last_decision})
        return {"summary": {"state": "ran", "decided_through": last_decision, "filed": filed}}

    touched: set[str] = set()

    def run_release() -> dict[str, Any]:
        # A Deploy becomes provable when a release is confirmed, long after the
        # task's last event, so a changed set of confirmed releases re-requests
        # the stages of the accepted tasks it can contain -- those accepted
        # before the newest confirmed build whose deploy is not ready yet --
        # a bounded batch per pass, resuming in task-id order.
        try:
            digest, cutoff = _confirmed_release_state(root)
        except sdlc_deploy_proof.ReleaseLedgerError:
            return {"summary": {"state": "skipped", "reason": "release_ledger_invalid"}}
        # Only a window task this loop would itself retry -- accepted before the
        # newest confirmed build -- may withhold the digest.
        window_retry = {
            task_id for task_id in window_transient
            for card in (window["tasks"][task_id]["card"] or {},)
            for accepted in (_instant(card.get("accepted_at")),)
            if cutoff is not None and accepted is not None and accepted < cutoff
        }
        recorded = _read_release(state_path)
        if digest == recorded.get(RELEASE_DIGEST, "") and not window_retry:
            return {"summary": {"state": "idle"}}
        resume = recorded.get(RELEASE_RESUME, "") if recorded.get(RELEASE_PENDING) == digest else ""
        rows = _read_stage_rows(state_path, _UNDEPLOYED_SQL, resume, MAX_TASKS_PER_PASS + 1)
        batch, drained = rows[:MAX_TASKS_PER_PASS], len(rows) <= MAX_TASKS_PER_PASS
        # A task recorded by this pass's window already saw the current ledger.
        retry = [row for row in batch if row[0] not in cases]
        cards = _read_cards(root, [row[0] for row in retry]) if cutoff else {}
        if cards is None:
            if window_retry:
                # Unrecord the digest even here, or an unchanged ledger would
                # idle past the window task's transient Deploy refusal.
                _write_state(state_path, task_states={}, release={
                    RELEASE_DIGEST: "", RELEASE_PENDING: digest, RELEASE_RESUME: "",
                })
            return {"summary": {"state": "deferred", "reason": "task_store_not_ready"}}
        outcomes: dict[str, tuple[int, dict[str, str], dict[str, str]]] = {}
        # A transient Deploy refusal anywhere in this pass -- the window's tasks
        # included -- withholds the digest, so that task is retried next pass.
        transient = False
        for task_id, event_id, _, _ in retry:
            card = cards.get(task_id)
            accepted = _instant((card or {}).get("accepted_at"))
            if card is None or cutoff is None or accepted is None or accepted >= cutoff:
                continue
            case_id, _ = _ensure_case(root, repository_id, task_id)
            states, reasons = _record_stages(root, repository_id, case_id, task_id, card)
            outcomes[task_id] = (event_id, states, reasons)
            touched.add(task_id)
            transient = transient or _transient(reasons.get("deploy"))
        if window_retry or transient:
            # The digest is unrecorded -- even one recorded before. A window
            # task may sort before ``resume``, so it restarts from the first
            # undeployed task; a transient batch alone retries the same batch.
            state, cursor = "deferred", {
                RELEASE_DIGEST: "", RELEASE_PENDING: digest,
                RELEASE_RESUME: "" if window_retry else resume,
            }
        elif drained:
            state, cursor = "ran", {RELEASE_DIGEST: digest, RELEASE_PENDING: "", RELEASE_RESUME: ""}
        else:
            state, cursor = "draining", {RELEASE_PENDING: digest, RELEASE_RESUME: batch[-1][0]}
        _write_state(state_path, task_states=outcomes, release=cursor)
        deployed = sum(states.get("deploy") == "ready" for _, states, _ in outcomes.values())
        return {"summary": {"state": state, "retried": len(outcomes), "deployed": deployed}}

    def run_maintain() -> dict[str, Any]:
        # Maintain alone, every pass: a cleared band breach or a closed NeedFix
        # promotes it with an unchanged ledger. A rotating resume point keeps
        # the capped batch moving through every waiting task.
        resume = _read_release(state_path).get(MAINTAIN_RESUME, "")
        rows = _read_stage_rows(state_path, _UNMAINTAINED_SQL, resume, MAX_TASKS_PER_PASS)
        rows += _read_stage_rows(
            state_path, _UNMAINTAINED_SQL, resume, MAX_TASKS_PER_PASS - len(rows), through=True
        )
        if not rows:
            return {"summary": {"state": "idle"}}
        retry = [row for row in rows if row[0] not in cases and row[0] not in touched]
        cards = _read_cards(root, [row[0] for row in retry])
        if cards is None:
            return {"summary": {"state": "deferred", "reason": "task_store_not_ready"}}
        outcomes: dict[str, tuple[int, dict[str, str], dict[str, str]]] = {}
        for task_id, event_id, states, reasons in retry:
            card = cards.get(task_id)
            if card is None:
                continue
            case_id, _ = _ensure_case(root, repository_id, task_id)
            reason = _request_stage(root, repository_id, case_id, "maintain", task_id, card)
            kept = {stage: text for stage, text in reasons.items() if stage != "maintain"}
            outcomes[task_id] = (
                event_id,
                {**states, "maintain": "unknown" if reason else "ready"},
                {**kept, "maintain": reason} if reason else kept,
            )
        _write_state(state_path, task_states=outcomes, release={MAINTAIN_RESUME: rows[-1][0]})
        maintained = sum(states["maintain"] == "ready" for _, states, _ in outcomes.values())
        return {"summary": {"state": "ran", "retried": len(outcomes), "maintained": maintained}}

    cases_ok = _guarded(result, "cases", run_cases) is not None
    # Every Maintain check this pass shares one control-band evaluation.
    with sdlc_deploy_proof.band_report_per_pass():
        stages_ok = _guarded(result, "stages", run_stages) is not None
        _guarded(result, "release", run_release)
        _guarded(result, "maintain", run_maintain)
    _guarded(result, "attribution", run_attribution)
    _guarded(result, "bands", run_bands)
    if cases_ok and stages_ok and window["last"] > after:
        # Only a fully recorded window advances the cursor; a failed one is
        # re-read next pass, where unchanged cards replay without a write.
        _guarded(result, "cursor", lambda: (
            _write_state(state_path, cursors={TASK_EVENTS_CURSOR: window["last"]})
            or {"summary": {"state": "advanced", "from": after, "to": window["last"]}}
        ))
    reported = _guarded(
        result, "report", lambda: {"summary": {"state": "read"}, "state": _read_state(state_path)}
    )
    if reported:
        result["stages"], result["refusals"] = reported["state"][1], reported["state"][2]
    result["ok"] = not result["failures"]
    return result
