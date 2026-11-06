"""Explicit, bounded compaction of finished cards' bulky evidence (NF-2026-02104).

Phase A of the task-store size bound. The canonical ``task_queue.sqlite`` grows
with evidence that no lifecycle code reads again once a card is finished: the
worker-attempt ``terminal_review.evidence`` sub-blocks, duplicated in the
``terminal_review``/``accept_review`` rows of ``task_events``. Every store read
decodes them and every writer holds the lease while it does.

:func:`compact_finished_cards` replaces exactly the sub-blocks the attempt
bundle itself persists (``validation``, ``quality_gate``, ``worker_mcp_gate``)
with the identity stand-in ``mcp_summary_folds`` already uses (``{summarized,
bytes, sha256}`` plus the verdict-like scalars that fold keeps), and only when
the full bytes are durable elsewhere: the bundle named by the attempt's own
``attempt_artifact_manifest`` receipt must resolve inside the repository's
attempt-artifact root, belong to that card's attempt (``request_id`` /
``accepted_request_id``) and re-verify from disk (every artifact re-hashed, the
manifest digest equal to the receipt). Anything else is left exactly as it is;
manager-side blocks (``MANAGER_BLOCKS``: ``accept_evidence``,
``review_feedback``, ``recovery_feedback``, ``history``, ``quality_review``...)
are stored in no bundle and are never touched.

Scope and safety:

* only cards whose canonical status is finished/archived/superseded and whose
  ``accepted_at``/``archived_at`` is older than ``max_age_seconds``;
* never a card named as the ``rework_predecessor`` (by task id or attempt
  request id) or in ``depends_on`` of an unfinished card -- re-checked inside
  every write transaction;
* never a lifecycle-read field (status, ids, sha256 identities,
  ``allowed_writes``, ``validation``, receipts): only allowlisted keys of
  ``terminal_review.evidence`` are rewritten;
* bounded batches, each its own short ``BEGIN IMMEDIATE`` under
  ``task_store``'s write lease, with an exact-preimage compare-and-swap so a
  concurrent writer always wins; ``PRAGMA incremental_vacuum`` after each batch;
* idempotent: a folded block is never folded again, so a second run is a no-op.

``dry_run`` (the default) measures the savings over read-only connections and
writes nothing. Phase B (NF-2026-02106) bounds the two event payloads at write
time through :func:`bound_event_payload` and runs this compaction non-dry-run
from ``storage_retention``'s janitor sweep.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import attempt_artifacts, mcp_summary_folds, storage_retention, task_store

SCHEMA_ID = "aiworkhub.task_store_compaction.v1"
DEFAULT_MAX_AGE_SECONDS = 3 * 86_400
DEFAULT_BATCH_SIZE = 25
MAX_BATCH_SIZE = 500
# A block at or below this many canonical bytes is left exact: its stand-in
# would save little and could even be larger than the block it replaces.
MIN_FOLD_BYTES = 1024

# The one finished-lifecycle vocabulary, owned by storage_retention.
FINISHED_STATUSES = storage_retention._FINISHED_STATUSES
COMPACTED_EVENTS: tuple[str, ...] = ("terminal_review", "accept_review")
# Card fields lifecycle code reads. Compaction rewrites only allowlisted keys of
# ``terminal_review.evidence``, never one of these (asserted at import time below).
LIFECYCLE_READ_FIELDS = frozenset(
    {
        "status",
        "worker_status",
        "task_id",
        "request_id",
        "launch_request_id",
        "accepted_request_id",
        "allowed_writes",
        "validation",
        "accepted_outcome_receipt",
        "manager_ready_receipt",
        "rework_predecessor",
        "depends_on",
        "archived_at",
        "accepted_at",
    }
)
# The only ``terminal_review.evidence`` keys ever folded: exactly what the
# launcher's attempt bundle persists in its ``validation`` role (checks,
# quality_gate, worker_mcp_gate), so their full bytes stay recoverable from
# the verified bundle. Nothing outside this allowlist is folded.
_FOLDABLE_EVIDENCE_KEYS = frozenset({"validation", "quality_gate", "worker_mcp_gate"})
# Manager-side blocks no attempt bundle stores: never rewritten.
MANAGER_BLOCKS: tuple[str, ...] = (
    "accept_evidence",
    "review_feedback",
    "recovery_feedback",
    "history",
    "quality_review",
    "quality_review_receipt",
)
assert not LIFECYCLE_READ_FIELDS.intersection(MANAGER_BLOCKS) and "terminal_review" not in MANAGER_BLOCKS
# Keys inside an evidence block that carry identity, lineage or receipts and
# stay exact even though their container is compacted.
_PRESERVED_EVIDENCE_KEYS = frozenset(
    {
        "attempt_artifact_manifest",
        "request_identity",
        "claim_state",
        "evidence_record",
        "workspace",
        "changed_paths",
        "rework_delta",
        "allowed_writes",
    }
)
_PRESERVED_SUFFIXES = ("_id", "_ids", "sha256", "_hashes", "_receipt", "_identity")
_RECEIPT_SCHEMA = "aiworkhub.attempt_artifact_bundle_receipt.v1"
_ARTIFACT_DIRNAME = "attempt-artifacts"


class CompactionError(RuntimeError):
    """Compaction refused; the message names the cause."""


@dataclass(frozen=True, slots=True)
class _Durable:
    attempt_id: str
    manifest_sha256: str


@dataclass(slots=True)
class _Plan:
    task_id: str
    card_before: str
    card_after: str | None
    # Attempt request ids this card owns, re-checked against lineage under lease.
    refs: frozenset[str] = frozenset()
    events: list[tuple[int, str, str]] = field(default_factory=list)


def _preserved_key(key: str) -> bool:
    # The lifecycle-read ``validation`` is the card's top-level command list,
    # which is never rewritten; an evidence block's ``validation`` is the
    # recorded run output this compaction exists to fold.
    return (
        key in _PRESERVED_EVIDENCE_KEYS
        or (key in LIFECYCLE_READ_FIELDS and key != "validation")
        or key.endswith(_PRESERVED_SUFFIXES)
    )


def _canonical_bytes(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


def _stand_in(value: Any) -> Any:
    """``value`` as its ``{summarized, bytes, sha256}`` stand-in, or ``value`` itself.

    The one identity fold of this module: phase A (``_fold``) and the
    write-time event bound (``bound_event_payload``) both go through it, so an
    event folded at write time is already folded for phase A (idempotent).
    """

    if mcp_summary_folds._is_folded(value):
        return value
    if not isinstance(value, (dict, list, str)) or _canonical_bytes(value) <= MIN_FOLD_BYTES:
        return value
    if isinstance(value, str):
        folded = dict(mcp_summary_folds._fold_to_identity(value))
    else:
        folded = dict(mcp_summary_folds._fold_blob_generic(value))
    # Keep only scalar verdict-like members so a stand-in is always small.
    for key in [k for k, v in folded.items() if isinstance(v, (dict, list))]:
        folded.pop(key)
    return folded


def _fold(value: Any, durable: _Durable) -> Any:
    """``value`` as its identity stand-in, or ``value`` itself when not worth it."""

    folded = _stand_in(value)
    if folded is value:
        return value
    folded["durable_attempt_id"] = durable.attempt_id
    folded["durable_manifest_sha256"] = durable.manifest_sha256
    return folded


def _fold_keys(block: Mapping[str, Any], fold: Any) -> tuple[dict[str, Any], bool]:
    """``block`` with only its ``_FOLDABLE_EVIDENCE_KEYS`` passed through ``fold``."""

    changed = False
    result = dict(block)
    for key, value in block.items():
        if key not in _FOLDABLE_EVIDENCE_KEYS or _preserved_key(str(key)):
            continue
        folded = fold(value)
        if folded is not value:
            result[key] = folded
            changed = True
    return result, changed


def _fold_evidence(block: Any, durable: _Durable) -> tuple[Any, bool]:
    """Fold only the bundle-backed keys of an attempt evidence block."""

    if not isinstance(block, dict):
        return block, False
    return _fold_keys(block, lambda value: _fold(value, durable))


# NF-2026-02106 (phase B): the ``task_events`` rows ``terminal_review`` and
# ``accept_review`` duplicate what the card itself holds, so at write time their
# foldable evidence blocks are stored as identities while the card keeps the
# full blocks. Every field a reader of these two event payloads extracts is
# outside ``_FOLDABLE_EVIDENCE_KEYS`` and therefore stays verbatim:
#   terminal_review: substatus, request_id, runner, claim_epoch, recorded_at,
#     adapter_id, error_hash, card_content_sha256, review_feedback_identity,
#     deterministic_verification(.evidence_verdict), evidence_support, and
#     evidence.{request_identity, workspace, changed_path_hashes,
#     attempt_artifact_manifest, adapter_id, model, request_id, error}
#     (task_store recovery/review_ready_changed_path_hashes/route observations,
#     successful_rework_recovery, process_launcher_accepted_row_restore,
#     launch_replay_guard, history_series, sdlc_control_bands, attempt
#     trajectory export, manager decision latency);
#   accept_review: request_id, accepted_outcome_receipt (sdlc_stage_evidence,
#     sdlc_outcome_metrics, attempt_trajectory_export); dashboard, history_series
#     and manager_decision_counts read only the event name and created_at.
BOUND_EVENTS: tuple[str, ...] = COMPACTED_EVENTS
EVENT_READ_FIELDS: Mapping[str, frozenset[str]] = {
    "terminal_review": frozenset(
        {
            "substatus",
            "request_id",
            "runner",
            "claim_epoch",
            "recorded_at",
            "adapter_id",
            "error_hash",
            "card_content_sha256",
            "review_feedback_identity",
            "deterministic_verification",
            "evidence_support",
            "evidence.request_identity",
            "evidence.workspace",
            "evidence.changed_path_hashes",
            "evidence.attempt_artifact_manifest",
            "evidence.adapter_id",
            "evidence.model",
            "evidence.request_id",
            "evidence.error",
        }
    ),
    "accept_review": frozenset({"request_id", "accepted_outcome_receipt"}),
}
assert not any(
    field.rsplit(".", 1)[-1] in _FOLDABLE_EVIDENCE_KEYS for fields in EVENT_READ_FIELDS.values() for field in fields
)


def bound_event_payload(event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The ``task_events`` payload to store for ``event`` (NF-2026-02106).

    ``terminal_review``: its ``evidence`` foldable blocks become identities.
    ``accept_review``: the payload is the acceptance evidence itself, so its
    own top-level foldable blocks do. Any other event, and every other key, is
    returned exactly. The caller's mapping (the card's block) is never mutated.
    """

    result = dict(payload)
    if event == "terminal_review":
        evidence = result.get("evidence")
        if isinstance(evidence, dict):
            result["evidence"], _changed = _fold_keys(evidence, _stand_in)
    elif event == "accept_review":
        result, _changed = _fold_keys(result, _stand_in)
    return result


