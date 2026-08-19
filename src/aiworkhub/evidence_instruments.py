"""Truth-preserving evidence instruments for AIWorkHub engineering loops.

Every report in this module distinguishes missing evidence from a zero value.
No byte count is promoted to a provider-token claim and no association is
reported as a causal saving.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import agent_tool_instructions, cost_ledger


MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000


class EvidenceInstrumentError(ValueError):
    pass


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FILE_RECEIPT_SHA256_RE = re.compile(r"file:[0-7]{3,4}:([0-9a-f]{64})")


def _relative(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise EvidenceInstrumentError("relative_path_invalid")
    return path.as_posix()


def _regular(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    path = root / _relative(relative)
    resolved = path.resolve(strict=False)
    if root != resolved and root not in resolved.parents:
        raise EvidenceInstrumentError("path_escape")
    if must_exist:
        try:
            # Absent is not invalid. A file that merely does not exist gets a
            # distinct ``artifact_missing`` code -- the same ``*_missing``
            # vocabulary ``source_graph_retrieval_eval`` already uses for its
            # absent registry -- so a reader can tell a gone artifact from a
            # corrupt one. A symlink, non-regular, or oversized file that DOES
            # exist is still ``artifact_invalid``.
            if not path.exists() and not path.is_symlink():
                raise EvidenceInstrumentError(f"artifact_missing:{relative}")
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise EvidenceInstrumentError(f"artifact_invalid:{relative}")
        except OSError as exc:
            raise EvidenceInstrumentError(f"artifact_unavailable:{relative}") from exc
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _receipt_sha256(value: Any) -> str:
    """Return one exact legacy/raw content digest, or an empty string.

    Changed-path receipts use this raw form. Required-output manifests have a
    richer token and are checked by ``_required_output_hash_matches`` so the
    file mode is not discarded.
    """

    text = str(value or "")
    if _SHA256_RE.fullmatch(text):
        return text
    return ""


def _required_output_hash_matches(path: Path, value: Any) -> bool:
    """Compare raw legacy hashes or exact ``file:<mode>:<sha>`` tokens."""

    text = str(value or "")
    raw = _receipt_sha256(text)
    if raw:
        return _sha256(path) == raw
    match = _FILE_RECEIPT_SHA256_RE.fullmatch(text)
    if not match:
        return False
    observed = (
        f"file:{stat.S_IMODE(path.stat().st_mode):o}:{_sha256(path)}"
    )
    return observed == text


def _json_rows(root: Path, relative: str) -> list[dict[str, Any]]:
    path = _regular(root, relative)
    try:
        if path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            document = json.loads(path.read_text(encoding="utf-8"))
            rows = document if isinstance(document, list) else document.get("rows", [])
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        raise EvidenceInstrumentError(f"artifact_parse_failed:{relative}") from exc
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, Mapping) for row in rows):
        raise EvidenceInstrumentError(f"artifact_rows_invalid:{relative}")
    return [dict(row) for row in rows]


def review_evidence_audit(
    repo_root: Path | str,
    candidate_root: Path | str,
    *,
    changed_paths: Sequence[str],
    stored_hashes: Mapping[str, Any],
    required_outputs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Recompute structured review references before promotion."""

    repo = Path(repo_root).resolve()
    candidate = Path(candidate_root).resolve()
    blockers: list[str] = []
    verified: list[dict[str, Any]] = []
    normalized: list[str] = []
    # Receipts created before ``changed_paths`` became explicit carried the
    # same referential set solely in ``changed_path_hashes``.  Recompute those
    # paths too instead of rejecting otherwise valid retained reviews.  When
    # the explicit list exists it remains authoritative and surplus hashes are
    # still rejected below.
    effective_paths: Sequence[str] = (
        changed_paths if changed_paths else tuple(str(key) for key in stored_hashes)
    )
    for raw in effective_paths:
        try:
            relative = _relative(raw)
            normalized.append(relative)
            path = _regular(candidate, relative)
            expected = _receipt_sha256(stored_hashes.get(relative))
            observed = _sha256(path)
            if len(expected) != 64 or observed != expected:
                blockers.append(f"changed_path_hash_mismatch:{relative}")
            verified.append({"path": relative, "sha256": observed, "bytes": path.stat().st_size})
        except EvidenceInstrumentError as exc:
            blockers.append(f"changed_path_invalid:{raw}:{exc}")
    extra_hashes = sorted(set(str(key) for key in stored_hashes) - set(normalized))
    if extra_hashes:
        blockers.append("unreferenced_stored_hashes:" + ",".join(extra_hashes[:20]))
    required_verified = 0
    for record in required_outputs:
        relative = str(record.get("path") or "")
        if not relative:
            blockers.append("required_output_path_missing")
            continue
        try:
            path = _regular(candidate, relative)
            expected_value = record.get("sha256")
            if expected_value and not _required_output_hash_matches(
                path, expected_value
            ):
                blockers.append(f"required_output_hash_mismatch:{relative}")
            declared_bytes = record.get("bytes")
            if declared_bytes is not None and int(declared_bytes) != path.stat().st_size:
                blockers.append(f"required_output_size_mismatch:{relative}")
            required_verified += 1
        except (EvidenceInstrumentError, TypeError, ValueError) as exc:
            blockers.append(f"required_output_invalid:{relative}:{exc}")
    return {
        "schema_id": "aiworkhub.review_evidence_audit.v1",
        "status": "pass" if not blockers else "fail",
        "blocking": bool(blockers),
        "blockers": blockers,
        "changed_path_count": len(normalized),
        "changed_paths_verified": verified,
        "required_outputs_verified": required_verified,
        "changed_path_source": "explicit" if changed_paths else "stored_hashes_compat",
        "canonical_repo": str(repo),
        "candidate_repo": str(candidate),
    }


