"""NF-2026-02247: the review packet cap bounds serialized bytes, not path count.

A move-only candidate names many changed paths with no content change. Before
the fix, more than 200 path hashes refused the packet with
``review_packet_overflow`` before any reviewer launched.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping

import pytest

from aiworkhub.quality_reviewer import (
    MAX_PACKET_PATH_BYTES,
    ReviewerEvidenceError,
    _canonical_bytes,
    build_review_packet,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _moved_paths(count: int, prefix: str) -> dict[str, str]:
    return {f"{prefix}/module_{index:04d}.py": _digest(f"moved-{index}") for index in range(count)}


def _build(paths: dict[str, str]) -> dict:
    return build_review_packet(
        request_id="req-nf02247",
        task_id="NF-2026-02247",
        claim_epoch=1,
        worker_provider="claude_haiku",
        changed_path_hashes=paths,
    )


def test_move_only_candidate_above_former_path_count_builds_packet() -> None:
    paths = _moved_paths(250, "src/aiworkhub/moved")

    packet = _build(paths)

    serialized = json.dumps(packet, sort_keys=True)
    assert "module_0249.py" in serialized


def test_packet_refused_when_serialized_path_rows_exceed_byte_budget() -> None:
    long_prefix = "moved/" + "d" * 180
    paths = _moved_paths(1000, long_prefix)

    with pytest.raises(ReviewerEvidenceError, match="review_packet_overflow"):
        _build(paths)


def _path_rows(paths: dict[str, str]) -> list[dict[str, str]]:
    return [{"path": path, "sha256": digest} for path, digest in sorted(paths.items())]


def test_byte_budget_is_the_refusal_rule_not_the_count() -> None:
    # Just under the budget with more than 200 paths: must still build.
    paths = _moved_paths(400, "m")
    assert len(_canonical_bytes(_path_rows(paths))) < MAX_PACKET_PATH_BYTES

    _build(paths)


def test_byte_budget_boundary_one_row_below_and_above() -> None:
    def rows_bytes(count: int) -> int:
        return len(_canonical_bytes(_path_rows(_moved_paths(count, "m"))))

    # Fixed-length paths and digests make every row cost the same bytes:
    # n rows serialize to n * (row + 1) + 1 bytes (commas and brackets).
    row_bytes = rows_bytes(1) - 2
    fits = (MAX_PACKET_PATH_BYTES - 1) // (row_bytes + 1)
    assert fits > 200
    assert rows_bytes(fits) <= MAX_PACKET_PATH_BYTES < rows_bytes(fits + 1)

    _build(_moved_paths(fits, "m"))
    with pytest.raises(ReviewerEvidenceError, match="review_packet_overflow"):
        _build(_moved_paths(fits + 1, "m"))


class _HugeMapping(Mapping[str, str]):
    """Reports a huge length; touching any entry fails the test."""

    def __len__(self) -> int:
        return 10_000_000

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("entries were normalized before the budget refusal")

    def __getitem__(self, key: str) -> str:
        raise AssertionError("entries were normalized before the budget refusal")


def test_huge_raw_input_refused_before_per_entry_normalization() -> None:
    with pytest.raises(ReviewerEvidenceError, match="review_packet_overflow"):
        _build(_HugeMapping())  # type: ignore[arg-type]
