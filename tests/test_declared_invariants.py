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
import ast
import json
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
    # Every tree detector is a blind spot, and says so in its own row rather
    # than claiming it ran: `evaluated: true, violations: 0` for a root that
    # does not exist is the shape NF-2026-00599 was filed against.
    assert report["unevaluable_count"] == len(di._TREE_INVARIANTS)
    tree = {name for name, _ in di._TREE_INVARIANTS}
    rows = [row for row in report["invariants"] if row["invariant"] in tree]
    assert all(row["evaluated"] is False and row["unevaluable"] for row in rows)
    assert all("NotADirectoryError" in row["reason"] for row in rows)

    # The sample says the root could not be read at all, which is not the same
    # fact as a root that was read and held nothing.
    assert report["source_sample"] == {
        "readable": False,
        "modules": 0,
        "reason": "source root could not be read: NotADirectoryError",
    }
    assert report["all_declared_obligations_checked"] is False

    # And the manifest verdict does not claim `absent`. Without an explicit
    # repo_root the manifest path is DERIVED from this unreadable root, so
    # "no manifest there" is a statement about a location that means nothing --
    # it fails closed as unavailable and adds its own violation.
    assert report["rule_coverage"]["status"] == "unavailable"
    assert report["violation_count"] == len(di._TREE_INVARIANTS) + 1
    coverage_breach = report["violations"][-1]
    assert coverage_breach["invariant"] == "declared_rules_are_all_classified"
    assert "derived from a source root" in coverage_breach["detail"]


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

def test_a_source_root_with_no_modules_is_no_sample_not_a_clean_tree(tmp_path: Path):
    """NF-2026-00599. Zero files read is not a clean read of zero violations.

    An existing but empty source root used to give every tree detector
    ``evaluated: true, violations: 0`` and the report ``passed: true`` -- a
    scan that never opened a file, presented as a scan that found nothing. The
    detectors were not breached and are not violations, but they were not
    evaluated either, and the report now says so in both places.
    """

    empty = tmp_path / "src" / "aiworkhub"
    empty.mkdir(parents=True)

    report = di.check(empty)

    assert report["source_sample"]["readable"] is True
    assert report["source_sample"]["modules"] == 0
    assert "no python module" in report["source_sample"]["reason"]

    assert report["unevaluable_count"] == 0
    tree = {name for name, _ in di._TREE_INVARIANTS}
    rows = [row for row in report["invariants"] if row["invariant"] in tree]
    assert rows and all(row["evaluated"] is False for row in rows)
    assert all(row["violations"] == 0 for row in rows)
    assert all("no python module" in row["reason"] for row in rows)
    assert tree <= {row["invariant"] for row in report["unevaluated"]}
    assert report["all_declared_obligations_checked"] is False


