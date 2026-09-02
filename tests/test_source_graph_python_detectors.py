"""Reproduction + guard tests for the Python ``leaks``/``nullrisks`` detectors.

Before this change both modes returned ``not_applicable`` for a Python-only
scope (the detectors "analysed only the C family"), so ~97% of this repo's code
was never scanned.  These tests pin the reproduction (each pattern must now
yield at least one finding), the truthful ``applicable_languages``/evidence
labelling, the explicit out-of-scope stance for ``rawptrs``/``casts``/
``looprisks``, and that existing C-family behaviour is unchanged.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from aiworkhub import source_graph_analytics
from aiworkhub.source_graph_analytics import _risk_views


@pytest.fixture()
def conn() -> sqlite3.Connection:
    # ``_risk_views`` only touches the DB for ``deadmethods``; the lexical
    # risk modes exercised here read symbol bodies from disk, so a bare
    # in-memory connection is enough.
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


def _scan(
    conn: sqlite3.Connection,
    repo_root: Path,
    mode: str,
    rel: str,
    name: str,
    source: str,
) -> dict:
    """Write ``source`` to ``repo_root/rel`` and run one risk mode over it."""

    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = textwrap.dedent(source).strip("\n") + "\n"
    path.write_text(text, encoding="utf-8")
    row = {
        "file_path": rel,
        "kind": "function",
        "name": name,
        "qualname": f"{rel}::{name}",
        "line_start": 1,
        "line_end": len(text.splitlines()),
    }
    return _risk_views(conn, repo_root, mode, [row], budget=50)


# --------------------------------------------------------------------------- #
# leaks (Python)
# --------------------------------------------------------------------------- #

def test_python_sqlite_with_leak_yields_leaks_finding(conn, tmp_path):
    result = _scan(
        conn,
        tmp_path,
        "leaks",
        "src/pkg/store.py",
        "load",
        """
        import sqlite3

        def load(db_path):
            with sqlite3.connect(db_path) as c:
                c.execute("PRAGMA journal_mode=WAL")
            return True
        """,
    )
    assert result["status"] == "available"
    assert "python" in result["applicable_languages"]
    assert result["findings"], "sqlite3.connect with-leak must produce a finding"
    reasons = result["findings"][0]["reasons"]
    assert any("sqlite3_connect_context_manager_leaks_connection" == r for r in reasons)
    assert (
        result["findings"][0]["evidence_class"]
        == "bounded_lexical_candidate_not_proven_defect"
    )


def test_python_unclosed_open_handle_yields_leaks_finding(conn, tmp_path):
    result = _scan(
        conn,
        tmp_path,
        "leaks",
        "src/pkg/reader.py",
        "read_config",
        """
        def read_config(path):
            f = open(path)
            data = f.read()
            return data
        """,
    )
    assert result["status"] == "available"
    assert result["findings"]
    assert any(
        r.startswith("resource_not_closed_on_all_paths:f")
        for r in result["findings"][0]["reasons"]
    )


def test_python_closed_handle_and_with_block_are_clean(conn, tmp_path):
    result = _scan(
        conn,
        tmp_path,
        "leaks",
        "src/pkg/ok.py",
        "read_ok",
        """
        def read_ok(path):
            with open(path) as f:
                return f.read()
        """,
    )
    assert result["status"] == "available"
    assert result["findings"] == []


# --------------------------------------------------------------------------- #
# nullrisks (Python)
# --------------------------------------------------------------------------- #

def test_python_unchecked_possibly_none_use_yields_nullrisks_finding(conn, tmp_path):
    result = _scan(
        conn,
        tmp_path,
        "nullrisks",
        "src/pkg/lookup.py",
        "lookup",
        """
        def lookup(config, key):
            entry = config.get(key)
            return entry.value
        """,
    )
    assert result["status"] == "available"
    assert "python" in result["applicable_languages"]
    assert result["findings"]
    assert any(
        r.startswith("unchecked_possibly_none_use:entry")
        for r in result["findings"][0]["reasons"]
    )
    assert (
        result["findings"][0]["evidence_class"]
        == "bounded_lexical_candidate_not_proven_defect"
    )


def test_python_or_zero_coercion_yields_nullrisks_finding(conn, tmp_path):
    result = _scan(
        conn,
        tmp_path,
        "nullrisks",
        "src/pkg/quota.py",
        "remaining",
        """
        def remaining(config):
            limit = config.get("limit") or 0
            return limit
        """,
    )
    assert result["status"] == "available"
    assert result["findings"]
    assert any(
        "none_coalescing_masks_absence" in r
        for r in result["findings"][0]["reasons"]
    )


def test_python_guarded_optional_is_clean(conn, tmp_path):
    result = _scan(
        conn,
        tmp_path,
        "nullrisks",
        "src/pkg/guarded.py",
        "safe_lookup",
        """
        def safe_lookup(config, key):
            entry = config.get(key)
            if entry:
                return entry.value
            return None
        """,
    )
    assert result["status"] == "available"
    assert result["findings"] == []


# --------------------------------------------------------------------------- #
# A Python file with neither pattern produces no findings
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["leaks", "nullrisks"])
def test_clean_python_file_yields_nothing(conn, tmp_path, mode):
    result = _scan(
        conn,
        tmp_path,
        mode,
        "src/pkg/math_ok.py",
        "add",
        """
        def add(a, b):
            total = a + b
            return total
        """,
    )
    assert result["status"] == "available"
    assert "python" in result["applicable_languages"]
    assert result["findings"] == []


# --------------------------------------------------------------------------- #
# rawptrs / casts / looprisks stay explicitly out of scope for Python
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["rawptrs", "casts", "looprisks"])
def test_out_of_scope_modes_stay_not_applicable_on_python(conn, tmp_path, mode):
    result = _scan(
        conn,
        tmp_path,
        mode,
        "src/pkg/loopy.py",
        "spin",
        """
        def spin(items):
            while True:
                items.append(1)
        """,
    )
    assert result["status"] == "not_applicable"
    assert result["applicable_languages"] == ["c_family"]
    assert result["reason"] and "only_c_family" in result["reason"]
    assert result["symbols_skipped_by_language"] == {"python": 1}
    assert result["findings"] == []


# --------------------------------------------------------------------------- #
# Existing C-family behaviour is unchanged
# --------------------------------------------------------------------------- #

def test_c_family_leaks_still_fire(conn, tmp_path):
    result = _scan(
        conn,
        tmp_path,
        "leaks",
        "src/pkg/buf.c",
        "make_buffer",
        """
        void make_buffer(void) {
            int* p = malloc(64);
        }
        """,
    )
    assert result["status"] == "available"
    assert "c_family" in result["applicable_languages"]
    assert any(
        "allocation_release_imbalance" in f["reasons"] for f in result["findings"]
    )


def test_c_family_nullrisks_still_fire(conn, tmp_path):
    result = _scan(
        conn,
        tmp_path,
        "nullrisks",
        "src/pkg/node.c",
        "value_of",
        """
        int value_of(Node* n) {
            return n->value;
        }
        """,
    )
    assert result["status"] == "available"
    assert any(
        r.startswith("unguarded_pointer_dereference:n")
        for f in result["findings"]
        for r in f["reasons"]
    )


# --- driven by the two REAL shapes, not only synthetic fixtures -------------
#
# The first version of this detector matched only a literal ``sqlite3.connect``
# call. It passed every synthetic fixture and still saw 2 of the 9 real leak
# sites in this repository, because seven of them spell the factory as a local
# helper. It also reported ``with closing(sqlite3.connect(p))`` -- the correct
# pattern, and the fix that landed for those nine sites -- as a leak. These
# cases pin both directions against the real source.


_LEAKY_LITERAL = """import sqlite3


