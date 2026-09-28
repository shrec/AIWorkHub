"""Worker-output semantic-edit evidence and provider tool-denial parsing.

Moved unchanged out of ``process_launcher``; that module re-imports every
name here so ``process_launcher.<name>`` callers and readers keep resolving.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from . import vscode_lm_bridge


def _provider_tool_denials_from_output(path: Path) -> dict[str, Any]:
    """Extract only bounded denial counts from provider JSON/JSONL output.

    Providers may expose ``permission_denials`` (or the camelCase/tool-denial
    equivalents) in their terminal result.  The raw payload can contain
    commands, paths or prompt fragments, so it is never persisted.  This
    parser returns only counts and a fixed allowlist of raw-discovery labels.
    Absence of a denial field means *not observed*, never proof of zero
    attempts.
    """

    result: dict[str, Any] = {
        "schema_id": "aiworkhub.provider_tool_denials.v1",
        "evidence_observed": False,
        "permission_denials_total": 0,
        "raw_discovery_denials": 0,
        "raw_discovery_labels": [],
    }
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            return result
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    candidates: list[Any] = []
    try:
        candidates.append(json.loads(raw))
    except json.JSONDecodeError:
        for line in raw.splitlines():
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    denial_keys = {
        "permission_denials", "permissionDenials", "tool_denials", "toolDenials",
    }
    patterns = {
        "grep": re.compile(r"(?<![a-z0-9_])grep(?![a-z0-9_])", re.IGNORECASE),
        "glob": re.compile(r"(?<![a-z0-9_])glob(?![a-z0-9_])", re.IGNORECASE),
        "rg": re.compile(r"(?<![a-z0-9_])rg(?![a-z0-9_])", re.IGNORECASE),
        "find": re.compile(r"(?<![a-z0-9_])find(?![a-z0-9_])", re.IGNORECASE),
        "tree": re.compile(r"(?<![a-z0-9_])tree(?![a-z0-9_])", re.IGNORECASE),
    }
    labels: set[str] = set()

    def record(value: Any) -> None:
        rows = value if isinstance(value, list) else [value]
        for row in rows[:256]:
            result["permission_denials_total"] += 1
            try:
                bounded = json.dumps(row, ensure_ascii=False, sort_keys=True)[:4096]
            except (TypeError, ValueError):
                bounded = str(row)[:4096]
            matched = {name for name, pattern in patterns.items() if pattern.search(bounded)}
            if matched:
                result["raw_discovery_denials"] += 1
                labels.update(matched)

    def walk(value: Any, *, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, nested in list(value.items())[:256]:
                if key in denial_keys:
                    result["evidence_observed"] = True
                    record(nested)
                else:
                    walk(nested, depth=depth + 1)
        elif isinstance(value, list):
            for nested in value[:256]:
                walk(nested, depth=depth + 1)

    for candidate in candidates[:2048]:
        walk(candidate)
    result["raw_discovery_labels"] = sorted(labels)
    return result


def _semantic_edit_evidence_from_output(
    path: Path,
    *,
    worker_mcp_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract bounded runtime-authored semantic-edit byte accounting.

    Only the local ``vscode_lm_worker`` result schema is accepted. Provider
    prose cannot create these fields, and byte labels are kept explicitly
    separate from token/cost claims.
    """

    empty = {
        "schema_id": "aiworkhub.semantic_edit_runtime_evidence.v1",
        "observed": False,
        "file_count": 0,
        "range_count": 0,
        "file_bytes": 0,
        "old_region_bytes": 0,
        "replacement_bytes": 0,
        "model_reemitted_old_bytes": 0,
        "token_savings_claimed": False,
    }

    def from_authenticated_ledger() -> dict[str, Any]:
        verification = (
            worker_mcp_gate.get("verification")
            if isinstance(worker_mcp_gate, dict) else None
        )
        rows = (
            verification.get("semantic_edit_apply_receipts")
            if isinstance(verification, dict) else None
        )
        if not isinstance(rows, list):
            return empty
        bounded = [row for row in rows[:128] if isinstance(row, dict)]
        if not bounded:
            return empty
        return {
            **empty,
            "observed": True,
            "file_count": len(bounded),
            "range_count": sum(int(row.get("range_count") or 0) for row in bounded),
            "file_bytes": sum(int(row.get("file_bytes") or 0) for row in bounded),
            "old_region_bytes": sum(
                int(row.get("old_region_bytes") or 0) for row in bounded
            ),
            "replacement_bytes": sum(
                int(row.get("replacement_bytes") or 0) for row in bounded
            ),
            "model_reemitted_old_bytes": sum(
                int(row.get("model_reemitted_old_bytes") or 0) for row in bounded
            ),
        }
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            return from_authenticated_ledger()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return from_authenticated_ledger()
    for raw_line in reversed(lines[-20000:]):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(event, dict)
            or event.get("type") != "result"
            or event.get("is_error") is not False
            or event.get("edit_protocol")
            != vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID
        ):
            continue
        rows = event.get("semantic_edit_metrics")
        if not isinstance(rows, list):
            return empty
        bounded = [row for row in rows[:128] if isinstance(row, dict)]
        return {
            **empty,
            "observed": True,
            "file_count": len(bounded),
            "range_count": sum(int(row.get("range_count") or 0) for row in bounded),
            "file_bytes": sum(int(row.get("file_bytes") or 0) for row in bounded),
            "old_region_bytes": sum(
                int(row.get("old_region_bytes") or 0) for row in bounded
            ),
            "replacement_bytes": sum(
                int(row.get("replacement_bytes") or 0) for row in bounded
            ),
            "model_reemitted_old_bytes": sum(
                int(row.get("model_reemitted_old_bytes") or 0) for row in bounded
            ),
        }
    return from_authenticated_ledger()