def _compact_containers(doc: dict[str, Any], durable: _Durable) -> bool:
    """Compact ``terminal_review.evidence`` of a card or event payload in place.

    Manager-side blocks (``MANAGER_BLOCKS``) are never read or rewritten here.
    """

    terminal = doc.get("terminal_review")
    if isinstance(terminal, dict) and isinstance(terminal.get("evidence"), dict):
        evidence, did = _fold_evidence(terminal["evidence"], durable)
        if did:
            doc["terminal_review"] = {**terminal, "evidence": evidence}
            return True
    return False


def _receipt_of(doc: Mapping[str, Any]) -> Any:
    evidence = doc.get("evidence")
    if isinstance(evidence, dict) and isinstance(evidence.get("attempt_artifact_manifest"), dict):
        return evidence["attempt_artifact_manifest"]
    terminal = doc.get("terminal_review")
    if isinstance(terminal, dict):
        inner = terminal.get("evidence")
        if isinstance(inner, dict):
            return inner.get("attempt_artifact_manifest")
    return None


def _attempt_artifact_root(repo_root: str | Path) -> Path:
    """The resolved root the launcher writes attempt bundles under.

    Same derivation as ``ProcessLauncher.process_dir`` (env override, else the
    repository default) plus the bundle directory name the launcher appends.
    Imported lazily: a dry run should not pay for the launcher import up front.
    """

    from .process_launcher import PROCESS_DIR_DEFAULT_REL, PROCESS_DIR_ENV

    repo = Path(repo_root)
    process_dir = Path(os.environ.get(PROCESS_DIR_ENV) or str(repo / PROCESS_DIR_DEFAULT_REL))
    return (process_dir / _ARTIFACT_DIRNAME).resolve(strict=False)


