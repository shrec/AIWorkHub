"""Deterministic release-assurance manifest validation.

The manifest joins public claims and critical product surfaces to pinned
repository evidence and named regression tests.  It does not execute models,
infer quality, or convert structural measurements into token claims.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


SCHEMA_ID = "aiworkhub.release_assurance.v1"
DEFAULT_MANIFEST = ".aiworkhub/release-assurance.json"
MAX_FILE_BYTES = 2 * 1024 * 1024
ALLOWED_VERDICTS = {
    "verified_structural",
    "historical_not_claim_eligible",
    "verified_contract",
}


class AssuranceManifestError(ValueError):
    """The assurance manifest or one of its pinned inputs is invalid."""


def _relative(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise AssuranceManifestError(f"unsafe_relative_path:{text[:120]}")
    return path.as_posix()


def _regular(root: Path, relative: Any) -> Path:
    rel = _relative(relative)
    candidate = root / rel
    if candidate.is_symlink() or not candidate.is_file():
        raise AssuranceManifestError(f"required_regular_file_missing:{rel}")
    resolved = candidate.resolve()
    if root != resolved and root not in resolved.parents:
        raise AssuranceManifestError(f"required_file_outside_repository:{rel}")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise AssuranceManifestError(f"required_file_too_large:{rel}")
    return resolved


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssuranceManifestError(f"json_unreadable:{path.name}:{exc}") from exc
    if not isinstance(value, Mapping):
        raise AssuranceManifestError(f"json_object_required:{path.name}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _python_functions(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise AssuranceManifestError(f"python_surface_unreadable:{path.name}:{exc}") from exc
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _selector_exists(root: Path, selector: Any) -> bool:
    text = str(selector or "")
    if "::" not in text:
        return False
    relative, function_name = text.split("::", 1)
    if not function_name or "::" in function_name:
        return False
    return function_name in _python_functions(_regular(root, relative))


def check(
    repo_root: Path | str,
    *,
    manifest_path: str = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    """Validate the complete static release-assurance join, fail closed."""

    root = Path(repo_root).resolve()
    errors: list[str] = []
    stats = {
        "claims": 0,
        "pinned_evidence": 0,
        "public_surfaces": 0,
        "required_tools": 0,
        "test_selectors": 0,
        "retrieval_cases": 0,
        "quality_checks": 0,
    }
    try:
        manifest = _load_json(_regular(root, manifest_path))
    except AssuranceManifestError as exc:
        return {
            "schema_id": SCHEMA_ID,
            "ok": False,
            "blocking": True,
            "errors": [str(exc)],
            "stats": stats,
        }
    if manifest.get("schema_id") != SCHEMA_ID:
        errors.append("manifest_schema_mismatch")

    claims = manifest.get("claims")
    if not isinstance(claims, list) or not claims:
        errors.append("claims_missing")
        claims = []
    claim_ids: set[str] = set()
    for raw_claim in claims:
        if not isinstance(raw_claim, Mapping):
            errors.append("claim_not_object")
            continue
        claim_id = str(raw_claim.get("id") or "").strip()
        if not claim_id or claim_id in claim_ids:
            errors.append(f"claim_id_invalid_or_duplicate:{claim_id}")
            continue
        claim_ids.add(claim_id)
        stats["claims"] += 1
        if raw_claim.get("verdict") not in ALLOWED_VERDICTS:
            errors.append(f"claim_verdict_invalid:{claim_id}")
        evidence = raw_claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"claim_evidence_missing:{claim_id}")
            evidence = []
        for item in evidence:
            if not isinstance(item, Mapping):
                errors.append(f"claim_evidence_invalid:{claim_id}")
                continue
            try:
                evidence_path = _regular(root, item.get("path"))
            except AssuranceManifestError as exc:
                errors.append(f"claim_evidence_invalid:{claim_id}:{exc}")
                continue
            expected = str(item.get("sha256") or "")
            if len(expected) != 64 or _sha256(evidence_path) != expected:
                errors.append(f"claim_evidence_hash_mismatch:{claim_id}:{item.get('path')}")
            stats["pinned_evidence"] += 1
        surfaces = raw_claim.get("surfaces") or []
        if not isinstance(surfaces, list):
            errors.append(f"claim_surfaces_invalid:{claim_id}")
            surfaces = []
        for item in surfaces:
            if not isinstance(item, Mapping):
                errors.append(f"claim_surface_invalid:{claim_id}")
                continue
            try:
                surface = _regular(root, item.get("path"))
                content = surface.read_text(encoding="utf-8").casefold()
            except (AssuranceManifestError, OSError, UnicodeError) as exc:
                errors.append(f"claim_surface_unreadable:{claim_id}:{exc}")
                continue
            for token in item.get("required_tokens") or []:
                if str(token).casefold() not in content:
                    errors.append(
                        f"claim_surface_token_missing:{claim_id}:{item.get('path')}:{token}"
                    )
            stats["public_surfaces"] += 1

    try:
        server_functions = _python_functions(_regular(root, "src/aiworkhub/server.py"))
    except AssuranceManifestError as exc:
        errors.append(str(exc))
        server_functions = set()
    for tool_name in manifest.get("required_tools") or []:
        if str(tool_name) not in server_functions:
            errors.append(f"required_tool_missing:{tool_name}")
        stats["required_tools"] += 1

    selector_groups = (
        "policy_projection_tests",
        "source_graph_contract_tests",
        "negative_fixture_tests",
        "quality_adapter_tests",
    )
    for group in selector_groups:
        selectors = manifest.get(group)
        if not isinstance(selectors, list) or not selectors:
            errors.append(f"test_group_missing:{group}")
            continue
        for selector in selectors:
            try:
                exists = _selector_exists(root, selector)
            except AssuranceManifestError as exc:
                errors.append(f"test_selector_invalid:{selector}:{exc}")
                exists = False
            if not exists:
                errors.append(f"test_selector_missing:{selector}")
            stats["test_selectors"] += 1

    try:
        retrieval = _load_json(
            _regular(root, manifest.get("source_graph_retrieval_registry"))
        )
        cases = retrieval.get("cases")
        if not isinstance(cases, list) or not cases:
            errors.append("source_graph_retrieval_cases_missing")
            cases = []
        minimums = retrieval.get("minimums")
        required_retrieval_metrics = {"recall_at_k", "mrr", "success_at_k"}
        if not isinstance(minimums, Mapping) or set(minimums) != required_retrieval_metrics:
            errors.append("source_graph_retrieval_minimums_invalid")
        else:
            for metric, raw_value in minimums.items():
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    errors.append(f"source_graph_retrieval_minimum_invalid:{metric}")
                    continue
                if not 0.0 <= value <= 1.0:
                    errors.append(f"source_graph_retrieval_minimum_invalid:{metric}")
        seen_cases: set[str] = set()
        for case in cases:
            if not isinstance(case, Mapping):
                errors.append("source_graph_retrieval_case_invalid")
                continue
            case_id = str(case.get("id") or "")
            expected_paths = case.get("expected_paths")
            if not case_id or case_id in seen_cases or not isinstance(expected_paths, list) or not expected_paths:
                errors.append(f"source_graph_retrieval_case_invalid:{case_id}")
                continue
            seen_cases.add(case_id)
            for expected_path in expected_paths:
                try:
                    _regular(root, expected_path)
                except AssuranceManifestError as exc:
                    errors.append(f"source_graph_expected_path_invalid:{case_id}:{exc}")
            stats["retrieval_cases"] += 1
    except AssuranceManifestError as exc:
        errors.append(str(exc))

    try:
        quality = _load_json(_regular(root, manifest.get("quality_policy")))
        configured_ids = {
            str(item.get("id") or "")
            for item in quality.get("checks") or []
            if isinstance(item, Mapping)
        }
        for check_id in manifest.get("required_quality_check_ids") or []:
            if str(check_id) not in configured_ids:
                errors.append(f"required_quality_check_missing:{check_id}")
            stats["quality_checks"] += 1
    except AssuranceManifestError as exc:
        errors.append(str(exc))

    return {
        "schema_id": SCHEMA_ID,
        "ok": not errors,
        "blocking": bool(errors),
        "errors": errors,
        "stats": stats,
        "claim_boundary": "static_release_assurance_not_runtime_or_causal_proof",
    }


__all__ = ["AssuranceManifestError", "DEFAULT_MANIFEST", "SCHEMA_ID", "check"]
