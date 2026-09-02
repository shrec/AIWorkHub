"""Language-applicability and false-clean regression tests for the Source
Graph risk analytics in :mod:`aiworkhub.source_graph_analytics`.

Nine confirmed defects made the risk detectors report ``available, no
findings`` on inputs they could not analyse, so a clean scan meant nothing.
Each test below states the behaviour *before* and *after* the fix so the
report can be read directly against the acceptance contract:

* Before: the C/C++ lexical detectors (``leaks``, ``rawptrs``, ``casts``,
  ``looprisks``, ``nullrisks``) returned ``status="available"`` with an empty
  findings list on Python -- indistinguishable from "analysed and clean".
  After: they return ``status="not_applicable"`` with a reason, and Python
  symbols are not counted as scanned.
* Before: ``_DIVISION_RE`` matched every path separator, so a path inside a
  string produced ``unchecked_divisor`` findings. After: string/comment
  content is masked and a left operand is required.
* Before: a Python truthiness guard (``if divisor:``) was never recognised.
  After: the colon guard is recognised by both ``crashes`` and ``nullrisks``.
* Before: ``duplicates`` normalisation used ``re.S`` so a line comment ate the
  rest of the body and two different functions collapsed. After: a line
  comment reaches only the end of its line.
* Before: ``gaps`` was saturated by low-confidence builtins and scoped after
  the SQL ``LIMIT``. After: scope and the builtin exclusion are pushed into
  the SQL.
* Before: ``deadmethods`` called a symbol dead on resolved edges alone.
  After: it states plainly that incoming-edge evidence excludes MCP/CLI
  dispatch.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import subprocess
import sys
from pathlib import Path

import aiworkhub
from aiworkhub import source_graph as sg
from aiworkhub import source_graph_analytics as analytics
from aiworkhub.repository_state import bootstrap_repository

# The determinism subprocess below has no worktree path on its ``sys.path``, so
# left to itself it imports ``aiworkhub`` from wherever it is installed -- the
# canonical tree, not the candidate under test.  Derive the parent's own import
# root from ``aiworkhub.__file__`` (the ``src`` dir holding the package) so the
# child can inherit it via ``PYTHONPATH`` and the same derivation stays correct
# in the worktree, in canonical, and in CI.
_AIWORKHUB_PKG = Path(aiworkhub.__file__).resolve().parent
_AIWORKHUB_IMPORT_ROOT = _AIWORKHUB_PKG.parent


# ``leaks`` and ``nullrisks`` gained Python rules, so they are no longer
# C-only: on a Python scope they now analyse rather than decline. The three
# below stay C-only because Python has no raw pointers or casts, and looprisks
# has no Python rule worth trusting yet. Every one of the five still analyses C,
# which is what ``C_DETECTOR_MODES`` covers.
C_ONLY_MODES = ("rawptrs", "casts", "looprisks")
C_AND_PYTHON_MODES = ("leaks", "nullrisks")
C_DETECTOR_MODES = C_ONLY_MODES + C_AND_PYTHON_MODES

_EXPECTED_C_REASON = {
    "leaks": "allocation_release_imbalance",
    "rawptrs": "raw_pointer_declaration",
    "casts": "unsafe_cast_candidate",
    "looprisks": "unbounded_loop_without_lexical_exit",
    "nullrisks": "unguarded_pointer_dereference",
}

_PYTHON_CORPUS = (
    "def routing_service():\n"
    '    route = "src/aiworkhub/service"\n'
    '    states = "READY/predecessor/KeyboardInterrupt"\n'
    "    return route\n\n"
    "def guarded_divide(total, divisor):\n"
    "    if divisor:\n"
    "        return total / divisor\n"
    "    return 0\n\n"
    "def unguarded_divide(total, divisor):\n"
    "    return total / divisor\n\n"
    "def floor_divide(total, divisor):\n"
    "    return total // divisor\n"
)

_C_CORPUS = (
    "int cprobe_leaky(void) { int *p = (int*)malloc(4); return *p; }\n"
    "void cprobe_loopy(void) { while (true) { tick(); } }\n"
    "int cprobe_deref(Item *item) { return item->value; }\n"
    "int cprobe_crashy(int divisor) { return 8 / divisor; }\n"
)


def _new_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _indexed(tmp_path: Path, relative: str, text: str) -> tuple[Path, sqlite3.Connection]:
    repo = _new_repo(tmp_path)
    _write(repo, relative, text)
    sg.build_index(repo, incremental=True)
    return repo, sg.connect(sg.resolve_db_path(repo), read_only=True)


def _entities(conn: sqlite3.Connection, *file_paths: str) -> list[dict]:
    rows: list[dict] = []
    for file_path in file_paths:
        rows += [
            dict(row)
            for row in conn.execute(
                "SELECT file_path, kind, name, qualname, line_start, line_end, "
                "signature, evidence_label, confidence FROM entities WHERE "
                "file_path=? AND kind IN ('function','method','class','struct') "
                "ORDER BY line_start",
                (file_path,),
            )
        ]
    return rows


def _analysis(conn, repo, mode, matches, query_text="probe", budget=20) -> dict:
    payload = analytics.query(
        conn, repo, mode=mode, query_text=query_text, matches=matches, budget=budget
    )
    return payload["analysis"]


# ---------------------------------------------------------------------------
# Defect 1: language applicability -- not_applicable on Python, available on C
# ---------------------------------------------------------------------------

def test_c_only_detectors_report_not_applicable_on_python(tmp_path: Path) -> None:
    repo, conn = _indexed(tmp_path, "pkg/service.py", _PYTHON_CORPUS)
    try:
        rows = _entities(conn, "pkg/service.py")
        assert rows, "python corpus must index at least one function"
        for mode in C_ONLY_MODES:
            analysis = _analysis(conn, repo, mode, rows)
            # Before: status was "available" with findings [] (false-clean).
            assert analysis["status"] == "not_applicable", mode
            assert isinstance(analysis["reason"], str) and analysis["reason"], mode
            # A Python file is never counted as checked by a C-only detector.
            assert analysis["symbols_scanned"] == 0, mode
            assert analysis["findings"] == [], mode
            assert analysis["applicable_languages"] == ["c_family"], mode
            assert analysis["symbols_skipped_by_language"] == {"python": len(rows)}, mode
    finally:
        conn.close()


def test_python_aware_detectors_analyse_python_rather_than_decline(
    tmp_path: Path,
) -> None:
    """leaks and nullrisks gained Python rules, so they must not decline here.

    They previously returned not_applicable on a Python scope, which left ~97%
    of this repository unscanned by half the bug-detection surface. Declining is
    honest but useless; this pins that they now analyse.
    """

    repo, conn = _indexed(tmp_path, "pkg/service.py", _PYTHON_CORPUS)
    try:
        rows = _entities(conn, "pkg/service.py")
        assert rows, "python corpus must index at least one function"
        for mode in C_AND_PYTHON_MODES:
            analysis = _analysis(conn, repo, mode, rows)
            assert analysis["status"] == "available", mode
            assert "python" in analysis["applicable_languages"], mode
            assert analysis["symbols_scanned"] >= 1, mode
    finally:
        conn.close()


def test_c_only_detectors_are_available_and_fire_on_c(tmp_path: Path) -> None:
    repo, conn = _indexed(tmp_path, "native/engine.cpp", _C_CORPUS)
    try:
        rows = _entities(conn, "native/engine.cpp")
        assert rows, "C corpus must index at least one function"
        for mode in C_DETECTOR_MODES:
            analysis = _analysis(conn, repo, mode, rows)
            # After: the detector can see this language, so it truly reports.
            assert analysis["status"] == "available", mode
            assert analysis["symbols_scanned"] >= 1, mode
            reasons = [r for finding in analysis["findings"] for r in finding["reasons"]]
            assert any(
                reason.startswith(_EXPECTED_C_REASON[mode]) for reason in reasons
            ), (mode, reasons)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Defect 2: division detector no longer matches a path inside a string/comment
# ---------------------------------------------------------------------------

def test_crashes_ignores_path_separators_inside_string_literals(tmp_path: Path) -> None:
    repo, conn = _indexed(tmp_path, "pkg/service.py", _PYTHON_CORPUS)
    try:
        rows = _entities(conn, "pkg/service.py")
        service = [r for r in rows if r["name"] == "routing_service"]
        analysis = _analysis(conn, repo, "crashes", service)
        reasons = [r for finding in analysis["findings"] for r in finding["reasons"]]
        # Before: "src/aiworkhub/service" produced unchecked_divisor:service,
        # :READY, :predecessor, :KeyboardInterrupt. After: the string is masked.
        for ghost in ("service", "READY", "predecessor", "KeyboardInterrupt"):
            assert f"unchecked_divisor:{ghost}" not in reasons, ghost
        assert analysis["findings"] == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Defect 3: a Python colon guard is recognised by nullrisks and crashes
# ---------------------------------------------------------------------------

def test_python_colon_guard_on_divisor_produces_no_crash_finding(tmp_path: Path) -> None:
    repo, conn = _indexed(tmp_path, "pkg/service.py", _PYTHON_CORPUS)
    try:
        rows = _entities(conn, "pkg/service.py")
        guarded = [r for r in rows if r["name"] == "guarded_divide"]
        analysis = _analysis(conn, repo, "crashes", guarded)
        # ``if divisor:`` immediately precedes ``total / divisor``.
        assert analysis["status"] == "available"
        assert analysis["findings"] == []
    finally:
        conn.close()


def test_guard_helper_recognises_both_colon_and_parenthesised_forms() -> None:
    # nullrisks and crashes share this helper; both must see either form.
    assert analytics._is_guarded("if divisor:\n    return total / divisor", "divisor")
    assert analytics._is_guarded("while count > 0:\n    total / count", "count")
    assert analytics._is_guarded("if (ptr) { return ptr->value; }", "ptr")
    assert analytics._is_guarded("assert divisor\nreturn total / divisor", "divisor")
    assert not analytics._is_guarded("return total / divisor", "divisor")


def test_crashes_detects_unguarded_and_floor_division_on_python(tmp_path: Path) -> None:
    repo, conn = _indexed(tmp_path, "pkg/service.py", _PYTHON_CORPUS)
    try:
        rows = _entities(conn, "pkg/service.py")
        for name in ("unguarded_divide", "floor_divide"):
            symbol = [r for r in rows if r["name"] == name]
            analysis = _analysis(conn, repo, "crashes", symbol)
            reasons = [r for finding in analysis["findings"] for r in finding["reasons"]]
            # crashes is applicable to Python and ``//`` floor division counts.
            assert "unchecked_divisor:divisor" in reasons, name
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rework defect 2: the reported divisor/dereference name is deterministic
# ---------------------------------------------------------------------------

_DETERMINISM_SCRIPT = (
    "import sqlite3, sys\n"
    "from pathlib import Path\n"
    "import aiworkhub\n"
    "from aiworkhub import source_graph_analytics as analytics\n"
    "rows = [{'file_path': 'pkg/mod.py', 'kind': 'function',\n"
    "         'name': 'many_divisors', 'qualname': 'pkg/mod.py::many_divisors',\n"
    "         'line_start': 1, 'line_end': 3, 'signature': '', 'confidence': 1.0}]\n"
    "analysis = analytics._risk_views(sqlite3.connect(':memory:'), Path(sys.argv[1]),\n"
    "                                 'crashes', rows, budget=20)\n"
    "reasons = [r for f in analysis['findings'] for r in f['reasons']]\n"
    # The child prints which aiworkhub it loaded first, then the reported name,
    # so the parent can prove it measured the candidate tree, not a stray one.
    "print(aiworkhub.__file__)\n"
    "print(next(r for r in reasons if r.startswith('unchecked_divisor:')))\n"
)


def test_crashes_reported_name_is_stable_across_hash_seeds(tmp_path: Path) -> None:
    # Before: ``_risk_views`` iterated ``set(_DIVISION_RE.findall(...))`` whose
    # order follows PYTHONHASHSEED, so a symbol with several unguarded divisors
    # reported ``unchecked_divisor:<x>`` in one run and ``:<y>`` in the next.
    # After: iteration is sorted, so the reported name is identical regardless
    # of hash seed -- proven here by two subprocesses with different seeds.
    #
    # For three rounds this test was mute: its subprocess imported aiworkhub
    # from the canonical tree (no worktree on ``sys.path``) and measured code
    # the candidate never touched.  The harness now hands the child the parent's
    # own import root via ``PYTHONPATH`` and asserts the child loaded aiworkhub
    # under the same root, so it can never again grade a different tree.
    repo = _new_repo(tmp_path)
    _write(
        repo,
        "pkg/mod.py",
        "def many_divisors(alpha, beta, gamma, delta, epsilon):\n"
        "    quotient = alpha / delta + beta / gamma + delta / epsilon + gamma / beta\n"
        "    return quotient\n",
    )

    def run(seed: str) -> str:
        env = dict(os.environ, PYTHONHASHSEED=seed)
        # Inherit the parent's own aiworkhub import root so the child measures
        # this candidate, not whatever aiworkhub happens to be installed.
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(_AIWORKHUB_IMPORT_ROOT) + (
            os.pathsep + existing if existing else ""
        )
        result = subprocess.run(
            [sys.executable, "-c", _DETERMINISM_SCRIPT, str(repo)],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        child_file, reason = result.stdout.splitlines()
        # A determinism test that silently measured a different aiworkhub than
        # the parent proves nothing about this candidate: require the child to
        # have resolved aiworkhub under the same root the parent imported from.
        assert Path(child_file).resolve().parent == _AIWORKHUB_PKG, child_file
        return reason

    first = run("0")
    second = run("1")
    # ``beta`` is the lexicographically smallest of {beta, delta, epsilon, gamma}.
    assert first == second == "unchecked_divisor:beta"


# ---------------------------------------------------------------------------
# Defect 4: duplicates no longer collapse two functions differing after a
# line comment
# ---------------------------------------------------------------------------

def _duplicate_corpus(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    repo = _new_repo(tmp_path)
    signature = (
        "def compute(one, two, three, four, five, six, seven, eight, "
        "multiplier, offset, baseline, adjustment):\n"
    )
    shared = (
        "    accumulator = one + two + three + four + five + six + seven + eight\n"
        "    weighted = accumulator * multiplier + offset - baseline + adjustment\n"
        "    scaled = weighted  # divergence marker comment here\n"
    )
    _write(repo, "a/mod.py", signature + shared + "    return scaled + 1\n")
    _write(repo, "b/mod.py", signature + shared + "    return scaled + 999\n")
    twin_sig = (
        "def twin(alpha, beta, gamma, delta, epsilon, zeta, eta, theta, iota, kappa):\n"
    )
    twin_body = (
        "    aggregate = alpha + beta + gamma + delta + epsilon + zeta + eta "
        "+ theta + iota + kappa\n"
        "    return aggregate * 3\n"
    )
    _write(repo, "a/same.py", twin_sig + twin_body)
    _write(repo, "b/same.py", twin_sig + twin_body)
    sg.build_index(repo, incremental=True)
    return repo, sg.connect(sg.resolve_db_path(repo), read_only=True)


def test_duplicates_do_not_collapse_functions_differing_after_a_comment(
    tmp_path: Path,
) -> None:
    repo, conn = _duplicate_corpus(tmp_path)
    try:
        # Two same-named ``compute`` functions differ only in the line after a
        # ``# ...`` comment. Before: re.S let the comment eat the tail and the
        # two collapsed to one digest. After: they stay distinct.
        differ = _entities(conn, "a/mod.py", "b/mod.py")
        analysis = _analysis(conn, repo, "duplicates", differ)
        assert analysis["status"] == "available"
        assert analysis["findings"] == []

        # Positive control: genuinely identical functions are still reported.
        identical = _entities(conn, "a/same.py", "b/same.py")
        analysis = _analysis(conn, repo, "duplicates", identical)
        assert len(analysis["findings"]) == 1
        symbols = {s["qualname"] for s in analysis["findings"][0]["symbols"]}
        assert symbols == {"a/same.py.twin", "b/same.py.twin"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Defect 5: gaps applies scope + excludes builtins so real rows survive
# ---------------------------------------------------------------------------

def _gaps_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE entities(file_path TEXT, kind TEXT, name TEXT, qualname TEXT, "
        "line_start INTEGER, line_end INTEGER, signature TEXT, evidence_label TEXT, "
        "confidence REAL)"
    )
    conn.execute(
        "CREATE TABLE files(file_path TEXT, language TEXT, status TEXT, "
        "source_hash TEXT, indexed_at TEXT, build_revision TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges(kind TEXT, src_qualname TEXT, dst_name TEXT, "
        "dst_qualname TEXT, file_path TEXT, line INTEGER, evidence_label TEXT, "
        "confidence REAL)"
    )
    conn.execute("INSERT INTO files VALUES('pkg/in_scope.py','python','ok','h','t','r')")
    conn.execute(
        "INSERT INTO files VALUES('tests/test_in_scope.py','python','ok','h','t','r')"
    )
    # Low-confidence builtins saturate the in-scope set (confidence 0.4).
    for index, builtin in enumerate(("len", "print", "range", "int", "str")):
        conn.execute(
            "INSERT INTO edges VALUES('calls','pkg/in_scope.py::caller',?,NULL,"
            "'pkg/in_scope.py',?,'AMBIGUOUS',0.4)",
            (builtin, 10 + index),
        )
    # The real unresolved call the caller actually cares about (confidence 0.6).
    conn.execute(
        "INSERT INTO edges VALUES('calls','pkg/in_scope.py::caller',"
        "'real_missing_target',NULL,'pkg/in_scope.py',30,'AMBIGUOUS',0.6)"
    )
    # A real low-confidence edge owned by an out-of-scope file.
    conn.execute(
        "INSERT INTO edges VALUES('calls','pkg/other.py::x','other_missing',NULL,"
        "'pkg/other.py',5,'AMBIGUOUS',0.5)"
    )
    return conn


def test_gaps_scopes_excludes_builtins_and_returns_real_rows(tmp_path: Path) -> None:
    conn = _gaps_conn()
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "in_scope.py").write_text("def caller():\n    return 1\n", encoding="utf-8")
    matches = [{
        "file_path": "pkg/in_scope.py", "kind": "function", "name": "caller",
        "qualname": "pkg/in_scope.py::caller", "line_start": 1, "line_end": 40,
        "signature": "", "evidence_label": "EXTRACTED", "confidence": 1.0,
    }]
    # Budget deliberately smaller than the builtin count: before the fix the
    # 0.4 builtins won the LIMIT and the real 0.6 row never surfaced.
    payload = analytics.query(
        conn, repo, mode="gaps", query_text="caller", matches=matches, budget=3
    )
    names = [row["dst_name"] for row in payload["low_confidence_edges"]]
    assert "real_missing_target" in names
    assert not ({"len", "print", "range", "int", "str"} & set(names))
    assert "other_missing" not in names  # out-of-scope file is excluded


# ---------------------------------------------------------------------------
# Defect 9: deadmethods states its incoming-edge evidence is incomplete
# ---------------------------------------------------------------------------

def test_deadmethods_states_incoming_edge_evidence_is_incomplete(tmp_path: Path) -> None:
    corpus = (
        "def reached_target():\n"
        "    return 1\n\n"
        "def caller():\n"
        "    return reached_target()\n\n"
        "def orphan_symbol():\n"
        "    return 2\n"
    )
    repo, conn = _indexed(tmp_path, "pkg/mod.py", corpus)
    try:
        rows = _entities(conn, "pkg/mod.py")
        analysis = _analysis(conn, repo, "deadmethods", rows)
        # The caveat is stated plainly and names the unobserved dispatch paths.
        evidence = analysis["incoming_edge_evidence"]
        assert "dispatch" in evidence and "mcp" in evidence
        flagged = {
            finding["qualname"].split(".")[-1]: finding["reasons"]
            for finding in analysis["findings"]
        }
        # A symbol reached by a resolved call is not called dead.
        assert "reached_target" not in flagged
        # An unreferenced symbol is flagged, but with the dispatch caveat.
        assert "orphan_symbol" in flagged
        assert flagged["orphan_symbol"] == [
            "no_resolved_incoming_calls_dynamic_dispatch_unobserved"
        ]
    finally:
        conn.close()


def test_gaps_excludes_every_language_builtin_not_just_a_curated_list() -> None:
    """The exclusion set is derived from the interpreter, not maintained by hand.

    Measured on the canonical index before this change, `gaps` over
    src/aiworkhub returned 20 rows of which 20 named builtins -- AttributeError,
    SystemExit, ValueError -- none of which the curated 53-name list contained.
    Builtins carry the extractor's lowest confidence (0.4) and the query orders
    by confidence ascending, so they won the LIMIT and crowded out every real
    row. A list that has to be extended by hand will always fall behind the
    interpreter; dir(builtins) cannot.
    """
    import builtins

    from aiworkhub.source_graph_analytics import (
        _GAPS_BUILTIN_CALLEES,
        _GAPS_EXCLUDED_CALLEES,
    )

    for missing in ("ValueError", "AttributeError", "SystemExit", "RuntimeError"):
        assert missing not in _GAPS_BUILTIN_CALLEES, "fixture assumption changed"
        assert missing in _GAPS_EXCLUDED_CALLEES, f"{missing} must be excluded"

    assert frozenset(dir(builtins)) <= _GAPS_EXCLUDED_CALLEES
    # The C-shaped curated names are not builtins and must survive the union.
    for c_name in ("memcpy", "memset", "printf", "sizeof"):
        assert c_name in _GAPS_EXCLUDED_CALLEES


def test_gaps_still_reports_a_dangling_call_to_a_name_nothing_defines() -> None:
    """A call to something the repository never defines IS the canonical gap.

    Excluding every unresolved-and-undefined name would have been an
    overcorrection: it makes the mode precise and useless at the same time,
    because a dangling reference is exactly the evidence a caller wants.
    """
    conn = _gaps_conn()
    repo = Path(tempfile.mkdtemp()) / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "in_scope.py").write_text(
        "def caller():\n    return 1\n", encoding="utf-8"
    )
    matches = [{
        "file_path": "pkg/in_scope.py", "kind": "function", "name": "caller",
        "qualname": "pkg/in_scope.py::caller", "line_start": 1, "line_end": 40,
        "signature": "", "evidence_label": "EXTRACTED", "confidence": 1.0,
    }]
    payload = analytics.query(
        conn, repo, mode="gaps", query_text="caller", matches=matches, budget=3
    )
    names = [row["dst_name"] for row in payload["low_confidence_edges"]]
    assert "real_missing_target" in names
