"""RM-2026-00048: the rules the repository declares, executed.

``development_rules.json`` says what must never be true in prose. A rule nobody
runs is a comment, and this repository proved it: over one session, hand auditing
found the terminal-outcome vocabulary written out six times with three copies
already drifted, two probe caches growing one entry per git commit forever, nine
``with sqlite3.connect(...)`` blocks that never closed their connection, and one
policy -- do POSIX mode bits apply here -- answered by two different predicates.

Every one of those is mechanically checkable. Run the same checker against
``9fc51ad``, the commit this session started from, and it reports 11 violations:
nine sqlite context managers, one unbounded cache, one split predicate. What
took hours by hand is seconds by machine, and it stays found.

These tests pin three things: the invariants hold on the current tree, the
checker actually fails when each is breached, and an invariant that cannot be
evaluated reports itself rather than passing quietly -- because "could not check"
and "checked and clean" must never look the same.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import declared_invariants as di  # noqa: E402

_PACKAGE = _SRC / "aiworkhub"


def test_the_current_tree_holds_every_declared_invariant():
    """The canonical tree is the baseline: it must be clean, not merely better."""
    report = di.check(_PACKAGE)
    assert report["passed"], report["violations"]
    assert report["violation_count"] == 0
    assert {row["invariant"] for row in report["invariants"]} == set(di.INVARIANT_NAMES)


def test_an_unbounded_module_cache_is_caught(tmp_path: Path):
    pkg = tmp_path / "src" / "aiworkhub"
    pkg.mkdir(parents=True)
    (pkg / "leaky.py").write_text(
        "_THING_CACHE: dict[str, str] = {}\n\n\ndef put(k, v):\n    _THING_CACHE[k] = v\n",
        encoding="utf-8",
    )
    report = di.check(pkg)
    assert not report["passed"]
    assert any(
        v["invariant"] == "module_level_caches_are_bounded" for v in report["violations"]
    )


def test_a_bounded_cache_using_a_shared_helper_is_not_flagged(tmp_path: Path):
    """Eviction through a helper taking the cache as a parameter still counts.

    That is the shape this repository moved TO when the two probe caches were
    fixed, so a checker that only looked for the global name would have flagged
    the corrected code.
    """
    pkg = tmp_path / "src" / "aiworkhub"
    pkg.mkdir(parents=True)
    (pkg / "ok.py").write_text(
        "from collections import OrderedDict\n\n"
        "_THING_CACHE_MAX_ENTRIES = 32\n"
        "_THING_CACHE: OrderedDict = OrderedDict()\n\n\n"
        "def _store(cache, key, value, *, max_entries):\n"
        "    cache[key] = value\n"
        "    while len(cache) > max_entries:\n"
        "        cache.popitem(last=False)\n",
        encoding="utf-8",
    )
    report = di.check(pkg)
    assert report["passed"], report["violations"]


def test_a_cache_declared_bounded_by_construction_is_exempt_with_a_reason():
    """The exemption carries a measured reason, so it stays reviewable."""
    assert di.BOUNDED_BY_CONSTRUCTION, "an empty exemption table hides nothing"
    for (filename, name), reason in di.BOUNDED_BY_CONSTRUCTION.items():
        assert filename.endswith(".py")
        assert name.startswith("_")
        assert len(reason) > 40, f"{name} exemption must state why, not just that"


def test_an_unclosed_sqlite_context_manager_is_caught(tmp_path: Path):
    pkg = tmp_path / "src" / "aiworkhub"
    pkg.mkdir(parents=True)
    (pkg / "dbleak.py").write_text(
        "import sqlite3\n\n\ndef f(p):\n    with sqlite3.connect(p) as c:\n        return c\n",
        encoding="utf-8",
    )
    report = di.check(pkg)
    assert any(
        v["invariant"] == "sqlite_context_managers_close" for v in report["violations"]
    )


def test_a_closing_wrapped_connection_is_not_flagged(tmp_path: Path):
    pkg = tmp_path / "src" / "aiworkhub"
    pkg.mkdir(parents=True)
    (pkg / "dbok.py").write_text(
        "import sqlite3\nfrom contextlib import closing\n\n\n"
        "def f(p):\n    with closing(sqlite3.connect(p)) as c:\n        return c\n",
        encoding="utf-8",
    )
    report = di.check(pkg)
    assert report["passed"], report["violations"]


def test_the_terminal_vocabulary_is_one_object_not_equal_copies():
    """Equality is not enough: equal copies are what drifted."""
    from aiworkhub import callback_store, process_launcher, task_fsm, task_store

    assert process_launcher.TERMINAL_PROCESS_STATES is task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES
    assert callback_store.CALLBACK_ELIGIBLE_TRANSITIONS is task_fsm.TERMINAL_CALLBACK_CLASSES
    assert task_store._ATOMIC_CALLBACK_TRANSITIONS is task_fsm.TERMINAL_CALLBACK_CLASSES
    assert di.terminal_vocabulary_has_one_owner() == []


def test_one_policy_is_decided_by_one_predicate():
    assert di.one_policy_one_predicate() == []


def test_an_unevaluable_invariant_reports_itself_rather_than_passing(monkeypatch):
    """'Could not check' must never be indistinguishable from 'clean'."""

    def _explode() -> list[di.Violation]:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(
        di, "_RUNTIME_INVARIANTS", (("one_policy_one_predicate", _explode),)
    )
    report = di.check(_PACKAGE)
    assert not report["passed"]
    assert any("could not be evaluated" in v["detail"] for v in report["violations"])


def test_exception_with_broken_string_still_fails_closed(monkeypatch, capsys, tmp_path):
    class UnformattableError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("diagnostic formatting failed")

    def _explode(_root: Path) -> list[di.Violation]:
        raise UnformattableError

    monkeypatch.setattr(
        di, "_TREE_INVARIANTS", (("module_level_caches_are_bounded", _explode),)
    )

    report = di.check(_PACKAGE)
    assert report["passed"] is False
    assert report["violations"][0]["path"] == str(_PACKAGE)
    assert report["violations"][0]["detail"].endswith("UnformattableError")

    assert di.main(["--src", str(_PACKAGE), "--repo", str(tmp_path)]) == 1
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["passed"] is False
    assert cli_report["violations"][0]["detail"].endswith("UnformattableError")


def test_an_unevaluable_tree_invariant_is_json_and_nonzero(monkeypatch, capsys, tmp_path):
    def _explode(_root: Path) -> list[di.Violation]:
        raise RuntimeError("tree index unavailable")

    monkeypatch.setattr(
        di, "_TREE_INVARIANTS", (("module_level_caches_are_bounded", _explode),)
    )

    # --repo names a directory that owns no canonical store, so the count below
    # is the tree verdict alone and not a reading of whatever task database the
    # machine running the suite happens to have.
    assert di.main(["--src", str(_PACKAGE), "--repo", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["violation_count"] == 1
    assert report["violations"][0]["invariant"] == "module_level_caches_are_bounded"
    assert "RuntimeError: tree index unavailable" in report["violations"][0]["detail"]


@pytest.mark.parametrize("make_file", [False, True])
def test_missing_or_nondirectory_source_root_fails_closed(tmp_path: Path, make_file: bool):
    root = tmp_path / "not-a-package"
    if make_file:
        root.write_text("not a directory\n", encoding="utf-8")

    report = di.check(root)

    assert not report["passed"]
    assert report["violation_count"] == len(di._TREE_INVARIANTS)
    assert all(v["path"] == str(root) for v in report["violations"])
    assert all("NotADirectoryError" in v["detail"] for v in report["violations"])


def test_source_read_failure_is_not_treated_as_clean(tmp_path: Path, monkeypatch):
    pkg = tmp_path / "src" / "aiworkhub"
    pkg.mkdir(parents=True)
    source = pkg / "broken.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    original_read_text = Path.read_text

    def _fail_read(path: Path, *args, **kwargs):
        if path == source:
            raise OSError("injected read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fail_read)
    report = di.check(pkg)

    assert not report["passed"]
    assert any(v["path"] == str(source) and "OSError" in v["detail"] for v in report["violations"])


def test_invalid_utf8_and_syntax_errors_fail_closed(tmp_path: Path):
    for filename, content in (("decode.py", b"\xff"), ("syntax.py", b"def nope(:\n")):
        pkg = tmp_path / filename / "aiworkhub"
        pkg.mkdir(parents=True)
        source = pkg / filename
        source.write_bytes(content)

        report = di.check(pkg)

        assert not report["passed"]
        assert any(v["path"] == str(source) for v in report["violations"])


def test_the_cli_exit_code_follows_the_verdict(tmp_path: Path, capsys):
    # --repo is pinned at a directory that owns no canonical store, so this
    # asserts the tree verdict and never the developer's live task database.
    pkg = tmp_path / "src" / "aiworkhub"
    pkg.mkdir(parents=True)
    (pkg / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert di.main(["--src", str(pkg), "--repo", str(tmp_path)]) == 0

    (pkg / "leaky.py").write_text("_X_CACHE: dict = {}\n", encoding="utf-8")
    assert di.main(["--src", str(pkg), "--repo", str(tmp_path)]) == 1

    capsys.readouterr()
    missing = tmp_path / "missing"
    assert di.main(["--src", str(missing), "--repo", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["src_root"] == str(missing)
    assert "source root is not a directory" in report["violations"][0]["detail"]


@pytest.mark.parametrize("name", di.INVARIANT_NAMES)
def test_every_invariant_is_named_in_the_report(name):
    report = di.check(_PACKAGE)
    assert name in {row["invariant"] for row in report["invariants"]}


# --------------------------------------------------------------------------- #
# the learning duty is discharged
#
# The duty was named at the decision (`accept_review` and `reject_review` return
# `learning_commit_owed`), measured in health (`learning_coverage`), and enforced
# nowhere. Measured on this repository on 2026-09-07: 105 decided cards in the
# 14-day window, 23 with a lesson, and the runs without one, newest first, are
# 6, 15, 1, 2, 14, 6, 38 -- lessons arrive in bursts and then stop. These tests
# pin the enforcement: a run past the limit is a violation, one lesson for one of
# the newest decisions clears it, a repository that decided nothing owes nothing,
# and a root with no store reports itself unevaluated rather than clean.
# --------------------------------------------------------------------------- #

# Seeding a canonical store is the same job in both files; sharing it keeps one
# definition of what a decided card looks like.
from test_learning_coverage_is_measured import _card, _repo, _seed  # noqa: E402


def _decided(root: Path, count: int, *, lessons: list[str]) -> None:
    """``count`` decided cards, newest first as ``D0``, ``D1``, ..."""
    _seed(
        root,
        [
            _card(f"D{i}", status="finished", topic="coding", age_days=1 + i)
            for i in range(count)
        ],
        lessons=lessons,
    )


def _learning_row(report: dict) -> dict:
    rows = [
        row for row in report["invariants"]
        if row["invariant"] == "recent_decisions_record_a_lesson"
    ]
    assert len(rows) == 1, "the invariant must be reported exactly once"
    return rows[0]


def test_a_run_of_decisions_recording_no_lesson_is_a_violation(tmp_path: Path):
    root = _repo(tmp_path)
    _decided(root, di.MAX_DECISIONS_WITHOUT_A_LESSON + 1, lessons=[])

    report = di.check(_PACKAGE, repo_root=root)

    assert not report["passed"]
    breach = [
        v for v in report["violations"]
        if v["invariant"] == "recent_decisions_record_a_lesson"
    ]
    assert len(breach) == 1
    detail = breach[0]["detail"]
    assert "3 most recently decided cards recorded no lesson" in detail
    # It names the cards that can clear it, so the manager is not left guessing
    # which decision the gate is about.
    assert "D0" in detail and "aiworkhub_manager_learning_commit" in detail
    assert _learning_row(report)["evaluated"] is True


def test_one_lesson_for_the_newest_decision_clears_the_run(tmp_path: Path):
    """Reachability is the whole design: a gate nobody can clear is a wedge."""
    root = _repo(tmp_path)
    _decided(root, 40, lessons=["D0"])

    report = di.check(_PACKAGE, repo_root=root)

    assert report["passed"], report["violations"]


def test_a_skip_inside_the_limit_is_not_accused(tmp_path: Path):
    """1 and 2 are what a deliberate skip measured like; 6 and up are not."""
    root = _repo(tmp_path)
    _decided(root, 6, lessons=["D2", "D3", "D4", "D5"])

    report = di.check(_PACKAGE, repo_root=root)

    assert report["passed"], report["violations"]


def test_a_repository_that_has_decided_nothing_owes_nothing(tmp_path: Path):
    """An absent denominator must never read as zero coverage."""
    root = _repo(tmp_path)
    _seed(root, [_card("RUNNING", status="processing", topic="coding", age_days=1)], [])

    report = di.check(_PACKAGE, repo_root=root)

    assert report["passed"], report["violations"]
    assert _learning_row(report)["evaluated"] is True


def test_a_root_with_no_canonical_store_reports_unevaluated_not_clean(tmp_path: Path):
    """A worker's worktree holds src/ and tests/ and never owed this duty."""
    report = di.check(_PACKAGE, repo_root=tmp_path)

    assert report["passed"], report["violations"]
    row = _learning_row(report)
    assert row["evaluated"] is False
    assert row["reason"].startswith("not_an_aiworkhub_repository:")
    assert report["unevaluated"] == [
        {"invariant": "recent_decisions_record_a_lesson", "reason": row["reason"]}
    ]


