#!/usr/bin/env python3
"""Turn a TDQS lint or score report into deterministic NeedFix drafts.

Usage: python scripts/tdqs_report_to_needfix.py REPORT.json [--markdown] [--output PATH]

TDQS (Tool Definition Quality Score, https://tdqs.dev) grades the tool definitions an MCP
server publishes and reports findings per tool. Each rule becomes one NeedFix draft, sorted
by rule. TDQS severity error, warning and info becomes high, medium and low; the most severe
finding of a rule decides. The description names the affected tools in sorted order with the
first message as an example. ``shadowCandidates`` (tools with a cheaper sibling) become one
``shadow-candidate`` draft unless the findings already carry that rule. A finding listed both
under its tool and in the top-level ``findings`` array counts once. ``--markdown`` prints a
short summary for ``$GITHUB_STEP_SUMMARY`` instead of the drafts: a rule | severity | count
table and the tools with the most findings.

Output goes to stdout, or to ``--output PATH``. Exit status: 0 success, 1 the output cannot be
written, 2 the report is unreadable or invalid; a failure names its cause on one stderr line.
Standard library only, no network and no database writes: drafts are printed, never filed.
The same report always yields byte-identical output; neither ``generatedAt`` nor the order of
the report's arrays changes it.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

MAX_REPORT_BYTES = 64 * 1024 * 1024
# TDQS severity -> NeedFix severity, least severe first: the order ranks a rule's findings.
NEEDFIX_SEVERITY = {"info": "low", "warning": "medium", "error": "high"}
NEEDFIX_KIND = "improvement"
NEEDFIX_TAGS = ("tdqs", "mcp_quality")
SHADOW_RULE = "shadow-candidate"
# shadowCandidates carry no severity of their own: a cheaper sibling is advice, not a defect.
SHADOW_SEVERITY = "info"
TOP_TOOLS = 10

_RANK = {severity: rank for rank, severity in enumerate(NEEDFIX_SEVERITY)}
_MISSING = object()


class ReportError(ValueError):
    """The report is unreadable or not shaped like a TDQS report."""


class Finding(NamedTuple):
    rule: str
    tool: str | None  # None: the finding names no tool
    severity: str
    message: str


class RuleGroup(NamedTuple):
    """Every finding of one rule; each group becomes one NeedFix draft."""

    rule: str
    severity: str  # the most severe TDQS severity among the findings
    count: int
    tools: tuple[str, ...]  # affected tool names, sorted
    example: Finding | None  # the first finding in tool, message order
    siblings: tuple[tuple[str, str], ...] = ()  # (tool, cheaper sibling) for shadow-candidate


class Summary(NamedTuple):
    server: str
    tool_count: int
    spec_version: str
    groups: tuple[RuleGroup, ...]  # sorted by rule
    tool_findings: tuple[tuple[str, int], ...]  # (tool, findings), most findings first


def _shown(value: object) -> str:
    """A short rendering of an offending value for a one-line error."""
    if value is _MISSING:
        return "nothing"
    if isinstance(value, str):
        text = ascii(value)
        return text if len(text) <= 80 else f"{text[:77]}..."
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "an array"
    return json.dumps(value)


def _text(value: object, where: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise ReportError(f"{where} must be a non-empty string, got {_shown(value)}")


def _array(
    container: dict[str, Any], key: str, where: str, *, required: bool = True
) -> list[Any]:
    value = container.get(key, _MISSING)
    if value is _MISSING and not required:
        return []
    if not isinstance(value, list):
        raise ReportError(f"{where} must be an array, got {_shown(value)}")
    return value


def _finding(raw: object, where: str, owner: str | None) -> Finding:
    """Validate one finding; ``owner`` names the tool it is listed under, if any."""
    if not isinstance(raw, dict):
        raise ReportError(f"{where} must be an object, got {_shown(raw)}")
    rule = _text(raw.get("rule", _MISSING), f"{where}.rule")
    severity = raw.get("severity", _MISSING)
    if not isinstance(severity, str) or severity not in NEEDFIX_SEVERITY:
        allowed = ", ".join(NEEDFIX_SEVERITY)
        raise ReportError(f"{where}.severity must be one of {allowed}, got {_shown(severity)}")
    message = raw.get("message", _MISSING)
    if not isinstance(message, str):
        raise ReportError(f"{where}.message must be a string, got {_shown(message)}")
    tool = raw.get("tool")
    named = owner if tool is None else _text(tool, f"{where}.tool")
    return Finding(rule, named, severity, message)


def _sibling(raw: object, where: str) -> tuple[str, str]:
    """Validate one shadow candidate as (tool, cheaper sibling)."""
    if not isinstance(raw, dict):
        raise ReportError(f"{where} must be an object, got {_shown(raw)}")
    return (
        _text(raw.get("tool", _MISSING), f"{where}.tool"),
        _text(raw.get("cheaperSibling", _MISSING), f"{where}.cheaperSibling"),
    )


def _example_order(row: tuple[Finding, int]) -> tuple[bool, str, str, str]:
    # A total order over a rule's distinct findings. Findings naming no tool sort last, so
    # the example comes from the first affected tool.
    finding = row[0]
    return finding.tool is None, finding.tool or "", finding.message, finding.severity


def _group(rule: str, rows: list[tuple[Finding, int]]) -> RuleGroup:
    rows.sort(key=_example_order)
    return RuleGroup(
        rule=rule,
        severity=max((finding.severity for finding, _ in rows), key=_RANK.__getitem__),
        count=sum(count for _, count in rows),
        tools=tuple(sorted({finding.tool for finding, _ in rows if finding.tool is not None})),
        example=rows[0][0],
    )


def summarize(report: dict[str, Any]) -> Summary:
    """Validate a parsed report and group its findings by rule."""
    server = report.get("server", _MISSING)
    if not isinstance(server, dict):
        raise ReportError(f"server must be an object, got {_shown(server)}")
    name = _text(server.get("name", _MISSING), "server.name")
    tool_count = server.get("toolCount", _MISSING)
    if type(tool_count) is not int or tool_count < 1:
        shown = _shown(tool_count)
        raise ReportError(f"server.toolCount must be a positive integer, got {shown}")
    spec_version = _text(report.get("specVersion", _MISSING), "specVersion")

    under_tools: collections.Counter[Finding] = collections.Counter()
    for index, tool in enumerate(_array(report, "tools", "tools")):
        where = f"tools[{index}]"
        if not isinstance(tool, dict):
            raise ReportError(f"{where} must be an object, got {_shown(tool)}")
        owner = _text(tool.get("name", _MISSING), f"{where}.name")
        for position, raw in enumerate(_array(tool, "findings", f"{where}.findings")):
            under_tools[_finding(raw, f"{where}.findings[{position}]", owner)] += 1
    at_top = collections.Counter(
        _finding(raw, f"findings[{position}]", None)
        for position, raw in enumerate(_array(report, "findings", "findings"))
    )
    # The top-level array may repeat the per-tool findings, so a finding counts as often as
    # the one section listing it most often does, never as the sum of both.
    findings = under_tools | at_top

    rows: dict[str, list[tuple[Finding, int]]] = collections.defaultdict(list)
    per_tool: collections.Counter[str] = collections.Counter()
    for finding, count in findings.items():
        rows[finding.rule].append((finding, count))
        if finding.tool is not None:
            per_tool[finding.tool] += count
    groups = [_group(rule, rule_rows) for rule, rule_rows in rows.items()]

    candidates = _array(report, "shadowCandidates", "shadowCandidates", required=False)
    siblings = sorted(
        {_sibling(raw, f"shadowCandidates[{position}]") for position, raw in enumerate(candidates)}
    )
    if siblings and SHADOW_RULE not in rows:
        shadowed = tuple(sorted({tool for tool, _ in siblings}))
        groups.append(
            RuleGroup(SHADOW_RULE, SHADOW_SEVERITY, len(siblings), shadowed, None, tuple(siblings))
        )
        per_tool.update(tool for tool, _ in siblings)
    return Summary(
        server=name,
        tool_count=tool_count,
        spec_version=spec_version,
        groups=tuple(sorted(groups, key=lambda group: group.rule)),
        tool_findings=tuple(sorted(per_tool.items(), key=lambda item: (-item[1], item[0]))),
    )


def _description(group: RuleGroup, summary: Summary) -> str:
    noun = "finding" if group.count == 1 else "findings"
    lines = [
        f"TDQS rule {group.rule} ({group.severity}) reported {group.count} {noun} on "
        f"{len(group.tools)} of {summary.tool_count} tools of MCP server {summary.server} "
        f"(TDQS spec {summary.spec_version}).",
        f"Affected tools: {', '.join(group.tools) or 'none (no finding names a tool)'}.",
    ]
    if group.siblings:
        pairs = ", ".join(f"{tool} -> {sibling}" for tool, sibling in group.siblings)
        lines.append(f"Cheaper siblings (tool -> sibling): {pairs}.")
    elif group.example is not None:
        example = group.example
        where = "" if example.tool is None else f" ({example.tool})"
        lines.append(f"Example{where}: {example.message}")
    return "\n".join(lines)


def needfix_drafts(summary: Summary) -> list[dict[str, Any]]:
    """One NeedFix draft per rule, in rule order; nothing is filed."""
    return [
        {
            "title": f"TDQS {group.rule}: {len(group.tools)} of {summary.tool_count} tools",
            "kind": NEEDFIX_KIND,
            "severity": NEEDFIX_SEVERITY[group.severity],
            "description": _description(group, summary),
            "tags": [*NEEDFIX_TAGS, group.rule],
            "evidence": {
                "rule": group.rule,
                "tools": list(group.tools),
                "tdqs_spec_version": summary.spec_version,
                "server": summary.server,
            },
        }
        for group in summary.groups
    ]


def render_json(summary: Summary) -> str:
    return json.dumps(needfix_drafts(summary), indent=2) + "\n"


def _cell(text: str) -> str:
    """Untrusted text on one line, kept inside its Markdown table cell."""
    cell = " ".join(text.split()).replace("\\", "\\\\").replace("|", "\\|")
    # JSON can carry a lone UTF-16 surrogate, which UTF-8 cannot encode: write its escape.
    return cell.encode("utf-8", "backslashreplace").decode("utf-8")


def render_markdown(summary: Summary) -> str:
    """The step summary: one row per rule, then the tools with the most findings."""
    lines = [
        f"### TDQS: {_cell(summary.server)} "
        f"({summary.tool_count} tools, spec {_cell(summary.spec_version)})",
        "",
    ]
    if not summary.groups:
        return "\n".join([*lines, "No findings."]) + "\n"
    lines += ["| Rule | Severity | Count |", "| --- | --- | --- |"]
    lines += [
        f"| {_cell(group.rule)} | {group.severity} | {group.count} |" for group in summary.groups
    ]
    top = summary.tool_findings[:TOP_TOOLS]
    if top:
        lines += ["", f"Top {len(top)} tools by finding count:", ""]
        lines += ["| Tool | Findings |", "| --- | --- |"]
        lines += [f"| {_cell(tool)} | {count} |" for tool, count in top]
    return "\n".join(lines) + "\n"


def load_report(path: Path) -> dict[str, Any]:
    """Parse one report of at most MAX_REPORT_BYTES; it must hold a JSON object."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
    except OSError as error:
        raise ReportError(f"cannot read {path}: {error.strerror or error}") from None
    if len(raw) > MAX_REPORT_BYTES:
        raise ReportError(f"{path} exceeds the {MAX_REPORT_BYTES}-byte report limit")
    try:
        report = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise ReportError(f"{path} is not a JSON document: {error}") from None
    if not isinstance(report, dict):
        raise ReportError(f"{path} must hold a JSON object, got {_shown(report)}")
    return report


