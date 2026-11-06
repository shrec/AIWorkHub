"""NF-2026-02126: _line_for_offset is O(log n) with an identity-keyed line index.

Tests the function itself (equality, cache invalidation, micro-benchmark) and
one functional extraction check with no wall-clock bound.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

from aiworkhub import source_graph_ast as sga


def _reference(text: str, position: int) -> int:
    return text.count("\n", 0, max(0, position)) + 1


def _edge_offsets(text: str) -> list[int]:
    n = len(text)
    return [-5, -1, 0, 1, n - 1, n, n + 1, n + 100]


def test_line_for_offset_matches_reference_on_fixed_texts() -> None:
    texts = ["", "no newline here", "a\r\nb\r\nc", "trailing\n", "\n\n\n", "x\ny\n\nz"]
    for text in texts:
        for position in _edge_offsets(text):
            assert sga._line_for_offset(text, position) == _reference(text, position), (
                text,
                position,
            )


def test_line_for_offset_matches_reference_on_random_texts() -> None:
    rng = random.Random(2126)
    for _ in range(20):
        text = "".join(rng.choice("ab\n\r") for _ in range(rng.randint(0, 400)))
        for position in _edge_offsets(text):
            assert sga._line_for_offset(text, position) == _reference(text, position)
    text = "".join(rng.choice("ab\n\r") for _ in range(2_000))
    for _ in range(200):
        position = rng.randint(-10, len(text) + 10)
        assert sga._line_for_offset(text, position) == _reference(text, position)


def test_line_for_offset_cache_invalidates_on_identity_change() -> None:
    first = "one\ntwo\nthree\n"
    second = "a\n\n\nb"
    equal_copy = "".join(list(first))
    assert equal_copy == first
    assert equal_copy is not first
    for _ in range(3):
        for text in (first, second, equal_copy):
            for position in range(0, len(text) + 2):
                assert sga._line_for_offset(text, position) == _reference(text, position)


def test_line_for_offset_benchmark_20000_calls_under_one_second() -> None:
    unit = "int fn(int a) {\n    return a;\n}\n// " + "p" * 180 + "\n\n"
    text = unit * 20_000
    step = len(text) // 20_000
    offsets = [i * step for i in range(20_000)]
    started = time.perf_counter()
    results = [sga._line_for_offset(text, offset) for offset in offsets]
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, elapsed
    for offset, got in zip(offsets[:2_000], results[:2_000]):
        assert got == _reference(text, offset)


def test_extract_file_from_bytes_20000_function_cpp_is_ok_with_correct_line(
    tmp_path: Path,
) -> None:
    source = "".join(
        f"int fn_{i}(int a) {{\n    return a + {i};\n}}\n" for i in range(20_000)
    )
    raw = source.encode("utf-8")
    extraction = sga.extract_file_from_bytes(
        tmp_path,
        tmp_path / "big.cpp",
        raw,
        build_revision="test-nf02126",
    )
    assert extraction.status == "ok"
    matches = [entity for entity in extraction.entities if entity.name == "fn_19999"]
    assert len(matches) == 1
    assert matches[0].line_start == _reference(source, source.index("int fn_19999"))
