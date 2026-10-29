"""TDQS reports become deterministic NeedFix drafts, and CI lints the MCP image with TDQS."""

from __future__ import annotations

import copy
import errno
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tdqs_report_to_needfix.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

_SPEC = importlib.util.spec_from_file_location("tdqs_report_to_needfix", SCRIPT)
assert _SPEC and _SPEC.loader
converter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(converter)

# tool -> [(rule, severity, message)]
TOOLS = {
    "task_create": [
        ("missing-param-description", "warning", "Parameter 'scope' has no description."),
        ("missing-param-description", "warning", "Parameter 'kind' has no description."),
        ("description-too-short", "error", "Description is 11 characters long."),
    ],
    "needfix_add": [
        ("missing-param-description", "warning", "Parameter 'evidence' has no description."),
        ("no-examples", "info", "No example invocation."),
    ],
    "kb_search": [("no-examples", "info", "No example invocation.")],
    "ai_memory_get": [],
}
SHADOW = [
    {
        "tool": "task_create",
        "invocationCost": 900,
        "cheaperSibling": "task_quick",
        "cheaperSiblingInvocationCost": 200,
    },
    {
        "tool": "kb_search",
        "invocationCost": 1200,
        "cheaperSibling": "kb_get",
        "cheaperSiblingInvocationCost": 300,
    },
]

EXPECTED_MARKDOWN = """\
### TDQS: AIWorkHub (4 tools, spec 0.1)

| Rule | Severity | Count |
| --- | --- | --- |
| description-too-short | error | 1 |
| missing-param-description | warning | 3 |
| no-examples | info | 2 |
| shadow-candidate | info | 2 |

Top 3 tools by finding count:

| Tool | Findings |
| --- | --- |
| task_create | 4 |
| kb_search | 2 |
| needfix_add | 2 |
"""


def make_report(tools=TOOLS, shadow=SHADOW):
    entries = [
        {
            "name": name,
            "score": 71,
            "findings": [
                {"rule": rule, "severity": severity, "tool": name, "message": message}
                for rule, severity, message in findings
            ],
        }
        for name, findings in tools.items()
    ]
    return {
        "specVersion": "0.1",
        "generatedAt": "2026-10-28T09:00:00Z",
        "server": {"name": "AIWorkHub", "toolCount": len(entries)},
        "tools": entries,
        # The flat top-level array repeats every per-tool finding.
        "findings": [dict(finding) for entry in entries for finding in entry["findings"]],
        "shadowCandidates": [dict(candidate) for candidate in shadow],
    }


def drafts_of(report):
    return converter.needfix_drafts(converter.summarize(report))


def by_rule(drafts):
    return {draft["evidence"]["rule"]: draft for draft in drafts}


def write(path, report):
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_findings_group_into_one_draft_per_rule_sorted_by_rule():
    drafts = drafts_of(make_report())
    assert [draft["evidence"]["rule"] for draft in drafts] == [
        "description-too-short",
        "missing-param-description",
        "no-examples",
        "shadow-candidate",
    ]
    draft = by_rule(drafts)["missing-param-description"]
    assert draft == {
        "title": "TDQS missing-param-description: 2 of 4 tools",
        "kind": "improvement",
        "severity": "medium",
        # Listed under its tool and again at top level, each finding counts once: 3, not 6.
        "description": (
            "TDQS rule missing-param-description (warning) reported 3 findings on 2 of 4 tools"
            " of MCP server AIWorkHub (TDQS spec 0.1).\n"
            "Affected tools: needfix_add, task_create.\n"
            "Example (needfix_add): Parameter 'evidence' has no description."
        ),
        "tags": ["tdqs", "mcp_quality", "missing-param-description"],
        "evidence": {
            "rule": "missing-param-description",
            "tools": ["needfix_add", "task_create"],
            "tdqs_spec_version": "0.1",
            "server": "AIWorkHub",
        },
    }
    assert by_rule(drafts)["no-examples"]["title"] == "TDQS no-examples: 2 of 4 tools"


