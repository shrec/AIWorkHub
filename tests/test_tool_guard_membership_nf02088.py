"""NF-2026-02088: the tool guard asks Source Graph, not the file extension.

(a) R8 lets Write create a new scratch file and names the MCP manager edit
    pair; ``scripts/manager_semantic_edit.py`` only where the root carries it.
(b) A path the current Source Graph generation lists as not indexed, or
    omits, is unindexed for R2-R5; without a readable generation the
    repository policy (suffix, excludes) stays the fallback.
(c) A searcher with no file operand that reads a pipe is not R1.
(d) A path outside the repository root is never an R3-R7 target.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph, tool_guard_hook  # noqa: E402

# The owner's own DDL for the two tables the guard reads.
_MARKER = "CREATE TABLE IF NOT EXISTS entities"
assert _MARKER in source_graph.SCHEMA
_DDL = source_graph.SCHEMA.split(_MARKER)[0]

_CMAKE_PRESETS = "CMakePresets.json"
_FILES = {
    "src/x.py": "foo = 1\n",
    "src/aiworkhub/core.py": "def core():\n    return 1\n",
    "docs/guide.md": "# guide\n",
    _CMAKE_PRESETS: "{}\n",
    ".aiworkhub/runtime/x.json": "{}\n",
}
_SCRATCH = ".scratch/s1/probe.py"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, text in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    (tmp_path / "pad").mkdir()
    (tmp_path / "pad" / "x.py").write_bytes(b"a = 1\n")
    return root.resolve()


def _db(root: Path) -> Path:
    return root.joinpath(*tool_guard_hook.SOURCE_GRAPH_DB_PARTS)


def _publish(root: Path, rows: dict[str, str], *, generation: bool = True) -> Path:
    """A Source Graph database whose files table holds ``rows`` (path -> status)."""
    db = _db(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DDL)
        if generation:
            build = {"build_revision": "test", "finished_at": "2026-11-05T00:00:00Z", "files_seen": len(rows)}
            conn.execute("INSERT INTO meta(key, value) VALUES ('last_build', ?)", (json.dumps(build),))
        conn.executemany(
            "INSERT INTO files(file_path, language, status, source_hash, indexed_at, build_revision)"
            " VALUES (?, 'python', ?, 'h', '2026-11-05T00:00:00Z', 'test')",
            sorted(rows.items()),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _rule(root: Path, tool: str, tool_input: dict[str, Any], cwd: Any = None) -> str | None:
    verdict = tool_guard_hook.decide(tool, tool_input, root, cwd)
    return None if verdict is None else verdict[0]


def _read(root: Path, rel: str) -> str | None:
    return _rule(root, "Read", {"file_path": str(root / rel)})


def _bash(root: Path, command: str, cwd: Any = None) -> str | None:
    return _rule(root, "Bash", {"command": command}, cwd)


_PY_READ = "python -c \"import json; print(json.load(open('CMakePresets.json')))\""


# (b) Source Graph membership --------------------------------------------------


def test_without_a_database_the_suffix_policy_still_decides(repo: Path) -> None:
    assert not _db(repo).exists()
    assert _read(repo, "src/x.py") == "R5"
    assert _read(repo, _CMAKE_PRESETS) == "R5"
    assert _bash(repo, _PY_READ) == "R4"
    assert _bash(repo, "ls -R docs") == "R2"


def test_a_path_the_generation_lists_as_unindexed_is_unindexed(repo: Path) -> None:
    _publish(repo, {"src/x.py": "ok", _CMAKE_PRESETS: "declared_input_unindexed"})

    assert _read(repo, _CMAKE_PRESETS) is None
    assert _bash(repo, f"cat {_CMAKE_PRESETS}") is None
    assert _bash(repo, _PY_READ) is None
    assert _read(repo, "src/x.py") == "R5"


def test_a_path_absent_from_an_existing_generation_is_unindexed(repo: Path) -> None:
    _publish(repo, {"src/x.py": "ok"})

    assert _read(repo, "src/aiworkhub/core.py") is None
    assert _bash(repo, "cat src/aiworkhub/core.py") is None
    assert _bash(repo, "ls -R docs") is None
    assert _bash(repo, "ls -R src/aiworkhub") is None
    # Members stay guarded, and a directory holding one stays guarded too.
    assert _read(repo, "src/x.py") == "R5"
    assert _bash(repo, "cat src/x.py") == "R3"
    assert _bash(repo, "ls -R src") == "R2"
    assert _bash(repo, "ls -R .") == "R2"


def test_an_unknown_status_keeps_the_path_guarded(repo: Path) -> None:
    _publish(repo, {"src/x.py": "parse_error"})
    assert _read(repo, "src/x.py") == "R5"


def test_policy_exclusions_still_win_over_a_listed_row(repo: Path) -> None:
    _publish(repo, {".aiworkhub/runtime/x.json": "ok"})
    assert _bash(repo, "cat .aiworkhub/runtime/x.json") is None


@pytest.mark.parametrize("state", ["no_generation", "garbage"])
def test_an_unreadable_generation_falls_back_to_the_policy(repo: Path, state: str) -> None:
    if state == "no_generation":
        _publish(repo, {}, generation=False)
    else:
        _db(repo).parent.mkdir(parents=True)
        _db(repo).write_bytes(b"this is not a sqlite database" * 64)

    assert _read(repo, "src/x.py") == "R5"
    assert _bash(repo, _PY_READ) == "R4"
    assert _bash(repo, "ls -R docs") == "R2"


def test_the_guard_opens_the_database_read_only(repo: Path) -> None:
    db = _publish(repo, {"src/x.py": "ok"})
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    siblings = sorted(path.name for path in db.parent.iterdir())

    assert _read(repo, "src/x.py") == "R5"
    assert _read(repo, "docs/guide.md") is None

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert sorted(path.name for path in db.parent.iterdir()) == siblings


def test_membership_answers_member_listed_absent_and_unknown(repo: Path) -> None:
    missing = tool_guard_hook.SourceGraphMembership(repo)
    try:
        assert missing.member("src/x.py", False) is None
    finally:
        missing.close()

    _publish(repo, {"src/X.py": "ok", "lib/a_b/c.py": "ok", "gen/d.py": "skipped"})
    membership = tool_guard_hook.SourceGraphMembership(repo)
    try:
        assert membership.member("src/X.py", False) is True
        # A case-insensitive file system hands over the caller's spelling.
        assert membership.member("src/x.py", False) is True
        assert membership.member("src", True) is True
        assert membership.member("lib/a_b", True) is True
        # "_" and "%" are literal in a directory prefix, not wildcards.
        assert membership.member("lib/aXb", True) is False
        assert membership.member("gen/d.py", False) is False
        assert membership.member("gen", True) is False
        assert membership.member("src/y.py", False) is False
        assert membership.member(".", True) is True
    finally:
        membership.close()


def test_the_database_location_is_the_storage_registry_canonical_path() -> None:
    parts = tool_guard_hook.SOURCE_GRAPH_DB_PARTS
    assert parts[0] == ".aiworkhub"
    assert "/".join(parts[1:]) == "source_graph/source_graph.sqlite"


def test_a_context_built_without_membership_still_asks_source_graph(repo: Path) -> None:
    # The four-argument form existing callers use (NF-2026-01737) keeps working.
    policy = tool_guard_hook.load_index_policy(repo)
    before = tool_guard_hook._Context(repo, repo, "bash", policy)
    assert before.indexed_file(str(repo / "src/x.py")) == "src/x.py"

    _publish(repo, {"src/x.py": "ok"})
    ctx = tool_guard_hook._Context(repo, repo, "bash", policy)
    try:
        assert ctx.indexed_file(str(repo / "src/x.py")) == "src/x.py"
        assert ctx.indexed_file(str(repo / "src/aiworkhub/core.py")) is None
    finally:
        ctx.membership.close()
        before.membership.close()


# (a) R8 ------------------------------------------------------------------------


def test_write_that_creates_a_scratch_file_is_allowed(repo: Path) -> None:
    tool_input = {"file_path": str(repo / _SCRATCH), "content": "x = 1\n"}
    assert _rule(repo, "Write", tool_input) is None


@pytest.mark.parametrize(
    ("tool", "extra", "exists"),
    [
        ("Read", {}, True),
        ("Write", {"content": "x = 2\n"}, True),
        ("Edit", {"old_string": "a", "new_string": "b"}, True),
        ("Edit", {"old_string": "a", "new_string": "b"}, False),
    ],
)
def test_read_edit_and_overwrite_in_scratch_stay_denied(
    repo: Path, tool: str, extra: dict[str, Any], exists: bool
) -> None:
    if exists:
        (repo / _SCRATCH).parent.mkdir(parents=True)
        (repo / _SCRATCH).write_bytes(b"a = 1\n")
    assert _rule(repo, tool, {"file_path": str(repo / _SCRATCH), **extra}) == "R8"


@pytest.mark.parametrize("script", [False, True])
def test_the_r8_reason_names_the_manager_script_only_where_it_exists(
    repo: Path, script: bool
) -> None:
    (repo / _SCRATCH).parent.mkdir(parents=True)
    (repo / _SCRATCH).write_bytes(b"a = 1\n")
    if script:
        (repo / "scripts").mkdir()
        (repo / "scripts" / "manager_semantic_edit.py").write_bytes(b"# manager\n")

    verdict = tool_guard_hook.decide("Read", {"file_path": str(repo / _SCRATCH)}, repo)

    assert verdict is not None and verdict[0] == "R8"
    reason = verdict[1]
    assert len(reason) <= tool_guard_hook.REASON_MAX_CHARS
    assert _SCRATCH in reason
    assert "aiworkhub_manager_semantic_edit_prepare/_apply" in reason
    assert "aiworkhub_worker_semantic_edit_prepare/_apply" in reason
    assert ("scripts/manager_semantic_edit.py" in reason) is script
    assert "{script}" not in reason
    assert reason.endswith(".")


# (c) a searcher reading a pipe ---------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "command"),
    [
        ("Bash", "git show HEAD:src/x.py | grep foo"),
        ("Bash", "git show HEAD:src/x.py |\n  grep foo"),
        ("Bash", "git show HEAD:src/x.py | \\\n  rg foo"),
        ("Bash", "git show HEAD:src/x.py | (grep foo)"),
        ("Bash", "rg foo <(git show HEAD:src/x.py)"),
        ("Bash", "grep foo < /dev/null"),
        ("PowerShell", "git show HEAD:src/x.py | Select-String foo"),
        ("PowerShell", "git show HEAD:src/x.py |\n  Select-String -Pattern foo"),
        ("PowerShell", "git show HEAD:src/x.py | findstr foo"),
    ],
)
def test_a_searcher_reading_a_pipe_is_not_r1(repo: Path, tool: str, command: str) -> None:
    assert _rule(repo, tool, {"command": command}) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn foo src",
        "rg foo",
        "git log |\n  grep foo src/x.py",
        "git show HEAD:src/x.py | grep -r foo src",
        # A recursive grep with no FILE ignores stdin and searches the directory.
        "git show HEAD:src/x.py | grep -rn foo",
        "grep -r foo < /dev/null",
        # Every "< file" feeds stdin; none of them is a FILE operand.
        "grep -r foo < /dev/null < /dev/null",
        "grep -rn foo </dev/null 0</dev/null",
    ],
)
def test_a_searcher_over_the_tree_is_still_r1(repo: Path, command: str) -> None:
    assert _bash(repo, command) == "R1", command


def test_more_than_one_stdin_redirect_is_no_file_operand(repo: Path) -> None:
    command = "grep foo < /dev/null < /dev/null"
    segments, _ = tool_guard_hook._Lexer(command, "bash").run()
    assert [segment.redirected for segment in segments] == [["/dev/null", "/dev/null"]]
    assert _bash(repo, command) is None


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("cd \"$(git rev-parse --show-toplevel)\" && grep -rn foo src", "R1"),
        ("cd \"$AIWORKHUB_NF02088_SCRATCHPAD\" && rg foo", "R1"),
        ("cd \"$AIWORKHUB_NF02088_SCRATCHPAD\" && ls -R src", "R2"),
    ],
)
def test_search_and_listing_after_an_unresolvable_cd_stay_guarded(
    repo: Path, monkeypatch: pytest.MonkeyPatch, command: str, rule: str
) -> None:
    monkeypatch.delenv("AIWORKHUB_NF02088_SCRATCHPAD", raising=False)
    assert _bash(repo, command) == rule, command


# (d) paths outside the repository root -----------------------------------------


def test_paths_outside_the_root_are_never_r3_to_r7_targets(repo: Path) -> None:
    pad = (repo.parent / "pad").resolve()
    outside = (pad / "x.py").as_posix()

    assert _bash(repo, f"sed -i 's/a/b/' {outside}") is None
    assert _bash(repo, f"cat {outside}") is None
    assert _bash(repo, f"python -c \"open('{outside}').read()\"") is None
    assert _bash(repo, f"cd {pad.as_posix()} && sed -i 's/a/b/' x.py") is None
    assert _bash(repo, "sed -i 's/a/b/' x.py", pad) is None
    for tool, extra in (
        ("Read", {}),
        ("Edit", {"old_string": "a", "new_string": "b"}),
        ("Write", {"content": "b = 2\n"}),
    ):
        assert _rule(repo, tool, {"file_path": str(pad / "x.py"), **extra}) is None, tool


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("cd \"$AIWH_UNSET_NF02088/y\" && sed -i s/a/b/ src/x.py", "R6"),
        # A shell read is R3 (the Read tool's twin, R5, has no cwd to keep).
        ("cd \"$AIWH_UNSET_NF02088/y\" && cat src/aiworkhub/core.py", "R3"),
        ("fd --base-directory \"$AIWH_UNSET_NF02088\" foo", "R2"),
    ],
)
def test_an_unresolvable_place_keeps_the_retained_cwd(
    repo: Path, monkeypatch: pytest.MonkeyPatch, command: str, rule: str
) -> None:
    # An unresolvable cd target or fd base is an unknown place, not an outside
    # one: every rule keeps judging relative operands from the retained cwd.
    monkeypatch.delenv("AIWH_UNSET_NF02088", raising=False)
    assert _bash(repo, command) == rule, command
    # Without the unresolvable cd the same edit is routed to the editor too.
    assert _bash(repo, "sed -i 's/a/b/' src/x.py") == "R6"