def test_no_repository_root_is_reported_rather_than_silently_skipped():
    report = di.check(_PACKAGE)

    row = _learning_row(report)
    assert row["evaluated"] is False
    assert row["reason"] == "no_repository_root_supplied"
    assert report["repo_root"] == ""


def test_a_store_that_cannot_be_measured_fails_closed(tmp_path: Path, monkeypatch):
    """'Could not measure' must not be indistinguishable from 'duty discharged'."""
    root = _repo(tmp_path)
    _decided(root, 5, lessons=[])

    from aiworkhub import learning_commit_store

    def _explode(*_args, **_kwargs):
        raise RuntimeError("canonical store unreadable")

    monkeypatch.setattr(learning_commit_store, "coverage", _explode)
    report = di.check(_PACKAGE, repo_root=root)

    assert not report["passed"]
    assert any(
        v["invariant"] == "recent_decisions_record_a_lesson"
        and "could not be evaluated" in v["detail"]
        for v in report["violations"]
    )


def test_only_a_missing_manifest_makes_the_duty_not_applicable(tmp_path: Path, monkeypatch):
    """The applicability probe must not swallow a repository that is simply broken.

    "There is no repository here" and "this repository's manifest cannot be
    read" are different facts, and only the first is not applicable. A probe
    that catches everything turns the second into a clean report.
    """
    root = _repo(tmp_path)
    _decided(root, 5, lessons=[])

    from aiworkhub import task_store

    def _explode(*_args, **_kwargs):
        raise RuntimeError("manifest unreadable")

    monkeypatch.setattr(task_store, "inspect_repository", _explode)
    report = di.check(_PACKAGE, repo_root=root)

    assert not report["passed"], "a repository that cannot be probed is not clean"
    assert report["unevaluated"] == []
    assert any(
        v["invariant"] == "recent_decisions_record_a_lesson"
        and "could not be evaluated" in v["detail"]
        for v in report["violations"]
    )


