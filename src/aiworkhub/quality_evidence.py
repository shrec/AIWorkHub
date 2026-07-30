from __future__ import annotations

import json
import ast
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

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

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_NOT_AVAILABLE = "not_available"
STATUS_SKIPPED = "skipped"
VALID_STATUSES = frozenset({STATUS_PASSED, STATUS_FAILED, STATUS_NOT_AVAILABLE, STATUS_SKIPPED})

CONFIG_RELATIVE_PATH = ".aiworkhub/quality.json"
MAX_AFFECTED_PATHS = 200
MAX_SUMMARY_CHARS = 2000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
DESTRUCTIVE_SOURCE_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".bash"})
DESTRUCTIVE_MIN_BASELINE_LINES = 200
DESTRUCTIVE_MIN_REMOVED_LINES = 100
DESTRUCTIVE_MAX_RETAINED_RATIO = 0.50
DESTRUCTIVE_MIN_PUBLIC_SYMBOLS = 4
DESTRUCTIVE_MAX_RETAINED_SYMBOL_RATIO = 0.50

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
            "duration_seconds": round(self.duration_seconds, 6),
            "affected_paths": list(self.affected_paths[:MAX_AFFECTED_PATHS]),
            "summary": self.summary[:MAX_SUMMARY_CHARS],
            "provenance": self.provenance,
            "error": self.error,
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


def _run_command_array(
    command: Iterable[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> tuple[str, str, str, float]:
    """Run one exact argv array, shell=False. Returns (status, stdout, stderr, duration)."""

    argv = list(command)
    start = time.monotonic()
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
) -> list[EvidenceCheck]:
    """Execute repo-local declared quality/security commands for one task delta.

    Fails closed (raises MalformedConfigError) on a malformed config instead
    of silently skipping or partially running it. Commands only ever run as
    argv arrays -- never a shell string.
    """

    root = Path(repo_root)
    config = load_repo_config(root)
    affected = tuple(sorted(str(p) for p in (changed_paths or ())))
    results: list[EvidenceCheck] = []
    for entry in config.get("checks", []):
        check_id = str(entry["id"])
        kind = str(entry["kind"])
        command = tuple(str(part) for part in entry["command"])
        status, stdout, stderr, duration = _run_command_array(
            command, cwd=root, timeout_seconds=timeout_seconds
        )
        summary = (stdout or "") + (("\n" + stderr) if stderr else "")
        results.append(
            EvidenceCheck(
                check_id=check_id,
                kind=kind,
                status=status,
                command=command,
                duration_seconds=duration,
                affected_paths=affected,
                summary=summary.strip(),
                provenance=f"repo_config:{CONFIG_RELATIVE_PATH}",
                error="" if status != STATUS_NOT_AVAILABLE else stderr,
            )
        )
    return results


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
    return checks


def run_completion_quality_gate(
    repo_root: Path | str,
    *,
    changed_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Execute the mandatory review-quality floor for one task delta.

    Built-in diff syntax checks always run. Every repo-declared check is
    mandatory: ``failed`` and ``not_available`` both block. Optional
    CodeQL/Semgrep/etc. availability is reported truthfully but does not pass
    or fail the task unless the repository declares its exact command in
    ``.aiworkhub/quality.json``.
    """
    root = Path(repo_root)
    affected = tuple(sorted(str(p) for p in (changed_paths or ())))
    try:
        checks = run_builtin_static_checks(root, changed_paths=affected)
        declared = run_declared_checks(root, changed_paths=affected)
        config_error = ""
    except MalformedConfigError as exc:
        checks = []
        declared = []
        config_error = str(exc)
    all_checks = [*checks, *declared]
    blockers = [
        check.check_id
        for check in all_checks
        if check.status in {STATUS_FAILED, STATUS_NOT_AVAILABLE}
    ]
    optional = [optional_gate_status(root, gate).to_dict() for gate in sorted(OPTIONAL_GATES)]
    return {
        "schema_id": "aiworkhub.completion_quality_gate.v1",
        "passed": not config_error and not blockers,
        "changed_paths": list(affected[:MAX_AFFECTED_PATHS]),
        "checks": [check.to_dict() for check in all_checks],
        "blocking_checks": blockers,
        "config_error": config_error,
        "optional_gates": optional,
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
    if total_tests == 0:
        status = STATUS_SKIPPED
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
        "schema_id": SCHEMA_ID,
        "role": "quality_reviewer",
        "read_only": True,
        "can_mutate_repo": False,
        "cross_provider": True,
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
        declared_checks = [
            c.to_dict() for c in declared_check_descriptors(root, changed_paths=changed_paths)
        ]
        config_error = ""
    except MalformedConfigError as exc:
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
        "config_error": config_error,
        "optional_gates": optional_gates,
        "quality_reviewer_contract": quality_reviewer_contract(),
    }