def contract_consistency_check(repo_root: Path | str) -> dict[str, Any]:
    """Compare generated provider projections and runtime policy semantics."""

    root = Path(repo_root).resolve()
    carriers: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    for provider in agent_tool_instructions.PROVIDERS:
        path = root / provider
        expected = agent_tool_instructions.render_projection(provider)
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = ""
        inspection = agent_tool_instructions.inspect_document(current)
        managed = ""
        if inspection.get("managed"):
            start = current.index(agent_tool_instructions.START)
            end = current.index(agent_tool_instructions.END, start) + len(agent_tool_instructions.END)
            managed = current[start:end].strip() + "\n"
        exact = managed == expected
        if not exact:
            blockers.append(f"projection_drift:{provider}")
        carriers[provider] = {
            "managed": bool(inspection.get("managed")),
            "exact": exact,
            "expected_sha256": hashlib.sha256(expected.encode()).hexdigest(),
            "observed_sha256": hashlib.sha256(managed.encode()).hexdigest() if managed else "",
        }
    runtime = agent_tool_instructions.render_worker_runtime_policy()
    canonical = agent_tool_instructions.render_canonical()
    clause_rows: dict[str, Any] = {}
    for clause, alternatives in agent_tool_instructions.CONTRACT_CLAUSES.items():
        provider_present = any(value in canonical for value in alternatives)
        runtime_present = any(value in runtime for value in alternatives)
        clause_rows[clause] = {
            "provider_policy": provider_present,
            "worker_runtime": runtime_present,
        }
        if not provider_present or not runtime_present:
            blockers.append(f"semantic_clause_drift:{clause}")
    return {
        "schema_id": "aiworkhub.contract_consistency.v1",
        "status": "pass" if not blockers else "fail",
        "blocking": bool(blockers),
        "blockers": blockers,
        "carriers": carriers,
        "clauses": clause_rows,
        "worker_runtime_sha256": hashlib.sha256(runtime.encode()).hexdigest(),
    }


