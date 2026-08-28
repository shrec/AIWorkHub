from __future__ import annotations

import ast
import fnmatch
import importlib.util
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping

from . import eval_artifact_gate, evidence_levels, known_bug_scanner, quality_review

# ---------------------------------------------------------------------------
# 0.6.30 Quality Evidence Engine foundation.
#
# Detects repo languages/tools from known manifests/configs using bounded
# exact paths, builds a zero-config deterministic profile from
# already-installed/declared commands only, and never installs anything.
# Every check normalizes into one canonical evidence schema so downstream
# adapters (SARIF/JUnit/coverage/benchmark/AI-reviewer) and the risk-based
# optional-gate policy share one shape. Fail-closed on malformed config;
# commands only ever run as argv arrays (shell=False), never as strings.
# ---------------------------------------------------------------------------

SCHEMA_ID = "aiworkhub.quality_evidence.v1"
VERDICT_SCHEMA_ID = "aiworkhub.quality_verdict.v2"
BEHAVIORAL_GATE_SCHEMA_ID = "aiworkhub.behavioral_quality_gate.v1"

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_NOT_AVAILABLE = "not_available"
STATUS_SKIPPED = "skipped"
STATUS_REVIEWER_COULD_NOT_INSPECT = "reviewer_could_not_inspect"
# Every status the deterministic fold can write into a lens row is enumerated
# here once, and VALID_STATUSES is derived from that single tuple. The fold
# assigns reviewer_could_not_inspect to a lens whose dispatched reviewer was
# blind (see fold_quality_verdict); omitting it from VALID_STATUSES -- as the
# hand-written set did -- made the module reject a row its own verdict
# legitimately produced. Deriving the set from LENS_STATUSES keeps the validity
# vocabulary and the fold's writable vocabulary from drifting apart again, and
# quality_verdict_contract() publishes this exact set rather than a second copy.
LENS_STATUSES = (
    STATUS_SKIPPED,
    STATUS_PASSED,
    STATUS_FAILED,
    STATUS_NOT_AVAILABLE,
    STATUS_REVIEWER_COULD_NOT_INSPECT,
)
VALID_STATUSES = frozenset(LENS_STATUSES)

LENS_CORRECTNESS = "correctness"
LENS_DOES_IT_RUN = "does_it_run"
LENS_TEST_ADEQUACY = "test_adequacy"
LENS_SECURITY = "security"
LENS_CODE_QUALITY = "code_quality"
LENS_REQUIREMENTS_SCOPE = "requirements_scope"
QUALITY_LENSES = (
    LENS_CORRECTNESS,
    LENS_DOES_IT_RUN,
    LENS_TEST_ADEQUACY,
    LENS_SECURITY,
    LENS_CODE_QUALITY,
    LENS_REQUIREMENTS_SCOPE,
)
JUDGMENT_LENSES = frozenset({LENS_CORRECTNESS, LENS_SECURITY, LENS_CODE_QUALITY})

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
VALID_SEVERITIES = frozenset(
    {SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW}
)
BLOCKING_SEVERITIES = frozenset({SEVERITY_CRITICAL, SEVERITY_HIGH})
FINDING_DISPOSITION_DEFECT = "defect"
FINDING_DISPOSITION_OBSERVATION = "observation"
FINDING_DISPOSITION_PROCESS_LIMIT = "process_limit"
VALID_FINDING_DISPOSITIONS = frozenset({
    FINDING_DISPOSITION_DEFECT,
    FINDING_DISPOSITION_OBSERVATION,
    FINDING_DISPOSITION_PROCESS_LIMIT,
})

WORK_KIND_GENERIC = "generic"
WORK_KIND_BUGFIX = "bugfix"
WORK_KIND_REFACTOR = "refactor"
WORK_KIND_PERFORMANCE = "performance"
WORK_KIND_SECURITY = "security"
WORK_KIND_DATA_ML = "data_ml"
WORK_KINDS = (
    WORK_KIND_GENERIC,
    WORK_KIND_BUGFIX,
    WORK_KIND_REFACTOR,
    WORK_KIND_PERFORMANCE,
    WORK_KIND_SECURITY,
    WORK_KIND_DATA_ML,
)

VALIDATION_ROLE_GENERIC = "generic"
VALIDATION_ROLE_REPRODUCTION = "reproduction"
VALIDATION_ROLE_REGRESSION = "regression"
VALIDATION_ROLE_PARITY = "parity"
VALIDATION_ROLE_BASELINE = "baseline"
VALIDATION_ROLE_DELTA = "delta"
VALIDATION_ROLE_NEGATIVE_FIXTURE = "negative_fixture"
VALIDATION_ROLE_SCHEMA = "schema"
VALIDATION_ROLE_DISTRIBUTION = "distribution"
VALIDATION_ROLES = (
    VALIDATION_ROLE_GENERIC,
    VALIDATION_ROLE_REPRODUCTION,
    VALIDATION_ROLE_REGRESSION,
    VALIDATION_ROLE_PARITY,
    VALIDATION_ROLE_BASELINE,
    VALIDATION_ROLE_DELTA,
    VALIDATION_ROLE_NEGATIVE_FIXTURE,
    VALIDATION_ROLE_SCHEMA,
    VALIDATION_ROLE_DISTRIBUTION,
)
_REQUIRED_BEHAVIORAL_ROLES: dict[str, tuple[str, ...]] = {
    WORK_KIND_BUGFIX: (
        VALIDATION_ROLE_REPRODUCTION,
        VALIDATION_ROLE_REGRESSION,
    ),
    WORK_KIND_REFACTOR: (VALIDATION_ROLE_PARITY,),
    WORK_KIND_PERFORMANCE: (
        VALIDATION_ROLE_BASELINE,
        VALIDATION_ROLE_DELTA,
    ),
    WORK_KIND_SECURITY: (VALIDATION_ROLE_NEGATIVE_FIXTURE,),
    WORK_KIND_DATA_ML: (
        VALIDATION_ROLE_SCHEMA,
        VALIDATION_ROLE_DISTRIBUTION,
    ),
}
_PERFORMANCE_METRIC_PREFIX = "AIWORKHUB_METRIC:"

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"
RISK_TIERS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL)
_RISK_RANK = {tier: index for index, tier in enumerate(RISK_TIERS)}
_RISK_SIGNAL_FLOORS = {
    "code_change": RISK_MEDIUM,
    "missing_validation": RISK_HIGH,
    "public_api": RISK_MEDIUM,
    "combined_change": RISK_MEDIUM,
    "authority_boundary": RISK_HIGH,
    "concurrency": RISK_HIGH,
    "destructive_change": RISK_HIGH,
    "schema_migration": RISK_HIGH,
    "security_sensitive": RISK_HIGH,
    "release": RISK_CRITICAL,
    # A candidate that weakens its own declared quality policy cannot thereby
    # lower its own acceptance bar: the observed weakening floors the tier high.
    "quality_policy_self_weakened": RISK_HIGH,
}

QUALITY_POLICY_SELF_WEAKENED_SIGNAL = "quality_policy_self_weakened"

_SOURCE_CODE_SUFFIXES = frozenset({
    ".bash", ".c", ".cc", ".cjs", ".cpp", ".cs", ".go", ".h", ".hpp",
    ".java", ".js", ".jsx", ".kt", ".mjs", ".php", ".ps1", ".py", ".pyi",
    ".rb", ".rs", ".sh", ".swift", ".ts", ".tsx", ".zsh",
})


def normalize_behavioral_contract(
    work_kind: Any,
    validation_commands: Iterable[Any],
    validation_roles: Iterable[Any] | None,
) -> tuple[str, list[str]]:
    """Validate the manager-declared behavioral evidence contract.

    The task title, objective, and worker prose are deliberately ignored.
    Specialized work must declare exact evidence roles aligned one-to-one
    with the already-authorized validation commands.  This lets task creation
    fail before provider spend and keeps the eventual completion verdict tied
    to deterministic command receipts rather than a model's self-assessment.
    """

    kind = str(work_kind or WORK_KIND_GENERIC).strip().lower()
    if kind not in WORK_KINDS:
        raise ValueError("invalid_work_kind")
    commands = list(validation_commands)
    raw_roles = [] if validation_roles is None else list(validation_roles)
    if not raw_roles:
        roles = [VALIDATION_ROLE_GENERIC for _ in commands]
    else:
        if len(raw_roles) != len(commands):
            raise ValueError("validation_roles_length_mismatch")
        roles = []
        for value in raw_roles:
            role = str(value or "").strip().lower()
            if role not in VALIDATION_ROLES:
                raise ValueError("invalid_validation_role")
            roles.append(role)
    required = _REQUIRED_BEHAVIORAL_ROLES.get(kind, ())
    missing = [role for role in required if role not in roles]
    if missing:
        raise ValueError("behavioral_validation_roles_missing:" + ",".join(missing))
    return kind, roles


def _validation_receipt_passed(receipt: Mapping[str, Any]) -> bool:
    return (
        receipt.get("timed_out") is not True
        and receipt.get("returncode") == 0
    )


def _merge_stdout_windows(head: str, tail: str) -> str:
    """Reconstruct one non-truncated stdout from its head and tail windows.

    When a capture is not truncated the head and tail windows describe the same
    complete output and may overlap.  Naive concatenation would then repeat the
    overlapping region -- and any metric line inside it -- producing a false
    "receipt_invalid" rejection from the exactly-one-candidate rule below.
    Merge on the longest suffix of ``head`` that is a prefix of ``tail`` so each
    line is contributed exactly once; genuinely disjoint windows still join.
    """

    if not tail or head == tail:
        return head or tail
    if not head:
        return tail
    if tail in head:
        return head
    if head in tail:
        return tail
    max_overlap = min(len(head), len(tail))
    for size in range(max_overlap, 0, -1):
        if head[-size:] == tail[:size]:
            return head + tail[size:]
    return head + "\n" + tail


