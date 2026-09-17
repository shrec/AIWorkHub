#!/usr/bin/env python3
"""Deterministic builder for the accepted-task continuous-evaluation corpus.

Selects 20-50 accepted task trajectories from the canonical AIWorkHub task
store -- stratified across task family (topic), risk tier and outcome
complexity -- using ``attempt_trajectory_export`` to authenticate each
candidate's accepted-outcome receipt against ``task_engine``'s own canonical
authority (the same authority ``export_attempt_trajectory`` binds). Nothing
here invokes a model or trusts chat prose: every row is derived from a
receipt that actually re-verified against sealed evidence, and a receipt that
fails authentication is silently excluded rather than counted.

Two entry points:

* ``python3 scripts/build_accepted_task_eval.py [--repo-root PATH]`` rebuilds
  the corpus from the live canonical task store and registers it with
  ``eval_artifact_gate``.
* ``python3 scripts/build_accepted_task_eval.py --check [--repo-root PATH]``
  recomputes the summary purely from the already-committed rows file (no
  task store access, no model) and reports any drift between the rows, the
  summary and ``eval_artifact_gate``'s own recomputation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from aiworkhub import attempt_trajectory_export as trajectory_export
from aiworkhub import eval_artifact_gate, task_store

SCHEMA_ID = "aiworkhub.accepted_task_trajectories.v1"
ROW_SCHEMA_ID = "aiworkhub.accepted_task_trajectories.row.v1"
ARTIFACT_ID = "accepted-task-trajectories-v1"

SUMMARY_RELATIVE_PATH = Path("eval/aiworkhub_accepted_task_trajectories_v1.json")
ROWS_RELATIVE_PATH = Path("eval/aiworkhub_accepted_task_trajectories_rows_v1.jsonl")
REGISTRY_RELATIVE_PATH = eval_artifact_gate.REGISTRY_RELATIVE_PATH

MIN_ROWS = 20
MAX_ROWS = 50

_RISK_TIERS: tuple[str, ...] = ("low", "medium", "high", "critical")
_DEFAULT_RISK_TIER = "low"

_REQUIRED_ROW_FIELDS: tuple[str, ...] = (
    "task_id", "request_id", "task_family", "risk_tier",
    "outcome_complexity", "row_sha256",
)


class AcceptedTaskEvalError(ValueError):
    """Base class for a refused corpus build or check. Always fail closed."""


class InsufficientAcceptedTrajectoriesError(AcceptedTaskEvalError):
    """Fewer than ``MIN_ROWS`` authenticated accepted trajectories exist."""


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _risk_tier(card: dict[str, Any]) -> str:
    value = str(card.get("risk_tier") or "").strip().lower()
    return value if value in _RISK_TIERS else _DEFAULT_RISK_TIER


def complexity_tier(promoted_path_count: int) -> str:
    """Bucket outcome complexity from the receipt's own promoted-path count.

    Derived only from authenticated receipt evidence -- never from chat
    prose or a subjective label -- so the same accepted trajectory always
    lands in the same tier.
    """
    if promoted_path_count <= 1:
        return "trivial"
    if promoted_path_count <= 5:
        return "small"
    if promoted_path_count <= 15:
        return "moderate"
    return "large"


def discover_candidates(repo_root: Path) -> list[dict[str, Any]]:
    """Authenticate every card's accepted-outcome receipt via the real authority.

    A card whose receipt fails authentication (tampered, forged, identity
    mismatch, contradictory terminal signals) is excluded rather than
    counted -- ``export_attempt_trajectory`` is the sole authority for what
    "accepted" means here.

    ``manager_decisions`` and ``usage_rows`` are each a whole-store snapshot
    taken once, up front, and reused for every card. Letting each card's
    ``export_attempt_trajectory`` call re-run those whole-table queries turns
    an N-card store into N full-table scans (O(N^2) total); fetching them
    once here keeps the rebuild bounded by the store's own size regardless
    of how many candidate cards it holds.
    """
    try:
        cards = task_store.list_task_cards(repo_root, limit=5000)
    except task_store.TaskStoreError as exc:
        raise AcceptedTaskEvalError(f"task_store_unavailable:{exc}") from exc
    try:
        manager_decisions = task_store.latest_manager_decisions(repo_root)
    except task_store.TaskStoreError as exc:
        raise AcceptedTaskEvalError(f"task_store_unavailable:{exc}") from exc
    try:
        usage_rows = task_store.list_usage_events(repo_root, limit=10_000)
    except task_store.TaskStoreError as exc:
        raise AcceptedTaskEvalError(f"task_store_unavailable:{exc}") from exc

    candidates: list[dict[str, Any]] = []
    for card in cards:
        task_id = str(card.get("task_id") or "").strip()
        request_id = str(card.get("accepted_request_id") or "").strip()
        if not task_id or not request_id:
            continue
        try:
            trajectory = trajectory_export.export_attempt_trajectory(
                repo_root, task_id=task_id, request_id=request_id,
                manager_decisions=manager_decisions, usage_rows=usage_rows,
            )
        except trajectory_export.AttemptTrajectoryExportError:
            continue
        if trajectory["outcome"]["state"] != "accepted":
            continue
        receipt = trajectory["outcome"]["accepted_outcome_receipt"] or {}
        promoted = receipt.get("promoted_paths")
        promoted_count = len(promoted) if isinstance(promoted, list) else 0
        candidates.append({
            "task_id": task_id,
            "request_id": request_id,
            "trajectory": trajectory,
            "family": str(trajectory.get("topic") or trajectory_export.UNKNOWN),
            "risk_tier": _risk_tier(card),
            "complexity": complexity_tier(promoted_count),
        })
    return candidates


def select_representative(
    candidates: list[dict[str, Any]], *, min_rows: int = MIN_ROWS, max_rows: int = MAX_ROWS,
) -> list[dict[str, Any]]:
    """Deterministically pick 20-50 candidates spread across every stratum.

    Candidates are grouped by (family, risk_tier, complexity), each group is
    sorted by (task_id, request_id), and selection round-robins across
    groups in sorted-key order so every represented stratum contributes
    before any stratum contributes twice. This depends only on candidate
    content, never on discovery order or wall-clock time, so it is
    reproducible given the same underlying evidence.
    """
    if len(candidates) < min_rows:
        raise InsufficientAcceptedTrajectoriesError(
            f"only {len(candidates)} authenticated accepted trajectories available, "
            f"need at least {min_rows}"
        )
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        key = (candidate["family"], candidate["risk_tier"], candidate["complexity"])
        groups.setdefault(key, []).append(candidate)
    for members in groups.values():
        members.sort(key=lambda c: (c["task_id"], c["request_id"]))
    ordered_keys = sorted(groups)
    target = min(len(candidates), max_rows)

    cursors = {key: 0 for key in ordered_keys}
    selected: list[dict[str, Any]] = []
    while len(selected) < target:
        progressed = False
        for key in ordered_keys:
            if len(selected) >= target:
                break
            idx = cursors[key]
            members = groups[key]
            if idx < len(members):
                selected.append(members[idx])
                cursors[key] = idx + 1
                progressed = True
        if not progressed:
            break
    return sorted(selected, key=lambda c: (c["task_id"], c["request_id"]))


def _row_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    trajectory = candidate["trajectory"]
    receipt = trajectory["outcome"]["accepted_outcome_receipt"] or {}
    body = {
        "schema_id": ROW_SCHEMA_ID,
        "task_id": candidate["task_id"],
        "request_id": candidate["request_id"],
        "runner": trajectory.get("runner", trajectory_export.UNKNOWN),
        "task_family": candidate["family"],
        "risk_tier": candidate["risk_tier"],
        "outcome_complexity": candidate["complexity"],
        "outcome_state": trajectory["outcome"]["state"],
        "accepted_outcome_receipt_id": receipt.get("receipt_id", trajectory_export.UNKNOWN),
        "promoted_path_count": len(receipt.get("promoted_paths") or []),
        "validations_state": trajectory["validations"]["state"],
        "usage_state": trajectory["usage"]["state"],
        "events_count": len(trajectory["events"]),
        "reviews_count": len(trajectory["reviews"]),
        "task_status": trajectory.get("task_status", trajectory_export.UNKNOWN),
    }
    body["row_sha256"] = _digest(body)
    return body


def build_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = select_representative(candidates)
    rows = [_row_from_candidate(candidate) for candidate in selected]
    rows.sort(key=lambda row: (row["task_id"], row["request_id"]))
    return rows


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows_sha256 = hashlib.sha256(
        b"\n".join(
            str(row.get("row_sha256") or "").encode("ascii")
            for row in sorted(rows, key=lambda row: (row["task_id"], row["request_id"]))
        )
    ).hexdigest()
    return {
        "schema_id": SCHEMA_ID,
        "record_type": "accepted_task_trajectory_eval_corpus",
        "verdict": "PASS" if MIN_ROWS <= len(rows) <= MAX_ROWS else "FAIL",
        "record_count": len(rows),
        "minimum_rows": MIN_ROWS,
        "maximum_rows": MAX_ROWS,
        "task_families": sorted({row["task_family"] for row in rows}),
        "risk_tiers": sorted({row["risk_tier"] for row in rows}),
        "outcome_complexity_tiers": sorted({row["outcome_complexity"] for row in rows}),
        "total_events_count": sum(row["events_count"] for row in rows),
        "all_rows_accepted": bool(rows) and all(row["outcome_state"] == "accepted" for row in rows),
        "rows_sha256": rows_sha256,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ordered = sorted(rows, key=lambda row: (row["task_id"], row["request_id"]))
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in ordered
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )


def registry_entry() -> dict[str, Any]:
    return {
        "id": ARTIFACT_ID,
        "summary_path": SUMMARY_RELATIVE_PATH.as_posix(),
        "rows_path": ROWS_RELATIVE_PATH.as_posix(),
        "row_format": "jsonl",
        "required_row_fields": list(_REQUIRED_ROW_FIELDS),
        "minimum_rows": MIN_ROWS,
        "pass_values": ["PASS"],
        "count_fields": ["record_count"],
        "aggregates": [
            {"summary_field": "total_events_count", "row_field": "events_count", "operation": "sum"},
            {
                "summary_field": "all_rows_accepted",
                "row_field": "outcome_state",
                "operation": "all",
                "equals": "accepted",
            },
        ],
        "verdict_field": "verdict",
    }


def _update_registry(repo_root: Path) -> None:
    registry_path = repo_root / REGISTRY_RELATIVE_PATH
    if registry_path.is_file():
        document = json.loads(registry_path.read_text(encoding="utf-8"))
    else:
        document = {"schema_id": eval_artifact_gate.SCHEMA_ID, "artifacts": []}
    artifacts = [
        dict(entry) for entry in document.get("artifacts", []) if entry.get("id") != ARTIFACT_ID
    ]
    artifacts.append(registry_entry())
    artifacts.sort(key=lambda entry: str(entry.get("id") or ""))
    document = {**document, "schema_id": eval_artifact_gate.SCHEMA_ID, "artifacts": artifacts}
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )


def verify_provenance(repo_root: Path, rows: list[dict[str, Any]]) -> None:
    """Re-authenticate every row's identity against the live canonical store.

    A row's own ``row_sha256`` only proves the row is internally
    self-consistent -- a fabricated (task_id, request_id, receipt) triple can
    compute a valid digest over its own fabricated content just as easily as
    a genuine one. This is the explicit canonical-source check: each row
    must resolve, live, to a task card whose own authenticated
    accepted-outcome receipt matches the row's declared identity and receipt
    id, or the row is refused as provenance-absent. This is intentionally
    separate from ``check()``, which is documented to never touch the task
    store; this function is the one place that re-derives "is this row real"
    from the sealed source rather than from the row's own claims about
    itself.
    """
    if not rows:
        return
    try:
        manager_decisions = task_store.latest_manager_decisions(repo_root)
        usage_rows = task_store.list_usage_events(repo_root, limit=10_000)
    except task_store.TaskStoreError as exc:
        raise AcceptedTaskEvalError(f"task_store_unavailable:{exc}") from exc
    for row in rows:
        task_id = str(row.get("task_id") or "")
        request_id = str(row.get("request_id") or "")
        try:
            trajectory = trajectory_export.export_attempt_trajectory(
                repo_root, task_id=task_id, request_id=request_id,
                manager_decisions=manager_decisions, usage_rows=usage_rows,
            )
        except trajectory_export.AttemptTrajectoryExportError as exc:
            raise AcceptedTaskEvalError(
                f"provenance_absent_from_sealed_source:{task_id}:{request_id}:{exc}"
            ) from exc
        if trajectory["outcome"]["state"] != "accepted":
            raise AcceptedTaskEvalError(
                f"provenance_absent_from_sealed_source:{task_id}:{request_id}:not_accepted_live"
            )
        live_receipt = trajectory["outcome"]["accepted_outcome_receipt"] or {}
        live_receipt_id = live_receipt.get("receipt_id", trajectory_export.UNKNOWN)
        if live_receipt_id != row.get("accepted_outcome_receipt_id"):
            raise AcceptedTaskEvalError(
                f"provenance_receipt_mismatch:{task_id}:{request_id}"
            )


def rebuild(repo_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Query the live canonical task store and (re)write the tracked corpus.

    Fails closed via ``AcceptedTaskEvalError`` when fewer than ``MIN_ROWS``
    authenticated accepted trajectories are available -- a thin/empty corpus
    is never written as if it passed -- and again if any selected row's
    provenance does not re-authenticate live via ``verify_provenance``.
    """
    candidates = discover_candidates(repo_root)
    rows = build_rows(candidates)
    verify_provenance(repo_root, rows)
    summary = build_summary(rows)
    _write_json(repo_root / SUMMARY_RELATIVE_PATH, summary)
    _write_jsonl(repo_root / ROWS_RELATIVE_PATH, rows)
    _update_registry(repo_root)
    return summary, rows


