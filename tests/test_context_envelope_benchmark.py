import json

from scripts import check_context_envelope_benchmark


def test_checked_in_context_envelope_benchmark_recomputes_exactly() -> None:
    assert check_context_envelope_benchmark.check() == []


def test_context_envelope_benchmark_rejects_token_claim(tmp_path) -> None:
    source = check_context_envelope_benchmark.DEFAULT_BENCHMARK
    document = json.loads(source.read_text(encoding="utf-8"))
    document["claim_status"]["token_savings_available"] = True
    target = tmp_path / "context-envelope.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    assert "unsafe_context_envelope_claim:token_savings_available" in (
        check_context_envelope_benchmark.check(target)
    )