SEMANTIC_EDIT_COVERAGE_SCHEMA_ID = "aiworkhub.semantic_edit_coverage.v1"

# The three exceptions the repository's semantic-edit rule actually allows.
# Kept as data so a declaration carrying anything else is reported as an
# unknown code rather than silently accepted.
SEMANTIC_EDIT_POLICY_EXCEPTIONS = (
    "new_file",
    "spans_most_of_file",
    "adapter_without_tools",
)
_COVERAGE_LIST_CAP = 200


def semantic_edit_path_identifier(relative: str) -> str:
    """The join key shared with the authenticated worker ledger.

    ``sha256`` of the repo-relative POSIX path text -- the identical rule
    ``worker_ai_tools_mcp.verify_audit_ledger`` applies to an apply receipt's
    private ``path``.  Both sides hash the same normalized string, so the
    finalizer can join an apply to a changed path without the ledger ever
    carrying path text.
    """
    text = str(relative or "").strip().replace("\\", "/")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _semantic_edit_coverage(
    changed_paths: Iterable[str],
    *,
    workspace: Any = None,
    worker_mcp_gate: dict[str, Any] | None = None,
    granted_tool_names: Iterable[str] = (),
    runtime_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-attempt semantic-edit coverage over the candidate's changed paths.

    THIS IS A MEASUREMENT, NOT A GATE.  Nothing in acceptance, promotion, the
    quality ratchet or the review decision reads this record; it exists so the
    question "is semantic edit actually mandatory, and does everyone use it?"
    has a durable numeric answer instead of an assertion.  This repository's
    own rule is never to bound what it cannot yet measure, so the measurement
    comes first and any threshold is a separate, later decision made against
    the distribution this record produces.

    Byte labels stay byte labels.  ``coverage_ratio`` is a ratio of BYTES of
    changed files, never of provider tokens and never of money;
    ``token_savings_claimed`` stays False for the same reason it is False on
    the apply receipt itself.

    Absence of evidence is never reported as zero coverage.  An unverifiable
    ledger, an attempt that changed nothing, or receipts written before the
    path identifier existed all resolve to ``measured: False`` with a named
    ``unmeasured_reason`` -- never to ``coverage_ratio: 0.0``.
    """

    record: dict[str, Any] = {
        "schema_id": SEMANTIC_EDIT_COVERAGE_SCHEMA_ID,
        "measured": False,
        "unmeasured_reason": "",
        "changed_paths_count": 0,
        "eligible_paths_count": 0,
        "paths_with_apply": 0,
        "paths_raw_only": [],
        "paths_raw_only_count": 0,
        "paths_new_file": 0,
        "paths_deleted": 0,
        "paths_baseline_unknown": 0,
        "bytes_basis": "changed_file_size_at_finalization",
        "bytes_changed": 0,
        "bytes_via_apply": 0,
        "coverage_ratio": None,
        "declared_exceptions": [],
        "declared_exception_count": 0,
        "derived_exceptions": [],
        "derived_exception_count": 0,
        "undeclared_raw_only": [],
        "undeclared_raw_only_count": 0,
        "apply_receipts_total": 0,
        "apply_receipts_joined": 0,
        "apply_receipts_unjoinable": 0,
        "adapter_semantic_edit_granted": None,
        "token_savings_claimed": False,
        "measurement_only": True,
    }

    changed = sorted({
        str(item).strip().replace("\\", "/")
        for item in (changed_paths or [])
        if str(item).strip()
    })
    record["changed_paths_count"] = len(changed)

    verification = (
        worker_mcp_gate.get("verification")
        if isinstance(worker_mcp_gate, dict) else None
    )
    if not isinstance(verification, dict) or verification.get("ok") is not True:
        # The ledger is the only authenticated statement about tool use.  An
        # adapter that reported nothing, or a ledger that could not be
        # verified, is UNMEASURED -- it is not a worker that used nothing.
        record["unmeasured_reason"] = "ledger_unverified"
        return record
    if not changed:
        record["unmeasured_reason"] = "no_changed_paths"
        return record

    receipts = verification.get("semantic_edit_apply_receipts")
    receipts = [row for row in receipts[:128] if isinstance(row, dict)] if isinstance(
        receipts, list
    ) else []
    record["apply_receipts_total"] = len(receipts)
    applied_digests = {
        str(row.get("path_sha256") or "") for row in receipts
    } - {""}
    if receipts and not applied_digests:
        # Every retained receipt predates the path identifier: the applies are
        # real but cannot be joined to a path, so coverage is unknown rather
        # than zero.
        record["unmeasured_reason"] = "receipts_without_path_identifier"
        return record
    if not receipts and isinstance(runtime_evidence, dict) and runtime_evidence.get(
        "observed"
    ) is True:
        # The authenticated ledger holds no apply, yet the local runtime bridge
        # observed semantic edits for this attempt -- the vscode_lm route
        # reports through ``semantic_edit_metrics`` on its own result stream,
        # not through the worker MCP ledger.  Counting that as 0% would be
        # exactly the "absence of evidence read as zero" defect this record
        # exists to avoid, and folding an unauthenticated stream into the
        # numerator would weaken the ledger.  So: unmeasured, and named.
        record["unmeasured_reason"] = "applies_observed_outside_the_authenticated_ledger"
        return record

    granted = [str(name) for name in (granted_tool_names or [])]
    semantic_edit_granted: bool | None = None
    if granted:
        semantic_edit_granted = any(
            name.endswith("semantic_edit_apply") for name in granted
        )
    record["adapter_semantic_edit_granted"] = semantic_edit_granted

    baselines: dict[str, str | None] = {}
    tree_baseline: dict[str, str | None] | None = None
    workspace_root: Path | None = None
    if workspace is not None:
        baselines = dict(getattr(workspace, "workspace_baseline", None) or {})
        raw_tree = getattr(workspace, "tree_baseline", None)
        tree_baseline = dict(raw_tree) if isinstance(raw_tree, dict) else None
        root = getattr(workspace, "path", None)
        workspace_root = Path(str(root)) if root else None

    def baseline_state(relative: str) -> str:
        """``present`` / ``absent`` / ``unknown`` at workspace creation."""
        if relative in baselines:
            return "present" if baselines[relative] is not None else "absent"
        if tree_baseline is not None:
            # The tree manifest covers EVERY file in the worktree, so absence
            # from it is decisive: the path did not exist before this attempt.
            return "present" if tree_baseline.get(relative) is not None else "absent"
        return "unknown"

    def path_bytes(relative: str) -> tuple[int, bool]:
        if workspace_root is None:
            return 0, False
        candidate = workspace_root / relative
        try:
            if candidate.is_symlink() or not candidate.is_file():
                return 0, False
            return max(0, candidate.stat().st_size), True
        except OSError:
            return 0, False

    declarations_by_digest: dict[str, dict[str, Any]] = {}
    raw_declarations = verification.get("semantic_edit_exception_declarations")
    if isinstance(raw_declarations, list):
        for row in raw_declarations[:128]:
            if not isinstance(row, dict):
                continue
            digest = str(row.get("path_sha256") or "")
            if digest:
                declarations_by_digest[digest] = row

    raw_only: list[str] = []
    new_files: list[str] = []
    derived: list[dict[str, Any]] = []
    declared: list[dict[str, Any]] = []
    joined_digests: set[str] = set()
    bytes_changed = 0
    bytes_via_apply = 0
    eligible = 0

    for relative in changed:
        digest = semantic_edit_path_identifier(relative)
        has_apply = digest in applied_digests
        if has_apply:
            joined_digests.add(digest)
        size, exists = path_bytes(relative)
        state = baseline_state(relative)
        if state == "unknown":
            record["paths_baseline_unknown"] += 1
        if not exists:
            # A removed path was never "written raw": there is no replacement
            # text to have gone through the editor, so it is outside the
            # denominator rather than an uncovered edit.
            record["paths_deleted"] += 1
            continue
        if state == "absent":
            new_files.append(relative)
            derived.append({
                "path": relative,
                "exception": "new_file",
                "basis": "no_baseline_hash_at_workspace_creation",
                "source": "runtime_derivation",
            })
            continue
        eligible += 1
        bytes_changed += size
        if has_apply:
            record["paths_with_apply"] += 1
            bytes_via_apply += size
            continue
        raw_only.append(relative)
        declaration = declarations_by_digest.get(digest)
        covered_by_derivation = False
        if semantic_edit_granted is False:
            derived.append({
                "path": relative,
                "exception": "adapter_without_tools",
                "basis": "semantic_edit_apply_absent_from_granted_tool_names",
                "source": "runtime_derivation",
            })
            covered_by_derivation = True
        if declaration is not None:
            code = str(declaration.get("exception") or "")
            if code not in SEMANTIC_EDIT_POLICY_EXCEPTIONS:
                corroboration = "unknown_exception_code"
                corroborated = False
            elif code == "new_file":
                corroborated = False
                corroboration = "baseline_hash_present_for_path"
            elif code == "adapter_without_tools":
                corroborated = semantic_edit_granted is False
                corroboration = (
                    "granted_tool_names"
                    if semantic_edit_granted is not None
                    else "granted_tool_names_unknown"
                )
            else:
                # "spans most of a file" is the genuinely judgemental case: the
                # runtime never saw the raw write, so it holds no preimage to
                # measure the span against.  The worker's word is recorded and
                # explicitly marked uncorroborated rather than dressed up as a
                # derivation.
                corroborated = False
                corroboration = "no_byte_evidence_for_a_raw_write"
            declared.append({
                "path": relative,
                "exception": code,
                "reason": str(declaration.get("reason") or "")[:200],
                "source": "worker_declaration",
                "corroborated": bool(corroborated),
                "corroboration": corroboration,
            })
        elif not covered_by_derivation:
            record["undeclared_raw_only"].append(relative)

    record["eligible_paths_count"] = eligible
    record["paths_raw_only"] = raw_only[:_COVERAGE_LIST_CAP]
    record["paths_raw_only_count"] = len(raw_only)
    record["paths_new_file"] = len(new_files)
    record["bytes_changed"] = bytes_changed
    record["bytes_via_apply"] = bytes_via_apply
    record["coverage_ratio"] = (
        round(bytes_via_apply / bytes_changed, 4) if bytes_changed > 0 else None
    )
    record["declared_exception_count"] = len(declared)
    record["declared_exceptions"] = declared[:_COVERAGE_LIST_CAP]
    record["derived_exception_count"] = len(derived)
    record["derived_exceptions"] = derived[:_COVERAGE_LIST_CAP]
    record["undeclared_raw_only_count"] = len(record["undeclared_raw_only"])
    record["undeclared_raw_only"] = record["undeclared_raw_only"][:_COVERAGE_LIST_CAP]
    record["apply_receipts_joined"] = len(joined_digests)
    record["apply_receipts_unjoinable"] = max(
        0, len(applied_digests) - len(joined_digests)
    )
    record["measured"] = True
    return record
