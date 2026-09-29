"""One bounded, idempotent pass that records provable SDLC stages (RM-2026-00076 C).

The reconciler scan calls ``sync_once`` so a task's SDLC case and its provable
stages appear without anyone calling a manager tool. The pass has no manager
route, so it never calls the route-bound ``core.sdlc_*`` wrappers: it calls the
same store functions they call -- ``sdlc_case_store.case_for_task`` /
``create_case`` with ``core._sdlc_task_case_id`` and ``append_stage`` -- with the
repository root and id passed explicitly.

Every stage is requested as ``ready`` with a payload derived only from the card
(objective, acceptance, coordinator_provider, risk_tier, allowed_writes,
forbidden and the current claim's request_id/claim_epoch). The store proves it
or refuses it; a refusal leaves the stage unknown and its typed reason is
reported, never forced. Stage request ids digest the payload, so a replay of an
unchanged card is the store's own no-write replay.

A durable cursor over canonical ``task_events`` bounds each pass to the tasks
that changed since the last one; it lives in this module's own SQLite file
under ``.aiworkhub/runtime/``, never in a context store. After the stages, fix
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
    sdlc_stage_evidence,
    task_store,
)
from .sqlite_readonly import connect_readonly

SCHEMA_ID = "aiworkhub.sdlc_sync.v1"
STATE_DB_REL = (".aiworkhub", "runtime", "sdlc_sync.sqlite")
SYNC_STAGES = ("plan", "design", "build", "test")
MAX_EVENTS_PER_PASS = 500
MAX_TASKS_PER_PASS = 64
MAX_REPORTED_TASKS = 64
DECISION_EVENTS = frozenset({"accept_review", "reject_review"})
ACCEPT_EVENT = "accept_review"
REQUEST_PREFIX = "sdlc_sync"
TASK_EVENTS_CURSOR = "task_events"
LAST_DECISION_CURSOR = "last_decision"
BAND_RUN_CURSOR = "band_run"
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


def _write_state(
    path: Path,
    *,
    cursors: dict[str, int] | None = None,
    task_states: dict[str, tuple[int, dict[str, str], dict[str, str]]] | None = None,
    pending_add: set[str] | None = None,
    pending_done: set[str] | None = None,
) -> None:
    """Upsert cursors, per-task stage states and attribution retries in one short write transaction."""

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
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
                "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is not None and len(row[0] or "") <= sdlc_stage_evidence.MAX_TASK_CARD_CHARS:
                card = json.loads(row[0] or "{}")
                entry["card"] = card if isinstance(card, dict) else None
    finally:
        conn.close()
    return {"ready": True, "tasks": tasks, "last": last}


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


def _stage_payload(stage: str, task_id: str, card: dict[str, Any]) -> dict[str, Any]:
    if stage in ("build", "test"):
        return _claim_pointers(task_id, card)
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
        payload = _stage_payload(stage, task_id, card)
        digest = hashlib.sha256(
            json.dumps([stage, payload], sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        try:
            sdlc_case_store.append_stage(
                root, repository_id, case_id, stage, "ready", payload,
                f"{REQUEST_PREFIX}:{stage}:{digest}",
            )
        except sdlc_case_store.SdlcStageEvidenceRefusal as exc:
            reasons[stage] = exc.decision.reason
        except (sdlc_case_store.SdlcCaseConflict, sdlc_case_store.SdlcCaseValidationError) as exc:
            reasons[stage] = f"sdlc_case_refused:{stage}:{str(exc)[:160]}"
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
        created, refused = 0, {}
        for task_id, entry in window["tasks"].items():
            if entry["card"] is None:
                refused[task_id] = "task_card_unavailable"
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
        }}

    def run_stages() -> dict[str, Any]:
        outcomes: dict[str, tuple[int, dict[str, str], dict[str, str]]] = {}
        for task_id, case_id in cases.items():
            states, reasons = _record_stages(
                root, repository_id, case_id, task_id, window["tasks"][task_id]["card"]
            )
            outcomes[task_id] = (window["last"], states, reasons)
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

    cases_ok = _guarded(result, "cases", run_cases) is not None
    stages_ok = _guarded(result, "stages", run_stages) is not None
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