def _verify_receipt(
    receipt: Any,
    cache: dict[str, _Durable | None],
    artifact_root: Path,
) -> _Durable | None:
    """The durable identity a receipt names, only if its bundle re-verifies now.

    The manifest must resolve (symlinks followed) to
    ``<artifact_root>/<attempt_id>/manifest.json``: a receipt naming a bundle
    anywhere else is never trusted, however well it verifies.
    """

    if not isinstance(receipt, dict):
        return None
    attempt_id = str(receipt.get("attempt_id") or "").strip()
    manifest_sha256 = str(receipt.get("manifest_sha256") or "").strip()
    raw_path = str(receipt.get("manifest_path") or "").strip()
    if (
        receipt.get("schema_id") != _RECEIPT_SCHEMA
        or receipt.get("verified") is not True
        or not attempt_id
        or len(manifest_sha256) != 64
        or not raw_path
    ):
        return None
    key = f"{raw_path}\0{attempt_id}\0{manifest_sha256}"
    if key in cache:
        return cache[key]
    durable: _Durable | None = None
    try:
        manifest_path: Path | None = Path(raw_path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        manifest_path = None
    if (
        manifest_path is not None
        and manifest_path.is_relative_to(artifact_root)
        and manifest_path.name == attempt_artifacts.MANIFEST_FILENAME
        and manifest_path.parent.name == attempt_id
        and manifest_path.parent.parent == artifact_root
    ):
        try:
            verification = attempt_artifacts.verify_json_bundle(manifest_path.parent)
            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        except (OSError, attempt_artifacts.InvalidArtifactError, attempt_artifacts.InvalidManifestError):
            verification, digest = {}, ""
        if (
            verification.get("verified") is True
            and str(verification.get("attempt_id") or "") == attempt_id
            and digest == manifest_sha256
        ):
            durable = _Durable(attempt_id, manifest_sha256)
    if len(cache) >= 4096:
        cache.clear()
    cache[key] = durable
    return durable


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_object(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw or "{}"))
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _dump(value: Mapping[str, Any]) -> str:
    # Same serialization the canonical writers use for card_json.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _protected_lineage(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """Task ids and attempt request ids unfinished cards still reference.

    Unfinished is judged by ``archived_at`` and the ``status`` column only:
    ``worker_status='done'`` is deliberately not consulted, because a card
    still in review whose worker finished (e.g. a rework successor awaiting
    review) must keep its predecessor and dependencies protected. Over-
    protecting only means less is compacted. An unfinished card whose
    card_json cannot be read makes lineage unknown, so this raises and the
    run is refused rather than guessing.
    """

    task_ids: set[str] = set()
    request_ids: set[str] = set()
    rows = conn.execute(
        "SELECT task_id, status, archived_at, card_json FROM tasks "
        "WHERE COALESCE(archived_at, '')='' "
        "AND COALESCE(status, '') NOT IN ('finished', 'completed', 'stale_already_done')"
    )
    for row in rows:
        if task_store.canonical_status({"status": row["status"], "archived_at": row["archived_at"]}) in FINISHED_STATUSES:
            continue
        card = _load_object(row["card_json"])
        if card is None:
            raise CompactionError(f"unfinished_card_unreadable:{row['task_id']}")
        predecessor = card.get("rework_predecessor")
        if isinstance(predecessor, dict):
            for key, bucket in (("task_id", task_ids), ("request_id", request_ids)):
                value = str(predecessor.get(key) or "").strip()
                if value:
                    bucket.add(value)
        depends = card.get("depends_on")
        if isinstance(depends, str):
            depends = [depends]
        if isinstance(depends, list):
            task_ids.update(str(item).strip() for item in depends if str(item or "").strip())
    return task_ids, request_ids


def _card_refs(card: Mapping[str, Any]) -> frozenset[str]:
    """Every attempt request id that identifies ``card``'s own attempts."""

    refs = {str(card.get(key) or "").strip() for key in ("launch_request_id", "accepted_request_id", "request_id")}
    receipt = _receipt_of(card)
    if isinstance(receipt, dict):
        refs.add(str(receipt.get("attempt_id") or "").strip())
    refs.discard("")
    return frozenset(refs)