def check(repo_root: Path) -> dict[str, Any]:
    """Recompute drift purely from committed files -- no task store, no model.

    Verifies each row's own digest binding, rejects duplicate trajectory
    identity, enforces the 20-50 row bound, recomputes the summary from the
    rows and reuses ``eval_artifact_gate.evaluate`` for the registered
    count/aggregate truth check.
    """
    summary_path = repo_root / SUMMARY_RELATIVE_PATH
    rows_path = repo_root / ROWS_RELATIVE_PATH
    reasons: list[str] = []
    if not summary_path.is_file() or not rows_path.is_file():
        return {"passed": False, "reasons": ["artifact_missing"], "row_count": 0}

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    seen_identity: set[tuple[str, str]] = set()
    for row in rows:
        identity = (str(row.get("task_id")), str(row.get("request_id")))
        if identity in seen_identity:
            reasons.append(f"duplicate_trajectory_identity:{identity[0]}:{identity[1]}")
        seen_identity.add(identity)
        declared = row.get("row_sha256")
        recomputed = _digest({key: value for key, value in row.items() if key != "row_sha256"})
        if declared != recomputed:
            reasons.append(f"row_digest_drift:{identity[0]}:{identity[1]}")

    if not (MIN_ROWS <= len(rows) <= MAX_ROWS):
        reasons.append(f"row_count_out_of_bounds:{len(rows)}")

    recomputed_summary = build_summary(rows)
    for key, expected in recomputed_summary.items():
        if summary.get(key) != expected:
            reasons.append(f"summary_field_drift:{key}")

    gate_report = eval_artifact_gate.evaluate(
        repo_root, changed_paths=[SUMMARY_RELATIVE_PATH.as_posix(), ROWS_RELATIVE_PATH.as_posix()],
    )
    if not gate_report["passed"]:
        reasons.extend(f"eval_artifact_gate:{reason}" for reason in gate_report["blocking_reasons"])

    return {"passed": not reasons, "reasons": reasons, "row_count": len(rows)}


