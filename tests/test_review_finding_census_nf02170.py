"""NF-2026-02170 / RM-2026-00086 phase 0: the reviewer-finding census.

Runs ``scripts/review_finding_census.py`` over a synthetic fixture store, never
a real one: the repository is public and the real report is not committed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_finding_census.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("review_finding_census_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


census = _load_script()

# (finding id, text). F4 is stored under the bare id; its disposition row uses
# the full reviewer:<lens>:<id> form.
FINDINGS = [
    ("reviewer:correctness:F1", "No test covers the new branch; add a regression test."),
    ("reviewer:correctness:F2", "The helper is never called from production code; it is dead code."),
    ("reviewer:correctness:F3", "except Exception: pass silently swallows the error."),
    ("F4", "A path traversal via ../ lets the argv option escape the worktree."),
    ("reviewer:correctness:F5", "Race between check and write; two threads can interleave."),
    ("reviewer:correctness:F6", "Zorp frobnicates the quux."),
    ("reviewer:docs:F7", "The docstring is stale and the CHANGELOG entry is missing."),
    ("reviewer:portability:F8", "Windows backslash paths break; chmod is POSIX only."),
    ("reviewer:correctness:F9", "File handle is never closed, leaking a descriptor."),
]
DISPOSITIONS = {
    "reviewer:correctness:F1": "confirmed",
    "reviewer:correctness:F2": "confirmed",
    "reviewer:correctness:F3": "dismissed",
    "reviewer:security:F4": "confirmed",
    "reviewer:correctness:F5": "confirmed",
}
REJECT_REASONS = [
    "rework: missing tests for the new option",
    "Out of scope edit outside allowed_writes violates the contract",
    "Unvalidated int() over candidate text; unbounded digits",
]
# A disposition row whose receipt carries the finding text but whose reviewer
# report is not in task_events.
DISPOSITION_ONLY = ("reviewer:docs:F10", "Docstring drift: README out of date")

EXPECTED = {
    "missing_test": (2, 2),
    "unwired_production_code": (1, 1),
    "swallowed_exception": (1, 0),
    "path_or_option_injection": (1, 1),
    "concurrency_race": (1, 1),
    "resource_leak": (1, 0),
    "platform_portability": (1, 0),
    "missing_input_validation": (1, 1),
    "scope_or_contract_violation": (1, 1),
    "docs_drift": (2, 1),
    "wrong_error_handling": (0, 0),
    "other": (1, 0),
}
TOTAL = 13


def _make_store(path: Path) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, event TEXT, "
            "runner TEXT, payload_json TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE review_orchestrator_finding_dispositions ("
            "target_task_id TEXT, request_id TEXT, reviewer_request_id TEXT, finding_id TEXT, "
            "disposition TEXT, actor TEXT, decided_at TEXT, receipt_json TEXT)"
        )

        def event(task_id: str, name: str, payload: dict) -> None:
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, name, "r", json.dumps(payload), "2026-01-01T00:00:00Z"),
            )

        findings = [{"id": fid, "summary": text} for fid, text in FINDINGS]
        event("T-1", "reviewer_receipt", {"report": {"findings": findings[:5]}})
        # A replayed receipt repeats F5 and adds a finding with no text.
        event(
            "T-1",
            "reviewer_receipt",
            {"report": {"findings": findings[4:] + [{"id": "F-EMPTY"}]}},
        )
        for reason in REJECT_REASONS:
            event("T-1", "reject_review", {"reason": reason})
        event("T-1", "unrelated", {"reason": "No test covers this, but it is not a reject"})

        for finding_id, disposition in DISPOSITIONS.items():
            conn.execute(
                "INSERT INTO review_orchestrator_finding_dispositions VALUES (?,?,?,?,?,?,?,?)",
                ("T-1", "req", "rev", finding_id, disposition, "mgr", "2026-01-01", "{}"),
            )
        only_id, only_text = DISPOSITION_ONLY
        conn.execute(
            "INSERT INTO review_orchestrator_finding_dispositions VALUES (?,?,?,?,?,?,?,?)",
            (
                "T-1", "req", "rev", only_id, "confirmed", "mgr", "2026-01-01",
                json.dumps({"finding": {"summary": only_text}}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    return _make_store(tmp_path / "fixture.sqlite")


def _report(store: Path) -> dict:
    conn = census.open_store(store)
    try:
        return census.build_report(conn)
    finally:
        conn.close()


def test_fixture_yields_expected_category_counts(store: Path) -> None:
    report = _report(store)
    assert report["total_items"] == TOTAL
    assert report["finding_items"] == 10
    assert report["reject_reason_items"] == 3
    got = {
        row["category"]: (row["count"], row["confirmed_count"]) for row in report["categories"]
    }
    assert got == EXPECTED
    assert report["findings_without_text"] == 1
    assert report["sources"] == {
        "review_orchestrator_finding_dispositions": "ok",
        "task_events": "ok",
    }


def test_report_shape_share_flags_and_examples_are_ids_only(store: Path) -> None:
    report = _report(store)
    rows = {row["category"]: row for row in report["categories"]}
    assert set(rows) == {category.name for category in census.CATEGORIES}
    assert sum(row["share"] for row in rows.values()) == pytest.approx(1.0, abs=0.01)
    assert rows["missing_test"]["share"] == round(2 / TOTAL, 4)
    for row in rows.values():
        assert isinstance(row["mechanically_detectable"], bool)
        assert row["rule_idea"].strip() and "\n" not in row["rule_idea"]
        assert len(row["examples"]) <= census.MAX_EXAMPLES
        assert row["count"] >= len(row["examples"])
    assert rows["swallowed_exception"]["mechanically_detectable"] is True
    assert rows["concurrency_race"]["mechanically_detectable"] is False
    assert rows["path_or_option_injection"]["examples"] == ["F4"]
    assert rows["missing_test"]["examples"][0] == "reviewer:correctness:F1"
    assert [row["count"] for row in report["categories"]] == sorted(
        (row["count"] for row in report["categories"]), reverse=True
    )
    # No finding or reason body leaks into the report.
    dumped = json.dumps(report)
    for _fid, text in FINDINGS + [DISPOSITION_ONLY]:
        assert text not in dumped
    for reason in REJECT_REASONS:
        assert reason not in dumped
    assert 0 < report["mechanically_detectable_share"] < 1


def test_category_table_is_fixed_and_unknown_text_lands_in_other() -> None:
    names = [category.name for category in census.CATEGORIES]
    assert len(names) == len(set(names))
    assert names[-1] == "other"
    assert {
        "missing_test", "unwired_production_code", "swallowed_exception",
        "path_or_option_injection", "missing_input_validation", "concurrency_race",
        "scope_or_contract_violation", "platform_portability", "resource_leak",
        "wrong_error_handling", "docs_drift", "other",
    } == set(names)
    assert census.classify("Zorp frobnicates the quux.") == "other"
    assert census.classify("") == "other"
    assert census.classify("No test covers the new branch") == "missing_test"


def test_store_is_opened_read_only_and_a_write_fails(store: Path) -> None:
    conn = census.open_store(store)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "INSERT INTO task_events(task_id, event, payload_json) VALUES ('x','y','{}')"
            )
            conn.commit()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("CREATE TABLE scratch (a)")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM review_orchestrator_finding_dispositions")
    finally:
        conn.close()


def test_missing_store_is_refused_not_created(tmp_path: Path) -> None:
    absent = tmp_path / "nope.sqlite"
    with pytest.raises(FileNotFoundError):
        census.open_store(absent)
    assert not absent.exists()
    assert census.main(["--db", str(absent)]) == 2


def test_store_without_tables_reports_absent_sources(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    report = _report(empty)
    assert report["total_items"] == 0
    assert report["mechanically_detectable_share"] == 0.0
    assert set(report["sources"].values()) == {"absent"}
    assert all(row["count"] == 0 and row["share"] == 0.0 for row in report["categories"])


def test_cli_prints_report_and_out_writes_the_same_json(
    store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = hashlib.sha256(store.read_bytes()).hexdigest()
    out = tmp_path / "report.json"
    assert census.main(["--db", str(store), "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert json.loads(out.read_text(encoding="utf-8")) == printed
    assert printed["total_items"] == TOTAL
    assert printed["schema_id"] == census.SCHEMA_ID
    assert hashlib.sha256(store.read_bytes()).hexdigest() == before
    assert census.main(["--db", str(store), "--limit", "0"]) == 2


def _mini_store(
    path: Path,
    events: list[tuple[str, list[tuple[str, str]]]],
    dispositions: list[tuple[str, str]],
) -> Path:
    """events: [(task_id, [(finding id, text)])]; dispositions: [(finding id, value)]."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, event TEXT, "
            "runner TEXT, payload_json TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE review_orchestrator_finding_dispositions ("
            "target_task_id TEXT, request_id TEXT, reviewer_request_id TEXT, finding_id TEXT, "
            "disposition TEXT, actor TEXT, decided_at TEXT, receipt_json TEXT)"
        )
        for task_id, findings in events:
            payload = {"report": {"findings": [{"id": f, "summary": t} for f, t in findings]}}
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, "reviewer_receipt", "r", json.dumps(payload), "2026-01-01T00:00:00Z"),
            )
        for finding_id, disposition in dispositions:
            conn.execute(
                "INSERT INTO review_orchestrator_finding_dispositions VALUES (?,?,?,?,?,?,?,?)",
                ("T-1", "req", "rev", finding_id, disposition, "mgr", "2026-01-01", "{}"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _items(store: Path) -> list:
    conn = census.open_store(store)
    try:
        return census.collect_items(conn)[0]
    finally:
        conn.close()


def test_two_texts_under_one_bare_id_are_two_items(tmp_path: Path) -> None:
    store = _mini_store(
        tmp_path / "a.sqlite",
        [
            ("T-1", [("F1", "No test covers the new branch.")]),
            ("T-2", [("F1", "File handle is never closed, leaking a descriptor.")]),
        ],
        [],
    )
    items = _items(store)
    assert len(items) == 2
    assert {item.item_id for item in items} == {"F1"}


def test_same_finding_text_on_two_cards_is_one_item(tmp_path: Path) -> None:
    store = _mini_store(
        tmp_path / "b.sqlite",
        [
            ("reviewer-card", [("reviewer:correctness:F1", "No test covers the new branch.")]),
            ("target-card", [("reviewer:correctness:F1", "No test covers the new branch.")]),
        ],
        [],
    )
    assert len(_items(store)) == 1


def test_prefixed_ids_keep_their_own_disposition_per_lens(tmp_path: Path) -> None:
    store = _mini_store(
        tmp_path / "c.sqlite",
        [
            (
                "T-1",
                [
                    ("reviewer:correctness:F1", "No test covers the new branch."),
                    ("reviewer:security:F1", "A path traversal via ../ escapes the worktree."),
                ],
            )
        ],
        [("reviewer:correctness:F1", "confirmed"), ("reviewer:security:F1", "dismissed")],
    )
    confirmed = {item.item_id: item.confirmed for item in _items(store)}
    assert confirmed == {"reviewer:correctness:F1": True, "reviewer:security:F1": False}


def test_prefixed_id_never_inherits_another_lens_disposition(tmp_path: Path) -> None:
    store = _mini_store(
        tmp_path / "d.sqlite",
        [("T-1", [("reviewer:correctness:F1", "No test covers the new branch.")])],
        [("reviewer:security:F1", "confirmed")],
    )
    assert [item.confirmed for item in _items(store)] == [False]


def test_bare_id_finding_resolves_a_prefixed_disposition(tmp_path: Path) -> None:
    store = _mini_store(
        tmp_path / "e.sqlite",
        [("T-1", [("F4", "A path traversal via ../ escapes the worktree.")])],
        [("reviewer:security:F4", "confirmed")],
    )
    assert [item.confirmed for item in _items(store)] == [True]


def test_fixed_by_correction_is_confirmed_but_accepted_nonblocking_is_not(tmp_path: Path) -> None:
    store = _mini_store(
        tmp_path / "f.sqlite",
        [
            (
                "T-1",
                [
                    ("reviewer:correctness:F1", "No test covers the new branch."),
                    ("reviewer:correctness:F2", "File handle is never closed, leaking a descriptor."),
                    ("reviewer:correctness:F3", "Race between check and write; two threads."),
                ],
            )
        ],
        [
            ("reviewer:correctness:F1", "fixed_by_correction"),
            ("reviewer:correctness:F2", "accepted_nonblocking"),
            ("reviewer:correctness:F3", "dismissed"),
        ],
    )
    confirmed = {item.item_id: item.confirmed for item in _items(store)}
    assert confirmed == {
        "reviewer:correctness:F1": True,
        "reviewer:correctness:F2": False,
        "reviewer:correctness:F3": False,
    }
    conn = census.open_store(store)
    try:
        basis = census.build_report(conn)["confirmed_basis"]
    finally:
        conn.close()
    assert "confirmed" in basis and "fixed_by_correction" in basis


def test_disposition_only_rows_dedupe_on_id_and_text(tmp_path: Path) -> None:
    store = _mini_store(tmp_path / "g.sqlite", [], [])
    conn = sqlite3.connect(store)
    try:
        receipt = json.dumps({"finding": {"summary": "Docstring drift: README out of date"}})
        for reviewer in ("rev-1", "rev-2"):
            conn.execute(
                "INSERT INTO review_orchestrator_finding_dispositions VALUES (?,?,?,?,?,?,?,?)",
                ("T-1", "req", reviewer, "reviewer:docs:F10", "confirmed", "mgr", "2026-01-01", receipt),
            )
        conn.commit()
    finally:
        conn.close()
    assert len(_items(store)) == 1