def _is_protected(task_id: str, refs: frozenset[str], lineage: tuple[set[str], set[str]]) -> bool:
    task_ids, request_ids = lineage
    return task_id in task_ids or not refs.isdisjoint(request_ids)


def _db_stats(db_path: Path) -> dict[str, Any]:
    conn = task_store._connect(db_path, readonly=True)
    try:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
    finally:
        conn.close()
    file_bytes = 0
    for suffix in ("", "-wal"):
        try:
            file_bytes += os.stat(str(db_path) + suffix).st_size
        except FileNotFoundError:
            pass
    return {
        "file_bytes": file_bytes,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "auto_vacuum": auto_vacuum,
    }


def _candidates(db_path: Path, cutoff: datetime) -> list[str]:
    """Finished task ids old enough, smallest key first (deterministic)."""

    conn = task_store._connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT task_id, status, worker_status, archived_at, "
            "CASE WHEN json_valid(card_json) THEN json_extract(card_json, '$.accepted_at') END AS accepted_at, "
            "CASE WHEN json_valid(card_json) THEN json_extract(card_json, '$.archived_at') END AS card_archived_at "
            "FROM tasks WHERE COALESCE(archived_at, '')!='' OR worker_status IN ('done', 'superseded') "
            "OR status IN ('finished', 'completed', 'stale_already_done', 'superseded') "
            "ORDER BY task_id"
        ).fetchall()
    finally:
        conn.close()
    selected: list[str] = []
    for row in rows:
        if task_store.canonical_status(dict(row)) not in FINISHED_STATUSES:
            continue
        stamps = [
            stamp
            for stamp in (
                _parse_time(row["accepted_at"]),
                _parse_time(row["archived_at"]),
                _parse_time(row["card_archived_at"]),
            )
            if stamp is not None
        ]
        # The newest terminal stamp decides; a card with none is never old.
        if stamps and max(stamps) < cutoff:
            selected.append(str(row["task_id"]))
    return selected