def test_severity_maps_error_warning_info_to_high_medium_low():
    drafts = by_rule(drafts_of(make_report()))
    assert drafts["description-too-short"]["severity"] == "high"
    assert drafts["missing-param-description"]["severity"] == "medium"
    assert drafts["no-examples"]["severity"] == "low"


def test_the_most_severe_finding_decides_the_severity_of_its_rule():
    report = make_report({"alpha": [("r", "info", "a")], "beta": [("r", "error", "b")]}, [])
    assert [draft["severity"] for draft in drafts_of(report)] == ["high"]


def test_a_finding_listed_under_its_tool_and_at_top_level_counts_once():
    report = make_report({"alpha": [("r", "warning", "m")]}, [])
    # A per-tool finding may leave its tool implied; the top-level copy still names it.
    del report["tools"][0]["findings"][0]["tool"]
    report["findings"].append(
        {"rule": "too-many-tools", "severity": "warning", "tool": None, "message": "120 tools."}
    )
    drafts = by_rule(drafts_of(report))
    assert drafts["r"]["title"] == "TDQS r: 1 of 1 tools"
    assert "reported 1 finding on 1 of 1 tools" in drafts["r"]["description"]
    server_level = drafts["too-many-tools"]
    assert server_level["title"] == "TDQS too-many-tools: 0 of 1 tools"
    assert server_level["evidence"]["tools"] == []
    assert server_level["description"].endswith(
        "Affected tools: none (no finding names a tool).\nExample: 120 tools."
    )


def test_the_same_report_yields_byte_identical_output_whatever_its_order(tmp_path):
    report = make_report()
    reordered = copy.deepcopy(report)
    reordered["generatedAt"] = "2026-10-29T17:45:00Z"
    reordered["tools"].reverse()
    for tool in reordered["tools"]:
        tool["findings"].reverse()
    reordered["findings"].reverse()
    reordered["shadowCandidates"].reverse()
    path = write(tmp_path / "lint.json", report)
    other = write(tmp_path / "reordered.json", reordered)
    for flags in ([], ["--markdown"]):
        first, again, shuffled = (run_cli(target, *flags) for target in (path, path, other))
        assert first.returncode == again.returncode == shuffled.returncode == 0, first.stderr
        assert first.stdout == again.stdout == shuffled.stdout
    assert json.loads(run_cli(path).stdout) == drafts_of(report)


def test_output_writes_the_printed_bytes_to_a_file(tmp_path):
    path = write(tmp_path / "lint.json", make_report())
    target = tmp_path / "drafts.json"
    printed = run_cli(path)
    written = run_cli(path, "--output", target)
    assert printed.returncode == written.returncode == 0, written.stderr
    assert written.stdout == b""
    assert target.read_bytes() == printed.stdout


def test_shadow_candidates_become_one_draft_listing_each_cheaper_sibling():
    draft = by_rule(drafts_of(make_report()))["shadow-candidate"]
    assert draft["title"] == "TDQS shadow-candidate: 2 of 4 tools"
    assert draft["severity"] == "low"
    assert draft["tags"] == ["tdqs", "mcp_quality", "shadow-candidate"]
    assert draft["evidence"]["tools"] == ["kb_search", "task_create"]
    assert draft["description"].endswith(
        "Cheaper siblings (tool -> sibling): kb_search -> kb_get, task_create -> task_quick."
    )


def test_a_shadow_candidate_rule_already_in_the_findings_is_not_drafted_twice():
    message = "kb_get answers the same query for less."
    report = make_report({"kb_search": [("shadow-candidate", "warning", message)]})
    [draft] = [d for d in drafts_of(report) if d["evidence"]["rule"] == "shadow-candidate"]
    assert draft["severity"] == "medium"
    assert draft["evidence"]["tools"] == ["kb_search"]
    assert draft["description"].endswith(f"Example (kb_search): {message}")


def test_markdown_prints_the_rule_table_and_the_tools_with_most_findings(tmp_path):
    result = run_cli(write(tmp_path / "lint.json", make_report()), "--markdown")
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8") == EXPECTED_MARKDOWN


