"""Bounded executor for the authenticated review lifecycle outbox."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import (
    needfix_store,
    review_lifecycle,
    task_engine,
    task_store,
    workforce_catalog,
    workforce_router,
)


LENSES = ("correctness", "security", "code_quality")
RECEIPT_SCHEMA = "aiworkhub.review_orchestrator_receipt.v1"

# The actions whose whole purpose is to drive ONE candidate through review.
# A target that has left `review` cannot be driven through it, so attempting
# them can only fail -- and a failed action parks every later action in its
# chain, which is how 129 chains and 1,389 actions became permanently
# unreservable here. ``needfix_close`` is deliberately absent: it is
# bookkeeping that stays meaningful after the review ended.
REVIEW_DRIVING_ACTIONS = frozenset({
    "launch", "accept", "archive", "target_accept", "target_archive",
})
# Statuses a target can still be driven through review from. Anything else is
# a decided outcome, and fail-closed means an UNREADABLE card counts as still
# reviewable -- never retire an action on a card we could not read.
REVIEWABLE_TARGET_STATUSES = frozenset({"review", "processing", "pending"})
MAX_RECEIPT_BYTES = 16 * 1024


class Manager(Protocol):
    repo: Path

    def _append_event(self, event: Mapping[str, Any]) -> None: ...
    def launch_quality_reviewer(self, **kwargs: Any) -> dict[str, Any]: ...
    def accept_review(self, request_id: str, task_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def status(self, request_id: str) -> dict[str, Any]: ...


RouteSelector = Callable[[Path, str, str], Mapping[str, Any]]


def canonical_review_db(manager: Manager) -> Path | None:
    """Return the sole task-store DB, or no authority for an unready fake repo."""
    readiness = task_store.storage_readiness(manager.repo)
    return Path(readiness.canonical_db) if readiness.ready else None


def select_reviewer_route(repo: Path, reviewer_task_id: str, lens: str) -> Mapping[str, Any]:
    """Select one currently available review worker through canonical policy."""
    readiness = task_store.storage_readiness(repo)
    if not readiness.ready:
        raise RuntimeError("review_route_storage_not_ready:" + readiness.reason)
    task = workforce_router.TaskRequirements.build(
        task_id=reviewer_task_id,
        repo_id=readiness.repo_id,
        kinds=("review",),
        risk="critical",
        tool_needs=("source-graph", "session-manager", "ai-memory", "kb"),
    )
    contract = workforce_catalog.rank_task(repo, task).get("launch_contract")
    if not isinstance(contract, Mapping):
        raise RuntimeError("review_route_unavailable:" + lens)
    route = {
        "runner": str(contract.get("runner") or ""),
        "adapter_id": str(contract.get("adapter_id") or ""),
        "model": str(contract.get("model") or ""),
    }
    if not all(route.values()) or route["runner"] == "codex":
        raise RuntimeError("review_route_identity_invalid")
    return route


def register_finalized_candidate(
    manager: Manager,
    *,
    db_path: str | Path,
    metadata: Mapping[str, Any],
    artifact_receipt: Mapping[str, Any],
    changed_path_hashes: Mapping[str, Any],
) -> review_lifecycle.ReviewChain:
    """Bind the automatic chain to the exact sealed candidate transition."""
    registration = candidate_registration(
        metadata=metadata,
        artifact_receipt=artifact_receipt,
        changed_path_hashes=changed_path_hashes,
    )
    return register_candidate(manager, db_path=db_path, registration=registration)


def candidate_registration(
    *,
    metadata: Mapping[str, Any],
    artifact_receipt: Mapping[str, Any],
    changed_path_hashes: Mapping[str, Any],
) -> dict[str, str]:
    """Return the bounded durable preimage needed to retry chain creation."""
    candidate_json = json.dumps(
        dict(changed_path_hashes), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    )
    return {
        "target_task_id": str(metadata["task_id"]),
        "target_request_id": str(metadata["request_id"]),
        "claim_epoch": str(metadata["claim_epoch"]),
        "packet_sha256": str(artifact_receipt.get("manifest_sha256") or ""),
        "candidate_sha256": hashlib.sha256(candidate_json.encode("utf-8")).hexdigest(),
    }


def register_candidate(
    manager: Manager,
    *,
    db_path: str | Path,
    registration: Mapping[str, Any],
) -> review_lifecycle.ReviewChain:
    """Create or replay one exact chain from a durable registration preimage."""
    return ReviewOrchestrator(manager, db_path=db_path).ensure_chain(
        target_task_id=str(registration["target_task_id"]),
        target_request_id=str(registration["target_request_id"]),
        claim_epoch=str(registration["claim_epoch"]),
        packet_sha256=str(registration["packet_sha256"]),
        candidate_sha256=str(registration["candidate_sha256"]),
    )


def retry_pending_registrations(
    manager: Manager,
    *,
    db_path: str | Path,
    events: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Replay each latest durable pending seed once per reconciliation pass."""
    retried = seeded = failed = 0
    failures: list[dict[str, str]] = []
    for event in events.values():
        automation = event.get("review_automation")
        if not isinstance(automation, dict) or automation.get("state") != "pending":
            continue
        registration = automation.get("registration")
        if not isinstance(registration, dict):
            continue
        retried += 1
        try:
            chain = register_candidate(manager, db_path=db_path, registration=registration)
        except Exception as exc:  # one durable failure receipt per bounded pass
            failed += 1
            error = f"{type(exc).__name__}:{exc}"[:300]
            failures.append({
                "request_id": str(event.get("request_id") or ""),
                "error": error,
            })
            manager._append_event({
                **dict(event),
                "review_automation": {
                    "state": "pending",
                    "registration": dict(registration),
                    "error": error,
                },
            })
        else:
            seeded += 1
            manager._append_event({
                **dict(event),
                "review_automation": {
                    "state": "seeded",
                    "registration": dict(registration),
                    "chain_identity_sha256": str(
                        getattr(chain, "chain_identity_sha256", "")
                    ),
                },
            })
    return {
        "automation_retried": retried,
        "automation_seeded": seeded,
        "automation_failed": failed,
        "automation_failures": failures,
    }


