"""Regression for NF-2026-00304: the completion quality gate must REPORT
candidate additions no recognised entry point can reach, and stay quiet when the
addition is reached.

All three quality lenses once passed a candidate whose new code had no caller
anywhere.  Green tests never proved reachability either -- a test can call a new
function directly and pass while no production path ever reaches it.

The decisive tests here exercise the PRODUCTION gate,
``quality_evidence.run_completion_quality_gate`` (whose record is the evidence a
manager reads and is embedded on the accepted card), and the PRODUCTION accept
seam that feeds it, ``ProcessManager._candidate_reachability_inputs`` (which
reads the candidate's own Source Graph index).  They fail if reachability is not
wired into the gate, because the ``reachability`` block would be absent.  The
lower block keeps the pure-function coverage of the reachability semantics.
"""

import sqlite3
import types

from aiworkhub import process_launcher, quality_evidence, source_graph
from aiworkhub.quality_review import (
    REACHABILITY_DISPOSITION,
    REACHABILITY_GATE_ACTION,
    analyze_candidate_reachability,
)

ENTRY = "src/aiworkhub/app.py.main"


def _call(src, dst):
    return {"src": src, "dst": dst}


# --------------------------------------------------------------------------
# Production path: reachability runs inside the evidence gate and its findings
# land in the record a manager reads.
# --------------------------------------------------------------------------


def test_gate_reports_unreachable_addition_in_its_record(tmp_path):
    inputs = {
        "changed_symbols": [{"symbol": "orphan", "file": "m.py", "change": "added"}],
        "call_edges": [_call("main", "reached")],
        "reference_edges": [],
        "entry_points": ["main"],
    }
    gate = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["m.py"], reachability_inputs=inputs
    )
    reach = gate["reachability"]
    assert reach["evaluated"] is True
    assert reach["all_reachable"] is False
    assert reach["unreachable_additions"] == ["orphan"]
    assert reach["findings"][0]["symbol"] == "orphan"
    assert reach["findings"][0]["disposition"] == REACHABILITY_DISPOSITION
    # Reported, never enforced: reachability never blocks the card.
    assert reach["blocking"] is False
    assert reach["gate_action"] == REACHABILITY_GATE_ACTION
    assert not any("orphan" in str(blocker) for blocker in gate["blocking_checks"])


def test_gate_quiet_when_addition_is_reached(tmp_path):
    inputs = {
        "changed_symbols": [{"symbol": "widget", "file": "m.py", "change": "added"}],
        "call_edges": [_call("main", "widget")],
        "entry_points": ["main"],
    }
    gate = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["m.py"], reachability_inputs=inputs
    )
    reach = gate["reachability"]
    assert reach["evaluated"] is True
    assert reach["all_reachable"] is True
    assert reach["findings"] == []
    assert reach["unreachable_additions"] == []


def test_gate_records_reachability_not_evaluated_without_inputs(tmp_path):
    # No Source Graph inputs: the record says so explicitly rather than silently
    # reading as "all reachable" -- reported, still non-blocking.
    gate = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["m.py"]
    )
    reach = gate["reachability"]
    assert reach["evaluated"] is False
    assert reach["all_reachable"] is None
    assert reach["reason"] == "reachability_inputs_unavailable"
    assert reach["findings"] == []


# --------------------------------------------------------------------------
# Production accept seam: the inputs are built from the candidate's own Source
# Graph index, then fed through the gate -- proving the end-to-end wiring fires
# on a real (index-backed) unreachable addition.
# --------------------------------------------------------------------------


def _candidate_index(db_path, edges):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE edges (kind TEXT, src_qualname TEXT, "
            "dst_qualname TEXT, dst_name TEXT)"
        )
        conn.executemany("INSERT INTO edges VALUES (?, ?, ?, ?)", edges)
        conn.commit()
    finally:
        conn.close()


def test_accept_seam_builds_inputs_and_gate_fires_on_orphan(tmp_path):
    ws_root = tmp_path / "candidate"
    module = ws_root / "m.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "def orphan():\n    return 1\n\n\ndef wired():\n    return 2\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "candidate_source_graph.sqlite"
    # Only ``wired`` has an incoming edge from an existing (recognised) symbol.
    _candidate_index(db_path, [("call", "src/aiworkhub/app.py.main", None, "wired")])

    manager = process_launcher.ProcessManager(repo=tmp_path, isolation_enabled=False)
    workspace = types.SimpleNamespace(path=str(ws_root))
    with source_graph.database_path_override(db_path):
        inputs = manager._candidate_reachability_inputs(workspace, ["m.py"])

    assert inputs is not None
    assert {row["symbol"] for row in inputs["changed_symbols"]} >= {"orphan", "wired"}
    assert "main" in inputs["entry_points"]

    gate = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["m.py"], reachability_inputs=inputs
    )
    reach = gate["reachability"]
    assert reach["evaluated"] is True
    assert reach["all_reachable"] is False
    assert reach["unreachable_additions"] == ["orphan"]
    assert "wired" not in reach["unreachable_additions"]