def test_an_absent_denominator_wins_over_any_run(tmp_path: Path, monkeypatch):
    """No decided cards means no duty, whatever else the measurement carries.

    A repository that has decided nothing must never read as zero coverage, and
    the rule has to be stated where the verdict is made rather than left to fall
    out of how the run happens to be counted today.
    """
    root = _repo(tmp_path)

    from aiworkhub import learning_commit_store

    monkeypatch.setattr(
        learning_commit_store,
        "coverage",
        lambda *_a, **_k: {
            "decided_cards": 0,
            "consecutive_recent_without_lesson": 99,
            "recent_without_lesson": [],
            "coverage_percent": None,
            "window_days": 14,
        },
    )

    assert di.recent_decisions_record_a_lesson(root) == []


def test_the_checker_never_writes_a_lesson(tmp_path: Path):
    """Authorship stays with the manager: a fabricated lesson is worse than none."""
    import sqlite3

    from aiworkhub import task_store

    root = _repo(tmp_path)
    _decided(root, 5, lessons=["D4"])
    db = task_store.canonical_db_path(root)

    def _rows() -> list[tuple]:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            return conn.execute(
                "SELECT commit_id, task_id, payload_sha256 FROM learning_commits"
                " ORDER BY commit_id"
            ).fetchall()

    before = _rows()
    report = di.check(_PACKAGE, repo_root=root)

    assert not report["passed"], "this fixture is in breach; the test is about writes"
    assert _rows() == before