def _bound_to_attempt(durable: _Durable, own_request: str, accepted: str) -> bool:
    """A durable bundle vouches only for the attempt it belongs to.

    Its attempt id must equal the record's own ``request_id`` (when present)
    and the card's ``accepted_request_id`` (when the card has one).
    """

    return (not own_request or own_request == durable.attempt_id) and (
        not accepted or accepted == durable.attempt_id
    )


def _plan_batch(
    db_path: Path,
    task_ids: Sequence[str],
    cache: dict[str, _Durable | None],
    counters: dict[str, int],
    artifact_root: Path,
) -> list[_Plan]:
    """Read one batch and compute its compaction; never writes."""

    placeholders = ",".join("?" for _ in task_ids)
    conn = task_store._connect(db_path, readonly=True)
    try:
        lineage = _protected_lineage(conn)
        cards = conn.execute(
            f"SELECT task_id, status, worker_status, archived_at, card_json FROM tasks "
            f"WHERE task_id IN ({placeholders}) ORDER BY task_id",
            tuple(task_ids),
        ).fetchall()
        event_rows = conn.execute(
            f"SELECT event_id, task_id, payload_json FROM task_events "
            f"WHERE task_id IN ({placeholders}) AND event IN ({','.join('?' for _ in COMPACTED_EVENTS)}) "
            f"ORDER BY event_id",
            (*task_ids, *COMPACTED_EVENTS),
        ).fetchall()
    finally:
        conn.close()
    events_by_task: dict[str, list[sqlite3.Row]] = {}
    for row in event_rows:
        events_by_task.setdefault(str(row["task_id"]), []).append(row)
    plans: list[_Plan] = []
    for row in cards:
        task_id = str(row["task_id"])
        if task_store.canonical_status(dict(row)) not in FINISHED_STATUSES:
            counters["skipped_not_finished"] += 1
            continue
        card = _load_object(row["card_json"])
        if card is None:
            counters["skipped_unreadable"] += 1
            continue
        events = [
            (event, payload)
            for event in events_by_task.get(task_id, [])
            if (payload := _load_object(event["payload_json"])) is not None
        ]
        refs = set(_card_refs(card))
        for _event, payload in events:
            receipt = _receipt_of(payload)
            if isinstance(receipt, dict) and str(receipt.get("attempt_id") or "").strip():
                refs.add(str(receipt["attempt_id"]).strip())
        if _is_protected(task_id, frozenset(refs), lineage):
            counters["skipped_protected_lineage"] += 1
            continue
        accepted = str(card.get("accepted_request_id") or "").strip()
        card_durable = _verify_receipt(_receipt_of(card), cache, artifact_root)
        if card_durable is not None and not _bound_to_attempt(card_durable, "", accepted):
            card_durable = None
        plan = _Plan(
            task_id=task_id,
            card_before=str(row["card_json"]),
            card_after=None,
            refs=frozenset(refs),
        )
        if card_durable is None:
            counters["skipped_not_durable"] += 1
        elif _compact_containers(card, card_durable):
            plan.card_after = _dump(card)
        for event, payload in events:
            own_request = str(payload.get("request_id") or "").strip()
            receipt = _receipt_of(payload)
            durable = _verify_receipt(receipt, cache, artifact_root)
            if durable is not None and not _bound_to_attempt(durable, own_request, accepted):
                # A verified bundle of some other attempt never vouches for
                # this card's event; neither does the card's own bundle then.
                counters["skipped_event_unbound"] += 1
                continue
            if durable is None and receipt is None and card_durable is not None:
                # A payload without any receipt is bound to the card's
                # verified attempt only when it names that exact attempt.
                if own_request == card_durable.attempt_id:
                    durable = card_durable
            if durable is None:
                continue
            changed = _compact_containers(payload, durable)
            evidence = payload.get("evidence")
            if isinstance(evidence, dict):
                folded, did = _fold_evidence(evidence, durable)
                if did:
                    payload["evidence"] = folded
                    changed = True
            if changed:
                plan.events.append(
                    (int(event["event_id"]), str(event["payload_json"]), json.dumps(payload, sort_keys=True))
                )
        if plan.card_after is not None or plan.events:
            plans.append(plan)
    return plans