def _performance_metric(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Extract one bounded machine-readable metric from validation stdout."""

    if receipt.get("stdout_truncated"):
        raise ValueError("performance_metric_stdout_truncated")
    head = str(receipt.get("stdout_head") or "")
    tail = str(receipt.get("stdout_tail") or "")
    text = _merge_stdout_windows(head, tail)
    candidates = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(_PERFORMANCE_METRIC_PREFIX):
            candidates.append(line[len(_PERFORMANCE_METRIC_PREFIX):].strip())
    if len(candidates) != 1 or len(candidates[0].encode("utf-8")) > 2048:
        raise ValueError("performance_metric_receipt_invalid")
    try:
        payload = json.loads(candidates[0])
    except json.JSONDecodeError as exc:
        raise ValueError("performance_metric_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("performance_metric_object_required")
    metric = str(payload.get("metric") or "").strip()
    unit = str(payload.get("unit") or "").strip()
    value = payload.get("value")
    if not metric or len(metric.encode("utf-8")) > 128:
        raise ValueError("performance_metric_name_invalid")
    if not unit or len(unit.encode("utf-8")) > 64:
        raise ValueError("performance_metric_unit_invalid")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("performance_metric_value_invalid")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError("performance_metric_value_invalid")
    return {**payload, "metric": metric, "unit": unit, "value": numeric}


def evaluate_behavioral_gate(
    authority: Mapping[str, Any],
    validation_receipts: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate task-type behavior from observed validation receipts.

    Generic work remains backward compatible. Specialized work fails closed
    on missing, failed, duplicated, or malformed role evidence. Performance
    work additionally requires two numeric, same-metric receipts and computes
    the direction/threshold verdict itself.
    """

    commands = list(authority.get("validation") or [])
    try:
        kind, declared_roles = normalize_behavioral_contract(
            authority.get("work_kind"),
            commands,
            authority.get("validation_roles"),
        )
    except ValueError as exc:
        return {
            "schema_id": BEHAVIORAL_GATE_SCHEMA_ID,
            "applicable": True,
            "passed": False,
            "work_kind": str(authority.get("work_kind") or WORK_KIND_GENERIC),
            "reason": str(exc),
            "required_roles": [],
            "observed_roles": [],
            "checks": [],
        }
    receipts = [dict(row) for row in validation_receipts]
    if len(receipts) != len(commands):
        applicable = kind != WORK_KIND_GENERIC
        return {
            "schema_id": BEHAVIORAL_GATE_SCHEMA_ID,
            "applicable": applicable,
            "passed": False if applicable else None,
            "work_kind": kind,
            "reason": "behavioral_validation_receipt_count_mismatch",
            "required_roles": list(_REQUIRED_BEHAVIORAL_ROLES.get(kind, ())),
            "observed_roles": [],
            "checks": [],
        }

    checks: list[dict[str, Any]] = []
    by_role: dict[str, list[dict[str, Any]]] = {}
    for index, (command, role, receipt) in enumerate(
        zip(commands, declared_roles, receipts, strict=True)
    ):
        observed_role = str(receipt.get("behavioral_role") or role).strip().lower()
        command_matches = str(receipt.get("declared_command") or receipt.get("command") or "") == str(command)
        passed = (
            observed_role == role
            and command_matches
            and _validation_receipt_passed(receipt)
        )
        check = {
            "index": index,
            "role": role,
            "command": str(command),
            "passed": passed,
            "returncode": receipt.get("returncode"),
            "timed_out": bool(receipt.get("timed_out")),
        }
        checks.append(check)
        by_role.setdefault(role, []).append(receipt)

    applicable = kind != WORK_KIND_GENERIC
    required = list(_REQUIRED_BEHAVIORAL_ROLES.get(kind, ()))
    if not applicable:
        return {
            "schema_id": BEHAVIORAL_GATE_SCHEMA_ID,
            "applicable": False,
            "passed": None,
            "work_kind": kind,
            "reason": "generic_work_kind",
            "required_roles": [],
            "observed_roles": sorted(by_role),
            "checks": checks,
        }
    missing = [role for role in required if role not in by_role]
    failed = [check["role"] for check in checks if not check["passed"]]
    if missing or failed:
        reason = (
            "behavioral_evidence_missing:" + ",".join(missing)
            if missing
            else "behavioral_evidence_failed:" + ",".join(sorted(set(failed)))
        )
        return {
            "schema_id": BEHAVIORAL_GATE_SCHEMA_ID,
            "applicable": True,
            "passed": False,
            "work_kind": kind,
            "reason": reason,
            "required_roles": required,
            "observed_roles": sorted(by_role),
            "checks": checks,
        }

    measurements: dict[str, Any] | None = None
    if kind == WORK_KIND_PERFORMANCE:
        if len(by_role[VALIDATION_ROLE_BASELINE]) != 1 or len(by_role[VALIDATION_ROLE_DELTA]) != 1:
            return {
                "schema_id": BEHAVIORAL_GATE_SCHEMA_ID,
                "applicable": True,
                "passed": False,
                "work_kind": kind,
                "reason": "performance_metric_role_duplicate",
                "required_roles": required,
                "observed_roles": sorted(by_role),
                "checks": checks,
            }
        try:
            baseline = _performance_metric(by_role[VALIDATION_ROLE_BASELINE][0])
            candidate = _performance_metric(by_role[VALIDATION_ROLE_DELTA][0])
            if baseline["metric"] != candidate["metric"] or baseline["unit"] != candidate["unit"]:
                raise ValueError("performance_metric_identity_mismatch")
            direction = str(candidate.get("direction") or "").strip().lower()
            if direction not in {"lower", "higher"}:
                raise ValueError("performance_metric_direction_invalid")
            tolerance = candidate.get("max_regression_percent", 0)
            if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
                raise ValueError("performance_metric_tolerance_invalid")
            tolerance = float(tolerance)
            if not math.isfinite(tolerance) or not 0 <= tolerance <= 100:
                raise ValueError("performance_metric_tolerance_invalid")
            baseline_value = float(baseline["value"])
            candidate_value = float(candidate["value"])
            threshold = (
                baseline_value * (1 + tolerance / 100)
                if direction == "lower"
                else baseline_value * (1 - tolerance / 100)
            )
            metric_passed = (
                candidate_value <= threshold
                if direction == "lower"
                else candidate_value >= threshold
            )
            measurements = {
                "metric": baseline["metric"],
                "unit": baseline["unit"],
                "baseline": baseline_value,
                "candidate": candidate_value,
                "direction": direction,
                "max_regression_percent": tolerance,
                "threshold": threshold,
            }
            if not metric_passed:
                raise ValueError("performance_regression_exceeds_threshold")
        except ValueError as exc:
            return {
                "schema_id": BEHAVIORAL_GATE_SCHEMA_ID,
                "applicable": True,
                "passed": False,
                "work_kind": kind,
                "reason": str(exc),
                "required_roles": required,
                "observed_roles": sorted(by_role),
                "checks": checks,
                "measurements": measurements,
            }

    result = {
        "schema_id": BEHAVIORAL_GATE_SCHEMA_ID,
        "applicable": True,
        "passed": True,
        "work_kind": kind,
        "reason": "",
        "required_roles": required,
        "observed_roles": sorted(by_role),
        "checks": checks,
    }
    if measurements is not None:
        result["measurements"] = measurements
    return result


def derive_risk_signals(
    card: Mapping[str, Any],
    changed_paths: Iterable[str],
    *,
    destructive_checks: Iterable[EvidenceCheck | Mapping[str, Any]] = (),
) -> list[str]:
    """Derive monotonic quality floors from the exact card and candidate diff.

    Manager-supplied signals may add stricter floors later, but cannot erase
    these observed signals. Detection is deliberately deterministic and
    path/card based; worker prose is never an authority source.
    """

    paths = tuple(sorted(dict.fromkeys(str(value) for value in changed_paths)))
    lowered = tuple(path.replace("\\", "/").lower() for path in paths)
    signals: set[str] = set()
    task_type = str(card.get("task_type") or "").strip().lower()
    if not task_type:
        project_context = card.get("project_context")
        if isinstance(project_context, Mapping):
            task_type = str(project_context.get("task_type") or "").strip().lower()
    if task_type == "code" or any(
        Path(path).suffix.lower() in _SOURCE_CODE_SUFFIXES for path in paths
    ):
        signals.add("code_change")
    if task_type == "code" and not (card.get("validation") or []):
        signals.add("missing_validation")
    if len(paths) > 1:
        signals.add("combined_change")

    def path_has(*markers: str) -> bool:
        return any(any(marker in path for marker in markers) for path in lowered)

    if path_has("/__init__.py", "/api/", "server.py", "runtime_adapters.py", "public/"):
        signals.add("public_api")
    if path_has("authority", "permission", "policy", "sandbox", "credential", "identity", "route"):
        signals.add("authority_boundary")
    if path_has("thread", "concurr", "callback", "queue", "process_launcher", "worker_workspace"):
        signals.add("concurrency")
    if path_has("migration", "schema", "sqlite", "database", "task_store.py", "storage.py"):
        signals.add("schema_migration")
    if path_has("security", "crypto", "secret", "token", "credential", "auth", "sandbox", "permission"):
        signals.add("security_sensitive")
    if path_has("pyproject.toml", "package.json", "/release", ".github/workflows/"):
        signals.add("release")

    for check in destructive_checks:
        payload = check.to_dict() if isinstance(check, EvidenceCheck) else dict(check)
        if payload.get("status") == STATUS_FAILED:
            signals.add("destructive_change")
            break
    return sorted(signals)
_RISK_PROFILES: dict[str, dict[str, Any]] = {
    RISK_LOW: {
        "required_reviewer_lenses": (),
        "combined_tree_required": False,
        "cross_provider_required": False,
        "explicit_human_approval_required": False,
    },
    RISK_MEDIUM: {
        "required_reviewer_lenses": (LENS_CORRECTNESS,),
        "combined_tree_required": True,
        "cross_provider_required": False,
        "explicit_human_approval_required": False,
    },
    RISK_HIGH: {
        "required_reviewer_lenses": (
            LENS_CORRECTNESS,
            LENS_SECURITY,
            LENS_CODE_QUALITY,
        ),
        "combined_tree_required": True,
        "cross_provider_required": True,
        "explicit_human_approval_required": True,
    },
    RISK_CRITICAL: {
        "required_reviewer_lenses": (
            LENS_CORRECTNESS,
            LENS_SECURITY,
            LENS_CODE_QUALITY,
        ),
        "combined_tree_required": True,
        "cross_provider_required": True,
        "explicit_human_approval_required": True,
    },
}

_CHECK_KIND_LENSES: dict[str, tuple[str, ...]] = {
    "build": (LENS_DOES_IT_RUN,),
    "test": (LENS_CORRECTNESS, LENS_DOES_IT_RUN, LENS_TEST_ADEQUACY),
    "lint": (LENS_CODE_QUALITY,),
    "typecheck": (LENS_CODE_QUALITY, LENS_CORRECTNESS),
    "static_analysis": (LENS_CODE_QUALITY,),
    "security": (LENS_SECURITY,),
    "dependency": (LENS_SECURITY,),
    "secret_scan": (LENS_SECURITY,),
    "coverage": (LENS_TEST_ADEQUACY,),
    "benchmark": (LENS_DOES_IT_RUN,),
    "memory_safety": (LENS_SECURITY, LENS_CORRECTNESS),
    "robustness": (LENS_CORRECTNESS, LENS_TEST_ADEQUACY),
    "requirements": (LENS_REQUIREMENTS_SCOPE,),
    "scope": (LENS_REQUIREMENTS_SCOPE,),
}
MAX_REVIEW_REPORTS = 12
MAX_REVIEW_FINDINGS = 50

CONFIG_RELATIVE_PATH = ".aiworkhub/quality.json"
MAX_AFFECTED_PATHS = 200
MAX_SUMMARY_CHARS = 2000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
MAX_CHECK_PATH_PATTERNS = 64
MAX_CHECK_PATH_PATTERN_BYTES = 256
MAX_NORMALIZED_REPORT_BYTES = 8 * 1024 * 1024
DECLARED_REPORT_FORMATS = frozenset(
    {
        "sarif",
        "junit_xml",
        "coverage_json",
        "benchmark_json",
        "ai_reviewer_findings",
    }
)
DESTRUCTIVE_SOURCE_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".bash"})
DESTRUCTIVE_MIN_BASELINE_LINES = 200
DESTRUCTIVE_MIN_REMOVED_LINES = 100
DESTRUCTIVE_MAX_RETAINED_RATIO = 0.50
DESTRUCTIVE_MIN_PUBLIC_SYMBOLS = 4
DESTRUCTIVE_MAX_RETAINED_SYMBOL_RATIO = 0.50

# When an evidence summary exceeds the cap it must SAY it was cut, and a
# FAILING check must keep the TAIL of its output: test runners print the
# passing lines first and the one line that says what broke last, so a
# head-only cut discards exactly the cause. The retained window for a failure
# is head+tail with a marked elision (the tail is always preserved); a
# non-failing summary keeps the head. Either way the field carries an explicit
# notice so a reader can tell short output from cut output.
SUMMARY_TRUNCATION_MARKER = "AIWORKHUB_SUMMARY_TRUNCATED"


def _bounded_summary(text: str, *, status: str = "") -> str:
    """Bound a summary to ``MAX_SUMMARY_CHARS``, always marking a real cut.

    A failing check keeps the tail so the line that says what broke survives;
    any truncated result carries :data:`SUMMARY_TRUNCATION_MARKER`, so a reader
    can never mistake cut output for short output.
    """

    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    if status == STATUS_FAILED:
        dropped = len(text) - MAX_SUMMARY_CHARS
        head_len = tail_len = 0
        note = ""
        # The total length is exactly ``MAX_SUMMARY_CHARS`` regardless of
        # convergence; the loop only refines the reported ``dropped`` count.
        for _ in range(4):
            note = (
                f"\n[{SUMMARY_TRUNCATION_MARKER}: {dropped} characters elided; "
                "tail retained]\n"
            )
            available = max(0, MAX_SUMMARY_CHARS - len(note))
            head_len = available // 4
            tail_len = available - head_len
            recomputed = len(text) - head_len - tail_len
            if recomputed == dropped:
                break
            dropped = recomputed
        tail = text[len(text) - tail_len:] if tail_len else ""
        return text[:head_len] + note + tail
    note = f"\n[{SUMMARY_TRUNCATION_MARKER}: head retained]"
    available = max(0, MAX_SUMMARY_CHARS - len(note))
    return text[:available] + note

# Repository-declared checks are intentionally broader than ordinary CI
# labels.  This lets a repository make CodeQL/Semgrep/SAST, dependency,
# secret, memory-safety and robustness checks first-class completion gates
# instead of disguising them as ``lint``.
DECLARED_CHECK_KINDS = frozenset(
    {
        "build",
        "test",
        "lint",
        "typecheck",
        "static_analysis",
        "security",
        "dependency",
        "secret_scan",
        "coverage",
        "benchmark",
        "memory_safety",
        "robustness",
    }
)

# Bounded, exact manifest/config paths used for language/tool detection.
# No globbing, no directory walks -- every entry is an exact repo-relative path.
_LANGUAGE_MANIFESTS: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"),
    "node": ("package.json",),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "c_cpp": ("CMakeLists.txt", "Makefile"),
    "dotnet": tuple(),  # detected via extension-free glob is out of scope; exact only
}

# Bounded, exact tool-config paths -- presence implies the tool is "declared"
# even if the binary itself must still be probed for availability.
_TOOL_CONFIGS: dict[str, tuple[str, ...]] = {
    "pytest": ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"),
    "ruff": ("ruff.toml", ".ruff.toml", "pyproject.toml"),
    "mypy": ("mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg"),
    "eslint": (".eslintrc.json", ".eslintrc.js", ".eslintrc", "eslint.config.js", "package.json"),
    "jest": ("jest.config.js", "jest.config.ts", "package.json"),
    "tsc": ("tsconfig.json",),
    "cargo": ("Cargo.toml",),
    "semgrep": (".semgrep.yml", ".semgrep.yaml"),
    "gitleaks": (".gitleaks.toml",),
}

