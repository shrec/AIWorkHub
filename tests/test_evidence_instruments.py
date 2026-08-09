from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from aiworkhub import agent_tool_instructions as instructions
from aiworkhub import evidence_instruments as evidence


def test_review_evidence_audit_recomputes_hash_and_size(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    path = candidate / "result.py"
    path.write_bytes(b"answer = 42\n")
    path.chmod(0o644)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report = evidence.review_evidence_audit(
        tmp_path,
        candidate,
        changed_paths=["result.py"],
        stored_hashes={"result.py": digest},
        required_outputs=[{"path": "result.py", "sha256": digest, "bytes": 12}],
    )
    assert report["status"] == "pass"
    assert report["required_outputs_verified"] == 1
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    manifest_token = evidence.review_evidence_audit(
        tmp_path,
        candidate,
        changed_paths=["result.py"],
        stored_hashes={"result.py": digest},
        required_outputs=[{
            "path": "result.py",
            "sha256": f"file:{actual_mode:o}:{digest}",
            "bytes": 12,
        }],
    )
    assert manifest_token["status"] == "pass"
    if os.name != "nt":
        path.chmod(0o4755)
        executable_token = evidence.review_evidence_audit(
            tmp_path,
            candidate,
            changed_paths=["result.py"],
            stored_hashes={"result.py": digest},
            required_outputs=[{
                "path": "result.py",
                "sha256": f"file:4755:{digest}",
                "bytes": 12,
            }],
        )
        assert executable_token["status"] == "pass"
    path.chmod(0o644)
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    malformed_values = (
        f"file:{actual_mode:o}:{digest[:-1]}",
        f"file:{actual_mode:o}:{'0' * 64}",
        f"file:{actual_mode ^ 0o100:o}:{digest}",
        f"file:xyz:{digest}",
        f"file:888:{digest}",
        f"file:{actual_mode:o}:extra:{digest}",
        f"file:{actual_mode:o}:{digest.upper()}",
        f" file:{actual_mode:o}:{digest}",
    )
    for malformed in malformed_values:
        malformed_token = evidence.review_evidence_audit(
            tmp_path,
            candidate,
            changed_paths=["result.py"],
            stored_hashes={"result.py": digest},
            required_outputs=[{
                "path": "result.py",
                "sha256": malformed,
                "bytes": 12,
            }],
        )
        assert malformed_token["blockers"] == [
            "required_output_hash_mismatch:result.py"
        ]
    failed = evidence.review_evidence_audit(
        tmp_path,
        candidate,
        changed_paths=["result.py"],
        stored_hashes={"result.py": "0" * 64},
    )
    assert failed["blocking"] is True


def test_contract_consistency_uses_generated_projections(tmp_path: Path) -> None:
    for provider in instructions.PROVIDERS:
        path = tmp_path / provider
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(instructions.render_projection(provider), encoding="utf-8")
    report = evidence.contract_consistency_check(tmp_path)
    assert report["status"] == "pass"
    (tmp_path / "CLAUDE.md").write_text("# drift\n", encoding="utf-8")
    report = evidence.contract_consistency_check(tmp_path)
    assert "projection_drift:CLAUDE.md" in report["blockers"]


def test_retrieval_eval_measures_wrapper_rank(tmp_path: Path) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "one", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
        }]
    }), encoding="utf-8")

    def query_fn(**_kwargs):
        return {
            "ok": True,
            "content": json.dumps({
                "ranked_symbols": [
                    {"file_path": "src/wrong.py"},
                    {"file_path": "src/right.py"},
                ]
            }),
        }

    report = evidence.source_graph_retrieval_eval(tmp_path, query_fn=query_fn)
    assert report["precision_at_k"] == 0.5
    assert report["mrr"] == 0.5


def test_retrieval_eval_distinguishes_missing_from_invalid_registry(tmp_path: Path) -> None:
    missing = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {},
    )
    assert missing["status"] == "not_configured"
    assert missing["configured"] is False
    assert missing["reason"].startswith("retrieval_registry_missing:")

    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text("{not-json}\n", encoding="utf-8")
    invalid = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {},
    )
    assert invalid["status"] == "configuration_invalid"
    assert invalid["configured"] is True
    assert invalid["repair_hint"]


def test_ab_report_excludes_unobserved_and_measures_complete_pair(tmp_path: Path) -> None:
    path = tmp_path / ".aiworkhub/prompt-ab-canary.jsonl"
    path.parent.mkdir()
    rows = [
        {"pair_id": "p", "variant": "A", "task_family": "x", "model": "m", "adapter": "a", "usage_observed": True, "input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "elapsed_ms": 1000, "accepted": True},
        {"pair_id": "p", "variant": "B", "task_family": "x", "model": "m", "adapter": "a", "usage_observed": True, "input_tokens": 90, "output_tokens": 20, "total_tokens": 110, "elapsed_ms": 800, "accepted": True},
        {"pair_id": "q", "variant": "A", "task_family": "x", "model": "m", "adapter": "a", "usage_observed": False},
        {"pair_id": "q", "variant": "B", "task_family": "x", "model": "m", "adapter": "a", "usage_observed": True},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = evidence.prompt_bundle_ab_report(tmp_path)
    assert report["pair_count"] == 1
    assert report["pairs"][0]["metrics"]["output_tokens"]["delta_percent"] == -50.0
    assert report["excluded"] == [{"pair_id": "q", "reason": "usage_unobserved"}]


def test_quality_ratchet_and_coverage_projection(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_text("a\nb\n", encoding="utf-8")
    config = tmp_path / ".aiworkhub/quality-ratchet.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"violation_ids": ["old"], "max_lines": {"large.py": 1}}), encoding="utf-8")
    ratchet = evidence.quality_gate_ratchet(tmp_path, violations=[{"id": "old"}, {"id": "new"}])
    assert ratchet["blocking"] is True
    assert ratchet["new_violation_ids"] == ["new"]
    coverage = tmp_path / "coverage.json"
    coverage.write_text(json.dumps({"files": {"large.py": {"summary": {"covered_lines": 1, "missing_lines": 1, "percent_covered": 50}}}}), encoding="utf-8")
    applied = evidence.coverage_import_apply(tmp_path, artifact_path="coverage.json")
    assert applied["file_count"] == 1
    projected = evidence.runtime_coverage_for_paths(tmp_path, ["large.py"])
    assert projected["status"] == "available"
    assert projected["files"][0]["percent_covered"] == 50.0


def test_suite_profile_detects_stable_exact_argv(tmp_path: Path) -> None:
    report = evidence.suite_profile(
        tmp_path,
        argv=[sys.executable, "-c", "print('ok')"],
        repeats=2,
    )
    assert report["status"] == "measured"
    assert report["flake_observed"] is False
    assert all(row["returncode"] == 0 for row in report["runs"])


def test_suite_profile_collects_per_test_metrics(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        "def test_sample():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    report = evidence.suite_profile(
        tmp_path,
        argv=[sys.executable, "-m", "pytest", "-q", "test_sample.py"],
        repeats=2,
    )
    assert report["per_test_observed"] is True
    assert report["tests"][0]["nodeid"] == "test_sample.py::test_sample"
    assert report["tests"][0]["run_count"] == 2
    assert report["tests"][0]["flake_observed"] is False
