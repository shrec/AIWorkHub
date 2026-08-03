from __future__ import annotations

import json
import os

from aiworkhub import quality_evidence


def test_completion_quality_gate_passes_valid_changed_sources(tmp_path) -> None:
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")
    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )
    assert packet["passed"] is True
    assert packet["blocking_checks"] == []
    assert {row["status"] for row in packet["checks"]} == {"passed"}
    assert all(row["status"] in {"skipped", "not_available"} for row in packet["optional_gates"])
    assert packet["repository_quality_policy"]["status"] == "unverified"
    assert packet["verification_scope"] == "builtin_and_task_contract_only"


def test_completion_quality_gate_blocks_invalid_python(tmp_path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["broken.py"]
    )
    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["builtin:syntax:broken.py"]


def test_completion_quality_gate_blocks_unavailable_declared_check(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "codeql-security",
            "kind": "security",
            "command": ["definitely-not-installed-aiworkhub-checker"],
        }]
    }), encoding="utf-8")
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")
    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )
    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["codeql-security"]
    declared = next(row for row in packet["checks"] if row["check_id"] == "codeql-security")
    assert declared["status"] == "not_available"
    assert declared["kind"] == "security"


def test_completion_quality_gate_accepts_codeql_like_static_analysis_kind(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "bounded-sast",
            "kind": "static_analysis",
            "command": ["python3", "-c", "raise SystemExit(0)"],
        }]
    }), encoding="utf-8")
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is True
    declared = next(row for row in packet["checks"] if row["check_id"] == "bounded-sast")
    assert declared["kind"] == "static_analysis"
    assert declared["status"] == "passed"


def test_completion_quality_gate_normalizes_declared_coverage_report(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "python-coverage",
            "kind": "coverage",
            "command": ["{python}", "-c", "raise SystemExit(0)"],
            "report": {
                "format": "coverage_json",
                "path": "coverage.json",
                "min_percent": 80,
            },
        }]
    }), encoding="utf-8")
    (tmp_path / "coverage.json").write_text(
        json.dumps({"total": {"lines": {"pct": 85.5}}}),
        encoding="utf-8",
    )
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is True
    report = next(
        row for row in packet["checks"]
        if row["check_id"] == "python-coverage:report"
    )
    assert report["kind"] == "coverage"
    assert report["status"] == "passed"
    assert report["provenance"] == "adapter:coverage_summary:coverage.json"


def test_completion_quality_gate_blocks_below_threshold_declared_report(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "python-coverage",
            "kind": "coverage",
            "command": ["{python}", "-c", "raise SystemExit(0)"],
            "report": {
                "format": "coverage_json",
                "path": "coverage.json",
                "min_percent": 80,
            },
        }]
    }), encoding="utf-8")
    (tmp_path / "coverage.json").write_text(
        json.dumps({"total": {"lines": {"pct": 42.0}}}),
        encoding="utf-8",
    )
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["python-coverage:report"]


def test_declared_report_path_rejects_globs_and_parent_escape(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    for unsafe in ("../coverage.json", "reports/*.json"):
        config.write_text(json.dumps({
            "checks": [{
                "id": "unsafe-report",
                "kind": "coverage",
                "command": ["{python}", "-c", "raise SystemExit(0)"],
                "report": {"format": "coverage_json", "path": unsafe},
            }]
        }), encoding="utf-8")
        try:
            quality_evidence.load_repo_config(tmp_path)
        except quality_evidence.MalformedConfigError as exc:
            assert "unsafe report path" in str(exc)
        else:
            raise AssertionError(f"unsafe report path was accepted: {unsafe}")


def test_declared_report_symlink_fails_closed(tmp_path) -> None:
    if os.name == "nt":
        return
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "linked-coverage",
            "kind": "coverage",
            "command": ["{python}", "-c", "raise SystemExit(0)"],
            "report": {"format": "coverage_json", "path": "coverage.json"},
        }]
    }), encoding="utf-8")
    target = tmp_path / "actual-coverage.json"
    target.write_text(
        json.dumps({"total": {"lines": {"pct": 100}}}), encoding="utf-8"
    )
    (tmp_path / "coverage.json").symlink_to(target)
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["linked-coverage:report"]
    report = next(row for row in packet["checks"] if row["check_id"].endswith(":report"))
    assert report["error"] == "report_symlink_forbidden"


def test_declared_check_skips_unrelated_exact_delta(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "extension-only",
            "kind": "test",
            "command": ["python3", "-c", "raise SystemExit(19)"],
            "paths": ["vscode-extension/**"],
        }]
    }), encoding="utf-8")
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is True
    row = next(check for check in packet["checks"] if check["check_id"] == "extension-only")
    assert row["status"] == "skipped"
    assert row["summary"] == "changed_paths_not_applicable"


def test_declared_check_runs_for_matching_delta_and_respects_minimum_risk(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "high-risk-python",
            "kind": "test",
            "command": ["python3", "-c", "raise SystemExit(19)"],
            "paths": ["src/*.py"],
            "minimum_risk": "high",
        }]
    }), encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text("value = 1\n", encoding="utf-8")

    low = quality_evidence.run_declared_checks(
        tmp_path,
        changed_paths=["src/engine.py"],
        effective_risk_tier="low",
    )
    high = quality_evidence.run_declared_checks(
        tmp_path,
        changed_paths=["src/engine.py"],
        effective_risk_tier="high",
    )

    assert low[0].status == "skipped"
    assert low[0].summary == "risk_below_minimum:low<high"
    assert high[0].status == "failed"


def test_destructive_diff_blocks_multi_signal_module_replacement(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root in (baseline, candidate):
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir(parents=True)
    functions = "\n".join(
        f"def public_{index}():\n    return {index}\n" for index in range(120)
    )
    (baseline / "src" / "engine.py").write_text(functions, encoding="utf-8")
    (candidate / "src" / "engine.py").write_text(
        "def public_0():\n    return 0\n", encoding="utf-8"
    )
    (candidate / "tests" / "test_engine.py").write_text(
        "def test_small():\n    assert True\n", encoding="utf-8"
    )

    checks = quality_evidence.run_destructive_diff_checks(
        baseline,
        candidate,
        changed_paths=["src/engine.py", "tests/test_engine.py"],
    )

    blocker = next(row for row in checks if row.check_id.endswith("src/engine.py"))
    assert blocker.status == "failed"
    assert "tests_changed=true" in blocker.summary
    assert "public_api_loss" in blocker.summary


def test_destructive_diff_allows_small_or_nonshrinking_edit(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    before = "\n".join(f"value_{index} = {index}" for index in range(250)) + "\n"
    (baseline / "engine.py").write_text(before, encoding="utf-8")
    (candidate / "engine.py").write_text(before + "new_value = 1\n", encoding="utf-8")

    checks = quality_evidence.run_destructive_diff_checks(
        baseline, candidate, changed_paths=["engine.py"]
    )

    assert [row.status for row in checks] == ["passed"]
