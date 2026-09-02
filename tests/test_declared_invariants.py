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


def test_the_cli_exit_code_follows_the_verdict(tmp_path: Path, capsys):
    pkg = tmp_path / "src" / "aiworkhub"
    pkg.mkdir(parents=True)
    (pkg / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert di.main(["--src", str(pkg)]) == 0

    (pkg / "leaky.py").write_text("_X_CACHE: dict = {}\n", encoding="utf-8")
    assert di.main(["--src", str(pkg)]) == 1


@pytest.mark.parametrize("name", di.INVARIANT_NAMES)
def test_every_invariant_is_named_in_the_report(name):
    report = di.check(_PACKAGE)
    assert name in {row["invariant"] for row in report["invariants"]}
