"""Registry-driven truth checks for committed evaluation artifacts.

Evaluation summaries are derived evidence, not authority.  This module binds a
summary to its row population and rejects the two most dangerous false-green
shapes: a PASS over zero eligible rows and a scalar/count that no longer agrees
with the retained rows.  It is intentionally deterministic and read-only.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


SCHEMA_ID = "aiworkhub.eval_artifact_gate.v1"
REGISTRY_RELATIVE_PATH = Path(".aiworkhub/eval-artifacts.json")
MAX_REGISTRY_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_ARTIFACTS = 128
MAX_ROWS = 100_000


class EvalArtifactError(ValueError):
    """A registry or registered artifact is malformed or unsafe to read."""


def _relative_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise EvalArtifactError("artifact_path_invalid")
    return pure.as_posix()


def _regular_file(root: Path, relative: str, *, max_bytes: int) -> Path:
    raw = root / relative
    resolved = raw.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise EvalArtifactError("artifact_path_escape")
    try:
        info = raw.lstat()
    except OSError as exc:
        raise EvalArtifactError(f"artifact_unavailable:{relative}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvalArtifactError(f"artifact_not_regular:{relative}")
    if info.st_size > max_bytes:
        raise EvalArtifactError(f"artifact_too_large:{relative}")
    return raw


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvalArtifactError(f"artifact_json_invalid:{path.name}") from exc


def _read_rows(path: Path, row_format: str) -> list[dict[str, Any]]:
    rows: list[Any]
    if row_format == "json":
        document = _read_json(path)
        rows = document if isinstance(document, list) else []
        if not isinstance(document, list):
            raise EvalArtifactError(f"artifact_rows_not_array:{path.name}")
    elif row_format == "jsonl":
        rows = []
        try:
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                if len(rows) >= MAX_ROWS:
                    raise EvalArtifactError(f"artifact_rows_overflow:{path.name}")
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise EvalArtifactError(
                        f"artifact_row_invalid:{path.name}:{line_no}"
                    ) from exc
        except (OSError, UnicodeError) as exc:
            raise EvalArtifactError(f"artifact_rows_unreadable:{path.name}") from exc
    else:
        raise EvalArtifactError(f"artifact_row_format_invalid:{row_format}")
    if len(rows) > MAX_ROWS or any(not isinstance(row, Mapping) for row in rows):
        raise EvalArtifactError(f"artifact_rows_invalid:{path.name}")
    return [dict(row) for row in rows]


def _path_value(document: Mapping[str, Any], dotted: str) -> Any:
    value: Any = document
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise EvalArtifactError(f"summary_field_missing:{dotted}")
        value = value[part]
    return value


def _eligible(row: Mapping[str, Any], required_fields: tuple[str, ...]) -> bool:
    return all(row.get(field) not in (None, "") for field in required_fields)


def _aggregate(rows: list[dict[str, Any]], spec: Mapping[str, Any]) -> Any:
    operation = str(spec.get("operation") or "").strip()
    row_field = str(spec.get("row_field") or "").strip()
    if operation == "count":
        return len(rows)
    if not row_field:
        raise EvalArtifactError("aggregate_row_field_missing")
    values = [row.get(row_field) for row in rows]
    if operation == "sum":
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise EvalArtifactError(f"aggregate_non_numeric:{row_field}")
        return sum(values)
    if operation == "all":
        expected = spec.get("equals", True)
        return bool(rows) and all(value == expected for value in values)
    if operation == "any":
        expected = spec.get("equals", True)
        return any(value == expected for value in values)
    raise EvalArtifactError(f"aggregate_operation_invalid:{operation}")


def load_registry(repo_root: Path | str) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    path = root / REGISTRY_RELATIVE_PATH
    if not path.exists():
        return {"schema_id": SCHEMA_ID, "artifacts": []}
    document = _read_json(_regular_file(root, REGISTRY_RELATIVE_PATH.as_posix(), max_bytes=MAX_REGISTRY_BYTES))
    if not isinstance(document, dict) or not isinstance(document.get("artifacts"), list):
        raise EvalArtifactError("registry_shape_invalid")
    artifacts = document["artifacts"]
    if len(artifacts) > MAX_ARTIFACTS or any(not isinstance(row, Mapping) for row in artifacts):
        raise EvalArtifactError("registry_artifacts_invalid")
    return {**document, "schema_id": SCHEMA_ID}


def evaluate(repo_root: Path | str, *, changed_paths: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Recompute registered summary claims from their retained row evidence."""

    root = Path(repo_root).resolve()
    changed = {str(path).replace("\\", "/") for path in (changed_paths or ())}
    try:
        registry = load_registry(root)
    except EvalArtifactError as exc:
        return {
            "schema_id": SCHEMA_ID,
            "status": "failed",
            "passed": False,
            "configured": True,
            "blocking_reasons": [str(exc)],
            "artifacts": [],
        }
    entries = list(registry.get("artifacts") or [])
    results: list[dict[str, Any]] = []
    blockers: list[str] = []
    for index, raw_entry in enumerate(entries):
        entry = dict(raw_entry)
        artifact_id = str(entry.get("id") or f"artifact-{index}").strip()[:160]
        reasons: list[str] = []
        try:
            summary_rel = _relative_path(entry.get("summary_path"))
            rows_rel = _relative_path(entry.get("rows_path"))
            if changed and summary_rel not in changed and rows_rel not in changed:
                results.append({
                    "id": artifact_id,
                    "status": "not_applicable",
                    "summary_path": summary_rel,
                    "rows_path": rows_rel,
                    "blocking_reasons": [],
                })
                continue
            summary_path = _regular_file(root, summary_rel, max_bytes=MAX_ARTIFACT_BYTES)
            rows_path = _regular_file(root, rows_rel, max_bytes=MAX_ARTIFACT_BYTES)
            summary = _read_json(summary_path)
            if not isinstance(summary, dict):
                raise EvalArtifactError(f"summary_not_object:{summary_rel}")
            rows = _read_rows(rows_path, str(entry.get("row_format") or "jsonl"))
            required_fields = tuple(str(field) for field in (entry.get("required_row_fields") or ()))
            eligible_rows = [row for row in rows if _eligible(row, required_fields)]
            minimum_rows = int(entry.get("minimum_rows") or 1)
            verdict_field = str(entry.get("verdict_field") or "verdict")
            pass_values = {str(value).upper() for value in (entry.get("pass_values") or ["PASS", "PASSED", "VERIFIED"])}
            declared_verdict = str(summary.get(verdict_field) or "").upper()
            if len(eligible_rows) < minimum_rows:
                reasons.append(f"insufficient_rows:{len(eligible_rows)}<{minimum_rows}")
                if declared_verdict in pass_values:
                    reasons.append("pass_without_eligible_rows")
            for field in entry.get("count_fields") or []:
                dotted = str(field)
                if _path_value(summary, dotted) != len(eligible_rows):
                    reasons.append(f"count_mismatch:{dotted}")
            for claim in entry.get("aggregates") or []:
                if not isinstance(claim, Mapping):
                    raise EvalArtifactError("aggregate_claim_invalid")
                dotted = str(claim.get("summary_field") or "")
                expected = _aggregate(eligible_rows, claim)
                if _path_value(summary, dotted) != expected:
                    reasons.append(f"aggregate_mismatch:{dotted}")
            assertion_tests = [str(value) for value in (entry.get("assertion_tests") or [])]
            for assertion in assertion_tests:
                if "::" not in assertion:
                    reasons.append(f"assertion_identity_invalid:{assertion}")
                    continue
                relative, test_name = assertion.split("::", 1)
                test_path = _regular_file(root, _relative_path(relative), max_bytes=MAX_ARTIFACT_BYTES)
                text = test_path.read_text(encoding="utf-8")
                if f"def {test_name}(" not in text:
                    reasons.append(f"assertion_missing:{assertion}")
            digest = hashlib.sha256(
                (summary_path.read_bytes() + b"\0" + rows_path.read_bytes())
            ).hexdigest()
            status = "passed" if not reasons else ("inconclusive" if len(eligible_rows) < minimum_rows else "failed")
            results.append({
                "id": artifact_id,
                "status": status,
                "summary_path": summary_rel,
                "rows_path": rows_rel,
                "row_count": len(rows),
                "eligible_row_count": len(eligible_rows),
                "minimum_rows": minimum_rows,
                "declared_verdict": declared_verdict,
                "evidence_sha256": digest,
                "blocking_reasons": reasons,
            })
            blockers.extend(f"{artifact_id}:{reason}" for reason in reasons)
        except (EvalArtifactError, OSError, UnicodeError, TypeError, ValueError) as exc:
            reason = str(exc)[:300]
            blockers.append(f"{artifact_id}:{reason}")
            results.append({"id": artifact_id, "status": "failed", "blocking_reasons": [reason]})
    return {
        "schema_id": SCHEMA_ID,
        "status": "passed" if not blockers else "failed",
        "passed": not blockers,
        "configured": bool(entries),
        "artifact_count": len(entries),
        "evaluated_count": sum(row.get("status") != "not_applicable" for row in results),
        "blocking_reasons": blockers[:256],
        "artifacts": results,
    }


__all__ = ["EvalArtifactError", "REGISTRY_RELATIVE_PATH", "SCHEMA_ID", "evaluate", "load_registry"]
