from pathlib import Path

from scripts.check_system_benefit_snapshot import check


def test_checked_in_system_benefit_snapshot_is_internally_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    assert check(root / "benchmarks" / "system-benefit-snapshot-v1.json") == []