def _fail(message: str, status: int) -> int:
    # One ASCII line, so the cause never spans lines or garbles a non-UTF-8 stderr.
    line = " ".join(message.split()).encode("ascii", "backslashreplace").decode("ascii")
    print(f"tdqs_report_to_needfix: {line}", file=sys.stderr)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tdqs_report_to_needfix.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("report", type=Path, help="a tdqs lint or score report (JSON)")
    parser.add_argument(
        "--markdown", action="store_true", help="print the step summary instead of the drafts"
    )
    parser.add_argument("--output", type=Path, metavar="PATH", help="write to PATH, not stdout")
    args = parser.parse_args(argv)
    try:
        summary = summarize(load_report(args.report))
    except ReportError as error:
        return _fail(str(error), 2)
    text = render_markdown(summary) if args.markdown else render_json(summary)
    # Bytes, not text: no platform newline or encoding translation touches the output.
    data = text.encode("utf-8")
    if args.output is not None:
        try:
            args.output.write_bytes(data)
        except OSError as error:
            return _fail(f"cannot write {args.output}: {error.strerror or error}", 1)
        return 0
    try:
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except OSError as error:
        # Closing drops the bytes the broken stream still buffers: the interpreter flushes
        # stdout again at exit and would fail on them, with a traceback and exit status 120.
        with contextlib.suppress(OSError):
            sys.stdout.close()
        return _fail(f"cannot write stdout: {error.strerror or error}", 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
