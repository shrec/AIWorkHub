import json

from scripts import check_semantic_edit_benchmark


def test_checked_in_semantic_edit_pilot_recomputes_exactly() -> None:
    assert check_semantic_edit_benchmark.check() == []


def test_semantic_edit_pilot_rejects_false_public_claim(tmp_path) -> None:
    source = check_semantic_edit_benchmark.DEFAULT_LEDGER
    document = json.loads(source.read_text(encoding="utf-8"))
    document["claim_status"]["public_claim_eligible"] = True
    target = tmp_path / "benchmark.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    assert "benchmark_small_pilot_must_not_be_public_claim_eligible" in (
        check_semantic_edit_benchmark.check(target)
    )
