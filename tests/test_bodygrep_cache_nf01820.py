"""NF-2026-01820: bodygrep keeps a bounded, generation-keyed file-bytes cache."""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402

_TERM = "cachedhaystackliteral"


@pytest.fixture(autouse=True)
def _fresh_cache():
    sg._bodygrep_cache_clear()
    yield
    sg._bodygrep_cache_clear()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="bodygrep")
    for name, text in files.items():
        _write(repo / name, text)
    sg.build_index(repo, incremental=False)
    return repo


def _small_files() -> dict[str, str]:
    return {
        f"src/mod_{i}.py": f"def probe_{i}():\n    return {_TERM!r}\n" for i in range(6)
    }


def _count_reads(monkeypatch: pytest.MonkeyPatch, repo: Path) -> list[str]:
    reads: list[str] = []
    original = Path.read_bytes
    root = repo.resolve()

    def counting_read_bytes(self: Path) -> bytes:
        if self.resolve().is_relative_to(root):
            reads.append(str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return reads


def _positions(result: dict) -> list[tuple[str, int]]:
    return [(row["file_path"], row["line_start"]) for row in result["matches"]]


def test_repeat_query_reads_no_candidate_from_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _build_repo(tmp_path, _small_files())
    first = sg.bodygrep_query(repo, _TERM, budget=20, target="src")
    assert first["cache_hits"] == 0
    assert first["bytes_scanned"] > 0

    reads = _count_reads(monkeypatch, repo)
    second = sg.bodygrep_query(repo, _TERM, budget=20, target="src")

    assert reads == []
    assert second["matches"] == first["matches"]
    assert second["cache_hits"] == second["files_scanned"] == 6
    assert second["bytes_scanned"] == 0


def test_changed_content_and_mtime_invalidates_the_entry(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path, _small_files())
    before = sg.bodygrep_query(repo, _TERM, budget=20, target="src")
    assert [row["line_start"] for row in before["matches"]] == [2] * 6

    target = repo / "src" / "mod_3.py"
    _write(target, f"# a longer header line\n# another\ndef probe_3():\n    return {_TERM!r}\n")
    after = sg.bodygrep_query(repo, _TERM, budget=20, target="src")

    by_file = {row["file_path"]: row["line_start"] for row in after["matches"]}
    assert by_file["src/mod_3.py"] == 4
    assert after["cache_hits"] == 5
    assert len(sg._BODYGREP_FILE_CACHE_KEYS) == len(sg._BODYGREP_FILE_CACHE)


def test_paging_past_the_byte_cap_matches_an_uncapped_scan(tmp_path: Path) -> None:
    filler = "# filler line of ordinary comment text\n" * 8000
    files = {f"src/big_{i}.py": filler for i in range(8)}
    files["src/big_1.py"] = filler + f"VALUE = {_TERM!r}\n"
    files["src/big_7.py"] = filler + f"OTHER = {_TERM!r}\n"
    repo = _build_repo(tmp_path, files)

    capped_pages: list[dict] = []
    cursor = None
    while True:
        page = sg.bodygrep_query(repo, _TERM, budget=1, target="src", cursor=cursor)
        capped_pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(capped_pages) < 40
    assert len(capped_pages) > 2
    assert all(page["scan_truncated"] for page in capped_pages[:-1])
    assert capped_pages[-1]["scan_truncated"] is False

    sg._bodygrep_cache_clear()
    uncapped = sg.bodygrep_query(repo, _TERM, budget=64, target="src")
    assert uncapped["scan_truncated"] is False
    paged = [pos for page in capped_pages for pos in _positions(page)]
    assert len(paged) == len(set(paged)) == 2
    assert set(paged) == set(_positions(uncapped))


def test_budget_one_pages_return_one_row_each_and_skip_none(tmp_path: Path) -> None:
    repo = _build_repo(
        tmp_path,
        {
            "src/one.py": f"VALUE = {_TERM!r}\n",
            "src/two.py": f"OTHER = {_TERM!r}\n",
        },
    )

    pages: list[dict] = []
    cursor = None
    while True:
        page = sg.bodygrep_query(repo, _TERM, budget=1, target="src", cursor=cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(pages) < 10

    assert [len(page["matches"]) for page in pages if page["matches"]] == [1, 1]
    assert sorted(pos for page in pages for pos in _positions(page)) == [
        ("src/one.py", 1),
        ("src/two.py", 1),
    ]


def test_cache_total_never_exceeds_the_byte_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sg, "BODYGREP_FILE_CACHE_MAX_BYTES", 100)

    sg._bodygrep_cache_store(("a", 1, 60), b"a" * 60)
    sg._bodygrep_cache_store(("b", 1, 60), b"b" * 60)

    assert ("a", 1, 60) not in sg._BODYGREP_FILE_CACHE
    assert "a" not in sg._BODYGREP_FILE_CACHE_KEYS
    assert list(sg._BODYGREP_FILE_CACHE) == [("b", 1, 60)]
    assert sg._BODYGREP_FILE_CACHE_BYTES == 60

    sg._bodygrep_cache_store(("huge", 1, 101), b"h" * 101)
    assert "huge" not in sg._BODYGREP_FILE_CACHE_KEYS
    assert sg._BODYGREP_FILE_CACHE_BYTES == 60


def test_replacing_a_generation_does_not_scan_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoScan(OrderedDict):
        def _refuse(self, *args, **kwargs):
            raise AssertionError("cache entries were scanned")

        __iter__ = keys = values = items = _refuse

    cache = NoScan()
    monkeypatch.setattr(sg, "_BODYGREP_FILE_CACHE", cache)
    monkeypatch.setattr(sg, "_BODYGREP_FILE_CACHE_KEYS", {})
    monkeypatch.setattr(sg, "_BODYGREP_FILE_CACHE_BYTES", 0)

    for other in range(20):
        sg._bodygrep_cache_store((f"other{other}", 1, 10), b"o" * 10)
    sg._bodygrep_cache_store(("p", 1, 10), b"1" * 10)
    sg._bodygrep_cache_store(("p", 2, 20), b"2" * 20)

    assert ("p", 1, 10) not in cache
    assert cache[("p", 2, 20)] == b"2" * 20
    assert sg._BODYGREP_FILE_CACHE_BYTES == 20 * 10 + 20


def test_delta_cached_repeat_query_disk_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _build_repo(tmp_path, _small_files())
    first = sg.bodygrep_query(repo, _TERM, budget=20, target="src")

    reads = _count_reads(monkeypatch, repo)
    second = sg.bodygrep_query(repo, _TERM, budget=20, target="src")

    assert second["matches"] == first["matches"]
    assert len(second["matches"]) == 6
    assert len(reads) == 0
    with capsys.disabled():
        print(
            "\nAIWORKHUB_METRIC:"
            + json.dumps(
                {
                    "metric": "bodygrep_repeat_query_disk_reads",
                    "unit": "reads",
                    "value": len(reads),
                    "direction": "lower",
                    "max_regression_percent": 0,
                }
            )
        )
