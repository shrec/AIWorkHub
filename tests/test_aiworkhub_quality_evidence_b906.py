from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

qe = importlib.import_module("aiworkhub.quality_evidence")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# --- detection ---------------------------------------------------------


def test_detect_languages_exact_manifest(tmp_path):
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    langs = qe.detect_languages(repo)
    assert langs["python"] is True
    assert langs["node"] is False


def test_detect_declared_tools_exact_config(tmp_path):
    repo = _repo(tmp_path)
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    declared = qe.detect_declared_tools(repo)
    assert declared["pytest"] is True
    assert declared["eslint"] is False


def test_zero_config_profile_never_installs(tmp_path):
    repo = _repo(tmp_path)
    (repo / "package.json").write_text("{}", encoding="utf-8")
    profile = qe.build_zero_config_profile(repo)
    assert profile["languages"]["node"] is True
    # runnable_tools only includes tools that are both declared AND installed
    for tool in profile["runnable_tools"]:
        assert profile["declared_tools"][tool] is True
        assert profile["installed_tools"][tool] is True


# --- status semantics ----------------------------------------------------


def test_not_available_is_never_passed():
    check = qe.EvidenceCheck(check_id="x", kind="test", status=qe.STATUS_NOT_AVAILABLE)
    d = check.to_dict()
    assert d["status"] == qe.STATUS_NOT_AVAILABLE
    assert d["status"] != qe.STATUS_PASSED


def test_invalid_status_rejected():
    with pytest.raises(qe.MalformedConfigError):
        qe.EvidenceCheck(check_id="x", kind="test", status="bogus")


def test_missing_command_binary_is_not_available(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": [{"id": "nope", "kind": "test", "command": ["definitely-not-a-real-binary-xyz"]}]}),
        encoding="utf-8",
    )
    checks = qe.run_declared_checks(repo)
    assert len(checks) == 1
    assert checks[0].status == qe.STATUS_NOT_AVAILABLE


def test_passing_command_reports_passed(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": [{"id": "true-check", "kind": "build", "command": ["python3", "-c", "pass"]}]}),
        encoding="utf-8",
    )
    checks = qe.run_declared_checks(repo)
    assert checks[0].status == qe.STATUS_PASSED


def test_failing_command_reports_failed(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": [{"id": "fail-check", "kind": "test", "command": ["python3", "-c", "import sys; sys.exit(1)"]}]}),
        encoding="utf-8",
    )
    checks = qe.run_declared_checks(repo)
    assert checks[0].status == qe.STATUS_FAILED


# --- malformed config: fail closed --------------------------------------


def test_malformed_json_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(qe.MalformedConfigError):
        qe.load_repo_config(repo)


def test_config_not_object_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text("[]", encoding="utf-8")
    with pytest.raises(qe.MalformedConfigError):
        qe.load_repo_config(repo)


def test_checks_not_array_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(json.dumps({"checks": "nope"}), encoding="utf-8")
    with pytest.raises(qe.MalformedConfigError):
        qe.load_repo_config(repo)


def test_check_missing_id_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": [{"kind": "test", "command": ["echo"]}]}), encoding="utf-8"
    )
    with pytest.raises(qe.MalformedConfigError):
        qe.load_repo_config(repo)


def test_check_invalid_kind_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": [{"id": "x", "kind": "not_a_kind", "command": ["echo"]}]}), encoding="utf-8"
    )
    with pytest.raises(qe.MalformedConfigError):
        qe.load_repo_config(repo)


def test_missing_config_is_empty_not_error(tmp_path):
    repo = _repo(tmp_path)
    config = qe.load_repo_config(repo)
    assert config == {"checks": []}


def test_missing_or_empty_config_status_is_explicitly_unverified(tmp_path):
    repo = _repo(tmp_path)
    assert qe.repo_config_status(repo) == {
        "config_present": False,
        "declared_check_count": 0,
        "status": "unverified",
        "reason": "quality_config_missing",
    }
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": []}), encoding="utf-8"
    )
    assert qe.repo_config_status(repo) == {
        "config_present": True,
        "declared_check_count": 0,
        "status": "unverified",
        "reason": "quality_checks_empty",
    }