# Risk-based policy metadata for optional gates. Absence of the underlying
# tool/config is reported as not_available -- never counted as a pass.
OPTIONAL_GATES: dict[str, dict[str, Any]] = {
    "semgrep": {"category": "static_analysis", "risk_tier": "medium", "blocking_by_default": False},
    "osv": {"category": "dependency_vulnerability", "risk_tier": "high", "blocking_by_default": False},
    "gitleaks": {"category": "secret_scan", "risk_tier": "high", "blocking_by_default": False},
    "codeql": {"category": "static_analysis", "risk_tier": "medium", "blocking_by_default": False},
    "mutation": {"category": "test_quality", "risk_tier": "low", "blocking_by_default": False},
    "sanitizer": {"category": "memory_safety", "risk_tier": "high", "blocking_by_default": False},
    "fuzz": {"category": "robustness", "risk_tier": "medium", "blocking_by_default": False},
}


class MalformedConfigError(ValueError):
    """Raised when repo-local .aiworkhub/quality.json fails closed."""


@dataclass(frozen=True)
class EvidenceCheck:
    """One canonical evidence-schema entry."""

    check_id: str
    kind: str
    status: str
    command: tuple[str, ...] = field(default_factory=tuple)
    executed_command: tuple[str, ...] = field(default_factory=tuple)
    command_resolution: str = "declared"
    duration_seconds: float = 0.0
    affected_paths: tuple[str, ...] = field(default_factory=tuple)
    summary: str = ""
    provenance: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise MalformedConfigError(f"invalid status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": SCHEMA_ID,
            "check_id": self.check_id,
            "kind": self.kind,
            "status": self.status,
            "command": list(self.command),
            "command_identity": " ".join(self.command) if self.command else "",
            "executed_command": list(self.executed_command),
            "executed_command_identity": (
                " ".join(self.executed_command) if self.executed_command else ""
            ),
            "command_resolution": self.command_resolution,
            "duration_seconds": round(self.duration_seconds, 6),
            "affected_paths": list(self.affected_paths[:MAX_AFFECTED_PATHS]),
            "summary": _bounded_summary(self.summary, status=self.status),
            "provenance": _bounded_summary(self.provenance),
            "error": self.error,
        }


def resolve_risk_profile(
    requested_tier: str = RISK_LOW,
    *,
    signals: Iterable[str] = (),
) -> dict[str, Any]:
    """Return one monotonic, deterministic quality-risk profile.

    A caller may request a stricter tier, but it cannot use ``requested_tier``
    to lower the floor implied by observed signals. Unknown tiers/signals fail
    closed instead of silently becoming low risk.
    """

    if requested_tier not in _RISK_RANK:
        raise MalformedConfigError(f"unknown risk tier: {requested_tier!r}")
    normalized_signals = tuple(sorted(dict.fromkeys(str(value) for value in signals)))
    unknown = sorted(set(normalized_signals) - set(_RISK_SIGNAL_FLOORS))
    if unknown:
        raise MalformedConfigError("unknown risk signal(s): " + ",".join(unknown))
    effective = requested_tier
    for signal in normalized_signals:
        floor = _RISK_SIGNAL_FLOORS[signal]
        if _RISK_RANK[floor] > _RISK_RANK[effective]:
            effective = floor
    profile = dict(_RISK_PROFILES[effective])
    profile.update(
        {
            "schema_id": VERDICT_SCHEMA_ID,
            "requested_tier": requested_tier,
            "effective_tier": effective,
            "signals": list(normalized_signals),
        }
    )
    profile["required_reviewer_lenses"] = list(profile["required_reviewer_lenses"])
    return profile


