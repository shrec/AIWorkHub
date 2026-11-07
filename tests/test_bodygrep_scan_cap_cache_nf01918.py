"""NF-2026-01918 / NF-2026-01957: cache-served files do not consume the file cap,
and a target that names no repository path is refused rather than answered as 0.

Single pages come from ``_bodygrep_query_page``: ``bodygrep_query`` keeps paging
past empty truncated pages on its own, which would hide the cap under test.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402

_TERM = "scancapcachedliteral"
_FILE_COUNT = 300  # more than scan_file_cap (256) at budget 8
_HIT_FILE = "src/f_299.py"


@pytest.fixture(autouse=True)
def _fresh_cache():
    sg._bodygrep_cache_clear()
    yield
    sg._bodygrep_cache_clear()


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="bodygrep")
    for index in range(_FILE_COUNT):
        text = f"def probe_{index}():\n    return 0\n"
        if index == _FILE_COUNT - 1:
            text += f"VALUE = {_TERM!r}\n"  # the only hit sits in the last file
        path = repo / "src" / f"f_{index:03d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    sg.build_index(repo, incremental=False)
    return repo


def _single_page(repo: Path, cursor: str | None = None) -> dict:
    return sg._bodygrep_query_page(repo, _TERM, 8, target="src", cursor=cursor)


def test_cached_files_do_not_consume_the_file_cap(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    # Warm the cache over every file by walking the raw pages; a cold walk pages
    # at the file cap, so this takes more than one page.
    warm_pages = [_single_page(repo)]
    while warm_pages[-1]["next_cursor"] is not None:
        assert len(warm_pages) < 50
        warm_pages.append(_single_page(repo, warm_pages[-1]["next_cursor"]))
    assert len(warm_pages) > 1

    cached = _single_page(repo)

    assert [row["file_path"] for row in cached["matches"]] == [_HIT_FILE]
    assert cached["scan_truncated"] is False
    assert cached["next_cursor"] is None
    assert cached["cache_hits"] == cached["files_scanned"] == _FILE_COUNT
    assert cached["bytes_scanned"] == 0


def test_cold_scan_still_stops_at_the_file_cap_and_pages_to_the_hit(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)

    first = _single_page(repo)

    assert first["scan_truncated"] is True
    assert first["next_cursor"] is not None
    assert first["files_scanned"] == 256
    assert first["matches"] == []

    found: list[str] = []
    page = first
    while page["next_cursor"] is not None:
        page = _single_page(repo, page["next_cursor"])
        found.extend(row["file_path"] for row in page["matches"])
    assert found == [_HIT_FILE]


def test_reply_names_its_match_semantics(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    literal = sg.bodygrep_query(repo, _TERM, budget=8, target="src")
    assert literal["match_semantics"] == "exact_phrase"


def test_target_naming_no_repository_path_is_refused_by_name(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with pytest.raises(sg.SourceGraphError, match="target_not_found:ORDER_NUMBER"):
        sg.bodygrep_query(repo, _TERM, budget=8, target="ORDER_NUMBER")


def test_existing_file_and_directory_targets_still_scan(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    directory = sg.bodygrep_query(repo, _TERM, budget=8, target="src")
    assert [row["file_path"] for row in directory["matches"]] == [_HIT_FILE]

    single = sg.bodygrep_query(repo, _TERM, budget=8, target=_HIT_FILE)
    assert [row["file_path"] for row in single["matches"]] == [_HIT_FILE]


_WALK_FIRST_PARTY = 400
_WALK_VENDORED = 200  # scan tier 1: the walk must cross from tier 0 into tier 1


def _build_walk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "walk"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="bodygrep_walk")
    for root, count in (("src", _WALK_FIRST_PARTY), ("third_party", _WALK_VENDORED)):
        directory = repo / root
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (directory / f"m_{index:03d}.py").write_text(
                f"VALUE_{index} = {index}\n", encoding="utf-8",
            )
    sg.build_index(repo, incremental=False)
    return repo


def _indexed_paths_in_scan_order(repo: Path) -> list[str]:
    """Reference order, read straight from the table rather than through the walk."""
    with closing(sqlite3.connect(str(sg.resolve_db_path(repo)))) as conn:
        paths = [row[0] for row in conn.execute("SELECT file_path FROM files")]
    return sorted(paths, key=lambda path: (sg._bodygrep_scan_tier(path), path))


class _CountingConnection:
    """Proxy that counts the ordered candidate query run on one connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.ordered_queries = 0

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def execute(self, sql: str, params=()):
        if "ORDER BY bodygrep_scan_tier" in sql:
            self.ordered_queries += 1
        return self._conn.execute(sql, params)


def test_candidate_walk_uses_one_connection_and_one_ordered_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _build_walk_repo(tmp_path)
    real_connect = sg.connect
    opened: list[_CountingConnection] = []

    def counting_connect(*args, **kwargs):
        wrapper = _CountingConnection(real_connect(*args, **kwargs))
        opened.append(wrapper)
        return wrapper

    monkeypatch.setattr(sg, "connect", counting_connect)
    walked = list(sg._iter_bodygrep_candidates(repo, "", False, None))

    assert len(opened) == 1
    assert opened[0].ordered_queries == 1
    assert len(walked) == _WALK_FIRST_PARTY + _WALK_VENDORED
    assert len(walked) > sg._BODYGREP_CANDIDATE_PAGE
    assert walked == _indexed_paths_in_scan_order(repo)
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0]._conn.execute("SELECT 1")  # closed once the walk finished


def test_candidate_walk_resumes_inclusive_and_past_each_resume_point(tmp_path: Path) -> None:
    repo = _build_walk_repo(tmp_path)
    full = list(sg._iter_bodygrep_candidates(repo, "", False, None))
    # The walk takes an inclusive resume file. The exclusive continuation the old
    # paged walk used between pages is the same walk resumed at the next file.
    for index in (0, 255, 256, _WALK_FIRST_PARTY - 1, _WALK_FIRST_PARTY, 511, len(full) - 2):
        inclusive = list(sg._iter_bodygrep_candidates(repo, "", False, full[index]))
        assert inclusive == full[index:]
        after = list(sg._iter_bodygrep_candidates(repo, "", False, full[index + 1]))
        assert after == full[index + 1:]
        assert full[index] not in after
