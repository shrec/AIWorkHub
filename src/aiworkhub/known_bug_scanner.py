"""Diff-scoped, dependency-free known bug pattern scanner."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_ID = "aiworkhub.known_bug_scan.v1"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PATHS = 500
MAX_FINDINGS = 200
RUNTIME_RECEIPT_SCHEMA = "aiworkhub.known_bug_runtime_reproduction_receipt.v1"

CWE_BY_CATEGORY = {
    "memory_safety": "CWE-119",
    "command_injection": "CWE-78",
    "correctness": "CWE-682",
    "cryptography": "CWE-327",
    "code_injection": "CWE-94",
    "deserialization": "CWE-502",
    "transport_security": "CWE-295",
    "filesystem_race": "CWE-377",
}


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    suffixes: frozenset[str]
    pattern: re.Pattern[str]
    severity: str
    category: str
    message: str
    crypto_only: bool = False


CRYPTO_CONTEXT = re.compile(
    r"crypto|cipher|encrypt|decrypt|hash|hmac|secret|private|key|nonce|seed|signature|rng",
    re.IGNORECASE,
)
RULES = (
    Rule("cpp.dangerous_gets", frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}),
         re.compile(r"\bgets\s*\("), "error", "memory_safety", "unbounded gets() can overflow its destination"),
    Rule("cpp.unbounded_copy", frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}),
         re.compile(r"\b(?:strcpy|strcat|sprintf)\s*\("), "warning", "memory_safety",
         "unbounded C string operation requires a destination-size proof"),
    Rule("cpp.unbounded_scanf_string", frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}),
         re.compile(r'\b(?:scanf|sscanf)\s*\(\s*"[^"\n]*%s'), "warning", "memory_safety",
         "%s without a field width requires a destination-size proof"),
    Rule("cpp.process_shell", frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}),
         re.compile(r"\b(?:system|popen)\s*\("), "warning", "command_injection",
         "shell command execution requires a trusted-input boundary"),
    Rule("cpp.literal_divide_zero", frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cu", ".cuh"}),
         re.compile(r"(?<!/)\/(?![/=*])\s*(?:0|0\.0+)\b"), "error", "correctness",
         "division by a literal zero is undefined or invalid"),
    Rule("crypto.weak_c_rng", frozenset({".c", ".cc", ".cpp", ".cxx", ".cu", ".cuh"}),
         re.compile(r"\b(?:rand|srand)\s*\("), "error", "cryptography",
         "rand()/srand() is not a cryptographic random generator", True),
    Rule("crypto.timing_memcmp", frozenset({".c", ".cc", ".cpp", ".cxx", ".cu", ".cuh"}),
         re.compile(r"\bmemcmp\s*\([^;]*(?:secret|private|key|mac|tag|signature|digest)", re.I),
         "warning", "cryptography", "memcmp() may leak timing for secret material"),
    Rule("python.shell_true", frozenset({".py"}), re.compile(r"\bshell\s*=\s*True\b"),
         "error", "command_injection", "subprocess shell=True requires a trusted-input boundary"),
    Rule("python.dynamic_exec", frozenset({".py"}), re.compile(r"\b(?:eval|exec)\s*\("),
         "warning", "code_injection", "dynamic code execution requires a trusted-input proof"),
    Rule("python.unsafe_pickle", frozenset({".py"}), re.compile(r"\bpickle\.(?:load|loads)\s*\("),
         "warning", "deserialization", "pickle is unsafe for untrusted data"),
    Rule("python.tls_verify_disabled", frozenset({".py"}), re.compile(r"\bverify\s*=\s*False\b"),
         "error", "transport_security", "TLS certificate verification is disabled"),
    Rule("python.unverified_ssl_context", frozenset({".py"}), re.compile(r"\b_create_unverified_context\s*\("),
         "error", "transport_security", "an unverified TLS context disables certificate validation"),
    Rule("python.unsafe_yaml_load", frozenset({".py"}),
         re.compile(r"\byaml\.load\s*\((?![^\n)]*Loader\s*=)"),
         "warning", "deserialization", "yaml.load without an explicit safe loader can construct arbitrary objects"),
    Rule("python.insecure_mktemp", frozenset({".py"}), re.compile(r"\btempfile\.mktemp\s*\("),
         "warning", "filesystem_race", "mktemp() has a create-after-check race; use a securely opened temporary file"),
    Rule("javascript.eval", frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}),
         re.compile(r"\beval\s*\("), "warning", "code_injection", "eval() requires a trusted-input proof"),
    Rule("javascript.tls_disabled", frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}),
         re.compile(r"\brejectUnauthorized\s*:\s*false\b"), "error", "transport_security",
         "TLS certificate verification is disabled"),
    Rule("javascript.node_tls_env_disabled", frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}),
         re.compile(r"\bNODE_TLS_REJECT_UNAUTHORIZED\b[^\n]*(?:=|:)\s*['\"]?0['\"]?"),
         "error", "transport_security", "Node TLS certificate verification is disabled globally"),
    Rule("javascript.process_shell", frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}),
         re.compile(
             r"\b(?:child_process|childProcess)\.(?:exec|execSync)\s*\(|"
             r"\brequire\(\s*['\"]child_process['\"]\s*\)\.(?:exec|execSync)\s*\("
         ), "warning", "command_injection",
         "shell execution requires a trusted-input boundary"),
    Rule("go.tls_disabled", frozenset({".go"}), re.compile(r"\bInsecureSkipVerify\s*:\s*true\b"),
         "error", "transport_security", "TLS certificate verification is disabled"),
    Rule("go.weak_crypto_rng", frozenset({".go"}), re.compile(r'\"math/rand(?:/v2)?\"'),
         "error", "cryptography", "math/rand is not cryptographically secure", True),
    Rule("go.process_shell", frozenset({".go"}),
         re.compile(r'\bexec\.Command\s*\(\s*"(?:sh|bash|cmd|powershell)(?:\.exe)?"\s*,\s*"(?:-c|/c|/C)"'),
         "warning", "command_injection", "shell command execution requires a trusted-input boundary"),
    Rule("java.ecb_mode", frozenset({".java", ".kt", ".kts"}),
         re.compile(r'Cipher\.getInstance\s*\(\s*\"[^\"]*ECB', re.I), "error", "cryptography",
         "ECB mode reveals plaintext block patterns"),
    Rule("java.permissive_hostname_verifier", frozenset({".java", ".kt", ".kts"}),
         re.compile(r"HostnameVerifier[^\n]*(?:->|return)\s*true\b"), "error", "transport_security",
         "hostname verification accepts every peer"),
    Rule("csharp.permissive_certificate_callback", frozenset({".cs"}),
         re.compile(r"ServerCertificateCustomValidationCallback[^\n]*(?:=>|return)\s*true\b"),
         "error", "transport_security", "certificate validation accepts every peer"),
    Rule("php.dynamic_eval", frozenset({".php", ".phtml"}), re.compile(r"\beval\s*\("),
         "warning", "code_injection", "eval() requires a trusted-input proof"),
    Rule("php.unsafe_unserialize", frozenset({".php", ".phtml"}), re.compile(r"\bunserialize\s*\("),
         "warning", "deserialization", "unserialize() requires a trusted-type boundary"),
    Rule("php.tls_verify_disabled", frozenset({".php", ".phtml"}),
         re.compile(r"CURLOPT_SSL_VERIFYPEER\s*,\s*(?:false|0)\b", re.I),
         "error", "transport_security", "TLS peer verification is disabled"),
)
BYTE_PERM_RE = re.compile(r"__byte_perm\s*\(\s*\w+\s*,\s*0\s*,\s*(0x[0-9a-fA-F]+)\s*\)")
ROTATION_RE = re.compile(r"rotl32\((\d+)\)|rotr32\((\d+)\)")
RELEASE_RE = re.compile(
    r"\bfree\s*\(\s*([A-Za-z_]\w*)\s*\)|"
    r"\bdelete(?:\s*\[\s*\])?\s+([A-Za-z_]\w*)"
)


def _rotation(selector: int) -> int | None:
    known = {0x2103: 8, 0x1032: 16, 0x0321: 24, 0x3210: 0}
    if selector in known:
        return known[selector]
    actual = [(selector >> shift) & 0xF for shift in (0, 4, 8, 12)]
    for count in range(4):
        if actual == [(count + index) % 4 for index in range(4)]:
            return count * 8
    return None


def _finding(rule: Rule, path: str, line: int, column: int, snippet: str) -> dict:
    digest = hashlib.sha256(f"{rule.rule_id}\0{path}\0{line}\0{snippet}".encode()).hexdigest()
    normalized_snippet = " ".join(snippet.strip().split())[:300]
    root_cause_fingerprint = hashlib.sha256(
        f"{rule.rule_id}\0{path}\0{normalized_snippet}".encode()
    ).hexdigest()
    return {"rule_id": rule.rule_id, "path": path, "line": line, "column": column,
            "severity": rule.severity, "category": rule.category, "message": rule.message,
            "cwe": CWE_BY_CATEGORY.get(rule.category, ""),
            "snippet": snippet.strip()[:300], "fingerprint": digest,
            "root_cause_fingerprint": root_cause_fingerprint,
            # A deterministic source pattern can be blocking without being a
            # claim that the defect was exercised at runtime.  Keeping this
            # boundary explicit prevents static candidates from masquerading
            # as reproduced vulnerabilities.
            "verification_state": "static_candidate",
            "evidence_class": "deterministic_source_pattern",
            "runtime_validated": False,
            "required_next_evidence": "targeted_test_or_reproduction"}


def _source_revision_sha256(root: Path, paths: Iterable[str]) -> str:
    """Hash the exact bounded path set and bytes inspected by the scanner."""

    revision_rows: list[dict[str, Any]] = []
    for relative in paths:
        path = (root / relative).resolve(strict=False)
        safe_regular = (
            (path == root or root in path.parents)
            and not path.is_symlink()
            and path.is_file()
        )
        revision_rows.append({
            "path": relative,
            "sha256": (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if safe_regular
                else None
            ),
        })
    encoded = json.dumps(
        revision_rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_receipt_bytes(receipt: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in receipt.items() if key != "hmac_sha256"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def issue_runtime_reproduction_receipt(
    report: dict[str, Any],
    *,
    bundle_dir: Path,
    root_cause_fingerprint: str,
    check_id: str,
    expected_returncodes: Iterable[int],
    verified_by: str,
    coordinator_key: bytes,
) -> dict[str, Any]:
    """Issue a manager-owned receipt from an already sealed attempt bundle.

    The validation command and exit semantics are read from the manifest-bound
    artifact, not accepted from worker prose.  The private coordinator key is
    required again when the receipt is applied to a scanner report.
    """

    from . import attempt_artifacts

    if not coordinator_key:
        raise ValueError("coordinator_key must be non-empty")
    if not verified_by or not verified_by.strip():
        raise ValueError("verified_by must be non-empty")
    source_revision = str(report.get("source_revision_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_revision):
        raise ValueError("report source revision is unavailable")
    finding_fingerprints = {
        str(row.get("root_cause_fingerprint") or "")
        for row in report.get("findings") or []
        if isinstance(row, dict)
    }
    if root_cause_fingerprint not in finding_fingerprints:
        raise ValueError("root cause fingerprint is not present in report")

    verification = attempt_artifacts.verify_json_bundle(bundle_dir)
    manifest_path = bundle_dir / attempt_artifacts.MANIFEST_FILENAME
    manifest = attempt_artifacts.parse_manifest_json(
        manifest_path.read_text(encoding="utf-8")
    )
    validation_entry = next(
        (entry for entry in manifest.artifacts if entry.role == "validation"),
        None,
    )
    if validation_entry is None:
        raise ValueError("attempt bundle has no validation artifact")
    validation = json.loads(
        (bundle_dir / validation_entry.path).read_text(encoding="utf-8")
    )
    checks = validation.get("checks") if isinstance(validation, dict) else None
    if not isinstance(checks, list):
        raise ValueError("attempt validation checks are unavailable")
    matching = [
        row for row in checks
        if isinstance(row, dict) and str(row.get("check_id") or "") == check_id
    ]
    if len(matching) != 1:
        raise ValueError("runtime reproduction check identity is ambiguous or missing")
    check = matching[0]
    command = check.get("executed_argv") or check.get("argv") or check.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
    ):
        raise ValueError("runtime reproduction command must be exact argv")
    returncode = check.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ValueError("runtime reproduction returncode is unavailable")
    allowed_returncodes = tuple(expected_returncodes)
    if (
        not allowed_returncodes
        or any(isinstance(code, bool) or not isinstance(code, int) for code in allowed_returncodes)
        or returncode not in allowed_returncodes
    ):
        raise ValueError("runtime reproduction exit semantics did not match")

    receipt: dict[str, Any] = {
        "schema_id": RUNTIME_RECEIPT_SCHEMA,
        "attempt_id": str(verification["attempt_id"]),
        "root_cause_fingerprint": root_cause_fingerprint,
        "source_revision_sha256": source_revision,
        "check_id": check_id,
        "command": command,
        "command_sha256": hashlib.sha256(
            json.dumps(command, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "returncode": returncode,
        "expected_returncodes": list(allowed_returncodes),
        "duration_seconds": check.get("duration_seconds"),
        "artifact_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "artifact_roles": list(verification["roles"]),
        "artifact_verified": verification.get("verified") is True,
        "runtime_reproduced": True,
        "verified_by": verified_by.strip(),
    }
    receipt["hmac_sha256"] = hmac.new(
        coordinator_key,
        _canonical_receipt_bytes(receipt),
        hashlib.sha256,
    ).hexdigest()
    return receipt


def bind_runtime_reproduction_receipts(
    report: dict[str, Any],
    receipts: Iterable[dict[str, Any]],
    *,
    coordinator_key: bytes,
) -> dict[str, Any]:
    """Return a report whose exact matching findings carry verified receipts."""

    if not coordinator_key:
        raise ValueError("coordinator_key must be non-empty")
    revision = str(report.get("source_revision_sha256") or "")
    verified: dict[str, dict[str, Any]] = {}
    for raw in receipts:
        if not isinstance(raw, dict) or raw.get("schema_id") != RUNTIME_RECEIPT_SCHEMA:
            raise ValueError("runtime reproduction receipt schema is invalid")
        signature = raw.get("hmac_sha256")
        expected = hmac.new(
            coordinator_key,
            _canonical_receipt_bytes(raw),
            hashlib.sha256,
        ).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise ValueError("runtime reproduction receipt signature is invalid")
        fingerprint = str(raw.get("root_cause_fingerprint") or "")
        if raw.get("source_revision_sha256") != revision:
            raise ValueError("runtime reproduction receipt revision mismatch")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or raw.get("artifact_verified") is not True
            or raw.get("runtime_reproduced") is not True
            or not raw.get("verified_by")
        ):
            raise ValueError("runtime reproduction receipt evidence is incomplete")
        verified[fingerprint] = dict(raw)

    bound_findings: list[dict[str, Any]] = []
    runtime_validated = 0
    for finding in report.get("findings") or []:
        row = dict(finding)
        receipt = verified.get(str(row.get("root_cause_fingerprint") or ""))
        if receipt is not None:
            row.update({
                "verification_state": "runtime_validated",
                "runtime_validated": True,
                "required_next_evidence": "",
                "runtime_reproduction_receipt": receipt,
            })
            runtime_validated += 1
        bound_findings.append(row)

    result = dict(report)
    result["findings"] = bound_findings
    summary = dict(result.get("evidence_summary") or {})
    summary["runtime_validated"] = runtime_validated
    summary["unvalidated_static_candidates"] = len(bound_findings) - runtime_validated
    summary["claim_boundary"] = (
        "runtime_receipt_bound_and_static_candidates_distinguished"
        if runtime_validated
        else "static_pattern_not_runtime_reproduction"
    )
    result["evidence_summary"] = summary
    return result


def _python_code_lines(text: str) -> list[str]:
    """Mask Python comments and literals while preserving line/column offsets."""

    lines = text.splitlines()
    masked = [list(line) for line in lines]
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type not in {tokenize.STRING, tokenize.COMMENT}:
                continue
            (start_line, start_col), (end_line, end_col) = token.start, token.end
            for line_no in range(start_line, end_line + 1):
                if not (1 <= line_no <= len(masked)):
                    continue
                left = start_col if line_no == start_line else 0
                right = end_col if line_no == end_line else len(masked[line_no - 1])
                for column in range(max(0, left), min(right, len(masked[line_no - 1]))):
                    masked[line_no - 1][column] = " "
    except (IndentationError, tokenize.TokenError):
        # Syntax validation is a separate Quality Evidence check. Returning
        # unmodified text keeps this scanner deterministic for partial files.
        return lines
    return ["".join(line) for line in masked]


def _generic_code_lines(text: str) -> list[str]:
    """Mask comments and string literals for non-Python source.

    A single offset-preserving scan blanks ``//`` and ``#`` line comments,
    ``/* */`` block comments, and single/double quoted strings, so a dangerous
    pattern inside a string literal is not reported and a block-comment
    continuation line beginning with ``*`` is masked to whitespace instead of
    being pattern-matched.  Every consumed character maps to exactly one output
    character, so column offsets stay accurate.
    """

    out: list[str] = []
    state: str | None = None  # None | "line" | "block" | "sq" | "dq"
    length = len(text)
    index = 0
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if state is None:
            if char == "/" and nxt == "/":
                state, index = "line", index + 2
                out.append("  ")
            elif char == "#":
                state, index = "line", index + 1
                out.append(" ")
            elif char == "/" and nxt == "*":
                state, index = "block", index + 2
                out.append("  ")
            elif char == '"':
                state, index = "dq", index + 1
                out.append(" ")
            elif char == "'":
                state, index = "sq", index + 1
                out.append(" ")
            else:
                out.append(char)
                index += 1
        elif state == "line":
            if char == "\n":
                state = None
                out.append("\n")
            else:
                out.append(" ")
            index += 1
        elif state == "block":
            if char == "*" and nxt == "/":
                state, index = None, index + 2
                out.append("  ")
            else:
                out.append("\n" if char == "\n" else " ")
                index += 1
        else:  # inside a single/double quoted string
            quote = "'" if state == "sq" else '"'
            if char == "\\" and index + 1 < length:
                # A backslash-newline is a legal string line continuation: keep
                # the newline so downstream line indexing stays aligned. Every
                # other escaped char still maps to a single blanking space.
                out.append(" \n" if nxt == "\n" else "  ")
                index += 2
            elif char == "\n":
                # An unterminated literal never crosses a physical line here.
                state = None
                out.append("\n")
                index += 1
            elif char == quote:
                state = None
                out.append(" ")
                index += 1
            else:
                out.append(" ")
                index += 1
    return "".join(out).splitlines()


def _masked_code_lines(text: str, suffix: str) -> list[str]:
    """Return per-line source with comments and string literals blanked."""

    if suffix == ".py":
        return _python_code_lines(text)
    return _generic_code_lines(text)


def _scan_path(root: Path, relative: str) -> tuple[list[dict], dict | None]:
    """Scan one path, returning its findings and an optional skip record.

    A skip record names why the file was not scanned (for example, it exceeds
    ``MAX_FILE_BYTES``) so an unscanned file is never silently reported as
    clean.
    """

    path = (root / relative).resolve(strict=False)
    if (path != root and root not in path.parents) or path.is_symlink() or not path.is_file():
        return [], None
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        return [], {
            "path": relative,
            "reason": "file_exceeds_max_bytes",
            "size_bytes": size,
            "max_bytes": MAX_FILE_BYTES,
        }
    suffix = path.suffix.lower()
    rules = [rule for rule in RULES if suffix in rule.suffixes]
    if not rules and suffix not in {".cu", ".cuh"}:
        return [], None
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    code_lines = _masked_code_lines(text, suffix)
    # Crypto context is a property of the whole file, not of the lines above the
    # offending one: compute it once so a crypto_only rule fires regardless of
    # whether the crypto keyword sits above or below the match.
    crypto = bool(CRYPTO_CONTEXT.search(relative)) or any(
        CRYPTO_CONTEXT.search(line) for line in code_lines
    )
    findings = []
    released: dict[str, int] = {}
    for number, raw in enumerate(lines, 1):
        source = code_lines[number - 1] if number - 1 < len(code_lines) else ""
        stripped = source.strip()
        # Comments and string literals are already blanked, so an empty masked
        # line is the only skip condition; a code line beginning with ``*`` (a C
        # pointer dereference) is no longer skipped as a block-comment body.
        if not stripped:
            continue
        if suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cu", ".cuh"}:
            for name in tuple(released):
                if re.search(rf"\b{re.escape(name)}\s*=(?!=)", source):
                    released.pop(name, None)
                    continue
                if re.search(rf"\b{re.escape(name)}\s*->|\*\s*{re.escape(name)}\b", source):
                    rule = Rule(
                        "cpp.use_after_release_candidate", frozenset({suffix}), RELEASE_RE,
                        "warning", "memory_safety",
                        f"{name} is dereferenced after release on line {released[name]}",
                    )
                    findings.append(_finding(rule, relative, number, 1, raw))
                    released.pop(name, None)
            for release in RELEASE_RE.finditer(source):
                name = release.group(1) or release.group(2)
                if name in released:
                    rule = Rule(
                        "cpp.double_release_candidate", frozenset({suffix}), RELEASE_RE,
                        "warning", "memory_safety",
                        f"{name} is released again after line {released[name]}",
                    )
                    findings.append(_finding(rule, relative, number, release.start() + 1, raw))
                released[name] = number
        for rule in rules:
            if rule.crypto_only and not crypto:
                continue
            for match in rule.pattern.finditer(source):
                findings.append(_finding(rule, relative, number, match.start() + 1, raw))
        if suffix in {".cu", ".cuh"}:
            for match in BYTE_PERM_RE.finditer(source):
                actual = _rotation(int(match.group(1), 16))
                claim = ROTATION_RE.search("\n".join(lines[max(0, number - 5):number]))
                claimed = int(claim.group(1) or claim.group(2)) if claim else None
                if actual is not None and claimed is not None and actual != claimed:
                    rule = Rule("cuda.byte_perm_rotation_mismatch", frozenset({suffix}), BYTE_PERM_RE,
                                "error", "correctness",
                                f"selector produces rotl32({actual}) but nearby code claims {claimed}")
                    findings.append(_finding(rule, relative, number, match.start() + 1, raw))
        if len(findings) >= MAX_FINDINGS:
            break
    return findings[:MAX_FINDINGS], None


def scan_file(root: Path, relative: str) -> list[dict]:
    """Return the findings for one path (skip records are report-level)."""

    return _scan_path(root, relative)[0]


def scan_changed_paths(repo_root: Path | str, changed_paths: Iterable[str]) -> dict:
    root = Path(repo_root).resolve()
    paths = tuple(sorted(dict.fromkeys(map(str, changed_paths))))[:MAX_PATHS]
    findings = []
    skipped_paths: list[dict] = []
    for relative in paths:
        file_findings, skip = _scan_path(root, relative)
        if skip is not None:
            skipped_paths.append(skip)
        findings.extend(file_findings)
        if len(findings) >= MAX_FINDINGS:
            break
    errors = sum(row["severity"] == "error" for row in findings)
    retained = findings[:MAX_FINDINGS]
    unique_root_causes = len({
        row["root_cause_fingerprint"] for row in retained
    })
    # A file skipped for size was not inspected, so it cannot be counted as
    # clean: ``passed`` must reflect the skip alongside any error findings.
    return {"schema_id": SCHEMA_ID,
            "passed": errors == 0 and not skipped_paths, "errors": errors,
            "warnings": sum(row["severity"] == "warning" for row in findings),
            "paths_considered": len(paths), "findings": retained,
            "skipped_paths": skipped_paths,
            "source_revision_sha256": _source_revision_sha256(root, paths),
            "source_revision_scope": "exact_paths_and_bytes",
            "truncated": len(findings) > MAX_FINDINGS,
            "dedupe_summary": {
                "candidate_count": len(retained),
                "unique_root_causes": unique_root_causes,
                "duplicate_candidates": len(retained) - unique_root_causes,
                "identity_scope": "rule_path_normalized_source",
            },
            "evidence_summary": {
                "static_candidates": min(len(findings), MAX_FINDINGS),
                "runtime_validated": 0,
                "claim_boundary": "static_pattern_not_runtime_reproduction",
            }}


def to_sarif(report: dict) -> dict:
    """Convert one bounded native report to deterministic SARIF 2.1.0.

    The export preserves the native verification boundary in result
    properties.  It never upgrades a static pattern to a reproduced finding.
    """

    findings = [row for row in report.get("findings", []) if isinstance(row, dict)]
    rules: dict[str, dict] = {}
    results: list[dict] = []
    for finding in findings[:MAX_FINDINGS]:
        rule_id = str(finding.get("rule_id") or "aiworkhub.unknown")
        category = str(finding.get("category") or "quality")
        cwe = str(finding.get("cwe") or "")
        rules.setdefault(rule_id, {
            "id": rule_id,
            "name": rule_id.replace(".", "_"),
            "shortDescription": {"text": str(finding.get("message") or rule_id)},
            "properties": {
                "category": category,
                "cwe": cwe,
                "evidenceClass": "deterministic_source_pattern",
            },
        })
        line = max(1, int(finding.get("line") or 1))
        column = max(1, int(finding.get("column") or 1))
        severity = str(finding.get("severity") or "warning")
        properties: dict[str, Any] = {
            "category": category,
            "cwe": cwe,
            "verificationState": str(
                finding.get("verification_state") or "static_candidate"
            ),
            "runtimeValidated": bool(finding.get("runtime_validated")),
            "requiredNextEvidence": str(
                finding.get("required_next_evidence") or ""
            ),
        }
        reproduction = finding.get("runtime_reproduction_receipt")
        if isinstance(reproduction, dict) and finding.get("runtime_validated") is True:
            properties["runtimeReproduction"] = {
                "attemptId": str(reproduction.get("attempt_id") or ""),
                "manifestSha256": str(
                    reproduction.get("artifact_manifest_sha256") or ""
                ),
                "commandSha256": str(reproduction.get("command_sha256") or ""),
                "returncode": reproduction.get("returncode"),
                "verifiedBy": str(reproduction.get("verified_by") or ""),
            }
        results.append({
            "ruleId": rule_id,
            "level": "error" if severity == "error" else "warning",
            "message": {"text": str(finding.get("message") or rule_id)},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": str(finding.get("path") or "")},
                    "region": {"startLine": line, "startColumn": column},
                }
            }],
            "partialFingerprints": {
                "aiworkhubFindingFingerprint": str(finding.get("fingerprint") or ""),
                "aiworkhubRootCauseFingerprint": str(
                    finding.get("root_cause_fingerprint") or ""
                ),
            },
            "properties": properties,
        })
    runtime_validated = sum(
        result["properties"].get("runtimeValidated") is True for result in results
    )
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "AIWorkHub Known Bug Scanner",
                "informationUri": "https://shrec.github.io/AIWorkHub/",
                "rules": [rules[key] for key in sorted(rules)],
            }},
            "results": results,
            "properties": {
                "sourceSchema": str(report.get("schema_id") or SCHEMA_ID),
                "claimBoundary": (
                    "runtime_receipt_bound_and_static_candidates_distinguished"
                    if runtime_validated
                    else "static_pattern_not_runtime_reproduction"
                ),
                "runtimeValidated": runtime_validated,
                "truncated": bool(report.get("truncated")),
            },
        }],
    }