def _apply_batch(db_path: Path, plans: Sequence[_Plan], counters: dict[str, int]) -> float:
    """Commit one batch under one short write lease; return the hold seconds."""

    started = time.monotonic()
    with task_store._write_connection(db_path) as conn:
        task_store._begin_immediate(conn)
        try:
            lineage = _protected_lineage(conn)
            for plan in plans:
                row = conn.execute(
                    "SELECT status, worker_status, archived_at, card_json FROM tasks WHERE task_id=?",
                    (plan.task_id,),
                ).fetchone()
                if (
                    row is None
                    or str(row["card_json"]) != plan.card_before
                    or task_store.canonical_status(dict(row)) not in FINISHED_STATUSES
                    or _is_protected(plan.task_id, plan.refs, lineage)
                ):
                    counters["write_conflicts"] += 1
                    continue
                if plan.card_after is not None:
                    cur = conn.execute(
                        "UPDATE tasks SET card_json=? WHERE task_id=? AND card_json=?",
                        (plan.card_after, plan.task_id, plan.card_before),
                    )
                    if cur.rowcount == 1:
                        counters["cards_compacted"] += 1
                        counters["bytes_saved"] += len(plan.card_before.encode("utf-8")) - len(
                            plan.card_after.encode("utf-8")
                        )
                for event_id, before, after in plan.events:
                    cur = conn.execute(
                        "UPDATE task_events SET payload_json=? WHERE event_id=? AND payload_json=?",
                        (after, event_id, before),
                    )
                    if cur.rowcount == 1:
                        counters["events_compacted"] += 1
                        counters["bytes_saved"] += len(before.encode("utf-8")) - len(after.encode("utf-8"))
                    else:
                        counters["write_conflicts"] += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        # Outside the transaction: return this batch's freed pages to the OS.
        conn.execute("PRAGMA incremental_vacuum").fetchall()
    return time.monotonic() - started