def source_graph_retrieval_eval(
    repo_root: Path | str,
    *,
    query_fn: Callable[..., Mapping[str, Any]],
    registry_path: str = ".aiworkhub/source-graph-retrieval-eval.json",
) -> dict[str, Any]:
    """Measure bounded retrieval quality, payload bytes, and wrapper latency."""

    root = Path(repo_root).resolve()
    candidate = _regular(root, registry_path, must_exist=False)
    if not candidate.exists() and not candidate.is_symlink():
        return {
            "schema_id": "aiworkhub.source_graph_retrieval_eval.v1",
            "status": "not_configured",
            "configured": False,
            "measured": False,
            "reason": f"retrieval_registry_missing:{registry_path}",
            "cases": [],
        }
    try:
        document = json.loads(_regular(root, registry_path).read_text(encoding="utf-8"))
        cases = document.get("cases") if isinstance(document, Mapping) else None
        if not isinstance(cases, list) or not cases:
            raise EvidenceInstrumentError("retrieval_cases_missing")
        raw_minimums = document.get("minimums") or {}
        if not isinstance(raw_minimums, Mapping):
            raise EvidenceInstrumentError("retrieval_minimums_invalid")
        minimums: dict[str, float] = {}
        for metric in ("recall_at_k", "mrr", "success_at_k"):
            if metric not in raw_minimums:
                continue
            value = float(raw_minimums[metric])
            if not 0.0 <= value <= 1.0:
                raise EvidenceInstrumentError(f"retrieval_minimum_invalid:{metric}")
            minimums[metric] = value
    except (OSError, UnicodeError, json.JSONDecodeError, EvidenceInstrumentError) as exc:
        return {
            "schema_id": "aiworkhub.source_graph_retrieval_eval.v1",
            "status": "configuration_invalid",
            "configured": True,
            "measured": False,
            "reason": str(exc),
            "repair_hint": (
                "Replace the registry with a regular UTF-8 JSON file containing "
                "a non-empty cases array, or remove it to declare evaluation unconfigured."
            ),
            "cases": [],
        }
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases[:512]):
        if not isinstance(case, Mapping):
            continue
        query = str(case.get("query") or "").strip()
        expected = {_relative(value) for value in case.get("expected_paths") or []}
        mode = str(case.get("mode") or "focus")
        budget = max(1, min(int(case.get("k") or 10), 64))
        started_ns = time.perf_counter_ns()
        result = dict(query_fn(mode=mode, query=query, budget=budget, workflow_stage="validation"))
        elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        content = result.get("content")
        returned_bytes = len(
            content.encode("utf-8")
            if isinstance(content, str)
            else json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        try:
            payload = json.loads(content) if isinstance(content, str) else result
        except json.JSONDecodeError:
            payload = {}
        ranked = _ranked_paths(payload)[:budget]
        relevant_ranks = [position for position, path in enumerate(ranked, 1) if path in expected]
        precision = len(relevant_ranks) / budget
        recall = len(relevant_ranks) / len(expected) if expected else 0.0
        reciprocal = 1.0 / min(relevant_ranks) if relevant_ranks else 0.0
        rows.append({
            "id": str(case.get("id") or f"case-{index}"),
            "ok": bool(result.get("ok")),
            "expected_count": len(expected),
            "returned_count": len(ranked),
            "relevant_count": len(relevant_ranks),
            "precision_at_k": round(precision, 6),
            "recall_at_k": round(recall, 6),
            "reciprocal_rank": round(reciprocal, 6),
            "success_at_k": bool(relevant_ranks),
            "returned_bytes": returned_bytes,
            "latency_ms": round(elapsed_ms, 3),
            "accepted_outcome_observed": False,
        })
    measured = bool(rows)
    ordered_latencies = sorted(row["latency_ms"] for row in rows)
    p95_index = max(0, math.ceil(len(ordered_latencies) * 0.95) - 1)
    precision_at_k = round(sum(row["precision_at_k"] for row in rows) / len(rows), 6) if rows else None
    recall_at_k = round(sum(row["recall_at_k"] for row in rows) / len(rows), 6) if rows else None
    mrr = round(sum(row["reciprocal_rank"] for row in rows) / len(rows), 6) if rows else None
    success_at_k = round(sum(bool(row["success_at_k"]) for row in rows) / len(rows), 6) if rows else None
    observed_metrics = {
        "recall_at_k": recall_at_k,
        "mrr": mrr,
        "success_at_k": success_at_k,
    }
    gate_failures = [
        f"{metric}:{observed_metrics[metric]}<{minimum}"
        for metric, minimum in minimums.items()
        if observed_metrics[metric] is None or observed_metrics[metric] < minimum
    ]
    return {
        "schema_id": "aiworkhub.source_graph_retrieval_eval.v1",
        "status": "below_gate" if gate_failures else ("measured" if measured else "inconclusive"),
        "configured": True,
        "measured": measured,
        "blocking": bool(gate_failures),
        "case_count": len(rows),
        "precision_at_k": precision_at_k,
        "recall_at_k": recall_at_k,
        "mrr": mrr,
        "success_at_k": success_at_k,
        "mean_returned_bytes": round(sum(row["returned_bytes"] for row in rows) / len(rows), 3) if rows else None,
        "mean_latency_ms": round(sum(row["latency_ms"] for row in rows) / len(rows), 3) if rows else None,
        "p95_latency_ms": ordered_latencies[p95_index] if rows else None,
        "accepted_outcome_coverage": 0.0 if rows else None,
        "accepted_outcome_measurement_pending": measured,
        "minimums": minimums,
        "gate_failures": gate_failures,
        "cases": rows,
        "causal_token_savings_claimed": False,
    }


def _ranked_paths(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"file", "file_path", "path"} and isinstance(item, str):
                normalized = item.replace("\\", "/")
                if normalized not in found:
                    found.append(normalized)
            elif isinstance(item, (Mapping, list)):
                for path in _ranked_paths(item):
                    if path not in found:
                        found.append(path)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and "/" in item:
                normalized = item.replace("\\", "/")
                if normalized not in found:
                    found.append(normalized)
            else:
                for path in _ranked_paths(item):
                    if path not in found:
                        found.append(path)
    return found


def session_token_profile(repo_root: Path | str, *, task_id: str | None = None) -> dict[str, Any]:
    """Reconcile provider usage with prompt byte sections; never estimate tokens."""

    root = Path(repo_root).resolve()
    ledger = cost_ledger.build_cost_ledger(repo_root=root, include_tasks=True)
    usage_rows = [
        row for row in ledger.get("tasks") or []
        if task_id is None or str(row.get("task_id") or "") == task_id
    ]
    usage_observed = [row for row in usage_rows if row.get("usage_observed")]
    totals = {
        key: sum(int(row.get(key) or 0) for row in usage_observed)
        for key in (
            "input_tokens", "output_tokens", "visible_output_tokens",
            "reasoning_output_tokens", "total_tokens", "cached_input_tokens",
            "cache_creation_input_tokens", "cache_write_input_tokens",
        )
    }
    prompt_sections: Counter[str] = Counter()
    event_path = root / ".aiworkhub/runtime/process_logs/process_events.jsonl"
    event_count = 0
    if event_path.is_file() and event_path.stat().st_size <= MAX_ARTIFACT_BYTES:
        for line in event_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if task_id and str(event.get("task_id") or "") != task_id:
                continue
            budget = event.get("prompt_budget") or event.get("worker_prompt_budget")
            if not isinstance(budget, Mapping):
                continue
            event_count += 1
            for key, value in (budget.get("sections") or {}).items():
                if isinstance(value, int) and value >= 0:
                    prompt_sections[str(key)] += value
    return {
        "schema_id": "aiworkhub.session_token_profile.v1",
        "status": "measured" if usage_observed else "usage_unobserved",
        "task_id": task_id,
        "usage_records": len(usage_rows),
        "usage_observed_records": len(usage_observed),
        "provider_tokens": totals if usage_observed else None,
        "prompt_byte_sections": dict(sorted(prompt_sections.items())),
        "prompt_budget_events": event_count,
        "bytes_are_provider_tokens": False,
        "reconciliation": "separate_observed_units_no_conversion",
    }


def risk_mode_precision_bench(
    repo_root: Path | str,
    *,
    artifact_path: str = ".aiworkhub/risk-mode-adjudication.jsonl",
) -> dict[str, Any]:
    """Compute per-mode/language precision from explicitly adjudicated rows."""

    root = Path(repo_root).resolve()
    try:
        rows = _json_rows(root, artifact_path)
    except EvidenceInstrumentError as exc:
        return {"schema_id": "aiworkhub.risk_mode_precision.v1", "status": "not_configured", "measured": False, "reason": str(exc)}
    buckets: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "adjudicated": 0})
    for row in rows:
        if row.get("adjudicated") is not True or row.get("predicted") is not True:
            continue
        key = (str(row.get("mode") or "unknown"), str(row.get("language") or "unknown"))
        buckets[key]["adjudicated"] += 1
        buckets[key]["tp" if row.get("correct") is True else "fp"] += 1
    results = []
    for (mode, language), counts in sorted(buckets.items()):
        denominator = counts["tp"] + counts["fp"]
        results.append({
            "mode": mode,
            "language": language,
            **counts,
            "precision": round(counts["tp"] / denominator, 6) if denominator else None,
        })
    return {
        "schema_id": "aiworkhub.risk_mode_precision.v1",
        "status": "measured" if results else "inconclusive",
        "measured": bool(results),
        "buckets": results,
        "cap_policy_changed": False,
    }


