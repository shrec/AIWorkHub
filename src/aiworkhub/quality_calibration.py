"""Deterministic calibration matrix for the canonical Quality Gate 2.0 fold.

This module exercises every blocker family owned by ``fold_quality_verdict``.
It has no repository or model side effects, so the same matrix runs on Linux,
Windows, macOS and the Remote-SSH contract. A gate predicate is not trusted
merely because a unit test reached its code path: the reference positives must
stay green, each targeted defect must stay red, and the expected blocker must
remain observable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import quality_evidence as qe

SCHEMA_ID = "aiworkhub.quality_calibration.v1"


def _check(status: str = qe.STATUS_PASSED) -> dict[str, str]:
    return {"check_id": "tests", "kind": "test", "status": status}


def _combined(status: str = qe.STATUS_PASSED) -> list[dict[str, str]]:
    return [{"check_id": "combined", "kind": "test", "status": status}]


def _report(
    lens: str = qe.LENS_CORRECTNESS,
    *,
    provider: str = "reviewer-b",
    findings: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "lens": lens,
        "provider": provider,
        "read_only": True,
        "can_mutate_repo": False,
        "findings": findings or [],
    }


def _finding(**overrides: Any) -> dict[str, str]:
    row = {
        "id": "finding-1",
        "severity": qe.SEVERITY_HIGH,
        "summary": "targeted calibration defect",
        "evidence": "fixture:targeted-defect",
    }
    row.update({str(key): str(value) for key, value in overrides.items()})
    return row


def _case(
    case_id: str,
    *,
    expected_pass: bool,
    expected_blocker: str = "",
    checks: list[Any] | None = None,
    risk: str = qe.RISK_LOW,
    reports: list[Any] | None = None,
    combined: list[Any] | None = None,
    worker_provider: str = "worker-a",
    approval: bool = False,
    config_error: str = "",
) -> dict[str, Any]:
    verdict = qe.fold_quality_verdict(
        checks if checks is not None else [_check()],
        risk_profile=qe.resolve_risk_profile(risk),
        reviewer_reports=reports or [],
        combined_tree_checks=combined or [],
        worker_provider=worker_provider,
        human_approval=approval,
        config_error=config_error,
    )
    blockers = [str(value) for value in verdict.get("blocking_evidence") or []]
    blocker_observed = not expected_blocker or any(
        value == expected_blocker or value.startswith(expected_blocker)
        for value in blockers
    )
    actual_pass = verdict.get("passed") is True
    return {
        "case_id": case_id,
        "expected_pass": expected_pass,
        "actual_pass": actual_pass,
        "expected_blocker": expected_blocker,
        "expected_blocker_observed": blocker_observed,
        "blocking_evidence": blockers,
        "calibrated": actual_pass is expected_pass and blocker_observed,
    }


# ── Sealed adjudicated review corpus (NF-2026-00936) ───────────────────────
# The matrix above proves the fold's *structure*: a malformed report blocks,
# a missing reviewer blocks. It cannot say whether a lens catches a real
# defect, because none of its candidates contains one. This corpus does: each
# candidate is a small change with a human adjudication (``defect`` or
# ``clean``) and the reviewer report that was recorded for it. Folding those
# recorded reports through the canonical gate yields per-lens TP/FP/FN/TN, and
# a lens with too few adjudicated candidates of either kind reads UNKNOWN
# rather than a vacuous perfect score. The corpus is sealed by digest: editing
# a candidate without re-sealing makes every lens UNKNOWN and the run not ok.
CORPUS_SCHEMA_ID = "aiworkhub.quality_calibration.review_corpus.v1"
ADJUDICATION_DEFECT = "defect"
ADJUDICATION_CLEAN = "clean"
UNKNOWN = "UNKNOWN"
LENS_STATE_MEASURED = "measured"
MIN_DEFECTS_PER_LENS = 2
MIN_CLEAN_PER_LENS = 2
MAX_CORPUS_CANDIDATES = 256


def _candidate(
    candidate_id: str,
    lens: str,
    adjudication: str,
    change: str,
    *findings: tuple[str, str, str],
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "lens": lens,
        "adjudication": adjudication,
        "change": change,
        "recorded_findings": [
            {"id": fid, "severity": qe.SEVERITY_HIGH, "summary": summary, "evidence": evidence}
            for fid, summary, evidence in findings
        ],
    }


REVIEW_CORPUS: tuple[dict[str, Any], ...] = (
    _candidate(
        "corr-off-by-one", qe.LENS_CORRECTNESS, ADJUDICATION_DEFECT,
        "for i in range(len(rows) - 1): emit(rows[i])  # drops the last row",
        ("off-by-one", "loop skips the final row", "range(len(rows) - 1)"),
    ),
    _candidate(
        "corr-inverted-guard", qe.LENS_CORRECTNESS, ADJUDICATION_DEFECT,
        "if not lease.expired(): reclaim(lease)  # reclaims live leases",
        ("inverted-guard", "guard condition is inverted", "if not lease.expired()"),
    ),
    # Adjudicated defect the recorded reviewer MISSED: kept so the corpus
    # measures recall honestly instead of only ever containing catches.
    _candidate(
        "corr-unknown-as-pass", qe.LENS_CORRECTNESS, ADJUDICATION_DEFECT,
        "return results.get(check_id, 'passed')  # absent evidence reads as pass",
    ),
    _candidate(
        "corr-clean-bounds", qe.LENS_CORRECTNESS, ADJUDICATION_CLEAN,
        "for row in rows[:limit]: emit(row)",
    ),
    _candidate(
        "corr-clean-guard", qe.LENS_CORRECTNESS, ADJUDICATION_CLEAN,
        "if lease.expired(): reclaim(lease)",
    ),
    _candidate(
        "sec-shell-string", qe.LENS_SECURITY, ADJUDICATION_DEFECT,
        "subprocess.run(f'git log {user_ref}', shell=True)",
        ("shell-injection", "user input interpolated into a shell string", "shell=True"),
    ),
    _candidate(
        "sec-path-escape", qe.LENS_SECURITY, ADJUDICATION_DEFECT,
        "open(repo_root / request['path'])  # no containment check",
        ("path-escape", "request path can escape the repository root", "repo_root / request['path']"),
    ),
    _candidate(
        "sec-clean-argv", qe.LENS_SECURITY, ADJUDICATION_CLEAN,
        "subprocess.run(['git', 'log', '--', user_ref], check=True)",
    ),
    _candidate(
        "sec-clean-contained", qe.LENS_SECURITY, ADJUDICATION_CLEAN,
        "target = (root / rel).resolve(); target.relative_to(root.resolve())",
    ),
    _candidate(
        "cq-copied-helper", qe.LENS_CODE_QUALITY, ADJUDICATION_DEFECT,
        "def _sha(b): return hashlib.sha256(b).hexdigest()  # third copy of the owner helper",
        ("copied-helper", "duplicates the single owning helper", "def _sha(b)"),
    ),
    _candidate(
        "cq-silent-except", qe.LENS_CODE_QUALITY, ADJUDICATION_DEFECT,
        "try: persist(row)\nexcept Exception: pass",
        ("silent-except", "failure swallowed with no named cause", "except Exception: pass"),
    ),
    _candidate(
        "cq-clean-import-owner", qe.LENS_CODE_QUALITY, ADJUDICATION_CLEAN,
        "from .hashing import sha256_hex",
    ),
    _candidate(
        "cq-clean-named-error", qe.LENS_CODE_QUALITY, ADJUDICATION_CLEAN,
        "except OSError as exc: raise StoreError(f'persist_failed:{exc.errno}') from exc",
    ),
    # Adjudicated clean change the recorded reviewer flagged anyway: a
    # measured false positive, so precision is not trivially 1.0.
    _candidate(
        "cq-clean-flagged", qe.LENS_CODE_QUALITY, ADJUDICATION_CLEAN,
        "MAX_ROWS = 500  # bounded batch, documented in the module docstring",
        ("magic-number", "unexplained constant", "MAX_ROWS = 500"),
    ),
)


def corpus_digest(corpus: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    """sha256 of the canonical JSON encoding of ``corpus``."""

    encoded = json.dumps(list(corpus), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# Re-seal deliberately: python tools/pyrun.py -c "from aiworkhub import
# quality_calibration as q; print(q.corpus_digest(q.REVIEW_CORPUS))"
REVIEW_CORPUS_SHA256 = "3f1d4049bacaf77fda7ad0c0a692b18d8d7aa46eef0cb762ef24fdbe4a9fd510"


def _lens_flagged(blockers: list[str], lens: str) -> bool:
    return any(
        value.startswith(f"reviewer:{lens}:")
        or value.startswith(f"refinement_required:reviewer:{lens}:")
        for value in blockers
    )


def _evaluate_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {"candidate_id": "", "valid": False, "error": "candidate_not_object"}
    candidate_id = str(candidate.get("candidate_id") or "")[:128]
    lens = candidate.get("lens")
    adjudication = candidate.get("adjudication")
    findings = candidate.get("recorded_findings")
    error = ""
    if not candidate_id:
        error = "candidate_id_missing"
    elif lens not in qe.JUDGMENT_LENSES:
        error = "candidate_lens_invalid"
    elif adjudication not in (ADJUDICATION_DEFECT, ADJUDICATION_CLEAN):
        error = "candidate_adjudication_invalid"
    elif not isinstance(findings, list):
        error = "candidate_findings_invalid"
    if error:
        return {"candidate_id": candidate_id, "valid": False, "error": error}
    reports = [
        _report(name, findings=list(findings) if name == lens else [])
        for name in sorted(qe.JUDGMENT_LENSES)
    ]
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=qe.resolve_risk_profile(qe.RISK_HIGH),
        reviewer_reports=reports,
        combined_tree_checks=_combined(),
        worker_provider="worker-a",
        human_approval=True,
        config_error="",
    )
    blockers = [str(value) for value in verdict.get("blocking_evidence") or []]
    schema_errors = [value for value in blockers if value.startswith("reviewer_schema")]
    if schema_errors:
        return {"candidate_id": candidate_id, "valid": False, "error": schema_errors[0]}
    flagged = _lens_flagged(blockers, str(lens))
    defect = adjudication == ADJUDICATION_DEFECT
    outcome = ("tp" if flagged else "fn") if defect else ("fp" if flagged else "tn")
    return {
        "candidate_id": candidate_id,
        "valid": True,
        "lens": lens,
        "adjudication": adjudication,
        "flagged": flagged,
        "outcome": outcome,
    }


def _lens_metrics(rows: list[dict[str, Any]], *, min_defects: int, min_clean: int) -> dict[str, Any]:
    counts = {key: sum(1 for row in rows if row["outcome"] == key) for key in ("tp", "fp", "fn", "tn")}
    defects = counts["tp"] + counts["fn"]
    clean = counts["fp"] + counts["tn"]
    measured = defects >= min_defects and clean >= min_clean
    flagged = counts["tp"] + counts["fp"]
    return {
        **counts,
        "defect_count": defects,
        "clean_count": clean,
        "state": LENS_STATE_MEASURED if measured else UNKNOWN,
        "recall": counts["tp"] / defects if measured else UNKNOWN,
        "precision": (counts["tp"] / flagged if flagged else 0.0) if measured else UNKNOWN,
        "false_positive_rate": counts["fp"] / clean if measured else UNKNOWN,
    }


def run_review_corpus_calibration(
    corpus: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    *,
    sealed_sha256: str = "",
    min_defects: int = MIN_DEFECTS_PER_LENS,
    min_clean: int = MIN_CLEAN_PER_LENS,
) -> dict[str, Any]:
    """Per-lens TP/FP/FN/TN over a sealed adjudicated defect/clean corpus.

    Fail-closed: a broken seal, an invalid candidate, an oversized corpus or
    an undersampled lens is never reported as a measured pass.
    """

    rows_in = REVIEW_CORPUS if corpus is None else corpus
    seal = sealed_sha256 or REVIEW_CORPUS_SHA256
    digest = corpus_digest(rows_in)
    seal_intact = digest == seal
    oversized = len(rows_in) > MAX_CORPUS_CANDIDATES
    evaluated = [] if oversized else [_evaluate_candidate(row) for row in rows_in]
    invalid = [row for row in evaluated if not row["valid"]]
    valid = [row for row in evaluated if row["valid"]]
    lenses: dict[str, Any] = {}
    for lens in sorted(qe.JUDGMENT_LENSES):
        metrics = _lens_metrics(
            [row for row in valid if row["lens"] == lens],
            min_defects=min_defects,
            min_clean=min_clean,
        )
        if not seal_intact:
            metrics = {**metrics, "state": UNKNOWN, "recall": UNKNOWN, "precision": UNKNOWN, "false_positive_rate": UNKNOWN}
        lenses[lens] = metrics
    unknown_lenses = [lens for lens, metrics in lenses.items() if metrics["state"] == UNKNOWN]
    return {
        "ok": seal_intact and not oversized and not invalid and not unknown_lenses,
        "schema_id": CORPUS_SCHEMA_ID,
        "corpus_sha256": digest,
        "sealed_sha256": seal,
        "seal_intact": seal_intact,
        "oversized": oversized,
        "candidate_count": len(rows_in),
        "defect_count": sum(1 for row in valid if row["adjudication"] == ADJUDICATION_DEFECT),
        "clean_count": sum(1 for row in valid if row["adjudication"] == ADJUDICATION_CLEAN),
        "invalid_candidates": [{"candidate_id": row["candidate_id"], "error": row["error"]} for row in invalid],
        "unknown_lenses": unknown_lenses,
        "lenses": lenses,
        "candidates": valid,
    }


def run_calibration() -> dict[str, Any]:
    """Run the bounded positive/negative predicate matrix and report rates."""

    good_medium = [_report()]
    good_high = [_report(lens) for lens in sorted(qe.JUDGMENT_LENSES)]
    malformed_finding_cases = [
        ("reviewer_finding_not_object", ["bad"], "reviewer_schema:0:0:not_object"),
        ("reviewer_finding_severity", [_finding(severity="unknown")], "reviewer_schema:0:0:invalid_severity"),
        ("reviewer_finding_id", [_finding(id="")], "reviewer_schema:0:0:id_missing"),
        ("reviewer_finding_summary", [_finding(summary="")], "reviewer_schema:0:0:summary_missing"),
        ("reviewer_finding_evidence", [_finding(evidence="")], "reviewer_schema:0:0:evidence_missing"),
    ]
    cases = [
        _case("reference_low", expected_pass=True),
        _case("mechanical_failed", expected_pass=False, expected_blocker="tests", checks=[_check(qe.STATUS_FAILED)]),
        _case("mechanical_unavailable", expected_pass=False, expected_blocker="tests", checks=[_check(qe.STATUS_NOT_AVAILABLE)]),
        _case("mechanical_schema", expected_pass=False, expected_blocker="mechanical_schema:", checks=[{"kind": "test", "status": qe.STATUS_PASSED}]),
        _case("reference_medium", expected_pass=True, risk=qe.RISK_MEDIUM, reports=good_medium, combined=_combined()),
        _case("required_reviewer", expected_pass=False, expected_blocker="required_reviewer_missing:correctness", risk=qe.RISK_MEDIUM, combined=_combined()),
        _case("reviewer_invalid_lens", expected_pass=False, expected_blocker="reviewer_schema:0:invalid_lens", risk=qe.RISK_MEDIUM, reports=[_report("invalid")], combined=_combined()),
        _case("reviewer_provider", expected_pass=False, expected_blocker="reviewer_schema:0:provider_missing", risk=qe.RISK_MEDIUM, reports=[_report(provider="")], combined=_combined()),
        _case("reviewer_read_only", expected_pass=False, expected_blocker="reviewer_schema:0:not_read_only", risk=qe.RISK_MEDIUM, reports=[{**_report(), "read_only": False}], combined=_combined()),
        _case("reviewer_findings_shape", expected_pass=False, expected_blocker="reviewer_schema:0:findings_invalid", risk=qe.RISK_MEDIUM, reports=[{**_report(), "findings": "bad"}], combined=_combined()),
        *[
            _case(case_id, expected_pass=False, expected_blocker=blocker, risk=qe.RISK_MEDIUM, reports=[_report(findings=findings)], combined=_combined())
            for case_id, findings, blocker in malformed_finding_cases
        ],
        _case("reviewer_report_overflow", expected_pass=False, expected_blocker="reviewer_schema:too_many_reports", risk=qe.RISK_MEDIUM, reports=[_report() for _ in range(qe.MAX_REVIEW_REPORTS + 1)], combined=_combined()),
        _case("reviewer_blocking_finding", expected_pass=False, expected_blocker="reviewer:correctness:finding-1", risk=qe.RISK_MEDIUM, reports=[_report(findings=[_finding()])], combined=_combined()),
        _case("reviewer_refinement", expected_pass=False, expected_blocker="refinement_required:reviewer:correctness:finding-1", risk=qe.RISK_MEDIUM, reports=[_report(findings=[_finding(severity=qe.SEVERITY_LOW)])], combined=_combined()),
        _case("combined_missing", expected_pass=False, expected_blocker="combined_tree_evidence_missing", risk=qe.RISK_MEDIUM, reports=good_medium),
        _case("combined_failed", expected_pass=False, expected_blocker="combined_tree:combined", risk=qe.RISK_MEDIUM, reports=good_medium, combined=_combined(qe.STATUS_FAILED)),
        _case("combined_schema", expected_pass=False, expected_blocker="combined_tree_schema:", risk=qe.RISK_MEDIUM, reports=good_medium, combined=[{"kind": "test", "status": qe.STATUS_PASSED}]),
        _case("reference_high", expected_pass=True, risk=qe.RISK_HIGH, reports=good_high, combined=_combined(), approval=True),
        _case("worker_provider_missing", expected_pass=False, expected_blocker="worker_provider_missing_for_independence:", risk=qe.RISK_HIGH, reports=good_high, combined=_combined(), worker_provider="", approval=True),
        # Independence is a recorded ladder, not a vendor check: a same-provider
        # reviewer with a present worker provider is no longer refused (see the
        # ``single_provider_same_model`` positive below). What still blocks is a
        # review with no resolvable rung -- an unattributable worker (missing
        # worker_provider) -- and that holds regardless of the reviewer vendor.
        _case("independence_no_resolvable_rung", expected_pass=False, expected_blocker="worker_provider_missing_for_independence:", risk=qe.RISK_HIGH, reports=[_report(lens, provider="worker-a") for lens in sorted(qe.JUDGMENT_LENSES)], combined=_combined(), worker_provider="", approval=True),
        # A single-provider, single-model install still gets a real independent
        # review: it degrades to the same_model_fresh_context rung and PASSES the
        # high-risk profile with three lenses and explicit approval.
        _case("single_provider_same_model", expected_pass=True, risk=qe.RISK_HIGH, reports=[_report(lens, provider="worker-a") for lens in sorted(qe.JUDGMENT_LENSES)], combined=_combined(), worker_provider="worker-a", approval=True),
        _case("human_approval", expected_pass=False, expected_blocker="explicit_human_approval_missing", risk=qe.RISK_HIGH, reports=good_high, combined=_combined()),
        _case("quality_config", expected_pass=False, expected_blocker="quality_config_error", config_error="broken policy"),
    ]
    false_greens = [row["case_id"] for row in cases if not row["expected_pass"] and row["actual_pass"]]
    false_reds = [row["case_id"] for row in cases if row["expected_pass"] and not row["actual_pass"]]
    uncalibrated = [row["case_id"] for row in cases if not row["calibrated"]]
    negatives = sum(1 for row in cases if not row["expected_pass"])
    positives = len(cases) - negatives
    review_corpus = run_review_corpus_calibration()
    return {
        "ok": not uncalibrated and review_corpus["ok"] is True,
        "schema_id": SCHEMA_ID,
        "case_count": len(cases),
        "positive_count": positives,
        "negative_count": negatives,
        "false_green_count": len(false_greens),
        "false_red_count": len(false_reds),
        # No negatives (or positives) means the rate was never measured; a
        # 0.0 there would be a vacuous pass, so it reads UNKNOWN instead.
        "false_green_rate": len(false_greens) / negatives if negatives else UNKNOWN,
        "false_red_rate": len(false_reds) / positives if positives else UNKNOWN,
        "false_green_cases": false_greens,
        "false_red_cases": false_reds,
        "uncalibrated_cases": uncalibrated,
        "cases": cases,
        "review_corpus": review_corpus,
    }


__all__ = [
    "ADJUDICATION_CLEAN",
    "ADJUDICATION_DEFECT",
    "CORPUS_SCHEMA_ID",
    "MIN_CLEAN_PER_LENS",
    "MIN_DEFECTS_PER_LENS",
    "REVIEW_CORPUS",
    "REVIEW_CORPUS_SHA256",
    "SCHEMA_ID",
    "UNKNOWN",
    "corpus_digest",
    "run_calibration",
    "run_review_corpus_calibration",
]