def test_a_tree_detector_that_ran_reports_the_sample_it_read(tmp_path: Path):
    """The distinction above is only checkable if the sample size is reported."""

    pkg = tmp_path / "src" / "aiworkhub"
    pkg.mkdir(parents=True)
    (pkg / "one.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "two.py").write_text("OTHER = 2\n", encoding="utf-8")

    report = di.check(pkg)

    assert report["source_sample"] == {"readable": True, "modules": 2, "reason": ""}
    tree = {name for name, _ in di._TREE_INVARIANTS}
    rows = [row for row in report["invariants"] if row["invariant"] in tree]
    assert all(row["evaluated"] is True for row in rows)
    assert all(row["measurement"]["modules_scanned"] == 2 for row in rows)


def test_an_unreadable_root_is_not_a_repository_that_declares_no_rules(tmp_path: Path):
    """NF-2026-00599. ``absent`` is a claim, so it needs a tree to be about.

    Without an explicit ``repo_root`` the manifest path is derived from
    ``src_root``. Derived from a root that cannot be inspected it named
    ``/.aiworkhub/config/development_rules.json`` -- and the missing file there
    was reported as ``not_applicable``: this tree simply does not declare rules.
    That is a positive statement about a tree nobody looked at.
    """

    missing = tmp_path / "gone"
    unreadable = di.load_manifest(missing, None)
    assert unreadable[0] is None
    assert unreadable[2] == "unreadable"
    assert "derived from a source root" in unreadable[1]

    # An explicit repo_root is a different question and keeps its honest answer:
    # this directory is a real place, and it declares no rules.
    absent = di.load_manifest(missing, tmp_path)
    assert absent[0] is None
    assert absent[2] == "absent"


def test_a_manifest_that_cannot_be_stat_ed_is_unreadable_not_absent(tmp_path: Path):
    """``Path.is_file()`` answers False for a file it was not permitted to stat."""

    repo = tmp_path / "repo"
    config = repo / ".aiworkhub" / "config"
    config.mkdir(parents=True)
    manifest = config / "development_rules.json"
    manifest.write_text("{}", encoding="utf-8")

    original_is_file = Path.is_file
    original_stat = Path.stat

    def _denied_is_file(path: Path, *args, **kwargs):
        if path == manifest:
            return False
        return original_is_file(path, *args, **kwargs)

    def _denied_stat(path: Path, *args, **kwargs):
        if path == manifest:
            raise PermissionError(13, "Permission denied", str(manifest))
        return original_stat(path, *args, **kwargs)

    # Injected rather than chmod-ed: the worker sandbox cannot run chmod, and a
    # release qualifies on Windows, where mode bits do not deny a stat at all.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "is_file", _denied_is_file)
        mp.setattr(Path, "stat", _denied_stat)
        result = di.load_manifest(repo / "src" / "aiworkhub", repo)

    assert result[0] is None
    assert result[2] == "unreadable"
    assert "could not be inspected: PermissionError" in result[1]


def test_the_checker_refuses_to_run_as_a_script_rather_than_report_a_broken_tree(
    monkeypatch, capsys
):
    """NF-2026-00599. ``python src/aiworkhub/declared_invariants.py`` cannot work.

    Four invariants and the manifest loader import the package they inspect
    relatively, so a script run resolves none of them: the report that came back
    described a repository with four breaches and unreadable rules, when the
    only thing wrong was the invocation. A checker that cannot run must say so,
    not answer.
    """

    monkeypatch.setattr(di, "__package__", "")
    assert di.main(["--src", str(_PACKAGE)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "python -m aiworkhub.declared_invariants" in captured.err

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
    # `unevaluated` also carries the manifest-scoped obligations now, and a root
    # with no canonical store has no manifest either, so assert membership.
    assert {"invariant": "recent_decisions_record_a_lesson", "reason": row["reason"]} in (
        report["unevaluated"]
    )


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

    # The point is that the learning duty is a VIOLATION here rather than being
    # excused as unevaluated; other invariants may legitimately be unevaluated.
    assert not any(
        row["invariant"] == "recent_decisions_record_a_lesson"
        for row in report["unevaluated"]
    )
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


# --------------------------------------------------------------------------- #
# RM-2026-00048: the manifest is read, and the report says what it does not check
# --------------------------------------------------------------------------- #

_MANIFEST_RELATIVE = Path(".aiworkhub") / "config" / "development_rules.json"
_CANONICAL_MANIFEST = Path(__file__).resolve().parents[1] / _MANIFEST_RELATIVE


def _manifest_mapping() -> dict:
    return json.loads(_CANONICAL_MANIFEST.read_text(encoding="utf-8"))


def _parsed_manifest():
    from aiworkhub.development_rules import parse_manifest

    return parse_manifest(_manifest_mapping())


# A body large enough to clear both declared thresholds (66 AST nodes), so these
# fixtures test the detector rather than the threshold.
_HELPER = '''\
def normalise(value, *, limit):
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > limit:
        text = text[:limit]
    parts = [p for p in text.split(",") if p]
    return ",".join(sorted(parts))
'''

# Identical structure, every identifier renamed: a parallel implementation, not
# a copy.
_HELPER_RENAMED = '''\
def collapse(item, *, ceiling):
    body = str(item).strip()
    if not body:
        return ""
    if len(body) > ceiling:
        body = body[:ceiling]
    pieces = [q for q in body.split(",") if q]
    return ",".join(sorted(pieces))
'''


def _repo_with_manifest(tmp_path: Path, baseline: list[dict] | None = None) -> Path:
    """A repository whose manifest declares the given duplication baseline."""
    root = tmp_path / "repo"
    package = root / "src" / "aiworkhub"
    package.mkdir(parents=True)
    mapping = _manifest_mapping()
    entries = list(baseline or [])
    total = sum(entry["count"] for entry in entries)
    mapping["single_definition_boundary"] = {
        "scan_root": "src/aiworkhub",
        "min_body_nodes": {"copied_helper": 20, "parallel_implementation": 40},
        "patterns": ["copied_helper", "parallel_implementation"],
        "baseline": entries,
        "measurement": {
            "reference_commit": "0" * 40,
            "reference_total": total,
            "current_total": total,
            "accepted_predecessor_delta": 0,
        },
    }
    manifest_path = root / _MANIFEST_RELATIVE
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(mapping), encoding="utf-8")
    return root


def _duplication_violations(report: dict, invariant: str) -> list[dict]:
    return [v for v in report["violations"] if v["invariant"] == invariant]


def test_every_declared_rule_is_either_detected_or_explained():
    """The gate this module now IS: a rule with no detector and no reason fails.

    Empty by construction today, exactly like
    ``dependency_autolaunch.unclassified_denial_reasons``. Adding a rule to
    ``development_rules.json`` makes this fail until someone writes the detector
    or writes down why there is none.
    """
    assert di.undetected_obligations(_parsed_manifest()) == []


def test_a_new_manifest_rule_with_no_detector_is_visible_immediately():
    """Silence about a newly declared rule must be a failure, not a pass."""
    from aiworkhub.development_rules import parse_manifest

    mapping = _manifest_mapping()
    mapping["rules"].append({
        "id": "freshly_invented_rule",
        "topic": "freshly_invented_rule",
        "kind": "forbidden_pattern",
        "applicability": {"languages": ["python"]},
        "allow": [],
        "forbid": ["a_thing_nobody_checks"],
        "payload": {"severity": "error", "rationale_id": "invented"},
    })
    undetected = di.undetected_obligations(parse_manifest(mapping))

    assert undetected == ["freshly_invented_rule:a_thing_nobody_checks"]


def test_the_detector_map_is_checked_against_the_manifest_not_inferred():
    """A map that claims a token the rule does not forbid must refuse, not guess."""
    from aiworkhub.development_rules import parse_manifest

    manifest = parse_manifest(_manifest_mapping())
    original = dict(di.RULE_DETECTORS["single_definition"])
    try:
        di.RULE_DETECTORS["single_definition"]["not_a_declared_token"] = (
            "terminal_vocabulary_has_one_owner"
        )
        with pytest.raises(ValueError, match="does not forbid"):
            di.rule_detector_coverage(manifest)
    finally:
        di.RULE_DETECTORS["single_definition"] = original


def test_the_detector_map_refuses_a_detector_that_does_not_run():
    from aiworkhub.development_rules import parse_manifest

    manifest = parse_manifest(_manifest_mapping())
    original = dict(di.RULE_DETECTORS["single_definition"])
    try:
        di.RULE_DETECTORS["single_definition"]["copied_helper"] = "no_such_detector"
        with pytest.raises(ValueError, match="detectors that do not run"):
            di.rule_detector_coverage(manifest)
    finally:
        di.RULE_DETECTORS["single_definition"] = original


def test_the_report_names_every_obligation_it_does_not_check():
    """`passed` must never be readable as "every declared rule holds"."""
    repo = Path(__file__).resolve().parents[1]
    report = di.check(_PACKAGE, repo_root=repo)
    coverage = report["rule_coverage"]

    assert coverage["status"] == "evaluated"
    # The honest split, stated rather than implied.
    assert report["all_declared_obligations_checked"] is False
    assert coverage["obligations"] == len(coverage["detected"]) + len(
        coverage["accepted_undetected"]
    ) + len(coverage["undetected"])
    assert coverage["declared_rules"] == 20

    named = {row["invariant"] for row in report["unevaluated"]}
    for row in coverage["accepted_undetected"]:
        assert f"{row['rule']}:{row['forbids']}" in named
        assert row["reason"], "an unchecked obligation must carry a reason"


def test_a_copied_helper_is_caught(tmp_path: Path):
    repo = _repo_with_manifest(tmp_path)
    package = repo / "src" / "aiworkhub"
    (package / "one.py").write_text(_HELPER, encoding="utf-8")
    (package / "two.py").write_text(_HELPER, encoding="utf-8")

    report = di.check(package, repo_root=repo)

    found = _duplication_violations(report, "copied_helpers_have_one_definition")
    assert not report["passed"]
    assert {v["path"] for v in found} == {
        "src/aiworkhub/one.py", "src/aiworkhub/two.py",
    }


def test_a_parallel_implementation_is_caught(tmp_path: Path):
    """Same structure, every name different: a re-implementation, not a copy."""
    repo = _repo_with_manifest(tmp_path)
    package = repo / "src" / "aiworkhub"
    (package / "one.py").write_text(_HELPER, encoding="utf-8")
    (package / "two.py").write_text(_HELPER_RENAMED, encoding="utf-8")

    report = di.check(package, repo_root=repo)

    assert not report["passed"]
    assert _duplication_violations(report, "parallel_implementations_have_one_owner")
    # A renamed twin is NOT a byte-identical copy, and must not be reported as one.
    assert not _duplication_violations(report, "copied_helpers_have_one_definition")


def test_a_body_below_the_declared_threshold_is_not_reported(tmp_path: Path):
    """Bodies whose shape is forced by their signature are not duplication.

    Measured on this repository: below 20 nodes the groups are ``return self``,
    a ``now()`` wrapper, a property getter. Reporting them would bury the 37
    real copies in noise.
    """
    repo = _repo_with_manifest(tmp_path)
    package = repo / "src" / "aiworkhub"
    (package / "one.py").write_text("def handle(self):\n    return self._handle\n", encoding="utf-8")
    (package / "two.py").write_text("def handle(self):\n    return self._handle\n", encoding="utf-8")

    report = di.check(package, repo_root=repo)

    assert report["passed"], report["violations"]


def test_the_declared_baseline_is_permitted_and_growth_is_not(tmp_path: Path):
    """The ratchet's whole purpose: today's duplication passes, tomorrow's does not."""
    repo = _repo_with_manifest(tmp_path, baseline=[
        {"path": "src/aiworkhub/one.py", "pattern": "copied_helper", "count": 1},
        {"path": "src/aiworkhub/two.py", "pattern": "copied_helper", "count": 1},
    ])
    package = repo / "src" / "aiworkhub"
    (package / "one.py").write_text(_HELPER, encoding="utf-8")
    (package / "two.py").write_text(_HELPER, encoding="utf-8")

    assert di.check(package, repo_root=repo)["passed"]

    # A third copy is growth in a module the baseline already knows about.
    (package / "two.py").write_text(_HELPER + "\n" + _HELPER.replace("normalise", "normalise2"), encoding="utf-8")
    grown = di.check(package, repo_root=repo)
    assert not grown["passed"]
    assert any(
        "may only descend" in v["detail"]
        for v in _duplication_violations(grown, "copied_helpers_have_one_definition")
    )


def test_a_clean_module_that_becomes_duplicated_is_a_new_identity(tmp_path: Path):
    repo = _repo_with_manifest(tmp_path, baseline=[
        {"path": "src/aiworkhub/one.py", "pattern": "copied_helper", "count": 1},
        {"path": "src/aiworkhub/two.py", "pattern": "copied_helper", "count": 1},
    ])
    package = repo / "src" / "aiworkhub"
    (package / "one.py").write_text(_HELPER, encoding="utf-8")
    (package / "two.py").write_text(_HELPER, encoding="utf-8")
    (package / "three.py").write_text(_HELPER, encoding="utf-8")

    report = di.check(package, repo_root=repo)

    assert not report["passed"]
    assert any(
        v["path"] == "src/aiworkhub/three.py" and "baseline records as having none" in v["detail"]
        for v in _duplication_violations(report, "copied_helpers_have_one_definition")
    )


def test_the_canonical_tree_matches_its_own_declared_baseline():
    """The baseline must describe this tree exactly, or the ratchet is fiction."""
    manifest = _parsed_manifest()
    boundary = manifest.single_definition_boundary
    assert boundary is not None

    thresholds = {p: boundary.threshold(p) for p in boundary.patterns}
    counts = di.duplicate_definition_counts(_PACKAGE, thresholds)
    declared: dict[str, dict[str, int]] = {}
    for entry in boundary.baseline:
        declared.setdefault(entry.pattern, {})[entry.path] = entry.count

    assert counts["copied_helper"] == declared.get("copied_helper", {})
    assert counts["parallel_implementation"] == declared.get("parallel_implementation", {})
    assert sum(
        sum(v.values()) for v in counts.values()
    ) == boundary.current_total


def test_a_parallel_scan_and_a_sequential_scan_agree(monkeypatch):
    """Parallelism may change how fast, never what is measured."""
    parallel = di.collect_definitions(_PACKAGE)
    monkeypatch.setattr(di, "_scan_workers", lambda count: 1)
    sequential = di.collect_definitions(_PACKAGE)

    assert parallel == sequential
    assert len(parallel) > 1000, "the scan must be exhaustive, not a sample"


def test_the_worker_count_is_derived_and_leaves_headroom(monkeypatch):
    """Derived from the observed cores, never a constant, and never all of them."""
    monkeypatch.setattr(di.os, "cpu_count", lambda: 16)
    assert di._scan_workers(500) == 16 - di._SCAN_CORE_HEADROOM

    monkeypatch.setattr(di.os, "cpu_count", lambda: 64)
    assert di._scan_workers(500) == 64 - di._SCAN_CORE_HEADROOM

    # A machine too small to spare a core still runs, sequentially.
    monkeypatch.setattr(di.os, "cpu_count", lambda: 2)
    assert di._scan_workers(500) == 1

    # And a scan too small to pay for the pool does not start one.
    monkeypatch.setattr(di.os, "cpu_count", lambda: 16)
    assert di._scan_workers(di._PARALLEL_SCAN_MIN_MODULES - 1) == 1


def test_shape_erasure_keeps_child_nodes(tmp_path: Path):
    """Erasing ``value`` by field name collapsed every return in the package.

    ``Constant.value`` is a literal; ``Return.value`` and ``Assign.value`` are
    whole subtrees. Erasing by name reported 324 duplicate definitions where
    reading the fields exactly finds 82, so this pins the distinction.
    """
    returns_a_call = ast.parse("def f():\n    return helper(1, 2)\n").body[0]
    returns_an_attribute = ast.parse("def g():\n    return thing.field\n").body[0]

    assert di._shape_dump(returns_a_call) != di._shape_dump(returns_an_attribute)

    # A pure rename, though, must still share a shape.
    renamed = ast.parse("def h():\n    return other(1, 2)\n").body[0]
    assert di._shape_dump(returns_a_call) == di._shape_dump(renamed)
