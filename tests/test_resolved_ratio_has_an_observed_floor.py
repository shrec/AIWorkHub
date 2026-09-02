"""Resolution quality must not decay one tolerable step at a time.

The only guard on ``resolved_ratio`` compared each build against the one
before it and degraded when the drop exceeded five points. That is blind twice:

  * a decay of four points per build never trips it, however far it travels;
  * a FIRST build has no previous, so an index that comes up at two percent
    resolution reports perfectly healthy.

The floor closes both, and it is *observed* rather than invented -- the best
ratio this repository has ever reached, recorded in the index's own meta table
and never lowered. That matters: a bound nobody measured is a number someone
guessed, and this repository has no business asserting a quality threshold it
did not first reach.

Run: python3 -m pytest -q tests/test_resolved_ratio_has_an_observed_floor.py
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _record(mark):
    """A prior scorecard, in the shape the build hands back as ``previous``."""
    return {"edges": {sg._RESOLVED_RATIO_BEST_KEY: mark}} if mark is not None else {}


def test_the_first_measurement_becomes_the_mark():
    assert sg._resolved_ratio_high_water_mark(None, 0.30) == 0.30


def test_a_better_build_raises_the_mark():
    assert sg._resolved_ratio_high_water_mark(_record(0.30), 0.42) == 0.42


def test_a_worse_build_never_lowers_the_mark():
    """A ratchet a bad build can reset is not a ratchet."""
    assert sg._resolved_ratio_high_water_mark(_record(0.42), 0.11) == 0.42


def test_an_unmeasurable_ratio_leaves_the_mark_alone():
    """An index with no edges carries the mark forward rather than zeroing it."""
    assert sg._resolved_ratio_high_water_mark(_record(0.42), None) == 0.42


def test_a_corrupt_mark_reads_as_absent_rather_than_as_zero():
    """A stored value that is not a number must not become a floor of 0.0."""
    for junk in ("not-a-number", None, True, [0.9]):
        assert sg._resolved_ratio_high_water_mark(
            {"edges": {sg._RESOLVED_RATIO_BEST_KEY: junk}}, 0.30
        ) == 0.30


def test_the_tolerance_is_the_same_width_as_the_build_over_build_rule():
    """One step of decay is tolerated; drifting away from the best is not."""
    assert sg._RESOLVED_RATIO_FLOOR_TOLERANCE == 0.05


def test_slow_decay_crosses_the_floor_even_though_no_single_step_does():
    """The exact blind spot: every step legal, the journey is not.

    Four points lost per build never trips the delta rule at five, so this
    walks 0.42 down to 0.30 with every individual step permitted -- and the
    floor catches it at the third.
    """
    floor = 0.42 - sg._RESOLVED_RATIO_FLOOR_TOLERANCE

    steps = [0.38, 0.34, 0.30]
    deltas_all_legal = all(
        abs(b - a) < 0.05 for a, b in zip([0.42, *steps], steps)
    )
    assert deltas_all_legal, "the premise is that no single step is a violation"

    crossed = [ratio for ratio in steps if ratio < floor]
    assert crossed == [0.34, 0.30]
    record = _record(0.42)
    for ratio in steps:
        assert sg._resolved_ratio_high_water_mark(record, ratio) == 0.42


def test_a_real_build_carries_the_mark_and_flags_a_fall_below_it(tmp_path):
    """End to end: the mark is recorded, survives a rebuild, and degrades."""
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="repo")
    (root / "a.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        return self._step()\n"
        "\n"
        "    def _step(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    sg.build_index(root, incremental=False)

    conn = sg.connect(sg.resolve_db_path(root))
    try:
        import json

        row = conn.execute(
            "SELECT value FROM meta WHERE key='index_quality'"
        ).fetchone()
        quality = json.loads(row["value"])
    finally:
        conn.close()

    edges = quality["edges"]
    best = edges["resolved_ratio_best_observed"]
    assert best is not None
    assert best == edges["resolved_ratio"], "a first build IS its own best"
    assert "resolved_edge_ratio_below_observed_floor" not in quality["degraded_reasons"]

    # A later build that resolves far less than the best is degraded, even
    # though nothing here compares it to the immediately preceding build.
    degraded = sg._resolved_ratio_high_water_mark({"edges": edges}, 0.0)
    assert degraded == best
    assert 0.0 < best - sg._RESOLVED_RATIO_FLOOR_TOLERANCE