def test_markdown_lists_at_most_ten_tools_most_findings_first():
    tools = {
        f"tool_{index:02d}": [("r", "info", f"finding {n}") for n in range(index)]
        for index in range(1, 13)
    }
    markdown = converter.render_markdown(converter.summarize(make_report(tools, [])))
    rows = [line for line in markdown.splitlines() if line.startswith("| tool_")]
    assert rows == [f"| tool_{index:02d} | {index} |" for index in range(12, 2, -1)]
    assert "Top 10 tools by finding count:" in markdown


def test_markdown_keeps_untrusted_names_inside_their_cells():
    report = make_report({"odd|tool\nname": [("odd|rule", "error", "m")]}, [])
    markdown = converter.render_markdown(converter.summarize(report))
    assert "| odd\\|rule | error | 1 |" in markdown
    assert "| odd\\|tool name | 1 |" in markdown


def test_markdown_shows_a_lone_surrogate_as_its_escape(tmp_path):
    # A JSON escape can carry a lone UTF-16 surrogate, which UTF-8 cannot encode.
    report = make_report({"tool\udc80": [("rule\udc80", "error", "m")]}, [])
    report["server"]["name"] = "AIWorkHub\udc80"
    report["specVersion"] = "0.1\udc80"
    path = write(tmp_path / "lint.json", report)
    result = run_cli(path, "--markdown")
    assert result.returncode == 0, result.stderr
    markdown = result.stdout.decode("utf-8")  # strict: the step summary stays valid UTF-8
    assert markdown.startswith("### TDQS: AIWorkHub\\udc80 (1 tools, spec 0.1\\udc80)\n")
    assert "| rule\\udc80 | error | 1 |" in markdown
    assert "| tool\\udc80 | 1 |" in markdown
    drafts = run_cli(path)
    assert drafts.returncode == 0, drafts.stderr
    assert json.loads(drafts.stdout)[0]["evidence"]["tools"] == ["tool\udc80"]


def test_a_report_without_findings_says_so():
    report = make_report({"alpha": []}, [])
    assert drafts_of(report) == []
    markdown = converter.render_markdown(converter.summarize(report))
    assert markdown.endswith("\n\nNo findings.\n")


def _mutated(mutate):
    def build():
        report = make_report()
        mutate(report)
        return json.dumps(report).encode("utf-8")

    return build


INVALID_REPORTS = {
    "not json": (lambda: b"tdqs crashed before writing", "is not a JSON document"),
    "not an object": (lambda: b"[]", "must hold a JSON object, got an array"),
    "no server": (
        _mutated(lambda report: report.pop("server")),
        "server must be an object, got nothing",
    ),
    "no tool linted": (
        _mutated(lambda report: report["server"].update(toolCount=0)),
        "server.toolCount must be a positive integer, got 0",
    ),
    "unknown severity": (
        _mutated(lambda report: report["tools"][0]["findings"][0].update(severity="fatal")),
        "tools[0].findings[0].severity must be one of info, warning, error, got 'fatal'",
    ),
    "no findings array": (
        _mutated(lambda report: report.pop("findings")),
        "findings must be an array, got nothing",
    ),
    "finding without rule": (
        _mutated(lambda report: report["findings"][1].pop("rule")),
        "findings[1].rule must be a non-empty string, got nothing",
    ),
    "shadow candidate without sibling": (
        _mutated(lambda report: report["shadowCandidates"][0].pop("cheaperSibling")),
        "shadowCandidates[0].cheaperSibling must be a non-empty string, got nothing",
    ),
}


@pytest.mark.parametrize("case", sorted(INVALID_REPORTS))
def test_an_invalid_report_exits_2_naming_the_cause_on_one_line(tmp_path, capsys, case):
    build, cause = INVALID_REPORTS[case]
    path = tmp_path / "lint.json"
    path.write_bytes(build())
    assert converter.main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    [line] = captured.err.splitlines()
    assert line.startswith("tdqs_report_to_needfix: ")
    assert cause in line


