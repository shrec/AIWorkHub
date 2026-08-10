from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from aiworkhub import release_evidence_pack


ROOT = Path(__file__).resolve().parents[1]


def _copy_inputs(tmp_path: Path) -> Path:
    paths = {
        ".aiworkhub/release-evidence.json",
        ".aiworkhub/release-residual-risks.json",
        ".aiworkhub/release-route-parity.json",
        ".aiworkhub/release-assurance.json",
        ".aiworkhub/source-graph-retrieval-eval.json",
        ".aiworkhub/quality.json",
        "benchmarks/semantic-edit-pilot-v1.json",
        "benchmarks/system-benefit-snapshot-v1.json",
        "README.md",
        "site/index.html",
        "site/benchmarks/index.html",
        "docs/BENCHMARKS.md",
        "docs/PRODUCT_ROADMAP.md",
        "src/aiworkhub/server.py",
        "tests/test_dashboard.py",
        "tests/test_process_launcher.py",
        "tests/test_worker_workspace.py",
        "tests/test_cross_platform_runtime.py",
        "tests/test_aiworkhub_agent_tool_instruction_generator_b816_v2.py",
        "tests/test_evidence_instruments.py",
        "tests/test_aiworkhub_source_graph_b849.py",
        "tests/test_readonly_research_lifecycle_b1463.py",
        "tests/test_quality_reviewer_contract.py",
        "tests/test_aiworkhub_quality_evidence_b906.py",
    }
    retrieval = json.loads(
        (ROOT / ".aiworkhub/source-graph-retrieval-eval.json").read_text(
            encoding="utf-8"
        )
    )
    for case in retrieval["cases"]:
        paths.update(case["expected_paths"])
    for relative in paths:
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return tmp_path


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def test_canonical_pack_is_deterministic_and_truth_bounded() -> None:
    first = release_evidence_pack.build(ROOT)
    second = release_evidence_pack.build(ROOT)

    assert first == second
    assert len(first["bundle_sha256"]) == 64
    assert first["stats"] == {
        "residual_risks": 4,
        "risk_evidence_refs": 8,
        "route_observations": 2,
    }
    assert first["measurement_boundary"] == release_evidence_pack.MEASUREMENT_BOUNDARY
    assert {row["evidence_kind"] for row in first["route_parity"]} == {
        "external_live_observation"
    }


def test_empty_residual_risk_register_fails_closed(tmp_path: Path) -> None:
    root = _copy_inputs(tmp_path)
    path = root / ".aiworkhub/release-residual-risks.json"
    document = _read(path)
    document["risks"] = []
    _write(path, document)

    result = release_evidence_pack.check(root)

    assert result["ok"] is False
    assert result["blocking"] is True
    assert "residual_risks_missing" in result["errors"][0]


def test_impossible_route_counts_fail_closed(tmp_path: Path) -> None:
    root = _copy_inputs(tmp_path)
    path = root / ".aiworkhub/release-route-parity.json"
    document = _read(path)
    observations = document["observations"]
    assert isinstance(observations, list)
    changed = copy.deepcopy(observations)
    changed[0]["available_routes"] = changed[0]["total_routes"] + 1
    document["observations"] = changed
    _write(path, document)

    result = release_evidence_pack.check(root)

    assert result["ok"] is False
    assert "route_parity_counts_invalid:linux" in result["errors"][0]


def test_missing_risk_evidence_reference_fails_closed(tmp_path: Path) -> None:
    root = _copy_inputs(tmp_path)
    path = root / ".aiworkhub/release-residual-risks.json"
    document = _read(path)
    risks = document["risks"]
    assert isinstance(risks, list)
    changed = copy.deepcopy(risks)
    changed[0]["evidence_refs"] = ["docs/DOES_NOT_EXIST.md"]
    document["risks"] = changed
    _write(path, document)

    result = release_evidence_pack.check(root)

    assert result["ok"] is False
    assert "required_regular_file_missing:docs/DOES_NOT_EXIST.md" in result["errors"][0]
