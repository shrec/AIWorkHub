"""A call to `len` is not unresolved work; it is not work.

``resolved_ratio`` divided resolved edges by every call edge, including calls
to language builtins, which can never resolve to repository code. Measured on
AIWorkHub: 47,046 of 212,603 call edges name a Python builtin -- ``str``
11,970, ``isinstance`` 4,834, ``setattr`` 4,512, ``len`` 4,382 -- so more than
a fifth of the graph was counted as outstanding resolution work that no
resolver could ever do.

No lens used those rows either. ``deadmethods`` counts only RESOLVED incoming
calls, and ``gaps`` has to filter builtins out explicitly before it reports.

They are reported out of the denominator rather than deleted. An edge saying
"this function calls str" is true, and destroying true rows to improve a number
is how a metric starts steering the data instead of describing it. Both ratios
are published: ``resolved_ratio`` unchanged so history stays comparable and the
observed floor keeps guarding the same series, and
``resolved_ratio_resolvable`` as the honest one.

Run: python3 -m pytest -q tests/test_builtin_calls_leave_the_denominator.py
"""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _quality(repo: Path) -> dict:
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='index_quality'"
        ).fetchone()
        return json.loads(row["value"])
    finally:
        conn.close()


def _repo(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="repo")
    (root / "a.py").write_text(source, encoding="utf-8")
    sg.build_index(root, incremental=False)
    return root


def test_builtin_calls_are_counted_and_removed_from_the_denominator(tmp_path):
    repo = _repo(tmp_path, (
        "class Worker:\n"
        "    def run(self, items):\n"
        "        n = len(items)\n"
        "        s = str(n)\n"
        "        return self._step(s)\n"
        "\n"
        "    def _step(self, s):\n"
        "        return sorted(s)\n"
    ))
    edges = _quality(repo)["edges"]

    assert edges["language_builtin_targets"] > 0
    assert edges["resolvable"] == edges["total"] - edges["language_builtin_targets"]
    assert edges["resolved_ratio_resolvable"] >= edges["resolved_ratio"], (
        "removing targets nothing can resolve can only raise the ratio"
    )


def test_the_original_ratio_is_left_exactly_as_it_was(tmp_path):
    """History stays comparable and the observed floor guards one series."""
    repo = _repo(tmp_path, (
        "class Worker:\n"
        "    def run(self):\n"
        "        return len(self._step())\n"
        "\n"
        "    def _step(self):\n"
        "        return []\n"
    ))
    edges = _quality(repo)["edges"]
    assert edges["resolved_ratio"] == round(
        edges["resolved"] / edges["total"], 6
    )


def test_a_repository_with_no_builtin_calls_reports_the_same_two_ratios(tmp_path):
    repo = _repo(tmp_path, (
        "class Worker:\n"
        "    def run(self):\n"
        "        return self._step()\n"
        "\n"
        "    def _step(self):\n"
        "        return 1\n"
    ))
    edges = _quality(repo)["edges"]
    assert edges["language_builtin_targets"] == 0
    assert edges["resolved_ratio_resolvable"] == edges["resolved_ratio"]


def test_the_rows_are_still_there(tmp_path):
    """Reported out of a denominator is not deleted from the graph."""
    repo = _repo(tmp_path, (
        "class Worker:\n"
        "    def run(self, items):\n"
        "        return len(items)\n"
    ))
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        kept = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='calls' AND dst_name='len'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert kept > 0
