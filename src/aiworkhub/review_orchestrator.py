"""Bounded executor for the authenticated review lifecycle outbox."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
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
        return review_lifecycle.create_or_replay_chain(
            self.db_path,
            target_task_id=target_task_id,
            target_request_id=target_request_id,
            claim_epoch=claim_epoch,
            packet_sha256=packet_sha256,
            candidate_sha256=candidate_sha256,
            now=now,
        )

    def drain(self, *, max_actions: int = 1, now: datetime | None = None) -> DrainResult:
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
        if action.action_type == "launch":
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
                reviewer_request_id=request_id, reviewer_route=route, result=result,
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
        reviewer_ids = [
            str(self._lens_receipt(prior, lens, "accept")["reviewer_request_id"])
            for lens in LENSES
        ]
        if action.action_type == "target_accept":
            result = self.manager.accept_review(
                target_request, target_task, reviewer_request_ids=reviewer_ids
            )
            self._require_ok(result, "target_accept_failed")
            return self._receipt(action, reviewer_request_ids=reviewer_ids, result=result)
        if action.action_type == "target_archive":
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
