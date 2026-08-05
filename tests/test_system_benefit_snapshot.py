import json
from pathlib import Path

from scripts.check_system_benefit_snapshot import check, check_public_claim_surfaces


def test_checked_in_system_benefit_snapshot_is_internally_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    assert check(root / "benchmarks" / "system-benefit-snapshot-v1.json") == []


def test_public_claim_surfaces_fail_closed_on_stale_pilot_copy(
    tmp_path: Path,
) -> None:
    for relative in ("README.md", "site/index.html", "site/benchmarks/index.html"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("27.5% fewer tokens. Pilot only.", encoding="utf-8")

    errors = check_public_claim_surfaces(tmp_path)
    assert "public_claim_boundary_missing:README.md" in errors
    assert "public_claim_cap_mismatch_missing:site/index.html" in errors


def test_system_snapshot_rejects_paired_metric_drift(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "benchmarks" / "system-benefit-snapshot-v1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document["semantic_edit_paired_pilot"][
        "total_token_reduction_percent"
    ] = 71.5
    target = tmp_path / "system-benefit.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    errors = check(
        target,
        root / "benchmarks" / "semantic-edit-pilot-v1.json",
    )
    assert (
        "semantic_edit_paired_snapshot_mismatch:total_token_reduction_percent"
        in errors
    )


def test_semantic_edit_pilot_rejects_hidden_token_budget_mismatch(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "benchmarks" / "semantic-edit-pilot-v1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document["design"]["token_budget_pair_parity"] = True
    document["claim_status"]["reason_codes"].remove(
        "pair_token_budget_mismatch"
    )
    target = tmp_path / "semantic-edit-pilot.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    errors = check(
        root / "benchmarks" / "system-benefit-snapshot-v1.json",
        target,
    )
    assert "semantic_edit_token_budget_pair_parity_mismatch" in errors
    assert "semantic_edit_missing_reason:pair_token_budget_mismatch" in errors