def test_an_unreadable_report_exits_2(tmp_path, capsys):
    assert converter.main([str(tmp_path / "missing.json")]) == 2
    assert converter.main([str(tmp_path)]) == 2  # a directory, not a report
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert all(line.startswith("tdqs_report_to_needfix: cannot read ") for line in lines)


def test_a_report_over_the_size_limit_is_refused(tmp_path, capsys, monkeypatch):
    path = write(tmp_path / "lint.json", make_report())
    monkeypatch.setattr(converter, "MAX_REPORT_BYTES", 64)
    assert converter.main([str(path)]) == 2
    assert "exceeds the 64-byte report limit" in capsys.readouterr().err


def test_the_cli_exits_2_with_one_stderr_line_on_invalid_input(tmp_path):
    path = tmp_path / "lint.json"
    path.write_bytes(b'{"server": ')
    result = run_cli(path)
    assert result.returncode == 2
    assert result.stdout == b""
    [line] = result.stderr.decode("ascii").splitlines()
    assert line.startswith("tdqs_report_to_needfix: ")
    assert "is not a JSON document" in line


def test_an_unwritable_output_exits_1_with_one_stderr_line(tmp_path):
    path = write(tmp_path / "lint.json", make_report())
    result = run_cli(path, "--output", tmp_path)  # a directory, not a file
    assert result.returncode == 1
    assert result.stdout == b""
    [line] = result.stderr.decode("ascii").splitlines()
    assert line.startswith("tdqs_report_to_needfix: cannot write ")
    assert tmp_path.name in line


class _RefusingStream(io.RawIOBase):
    """A raw stream refusing every write, as a full disk does."""

    def writable(self):
        return True

    def write(self, data):
        raise OSError(errno.ENOSPC, "No space left on device")


def test_an_unwritable_stdout_exits_1_with_one_stderr_line(tmp_path, capsys, monkeypatch):
    path = write(tmp_path / "lint.json", make_report())
    stdout = io.TextIOWrapper(io.BufferedWriter(_RefusingStream()), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", stdout)
    assert converter.main([str(path)]) == 1
    [line] = capsys.readouterr().err.splitlines()
    assert line == "tdqs_report_to_needfix: cannot write stdout: No space left on device"
    # Closed, so the exit-time flush of stdout cannot fail again on the bytes left buffered.
    assert stdout.closed


def test_a_stdout_pipe_nobody_reads_exits_1_with_one_stderr_line(tmp_path):
    path = write(tmp_path / "lint.json", make_report())
    reader, writer = os.pipe()
    os.close(reader)  # without a reader every write to the pipe fails
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(path), "--markdown"],
            stdout=writer,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    finally:
        os.close(writer)
    # 1, not the 120 Python exits with when its exit-time flush of stdout fails as well.
    assert result.returncode == 1, result.stderr
    [line] = result.stderr.decode("ascii").splitlines()
    assert line.startswith("tdqs_report_to_needfix: cannot write stdout: ")


def test_drafts_fit_the_needfix_store_vocabulary_and_bounds():
    # The script runs before anything is installed, so it names a subset of the store's
    # vocabulary instead of importing it; this pins that subset to the owner.
    needfix_store = pytest.importorskip("aiworkhub.needfix_store")
    assert set(converter.NEEDFIX_SEVERITY.values()) <= set(needfix_store.SEVERITIES)
    assert converter.NEEDFIX_KIND in needfix_store.KINDS
    for draft in drafts_of(make_report()):
        needfix_store._validate_payload(
            draft["title"],
            draft["description"],
            draft["evidence"],
            kind=draft["kind"],
            severity=draft["severity"],
            tags=draft["tags"],
        )


def _job(name):
    """The lines of one ci.yml job, from its header to the next job or top-level key."""
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:")
    end = next(
        (index for index in range(start + 1, len(lines)) if re.match(r" {0,2}\S", lines[index])),
        len(lines),
    )
    return lines[start:end]


def _steps(job):
    """Each step of a job as one block of text, in order, without comments."""
    steps = []
    for line in job:
        if line.lstrip().startswith("#"):
            continue
        if line.startswith("      - "):
            steps.append([])
        if steps:
            steps[-1].append(line)
    return ["\n".join(step).rstrip() for step in steps]


