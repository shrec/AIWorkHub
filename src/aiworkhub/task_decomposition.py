"""Pure manager-approved task-decomposition proposal contract.

This module never creates, claims, or launches a task.  It validates a bounded
child DAG against one exact canonical Source Graph ``impact``/``deps`` receipt
and returns a content-addressed proposal for explicit manager approval.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from . import task_plan

SCHEMA_ID = "aiworkhub.task_decomposition_proposal.v1"
MAX_CHILDREN = 16
MAX_EVIDENCE_REFS = 16
APPROVAL_BOUNDARY_ID = "aiworkhub_rm17_decomposition_approval_boundary"


class ApprovalActionClass(str, Enum):
    ARCHITECTURE_BROAD_REFACTOR = "architecture_broad_refactor"
    DEPENDENCY_TOOLCHAIN_CHANGE = "dependency_toolchain_change"
    DESTRUCTIVE_STORAGE = "destructive_storage"
    SECURITY_SENSITIVE_CHANGE = "security_sensitive_change"
    RELEASE_PROMOTION = "release_promotion"
    LOW_CONFIDENCE_LARGE_DECOMPOSITION = "low_confidence_large_decomposition"


APPROVAL_ACTION_CLASSES = tuple(action.value for action in ApprovalActionClass)


class TaskDecompositionError(ValueError):
    pass


def approval_boundary(action_class: ApprovalActionClass | str | None) -> dict[str, str]:
    if action_class is None:
        raise TaskDecompositionError("approval_action_class_required")
    try:
        resolved = ApprovalActionClass(action_class)
    except (TypeError, ValueError) as exc:
        raise TaskDecompositionError("unknown_approval_action_class") from exc
    return {
        "boundary_id": APPROVAL_BOUNDARY_ID,
        "action_class": resolved.value,
    }


def _bounded_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise TaskDecompositionError(f"invalid_{field}")
    return text


def _bounded_paths(value: Any, field: str, *, required: bool) -> list[str]:
    if not isinstance(value, list) or len(value) > 128 or (required and not value):
        raise TaskDecompositionError(f"invalid_{field}")
    result: list[str] = []
    for raw in value:
        path = _bounded_text(raw, field, 500).replace("\\", "/")
        parsed = Path(path)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise TaskDecompositionError(f"invalid_{field}_path:{path}")
        if path not in result:
            result.append(path)
    return result


def _receipt_evidence_refs(receipt: Mapping[str, Any]) -> set[str]:
    content = str(receipt.get("content") or "")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise TaskDecompositionError("source_graph_content_invalid") from exc
    found: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if key in {
            "file_path", "callee_file", "caller_file", "target",
            "affected_files", "impacted_files", "qualname", "symbol",
        }:
            text = str(value or "").strip()
            if text:
                found.add(text)

    visit(payload)
    return found


def _validated_source_graph_receipt(
    repo_root: Path, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    if receipt.get("ok") is not True or receipt.get("tool") != "source_graph":
        raise TaskDecompositionError("source_graph_receipt_invalid")
    mode = str(receipt.get("mode") or "")
    if mode not in {"impact", "deps"}:
        raise TaskDecompositionError("source_graph_impact_or_deps_required")
    authority_repo = Path(str(receipt.get("authority_repo") or "")).resolve()
    if authority_repo != repo_root.resolve():
        raise TaskDecompositionError("source_graph_authority_repo_mismatch")
    if receipt.get("authority_source") != "canonical":
        raise TaskDecompositionError("source_graph_authority_not_canonical")
    content = str(receipt.get("content") or "")
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if content_sha256 != str(receipt.get("content_sha256") or ""):
        raise TaskDecompositionError("source_graph_content_hash_mismatch")
    index_revision = _bounded_text(
        receipt.get("index_revision"), "source_graph_index_revision", 200
    )
    evidence_refs = _receipt_evidence_refs(receipt)
    if int(receipt.get("hit_count") or 0) < 1 or not evidence_refs:
        raise TaskDecompositionError("source_graph_evidence_empty")
    return {
        "mode": mode,
        "content_sha256": content_sha256,
        "index_revision": index_revision,
        "evidence_refs": sorted(evidence_refs),
    }


def build_proposal(
    repo_root: Path | str,
    *,
    parent_task_id: str,
    objective: str,
    source_graph_receipt: Mapping[str, Any],
    children: list[Mapping[str, Any]],
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    parent = _bounded_text(parent_task_id, "parent_task_id", 256)
    if not task_plan._TASK_ID_RE.fullmatch(parent):
        raise TaskDecompositionError("invalid_parent_task_id")
    objective_text = _bounded_text(objective, "objective", 4000)
    if not isinstance(children, list) or not 2 <= len(children) <= MAX_CHILDREN:
        raise TaskDecompositionError("children_must_be_bounded_multi_task_list")
    graph = _validated_source_graph_receipt(root, source_graph_receipt)
    graph_refs = set(graph["evidence_refs"])

    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in children:
        if not isinstance(raw, Mapping):
            raise TaskDecompositionError("child_must_be_object")
        task_id = _bounded_text(raw.get("task_id"), "child_task_id", 256)
        if not task_plan._TASK_ID_RE.fullmatch(task_id) or task_id == parent:
            raise TaskDecompositionError(f"invalid_child_task_id:{task_id}")
        if task_id in ids:
            raise TaskDecompositionError(f"duplicate_child_task_id:{task_id}")
        ids.add(task_id)
        writes = _bounded_paths(raw.get("allowed_writes"), "allowed_writes", required=True)
        outputs = _bounded_paths(raw.get("required_outputs"), "required_outputs", required=True)
        for output in outputs:
            if not any(task_plan.paths_conflict(output, allowed) for allowed in writes):
                raise TaskDecompositionError(
                    f"required_output_not_in_child_write_scope:{task_id}:{output}"
                )
        refs = _bounded_paths(raw.get("evidence_refs"), "evidence_refs", required=True)
        if len(refs) > MAX_EVIDENCE_REFS or any(ref not in graph_refs for ref in refs):
            raise TaskDecompositionError(
                f"child_source_graph_evidence_unverified:{task_id}"
            )
        normalized.append({
            "task_id": task_id,
            "title": _bounded_text(raw.get("title"), "child_title", 300),
            "objective": _bounded_text(raw.get("objective"), "child_objective", 4000),
            "allowed_writes": writes,
            "required_outputs": outputs,
            "depends_on": task_plan.normalize_depends_on(raw.get("depends_on")),
            "evidence_refs": refs,
        })

    by_id = {child["task_id"]: child for child in normalized}
    for child in normalized:
        for dependency in child["depends_on"]:
            if dependency not in by_id:
                raise TaskDecompositionError(
                    f"child_dependency_outside_proposal:{child['task_id']}:{dependency}"
                )
    edges = {task_id: list(child["depends_on"]) for task_id, child in by_id.items()}
    for task_id, dependencies in edges.items():
        try:
            task_plan.validate_new_dependency_edge(task_id, dependencies, edges)
        except task_plan.TaskPlanError as exc:
            raise TaskDecompositionError(f"child_dag_invalid:{exc}") from exc

    for index, left in enumerate(normalized):
        for right in normalized[index + 1:]:
            if any(
                task_plan.paths_conflict(a, b)
                for a in left["allowed_writes"]
                for b in right["allowed_writes"]
            ):
                raise TaskDecompositionError(
                    f"child_write_scope_collision:{left['task_id']}:{right['task_id']}"
                )

    snapshot = task_plan.build_snapshot([
        {
            **child,
            "status": "pending",
            "worker_status": "unclaimed",
            "created_at": "",
        }
        for child in normalized
    ])
    if not snapshot["dag_valid"]:
        raise TaskDecompositionError("child_dag_invalid")
    boundary = approval_boundary(
        ApprovalActionClass.LOW_CONFIDENCE_LARGE_DECOMPOSITION
    )
    canonical = {
        "schema_id": SCHEMA_ID,
        "approval_boundary": boundary,
        "parent_task_id": parent,
        "objective": objective_text,
        "source_graph": {
            key: graph[key] for key in ("mode", "content_sha256", "index_revision")
        },
        "children": sorted(normalized, key=lambda child: child["task_id"]),
        "layers": snapshot["layers"],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True,
        **canonical,
        "proposal_digest": digest,
        "status": "proposal",
        "manager_approval_required": True,
        "task_creation_performed": False,
        "task_launch_performed": False,
        "parallel_child_count": len(snapshot["ready"]),
        "claim_boundary": (
            "Source Graph-grounded decomposition proposal only. No throughput, "
            "quality, token, or cost improvement is claimed before matched evidence."
        ),
    }


__all__ = [
    "APPROVAL_ACTION_CLASSES",
    "APPROVAL_BOUNDARY_ID",
    "ApprovalActionClass",
    "MAX_CHILDREN",
    "SCHEMA_ID",
    "TaskDecompositionError",
    "approval_boundary",
    "build_proposal",
]
