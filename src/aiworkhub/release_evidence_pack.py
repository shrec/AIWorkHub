"""Build a deterministic, replayable release-evidence join.

The pack binds static release assurance, a residual-risk register, and an
adapter/route parity snapshot.  It deliberately does not approve a release,
waive a risk, or turn environment observations into runtime guarantees.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import assurance_claims


SCHEMA_ID = "aiworkhub.release_evidence_pack.v1"
MANIFEST_SCHEMA_ID = "aiworkhub.release_evidence_manifest.v1"
RISK_SCHEMA_ID = "aiworkhub.release_residual_risks.v1"
ROUTE_SCHEMA_ID = "aiworkhub.release_route_parity.v1"
DEFAULT_MANIFEST = ".aiworkhub/release-evidence.json"
MAX_FILE_BYTES = 2 * 1024 * 1024
ALLOWED_RISK_STATUSES = {
    "captured",
    "triaged",
    "accepted",
    "task_created",
    "resolved",
}
ALLOWED_SEVERITIES = {"low", "medium", "high", "critical"}
ALLOWED_ROUTE_EVIDENCE = {"external_live_observation", "verified_fixture"}
MEASUREMENT_BOUNDARY = (
    "replayable_evidence_join_not_release_approval_risk_waiver_or_runtime_guarantee"
)


class ReleaseEvidenceError(ValueError):
    """A release-evidence input is unsafe, incomplete, or contradictory."""


def _relative(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ReleaseEvidenceError(f"unsafe_relative_path:{text[:120]}")
    return path.as_posix()


def _regular(root: Path, relative: Any) -> Path:
    rel = _relative(relative)
    candidate = root / rel
    if candidate.is_symlink() or not candidate.is_file():
        raise ReleaseEvidenceError(f"required_regular_file_missing:{rel}")
    resolved = candidate.resolve()
    if root != resolved and root not in resolved.parents:
        raise ReleaseEvidenceError(f"required_file_outside_repository:{rel}")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise ReleaseEvidenceError(f"required_file_too_large:{rel}")
    return resolved


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"json_unreadable:{path.name}:{exc}") from exc
    if not isinstance(value, Mapping):
        raise ReleaseEvidenceError(f"json_object_required:{path.name}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_risks(root: Path, path: Path) -> tuple[list[dict[str, Any]], int]:
    document = _load_json(path)
    if document.get("schema_id") != RISK_SCHEMA_ID:
        raise ReleaseEvidenceError("residual_risk_schema_mismatch")
    rows = document.get("risks")
    if not isinstance(rows, list) or not rows:
        raise ReleaseEvidenceError("residual_risks_missing")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    evidence_count = 0
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ReleaseEvidenceError("residual_risk_not_object")
        risk_id = str(raw.get("id") or "").strip()
        status = str(raw.get("status") or "").strip()
        severity = str(raw.get("severity") or "").strip()
        summary = str(raw.get("summary") or "").strip()
        if not risk_id or risk_id in seen:
            raise ReleaseEvidenceError(f"residual_risk_id_invalid_or_duplicate:{risk_id}")
        if status not in ALLOWED_RISK_STATUSES:
            raise ReleaseEvidenceError(f"residual_risk_status_invalid:{risk_id}:{status}")
        if severity not in ALLOWED_SEVERITIES:
            raise ReleaseEvidenceError(
                f"residual_risk_severity_invalid:{risk_id}:{severity}"
            )
        if not summary:
            raise ReleaseEvidenceError(f"residual_risk_summary_missing:{risk_id}")
        refs = raw.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            raise ReleaseEvidenceError(f"residual_risk_evidence_missing:{risk_id}")
        normalized_refs: list[dict[str, str]] = []
        for value in refs:
            rel = _relative(value)
            evidence_path = _regular(root, rel)
            normalized_refs.append({"path": rel, "sha256": _sha256(evidence_path)})
            evidence_count += 1
        seen.add(risk_id)
        normalized.append(
            {
                "id": risk_id,
                "status": status,
                "severity": severity,
                "summary": summary,
                "evidence": sorted(normalized_refs, key=lambda item: item["path"]),
            }
        )
    return sorted(normalized, key=lambda item: item["id"]), evidence_count


def _validate_routes(path: Path) -> list[dict[str, Any]]:
    document = _load_json(path)
    if document.get("schema_id") != ROUTE_SCHEMA_ID:
        raise ReleaseEvidenceError("route_parity_schema_mismatch")
    rows = document.get("observations")
    if not isinstance(rows, list) or not rows:
        raise ReleaseEvidenceError("route_parity_observations_missing")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ReleaseEvidenceError("route_parity_observation_not_object")
        platform = str(raw.get("platform") or "").strip().lower()
        kind = str(raw.get("evidence_kind") or "").strip()
        scope = str(raw.get("claim_scope") or "").strip()
        available = raw.get("available_routes")
        total = raw.get("total_routes")
        if not platform or platform in seen:
            raise ReleaseEvidenceError(
                f"route_parity_platform_invalid_or_duplicate:{platform}"
            )
        if (
            isinstance(available, bool)
            or isinstance(total, bool)
            or not isinstance(available, int)
            or not isinstance(total, int)
            or total <= 0
            or available < 0
            or available > total
        ):
            raise ReleaseEvidenceError(f"route_parity_counts_invalid:{platform}")
        if kind not in ALLOWED_ROUTE_EVIDENCE:
            raise ReleaseEvidenceError(f"route_parity_evidence_invalid:{platform}:{kind}")
        if scope != "environment_specific_not_cross_platform_guarantee":
            raise ReleaseEvidenceError(f"route_parity_claim_scope_invalid:{platform}")
        seen.add(platform)
        normalized.append(
            {
                "platform": platform,
                "available_routes": available,
                "total_routes": total,
                "evidence_kind": kind,
                "claim_scope": scope,
            }
        )
    return sorted(normalized, key=lambda item: item["platform"])


def build(
    repo_root: Path | str,
    *,
    manifest_path: str = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    """Build and hash the complete release-evidence pack deterministically."""

    root = Path(repo_root).resolve()
    manifest_file = _regular(root, manifest_path)
    manifest = _load_json(manifest_file)
    if manifest.get("schema_id") != MANIFEST_SCHEMA_ID:
        raise ReleaseEvidenceError("release_evidence_manifest_schema_mismatch")
    if manifest.get("measurement_boundary") != MEASUREMENT_BOUNDARY:
        raise ReleaseEvidenceError("release_evidence_measurement_boundary_mismatch")

    assurance_rel = _relative(manifest.get("assurance_manifest"))
    risk_rel = _relative(manifest.get("residual_risk_register"))
    route_rel = _relative(manifest.get("route_parity_matrix"))
    assurance = assurance_claims.check(root, manifest_path=assurance_rel)
    if not assurance.get("ok"):
        errors = assurance.get("errors") or []
        raise ReleaseEvidenceError(f"release_assurance_failed:{'|'.join(map(str, errors))}")

    assurance_path = _regular(root, assurance_rel)
    risk_path = _regular(root, risk_rel)
    route_path = _regular(root, route_rel)
    risks, risk_evidence_count = _validate_risks(root, risk_path)
    routes = _validate_routes(route_path)
    payload: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "measurement_boundary": MEASUREMENT_BOUNDARY,
        "inputs": {
            "manifest": {"path": _relative(manifest_path), "sha256": _sha256(manifest_file)},
            "assurance": {"path": assurance_rel, "sha256": _sha256(assurance_path)},
            "residual_risks": {"path": risk_rel, "sha256": _sha256(risk_path)},
            "route_parity": {"path": route_rel, "sha256": _sha256(route_path)},
        },
        "assurance": assurance,
        "residual_risks": risks,
        "route_parity": routes,
        "stats": {
            "residual_risks": len(risks),
            "risk_evidence_refs": risk_evidence_count,
            "route_observations": len(routes),
        },
    }
    return {**payload, "bundle_sha256": _canonical_sha256(payload)}


def check(
    repo_root: Path | str,
    *,
    manifest_path: str = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    """Return a structured fail-closed result suitable for CI."""

    try:
        pack = build(repo_root, manifest_path=manifest_path)
    except (OSError, ReleaseEvidenceError) as exc:
        return {
            "schema_id": SCHEMA_ID,
            "ok": False,
            "blocking": True,
            "errors": [str(exc)],
            "measurement_boundary": MEASUREMENT_BOUNDARY,
        }
    return {"ok": True, "blocking": False, "errors": [], **pack}


__all__ = [
    "DEFAULT_MANIFEST",
    "MEASUREMENT_BOUNDARY",
    "ReleaseEvidenceError",
    "SCHEMA_ID",
    "build",
    "check",
]