def verify_committed_provenance(repo_root: Path) -> dict[str, Any]:
    """Re-authenticate the committed rows against the live canonical store.

    Distinct from ``check()``: this requires live task store access and is
    the explicit canonical-source step a coordinator or reviewer runs to
    confirm the tracked corpus's identities are genuinely accepted, rather
    than merely internally self-consistent.
    """
    rows_path = repo_root / ROWS_RELATIVE_PATH
    if not rows_path.is_file():
        return {"passed": False, "reasons": ["artifact_missing"], "row_count": 0}
    rows = [
        json.loads(line)
        for line in rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    try:
        verify_provenance(repo_root, rows)
    except AcceptedTaskEvalError as exc:
        return {"passed": False, "reasons": [str(exc)], "row_count": len(rows)}
    return {"passed": True, "reasons": [], "row_count": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="Repository root (defaults to cwd).")
    parser.add_argument(
        "--check", action="store_true",
        help="Recompute drift from the committed rows only; no task store access, no model.",
    )
    parser.add_argument(
        "--verify-provenance", action="store_true",
        help=(
            "Re-authenticate committed rows against the live canonical task "
            "store; requires store access, unlike --check."
        ),
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    if args.check:
        report = check(repo_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["passed"] else 1

    if args.verify_provenance:
        report = verify_committed_provenance(repo_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["passed"] else 1

    try:
        summary, _rows = rebuild(repo_root)
    except AcceptedTaskEvalError as exc:
        print(json.dumps({"passed": False, "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"passed": True, "record_count": summary["record_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