def test_accept_seam_degrades_to_none_without_index(tmp_path):
    # No resolvable candidate index -> None, so the gate records not-evaluated
    # rather than fabricating a reachability verdict.  Never raises.
    ws_root = tmp_path / "candidate"
    module = ws_root / "m.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def orphan():\n    return 1\n", encoding="utf-8")
    manager = process_launcher.ProcessManager(repo=tmp_path, isolation_enabled=False)
    workspace = types.SimpleNamespace(path=str(ws_root))
    assert manager._candidate_reachability_inputs(workspace, ["m.py"]) is None


def test_accept_seam_degrades_when_no_python_changed(tmp_path):
    manager = process_launcher.ProcessManager(repo=tmp_path, isolation_enabled=False)
    workspace = types.SimpleNamespace(path=str(tmp_path / "candidate"))
    assert manager._candidate_reachability_inputs(workspace, ["README.md"]) is None


# --------------------------------------------------------------------------
# Reachability semantics (pure): transitive paths, reference edges, and each
# unreachable addition named individually.
# --------------------------------------------------------------------------


def test_reachability_fires_on_addition_nothing_calls():
    result = analyze_candidate_reachability(
        changed_symbols=[
            {
                "qualname": "src/aiworkhub/mod.py.orphan",
                "file": "src/aiworkhub/mod.py",
                "change": "added",
            },
        ],
        call_edges=[_call(ENTRY, "src/aiworkhub/mod.py.reached")],
        reference_edges=[],
        entry_points=[ENTRY],
    )
    assert result["all_reachable"] is False
    assert result["unreachable_additions"] == ["src/aiworkhub/mod.py.orphan"]
    finding = result["findings"][0]
    assert finding["symbol"] == "src/aiworkhub/mod.py.orphan"
    assert finding["blocking"] is False
    assert finding["disposition"] == REACHABILITY_DISPOSITION
    assert result["blocking"] is False
    assert result["gate_action"] == REACHABILITY_GATE_ACTION


def test_reachability_follows_transitive_paths():
    result = analyze_candidate_reachability(
        changed_symbols=[{"qualname": "pkg.c", "change": "added"}],
        call_edges=[
            _call(ENTRY, "pkg.a"),
            _call("pkg.a", "pkg.b"),
            _call("pkg.b", "pkg.c"),
        ],
        entry_points=[ENTRY],
    )
    assert result["all_reachable"] is True


def test_reachability_uses_reference_edges_not_only_calls():
    result = analyze_candidate_reachability(
        changed_symbols=[{"qualname": "pkg.handler", "change": "added"}],
        call_edges=[_call(ENTRY, "pkg.registry")],
        reference_edges=[_call("pkg.registry", "pkg.handler")],
        entry_points=[ENTRY],
    )
    assert result["all_reachable"] is True


def test_reachability_names_each_unreachable_addition_individually():
    result = analyze_candidate_reachability(
        changed_symbols=[
            {"qualname": "pkg.orphan_one", "file": "a.py", "change": "added"},
            {"qualname": "pkg.orphan_two", "file": "b.py", "change": "modified"},
            {"qualname": "pkg.reached", "file": "c.py", "change": "modified"},
        ],
        call_edges=[_call(ENTRY, "pkg.reached")],
        entry_points=[ENTRY],
    )
    assert result["unreachable_additions"] == ["pkg.orphan_one", "pkg.orphan_two"]
    assert {f["symbol"] for f in result["findings"]} == {
        "pkg.orphan_one",
        "pkg.orphan_two",
    }


def test_reachability_reports_legitimately_unreferenced_public_api():
    # A brand-new public API nothing calls yet: a non-blocking finding a manager
    # disposes of, never a card failure and never silence.
    result = analyze_candidate_reachability(
        changed_symbols=[
            {
                "qualname": "aiworkhub.api.new_public_call",
                "file": "api.py",
                "change": "added",
            }
        ],
        call_edges=[_call(ENTRY, "aiworkhub.api.existing")],
        entry_points=[ENTRY],
    )
    assert result["unreachable_additions"] == ["aiworkhub.api.new_public_call"]
    finding = result["findings"][0]
    assert finding["blocking"] is False
    assert finding["disposition"] == "observation"
    assert "new public API" in finding["reason"]