def _repair(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")
"""

_LEAKY_VIA_LOCAL_HELPER = """import sqlite3
from pathlib import Path


def _connect(path, *, readonly=False):
    conn = sqlite3.connect(path)
    return conn


def quick_check(path):
    with _connect(path, readonly=True) as conn:
        return conn.execute("PRAGMA quick_check").fetchone()
"""

_CLOSED_LITERAL = """import sqlite3
from contextlib import closing


def _repair(db_path):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")
"""

_CLOSED_VIA_LOCAL_HELPER = """import sqlite3
from contextlib import closing


def _connect(path, *, readonly=False):
    return sqlite3.connect(path)


def quick_check(path):
    with closing(_connect(path, readonly=True)) as conn:
        return conn.execute("PRAGMA quick_check").fetchone()
"""


def test_literal_sqlite_connect_with_block_is_flagged():
    assert source_graph_analytics._python_leak_reasons(_LEAKY_LITERAL) == [
        "sqlite3_connect_context_manager_leaks_connection"
    ]


def test_local_factory_helper_with_block_is_flagged():
    """Seven of the nine real sites read `with _connect(...) as conn:`."""
    assert source_graph_analytics._python_leak_reasons(_LEAKY_VIA_LOCAL_HELPER) == [
        "sqlite3_connect_context_manager_leaks_connection"
    ]


def test_closing_wrapped_literal_is_not_a_leak():
    """The correct pattern must not be reported, or the detector becomes noise."""
    assert source_graph_analytics._python_leak_reasons(_CLOSED_LITERAL) == []


def test_closing_wrapped_local_factory_is_not_a_leak():
    assert source_graph_analytics._python_leak_reasons(_CLOSED_VIA_LOCAL_HELPER) == []


def test_factory_resolution_finds_the_helper_name():
    names = source_graph_analytics._python_sqlite_factory_names(_LEAKY_VIA_LOCAL_HELPER)
    assert "_connect" in names
    assert "quick_check" not in names


def test_strip_closing_wrappers_removes_only_the_balanced_region():
    stripped = source_graph_analytics._strip_closing_wrappers(
        "closing(sqlite3.connect(p)) as c, other(sqlite3.connect(q)) as d"
    )
    assert "other(sqlite3.connect(q))" in stripped
    assert "closing(" not in stripped


# --- precision: three shapes that are NOT leaks ----------------------------
#
# Found by pointing the detector at this repository. Each of these was reported
# as a leak by an earlier version, and each is correct code. A detector that
# cries wolf on 137 modules gets switched off, so these are pinned.

_CONTEXTMANAGER_FACTORY = """import sqlite3
from contextlib import contextmanager


class View:
    @contextmanager
    def open(self):
        conn = sqlite3.connect("file::memory:", uri=True)
        try:
            yield conn
        finally:
            conn.close()

    def find(self, term):
        with self.open() as conn:
            return conn.execute("SELECT 1").fetchone()

    def read(self, path):
        with open(path) as handle:
            return handle.read()
"""

_RETURNED_SOCKET = """import socket


def connect_sideband_socket(path, *, timeout=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if timeout is not None:
        sock.settimeout(timeout)
    return sock
"""

_UNCLOSED_SOCKET = """import socket


def ping():
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.send(b"x")
"""


def test_contextmanager_factory_is_not_a_leak():
    """A @contextmanager closes in its own finally -- the opposite of the bug.

    Counting one as a connection factory made every `with self.open() as conn`
    a reported leak, and because the method was named ``open`` it also made
    every ordinary `with open(path) as handle` one.
    """
    assert source_graph_analytics._python_leak_reasons(_CONTEXTMANAGER_FACTORY) == []


def test_method_factory_requires_an_attribute_call():
    """A method name that shadows a builtin must not match the bare builtin."""
    factories = source_graph_analytics._python_sqlite_factory_names(
        """import sqlite3


class View:
    def open(self):
        return sqlite3.connect(":memory:")
"""
    )
    assert factories == {"open": True}


def test_module_level_factory_is_matched_bare():
    factories = source_graph_analytics._python_sqlite_factory_names(
        """import sqlite3


def _connect(path):
    return sqlite3.connect(path)
"""
    )
    assert factories == {"_connect": False}


def test_returned_handle_belongs_to_the_caller():
    """A factory hands ownership back; not closing it here is correct."""
    assert source_graph_analytics._python_leak_reasons(_RETURNED_SOCKET) == []


def test_unreturned_unclosed_handle_is_still_a_leak():
    assert source_graph_analytics._python_leak_reasons(_UNCLOSED_SOCKET) == [
        "resource_not_closed_on_all_paths:sock"
    ]