def test_portable_python_placeholder_uses_current_interpreter(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "id": "portable-python",
                        "kind": "test",
                        "command": ["{python}", "-c", "raise SystemExit(0)"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    checks = qe.run_declared_checks(repo)
    assert checks[0].status == qe.STATUS_PASSED


def test_evidence_packet_never_reports_missing_policy_as_green(tmp_path):
    repo = _repo(tmp_path)
    packet = qe.build_evidence_packet(repo)
    assert packet["ok"] is False
    assert packet["status"] == "unverified"
    assert packet["repository_quality_policy"]["reason"] == "quality_config_missing"


# --- command array safety (shell=False only) ----------------------------


def test_command_must_be_array_of_strings_not_shell_string(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": [{"id": "x", "kind": "test", "command": "echo hi"}]}), encoding="utf-8"
    )
    with pytest.raises(qe.MalformedConfigError):
        qe.load_repo_config(repo)


def test_command_empty_array_rejected(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": [{"id": "x", "kind": "test", "command": []}]}), encoding="utf-8"
    )
    with pytest.raises(qe.MalformedConfigError):
        qe.load_repo_config(repo)


def test_shell_metacharacters_not_interpreted(tmp_path):
    # A shell string would expand `$(...)`; as an argv array it must be a
    # literal argument passed to the (nonexistent) binary, never executed.
    repo = _repo(tmp_path)
    marker = tmp_path / "should_not_exist.txt"
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "id": "x",
                        "kind": "test",
                        "command": ["echo", f"$(touch {marker})"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qe.run_declared_checks(repo)
    assert not marker.exists()


# --- adapter normalization ------------------------------------------------


def test_adapt_sarif_no_results_is_passed():
    doc = {"runs": [{"results": []}]}
    check = qe.adapt_sarif("sarif-check", doc)
    assert check.status == qe.STATUS_PASSED


def test_adapt_sarif_with_results_is_failed():
    doc = {
        "runs": [
            {
                "results": [
                    {"locations": [{"physicalLocation": {"artifactLocation": {"uri": "a.py"}}}]}
                ]
            }
        ]
    }
    check = qe.adapt_sarif("sarif-check", doc)
    assert check.status == qe.STATUS_FAILED
    assert check.affected_paths == ("a.py",)


def test_adapt_junit_xml_all_pass():
    xml = '<testsuite tests="2" failures="0" errors="0"></testsuite>'
    check = qe.adapt_junit_xml("junit-check", xml)
    assert check.status == qe.STATUS_PASSED


def test_adapt_junit_xml_with_failure():
    xml = (
        '<testsuite tests="1" failures="1" errors="0">'
        '<testcase classname="mod.Test" name="t"><failure/></testcase>'
        "</testsuite>"
    )
    check = qe.adapt_junit_xml("junit-check", xml)
    assert check.status == qe.STATUS_FAILED
    assert "mod.Test" in check.affected_paths


def test_adapt_junit_xml_malformed_is_failed():
    check = qe.adapt_junit_xml("junit-check", "<not-xml")
    assert check.status == qe.STATUS_FAILED


def test_adapt_junit_xml_zero_tests_is_skipped():
    xml = '<testsuite tests="0" failures="0" errors="0"></testsuite>'
    check = qe.adapt_junit_xml("junit-check", xml)
    assert check.status == qe.STATUS_SKIPPED


def test_adapt_coverage_summary_passes_threshold():
    doc = {"total": {"lines": {"pct": 85.0}}}
    check = qe.adapt_coverage_summary("cov", doc, min_percent=80.0)
    assert check.status == qe.STATUS_PASSED


def test_adapt_coverage_summary_below_threshold_fails():
    doc = {"total": {"lines": {"pct": 40.0}}}
    check = qe.adapt_coverage_summary("cov", doc, min_percent=80.0)
    assert check.status == qe.STATUS_FAILED


def test_adapt_coverage_summary_missing_field_not_available():
    check = qe.adapt_coverage_summary("cov", {})
    assert check.status == qe.STATUS_NOT_AVAILABLE


def test_adapt_benchmark_json_with_entries():
    doc = {"benchmarks": [{"name": "b1"}]}
    check = qe.adapt_benchmark_json("bench", doc)
    assert check.status == qe.STATUS_PASSED


def test_adapt_benchmark_json_missing_field_not_available():
    check = qe.adapt_benchmark_json("bench", {})
    assert check.status == qe.STATUS_NOT_AVAILABLE


def test_adapt_ai_reviewer_findings_bounded():
    findings = [{"file": f"f{i}.py"} for i in range(100)]
    check = qe.adapt_ai_reviewer_findings("ai-review", findings, max_findings=10)
    assert "10 bounded" in check.summary
    assert check.status == qe.STATUS_FAILED


def test_adapt_ai_reviewer_findings_empty_is_passed():
    check = qe.adapt_ai_reviewer_findings("ai-review", [])
    assert check.status == qe.STATUS_PASSED


# --- risk-based optional gates: absence != failure, absence != pass -----


def test_optional_gate_missing_binary_not_available(tmp_path):
    repo = _repo(tmp_path)
    check = qe.optional_gate_status(repo, "codeql")
    assert check.status in {qe.STATUS_NOT_AVAILABLE, qe.STATUS_SKIPPED}
    assert check.status != qe.STATUS_PASSED
    assert check.status != qe.STATUS_FAILED


def test_optional_gate_unknown_gate_rejected(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(qe.MalformedConfigError):
        qe.optional_gate_status(repo, "not-a-real-gate")


# --- quality_reviewer contract: read-only, cannot mutate ------------------


def test_quality_reviewer_contract_is_read_only():
    contract = qe.quality_reviewer_contract()
    assert contract["read_only"] is True
    assert contract["can_mutate_repo"] is False


# --- full evidence packet -------------------------------------------------


def test_build_evidence_packet_shape(tmp_path):
    repo = _repo(tmp_path)
    packet = qe.build_evidence_packet(repo, changed_paths=["a.py"])
    assert packet["schema_id"] == qe.SCHEMA_ID
    assert "profile" in packet
    assert "declared_checks" in packet
    assert "optional_gates" in packet
    assert packet["quality_reviewer_contract"]["can_mutate_repo"] is False


def test_build_evidence_packet_malformed_config_surfaces_error(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text("{bad json", encoding="utf-8")
    packet = qe.build_evidence_packet(repo)
    assert packet["declared_checks"] == []
    assert packet["config_error"]


def test_build_evidence_packet_never_executes_declared_commands(tmp_path, monkeypatch):
    """READ-ONLY contract: aiworkhub_quality_evidence_packet must never run a
    repo-declared command. Only aiworkhub_quality_run_checks (run_declared_checks)
    may call subprocess.run. A marker file the declared command would create
    if executed must never appear, and subprocess.run itself must never be
    invoked while building the packet."""

    repo = _repo(tmp_path)
    marker = tmp_path / "should_not_run.txt"
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "id": "would-run",
                        "kind": "build",
                        "command": ["python3", "-c", f"open({str(marker)!r}, 'w').close()"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    calls: list[object] = []
    monkeypatch.setattr(qe.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    packet = qe.build_evidence_packet(repo)

    assert calls == []
    assert not marker.exists()
    assert len(packet["declared_checks"]) == 1
    declared = packet["declared_checks"][0]
    assert declared["check_id"] == "would-run"
    assert declared["status"] == qe.STATUS_SKIPPED
    assert declared["command"] == ["python3", "-c", f"open({str(marker)!r}, 'w').close()"]


def test_declared_check_descriptors_never_executes(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / ".aiworkhub").mkdir()
    (repo / ".aiworkhub" / "quality.json").write_text(
        json.dumps({"checks": [{"id": "x", "kind": "lint", "command": ["definitely-not-a-real-binary"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        qe.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not execute"))
    )
    descriptors = qe.declared_check_descriptors(repo)
    assert len(descriptors) == 1
    assert descriptors[0].status == qe.STATUS_SKIPPED