def _check_payload(check: EvidenceCheck | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(check, EvidenceCheck):
        return check.to_dict()
    if not isinstance(check, Mapping):
        raise MalformedConfigError("quality check must be an EvidenceCheck or mapping")
    check_id = check.get("check_id")
    kind = check.get("kind")
    status = check.get("status")
    if not isinstance(check_id, str) or not check_id.strip():
        raise MalformedConfigError("quality check missing non-empty check_id")
    if not isinstance(kind, str) or not kind.strip():
        raise MalformedConfigError(f"quality check {check_id!r} missing kind")
    if status not in VALID_STATUSES:
        raise MalformedConfigError(f"quality check {check_id!r} has invalid status")
    return {
        "check_id": check_id,
        "kind": kind,
        "status": status,
        "summary": _bounded_summary(str(check.get("summary") or ""), status=str(status)),
        "provenance": _bounded_summary(str(check.get("provenance") or "")),
    }


def normalize_reviewer_reports(
    reports: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize read-only reviewer evidence; never accept reviewer verdicts.

    Reports carry findings only. Any supplied ``verdict``/``passed`` field is
    ignored deliberately: final status belongs to :func:`fold_quality_verdict`.
    Malformed reports become bounded schema blockers rather than exceptions.
    """

    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    report_rows = list(reports)
    if len(report_rows) > MAX_REVIEW_REPORTS:
        errors.append("reviewer_schema:too_many_reports")
    for index, report in enumerate(report_rows[:MAX_REVIEW_REPORTS]):
        if not isinstance(report, Mapping):
            errors.append(f"reviewer_schema:{index}:not_object")
            continue
        lens = report.get("lens")
        provider = report.get("provider")
        findings = report.get("findings")
        if lens not in JUDGMENT_LENSES:
            errors.append(f"reviewer_schema:{index}:invalid_lens")
            continue
        if not isinstance(provider, str) or not provider.strip():
            errors.append(f"reviewer_schema:{index}:provider_missing")
            continue
        if report.get("read_only") is not True or report.get("can_mutate_repo") is not False:
            errors.append(f"reviewer_schema:{index}:not_read_only")
            continue
        if not isinstance(findings, list) or len(findings) > MAX_REVIEW_FINDINGS:
            errors.append(f"reviewer_schema:{index}:findings_invalid")
            continue
        clean_findings: list[dict[str, Any]] = []
        malformed = False
        for finding_index, finding in enumerate(findings):
            if not isinstance(finding, Mapping):
                errors.append(f"reviewer_schema:{index}:{finding_index}:not_object")
                malformed = True
                continue
            severity = finding.get("severity")
            finding_id = finding.get("id")
            summary = finding.get("summary")
            evidence = finding.get("evidence")
            disposition = finding.get(
                "disposition", FINDING_DISPOSITION_DEFECT
            )
            if severity not in VALID_SEVERITIES:
                errors.append(f"reviewer_schema:{index}:{finding_index}:invalid_severity")
                malformed = True
                continue
            if not isinstance(finding_id, str) or not finding_id.strip():
                errors.append(f"reviewer_schema:{index}:{finding_index}:id_missing")
                malformed = True
                continue
            if not isinstance(summary, str) or not summary.strip():
                errors.append(f"reviewer_schema:{index}:{finding_index}:summary_missing")
                malformed = True
                continue
            if not isinstance(evidence, str) or not evidence.strip():
                errors.append(f"reviewer_schema:{index}:{finding_index}:evidence_missing")
                malformed = True
                continue
            if disposition not in VALID_FINDING_DISPOSITIONS:
                errors.append(
                    f"reviewer_schema:{index}:{finding_index}:invalid_disposition"
                )
                malformed = True
                continue
            if (
                disposition != FINDING_DISPOSITION_DEFECT
                and severity != SEVERITY_LOW
            ):
                errors.append(
                    f"reviewer_schema:{index}:{finding_index}:"
                    "nondefect_severity_must_be_low"
                )
                malformed = True
                continue
            normalized_finding: dict[str, Any] = {
                "id": finding_id[:200],
                "severity": str(severity),
                "disposition": str(disposition),
                "actionable": disposition == FINDING_DISPOSITION_DEFECT,
                "summary": summary[:MAX_SUMMARY_CHARS],
                "evidence": evidence[:MAX_SUMMARY_CHARS],
            }
            structured_text = (
                "confidence",
                "evidence_level",
                "symbol",
                "claim",
                "reproduction",
                "required_validation",
            )
            invalid_structured = False
            for field_name in structured_text:
                if field_name not in finding:
                    continue
                field_value = finding.get(field_name)
                if not isinstance(field_value, str):
                    errors.append(
                        f"reviewer_schema:{index}:{finding_index}:"
                        f"{field_name}_invalid"
                    )
                    malformed = True
                    invalid_structured = True
                    break
                normalized_finding[field_name] = field_value[:MAX_SUMMARY_CHARS]
            if invalid_structured:
                continue
            if "confidence" in normalized_finding and normalized_finding[
                "confidence"
            ] not in {"low", "medium", "high"}:
                errors.append(
                    f"reviewer_schema:{index}:{finding_index}:confidence_invalid"
                )
                malformed = True
                continue
            if "evidence_level" in normalized_finding and normalized_finding[
                "evidence_level"
            ] not in {level.name.lower() for level in evidence_levels.EvidenceLevel}:
                errors.append(
                    f"reviewer_schema:{index}:{finding_index}:evidence_level_invalid"
                )
                malformed = True
                continue
            evidence_reference = finding.get("evidence_reference")
            if evidence_reference is not None:
                if not isinstance(evidence_reference, Mapping) or str(
                    evidence_reference.get("kind") or ""
                ) not in {"source", "test_target", "check"}:
                    errors.append(
                        f"reviewer_schema:{index}:{finding_index}:"
                        "evidence_reference_invalid"
                    )
                    malformed = True
                    continue
                normalized_finding["evidence_reference"] = dict(evidence_reference)
            clean_findings.append(normalized_finding)
        if malformed:
            continue
        normalized.append(
            {
                "lens": str(lens),
                "provider": provider.strip()[:200],
                "read_only": True,
                "can_mutate_repo": False,
                "findings": clean_findings,
            }
        )
    return normalized, errors


def reviewer_report_could_not_inspect(report: Mapping[str, Any]) -> bool:
    """Detect a positive signal that a reviewer could not inspect its packet.

    Two affirmative signals mark a report as blind rather than merely
    low-signal:

    * every finding carries ``disposition: process_limit`` -- the reviewer
      itself reporting it was prevented from inspecting; or
    * ``usage`` telemetry is present and records zero activity
      (``usage_observed: false`` with ``input_tokens: 0`` and
      ``output_tokens: 0``).

    Missing telemetry is unknown, never blindness: a report with no ``usage``
    field keeps satisfying its lens. This function performs no I/O and never
    requires proof of inspection.
    """

    findings = report.get("findings")
    if isinstance(findings, list) and findings:
        all_process_limit = True
        for finding in findings:
            if not isinstance(finding, Mapping):
                all_process_limit = False
                break
            if finding.get("disposition") != FINDING_DISPOSITION_PROCESS_LIMIT:
                all_process_limit = False
                break
        if all_process_limit:
            return True
    usage = report.get("usage")
    if isinstance(usage, Mapping):
        if (
            usage.get("usage_observed") is False
            and usage.get("input_tokens") == 0
            and usage.get("output_tokens") == 0
        ):
            return True
    return False


def _reviewer_model_for(report: Mapping[str, Any]) -> str:
    """Recover a reviewer's declared model from the raw report it describes.

    The rung is derived from the report ITSELF, never by searching the report
    list for the first ``(lens, provider)`` match: two reviewers for the same
    lens and provider on different models must resolve to their OWN models, not
    both to the first one found. The normalized report intentionally carries no
    ``model`` key (its shape is a fixed five-key contract), so the model is read
    straight from the raw report. Absent/non-string is a bounded ``""``, which
    resolves to the same_model rung against a same-model worker.
    """

    if not isinstance(report, Mapping):
        return ""
    model = report.get("model")
    return model.strip() if isinstance(model, str) else ""


def _best_independence_rung(
    *,
    worker_provider: str,
    worker_model: str,
    lens: str,
    reports: list[Mapping[str, Any]],
    raw_reports: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the most independent rung any sighted reviewer for a lens reached.

    Independence is the recorded ladder resolved by
    :func:`quality_review.resolve_independence_rung`, never a vendor comparison.
    Each rung is derived from the RAW report it describes -- that report's own
    provider and its own model -- so a second reviewer for the same lens and
    provider on a different model is no longer invisible, and a same-model review
    can no longer be recorded at a cross-model rung. Only reports carrying the
    validated read-only reviewer shape and a non-empty string provider are
    counted. ``reports`` is non-empty by construction, so at least one matching
    raw report exists and a rung is always returned. When a lens has several
    reports the best (lowest ``rung_index``, i.e. most independent) rung is kept.
    """

    best: dict[str, Any] | None = None
    for report in raw_reports:
        if not isinstance(report, Mapping) or report.get("lens") != lens:
            continue
        if report.get("read_only") is not True or report.get("can_mutate_repo") is not False:
            continue
        provider = report.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            continue
        record = quality_review.resolve_independence_rung(
            worker_provider=worker_provider,
            reviewer_provider=provider.strip(),
            worker_model=worker_model,
            reviewer_model=_reviewer_model_for(report),
        )
        if best is None or record["rung_index"] < best["rung_index"]:
            best = record
    # A normalized report for this lens implies a matching raw one, so the
    # loop always found at least one report and ``best`` is set.
    assert reports and best is not None
    return best


def fold_quality_verdict(
    checks: Iterable[EvidenceCheck | Mapping[str, Any]],
    *,
    risk_profile: Mapping[str, Any] | None = None,
    reviewer_reports: Iterable[Mapping[str, Any]] = (),
    combined_tree_checks: Iterable[EvidenceCheck | Mapping[str, Any]] = (),
    worker_provider: str = "",
    worker_model: str = "",
    human_approval: bool = False,
    config_error: str = "",
) -> dict[str, Any]:
    """Purely fold mechanical and reviewer evidence into one final verdict.

    The function performs no I/O and trusts no model-supplied pass/fail field.
    Blocking mechanical states, malformed reviewer evidence, missing required
    lenses and required combined-tree failures all produce ``unverified``.
    """

    profile = dict(risk_profile or resolve_risk_profile())
    effective_tier = profile.get("effective_tier")
    if effective_tier not in _RISK_RANK:
        raise MalformedConfigError("risk profile effective_tier invalid")
    required_lenses = tuple(profile.get("required_reviewer_lenses") or ())
    if any(lens not in JUDGMENT_LENSES for lens in required_lenses):
        raise MalformedConfigError("risk profile contains invalid reviewer lens")

    lens_rows: dict[str, dict[str, Any]] = {
        lens: {
            "lens": lens,
            "status": STATUS_SKIPPED,
            "evidence_ids": [],
            "finding_ids": [],
            "observation_ids": [],
        }
        for lens in QUALITY_LENSES
    }
    blockers: list[str] = []
    normalized_checks: list[dict[str, Any]] = []
    try:
        normalized_checks = [_check_payload(check) for check in checks]
    except MalformedConfigError as exc:
        blockers.append("mechanical_schema:" + str(exc)[:300])
    for check in normalized_checks:
        check_id = str(check["check_id"])
        status = str(check["status"])
        lenses = _CHECK_KIND_LENSES.get(str(check["kind"]), (LENS_CODE_QUALITY,))
        for lens in lenses:
            row = lens_rows[lens]
            row["evidence_ids"].append(check_id)
            if status in {STATUS_FAILED, STATUS_NOT_AVAILABLE}:
                row["status"] = status
            elif status == STATUS_PASSED and row["status"] == STATUS_SKIPPED:
                row["status"] = STATUS_PASSED
        if status in {STATUS_FAILED, STATUS_NOT_AVAILABLE}:
            blockers.append(check_id)

    raw_reports = list(reviewer_reports)
    normalized_reports, schema_errors = normalize_reviewer_reports(raw_reports)
    blockers.extend(schema_errors)

    # Blindness is a property of the REPORT, not of the tier. It is computed up
    # front for EVERY judgment lens so that both the skipped->passed lift below
    # and the tier-wide blindness sweep after it can honour it. Previously this
    # was consumed only inside the required-lenses loop, so a blind reviewer for
    # a lens the tier did not require (security/code_quality at medium) was
    # never checked and its report's mere existence lifted the lens to passed.
    blind_lenses: set[str] = set()
    for report in raw_reports:
        if not isinstance(report, Mapping):
            continue
        lens = report.get("lens")
        if not isinstance(lens, str) or lens not in JUDGMENT_LENSES:
            continue
        if reviewer_report_could_not_inspect(report):
            blind_lenses.add(lens)

    reports_by_lens: dict[str, list[dict[str, Any]]] = {}
    refine_required = False
    for report in normalized_reports:
        lens = str(report["lens"])
        reports_by_lens.setdefault(lens, []).append(report)
        row = lens_rows[lens]
        # A report's mere EXISTENCE no longer lifts a lens from skipped to
        # passed: only a report from a reviewer that actually observed the
        # packet does. A blind reviewer leaves the lens skipped here and is
        # marked reviewer_could_not_inspect by the sweep below.
        if row["status"] == STATUS_SKIPPED and lens not in blind_lenses:
            row["status"] = STATUS_PASSED
        for finding in report["findings"]:
            finding_id = f"reviewer:{lens}:{finding['id']}"
            if finding["actionable"] is not True:
                row["observation_ids"].append(finding_id)
                continue
            row["finding_ids"].append(finding_id)
            if finding["severity"] in BLOCKING_SEVERITIES:
                blockers.append(finding_id)
                row["status"] = STATUS_FAILED
            if lens in {LENS_CORRECTNESS, LENS_SECURITY}:
                refine_required = True
                if finding["severity"] not in BLOCKING_SEVERITIES:
                    blockers.append(f"refinement_required:{finding_id}")
                    row["status"] = STATUS_FAILED

    # A reviewer that produced no observation is never PASSED, at any tier --
    # the skipped->passed lift above already refuses to promote a blind lens, so
    # it stays skipped here. Where the tier runs an attributable review (it
    # requires at least one lens), a dispatched-but-blind reviewer for ANY
    # judgment lens -- not only the tier's required ones -- is a blocking process
    # failure: reviewer_could_not_inspect, never passed. At a tier that requires
    # no review at all, an unsolicited blind review is non-blocking noise: it is
    # simply not counted (left skipped), still never passed. The required-lenses
    # loop below re-affirms the blocker for its own lenses (de-duplicated).
    review_active = bool(required_lenses)
    if review_active:
        for lens in sorted(blind_lenses):
            lens_rows[lens]["status"] = STATUS_REVIEWER_COULD_NOT_INSPECT
            blockers.append(f"reviewer_could_not_inspect:{lens}")

    # Independence is a recorded ladder, not a vendor check. The
    # ``cross_provider_required`` flag now only marks the tiers that *require* an
    # attributable independent review; it no longer gates acceptance by comparing
    # vendors. A same-provider (or single-provider, single-model) review is
    # accepted at the ``same_model_fresh_context`` rung -- what makes it
    # independent (the anti-anchored packet, the sealed candidate, the separate
    # read-only reviewer process and the authenticated packet_sha256-bound
    # submission) holds on every rung. The fold blocks only when no rung in the
    # ladder applies: an unattributable worker (missing ``worker_provider``) is
    # not a review, so it stays a blocker.
    independence_required = bool(profile.get("cross_provider_required"))
    independence_rungs: dict[str, dict[str, Any]] = {}
    for lens in required_lenses:
        reports = reports_by_lens.get(lens, [])
        if not reports:
            blocker = f"required_reviewer_missing:{lens}"
            blockers.append(blocker)
            lens_rows[lens]["status"] = STATUS_NOT_AVAILABLE
            continue
        if lens in blind_lenses:
            blockers.append(f"reviewer_could_not_inspect:{lens}")
            lens_rows[lens]["status"] = STATUS_REVIEWER_COULD_NOT_INSPECT
            continue
        if not independence_required:
            continue
        if not worker_provider:
            # No rung on the ladder applies to an unattributable worker.
            blockers.append(f"worker_provider_missing_for_independence:{lens}")
            lens_rows[lens]["status"] = STATUS_NOT_AVAILABLE
            continue
        rung_record = _best_independence_rung(
            worker_provider=worker_provider,
            worker_model=worker_model,
            lens=lens,
            reports=reports,
            raw_reports=raw_reports,
        )
        if rung_record["rung"] not in quality_review.INDEPENDENCE_LADDER:
            # Defensive: an unresolvable rung is never accepted silently.
            blockers.append(f"independence_rung_unresolved:{lens}")
            lens_rows[lens]["status"] = STATUS_NOT_AVAILABLE
            continue
        independence_rungs[lens] = rung_record
        lens_rows[lens]["independence_rung"] = rung_record["rung"]
        lens_rows[lens]["independence"] = rung_record

    combined_rows: list[dict[str, Any]] = []
    try:
        combined_rows = [_check_payload(check) for check in combined_tree_checks]
    except MalformedConfigError as exc:
        blockers.append("combined_tree_schema:" + str(exc)[:300])
    if profile.get("combined_tree_required"):
        if not combined_rows:
            blockers.append("combined_tree_evidence_missing")
        else:
            for row in combined_rows:
                if row["status"] == STATUS_PASSED:
                    continue
                if row["status"] == STATUS_SKIPPED and _combined_tree_skip_exempt(
                    str(row.get("summary") or ""),
                    parent_tier=str(profile.get("effective_tier") or RISK_LOW),
                ):
                    continue
                blockers.append(f"combined_tree:{row['check_id']}")

    if profile.get("explicit_human_approval_required") and not human_approval:
        blockers.append("explicit_human_approval_missing")

    if config_error:
        blockers.append("quality_config_error")
    unique_blockers = list(dict.fromkeys(blockers))
    return {
        "schema_id": VERDICT_SCHEMA_ID,
        "status": "verified" if not unique_blockers else "unverified",
        "passed": not unique_blockers,
        "risk_profile": profile,
        "lenses": [lens_rows[lens] for lens in QUALITY_LENSES],
        "mechanical_checks": normalized_checks,
        "reviewer_reports": normalized_reports,
        "combined_tree_checks": combined_rows,
        "blocking_evidence": unique_blockers,
        "refine_required": bool(refine_required),
        # The achieved independence rung per required lens, so an accepted card
        # records exactly how independent each review was.
        "independence_rungs": independence_rungs,
        "config_error": config_error[:MAX_SUMMARY_CHARS],
    }


def _exact_exists(repo_root: Path, relative: str) -> bool:
    candidate = repo_root / relative
    return candidate.is_file()


def detect_languages(repo_root: Path | str) -> dict[str, bool]:
    """Bounded exact-path detection of declared languages. No globbing."""

    root = Path(repo_root)
    return {
        lang: any(_exact_exists(root, rel) for rel in manifests)
        for lang, manifests in _LANGUAGE_MANIFESTS.items()
        if manifests
    }


def detect_declared_tools(repo_root: Path | str) -> dict[str, bool]:
    """Bounded exact-path detection of declared tool configs."""

    root = Path(repo_root)
    return {
        tool: any(_exact_exists(root, rel) for rel in relatives)
        for tool, relatives in _TOOL_CONFIGS.items()
    }


def _which(executable: str) -> str | None:
    return shutil.which(executable)


def detect_installed_tools(tools: Iterable[str]) -> dict[str, bool]:
    """Zero-config probe of already-installed commands. Never installs."""

    return {tool: _which(tool) is not None for tool in tools}


def build_zero_config_profile(repo_root: Path | str) -> dict[str, Any]:
    """Deterministic, zero-config, fast profile from installed/declared state only."""

    root = Path(repo_root)
    languages = detect_languages(root)
    declared_tools = detect_declared_tools(root)
    installed_tools = detect_installed_tools(sorted(declared_tools.keys()))
    return {
        "schema_id": SCHEMA_ID,
        "repo_root": str(root),
        "languages": languages,
        "declared_tools": declared_tools,
        "installed_tools": installed_tools,
        "runnable_tools": sorted(
            tool
            for tool in declared_tools
            if declared_tools[tool] and installed_tools.get(tool, False)
        ),
    }


def load_repo_config(repo_root: Path | str) -> dict[str, Any]:
    """Load repo-local .aiworkhub/quality.json. Fail closed on malformed config."""

    root = Path(repo_root)
    path = root / CONFIG_RELATIVE_PATH
    if not path.is_file():
        return {"checks": []}
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MalformedConfigError(f"unreadable config: {exc}") from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MalformedConfigError(f"invalid JSON in {CONFIG_RELATIVE_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedConfigError(f"{CONFIG_RELATIVE_PATH} must be a JSON object")
    checks = data.get("checks", [])
    if not isinstance(checks, list):
        raise MalformedConfigError("'checks' must be a JSON array")
    for entry in checks:
        _validate_declared_check(entry)
    return data


def repo_config_status(repo_root: Path | str) -> dict[str, Any]:
    """Return explicit repository quality-policy truth without executing it."""

    root = Path(repo_root)
    path = root / CONFIG_RELATIVE_PATH
    if not path.is_file():
        return {
            "config_present": False,
            "declared_check_count": 0,
            "status": "unverified",
            "reason": "quality_config_missing",
        }
    config = load_repo_config(root)
    count = len(config.get("checks") or [])
    return {
        "config_present": True,
        "declared_check_count": count,
        "status": "configured" if count else "unverified",
        "reason": "" if count else "quality_checks_empty",
    }


def assess_quality_policy_authority(
    canonical_root: Path | str,
    candidate_root: Path | str,
) -> dict[str, Any]:
    """Detect a candidate weakening its own declared quality policy.

    A candidate cannot lower its own acceptance bar by hollowing out
    ``.aiworkhub/quality.json``. Weakening is compared by CONTENT, not by count:
    a canonical check is preserved only when the candidate still declares a
    check with the same command identity, a path scope no narrower than the
    canonical one, and a ``minimum_risk`` no higher. Replacing a command with a
    no-op, narrowing its paths, or raising its ``minimum_risk`` all weaken the
    policy even when the number of checks is unchanged. Any unpreserved
    canonical check is an observed signal that escalates the risk tier -- it
    does not block the gate outright, but the escalated tier (medium+) then
    demands combined-tree and reviewer evidence the bare candidate cannot
    fabricate.

    An unreadable/malformed config on EITHER side makes the comparison
    impossible. That is its own outcome, ``unable_to_compare`` with ``weakened``
    None -- never ``weakened`` False, which would silently disable the detector
    the one time comparison cannot be performed. A malformed candidate is still
    separately failed closed by the completion gate via ``config_error``. This
    assessment reads only declared check descriptors and never executes a
    command.
    """

    def _signatures(root: Path | str) -> list[dict[str, Any]] | None:
        """Return one content signature per declared check, or None when the
        config cannot be read/parsed at all (comparison is then impossible)."""

        try:
            config = load_repo_config(root)
        except MalformedConfigError:
            return None
        signatures: list[dict[str, Any]] = []
        for entry in config.get("checks") or []:
            command = tuple(str(part) for part in (entry.get("command") or ()))
            patterns = entry.get("paths")
            path_scope = (
                None
                if not patterns
                else frozenset(
                    str(pattern).strip().replace("\\", "/") for pattern in patterns
                )
            )
            minimum_risk = str(entry.get("minimum_risk") or RISK_LOW)
            signatures.append(
                {
                    "command": command,
                    "path_scope": path_scope,
                    "minimum_risk": minimum_risk,
                }
            )
        return signatures

    canonical_sigs = _signatures(canonical_root)
    candidate_sigs = _signatures(candidate_root)
    canonical_ok = canonical_sigs is not None
    candidate_ok = candidate_sigs is not None
    canonical_count = len(canonical_sigs) if canonical_sigs is not None else 0
    candidate_count = len(candidate_sigs) if candidate_sigs is not None else 0

    if canonical_sigs is None or candidate_sigs is None:
        # The one state in which weakening cannot be ruled out is reported as
        # its own outcome, never as weakened=False.
        return {
            "schema_id": "aiworkhub.quality_policy_authority.v1",
            "weakened": None,
            "outcome": "unable_to_compare",
            "action": "none",
            "reason": (
                "quality_policy_unable_to_compare:"
                f"canonical_readable={str(canonical_ok).lower()},"
                f"candidate_readable={str(candidate_ok).lower()}"
            ),
            "escalation_signal": "",
            "blocks_gate": False,
            "canonical_declared_checks": canonical_count,
            "candidate_declared_checks": candidate_count,
            "canonical_config_readable": canonical_ok,
            "candidate_config_readable": candidate_ok,
        }

    def _scope_preserved(
        canonical_scope: frozenset[str] | None,
        candidate_scope: frozenset[str] | None,
    ) -> bool:
        # None means "runs on every path" -- the broadest possible scope. A
        # candidate preserves scope only when it is at least as broad: an
        # all-paths canonical check demands an all-paths candidate, and a
        # scoped canonical check demands a candidate whose scope is a superset.
        if canonical_scope is None:
            return candidate_scope is None
        if candidate_scope is None:
            return True
        return canonical_scope <= candidate_scope

    def _preserved(canonical_sig: dict[str, Any]) -> bool:
        for candidate_sig in candidate_sigs:
            if candidate_sig["command"] != canonical_sig["command"]:
                continue
            if not _scope_preserved(
                canonical_sig["path_scope"], candidate_sig["path_scope"]
            ):
                continue
            if (
                _RISK_RANK[candidate_sig["minimum_risk"]]
                > _RISK_RANK[canonical_sig["minimum_risk"]]
            ):
                continue
            return True
        return False

    weakened_checks = [sig for sig in canonical_sigs if not _preserved(sig)]
    weakened = bool(weakened_checks)
    if weakened:
        action = "escalate_risk_tier"
        reason = (
            "quality_policy_self_weakened:"
            f"unpreserved_checks={len(weakened_checks)}/{canonical_count}:"
            "commands="
            + ",".join(
                (" ".join(sig["command"]) or "<empty>")
                for sig in weakened_checks[:8]
            )
        )
        signal = QUALITY_POLICY_SELF_WEAKENED_SIGNAL
    else:
        action = "none"
        reason = ""
        signal = ""
    return {
        "schema_id": "aiworkhub.quality_policy_authority.v1",
        "weakened": weakened,
        "outcome": "weakened" if weakened else "preserved",
        "action": action,
        "reason": reason,
        "escalation_signal": signal,
        "blocks_gate": False,
        "canonical_declared_checks": canonical_count,
        "candidate_declared_checks": candidate_count,
        "canonical_config_readable": canonical_ok,
        "candidate_config_readable": candidate_ok,
    }


def _validate_declared_check(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise MalformedConfigError("each declared check must be a JSON object")
    check_id = entry.get("id")
    if not isinstance(check_id, str) or not check_id.strip():
        raise MalformedConfigError("declared check missing non-empty 'id'")
    kind = entry.get("kind")
    if kind not in DECLARED_CHECK_KINDS:
        raise MalformedConfigError(f"declared check {check_id!r} has invalid 'kind': {kind!r}")
    command = entry.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise MalformedConfigError(
            f"declared check {check_id!r} 'command' must be a non-empty array of strings (shell=False only)"
        )
    paths = entry.get("paths")
    if paths is not None:
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) > MAX_CHECK_PATH_PATTERNS
            or not all(isinstance(pattern, str) and pattern.strip() for pattern in paths)
        ):
            raise MalformedConfigError(
                f"declared check {check_id!r} 'paths' must be a bounded non-empty array of patterns"
            )
        for pattern in paths:
            normalized = pattern.strip().replace("\\", "/")
            if (
                len(normalized.encode("utf-8")) > MAX_CHECK_PATH_PATTERN_BYTES
                or normalized.startswith("/")
                or "\x00" in normalized
                or ".." in normalized.split("/")
            ):
                raise MalformedConfigError(
                    f"declared check {check_id!r} has unsafe path pattern"
                )
    minimum_risk = entry.get("minimum_risk")
    if minimum_risk is not None and minimum_risk not in _RISK_RANK:
        raise MalformedConfigError(
            f"declared check {check_id!r} has invalid 'minimum_risk': {minimum_risk!r}"
        )
    report = entry.get("report")
    if report is not None:
        if not isinstance(report, Mapping):
            raise MalformedConfigError(
                f"declared check {check_id!r} 'report' must be an object"
            )
        report_format = report.get("format")
        if report_format not in DECLARED_REPORT_FORMATS:
            raise MalformedConfigError(
                f"declared check {check_id!r} has invalid report format: {report_format!r}"
            )
        report_path = report.get("path")
        if not isinstance(report_path, str) or not report_path.strip():
            raise MalformedConfigError(
                f"declared check {check_id!r} report path must be non-empty"
            )
        normalized_report_path = report_path.strip().replace("\\", "/")
        if (
            len(normalized_report_path.encode("utf-8")) > MAX_CHECK_PATH_PATTERN_BYTES
            or normalized_report_path.startswith("/")
            or "\x00" in normalized_report_path
            or ".." in normalized_report_path.split("/")
            or any(char in normalized_report_path for char in "*?[]")
        ):
            raise MalformedConfigError(
                f"declared check {check_id!r} has unsafe report path"
            )
        min_percent = report.get("min_percent")
        if min_percent is not None:
            if report_format != "coverage_json" or isinstance(min_percent, bool):
                raise MalformedConfigError(
                    f"declared check {check_id!r} min_percent requires coverage_json"
                )
            try:
                numeric_minimum = float(min_percent)
            except (TypeError, ValueError) as exc:
                raise MalformedConfigError(
                    f"declared check {check_id!r} min_percent must be numeric"
                ) from exc
            if not 0.0 <= numeric_minimum <= 100.0:
                raise MalformedConfigError(
                    f"declared check {check_id!r} min_percent out of range"
                )


# The applicability skip reasons a combined-tree check may carry without
# blocking the parent fold, matched by explicit reason KIND -- the token before
# the first ':'. ``risk_below_minimum`` interpolates the tiers into its reason,
# so a fixed-string equality recognised only ``changed_paths_not_applicable``
# and silently missed its sibling: a check with ``minimum_risk`` critical on a
# high-tier card was then skipped for a reason the exemption could not match and
# became a permanent combined-tree blocker no tier could clear. Comparing the
# kind exactly -- never a prefix or a substring of the whole formatted string --
# also keeps a future reason that merely shares a prefix from being mistaken for
# an exempt one.
APPLICABILITY_SKIP_CHANGED_PATHS = "changed_paths_not_applicable"
APPLICABILITY_SKIP_RISK_BELOW_MINIMUM = "risk_below_minimum"
COMBINED_TREE_EXEMPT_SKIP_KINDS = frozenset(
    {APPLICABILITY_SKIP_CHANGED_PATHS, APPLICABILITY_SKIP_RISK_BELOW_MINIMUM}
)


def _applicability_skip_kind(summary: str) -> str:
    """Return the explicit reason KIND of a declared-check applicability skip.

    The kind is the token before the first ':' -- ``risk_below_minimum`` for the
    tier-interpolated reason and the whole string for
    ``changed_paths_not_applicable``. Callers compare it against the exact
    :data:`COMBINED_TREE_EXEMPT_SKIP_KINDS` set, so a formatted-value tail can
    never make an exempt reason look non-exempt (or a non-exempt reason exempt).
    """

    return summary.split(":", 1)[0]


def _combined_tree_skip_exempt(summary: str, *, parent_tier: str) -> bool:
    """Whether a skipped combined-tree check may pass without blocking the fold.

    ``changed_paths_not_applicable`` is unconditionally exempt: the check did not
    apply to this change at any tier.

    ``risk_below_minimum`` is exempt ONLY when the PARENT fold's own tier is also
    below the check's minimum. The distinction is the whole point:

    * parent high, check minimum critical -- no sub-gate tier could ever have run
      it, so blocking is unclearable and the check is exempt.
    * parent medium, check minimum medium, skipped because the sub-gate ran at
      low -- the check SHOULD have run and the sub-gate was mis-scoped. Blocking
      is the guarantee that a combined tree carries the parent's tier, and
      exempting it here would silently discard that guarantee.

    A malformed or unparseable reason is NOT exempt: an applicability claim that
    cannot be read is not an applicability claim.
    """

    kind = _applicability_skip_kind(summary)
    if kind == APPLICABILITY_SKIP_CHANGED_PATHS:
        return True
    if kind != APPLICABILITY_SKIP_RISK_BELOW_MINIMUM:
        return False
    _, _, tiers = summary.partition(":")
    _, _, minimum_risk = tiers.partition("<")
    minimum_risk = minimum_risk.strip()
    if minimum_risk not in _RISK_RANK or parent_tier not in _RISK_RANK:
        return False
    return _RISK_RANK[parent_tier] < _RISK_RANK[minimum_risk]


def _declared_check_applicability(
    entry: Mapping[str, Any],
    *,
    changed_paths: tuple[str, ...],
    effective_risk_tier: str,
) -> tuple[bool, str]:
    """Return deterministic applicability for one declared check.

    An absent ``paths`` selector preserves the historical always-run policy.
    Empty changed-path evidence also runs conservatively; filtering is only
    allowed when the caller supplied an exact task delta.
    """

    minimum_risk = str(entry.get("minimum_risk") or RISK_LOW)
    if _RISK_RANK[effective_risk_tier] < _RISK_RANK[minimum_risk]:
        return (
            False,
            f"{APPLICABILITY_SKIP_RISK_BELOW_MINIMUM}:{effective_risk_tier}<{minimum_risk}",
        )
    patterns = tuple(
        str(pattern).strip().replace("\\", "/")
        for pattern in (entry.get("paths") or ())
    )
    if not patterns or not changed_paths:
        return True, ""
    for relative in changed_paths:
        normalized = relative.replace("\\", "/")
        if any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns):
            return True, ""
    return False, APPLICABILITY_SKIP_CHANGED_PATHS


def _windows_noncanonical_path_alias(value: str) -> bool:
    """Return whether a raw Windows path contains a dot/space alias."""

    raw_path = PureWindowsPath(value)
    components = list(raw_path.parts)
    if raw_path.anchor and components and components[0] == raw_path.anchor:
        components = components[1:]
    drive = raw_path.drive.replace("/", "\\")
    if drive.startswith("\\\\"):
        # PureWindowsPath stores a UNC server/share pair inside the anchor.
        # Validate those authority components instead of treating the whole
        # anchor like a drive-letter root.  Namespace markers remain harmless
        # components, while server/share aliases still fail closed.
        components = [
            part for part in drive.lstrip("\\").split("\\") if part
        ] + components
    return any(part != part.rstrip(" .") for part in components)


def _windows_path_component_chain_error(path: Path) -> str:
    """Reject symlink, junction, or other reparse components in a raw path."""

    components = list(path.parts)
    if path.anchor and components and components[0] == path.anchor:
        current = Path(path.anchor)
        components = components[1:]
    else:
        current = Path()
    for component in components:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # Descendants cannot exist through a missing ancestor.  Strict
            # resolution below will produce the canonical not-found result.
            break
        except OSError:
            return "windows_path_component_inspection_failed"
        if stat.S_ISLNK(metadata.st_mode):
            return "windows_path_reparse_forbidden"
        try:
            is_junction = getattr(current, "is_junction", None)
            if callable(is_junction) and is_junction():
                return "windows_path_reparse_forbidden"
        except OSError:
            return "windows_path_component_inspection_failed"
        file_attributes = getattr(metadata, "st_file_attributes", 0)
        if file_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return "windows_path_reparse_forbidden"
    return ""


def _windows_native_command_argv(argv: list[str]) -> tuple[list[str] | None, str]:
    """Resolve a Windows command to native executable argv or fail closed.

    Windows may ask ``cmd.exe`` to interpret ``.cmd``/``.bat`` files even
    when ``subprocess.run(..., shell=False)`` is used.  That violates this
    module's exact-argv contract because metacharacters in later arguments
    regain shell meaning.  Never return a batch file as argv[0].

    The one supported wrapper is npm's standard ``npm.cmd`` installation:
    execute its adjacent native ``node.exe`` with the exact, verified
    ``node_modules/npm/bin/npm-cli.js`` entry point.  Other batch wrappers and
    incomplete/non-standard npm layouts remain unavailable.
    """

    resolved = _which(argv[0])
    if resolved is None:
        return None, "command_not_found"
    raw_path = PureWindowsPath(resolved)
    if _windows_noncanonical_path_alias(resolved):
        return None, "windows_noncanonical_executable_alias"
    raw_suffix = raw_path.suffix.casefold()
    native_suffixes = {".exe", ".com"}
    batch_suffixes = {".cmd", ".bat"}
    if raw_suffix not in native_suffixes | batch_suffixes:
        return None, "windows_non_native_executable_forbidden"
    unresolved = Path(resolved)
    component_error = _windows_path_component_chain_error(unresolved)
    if component_error:
        return None, component_error
    try:
        executable = unresolved.resolve(strict=True)
    except FileNotFoundError:
        return None, "command_not_found"
    except (OSError, RuntimeError):
        return None, "windows_executable_resolution_failed"
    executable_suffix = executable.suffix.casefold()
    if raw_suffix in native_suffixes:
        if executable_suffix not in native_suffixes or not executable.is_file():
            return None, "windows_native_executable_unavailable"
        return [str(executable), *argv[1:]], ""
    if executable_suffix not in batch_suffixes:
        return None, "windows_batch_wrapper_forbidden"
    if (
        PureWindowsPath(argv[0]).name.casefold() not in {"npm", "npm.cmd"}
        or executable.name.casefold() != "npm.cmd"
    ):
        return None, "windows_batch_wrapper_forbidden"
    if not executable.is_file():
        return None, "npm_native_entrypoint_unavailable"
    try:
        install_root = executable.parent.resolve(strict=True)
        node = install_root / "node.exe"
        npm_cli = install_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
        for entrypoint in (node, npm_cli):
            component_error = _windows_path_component_chain_error(entrypoint)
            if component_error:
                return None, component_error
        if (
            node.is_symlink()
            or npm_cli.is_symlink()
            or not node.is_file()
            or not npm_cli.is_file()
        ):
            return None, "npm_native_entrypoint_unavailable"
        node = node.resolve(strict=True)
        npm_cli = npm_cli.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "npm_native_entrypoint_unavailable"
    if node.parent != install_root or npm_cli != (
        install_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    ):
        return None, "npm_native_entrypoint_unavailable"
    return [str(node), str(npm_cli), *argv[1:]], ""


def _run_command_array(
    command: Iterable[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    execution_receipt: dict[str, Any] | None = None,
) -> tuple[str, str, str, float]:
    """Run one exact argv array, shell=False. Returns (status, stdout, stderr, duration)."""

    declared_argv = list(command)
    argv = [sys.executable if part == "{python}" else part for part in declared_argv]
    resolution = "declared"
    if (
        len(declared_argv) >= 3
        and declared_argv[0] == "{python}"
        and declared_argv[1] == "-m"
        and declared_argv[2] in {"mypy", "ruff"}
        and importlib.util.find_spec(declared_argv[2]) is None
    ):
        # The MCP runtime may intentionally use a minimal system interpreter
        # while repository quality tools are installed as trusted PATH
        # entrypoints. Preserve the declared semantic command, but execute the
        # exact installed CLI when that interpreter cannot import the module.
        # The allowlist prevents an arbitrary ``-m`` name from becoming a PATH
        # lookup, and Windows still passes through the native-executable guard.
        module_entrypoint = _which(declared_argv[2])
        if module_entrypoint is not None:
            argv = [module_entrypoint, *declared_argv[3:]]
            resolution = "python_module_path_entrypoint"
    if os.name == "nt" and argv and argv[0] in {"python", "python3"}:
        # Repository quality manifests are shared across hosts. Windows does
        # not consistently install the POSIX ``python3`` launcher alias, so
        # bind that interpreter token to the already-running trusted runtime.
        argv[0] = sys.executable
    start = time.monotonic()
    if os.name == "nt" and argv:
        native_argv, resolution_error = _windows_native_command_argv(argv)
        if native_argv is None:
            return (
                STATUS_NOT_AVAILABLE,
                "",
                resolution_error,
                time.monotonic() - start,
            )
        argv = native_argv
    if execution_receipt is not None:
        execution_receipt.update(
            {
                "declared_command": declared_argv,
                "executed_command": list(argv),
                "command_resolution": resolution,
            }
        )
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return STATUS_NOT_AVAILABLE, "", "command_not_found", time.monotonic() - start
    except subprocess.TimeoutExpired:
        return STATUS_FAILED, "", "timeout", time.monotonic() - start
    except OSError:
        return STATUS_NOT_AVAILABLE, "", "process_start_failed", time.monotonic() - start
    duration = time.monotonic() - start
    status = STATUS_PASSED if completed.returncode == 0 else STATUS_FAILED
    return status, completed.stdout, completed.stderr, duration


def declared_check_descriptors(
    repo_root: Path | str,
    *,
    changed_paths: Iterable[str] | None = None,
) -> list[EvidenceCheck]:
    """Validated, read-only descriptors for repo-declared checks.

    Loads and validates .aiworkhub/quality.json (fail-closed on malformed
    config, same as run_declared_checks) but never executes a command --
    every descriptor is reported as ``skipped`` with the declared argv
    preserved for display. Only aiworkhub_quality_run_checks/
    run_declared_checks may actually invoke subprocess.run.
    """

    root = Path(repo_root)
    config = load_repo_config(root)
    affected = tuple(sorted(str(p) for p in (changed_paths or ())))
    results: list[EvidenceCheck] = []
    for entry in config.get("checks", []):
        command = tuple(str(part) for part in entry["command"])
        results.append(
            EvidenceCheck(
                check_id=str(entry["id"]),
                kind=str(entry["kind"]),
                status=STATUS_SKIPPED,
                command=command,
                affected_paths=affected,
                summary="declared, not executed (read-only evidence packet)",
                provenance=f"repo_config:{CONFIG_RELATIVE_PATH}:declared_only",
            )
        )
    return results


def run_declared_checks(
    repo_root: Path | str,
    *,
    changed_paths: Iterable[str] | None = None,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    effective_risk_tier: str = RISK_LOW,
) -> list[EvidenceCheck]:
    """Execute repo-local declared quality/security commands for one task delta.

    Fails closed (raises MalformedConfigError) on a malformed config instead
    of silently skipping or partially running it. Commands only ever run as
    argv arrays -- never a shell string.
    """

    root = Path(repo_root)
    config = load_repo_config(root)
    if effective_risk_tier not in _RISK_RANK:
        raise MalformedConfigError(
            f"unknown effective risk tier: {effective_risk_tier!r}"
        )
    affected = tuple(sorted(str(p) for p in (changed_paths or ())))
    results: list[EvidenceCheck] = []
    for entry in config.get("checks", []):
        check_id = str(entry["id"])
        kind = str(entry["kind"])
        command = tuple(str(part) for part in entry["command"])
        applies, skip_reason = _declared_check_applicability(
            entry,
            changed_paths=affected,
            effective_risk_tier=effective_risk_tier,
        )
        if not applies:
            results.append(EvidenceCheck(
                check_id=check_id,
                kind=kind,
                status=STATUS_SKIPPED,
                command=command,
                affected_paths=affected,
                summary=skip_reason,
                provenance=f"repo_config:{CONFIG_RELATIVE_PATH}:applicability",
            ))
            continue
        execution_receipt: dict[str, Any] = {}
        status, stdout, stderr, duration = _run_command_array(
            command,
            cwd=root,
            timeout_seconds=timeout_seconds,
            execution_receipt=execution_receipt,
        )
        summary = (stdout or "") + (("\n" + stderr) if stderr else "")
        results.append(
            EvidenceCheck(
                check_id=check_id,
                kind=kind,
                status=status,
                command=command,
                executed_command=tuple(execution_receipt.get("executed_command") or ()),
                command_resolution=str(
                    execution_receipt.get("command_resolution") or "declared"
                ),
                duration_seconds=duration,
                affected_paths=affected,
                summary=summary.strip(),
                provenance=f"repo_config:{CONFIG_RELATIVE_PATH}",
                error="" if status != STATUS_NOT_AVAILABLE else stderr,
            )
        )
        if isinstance(entry.get("report"), Mapping):
            results.append(
                _adapt_declared_report(root, entry, affected_paths=affected)
            )
    return results


def _adapt_declared_report(
    repo_root: Path,
    entry: Mapping[str, Any],
    *,
    affected_paths: tuple[str, ...],
) -> EvidenceCheck:
    """Normalize one bounded report produced by a declared exact command."""

    check_id = f"{entry['id']}:report"
    declared_kind = str(entry["kind"])
    report = entry["report"]
    assert isinstance(report, Mapping)
    relative = str(report["path"]).strip().replace("\\", "/")
    report_format = str(report["format"])
    raw_candidate = repo_root / relative
    candidate = raw_candidate.resolve(strict=False)
    if candidate != repo_root and repo_root not in candidate.parents:
        return EvidenceCheck(
            check_id=check_id,
            kind=declared_kind,
            status=STATUS_FAILED,
            affected_paths=affected_paths,
            summary="normalized report path escapes repository",
            provenance=f"adapter:{report_format}:{relative}",
            error="report_path_escape",
        )
    try:
        if raw_candidate.is_symlink():
            return EvidenceCheck(
                check_id=check_id,
                kind=declared_kind,
                status=STATUS_FAILED,
                affected_paths=affected_paths,
                summary=f"normalized report must not be a symlink: {relative}",
                provenance=f"adapter:{report_format}:{relative}",
                error="report_symlink_forbidden",
            )
        if not candidate.is_file():
            raise FileNotFoundError(relative)
        size = candidate.stat().st_size
        if size > MAX_NORMALIZED_REPORT_BYTES:
            return EvidenceCheck(
                check_id=check_id,
                kind=declared_kind,
                status=STATUS_FAILED,
                affected_paths=affected_paths,
                summary=(
                    f"normalized report too large: {size}>"
                    f"{MAX_NORMALIZED_REPORT_BYTES}"
                ),
                provenance=f"adapter:{report_format}:{relative}",
                error="report_too_large",
            )
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return EvidenceCheck(
            check_id=check_id,
            kind=declared_kind,
            status=STATUS_NOT_AVAILABLE,
            affected_paths=affected_paths,
            summary=f"normalized report unavailable: {relative}",
            provenance=f"adapter:{report_format}:{relative}",
            error=type(exc).__name__,
        )

    try:
        if report_format == "junit_xml":
            adapted = adapt_junit_xml(check_id, text)
        else:
            document = json.loads(text)
            if report_format == "sarif":
                adapted = adapt_sarif(check_id, document)
            elif report_format == "coverage_json":
                adapted = adapt_coverage_summary(
                    check_id,
                    document,
                    min_percent=float(report.get("min_percent") or 0.0),
                )
            elif report_format == "benchmark_json":
                adapted = adapt_benchmark_json(check_id, document)
            else:
                findings = (
                    document.get("findings")
                    if isinstance(document, Mapping)
                    else document
                )
                if not isinstance(findings, list):
                    raise ValueError("AI reviewer report must contain a findings array")
                adapted = adapt_ai_reviewer_findings(check_id, findings)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return EvidenceCheck(
            check_id=check_id,
            kind=declared_kind,
            status=STATUS_FAILED,
            affected_paths=affected_paths,
            summary=f"malformed {report_format} report: {exc}",
            provenance=f"adapter:{report_format}:{relative}",
            error=str(exc),
        )
    return EvidenceCheck(
        check_id=adapted.check_id,
        kind=adapted.kind,
        status=adapted.status,
        command=tuple(str(part) for part in entry["command"]),
        duration_seconds=adapted.duration_seconds,
        affected_paths=adapted.affected_paths or affected_paths,
        summary=adapted.summary,
        provenance=f"{adapted.provenance}:{relative}",
        error=adapted.error,
    )


def run_builtin_static_checks(
    repo_root: Path | str,
    *,
    changed_paths: Iterable[str] | None = None,
    timeout_seconds: int = 60,
) -> list[EvidenceCheck]:
    """Always-available, diff-scoped syntax/static checks.

    This is the deterministic floor beneath optional Semgrep/CodeQL gates.
    It never walks the repository and never downloads a tool or rule pack.
    Only exact changed paths are inspected, capped by ``MAX_AFFECTED_PATHS``.
    """
    root = Path(repo_root).resolve()
    paths = tuple(sorted(dict.fromkeys(str(p) for p in (changed_paths or ()))))[:MAX_AFFECTED_PATHS]
    checks: list[EvidenceCheck] = []
    for relative in paths:
        candidate = (root / relative).resolve(strict=False)
        if candidate != root and root not in candidate.parents:
            checks.append(EvidenceCheck(
                check_id=f"builtin:path:{relative}", kind="static_analysis", status=STATUS_FAILED,
                affected_paths=(relative,), summary="changed path escapes repository",
                provenance="builtin:exact_changed_path", error="path_escape",
            ))
            continue
        if not candidate.exists():
            # Deleted paths are legitimate changes and have no new source to scan.
            continue
        if candidate.is_symlink() or not candidate.is_file():
            checks.append(EvidenceCheck(
                check_id=f"builtin:path:{relative}", kind="static_analysis", status=STATUS_FAILED,
                affected_paths=(relative,), summary="changed source is not a regular file",
                provenance="builtin:exact_changed_path", error="non_regular_source",
            ))
            continue
        suffix = candidate.suffix.lower()
        started = time.monotonic()
        status = STATUS_PASSED
        error = ""
        summary = "syntax valid"
        command: tuple[str, ...] = ()
        try:
            if suffix == ".py":
                ast.parse(candidate.read_text(encoding="utf-8"), filename=relative)
            elif suffix in {".js", ".cjs", ".mjs"}:
                command = ("node", "--check", relative)
                status, stdout, stderr, _duration = _run_command_array(
                    command, cwd=root, timeout_seconds=timeout_seconds
                )
                error = stderr if status != STATUS_PASSED else ""
                summary = (stdout or stderr or "syntax valid").strip()
            elif suffix in {".sh", ".bash"}:
                command = ("bash", "-n", relative)
                status, stdout, stderr, _duration = _run_command_array(
                    command, cwd=root, timeout_seconds=timeout_seconds
                )
                error = stderr if status != STATUS_PASSED else ""
                summary = (stdout or stderr or "syntax valid").strip()
            else:
                continue
        except (OSError, UnicodeError, SyntaxError, json.JSONDecodeError) as exc:
            status = STATUS_FAILED
            error = str(exc)
            summary = f"syntax/static parse failed: {exc}"
        checks.append(EvidenceCheck(
            check_id=f"builtin:syntax:{relative}",
            kind="static_analysis",
            status=status,
            command=command,
            duration_seconds=time.monotonic() - started,
            affected_paths=(relative,),
            summary=summary,
            provenance="builtin:diff_scoped_syntax",
            error=error,
        ))
    if not checks:
        checks.append(EvidenceCheck(
            check_id="builtin:syntax:no_supported_changed_paths",
            kind="static_analysis",
            status=STATUS_PASSED,
            affected_paths=paths,
            summary="no changed Python/JavaScript/shell source required syntax parsing",
            provenance="builtin:diff_scoped_syntax",
        ))
    bug_report = known_bug_scanner.scan_changed_paths(root, paths)
    checks.append(EvidenceCheck(
        check_id="builtin:known_bug_patterns",
        kind="security",
        status=STATUS_PASSED if bug_report["passed"] else STATUS_FAILED,
        affected_paths=paths,
        summary=json.dumps({
            "errors": bug_report["errors"], "warnings": bug_report["warnings"],
            "findings": bug_report["findings"][:20], "truncated": bug_report["truncated"],
            "evidence_summary": bug_report["evidence_summary"],
            "dedupe_summary": bug_report["dedupe_summary"],
            "source_revision_sha256": bug_report["source_revision_sha256"],
            "source_revision_scope": bug_report["source_revision_scope"],
        }, sort_keys=True)[:MAX_SUMMARY_CHARS],
        provenance="builtin:diff_scoped_known_bug_registry.v1",
        error="" if bug_report["passed"] else "high_confidence_known_bug_pattern",
    ))
    eval_report = eval_artifact_gate.evaluate(root, changed_paths=list(paths))
    if eval_report["configured"]:
        eval_status = (
            STATUS_SKIPPED
            if int(eval_report.get("evaluated_count") or 0) == 0
            else STATUS_PASSED if eval_report["passed"] else STATUS_FAILED
        )
        checks.append(EvidenceCheck(
            check_id="builtin:eval_artifact_truth",
            kind="requirements",
            status=eval_status,
            affected_paths=tuple(
                sorted({
                    str(row.get(key) or "")
                    for row in eval_report.get("artifacts") or []
                    for key in ("summary_path", "rows_path")
                    if row.get(key)
                })
            ),
            summary=(
                "changed_paths_not_applicable"
                if eval_status == STATUS_SKIPPED
                else json.dumps(eval_report, sort_keys=True)[:MAX_SUMMARY_CHARS]
            ),
            provenance="builtin:registry_driven_eval_artifact_gate.v1",
            error="" if eval_status != STATUS_FAILED else "eval_artifact_evidence_diverged",
        ))
    return checks


def _reachability_record(
    reachability_inputs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Report candidate additions no recognised entry point can reach.

    Runs as part of :func:`run_completion_quality_gate` so its findings appear
    in the evidence a manager reads (NF-2026-00304), rather than only when
    someone calls :func:`quality_review.analyze_candidate_reachability` on
    purpose.  Reachability is a non-blocking observation: an unreachable
    addition is NAMED for a manager to dispose of, never a gate failure, and it
    never affects ``passed``.  When no Source Graph reachability inputs are
    available the record says so explicitly (``evaluated`` False) rather than
    silently reading as "all reachable".
    """

    if not reachability_inputs:
        return {
            "schema_id": quality_review.REACHABILITY_SCHEMA_ID,
            "evaluated": False,
            "reason": "reachability_inputs_unavailable",
            "all_reachable": None,
            "blocking": False,
            "gate_action": quality_review.REACHABILITY_GATE_ACTION,
            "entry_point_count": 0,
            "changed_symbol_count": 0,
            "unreachable_additions": [],
            "findings": [],
        }
    record = quality_review.analyze_candidate_reachability(
        changed_symbols=reachability_inputs.get("changed_symbols") or (),
        call_edges=reachability_inputs.get("call_edges") or (),
        reference_edges=reachability_inputs.get("reference_edges") or (),
        entry_points=reachability_inputs.get("entry_points") or (),
    )
    record["evaluated"] = True
    return record


def run_completion_quality_gate(
    repo_root: Path | str,
    *,
    changed_paths: Iterable[str] | None = None,
    requested_risk_tier: str = RISK_LOW,
    risk_signals: Iterable[str] = (),
    reviewer_reports: Iterable[Mapping[str, Any]] = (),
    combined_tree_checks: Iterable[EvidenceCheck | Mapping[str, Any]] = (),
    worker_provider: str = "",
    human_approval: bool = False,
    reachability_inputs: Mapping[str, Any] | None = None,
    combined_tree_scope: bool = False,
) -> dict[str, Any]:
    """Execute the mandatory review-quality floor for one task delta.

    Built-in diff syntax checks always run. Every repo-declared check is
    mandatory *when it applies to this delta and tier*: an applicable check
    that runs and reports ``failed`` or ``not_available`` blocks. A check that
    does not apply is skipped with an explicit, recorded reason
    (``risk_below_minimum`` or ``changed_paths_not_applicable``) -- it is never
    silently dropped, and a skipped applicable check is never counted as a
    pass. ``verification_scope`` is ``repository_policy`` only when at least one
    declared check actually EXECUTED; an all-skipped run reports
    ``builtin_and_task_contract_only`` rather than claiming the repository's
    policy governed a verdict none of its checks ran. Optional
    CodeQL/Semgrep/etc. availability is reported truthfully but does not pass
    or fail the task unless the repository declares its exact command in
    ``.aiworkhub/quality.json``.
    """
    root = Path(repo_root)
    affected = tuple(sorted(str(p) for p in (changed_paths or ())))
    try:
        config_status = repo_config_status(root)
    except MalformedConfigError as exc:
        config_status = {
            "config_present": True,
            "declared_check_count": 0,
            "status": "unverified",
            "reason": str(exc)[:MAX_SUMMARY_CHARS],
        }
    try:
        risk_profile = resolve_risk_profile(requested_risk_tier, signals=risk_signals)
        if combined_tree_scope:
            # The combined-tree validation must carry the parent fold's exact
            # risk tier and signals so a declared check with ``minimum_risk``
            # medium runs here too (otherwise it is skipped as
            # ``risk_below_minimum`` and re-blocks in the parent as a permanent
            # ``combined_tree`` blocker). But the sub-gate must NOT recursively
            # demand its own combined tree, independent reviewers, or human
            # approval: those meta-gates are enforced once, by the parent fold.
            # Stripping them keeps the low-tier behavior the sub-gate always had
            # while adding the correct declared-check applicability tier.
            risk_profile = {
                **risk_profile,
                "combined_tree_required": False,
                "cross_provider_required": False,
                "explicit_human_approval_required": False,
                "required_reviewer_lenses": [],
            }
        checks = run_builtin_static_checks(root, changed_paths=affected)
        declared = run_declared_checks(
            root,
            changed_paths=affected,
            effective_risk_tier=str(risk_profile["effective_tier"]),
        )
        config_error = ""
    except MalformedConfigError as exc:
        checks = []
        declared = []
        config_error = str(exc)
        risk_profile = {}
    all_checks = [*checks, *declared]
    try:
        if not risk_profile:
            raise MalformedConfigError(config_error or "risk_profile_unavailable")
        verdict = fold_quality_verdict(
            all_checks,
            risk_profile=risk_profile,
            reviewer_reports=reviewer_reports,
            combined_tree_checks=combined_tree_checks,
            worker_provider=worker_provider,
            human_approval=human_approval,
            config_error=config_error,
        )
    except MalformedConfigError as exc:
        risk_profile = {}
        verdict = {
            "schema_id": VERDICT_SCHEMA_ID,
            "status": "unverified",
            "passed": False,
            "risk_profile": {},
            "lenses": [],
            "mechanical_checks": [check.to_dict() for check in all_checks],
            "reviewer_reports": [],
            "combined_tree_checks": [],
            "blocking_evidence": ["quality_verdict_schema_error"],
            "refine_required": False,
            "config_error": str(exc)[:MAX_SUMMARY_CHARS],
        }
    blockers = list(verdict["blocking_evidence"])
    optional = [optional_gate_status(root, gate).to_dict() for gate in sorted(OPTIONAL_GATES)]
    return {
        "schema_id": "aiworkhub.completion_quality_gate.v1",
        "passed": bool(verdict["passed"]),
        "changed_paths": list(affected[:MAX_AFFECTED_PATHS]),
        "checks": [check.to_dict() for check in all_checks],
        "blocking_checks": blockers,
        "config_error": config_error,
        "optional_gates": optional,
        "risk_profile": risk_profile,
        "quality_verdict": verdict,
        "repository_quality_policy": config_status,
        "reachability": _reachability_record(reachability_inputs),
        # verification_scope is derived from what EXECUTED, not from the mere
        # presence of a declared policy. An all-skipped run (every declared
        # check risk_below_minimum- or changed_paths_not_applicable-skipped) did
        # not let the repository's policy govern anything, so it must not claim
        # repository_policy scope.
        "verification_scope": (
            "repository_policy"
            if config_status["status"] == "configured"
            and any(check.status != STATUS_SKIPPED for check in declared)
            else "builtin_and_task_contract_only"
        ),
    }


def _public_python_symbols(source: str) -> set[str]:
    """Return bounded top-level public API names, or an empty set on parse failure."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
    return names


def run_destructive_diff_checks(
    baseline_root: Path | str,
    candidate_root: Path | str,
    *,
    changed_paths: Iterable[str],
) -> list[EvidenceCheck]:
    """Detect high-confidence module replacement using multiple signals.

    This deliberately does not rely on one brittle line threshold. A source
    path blocks only when it is a substantial existing file, loses both a
    large absolute and relative amount of content, and (for Python modules
    with a measurable API) loses most public top-level symbols. The manager
    can explicitly confirm an intentional destructive refactor at accept
    time; workers cannot self-authorize it through their own tests/config.
    """
    baseline = Path(baseline_root)
    candidate = Path(candidate_root)
    changed = tuple(sorted({str(path).replace("\\", "/") for path in changed_paths}))
    tests_changed = any(
        path.startswith("tests/") or "/test" in path or Path(path).name.startswith("test_")
        for path in changed
    )
    checks: list[EvidenceCheck] = []
    for relative in changed[:MAX_AFFECTED_PATHS]:
        if Path(relative).suffix.lower() not in DESTRUCTIVE_SOURCE_SUFFIXES:
            continue
        before_path = baseline / relative
        after_path = candidate / relative
        if not before_path.is_file():
            continue
        try:
            before = before_path.read_text(encoding="utf-8")
            after = after_path.read_text(encoding="utf-8") if after_path.is_file() else ""
        except (OSError, UnicodeError):
            continue
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        baseline_lines = len(before_lines)
        candidate_lines = len(after_lines)
        removed_lines = max(0, baseline_lines - candidate_lines)
        retained_ratio = candidate_lines / max(1, baseline_lines)
        signals = {
            "substantial_baseline": baseline_lines >= DESTRUCTIVE_MIN_BASELINE_LINES,
            "large_absolute_loss": removed_lines >= DESTRUCTIVE_MIN_REMOVED_LINES,
            "large_relative_loss": retained_ratio <= DESTRUCTIVE_MAX_RETAINED_RATIO,
        }
        before_symbols: set[str] = set()
        after_symbols: set[str] = set()
        if Path(relative).suffix.lower() == ".py":
            before_symbols = _public_python_symbols(before)
            after_symbols = _public_python_symbols(after)
            if len(before_symbols) >= DESTRUCTIVE_MIN_PUBLIC_SYMBOLS:
                signals["public_api_loss"] = (
                    len(after_symbols & before_symbols) / len(before_symbols)
                    <= DESTRUCTIVE_MAX_RETAINED_SYMBOL_RATIO
                )
        blocking = all(
            signals.get(name, False)
            for name in ("substantial_baseline", "large_absolute_loss", "large_relative_loss")
        ) and ("public_api_loss" not in signals or signals["public_api_loss"])
        checks.append(EvidenceCheck(
            check_id=f"builtin:destructive_diff:{relative}",
            kind="static_analysis",
            status=STATUS_FAILED if blocking else STATUS_PASSED,
            affected_paths=(relative,),
            summary=(
                f"baseline_lines={baseline_lines}; candidate_lines={candidate_lines}; "
                f"removed_lines={removed_lines}; retained_ratio={retained_ratio:.3f}; "
                f"public_symbols={len(before_symbols)}->{len(after_symbols)}; "
                f"tests_changed={str(tests_changed).lower()}; signals="
                + ",".join(name for name, active in signals.items() if active)
            ),
            provenance="builtin:manager_accept_destructive_diff",
            error=(
                "high-confidence destructive module replacement requires explicit manager confirmation"
                if blocking else ""
            ),
        ))
    return checks


# ---------------------------------------------------------------------------
# Adapters: normalize third-party report formats into EvidenceCheck without
# requiring the producing tool to exist. Each adapter is a pure function over
# already-produced report content (never runs anything itself).
# ---------------------------------------------------------------------------


def adapt_sarif(check_id: str, sarif_doc: Mapping[str, Any]) -> EvidenceCheck:
    runs = sarif_doc.get("runs", []) if isinstance(sarif_doc, Mapping) else []
    results: list[Any] = []
    for run in runs if isinstance(runs, list) else []:
        if isinstance(run, Mapping):
            run_results = run.get("results", [])
            if isinstance(run_results, list):
                results.extend(run_results)
    affected: list[str] = []
    for result in results:
        if not isinstance(result, Mapping):
            continue
        for loc in result.get("locations", []) if isinstance(result.get("locations"), list) else []:
            uri = (
                loc.get("physicalLocation", {})
                .get("artifactLocation", {})
                .get("uri")
                if isinstance(loc, Mapping)
                else None
            )
            if isinstance(uri, str):
                affected.append(uri)
    status = STATUS_FAILED if results else STATUS_PASSED
    return EvidenceCheck(
        check_id=check_id,
        kind="static_analysis",
        status=status,
        affected_paths=tuple(dict.fromkeys(affected)),
        summary=f"{len(results)} SARIF result(s)",
        provenance="adapter:sarif",
    )


def adapt_junit_xml(check_id: str, junit_text: str) -> EvidenceCheck:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(junit_text)
    except ET.ParseError as exc:
        return EvidenceCheck(
            check_id=check_id,
            kind="test",
            status=STATUS_FAILED,
            summary=f"malformed JUnit XML: {exc}",
            provenance="adapter:junit_xml",
            error=str(exc),
        )
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    total_failures = 0
    total_errors = 0
    total_tests = 0
    affected: list[str] = []
    for suite in suites:
        total_failures += int(suite.attrib.get("failures", 0) or 0)
        total_errors += int(suite.attrib.get("errors", 0) or 0)
        total_tests += int(suite.attrib.get("tests", 0) or 0)
        for case in suite.findall("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                classname = case.attrib.get("classname", "")
                if classname:
                    affected.append(classname)
    junit_error = ""
    if total_tests == 0:
        # A report that ran but exercised no tests is the absence of evidence,
        # not a pass. STATUS_NOT_AVAILABLE blocks a mandatory test check on both
        # the declared-check and the combined-tree fold paths; STATUS_SKIPPED
        # blocked on only one, so the two paths disagreed.
        status = STATUS_NOT_AVAILABLE
        junit_error = "junit_zero_tests"
    elif total_failures or total_errors:
        status = STATUS_FAILED
    else:
        status = STATUS_PASSED
    return EvidenceCheck(
        check_id=check_id,
        kind="test",
        status=status,
        affected_paths=tuple(dict.fromkeys(affected)),
        summary=f"{total_tests} test(s), {total_failures} failure(s), {total_errors} error(s)",
        provenance="adapter:junit_xml",
        error=junit_error,
    )


def adapt_coverage_summary(
    check_id: str,
    coverage_doc: Mapping[str, Any],
    *,
    min_percent: float = 0.0,
) -> EvidenceCheck:
    total = coverage_doc.get("total") if isinstance(coverage_doc, Mapping) else None
    percent = None
    if isinstance(total, Mapping):
        lines = total.get("lines")
        if isinstance(lines, Mapping):
            percent = lines.get("pct")
    if percent is None:
        return EvidenceCheck(
            check_id=check_id,
            kind="coverage",
            status=STATUS_NOT_AVAILABLE,
            summary="coverage summary missing total.lines.pct",
            provenance="adapter:coverage_summary",
        )
    try:
        percent_value = float(percent)
    except (TypeError, ValueError):
        return EvidenceCheck(
            check_id=check_id,
            kind="coverage",
            status=STATUS_FAILED,
            summary=f"non-numeric coverage percent: {percent!r}",
            provenance="adapter:coverage_summary",
        )
    status = STATUS_PASSED if percent_value >= min_percent else STATUS_FAILED
    return EvidenceCheck(
        check_id=check_id,
        kind="coverage",
        status=status,
        summary=f"line coverage {percent_value}% (threshold {min_percent}%)",
        provenance="adapter:coverage_summary",
    )


def adapt_benchmark_json(
    check_id: str,
    benchmark_doc: Mapping[str, Any],
) -> EvidenceCheck:
    benchmarks = benchmark_doc.get("benchmarks") if isinstance(benchmark_doc, Mapping) else None
    if not isinstance(benchmarks, list):
        return EvidenceCheck(
            check_id=check_id,
            kind="benchmark",
            status=STATUS_NOT_AVAILABLE,
            summary="benchmark JSON missing 'benchmarks' array",
            provenance="adapter:benchmark_json",
        )
    return EvidenceCheck(
        check_id=check_id,
        kind="benchmark",
        status=STATUS_PASSED if benchmarks else STATUS_SKIPPED,
        summary=f"{len(benchmarks)} benchmark(s) recorded",
        provenance="adapter:benchmark_json",
    )


def adapt_ai_reviewer_findings(
    check_id: str,
    findings: Iterable[Mapping[str, Any]],
    *,
    max_findings: int = 50,
) -> EvidenceCheck:
    bounded = list(findings)[:max_findings]
    affected = tuple(
        dict.fromkeys(str(f.get("file")) for f in bounded if isinstance(f, Mapping) and f.get("file"))
    )
    status = STATUS_FAILED if bounded else STATUS_PASSED
    return EvidenceCheck(
        check_id=check_id,
        kind="ai_review",
        status=status,
        affected_paths=affected,
        summary=f"{len(bounded)} bounded AI reviewer finding(s)",
        provenance="adapter:ai_reviewer_findings",
    )


# ---------------------------------------------------------------------------
# Optional-gate policy metadata: risk-based, read-only. Absence of a tool is
# not_available, never a silent pass and never a hard failure by default.
# ---------------------------------------------------------------------------


def optional_gate_status(repo_root: Path | str, gate: str) -> EvidenceCheck:
    if gate not in OPTIONAL_GATES:
        raise MalformedConfigError(f"unknown optional gate: {gate!r}")
    binary_name = "osv-scanner" if gate == "osv" else gate
    available = _which(binary_name) is not None
    meta = OPTIONAL_GATES[gate]
    return EvidenceCheck(
        check_id=f"optional_gate:{gate}",
        kind=str(meta["category"]),
        status=STATUS_NOT_AVAILABLE if not available else STATUS_SKIPPED,
        summary=f"risk_tier={meta['risk_tier']} blocking_by_default={meta['blocking_by_default']}",
        provenance=f"policy:optional_gate:{gate}",
    )


def quality_reviewer_contract() -> dict[str, Any]:
    """Cross-provider read-only quality_reviewer contract descriptor.

    Represented in evidence only -- this contract can never mutate the repo.
    """

    return {
        "schema_id": VERDICT_SCHEMA_ID,
        "role": "quality_reviewer",
        "read_only": True,
        "can_mutate_repo": False,
        "cross_provider": True,
        "judgment_lenses": sorted(JUDGMENT_LENSES),
        "finding_severities": sorted(VALID_SEVERITIES),
        "reviewer_verdict_accepted": False,
        "max_reports": MAX_REVIEW_REPORTS,
        "max_findings_per_report": MAX_REVIEW_FINDINGS,
    }


def quality_verdict_contract() -> dict[str, Any]:
    """Read-only schema/authority description for UI and model consumers."""

    return {
        "schema_id": VERDICT_SCHEMA_ID,
        "verdict_owner": "pure_deterministic_fold",
        "model_verdict_accepted": False,
        "quality_lenses": list(QUALITY_LENSES),
        # The lens status vocabulary a consumer must validate a row against,
        # published straight from VALID_STATUSES -- the same constant the fold
        # writes and validates against -- so a consumer can learn that
        # reviewer_could_not_inspect exists without a second hand-written list.
        "lens_statuses": sorted(VALID_STATUSES),
        "blocking_severities": sorted(BLOCKING_SEVERITIES),
        "risk_tiers": list(RISK_TIERS),
        "risk_signal_floors": dict(sorted(_RISK_SIGNAL_FLOORS.items())),
        "profiles": {
            tier: {
                **dict(_RISK_PROFILES[tier]),
                "required_reviewer_lenses": list(
                    _RISK_PROFILES[tier]["required_reviewer_lenses"]
                ),
            }
            for tier in RISK_TIERS
        },
    }


def build_evidence_packet(
    repo_root: Path | str,
    *,
    changed_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Assemble the full canonical evidence packet: profile + declared checks
    + optional-gate policy metadata + the read-only reviewer contract."""

    root = Path(repo_root)
    try:
        config_status = repo_config_status(root)
        declared_checks = [
            c.to_dict() for c in declared_check_descriptors(root, changed_paths=changed_paths)
        ]
        config_error = ""
    except MalformedConfigError as exc:
        config_status = {
            "config_present": True,
            "declared_check_count": 0,
            "status": "unverified",
            "reason": str(exc)[:MAX_SUMMARY_CHARS],
        }
        declared_checks = []
        config_error = str(exc)
    optional_gates = [
        optional_gate_status(root, gate).to_dict() for gate in sorted(OPTIONAL_GATES.keys())
    ]
    return {
        "schema_id": SCHEMA_ID,
        "repo_root": str(root),
        "profile": build_zero_config_profile(root),
        "declared_checks": declared_checks,
        "repository_quality_policy": config_status,
        "status": config_status["status"],
        "ok": config_status["status"] == "configured",
        "config_error": config_error,
        "optional_gates": optional_gates,
        "quality_reviewer_contract": quality_reviewer_contract(),
        "quality_verdict_contract": quality_verdict_contract(),
        "eval_artifact_contract": {
            "schema_id": eval_artifact_gate.SCHEMA_ID,
            "registry_path": eval_artifact_gate.REGISTRY_RELATIVE_PATH.as_posix(),
            "zero_eligible_rows": "inconclusive_never_pass",
            "summary_authority": "recomputed_from_registered_rows",
        },
    }
