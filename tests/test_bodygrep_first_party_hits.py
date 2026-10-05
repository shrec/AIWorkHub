"""NF-2026-01363: vendored noise starved first-party bodygrep hits.

``bodygrep_query`` walked candidates ``ORDER BY file_path``, and
``AIWorkhubCli/third_party/`` sorts before ``src/``. A literal repeated many
times in a vendored file (sqlite3.c, catch2) filled the whole match budget --
or the byte cap -- before the walk ever reached first-party source, so live
queries for ``MAX_PROCESSES_CEILING`` and ``full_detail_available`` returned
only third_party rows.

First-party files are now walked before vendored ones; vendored files are
still indexed and still reachable on later pages through the cursor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402

_TERM = "FIRST_PARTY_PROBE_LITERAL"
_VENDORED = "AIWorkhubCli/third_party/big.c"
_FIRST_PARTY = "src/pkg/mod.py"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def repo_with_vendored_noise(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("bodygrep_first_party") / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="bodygrep_first_party")
    _write(
        repo / _VENDORED,
        "".join(f"int v{i} = 0; /* {_TERM} */\n" for i in range(400)),
    )
    _write(repo / _FIRST_PARTY, f"VALUE = {_TERM!r}\n")
    sg.build_index(repo, incremental=False)
    return repo


def test_first_party_hit_is_not_starved_by_vendored_matches(repo_with_vendored_noise):
    result = sg.bodygrep_query(repo_with_vendored_noise, _TERM, budget=4)
    files = {row["file_path"] for row in result["matches"]}
    assert _FIRST_PARTY in files, (
        f"first-party hit missing; returned only {sorted(files)}"
    )


def test_vendored_hits_remain_reachable_and_paging_is_deterministic(
    repo_with_vendored_noise,
):
    first = sg.bodygrep_query(repo_with_vendored_noise, _TERM, budget=4)
    again = sg.bodygrep_query(repo_with_vendored_noise, _TERM, budget=4)
    assert first == again
    cursor = first["next_cursor"]
    assert cursor
    seen: list[tuple[str, int]] = [
        (row["file_path"], row["line_start"]) for row in first["matches"]
    ]
    for _ in range(200):
        page = sg.bodygrep_query(
            repo_with_vendored_noise, _TERM, budget=4, cursor=cursor,
        )
        assert page == sg.bodygrep_query(
            repo_with_vendored_noise, _TERM, budget=4, cursor=cursor,
        )
        seen.extend((row["file_path"], row["line_start"]) for row in page["matches"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert cursor is None
    assert len(seen) == len(set(seen))
    assert seen[0] == (_FIRST_PARTY, 1)
    assert sorted(line for path, line in seen if path == _VENDORED) == list(
        range(1, 401)
    )