def quality_gate_ratchet(
    repo_root: Path | str,
    *,
    violations: Sequence[Mapping[str, Any]],
    baseline_path: str = ".aiworkhub/quality-ratchet.json",
) -> dict[str, Any]:
    """Fail only on new identities and configured no-net-growth overflow."""

    root = Path(repo_root).resolve()
    # Resolve the relative path first: an invalid/escaping path is a
    # misconfiguration, not a corrupted baseline, and stays not_configured.
    try:
        candidate = _regular(root, baseline_path, must_exist=False)
    except EvidenceInstrumentError as exc:
        return {"schema_id": "aiworkhub.quality_gate_ratchet.v1", "status": "not_configured", "blocking": False, "reason": f"baseline_path_invalid:{exc}"}
    # An absent baseline declares the ratchet unconfigured (non-blocking). A
    # present-but-unreadable or shape-invalid baseline must fail closed: a
    # corrupt or oversized file can no longer silently erase the ratchet.
    if not candidate.exists() and not candidate.is_symlink():
        return {"schema_id": "aiworkhub.quality_gate_ratchet.v1", "status": "not_configured", "blocking": False, "reason": "baseline_absent"}
    try:
        baseline = json.loads(_regular(root, baseline_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, EvidenceInstrumentError) as exc:
        return {"schema_id": "aiworkhub.quality_gate_ratchet.v1", "status": "error", "blocking": True, "reason": f"baseline_unreadable:{exc}"}
    if not isinstance(baseline, Mapping):
        return {"schema_id": "aiworkhub.quality_gate_ratchet.v1", "status": "error", "blocking": True, "reason": "baseline_shape_invalid"}
    known = {str(value) for value in baseline.get("violation_ids") or []}
    observed = {
        str(row.get("id") or "") for row in violations
        if isinstance(row, Mapping) and str(row.get("id") or "")
    }
    new = sorted(observed - known)
    growth: list[dict[str, Any]] = []
    for relative, maximum in (baseline.get("max_lines") or {}).items():
        try:
            path = _regular(root, str(relative))
            count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            if count > int(maximum):
                growth.append({"path": str(relative), "lines": count, "maximum": int(maximum)})
        except (EvidenceInstrumentError, TypeError, ValueError):
            growth.append({"path": str(relative), "error": "unreadable"})
    return {
        "schema_id": "aiworkhub.quality_gate_ratchet.v1",
        "status": "pass" if not new and not growth else "fail",
        "blocking": bool(new or growth),
        "new_violation_ids": new,
        "grandfathered_observed": len(observed & known),
        "no_net_growth_violations": growth,
    }


def prompt_bundle_ab_report(
    repo_root: Path | str,
    *,
    artifact_path: str = ".aiworkhub/prompt-ab-canary.jsonl",
) -> dict[str, Any]:
    """Evaluate preregistered, matched A/B rows with observed provider usage."""

    root = Path(repo_root).resolve()
    try:
        rows = _json_rows(root, artifact_path)
    except EvidenceInstrumentError as exc:
        return {"schema_id": "aiworkhub.prompt_bundle_ab.v1", "status": "not_configured", "measured": False, "reason": str(exc)}
    pairs: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        pair = str(row.get("pair_id") or "")
        variant = str(row.get("variant") or "").upper()
        if pair and variant in {"A", "B"}:
            pairs[pair][variant] = row
    measured: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for pair_id, variants in sorted(pairs.items()):
        if set(variants) != {"A", "B"}:
            excluded.append({"pair_id": pair_id, "reason": "incomplete_pair"})
            continue
        a, b = variants["A"], variants["B"]
        identity = ("task_family", "model", "adapter")
        if any(str(a.get(key) or "") != str(b.get(key) or "") for key in identity):
            excluded.append({"pair_id": pair_id, "reason": "identity_mismatch"})
            continue
        if a.get("usage_observed") is not True or b.get("usage_observed") is not True:
            excluded.append({"pair_id": pair_id, "reason": "usage_unobserved"})
            continue
        metrics: dict[str, Any] = {}
        for key in ("input_tokens", "output_tokens", "total_tokens", "elapsed_ms"):
            before = int(a.get(key) or 0)
            after = int(b.get(key) or 0)
            metrics[key] = {
                "a": before,
                "b": after,
                "delta": after - before,
                "delta_percent": round(100.0 * (after - before) / before, 3) if before else None,
            }
        measured.append({
            "pair_id": pair_id,
            "metrics": metrics,
            "acceptance_equal": a.get("accepted") == b.get("accepted"),
            "preregistered_metric": str(a.get("preregistered_metric") or "total_tokens"),
        })
    return {
        "schema_id": "aiworkhub.prompt_bundle_ab.v1",
        "status": "measured" if measured else "inconclusive",
        "measured": bool(measured),
        "pair_count": len(measured),
        "pairs": measured,
        "excluded": excluded,
        "causal_scope": "paired_rows_only",
    }


def suite_profile(
    repo_root: Path | str,
    *,
    argv: Sequence[str],
    repeats: int = 2,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Run an exact argv repeatedly and report time/flake evidence."""

    root = Path(repo_root).resolve()
    repeats = max(1, min(int(repeats), 5))
    timeout_seconds = max(1, min(int(timeout_seconds), 3600))
    if not argv or any(not isinstance(value, str) or not value or "\x00" in value for value in argv):
        raise EvidenceInstrumentError("suite_profile_argv_invalid")
    runs: list[dict[str, Any]] = []
    per_test_runs: list[list[dict[str, Any]]] = []
    pytest_command = any(
        value == "pytest" or Path(value).name.startswith("pytest")
        for value in argv
    )
    for _ in range(repeats):
        started = time.perf_counter()
        profile_path = ""
        run_argv = list(argv)
        env = os.environ.copy()
        if pytest_command:
            handle = tempfile.NamedTemporaryFile(
                prefix="aiworkhub-suite-", suffix=".json", delete=False
            )
            profile_path = handle.name
            handle.close()
            env["AIWORKHUB_SUITE_PROFILE_OUTPUT"] = profile_path
            package_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = os.pathsep.join(
                value for value in (package_root, env.get("PYTHONPATH", "")) if value
            )
            run_argv.extend(["-p", "aiworkhub.suite_profile_plugin"])
        try:
            completed = subprocess.run(
                run_argv, cwd=root, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout_seconds, shell=False, check=False, env=env,
            )
            runs.append({
                "returncode": completed.returncode,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
                "stdout_bytes": len(completed.stdout),
                "stderr_bytes": len(completed.stderr),
                "timed_out": False,
            })
        except subprocess.TimeoutExpired as exc:
            runs.append({
                "returncode": None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "stdout_sha256": hashlib.sha256(exc.stdout or b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(exc.stderr or b"").hexdigest(),
                "stdout_bytes": len(exc.stdout or b""),
                "stderr_bytes": len(exc.stderr or b""),
                "timed_out": True,
            })
        finally:
            test_rows: list[dict[str, Any]] = []
            if profile_path:
                try:
                    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
                    test_rows = [dict(row) for row in profile.get("tests") or []]
                except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                    test_rows = []
                try:
                    Path(profile_path).unlink()
                except OSError:
                    pass
            per_test_runs.append(test_rows)
    outcomes = {(row["returncode"], row["timed_out"]) for row in runs}
    tests_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for test_rows in per_test_runs:
        for row in test_rows:
            tests_by_node[str(row.get("nodeid") or "")].append(row)
    test_profiles = []
    for nodeid, rows in sorted(tests_by_node.items()):
        observed_outcomes = {str(row.get("outcome") or "") for row in rows}
        test_profiles.append({
            "nodeid": nodeid,
            "run_count": len(rows),
            "flake_observed": len(observed_outcomes) > 1,
            "outcomes": dict(sorted(Counter(str(row.get("outcome") or "") for row in rows).items())),
            "duration_seconds": [row.get("duration_seconds") for row in rows],
            "cpu_seconds": [row.get("cpu_seconds") for row in rows],
            "rss_delta_bytes": [row.get("rss_delta_bytes") for row in rows],
            "disk_free_delta_bytes": [row.get("disk_free_delta_bytes") for row in rows],
        })
    return {
        "schema_id": "aiworkhub.suite_profile.v1",
        "status": "measured",
        "argv": list(argv),
        "repeat_count": repeats,
        "flake_observed": len(outcomes) > 1 or any(row["flake_observed"] for row in test_profiles),
        "runs": runs,
        "per_test_observed": bool(test_profiles),
        "tests": test_profiles,
        "rss_semantics": "per-process-high-water delta; zero does not mean no allocation",
        "disk_semantics": "filesystem-free-space delta; association only",
    }


def coverage_import_preview(
    repo_root: Path | str,
    *,
    artifact_path: str,
) -> dict[str, Any]:
    """Parse coverage.py JSON into a bounded canonical file map."""

    root = Path(repo_root).resolve()
    path = _regular(root, artifact_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceInstrumentError("coverage_artifact_invalid") from exc
    files = document.get("files") if isinstance(document, Mapping) else None
    if not isinstance(files, Mapping):
        raise EvidenceInstrumentError("coverage_files_missing")
    mapped: list[dict[str, Any]] = []
    for raw_path, row in list(files.items())[:MAX_ROWS]:
        if not isinstance(row, Mapping):
            continue
        try:
            relative = _relative(raw_path)
        except EvidenceInstrumentError:
            continue
        summary = row.get("summary") or {}
        mapped.append({
            "file_path": relative,
            "covered_lines": int(summary.get("covered_lines") or 0),
            "missing_lines": int(summary.get("missing_lines") or 0),
            "percent_covered": float(summary.get("percent_covered") or 0.0),
        })
    return {
        "schema_id": "aiworkhub.runtime_coverage.v1",
        "status": "preview",
        "source_path": _relative(artifact_path),
        "source_sha256": _sha256(path),
        "file_count": len(mapped),
        "files": mapped,
    }


def coverage_import_apply(repo_root: Path | str, *, artifact_path: str) -> dict[str, Any]:
    """Persist a manager-authorized runtime coverage projection atomically."""

    root = Path(repo_root).resolve()
    preview = coverage_import_preview(root, artifact_path=artifact_path)
    target = root / ".aiworkhub/source_graph/runtime_coverage.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**preview, "status": "available", "imported_at_epoch": int(time.time())}
    temp = target.with_suffix(f".tmp-{os.getpid()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return {**payload, "stored_path": target.relative_to(root).as_posix()}


def runtime_coverage_for_paths(repo_root: Path | str, paths: Iterable[str]) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    target = root / ".aiworkhub/source_graph/runtime_coverage.json"
    if not target.is_file():
        return {
            "status": "not_available",
            "line_coverage": None,
            "branch_coverage": None,
            "reason": "no_runtime_coverage_evidence_imported",
        }
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "status": "unavailable",
            "line_coverage": None,
            "branch_coverage": None,
            "reason": "runtime_coverage_projection_invalid",
        }
    # A document that is not a Mapping, or whose "files" is not a list, is a
    # shape-invalid projection -- report it, never raise on bad input.
    files = document.get("files") if isinstance(document, Mapping) else None
    if not isinstance(files, list):
        return {
            "status": "unavailable",
            "line_coverage": None,
            "branch_coverage": None,
            "reason": "runtime_coverage_projection_shape_invalid",
        }
    wanted = {str(path).replace("\\", "/") for path in paths}
    rows = [
        row for row in files
        if isinstance(row, Mapping) and str(row.get("file_path") or "") in wanted
    ]
    # Zero matched rows is unmeasured, never "available": an unmeasured path
    # must not masquerade as measured.
    if not rows:
        return {
            "status": "not_available",
            "line_coverage": None,
            "branch_coverage": None,
            "reason": "no_runtime_coverage_for_requested_paths",
            "matched_files": 0,
        }
    total_covered = sum(int(row.get("covered_lines") or 0) for row in rows)
    total_missing = sum(int(row.get("missing_lines") or 0) for row in rows)
    denominator = total_covered + total_missing
    return {
        "status": "available",
        "line_coverage": round(100.0 * total_covered / denominator, 6) if denominator else None,
        "branch_coverage": None,
        "source_sha256": str(document.get("source_sha256") or ""),
        "files": rows[:200],
        "matched_files": len(rows),
    }