def _ensure_incremental_vacuum(db_path: Path) -> dict[str, Any]:
    """Switch the store to auto_vacuum=INCREMENTAL once (one VACUUM)."""

    with task_store._write_connection(db_path) as conn:
        mode = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        if mode == 2:
            return {"converted": False, "auto_vacuum": "incremental"}
        started = time.monotonic()
        try:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("VACUUM")
        except sqlite3.Error as exc:
            return {"converted": False, "error": f"vacuum_failed:{type(exc).__name__}:{exc}"}
        mode = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        return {
            "converted": mode == 2,
            "auto_vacuum": "incremental" if mode == 2 else str(mode),
            "seconds": round(time.monotonic() - started, 3),
        }


def compact_finished_cards(
    repo_root: str | Path,
    *,
    dry_run: bool = True,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compact durable evidence of old finished cards; ``dry_run`` only measures.

    Raises ``CompactionError`` (named cause) for an invalid bound or an
    unreadable unfinished card, and ``task_store.StorageNotReadyError`` when the
    canonical store is not ready.
    """

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise CompactionError(f"batch_size_out_of_range:1..{MAX_BATCH_SIZE}")
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or max_age_seconds < 0:
        raise CompactionError("max_age_seconds_invalid")
    db_path = task_store.canonical_db_path(repo_root)
    moment = now or datetime.now(timezone.utc)
    cutoff = datetime.fromtimestamp(moment.timestamp() - float(max_age_seconds), tz=timezone.utc)
    counters: dict[str, int] = {
        key: 0
        for key in (
            "cards_compacted",
            "events_compacted",
            "bytes_saved",
            "write_conflicts",
            "skipped_not_finished",
            "skipped_unreadable",
            "skipped_protected_lineage",
            "skipped_not_durable",
            "skipped_event_unbound",
        )
    }
    before = _db_stats(db_path)
    candidates = _candidates(db_path, cutoff)
    artifact_root = _attempt_artifact_root(repo_root)
    cache: dict[str, _Durable | None] = {}
    batches = 0
    max_lease = 0.0
    planned_cards = planned_events = planned_saving = 0
    # Converted before the first batch so every batch's incremental_vacuum
    # already returns its freed pages; a dry run never writes.
    vacuum: dict[str, Any] = {"converted": False, "skipped": "dry_run"} if dry_run else _ensure_incremental_vacuum(db_path)
    for start in range(0, len(candidates), batch_size):
        plans = _plan_batch(db_path, candidates[start : start + batch_size], cache, counters, artifact_root)
        for plan in plans:
            if plan.card_after is not None:
                planned_cards += 1
                planned_saving += len(plan.card_before.encode("utf-8")) - len(plan.card_after.encode("utf-8"))
            planned_events += len(plan.events)
            planned_saving += sum(len(b.encode("utf-8")) - len(a.encode("utf-8")) for _, b, a in plan.events)
        if dry_run or not plans:
            continue
        batches += 1
        max_lease = max(max_lease, _apply_batch(db_path, plans, counters))
    after = before if dry_run else _db_stats(db_path)
    return {
        "schema_id": SCHEMA_ID,
        "ok": "error" not in vacuum,
        "dry_run": dry_run,
        "max_age_seconds": max_age_seconds,
        "batch_size": batch_size,
        "candidates": len(candidates),
        "planned": {
            "cards": planned_cards,
            "events": planned_events,
            "bytes_saved": planned_saving,
        },
        **counters,
        "write_batches": batches,
        "max_write_lease_seconds": round(max_lease, 3),
        "db_before": before,
        "db_after": after,
        "vacuum": vacuum,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aiworkhub.task_store_compaction",
        description="Compact durable evidence of finished task cards (dry run unless --apply).",
    )
    parser.add_argument("--repo", required=True, help="repository root")
    parser.add_argument("--apply", action="store_true", help="write the compaction (default: dry run)")
    parser.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        report = compact_finished_cards(
            args.repo,
            dry_run=not args.apply,
            max_age_seconds=args.max_age_seconds,
            batch_size=args.batch_size,
        )
    except (CompactionError, task_store.TaskStoreError) as exc:
        print(json.dumps({"schema_id": SCHEMA_ID, "ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
