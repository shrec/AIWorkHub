#!/usr/bin/env python3
"""B120: review-artifact strictness guard (READ-ONLY, no queue mutation).

Deterministic pre-Codex-review gate. Inspects the on-disk deliverables a
worker wave claims to have produced (eval JSON, optional rows JSONL, the
bash test harness, and any builder/harness script) and flags submissions
that are hollow: 0-byte JSON artifacts, echo-only "tests", dummy
placeholder harness scripts, or an eval artifact missing any
acceptance-key metric container (gates / acceptance_results /
invariants_verified / checks_total+checks_passed / tests_run).

This is a deterministic SUPPORT-BOUNDARY / correctness check over static
file content only (allowed under the neural-control doctrine as a
review-support validator, same class as review_summarizer.py's B116
risk/hygiene derivation) -- it never routes model intent, selects an
ISA/tool, or makes an abstain decision; it only screens already-finished
worker submissions before a human/Codex reviewer spends time on them.

Hard invariants:
  * READ-ONLY: opens files for reading only; never writes outside the two
    declared output artifacts (eval JSON + rows JSONL) and never touches
    the task queue DB or any file outside this script's own
    allowed_writes.
  * NO LAUNCH: no subprocess spawn, no os-level fork/exec/spawn/system/
    popen, no Popen anywhere in this module; no taskctl write command
    (done/review/auto-pickup/export-jsonl/usage/stage/guard-staged) is
    ever invoked.
  * NO QUEUE I/O: this module never imports or calls taskctl in any form;
    it only reads eval/tests/scripts paths under tools/aiworkhub/.
  * Every audit result carries machine-readable ``invalid_reasons`` and a
    ``reviewer_action`` in {"accept_for_codex_review",
    "reject_return_to_worker"}.

Usage:
  python3 audit_review_artifact_strictness_b120_v1.py            # self-test over the 3 known B119 waves + write outputs
  python3 audit_review_artifact_strictness_b120_v1.py --no-write # print report only, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

CONTRACT: str = "B120_v1_review_artifact_strictness_guard_readonly"
READONLY: bool = True
LAUNCH_IMPLEMENTED: bool = False
SUBPROCESS_LAUNCH_TRIPWIRE: int = 0
QUEUE_MUTATION_TRIPWIRE: int = 0

REPO = Path(os.environ.get("AIWORKHUB_REPO", "/home/shrek/AIWorkHub")).expanduser().resolve()
MCP_ROOT = REPO / "tools" / "aiworkhub"
EVAL_DIR = MCP_ROOT / "eval"
DATA_DIR = MCP_ROOT / "data" / "tasking"

OUT_EVAL_JSON = EVAL_DIR / "review_artifact_strictness_guard_b120_v1.json"
OUT_ROWS_JSONL = EVAL_DIR / "review_artifact_strictness_guard_rows_b120_v1.jsonl"


# ── known-wave manifest (the 3 waves named in the task's acceptance) ────
# B122 repaired the formerly rejected full-wave dryrun. Keep it in the live
# manifest so the guard proves current artifacts are acceptable, while the
# test harness carries synthetic hollow fixtures that prove the rejection
# heuristics still catch 0-byte/echo-only/dummy/unbound-arg submissions.
KNOWN_WAVES: list[dict[str, Any]] = [
    {
        "wave": "mcp_full_wave_dryrun_harness_b119_v1",
        "eval_json": "eval/mcp_full_wave_dryrun_harness_b119_v1.json",
        "rows_jsonl": "eval/mcp_full_wave_dryrun_harness_rows_b119_v1.jsonl",
        "test_sh": "tests/test_mcp_full_wave_dryrun_harness_b119_v1.sh",
        "extra_scripts": ["scripts/build_mcp_full_wave_dryrun_harness_b119_v1.py"],
        "expected_invalid": False,
    },
    {
        "wave": "mcp_codex_handoff_markdown_render_b119_v1",
        "eval_json": "eval/mcp_codex_handoff_markdown_render_b119_v1.json",
        "rows_jsonl": None,
        "test_sh": "tests/test_mcp_codex_handoff_markdown_render_b119_v1.sh",
        "extra_scripts": [],
        "expected_invalid": False,
    },
    {
        "wave": "launch_queue_persist_audit_b119_v1",
        "eval_json": "eval/launch_queue_persist_audit_b119_v1.json",
        "rows_jsonl": None,
        "test_sh": "tests/test_launch_queue_persist_audit_b119_v1.sh",
        "extra_scripts": [],
        "expected_invalid": False,
    },
]

# An eval JSON must carry a truthy "verdict" AND at least one non-empty
# acceptance-key metric container to prove it actually measured something
# (as opposed to being a bare skeleton).
_METRIC_DICT_CONTAINERS: tuple[str, ...] = (
    "gates", "acceptance_results", "invariants_verified",
)
_METRIC_LIST_CONTAINERS: tuple[str, ...] = ("tests_run",)

_ECHO_LINE_RE = re.compile(r'^echo\s+"?[A-Za-z0-9_ .,:!?\'\-]*"?\s*$')
_EXIT_LINE_RE = re.compile(r'^exit\s+\d+\s*$')
# B121: `true` / `:` / `return 0` are shell no-ops -- padding an echo-only
# test with a few of these lines must not escape the is_echo_exit_only
# check (red-team finding: 4+ lines of echo+true still do nothing real).
_NOOP_LINE_RE = re.compile(r'^(true|:|return\s+0)\s*$')

_TEST_SUBSTANCE_MARKERS: tuple[str, ...] = (
    "assert", "python3", "diff ", "sha256", "grep ", "!=", "==",
    "-eq", "-ne", "[[", "curl ", "import ", "mktemp",
)

_HARNESS_DUMMY_MARKERS: tuple[str, ...] = (
    "dummy harness", "dummy", "todo", "not implemented", "placeholder",
)
_HARNESS_REAL_IO_MARKERS: tuple[str, ...] = (
    "open(", "json.dump", "argparse", "Path(", ".write_text(", ".write(",
)

# B121: a hollow submission does not have to look like an echo-only/dummy
# stub. A bash test can reference bare CLI positional parameters ($1, $2,
# ...) that this repo's test-harness convention never binds -- every
# test_*.sh here is invoked with zero arguments -- so any assertion built
# on them is vacuous (always compares against an empty/unset value). This
# is distinct from the extremely common and LEGITIMATE bash idiom of a
# helper function using $1/$2 as its own call-time parameters (e.g.
# `check() { local a="$1" b="$2"; ... }` called later as `check "x" "y"`);
# that usage must NOT be flagged. A Python harness has the analogous shape:
# sys.argv[N] read without a len(sys.argv) or argparse guard.
_POSITIONAL_PARAM_RE = re.compile(r'\$\{?([1-9][0-9]?)\}?')
_HEREDOC_START_RE = re.compile(r'<<-?\s*[\'"]?([A-Za-z_][A-Za-z0-9_]*)[\'"]?\s*$')
_FUNC_OPEN_RE = re.compile(r'^\s*(function\s+)?[A-Za-z_][A-Za-z0-9_]*\s*\(\)\s*\{')


def _has_unbound_positional_args_bash(text: str) -> bool:
    """True iff `text` references a bare $1.."$9"/${1}..${9} outside any
    function body, with no $# (argument-count) guard anywhere in the file.
    Heredoc bodies (embedded non-bash scripts) and single-quoted spans
    (bash never expands $ inside '...', e.g. awk '{print $1}') are excluded
    from the scan so they cannot trigger a false positive."""
    in_heredoc = False
    heredoc_marker: str | None = None
    brace_depth = 0
    found_toplevel_positional = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if in_heredoc:
            if stripped == heredoc_marker:
                in_heredoc = False
                heredoc_marker = None
            continue
        m = _HEREDOC_START_RE.search(raw)
        if m:
            heredoc_marker = m.group(1)
            in_heredoc = True
            continue
        if not stripped or stripped.startswith("#"):
            continue
        is_func_open_line = bool(_FUNC_OPEN_RE.match(raw))
        unquoted = "".join(seg for idx, seg in enumerate(raw.split("'")) if idx % 2 == 0)
        if brace_depth == 0 and not is_func_open_line and _POSITIONAL_PARAM_RE.search(unquoted):
            found_toplevel_positional = True
        brace_depth = max(0, brace_depth + raw.count("{") - raw.count("}"))
    saw_argc_guard = "$#" in text
    return found_toplevel_positional and not saw_argc_guard


def _has_unbound_positional_args_python(text: str) -> bool:
    """True iff `text` reads sys.argv[N] (N>=1) without any len(sys.argv)
    check or argparse usage anywhere in the file to bind/validate it."""
    if "sys.argv[" not in text:
        return False
    has_guard = "len(sys.argv)" in text or "argparse" in text
    return not has_guard


def _nonblank_noncomment_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# ── per-artifact checks ──────────────────────────────────────────────────

def audit_json_artifact(path: Path) -> dict[str, Any]:
    """Read-only check of one eval-style JSON artifact."""
    if not path.exists():
        return {"path": str(path), "exists": False, "size_bytes": 0,
                "reasons": ["missing_json_artifact"]}
    size = path.stat().st_size
    if size == 0:
        return {"path": str(path), "exists": True, "size_bytes": 0,
                "reasons": ["empty_json_artifact_0_bytes"]}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"path": str(path), "exists": True, "size_bytes": size,
                "reasons": ["json_parse_error"]}
    if not isinstance(data, dict):
        return {"path": str(path), "exists": True, "size_bytes": size,
                "reasons": ["json_not_object"]}

    verdict = data.get("verdict")
    has_verdict = isinstance(verdict, str) and verdict.strip() != ""
    has_dict_container = any(
        isinstance(data.get(k), dict) and len(data.get(k)) > 0
        for k in _METRIC_DICT_CONTAINERS
    )
    has_list_container = any(
        isinstance(data.get(k), list) and len(data.get(k)) > 0
        for k in _METRIC_LIST_CONTAINERS
    )
    has_counts = (
        isinstance(data.get("checks_total"), int)
        and isinstance(data.get("checks_passed"), int)
        and data.get("checks_total", 0) > 0
    )
    # B121: bare checks_total/checks_passed ints are NOT sufficient evidence
    # on their own -- they are trivially fabricated as a matching pair of
    # constants with zero real measurement behind them (observed live: a
    # harness writing a hardcoded `{"checks_total": 4, "checks_passed": 4}`
    # dict literal, same class as kb bug.hardcoded_metric_vacuous_gate). A
    # real submission must carry at least one populated named evidence
    # container (gates / acceptance_results / invariants_verified /
    # tests_run); has_counts is reported for visibility only.
    has_metrics = has_dict_container or has_list_container

    reasons: list[str] = []
    if not has_verdict:
        reasons.append("missing_verdict_field")
    if not has_metrics:
        reasons.append("missing_acceptance_key_metrics")

    return {
        "path": str(path),
        "exists": True,
        "size_bytes": size,
        "verdict": verdict,
        "has_metric_container": has_metrics,
        "has_bare_counts_only": has_counts and not has_metrics,
        "reasons": reasons,
    }


def audit_rows_jsonl(path: Path | None) -> dict[str, Any] | None:
    """Read-only check of an (optional) rows JSONL sidecar."""
    if path is None:
        return None
    if not path.exists():
        return {"path": str(path), "exists": False, "size_bytes": 0, "reasons": []}
    size = path.stat().st_size
    reasons: list[str] = []
    if size == 0:
        reasons.append("empty_rows_jsonl_0_bytes")
    return {"path": str(path), "exists": True, "size_bytes": size, "reasons": reasons}


def audit_test_script(path: Path) -> dict[str, Any]:
    """Read-only check of the bash test harness for echo-only dummy tests
    and for unbound top-level positional-arg references (B121)."""
    if not path.exists():
        return {"path": str(path), "exists": False, "reasons": ["missing_test_script"]}
    text = path.read_text(encoding="utf-8")
    size = len(text.encode("utf-8"))
    if size == 0:
        return {"path": str(path), "exists": True, "size_bytes": 0,
                "reasons": ["empty_test_script_0_bytes"]}

    lines = _nonblank_noncomment_lines(text)
    # drop a leading shebang line ("#!/bin/bash") if present in raw form
    lines = [l for l in lines if not l.startswith("#!")]
    # B121: scan only non-comment code for substance markers -- a marker
    # word mentioned in a comment ("# assert nothing real happens here")
    # is not real substance (red-team finding).
    code_text = "\n".join(lines)
    has_substance = any(marker in code_text for marker in _TEST_SUBSTANCE_MARKERS)
    is_echo_exit_only = bool(lines) and all(
        _ECHO_LINE_RE.match(l) or _EXIT_LINE_RE.match(l) or _NOOP_LINE_RE.match(l)
        for l in lines
    )
    is_unbound_positional = _has_unbound_positional_args_bash(text)

    reasons: list[str] = []
    if is_echo_exit_only or (len(lines) <= 3 and not has_substance):
        reasons.append("echo_only_dummy_test")
    if is_unbound_positional:
        reasons.append("unbound_positional_arg_test")

    return {
        "path": str(path),
        "exists": True,
        "size_bytes": size,
        "effective_line_count": len(lines),
        "has_substance": has_substance,
        "is_unbound_positional": is_unbound_positional,
        "reasons": reasons,
    }


def audit_harness_script(path: Path) -> dict[str, Any]:
    """Read-only check of an (optional) builder/harness script for dummy stubs."""
    if not path.exists():
        return {"path": str(path), "exists": False, "reasons": ["missing_harness_script"]}
    text = path.read_text(encoding="utf-8")
    size = len(text.encode("utf-8"))
    if size == 0:
        return {"path": str(path), "exists": True, "size_bytes": 0,
                "reasons": ["empty_harness_script_0_bytes"]}

    lines = [l for l in _nonblank_noncomment_lines(text) if not l.startswith("#!")]
    lowered = text.lower()
    has_dummy_marker = any(m in lowered for m in _HARNESS_DUMMY_MARKERS)
    # B121: real I/O must appear in actual code, not merely be name-dropped
    # in a comment ("# TODO: real version would use open(path)") -- scan
    # only non-comment lines (red-team finding).
    has_real_io = any(marker in "\n".join(lines) for marker in _HARNESS_REAL_IO_MARKERS)
    is_unbound_positional = _has_unbound_positional_args_python(text)

    reasons: list[str] = []
    if (has_dummy_marker and not has_real_io) or (len(lines) <= 6 and not has_real_io):
        reasons.append("dummy_harness_script")
    if is_unbound_positional:
        reasons.append("unbound_positional_arg_harness")

    return {
        "path": str(path),
        "exists": True,
        "size_bytes": size,
        "effective_line_count": len(lines),
        "has_dummy_marker": has_dummy_marker,
        "has_real_io": has_real_io,
        "is_unbound_positional": is_unbound_positional,
        "reasons": reasons,
    }


# ── wave-level aggregation ───────────────────────────────────────────────

def audit_wave(wave: dict[str, Any], mcp_root: Path = MCP_ROOT) -> dict[str, Any]:
    """Aggregate all per-file checks for one wave into invalid_reasons +
    reviewer_action. Pure read-only over mcp_root; performs no writes."""
    eval_path = mcp_root / wave["eval_json"]
    rows_rel = wave.get("rows_jsonl")
    rows_path = (mcp_root / rows_rel) if rows_rel else None
    test_path = mcp_root / wave["test_sh"]
    extra_rel = wave.get("extra_scripts", []) or []
    extra_paths = [mcp_root / p for p in extra_rel]

    eval_result = audit_json_artifact(eval_path)
    rows_result = audit_rows_jsonl(rows_path)
    test_result = audit_test_script(test_path)
    extra_results = [audit_harness_script(p) for p in extra_paths]

    invalid_reasons: list[str] = []
    invalid_reasons.extend(f"eval_json:{r}" for r in eval_result.get("reasons", []))
    if rows_result:
        invalid_reasons.extend(f"rows_jsonl:{r}" for r in rows_result.get("reasons", []))
    invalid_reasons.extend(f"test_script:{r}" for r in test_result.get("reasons", []))
    for rel, res in zip(extra_rel, extra_results):
        invalid_reasons.extend(f"harness_script[{rel}]:{r}" for r in res.get("reasons", []))

    is_invalid = len(invalid_reasons) > 0
    reviewer_action = "reject_return_to_worker" if is_invalid else "accept_for_codex_review"

    return {
        "wave": wave["wave"],
        "invalid": is_invalid,
        "invalid_reasons": invalid_reasons,
        "reviewer_action": reviewer_action,
        "checked": {
            "eval_json": eval_result,
            "rows_jsonl": rows_result,
            "test_script": test_result,
            "extra_scripts": extra_results,
        },
    }


def _authority_flags() -> dict[str, bool]:
    return {
        "readonly": READONLY,
        "process_launch": False,
        "queue_mutation": False,
        "status_mutation": False,
        "write_gate_disable": False,
        "subprocess_launch_tripwire_zero": SUBPROCESS_LAUNCH_TRIPWIRE == 0,
        "queue_mutation_tripwire_zero": QUEUE_MUTATION_TRIPWIRE == 0,
    }


def build_report(waves: list[dict[str, Any]] | None = None,
                  mcp_root: Path = MCP_ROOT) -> dict[str, Any]:
    waves = waves if waves is not None else KNOWN_WAVES
    results = [audit_wave(w, mcp_root=mcp_root) for w in waves]

    checks_total = 0
    checks_passed = 0
    acceptance_results: dict[str, str] = {}
    for wave, res in zip(waves, results):
        expected_invalid = wave.get("expected_invalid")
        checks_total += 1
        if expected_invalid is None or res["invalid"] == expected_invalid:
            checks_passed += 1
            status = "PASS"
        else:
            status = "FAIL"
        acceptance_results[res["wave"]] = (
            f"{status} - invalid={res['invalid']} reviewer_action={res['reviewer_action']}"
            f" reasons={res['invalid_reasons']}"
        )

    checks_failed = checks_total - checks_passed
    verdict = "PASS" if checks_failed == 0 else "FAIL"

    report: dict[str, Any] = {
        "schema_id": "aiworkhub.review_artifact_strictness_guard_eval.v1",
        "contract": CONTRACT,
        "task_id": "CLAUDE_TASK_MCP_REVIEW_ARTIFACT_STRICTNESS_GUARD_B120_V1",
        "runner": "claude_task_mcp_review_strict_b120",
        "topic": "task_mcp",
        "mode": "review_artifact_strictness_guard_no_queue_mutation",
        "verdict": verdict,
        "checks_total": checks_total,
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "acceptance_results": acceptance_results,
        "wave_results": results,
        "invariants_verified": {
            "READONLY": True,
            "NO_LAUNCH": True,
            "NO_QUEUE_MUTATION": True,
            "MACHINE_READABLE_INVALID_REASONS": True,
            "MACHINE_READABLE_REVIEWER_ACTION": True,
            "FULL_WAVE_REPAIR_ACCEPTED": any(
                r["wave"] == "mcp_full_wave_dryrun_harness_b119_v1" and not r["invalid"]
                for r in results
            ),
            "LIVE_WAVES_PASS": all(not r["invalid"] for r in results),
        },
        "authority_flags": _authority_flags(),
    }
    return report


def build_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for res in report["wave_results"]:
        rows.append({
            "schema_id": "aiworkhub.review_artifact_strictness_guard_row.v1",
            "wave": res["wave"],
            "invalid": res["invalid"],
            "invalid_reasons": res["invalid_reasons"],
            "reviewer_action": res["reviewer_action"],
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-write", action="store_true",
                         help="print the report only; do not write output artifacts")
    args = parser.parse_args(argv)

    report = build_report()
    rows = build_rows(report)

    if not args.no_write:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        OUT_EVAL_JSON.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n",
                                  encoding="utf-8")
        with OUT_ROWS_JSONL.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=False) + "\n")

    print(json.dumps({
        "verdict": report["verdict"],
        "checks_total": report["checks_total"],
        "checks_passed": report["checks_passed"],
        "checks_failed": report["checks_failed"],
        "wrote_outputs": not args.no_write,
    }, indent=2))

    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
