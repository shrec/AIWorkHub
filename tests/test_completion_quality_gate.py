from __future__ import annotations

import json

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