def _only(steps, needle):
    matches = [index for index, step in enumerate(steps) if needle in step]
    assert len(matches) == 1, f"expected one docker-smoke step with {needle!r}, got {matches}"
    return matches[0]


def test_ci_lints_the_mcp_image_with_pinned_tdqs_and_never_fails_on_findings():
    steps = _steps(_job("docker-smoke"))
    smoke = _only(steps, "scripts/mcp_stdio_smoke.py -- docker run -i --rm aiworkhub-mcp:ci")
    install = _only(steps, "run: python3 -m pip install tdqs==0.1.0")
    lint = _only(
        steps,
        'run: tdqs lint --command "docker run -i --rm -e AIWORKHUB_MCP_SURFACE=all'
        ' aiworkhub-mcp:ci"',
    )
    summary = _only(
        steps,
        "run: python3 scripts/tdqs_report_to_needfix.py tdqs-lint.json --markdown"
        ' >> "$GITHUB_STEP_SUMMARY"',
    )
    upload = _only(steps, "path: tdqs-lint.json")
    assert smoke < install < lint < summary < upload
    for flag in ("--server-name AIWorkHub", "--format json", "--fail-on never"):
        assert flag in steps[lint]
    assert steps[lint].endswith("--output tdqs-lint.json")
    for line in ("uses: actions/upload-artifact@v6", "name: tdqs-report", "if-no-files-found: error"):
        assert line in steps[upload]
    commands = [line.strip() for step in steps for line in step.splitlines()]
    tdqs_runs = [command for command in commands if command.startswith("run: tdqs ")]
    assert len(tdqs_runs) == 2 and all("--fail-on never" in run for run in tdqs_runs)
    installs = [command for command in commands if "pip install" in command and "tdqs" in command]
    assert installs == ["run: python3 -m pip install tdqs==0.1.0"]


def test_ci_scores_on_tdqs_dev_only_with_the_secret_and_adds_it_to_the_report():
    steps = _steps(_job("docker-smoke"))
    score = _only(steps, "run: tdqs score")
    for line in (
        "id: tdqs_score",
        "env.TDQS_API_KEY != ''",
        "continue-on-error: true",
        "TDQS_API_KEY: ${{ secrets.TDQS_API_KEY }}",
        '--command "docker run -i --rm -e AIWORKHUB_MCP_SURFACE=all aiworkhub-mcp:ci"',
        "--hosted https://tdqs.dev",
        "--format json",
        "--fail-on never",
        "--output tdqs-score.json",
    ):
        assert line in steps[score]
    # Only the hosted step sees the secret.
    assert CI_WORKFLOW.read_text(encoding="utf-8").count("secrets.TDQS_API_KEY") == 1
    upload = _only(steps, "if-no-files-found: ignore")
    assert _only(steps, "path: tdqs-lint.json") < score < upload
    for line in (
        "steps.tdqs_score.outcome == 'success'",
        "uses: actions/upload-artifact@v6",
        "name: tdqs-report",
        "tdqs-lint.json",
        "tdqs-score.json",
        "overwrite: true",
    ):
        assert line in steps[upload]


def test_ci_keeps_the_tdqs_report_when_its_summary_step_fails():
    # A condition without a status function implies success(), which would skip the upload
    # and the hosted score exactly when the summary step fails on the report.
    steps = _steps(_job("docker-smoke"))
    summary = _only(steps, "run: python3 scripts/tdqs_report_to_needfix.py")
    expected = {
        "path: tdqs-lint.json": "if: ${{ !cancelled() }}",
        "run: tdqs score": "if: ${{ !cancelled() && env.TDQS_API_KEY != '' }}",
        "if-no-files-found: ignore": (
            "if: ${{ !cancelled() && steps.tdqs_score.outcome == 'success' }}"
        ),
    }
    for needle, condition in expected.items():
        step = _only(steps, needle)
        lines = [line.strip() for line in steps[step].splitlines()]
        assert summary < step
        assert [line for line in lines if line.startswith("if: ")] == [condition]