@dataclass(frozen=True, slots=True)
class DrainResult:
    attempted: int
    completed: int
    failed: int
    pending: int
    counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "completed": self.completed,
            "failed": self.failed,
            "pending": self.pending,
            "review_actions": dict(self.counts),
        }


class _DeferredLaunch(RuntimeError):
    """A recoverable launch gate result, kept distinct from effect failures."""

    def __init__(self, reason: str, receipt: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.receipt = dict(receipt)


class ReviewOrchestrator:
    """Execute at most ``max_actions`` effects, never while SQLite is open."""

    def __init__(
        self,
        manager: Manager,
        *,
        db_path: str | Path,
        route_selector: RouteSelector | None = None,
        owner: str = "process-manager-review-driver",
        lease_seconds: int = 300,
    ) -> None:
        self.manager = manager
        self.db_path = Path(db_path)
        self.route_selector = route_selector or select_reviewer_route
        self.owner = str(owner)
        self.lease_seconds = max(1, int(lease_seconds))

    def ensure_chain(
        self,
        *,
        target_task_id: str,
        target_request_id: str,
        claim_epoch: str | int,
        packet_sha256: str,
        candidate_sha256: str,
        now: datetime | None = None,
    ) -> review_lifecycle.ReviewChain:
        chain = review_lifecycle.create_or_replay_chain(
            self.db_path,
            target_task_id=target_task_id,
            target_request_id=target_request_id,
            claim_epoch=claim_epoch,
            packet_sha256=packet_sha256,
            candidate_sha256=candidate_sha256,
            now=now,
        )
        self._bind_expected_workspace(chain)
        return chain

    def _bind_expected_workspace(self, chain: review_lifecycle.ReviewChain) -> None:
        """Persist the original workspace identity outside immutable lifecycle rows."""
        identity = chain.chain_identity
        expected = ""
        try:
            status = self.manager.status(str(identity["target_request_id"]))
            card = status.get("task_card") if isinstance(status, Mapping) else None
            if (
                isinstance(card, Mapping)
                and str(card.get("task_id") or "") == str(identity["target_task_id"])
                and str(card.get("request_id") or "") == str(identity["target_request_id"])
                and str(card.get("claim_epoch") or "") == str(identity["claim_epoch"])
                and str(card.get("packet_sha256") or "") == str(identity["packet_sha256"])
                and str(card.get("candidate_sha256") or "") == str(identity["candidate_sha256"])
            ):
                expected = str(card.get("workspace_identity") or "")
        except Exception:
            pass
        self._repair_expected_workspace(chain.chain_id, expected)

    def _repair_expected_workspace(self, chain_id: int, workspace_identity: str) -> None:
        """Bind a later verified workspace only while the retained binding is empty."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS review_orchestrator_workspace_bindings "
                "(chain_id INTEGER PRIMARY KEY, workspace_identity TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO review_orchestrator_workspace_bindings "
                "(chain_id, workspace_identity) VALUES (?, ?)",
                (chain_id, workspace_identity),
            )
            if workspace_identity:
                conn.execute(
                    "UPDATE review_orchestrator_workspace_bindings "
                    "SET workspace_identity=? WHERE chain_id=? AND workspace_identity=''",
                    (workspace_identity, chain_id),
                )

    def _expected_workspace_identity(self, chain_id: int) -> str:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT workspace_identity FROM review_orchestrator_workspace_bindings "
                "WHERE chain_id=?",
                (chain_id,),
            ).fetchone()
        return str(row[0]) if row is not None else ""

    def drain(self, *, max_actions: int = 1, now: datetime | None = None) -> DrainResult:
        """Claim and execute a bounded number of lifecycle effects exactly once."""
        instant = now or datetime.now(timezone.utc)
        attempted = completed = failed = pending = 0
        for _ in range(max(0, min(int(max_actions), 12))):
            token = uuid.uuid4().hex
            action = review_lifecycle.reserve_next_action(
                self.db_path,
                owner=self.owner,
                lease_token=token,
                now=instant,
                lease_seconds=self.lease_seconds,
            )
            if action is None:
                break
            attempted += 1
            try:
                receipt = self._execute(action)
                if receipt is None:
                    review_lifecycle.defer_action(
                        self.db_path, action_id=action.action_id, owner=self.owner,
                        lease_token=token, now=instant,
                    )
                    pending += 1
                    break
                self._validate_receipt(action, receipt)
            except _DeferredLaunch as deferred:
                self._record_deferred_wait(action, deferred, instant)
                review_lifecycle.defer_action(
                    self.db_path, action_id=action.action_id, owner=self.owner,
                    lease_token=token, now=instant,
                )
                pending += 1
                break
            except Exception as exc:  # fail closed and stop this chain/pass
                review_lifecycle.fail_action(
                    self.db_path,
                    action_id=action.action_id,
                    owner=self.owner,
                    lease_token=token,
                    reason=f"{type(exc).__name__}:{exc}",
                    now=instant,
                )
                failed += 1
                break
            # Deliberately outside the effect-error handler: a crash or store
            # fault here leaves the lease reclaimable and never converts a
            # successful external effect into a terminal action failure.
            review_lifecycle.complete_action(
                self.db_path,
                action_id=action.action_id,
                owner=self.owner,
                lease_token=token,
                receipt=receipt,
                now=instant,
            )
            completed += 1
        return DrainResult(
            attempted, completed, failed, pending,
            review_lifecycle.lifecycle_counts(self.db_path),
        )

    def _execute(self, action: review_lifecycle.ReviewAction) -> dict[str, Any] | None:
        identity = action.descriptor["chain_identity"]
        target_task = str(identity["target_task_id"])
        target_request = str(identity["target_request_id"])
        reviewer_task = self._reviewer_task_id(identity, action.lens)
        prior = self._receipts(action.chain_id)
        if action.action_type in REVIEW_DRIVING_ACTIONS:
            decided = self._target_left_review(target_task)
            if decided:
                # Retiring an obsolete action is a real terminal outcome, not a
                # failure: there was nothing left to do. Failing it instead
                # parked the rest of its chain forever.
                return self._receipt(
                    action,
                    obsolete_reason=f"target_left_review:{decided}",
                    result={"ok": True, "state": "obsolete", "task_id": target_task},
                )
        if action.action_type == "launch":
            readiness = self._launch_readiness(action)
            if readiness["outcome"] == "deferred":
                raise _DeferredLaunch(str(readiness["reason"]), readiness)
            if readiness["outcome"] == "terminal":
                raise RuntimeError(str(readiness["reason"]))
            route = dict(self.route_selector(self.manager.repo, reviewer_task, action.lens))
            runner = str(route.get("runner") or "")
            adapter_id = str(route.get("adapter_id") or "")
            model = str(route.get("model") or "")
            if not runner or not adapter_id or not model or runner == "codex":
                raise RuntimeError("review_route_identity_invalid")
            result = self.manager.launch_quality_reviewer(
                target_request_id=target_request,
                target_task_id=target_task,
                reviewer_task_id=reviewer_task,
                runner=runner,
                adapter_id=adapter_id,
                model=model,
                lens=action.lens,
            )
            self._require_ok(result, "reviewer_launch_failed")
            request_id = str(result.get("request_id") or "")
            if not request_id or str(result.get("task_id") or reviewer_task) != reviewer_task:
                raise RuntimeError("reviewer_launch_identity_invalid")
            return self._receipt(
                action, reviewer_task_id=reviewer_task,
                reviewer_request_id=request_id, reviewer_route=route,
                target_readiness_receipt=readiness, result=result,
            )
        if action.action_type == "accept":
            launch = self._lens_receipt(prior, action.lens, "launch")
            reviewer_request = str(launch["reviewer_request_id"])
            status = self.manager.status(reviewer_request)
            if str(status.get("state") or "") in {
                "starting", "running", "processing", "finalizing", "reconcile_pending"
            }:
                return None
            receipt = self._review_receipt(
                action, status, reviewer_request, reviewer_task
            )
            findings = receipt["report"]["findings"]
            if any(
                finding.get("actionable") is True
                or finding.get("disposition") == "defect"
                for finding in findings
            ):
                raise RuntimeError("reviewer_actionable_findings")
            result = self.manager.accept_review(reviewer_request, reviewer_task)
            self._require_ok(result, "reviewer_accept_failed")
            return self._receipt(action, reviewer_task_id=reviewer_task,
                                 reviewer_request_id=reviewer_request, result=result)
        if action.action_type == "archive":
            accepted = self._lens_receipt(prior, action.lens, "accept")
            launch = self._lens_receipt(prior, action.lens, "launch")
            result = task_engine.archive_task(
                self.manager.repo, reviewer_task,
                actor=str((launch.get("reviewer_route") or {}).get("runner") or "system"),
                reason=f"automatic review accepted:{target_request}",
            )
            if result.get("ok") is not True and not self._is_archived(reviewer_task):
                self._require_ok(result, "reviewer_archive_failed")
            return self._receipt(
                action, reviewer_task_id=reviewer_task,
                reviewer_request_id=str(accepted["reviewer_request_id"]), result=result,
            )
        # Computed per branch, not up front: only the two target actions carry
        # reviewer identities into their effect. needfix_close never used them,
        # and hoisting the lookup made it fail with KeyError on a chain whose
        # reviewer actions had been retired as obsolete -- bookkeeping dying of
        # a dependency it did not have.
        def _reviewer_ids() -> list[str]:
            return [
                str(self._lens_receipt(prior, lens, "accept")["reviewer_request_id"])
                for lens in LENSES
            ]

        if action.action_type == "target_accept":
            reviewer_ids = _reviewer_ids()
            result = self.manager.accept_review(
                target_request, target_task, reviewer_request_ids=reviewer_ids
            )
            self._require_ok(result, "target_accept_failed")
            return self._receipt(action, reviewer_request_ids=reviewer_ids, result=result)
        if action.action_type == "target_archive":
            reviewer_ids = _reviewer_ids()
            result = task_engine.archive_task(
                self.manager.repo, target_task, actor="system",
                reason=f"automatic review chain complete:{target_request}",
            )
            if result.get("ok") is not True and not self._is_archived(target_task):
                self._require_ok(result, "target_archive_failed")
            return self._receipt(action, reviewer_request_ids=reviewer_ids, result=result)
        if action.action_type == "needfix_close":
            linked = self._linked_needfix_rows(target_task)
            newly_resolved: list[str] = []
            for row in linked:
                if row["status"] != "task_created":
                    continue
                needfix_id = str(row["id"])
                resolved = needfix_store.resolve_needfix(
                    self.manager.repo,
                    needfix_id,
                    resolution_note=(
                        "automatic review lifecycle accepted and archived task "
                        + target_task
                    ),
                )
                if (
                    resolved.get("id") != needfix_id
                    or resolved.get("status") != "resolved"
                    or resolved.get("converted_task_id") != target_task
                ):
                    raise RuntimeError("needfix_close_receipt_invalid")
                newly_resolved.append(needfix_id)
            return self._receipt(
                action,
                needfix_ids=sorted(str(row["id"]) for row in linked),
                needfix_newly_resolved=sorted(newly_resolved),
                needfix_closed_count=len(linked),
                result={"ok": True, "state": "resolved"},
            )
        raise RuntimeError("unknown_review_action")

    def _launch_readiness(self, action: review_lifecycle.ReviewAction) -> dict[str, Any]:
        """Bind one launch evaluation to the retained canonical target envelope."""
        identity = action.descriptor["chain_identity"]
        target_request = str(identity["target_request_id"])
        target_task = str(identity["target_task_id"])
        try:
            status = self.manager.status(target_request)
        except Exception as exc:
            return self._readiness_receipt(
                action, "deferred", "target_status_unavailable:" + type(exc).__name__
            )
        if not isinstance(status, Mapping) or status.get("ok") is not True:
            return self._readiness_receipt(action, "deferred", "target_status_unavailable")
        card = status.get("task_card")
        if not isinstance(card, Mapping):
            return self._readiness_receipt(action, "deferred", "target_card_missing")
        if str(card.get("task_id") or "") != target_task:
            return self._readiness_receipt(action, "terminal", "target_task_identity_invalid")
        if str(card.get("request_id") or "") != target_request:
            return self._readiness_receipt(action, "terminal", "target_request_identity_invalid")
        if str(card.get("claim_epoch") or "") != str(identity["claim_epoch"]):
            return self._readiness_receipt(action, "terminal", "target_claim_identity_invalid")
        if str(card.get("packet_sha256") or "") != str(identity["packet_sha256"]):
            return self._readiness_receipt(action, "terminal", "target_packet_identity_invalid")
        if str(card.get("candidate_sha256") or "") != str(identity["candidate_sha256"]):
            return self._readiness_receipt(action, "terminal", "target_candidate_identity_invalid")
        if str(status.get("state") or "") != "review_ready":
            return self._readiness_receipt(action, "deferred", "target_not_review_ready")
        workspace = str(card.get("workspace_identity") or "")
        if not workspace:
            return self._readiness_receipt(action, "deferred", "target_workspace_identity_missing")
        expected_workspace = self._expected_workspace_identity(action.chain_id)
        if not expected_workspace:
            # Chain registration may predate canonical target availability.
            # Bind only after all immutable identities above have matched.
            self._repair_expected_workspace(action.chain_id, workspace)
            expected_workspace = self._expected_workspace_identity(action.chain_id)
            if not expected_workspace:
                return self._readiness_receipt(
                    action, "terminal", "target_workspace_identity_unbound"
                )
        if workspace != expected_workspace:
            return self._readiness_receipt(
                action, "terminal", "target_workspace_identity_invalid"
            )
        evidence = card.get("evidence")
        partitions = (
            evidence.get("source_graph_partition_readiness")
            if isinstance(evidence, Mapping)
            else None
        )
        if not isinstance(partitions, Mapping) or not partitions:
            return self._readiness_receipt(action, "deferred", "source_graph_partition_empty")
        if any(value is not True for value in partitions.values()):
            return self._readiness_receipt(action, "deferred", "source_graph_partition_not_ready")
        return self._readiness_receipt(action, "ready", "ready", workspace, partitions)

    @staticmethod
    def _readiness_receipt(
        action: review_lifecycle.ReviewAction,
        outcome: str,
        reason: str,
        workspace_identity: str = "",
        partitions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = action.descriptor["chain_identity"]
        return {
            "schema_id": "aiworkhub.review_target_readiness_receipt.v1",
            "action_id": action.action_id,
            "target_task_id": identity["target_task_id"],
            "target_request_id": identity["target_request_id"],
            "claim_epoch": identity["claim_epoch"],
            "packet_sha256": identity["packet_sha256"],
            "candidate_sha256": identity["candidate_sha256"],
            "workspace_identity": workspace_identity,
            "partition_readiness": dict(partitions or {}),
            "outcome": outcome,
            "reason": reason,
        }

    def _record_deferred_wait(
        self, action: review_lifecycle.ReviewAction, deferred: _DeferredLaunch, now: datetime
    ) -> None:
        append = getattr(self.manager, "_append_event", None)
        if not callable(append):
            return
        identity = action.descriptor["chain_identity"]
        append({
            "event_type": "review_orchestrator_wait",
            "request_id": identity["target_request_id"],
            "task_id": identity["target_task_id"],
            "review_automation": {
                "state": "deferred",
                "reason": deferred.reason,
                "action_id": action.action_id,
                "retry_after_seconds": 60,
                "recorded_at": now.isoformat(),
                "readiness_receipt": deferred.receipt,
            },
        })

    def _linked_needfix_rows(self, target_task_id: str) -> list[dict[str, Any]]:
        """Return every durable NeedFix row already bound to this exact task."""
        linked: list[dict[str, Any]] = []
        for status in ("task_created", "resolved"):
            offset = 0
            while True:
                page = needfix_store.list_needfix(
                    self.manager.repo,
                    status=status,
                    include_archived=True,
                    limit=500,
                    offset=offset,
                    order_by="created_at",
                    order_dir="ASC",
                )
                linked.extend(
                    row
                    for row in page
                    if str(row.get("converted_task_id") or "") == target_task_id
                )
                if len(page) < 500:
                    break
                offset += len(page)
        return linked

    def _receipt(self, action: review_lifecycle.ReviewAction, **payload: Any) -> dict[str, Any]:
        result = payload.pop("result", {})
        bounded_result = {
            key: result[key] for key in ("ok", "already_accepted", "already_reserved",
                                         "request_id", "task_id", "state") if key in result
        }
        return {
            "schema_id": RECEIPT_SCHEMA,
            "action_id": action.action_id,
            "action_index": action.action_index,
            "action_type": action.action_type,
            "lens": action.lens,
            "descriptor_sha256": action.descriptor_sha256,
            "target_task_id": action.descriptor["target_task_id"],
            "target_request_id": action.descriptor["target_request_id"],
            **payload,
            "result": bounded_result,
        }

    def _validate_receipt(self, action: review_lifecycle.ReviewAction,
                          receipt: Mapping[str, Any]) -> None:
        if (
            receipt.get("schema_id") != RECEIPT_SCHEMA
            or receipt.get("action_id") != action.action_id
            or receipt.get("action_index") != action.action_index
            or receipt.get("action_type") != action.action_type
            or receipt.get("lens") != action.lens
            or receipt.get("descriptor_sha256") != action.descriptor_sha256
            or receipt.get("target_task_id") != action.descriptor["target_task_id"]
            or receipt.get("target_request_id") != action.descriptor["target_request_id"]
        ):
            raise RuntimeError("external_receipt_binding_invalid")
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise RuntimeError("external_receipt_too_large")

    def _receipts(self, chain_id: int) -> list[dict[str, Any]]:
        return list(review_lifecycle.completed_receipts_for_chain(self.db_path, chain_id))

    @staticmethod
    def _review_receipt(
        action: review_lifecycle.ReviewAction,
        status: Mapping[str, Any],
        reviewer_request: str,
        reviewer_task: str,
    ) -> dict[str, Any]:
        if status.get("ok") is not True or status.get("state") != "review_ready":
            raise RuntimeError("reviewer_terminal_receipt_missing")
        event = status.get("latest_event")
        card = status.get("task_card")
        if not isinstance(event, Mapping) or not isinstance(card, Mapping):
            raise RuntimeError("reviewer_terminal_receipt_missing")
        terminal = card.get("terminal_review")
        evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
        event_receipt = event.get("quality_review_receipt")
        card_receipt = evidence.get("quality_review_receipt") if isinstance(evidence, Mapping) else None
        if not isinstance(event_receipt, Mapping) or event_receipt != card_receipt:
            raise RuntimeError("reviewer_terminal_receipt_mismatch")
        receipt = json.loads(json.dumps(event_receipt, ensure_ascii=False))
        target, reviewer, report, authority = (
            receipt.get("target"), receipt.get("reviewer"),
            receipt.get("report"), receipt.get("authority"),
        )
        identity = action.descriptor["chain_identity"]
        if not all(isinstance(value, dict) for value in (target, reviewer, report, authority)):
            raise RuntimeError("reviewer_receipt_shape_invalid")
        if (
            receipt.get("packet_sha256") != identity["packet_sha256"]
            or target.get("request_id") != identity["target_request_id"]
            or target.get("task_id") != identity["target_task_id"]
            or str(target.get("claim_epoch")) != identity["claim_epoch"]
            or reviewer.get("request_id") != reviewer_request
            or reviewer.get("task_id") != reviewer_task
            or reviewer.get("provider") != status.get("adapter_id")
            or report.get("provider") != status.get("adapter_id")
            or report.get("lens") != action.lens
            or report.get("read_only") is not True
            or report.get("can_mutate_repo") is not False
            or not isinstance(report.get("findings"), list)
            or authority != {
                "process_identity_verified": True,
                "audit_verified": True,
                "terminal_state": "review_ready",
            }
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("submission_id") or ""))
            or receipt.get("physical_submission_count") != 1
            or receipt.get("logical_submission_count") != 1
        ):
            raise RuntimeError("reviewer_receipt_binding_invalid")
        if not all(isinstance(finding, dict) for finding in report["findings"]):
            raise RuntimeError("reviewer_findings_invalid")
        return receipt

    @staticmethod
    def _lens_receipt(receipts: list[dict[str, Any]], lens: str,
                      action_type: str) -> dict[str, Any]:
        matches = [r for r in receipts if r.get("lens") == lens
                   and r.get("action_type") == action_type]
        if len(matches) != 1:
            raise RuntimeError("reviewer_receipt_missing_or_duplicate")
        return matches[0]

    @staticmethod
    def _require_ok(result: Mapping[str, Any], prefix: str) -> None:
        if result.get("ok") is not True:
            detail = result.get("error") or result.get("stderr") or "unknown"
            raise RuntimeError(prefix + ":" + str(detail))

    def _target_left_review(self, task_id: str) -> str:
        """Canonical status once the target is no longer a review surface.

        Returns "" when the target can still be driven through review AND when
        the card cannot be read at all -- an unreadable card must never cause
        an action to be retired, only a card that is readably decided.
        """
        try:
            status = task_engine.show_task(self.manager.repo, task_id)
            if status.get("returncode") != 0:
                return ""
            card = json.loads(str(status.get("stdout") or ""))
        except Exception:
            return ""
        if not isinstance(card, dict):
            return ""
        canonical = task_store.canonical_status(card)
        return "" if canonical in REVIEWABLE_TARGET_STATUSES else str(canonical)

    def _is_archived(self, task_id: str) -> bool:
        try:
            status = task_engine.show_task(self.manager.repo, task_id)
            if status.get("returncode") != 0:
                return False
            card = json.loads(str(status.get("stdout") or ""))
        except Exception:
            return False
        return bool(str(card.get("archived_at") or "").strip())

    @staticmethod
    def _reviewer_task_id(identity: Mapping[str, Any], lens: str) -> str:
        preimage = json.dumps({"identity": dict(identity), "lens": lens},
                              sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "QUALITY_REVIEW_" + hashlib.sha256(preimage.encode()).hexdigest()[:24].upper()
