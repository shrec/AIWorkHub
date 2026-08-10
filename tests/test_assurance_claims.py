from __future__ import annotations

import json
import shutil
from pathlib import Path

from aiworkhub import assurance_claims


ROOT = Path(__file__).resolve().parents[1]


def _copy_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    manifest = json.loads(
        (ROOT / assurance_claims.DEFAULT_MANIFEST).read_text(encoding="utf-8")
    )
    paths = {
        assurance_claims.DEFAULT_MANIFEST,
        "src/aiworkhub/server.py",
        str(manifest["source_graph_retrieval_registry"]),
        str(manifest["quality_policy"]),
    }
    for claim in manifest["claims"]:
        paths.update(item["path"] for item in claim["evidence"])
        paths.update(item["path"] for item in claim.get("surfaces", []))
    for group in (
        "policy_projection_tests",
        "source_graph_contract_tests",
        "negative_fixture_tests",
        "quality_adapter_tests",
    ):
        paths.update(selector.split("::", 1)[0] for selector in manifest[group])
    for relative in paths:
        source = ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return root


def test_canonical_release_assurance_manifest_passes() -> None:
    result = assurance_claims.check(ROOT)
    assert result["ok"] is True, result["errors"]
    assert result["blocking"] is False
    assert result["stats"] == {
        "claims": 3,
        "pinned_evidence": 4,
        "public_surfaces": 6,
        "required_tools": 6,
        "test_selectors": 12,
        "retrieval_cases": 10,
        "quality_checks": 5,
    }


def test_claim_evidence_hash_drift_fails_closed(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    target = root / "benchmarks" / "system-benefit-snapshot-v1.json"
    target.write_text("{}\n", encoding="utf-8")
    result = assurance_claims.check(root)
    assert result["ok"] is False
    assert any("claim_evidence_hash_mismatch" in error for error in result["errors"])


def test_missing_public_tool_fails_closed(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    manifest_path = root / assurance_claims.DEFAULT_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["required_tools"].append("aiworkhub_missing_release_surface")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = assurance_claims.check(root)
    assert "required_tool_missing:aiworkhub_missing_release_surface" in result["errors"]


def test_empty_retrieval_registry_and_missing_negative_fixture_fail_closed(
    tmp_path: Path,
) -> None:
    root = _copy_tree(tmp_path)
    registry_path = root / ".aiworkhub" / "source-graph-retrieval-eval.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["cases"] = []
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    manifest_path = root / assurance_claims.DEFAULT_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["negative_fixture_tests"][0] = (
        "tests/test_readonly_research_lifecycle_b1463.py::test_missing_negative_fixture"
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = assurance_claims.check(root)
    assert "source_graph_retrieval_cases_missing" in result["errors"]
    assert any("test_selector_missing:" in error for error in result["errors"])
