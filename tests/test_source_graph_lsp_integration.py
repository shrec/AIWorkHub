"""Task 3 integration tests: LSP batch resolution inside the Source Graph index.

Every enrichment path here drives the production transport against a real
stdio language server -- a small scripted process that speaks Content-Length
framing and answers ``textDocument/definition`` from a JSON scenario. Nothing
monkeypatches ``resolve_definitions``, so the bounded private workspace, the
workspace-relative definition URIs a real server actually returns, the single
1-based -> 0-based position conversion, and every fail-closed classification
are exercised end to end rather than asserted against a stub.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository

PYTHON_SERVER_ENV = "AIWORKHUB_LSP_PYTHON_SERVER"
TYPESCRIPT_SERVER_ENV = "AIWORKHUB_LSP_TYPESCRIPT_SERVER"
SERVER_VERSION_ENV = "AIWORKHUB_LSP_SERVER_VERSION"
SERVER_VERSION = "fake-lsp-1.0"

# A scripted stdio LSP server. It is deliberately a separate process speaking
# the real wire protocol: a stub returning repository URIs cannot show that a
# server confined to a private workspace answers with workspace URIs, and a
# stub called in-process cannot show that the subprocess runs with no write
# lease held. ``fail`` makes it crash, hang, emit a malformed frame or answer
# with a malformed payload on one exact position, which is what a real server
# does when it breaks.
FAKE_LSP_SERVER = r'''
"""Scenario-driven stdio LSP server used by the Source Graph Task 3 tests."""

import json
import os
import sys
import time


def _read_message(stream):
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        chunk = stream.read(1)
        if not chunk:
            return None
        header += chunk
    length = 0
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def _send(stream, payload):
    raw = json.dumps(payload).encode("utf-8")
    stream.write(b"Content-Length: " + str(len(raw)).encode("ascii") + b"\r\n\r\n")
    stream.write(raw)
    stream.flush()


def _wait_for(path, timeout):
    if not path:
        return
    deadline = time.monotonic() + timeout
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.01)


def _append(path, payload):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _locations(entries, root):
    found = []
    for entry in entries:
        uri = entry.get("uri") or (root + "/" + entry["workspace_path"])
        span = entry.get("range") or [0, 0, 0, 0]
        found.append({
            "uri": uri,
            "range": {
                "start": {"line": span[0], "character": span[1]},
                "end": {"line": span[2], "character": span[3]},
            },
        })
    return found


def main():
    with open(os.environ["AIWORKHUB_FAKE_LSP_SCENARIO"], encoding="utf-8") as handle:
        scenario = json.loads(handle.read())
    log_path = os.environ.get("AIWORKHUB_FAKE_LSP_LOG", "")
    ready_path = scenario.get("ready_file", "")
    release_path = scenario.get("release_file", "")
    definitions = scenario.get("definitions", {})
    failures = scenario.get("fail", {})
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    root = ""
    while True:
        message = _read_message(stdin)
        if message is None:
            return 0
        method = message.get("method")
        ident = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            root = str(params.get("rootUri") or "")
            _send(stdout, {"jsonrpc": "2.0", "id": ident, "result": {
                "capabilities": {
                    "positionEncoding": scenario.get("position_encoding", "utf-16"),
                    "definitionProvider": True,
                },
            }})
        elif method == "shutdown":
            _send(stdout, {"jsonrpc": "2.0", "id": ident, "result": None})
        elif method == "exit":
            return 0
        elif method == "textDocument/definition":
            uri = str((params.get("textDocument") or {}).get("uri") or "")
            position = params.get("position") or {}
            prefix = root + "/"
            relative = uri[len(prefix):] if root and uri.startswith(prefix) else uri
            key = "%s:%s:%s" % (
                relative, position.get("line"), position.get("character"),
            )
            line_key = "%s:%s" % (relative, position.get("line"))
            _append(log_path, {"key": key, "uri": uri, "root": root})
            if ready_path:
                with open(ready_path, "a", encoding="utf-8"):
                    pass
                _wait_for(release_path, 20.0)
            failure = failures.get(key) or failures.get(line_key)
            if failure == "crash":
                os._exit(3)
            if failure == "hang":
                time.sleep(60)
            if failure == "malformed":
                stdout.write(b"Content-Length: 7\r\n\r\nnotjson")
                stdout.flush()
                continue
            if failure == "garbage":
                # Well framed and well formed JSON-RPC, but the payload is not
                # a Location list: an answer nobody can trust, not "no answer".
                _send(stdout, {"jsonrpc": "2.0", "id": ident, "result": [{"uri": 7}]})
                continue
            # An exact position wins; a line-only entry answers wherever on
            # that line the caller asked, which is what a test needs when the
            # available extractor decides whether a column was recorded.
            entries = definitions.get(key)
            if entries is None:
                entries = scenario.get("lines", {}).get(line_key) or []
            _send(stdout, {
                "jsonrpc": "2.0",
                "id": ident,
                "result": _locations(entries, root),
            })
        elif ident is not None:
            _send(stdout, {"jsonrpc": "2.0", "id": ident, "result": None})


if __name__ == "__main__":
    sys.exit(main())
'''

PY_TARGET = "def target_symbol():\n    return 2\n"
# The caller's reference name must name the declaration the server points at,
# exactly as a real call does. ``other`` is an unrelated, unresolvable import
# that gives the fixture an unresolved first-line edge.
PY_CALLER = "from missing import other\n\ndef caller():\n    return target_symbol()\n"
CALL_NAME = "target_symbol"
IMPORT_NAME = "missing.other"
# ``a.py``'s unresolved call edge is graph line 4, byte column 11; its
# unresolved import edge is graph line 1, column 0. On the wire those become
# LSP (3, 11) and (0, 0) -- converted exactly once, in the transport.
CALL_KEY = "a.py:3:11"
IMPORT_KEY = "a.py:0:0"
# Exactly the ``target_symbol`` identifier in ``def target_symbol():``.
TARGET_SPAN = [0, 4, 0, 17]
TARGET_QUALNAME = "b.py.target_symbol"


def _await(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path}"
        time.sleep(0.01)


class _Lsp:
    """One bootstrapped repository plus the scripted server that answers it."""

    def __init__(self, tmp_path: Path, monkeypatch, name: str) -> None:
        root = tmp_path / name
        root.mkdir()
        bootstrap_repository(root, repo_name=name)
        self.root = root.resolve()
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.script = tmp_path / f"{name}_server.py"
        self.script.write_text(FAKE_LSP_SERVER, encoding="utf-8")
        self.scenario_path = tmp_path / f"{name}_scenario.json"
        self.log_path = tmp_path / f"{name}_lsp.log"
        self.scenario: dict = {"definitions": {}}
        self.publish()
        monkeypatch.setenv("AIWORKHUB_FAKE_LSP_SCENARIO", str(self.scenario_path))
        monkeypatch.setenv("AIWORKHUB_FAKE_LSP_LOG", str(self.log_path))
        monkeypatch.setenv(SERVER_VERSION_ENV, SERVER_VERSION)
        monkeypatch.delenv(PYTHON_SERVER_ENV, raising=False)
        monkeypatch.delenv(TYPESCRIPT_SERVER_ENV, raising=False)

    # --- configuration -------------------------------------------------
    def publish(self) -> None:
        self.scenario_path.write_text(json.dumps(self.scenario), encoding="utf-8")

    def serve(self, env_name: str = PYTHON_SERVER_ENV) -> None:
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(self.script))}"
        self.monkeypatch.setenv(env_name, command)

    def unserve(self, env_name: str = PYTHON_SERVER_ENV) -> None:
        self.monkeypatch.delenv(env_name, raising=False)

    def identity(self) -> str:
        """The observed identity every receipt this server earns must carry."""
        return sg._lsp_server_identity((sys.executable, str(self.script)))

    def define(self, key: str, *locations: dict) -> None:
        self.scenario["definitions"][key] = list(locations)
        self.publish()

    def define_line(self, rel: str, lsp_line: int, *locations: dict) -> None:
        """Answer anywhere on one LSP line.

        Whether an extractor records a call-site column is a property of the
        extractors installed on the host, not of this feature, so a fixture
        that only cares about the line says exactly that.
        """
        self.scenario.setdefault("lines", {})[f"{rel}:{lsp_line}"] = list(locations)
        self.publish()

    def fail(self, key: str, mode: str | None) -> None:
        failures = self.scenario.setdefault("fail", {})
        if mode is None:
            failures.pop(key, None)
        else:
            failures[key] = mode
        self.publish()

    def asked_key(self, rel: str, dst_name: str) -> str:
        """The wire position an unresolved edge must have produced."""
        edge = self.edge(rel, dst_name)
        return f"{rel}:{edge['line'] - 1}:{max(edge['source_col'], 0)}"

    def hold(self) -> tuple[Path, Path]:
        ready = self.tmp_path / "server_ready"
        release = self.tmp_path / "server_release"
        self.scenario["ready_file"] = str(ready)
        self.scenario["release_file"] = str(release)
        self.publish()
        return ready, release

    # --- repository ----------------------------------------------------
    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def hash(self, rel: str) -> str:
        return hashlib.sha256((self.root / rel).read_bytes()).hexdigest()

    def index(self, rel: str) -> dict:
        return sg.index_file(self.root, rel, self.hash(rel))

    # --- observation ---------------------------------------------------
    def asked(self) -> list[dict]:
        if not self.log_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def edges(self, rel: str) -> list[dict]:
        conn = sg.connect(sg.resolve_db_path(self.root), read_only=True)
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT line, source_col, dst_name, dst_qualname, "
                    "evidence_label, confidence FROM edges WHERE file_path=? "
                    "ORDER BY line, source_col, dst_name",
                    (rel,),
                )
            ]
        finally:
            conn.close()

    def edge(self, rel: str, dst_name: str) -> dict:
        matches = [row for row in self.edges(rel) if row["dst_name"] == dst_name]
        assert len(matches) == 1, matches
        return matches[0]

    def meta(self, key: str) -> dict:
        conn = sg.connect(sg.resolve_db_path(self.root), read_only=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        return json.loads(row["value"]) if row is not None else {}


def _python_pair(fixture: _Lsp) -> None:
    """The canonical fixture: ``a.py`` calls a symbol defined in ``b.py``."""
    fixture.write("b.py", PY_TARGET)
    fixture.write("a.py", PY_CALLER)
    fixture.define(CALL_KEY, {"workspace_path": "b.py", "range": TARGET_SPAN})
    fixture.serve()


def _assert_evidence_agrees(fixture: _Lsp, rel: str) -> None:
    """Durable LSP evidence must describe the graph ``edges`` actually holds.

    Provenance, the file receipt and ``bound_edges`` are what ``callers`` and
    ``impact`` are trusted on. Every claim one of them makes has to be a
    binding an edge row carries in the same published generation -- never one
    a later pass is merely expected to produce.
    """
    provenance = sg.lsp_provenance(fixture.root, source_path=rel)
    rows = fixture.edges(rel)
    for claim in provenance:
        assert [
            row
            for row in rows
            if row["line"] == claim["source_line"]
            and max(row["source_col"], 0) == claim["source_column"]
            and row["dst_qualname"] == claim["target_qualname"]
        ], (
            f"{rel} provenance claims {claim['target_qualname']} at line "
            f"{claim['source_line']} but no edge there carries it"
        )
    receipt = sg.lsp_receipt(fixture.root, rel)
    if receipt is None:
        assert provenance == ()
    else:
        assert receipt["edge_count"] == len(provenance)
    assert sg.lsp_health(fixture.root)["bound_edges"] >= len(provenance)


def _assert_classified(health: dict) -> None:
    """Every attempted position lands in exactly one classification."""
    assert health["attempted"] == health["classified"], health


# --- enrichment ------------------------------------------------------------


def test_workspace_definition_uri_enriches_the_canonical_target(tmp_path, monkeypatch):
    """A real server answers from the private workspace, not the repository."""
    fixture = _Lsp(tmp_path, monkeypatch, "workspace_uri")
    _python_pair(fixture)
    fixture.index("b.py")

    summary = fixture.index("a.py")

    assert summary["lsp"]["status"] == "enriched"
    assert summary["lsp"]["enriched"] == 1
    assert summary["lsp"]["language"] == summary["language"]
    asked = fixture.asked()
    assert asked, "the language server was never asked for a definition"
    # The server only ever saw the bounded private workspace, so the URIs it
    # answers with are workspace URIs, never canonical repository paths.
    workspace_root = asked[0]["root"]
    assert "/lsp/" in workspace_root
    assert all(item["uri"].startswith(workspace_root + "/") for item in asked)
    assert all(
        item["uri"] != f"{fixture.root.as_uri()}/a.py" for item in asked
    )
    # And that workspace lives in a directory the single-file path validator
    # refuses outright, which is exactly why the remap has to be explicit.
    excluded = Path(workspace_root[len("file://"):])
    assert ".aiworkhub" in excluded.parts

    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    provenance = sg.lsp_provenance(fixture.root, source_path="a.py")
    assert len(provenance) == 1
    row = provenance[0]
    assert row["target_path"] == "b.py"
    assert row["target_hash"] == fixture.hash("b.py")
    assert row["source_hash"] == fixture.hash("a.py")
    assert row["target_qualname"] == TARGET_QUALNAME
    assert row["server_version"] == f"{SERVER_VERSION}+{fixture.identity()}"
    assert row["config_digest"]
    assert row["latency_ms"] >= 0
    assert row["schema_id"] == sg.LSP_ENRICHMENT_SCHEMA_ID
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    assert receipt["complete"] == 1
    assert receipt["edge_count"] == 1


def test_definition_positions_convert_graph_lines_exactly_once(tmp_path, monkeypatch):
    """Graph line N is asked as LSP line N-1, including on the first line."""
    fixture = _Lsp(tmp_path, monkeypatch, "coordinates")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")

    assert sorted(item["key"] for item in fixture.asked()) == [IMPORT_KEY, CALL_KEY]

    call_edge = fixture.edge("a.py", CALL_NAME)
    import_edge = fixture.edge("a.py", IMPORT_NAME)
    assert call_edge["line"] == 4
    assert call_edge["source_col"] == 11
    assert call_edge["dst_qualname"] == TARGET_QUALNAME
    # The graph-line-1 edge was asked and answered with nothing, so it stays
    # exactly as lexical extraction left it.
    assert import_edge["line"] == 1
    assert import_edge["dst_qualname"] is None
    assert sg.lsp_health(fixture.root)["unresolved"] >= 1

    binding = sg.lsp_provenance(fixture.root, source_path="a.py")[0]
    assert binding["source_line"] == 4
    assert binding["source_column"] == 11
    assert binding["target_line_start"] == 1


def test_full_build_runs_batch_enrichment_after_publish(tmp_path, monkeypatch):
    """Task 3 is a build feature, not only a single-file one."""
    fixture = _Lsp(tmp_path, monkeypatch, "full_build")
    _python_pair(fixture)

    report = sg.build_index(fixture.root)

    assert Path(report.db_path).is_file()
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    assert len(sg.lsp_provenance(fixture.root)) == 1
    health = sg.lsp_health(fixture.root)
    assert health["attempted"] >= 1
    assert health["internal"] >= 1
    # ``b.py`` has no unresolved edge, so the batch skipped it by name.
    assert health["files_skipped"] >= 1
    assert health["batches"] >= 1
    assert health["green"] is True
    _assert_classified(health)


def test_typescript_and_javascript_fixtures_are_enriched(tmp_path, monkeypatch):
    fixture = _Lsp(tmp_path, monkeypatch, "ts_js")
    fixture.write("util.ts", "export function helper(): number {\n  return 1;\n}\n")
    # A second ``helper`` keeps lexical resolution honestly ambiguous, so the
    # server is the only thing that can say which declaration a call means.
    fixture.write("other.ts", "export function helper(): number {\n  return 2;\n}\n")
    fixture.write(
        "app.ts",
        'import { helper } from "./missing";\n\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    fixture.write("lone.js", "function run() {\n  return helper();\n}\n")
    # Whether the installed extractor records a column is not this feature's
    # business, so the server answers by line and the test then asserts the
    # exact wire position afterwards.
    definition = {"workspace_path": "util.ts", "range": [0, 16, 0, 22]}
    fixture.define_line("app.ts", 3, definition)
    fixture.define_line("lone.js", 1, definition)
    fixture.serve(TYPESCRIPT_SERVER_ENV)

    fixture.index("util.ts")
    fixture.index("other.ts")
    typescript = fixture.index("app.ts")
    javascript = fixture.index("lone.js")

    assert typescript["lsp"]["status"] == "enriched"
    assert typescript["lsp"]["enriched"] == 1
    assert typescript["lsp"]["language"] == "typescript"
    assert javascript["lsp"]["status"] == "enriched"
    assert javascript["lsp"]["enriched"] == 1
    assert javascript["lsp"]["language"] == "javascript"
    assert fixture.edge("app.ts", "helper")["dst_qualname"] == "util.ts::helper"
    assert fixture.edge("lone.js", "helper")["dst_qualname"] == "util.ts::helper"
    # The unrelated module import is left exactly as it was, whether or not
    # the extractor recorded a column it could be asked at.
    assert fixture.edge("app.ts", "./missing")["dst_qualname"] is None
    # Each edge was asked at its own 1-based graph line, converted once.
    keys = {item["key"] for item in fixture.asked()}
    assert fixture.asked_key("app.ts", "helper") in keys
    assert fixture.asked_key("lone.js", "helper") in keys
    targets = {row["target_path"] for row in sg.lsp_provenance(fixture.root)}
    assert targets == {"util.ts"}


def test_cpp_is_skipped_and_its_lexical_edges_are_preserved(tmp_path, monkeypatch):
    """A configured Python server must not touch a C++ file's edges."""
    fixture = _Lsp(tmp_path, monkeypatch, "cpp_control")
    fixture.write("util.hpp", "int util_value();\n")
    fixture.write(
        "util.cpp", '#include "util.hpp"\n\nint call() { return util_value(); }\n'
    )
    fixture.serve()
    fixture.index("util.hpp")

    result = fixture.index("util.cpp")

    assert result["language"] == "cpp"
    assert result["lsp"]["status"] == "skipped"
    assert result["lsp"]["reason"] == "language"
    assert fixture.asked() == []
    assert sg.lsp_provenance(fixture.root) == ()
    assert sg.lsp_health(fixture.root)["attempted"] == 0
    lexical = {
        (row["dst_name"], row["dst_qualname"]) for row in fixture.edges("util.cpp")
    }
    assert lexical == {
        ("util.hpp", None),
        ("call", "util.cpp::call"),
        ("util_value", None),
    }


# --- exact target verification ---------------------------------------------


def test_external_ambiguous_and_symlink_targets_all_fail_closed(tmp_path, monkeypatch):
    fixture = _Lsp(tmp_path, monkeypatch, "fail_closed")
    fixture.write("b.py", PY_TARGET)
    fixture.write(
        "a.py",
        "from missing import other\n\ndef caller():\n    return target_symbol()\n\n\n"
        "def second():\n    return target_symbol()\n",
    )
    outside = tmp_path / "outside" / "dep.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("def target_symbol():\n    return 0\n", encoding="utf-8")
    link = fixture.root / "b_link.py"
    os.symlink(fixture.root / "b.py", link)

    fixture.define(CALL_KEY, {"uri": outside.resolve().as_uri(), "range": TARGET_SPAN})
    fixture.define(
        "a.py:7:11",
        {"workspace_path": "b.py", "range": TARGET_SPAN},
        {"workspace_path": "b.py", "range": [1, 4, 1, 12]},
    )
    fixture.define(IMPORT_KEY, {"uri": link.as_uri(), "range": TARGET_SPAN})
    fixture.serve()
    fixture.index("b.py")

    result = fixture.index("a.py")

    assert result["lsp"]["status"] == "enriched"
    assert result["lsp"]["enriched"] == 0
    assert sg.lsp_provenance(fixture.root) == ()
    assert all(row["dst_qualname"] is None for row in fixture.edges("a.py")
               if row["dst_name"] in {CALL_NAME, IMPORT_NAME})
    health = sg.lsp_health(fixture.root)
    assert health["external"] >= 2
    assert health["ambiguous"] >= 1
    # Units are consistent: three positions in ONE file, each classified once.
    assert health["files_attempted"] == 1
    assert health["attempted"] == 3
    _assert_classified(health)


def test_parameter_on_a_declaration_line_never_binds_the_enclosing_function(
    tmp_path, monkeypatch
):
    """A range naming ``callback`` on ``def outer(callback):`` is not ``outer``."""
    fixture = _Lsp(tmp_path, monkeypatch, "parameter_span")
    fixture.write("c.py", "def outer(callback):\n    return callback()\n")
    # The server resolves the call to the parameter declaration, which shares
    # its start line with the canonical ``outer`` function entity.
    fixture.define_line("c.py", 1, {"workspace_path": "c.py", "range": [0, 10, 0, 18]})
    fixture.serve()

    fixture.index("c.py")

    assert fixture.edge("c.py", "callback")["dst_qualname"] is None
    assert sg.lsp_provenance(fixture.root) == ()
    health = sg.lsp_health(fixture.root)
    assert health["internal"] == 0
    assert health["ambiguous"] >= 1
    _assert_classified(health)


def test_definition_naming_a_different_declaration_than_the_call_fails_closed(
    tmp_path, monkeypatch
):
    """The caller's reference name must name the declaration it binds to."""
    fixture = _Lsp(tmp_path, monkeypatch, "name_mismatch")
    fixture.write(
        "b.py", "def target_symbol():\n    return 2\n\n\ndef other_symbol():\n    return 3\n"
    )
    fixture.write("a.py", PY_CALLER)
    fixture.define(CALL_KEY, {"workspace_path": "b.py", "range": [4, 4, 4, 16]})
    fixture.serve()
    fixture.index("b.py")

    fixture.index("a.py")

    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] is None
    assert sg.lsp_provenance(fixture.root) == ()
    assert sg.lsp_health(fixture.root)["ambiguous"] >= 1


def test_same_named_parameter_on_a_declaration_line_never_binds_it(
    tmp_path, monkeypatch
):
    """Only the declaration's own identifier token names the declaration.

    ``def thing(thing=None):`` carries the name ``thing`` twice. A range over
    the parameter shares both the function's start line and its name, so only
    the exact span can tell the two apart -- while a range over the
    declaration's own identifier still binds.
    """
    fixture = _Lsp(tmp_path, monkeypatch, "shadowing_parameter")
    fixture.write("b.py", "def thing(thing=None):\n    return thing\n")
    fixture.write("a.py", "def caller():\n    return thing()\n")
    fixture.write("c.py", "def other_caller():\n    return thing()\n")
    fixture.define_line("a.py", 1, {"workspace_path": "b.py", "range": [0, 10, 0, 15]})
    fixture.define_line("c.py", 1, {"workspace_path": "b.py", "range": [0, 4, 0, 9]})
    fixture.serve()
    fixture.index("b.py")

    fixture.index("a.py")
    fixture.index("c.py")

    assert fixture.edge("a.py", "thing")["dst_qualname"] is None
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert fixture.edge("c.py", "thing")["dst_qualname"] == "b.py.thing"
    assert len(sg.lsp_provenance(fixture.root, source_path="c.py")) == 1
    health = sg.lsp_health(fixture.root)
    assert health["internal"] == 1
    assert health["ambiguous"] >= 1
    _assert_classified(health)


# --- concurrency -------------------------------------------------------------


def test_concurrent_target_write_is_never_bound_into_the_new_generation(
    tmp_path, monkeypatch
):
    """A result computed against generation N may not land in generation N+1."""
    fixture = _Lsp(tmp_path, monkeypatch, "stale_generation")
    _python_pair(fixture)
    ready, release = fixture.hold()
    fixture.index("b.py")

    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(result=fixture.index("a.py")))
    worker.start()
    try:
        _await(ready)
        # The writer lease is free while the server runs, which is only true
        # because no subprocess is spawned inside the merge transaction.
        fixture.write("b.py", "def target_symbol():\n    return 3\n")
        fixture.index("b.py")
    finally:
        release.write_text("go", encoding="utf-8")
        worker.join(timeout=60.0)

    assert not worker.is_alive()
    call_edge = fixture.edge("a.py", CALL_NAME)
    assert call_edge["dst_qualname"] is None
    assert call_edge["evidence_label"] == "AMBIGUOUS"
    assert call_edge["confidence"] == 0.4
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_health(fixture.root)["stale"] >= 1


def test_concurrent_workspace_change_revokes_the_whole_batch(tmp_path, monkeypatch):
    """A workspace that moved under the server invalidates its whole answer."""
    fixture = _Lsp(tmp_path, monkeypatch, "stale_workspace")
    _python_pair(fixture)
    ready, release = fixture.hold()
    fixture.index("b.py")

    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(result=fixture.index("a.py")))
    worker.start()
    try:
        _await(ready)
        fixture.write("c.py", "def extra():\n    return 3\n")
        fixture.index("c.py")
    finally:
        release.write_text("go", encoding="utf-8")
        worker.join(timeout=60.0)

    assert not worker.is_alive()
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] is None
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    health = sg.lsp_health(fixture.root)
    # The server verified the binding, but it was computed for a workspace
    # this generation no longer has, so it was discarded -- never bound.
    assert health["internal"] >= 1
    assert health["discarded"] >= 1
    assert health["enriched"] == 0
    # Answers verified before commit are not coverage: the discarded batch
    # landed nothing, so the published generation carries no binding and
    # health may not report green on ``internal`` alone.
    assert health["bound_edges"] == 0
    assert health["green"] is False


def test_reader_sees_the_published_generation_while_the_server_runs(
    tmp_path, monkeypatch
):
    fixture = _Lsp(tmp_path, monkeypatch, "concurrent_reader")
    _python_pair(fixture)
    ready, release = fixture.hold()
    fixture.index("b.py")

    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(result=fixture.index("a.py")))
    worker.start()
    try:
        _await(ready)
        conn = sg.connect(sg.resolve_db_path(fixture.root), read_only=True)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE file_path=?", ("a.py",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert count > 0
    finally:
        release.write_text("go", encoding="utf-8")
        worker.join(timeout=60.0)

    assert box["result"]["lsp"]["status"] == "enriched"


# --- revocation --------------------------------------------------------------


def test_changed_target_revokes_the_binding_without_reindexing_the_caller(
    tmp_path, monkeypatch
):
    fixture = _Lsp(tmp_path, monkeypatch, "target_change")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    asked_before = len(fixture.asked())

    fixture.write("b.py", "# edited\ndef target_symbol():\n    return 3\n")
    fixture.index("b.py")

    call_edge = fixture.edge("a.py", CALL_NAME)
    assert call_edge["dst_qualname"] is None
    assert call_edge["evidence_label"] == "AMBIGUOUS"
    assert call_edge["confidence"] == 0.4
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    assert len(fixture.asked()) == asked_before
    assert sg.lsp_health(fixture.root)["revoked"] >= 1


def test_removed_target_revokes_the_binding_without_reindexing_the_caller(
    tmp_path, monkeypatch
):
    fixture = _Lsp(tmp_path, monkeypatch, "target_removed")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    assert sg.impact(fixture.root, "target_symbol", budget=16)["impacted_files"]

    sg.remove_file(fixture.root, "b.py")

    call_edge = fixture.edge("a.py", CALL_NAME)
    assert call_edge["dst_qualname"] is None
    assert call_edge["evidence_label"] == "AMBIGUOUS"
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.impact(fixture.root, "target_symbol", budget=16)["impacted_files"] == []


def test_full_build_revokes_a_deleted_caller_without_a_server(tmp_path, monkeypatch):
    """The full-build merge itself drops a deleted source's evidence."""
    fixture = _Lsp(tmp_path, monkeypatch, "full_build_deleted_caller")
    _python_pair(fixture)
    sg.build_index(fixture.root)
    assert len(sg.lsp_provenance(fixture.root, source_path="a.py")) == 1

    (fixture.root / "a.py").unlink()
    fixture.unserve()
    sg.build_index(fixture.root, incremental=True)

    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    health = sg.lsp_health(fixture.root)
    assert health["bound_edges"] == 0
    assert health["revoked"] >= 1


def test_full_build_revokes_a_changed_target_without_a_server(tmp_path, monkeypatch):
    """An incremental build that re-extracts only the target still revokes."""
    fixture = _Lsp(tmp_path, monkeypatch, "full_build_changed_target")
    _python_pair(fixture)
    sg.build_index(fixture.root)
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME

    fixture.write("b.py", "# edited\ndef target_symbol():\n    return 3\n")
    fixture.unserve()
    sg.build_index(fixture.root, incremental=True)

    call_edge = fixture.edge("a.py", CALL_NAME)
    assert call_edge["dst_qualname"] is None
    assert call_edge["evidence_label"] == "AMBIGUOUS"
    assert call_edge["confidence"] == 0.4
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    # The changed target still exists, but no caller reaches it through the
    # revoked binding any more.
    impacted = sg.impact(fixture.root, "target_symbol", budget=16)["impacted_files"]
    assert "a.py" not in {item["file_path"] for item in impacted}
    assert sg.lsp_health(fixture.root)["revoked"] >= 1


def test_revoking_one_target_of_a_two_target_caller_keeps_evidence_consistent(
    tmp_path, monkeypatch
):
    """Receipt and provenance agree after a partial revocation, then recover."""
    fixture = _Lsp(tmp_path, monkeypatch, "two_targets")
    fixture.write("b.py", PY_TARGET)
    fixture.write("c.py", "def other_symbol():\n    return 3\n")
    fixture.write("a.py", "def caller():\n    target_symbol()\n    return other_symbol()\n")
    fixture.define_line("a.py", 1, {"workspace_path": "b.py", "range": TARGET_SPAN})
    fixture.define_line("a.py", 2, {"workspace_path": "c.py", "range": [0, 4, 0, 16]})
    fixture.serve()
    fixture.index("b.py")
    fixture.index("c.py")
    fixture.index("a.py")
    assert fixture.edge("a.py", "target_symbol")["dst_qualname"] == TARGET_QUALNAME
    assert fixture.edge("a.py", "other_symbol")["dst_qualname"] == "c.py.other_symbol"
    assert sg.lsp_receipt(fixture.root, "a.py")["edge_count"] == 2

    sg.remove_file(fixture.root, "c.py")

    # The surviving binding is kept and still carried; the receipt counts
    # exactly it, and can no longer be reused as a complete answer.
    assert fixture.edge("a.py", "target_symbol")["dst_qualname"] == TARGET_QUALNAME
    assert fixture.edge("a.py", "other_symbol")["dst_qualname"] is None
    _assert_evidence_agrees(fixture, "a.py")
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    assert receipt["edge_count"] == 1
    assert receipt["complete"] == 0

    asked_before = len(fixture.asked())
    fixture.index("a.py")

    # An incomplete receipt is re-asked, including its still-bound position.
    assert len(fixture.asked()) > asked_before
    assert fixture.edge("a.py", "target_symbol")["dst_qualname"] == TARGET_QUALNAME
    _assert_evidence_agrees(fixture, "a.py")

    sg.remove_file(fixture.root, "a.py")

    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    assert sg.lsp_health(fixture.root)["bound_edges"] == 0


def _enriched_javascript_callers(
    tmp_path: Path, monkeypatch, name: str
) -> tuple[_Lsp, dict[str, dict]]:
    """Bind ``app.ts`` and ``lone.js``; return each ``helper`` edge's lexical state.

    The repository is built once with no server, so the label and confidence
    revocation must restore are observed from extraction rather than assumed
    -- the extractor installed on the host decides them. A second ``helper``
    keeps lexical resolution ambiguous, so only the server binds the calls.
    """
    fixture = _Lsp(tmp_path, monkeypatch, name)
    fixture.write("util.ts", "export function helper(): number {\n  return 1;\n}\n")
    fixture.write("other.ts", "export function helper(): number {\n  return 2;\n}\n")
    fixture.write(
        "app.ts",
        'import { helper } from "./missing";\n\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    fixture.write("lone.js", "function run() {\n  return helper();\n}\n")
    sg.build_index(fixture.root)
    lexical = {rel: fixture.edge(rel, "helper") for rel in ("app.ts", "lone.js")}
    definition = {"workspace_path": "util.ts", "range": [0, 16, 0, 22]}
    fixture.define_line("app.ts", 3, definition)
    fixture.define_line("lone.js", 1, definition)
    fixture.serve(TYPESCRIPT_SERVER_ENV)
    for rel, before in lexical.items():
        assert before["dst_qualname"] is None, rel
        assert fixture.index(rel)["lsp"]["enriched"] == 1, rel
        bound = fixture.edge(rel, "helper")
        assert bound["dst_qualname"] == "util.ts::helper", rel
    # Revocation is only observable where the server's evidence differs from
    # what extraction produced. ``app.ts``'s imported call is EXTRACTED
    # lexically already; the bare call in ``lone.js`` carries weaker evidence.
    bound = fixture.edge("lone.js", "helper")
    assert (bound["evidence_label"], bound["confidence"]) != (
        lexical["lone.js"]["evidence_label"], lexical["lone.js"]["confidence"],
    )
    return fixture, lexical


@pytest.mark.parametrize("writer", ["index_file", "incremental_build"])
def test_edited_javascript_target_revokes_evidence_a_resolver_already_cleared(
    tmp_path, monkeypatch, writer
):
    """Revocation may not depend on the destination a resolver just rewrote.

    Re-extracting ``util.ts`` re-runs JS/TS cross-file resolution, which
    clears every lexical call destination -- bound ones included -- in the
    same merge that revokes the stale bindings. Each revoked call must still
    return to exactly what extraction produced, never keep the server's
    label and confidence on an edge that names no target at all.
    """
    fixture, lexical = _enriched_javascript_callers(
        tmp_path, monkeypatch, f"ts_js_target_edit_{writer}"
    )
    fixture.unserve(TYPESCRIPT_SERVER_ENV)

    fixture.write("util.ts", "export function helper(): number {\n  return 3;\n}\n")
    if writer == "index_file":
        fixture.index("util.ts")
    else:
        sg.build_index(fixture.root, incremental=True)

    for rel, before in lexical.items():
        assert fixture.edge(rel, "helper") == before, rel
        assert sg.lsp_provenance(fixture.root, source_path=rel) == ()
        assert sg.lsp_receipt(fixture.root, rel) is None
        _assert_evidence_agrees(fixture, rel)
    health = sg.lsp_health(fixture.root)
    assert health["revoked"] >= len(lexical)
    assert health["bound_edges"] == 0
    # Revoked coverage is not coverage: nothing usable is left in the graph.
    assert health["green"] is False


@pytest.mark.parametrize("writer", ["index_file", "incremental_build"])
def test_edited_javascript_target_keeps_a_fresh_lexical_resolution(
    tmp_path, monkeypatch, writer
):
    """Revoking a binding may not clobber what lexical resolution now proves.

    With ``other.ts`` gone, ``helper`` names exactly one declaration, so the
    merge that revokes the stale bindings also re-resolves both calls
    lexically -- here to the very target the server had named. That fresh
    destination is lexical evidence in its own right: it survives the
    revocation, under extraction's label and confidence rather than the
    server's.
    """
    fixture, lexical = _enriched_javascript_callers(
        tmp_path, monkeypatch, f"ts_js_fresh_lexical_{writer}"
    )
    fixture.unserve(TYPESCRIPT_SERVER_ENV)
    (fixture.root / "other.ts").unlink()
    fixture.write("util.ts", "export function helper(): number {\n  return 3;\n}\n")
    if writer == "index_file":
        sg.remove_file(fixture.root, "other.ts")
        # Removal re-runs no resolver, and every binding still verifies.
        for rel in lexical:
            assert fixture.edge(rel, "helper")["dst_qualname"] == "util.ts::helper"
            assert len(sg.lsp_provenance(fixture.root, source_path=rel)) == 1
        fixture.index("util.ts")
    else:
        sg.build_index(fixture.root, incremental=True)

    for rel, before in lexical.items():
        edge = fixture.edge(rel, "helper")
        assert edge["dst_qualname"] == "util.ts::helper", rel
        assert (edge["evidence_label"], edge["confidence"]) == (
            before["evidence_label"], before["confidence"],
        ), rel
        assert sg.lsp_provenance(fixture.root, source_path=rel) == ()
        assert sg.lsp_receipt(fixture.root, rel) is None
        _assert_evidence_agrees(fixture, rel)


@pytest.mark.parametrize("writer", ["index_file", "incremental_build"])
def test_renamed_python_target_revokes_evidence_a_resolver_already_cleared(
    tmp_path, monkeypatch, writer
):
    """A renamed target is revoked even after its resolver reset the call.

    The Python import resolver clears every call destination that no longer
    names an entity, in the same merge that revokes the stale binding. The
    call must still return to exactly what extraction produced --
    AMBIGUOUS/0.4 with no destination -- not keep the server's EXTRACTED/1.0.
    """
    fixture = _Lsp(tmp_path, monkeypatch, f"python_rename_{writer}")
    _python_pair(fixture)
    sg.build_index(fixture.root)
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME

    fixture.unserve()
    fixture.write("b.py", "def renamed_symbol():\n    return 2\n")
    if writer == "index_file":
        fixture.index("b.py")
    else:
        sg.build_index(fixture.root, incremental=True)

    call_edge = fixture.edge("a.py", CALL_NAME)
    assert call_edge["dst_qualname"] is None
    assert call_edge["evidence_label"] == "AMBIGUOUS"
    assert call_edge["confidence"] == 0.4
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    _assert_evidence_agrees(fixture, "a.py")
    health = sg.lsp_health(fixture.root)
    assert health["revoked"] >= 1
    assert health["bound_edges"] == 0
    assert health["green"] is False


# --- edge ownership ------------------------------------------------------------

# ``b.foo()`` on a module attribute puts two edges on one token: a ``calls``
# edge the Python call resolver cannot bind (``foo`` is no function) and a
# ``references`` edge the reference resolver binds lexically. The server's
# answer binds the call; the reference is a sibling that binding never owns.
ATTR_TARGET = (
    "def _make():\n    return len\n\n\nfoo = _make()\n\n\ndef bar():\n    return 1\n"
)
ATTR_CALLER = "import b\n\n\ndef caller():\n    return b.foo()\n"
# Exactly the ``foo`` identifier in ``foo = _make()``.
ATTR_SPAN = [4, 0, 4, 3]
ATTR_QUALNAME = "b.py.foo"


def _token_edges(fixture: _Lsp, rel: str, dst_name: str) -> dict[str, dict]:
    """Every edge on one reference token, keyed by kind."""
    conn = sg.connect(sg.resolve_db_path(fixture.root), read_only=True)
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT kind, line, source_col, dst_name, dst_qualname, "
                "evidence_label, confidence FROM edges "
                "WHERE file_path=? AND dst_name=? ORDER BY kind",
                (rel, dst_name),
            )
        ]
    finally:
        conn.close()
    edges = {row["kind"]: row for row in rows}
    assert len(edges) == len(rows), rows
    return edges


def _pin_sibling(fixture: _Lsp, target: str) -> None:
    """Give the ``references`` sibling EXTRACTED/1.0 evidence of its own.

    That is exactly what a server binding writes onto the call beside it, so
    only the binding's persisted edge identity can tell the two apart.
    """
    raw = sqlite3.connect(str(sg.resolve_db_path(fixture.root)))
    try:
        raw.execute(
            "UPDATE edges SET dst_qualname=?, evidence_label='EXTRACTED', "
            "confidence=1.0 WHERE file_path='a.py' AND kind='references' "
            "AND dst_name='foo'",
            (target,),
        )
        raw.commit()
    finally:
        raw.close()


def _bound_call_beside_a_resolved_sibling(
    tmp_path: Path, monkeypatch, name: str, sibling: str = "lexical"
) -> tuple[_Lsp, dict[str, dict]]:
    """Bind ``a.py``'s ``b.foo()`` call; return each edge's pre-server state.

    ``sibling`` is the ``references`` edge's own evidence: what lexical
    resolution gives it (``lexical``), or EXTRACTED/1.0 on the binding's own
    target (``same_target``) or on another declaration (``other_target``).
    The state is observed with no server configured, and ``a.py`` is never
    re-extracted afterwards, so the sibling keeps it unless a writer
    touches it.
    """
    fixture = _Lsp(tmp_path, monkeypatch, name)
    fixture.write("b.py", ATTR_TARGET)
    fixture.write("a.py", ATTR_CALLER)
    sg.build_index(fixture.root)
    if sibling != "lexical":
        _pin_sibling(fixture, ATTR_QUALNAME if sibling == "same_target" else "b.py.bar")
    lexical = _token_edges(fixture, "a.py", "foo")
    call, reference = lexical["calls"], lexical["references"]
    # Two edges on one token: one position, one reference name.
    assert (call["line"], call["source_col"]) == (reference["line"], reference["source_col"])
    assert call["dst_qualname"] is None
    assert reference["dst_qualname"] is not None
    fixture.define_line(
        "a.py", call["line"] - 1, {"workspace_path": "b.py", "range": ATTR_SPAN}
    )
    fixture.serve()

    sg.build_index(fixture.root, incremental=True)

    bound = _token_edges(fixture, "a.py", "foo")
    assert bound["calls"]["dst_qualname"] == ATTR_QUALNAME
    assert (bound["calls"]["evidence_label"], bound["calls"]["confidence"]) == (
        "EXTRACTED", 1.0,
    )
    assert bound["references"] == reference
    (binding,) = sg.lsp_provenance(fixture.root, source_path="a.py")
    # The durable binding names the one edge it owns, not only its position.
    assert json.loads(binding["edge_identity"])[:4] == ["calls", "a.py.caller", "foo", "b"]
    return fixture, lexical


@pytest.mark.parametrize("sibling", ["lexical", "same_target", "other_target"])
@pytest.mark.parametrize("revocation", ["index_file", "incremental_build", "server_missing"])
def test_revoking_a_binding_never_touches_a_sibling_on_the_same_token(
    tmp_path, monkeypatch, sibling, revocation
):
    """Revocation restores the one edge the binding owns, and only that edge.

    The ``references`` sibling was resolved without any server -- to the
    binding's own target or elsewhere, under whatever label. It keeps its
    destination, label and confidence through a merge that revokes the
    binding (an edited target, via ``index_file`` or an incremental build)
    and through a pass that clears it (a server that is gone), while the
    call returns to exactly what extraction produced.
    """
    fixture, lexical = _bound_call_beside_a_resolved_sibling(
        tmp_path, monkeypatch, f"sibling_{sibling}_{revocation}", sibling
    )
    if revocation == "server_missing":
        # The receipt no longer matches the configured server, so the pass
        # clears the file's evidence through ``_lsp_clear_file``.
        monkeypatch.setenv(PYTHON_SERVER_ENV, str(tmp_path / "absent-language-server"))
        sg.build_index(fixture.root, incremental=True)
    else:
        fixture.unserve()
        fixture.write("b.py", "# edited\n" + ATTR_TARGET)
        if revocation == "index_file":
            fixture.index("b.py")
        else:
            sg.build_index(fixture.root, incremental=True)

    assert _token_edges(fixture, "a.py", "foo") == lexical
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    health = sg.lsp_health(fixture.root)
    assert health["revoked"] >= 1
    assert health["bound_edges"] == 0
    assert health["green"] is False


def test_noop_reindex_reattaches_the_owned_edge_beside_an_unresolved_sibling(
    tmp_path, monkeypatch
):
    """Re-attachment finds the binding's own edge, not the only unresolved one.

    Re-indexing identical bytes re-extracts both edges on the token
    unresolved, before any resolver runs. The binding re-attaches to its own
    call by identity, so the receipt is reused and the server is not asked
    again -- and the sibling still resolves lexically, exactly as before.
    """
    fixture, _lexical = _bound_call_beside_a_resolved_sibling(
        tmp_path, monkeypatch, "sibling_reattach"
    )
    bound = _token_edges(fixture, "a.py", "foo")
    asked_before = len(fixture.asked())

    assert fixture.index("a.py")["lsp"]["status"] == "reused"

    assert len(fixture.asked()) == asked_before
    assert _token_edges(fixture, "a.py", "foo") == bound
    assert sg.lsp_receipt(fixture.root, "a.py")["edge_count"] == 1
    _assert_evidence_agrees(fixture, "a.py")


def test_a_sibling_on_the_target_never_counts_as_carrying_the_binding(
    tmp_path, monkeypatch
):
    """Only the owned edge carries a binding; a sibling naming the target does not.

    A resolver's reset clears the call's destination (its label stays) while
    the ``references`` sibling beside it still names the same target. The
    next merge must see the binding as not carried and re-attach it to its
    own edge, rather than keep provenance no edge of its own backs.
    """
    fixture, _lexical = _bound_call_beside_a_resolved_sibling(
        tmp_path, monkeypatch, "sibling_carried"
    )
    bound = _token_edges(fixture, "a.py", "foo")
    raw = sqlite3.connect(str(sg.resolve_db_path(fixture.root)))
    try:
        raw.execute(
            "UPDATE edges SET dst_qualname=NULL WHERE file_path='a.py' "
            "AND kind='calls' AND dst_name='foo'"
        )
        raw.commit()
    finally:
        raw.close()
    fixture.unserve()

    # Any merge reconciles; this one re-extracts neither ``a.py`` nor ``b.py``.
    fixture.write("c.py", "def unrelated():\n    return 0\n")
    fixture.index("c.py")

    assert _token_edges(fixture, "a.py", "foo") == bound
    assert len(sg.lsp_provenance(fixture.root, source_path="a.py")) == 1
    assert sg.lsp_receipt(fixture.root, "a.py")["edge_count"] == 1
    assert sg.lsp_health(fixture.root)["bound_edges"] == 1


def _rewrite_provenance_without_edge_identity(fixture: _Lsp) -> None:
    """Rebuild the provenance table in the shape an earlier schema wrote.

    Its rows name only a position. ``connect`` adds the identity column on
    the next write open, and every row carried over is legacy provenance.
    """
    table = sg.LSP_PROVENANCE_TABLE
    raw = sqlite3.connect(str(sg.resolve_db_path(fixture.root)))
    try:
        columns = ", ".join(
            str(row[1])
            for row in raw.execute(f"PRAGMA table_info({table})")
            if str(row[1]) != "edge_identity"
        )
        raw.execute(f"ALTER TABLE {table} RENAME TO legacy_provenance")
        raw.execute(
            f"CREATE TABLE {table} ({columns}, "
            "PRIMARY KEY(source_path, source_line, source_column))"
        )
        raw.execute(f"INSERT INTO {table} SELECT {columns} FROM legacy_provenance")
        raw.execute("DROP TABLE legacy_provenance")
        raw.commit()
    finally:
        raw.close()


def test_legacy_provenance_fails_closed_on_a_token_it_cannot_attribute(
    tmp_path, monkeypatch
):
    """Position-only provenance owns an edge only where no sibling shares it.

    Where exactly one edge at the position names the target, legacy
    provenance still owns it and stays carried. Where a sibling shares the
    token it cannot say which edge the server's answer was written to, so it
    owns nothing: the binding is revoked and no edge on the token is mutated
    -- the sibling least of all. Re-extracting the caller rewrites the whole
    token, and a server then binds it afresh under an edge identity.
    """
    fixture, _lexical = _bound_call_beside_a_resolved_sibling(
        tmp_path, monkeypatch, "legacy_provenance"
    )
    fixture.write("c.py", "def other_caller():\n    return bar()\n")
    # Exactly the ``bar`` identifier in ``def bar():``.
    fixture.define_line("c.py", 1, {"workspace_path": "b.py", "range": [7, 4, 7, 7]})
    assert fixture.index("c.py")["lsp"]["enriched"] == 1
    assert fixture.edge("c.py", "bar")["dst_qualname"] == "b.py.bar"
    bound = _token_edges(fixture, "a.py", "foo")
    _rewrite_provenance_without_edge_identity(fixture)
    fixture.unserve()

    # Any merge reconciles; this one re-extracts neither caller.
    fixture.write("d.py", "def unrelated():\n    return 0\n")
    fixture.index("d.py")

    (legacy,) = sg.lsp_provenance(fixture.root, source_path="c.py")
    assert legacy["edge_identity"] == ""
    assert fixture.edge("c.py", "bar")["dst_qualname"] == "b.py.bar"
    assert sg.lsp_receipt(fixture.root, "c.py")["edge_count"] == 1
    _assert_evidence_agrees(fixture, "c.py")
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    assert _token_edges(fixture, "a.py", "foo") == bound
    assert sg.lsp_health(fixture.root)["revoked"] >= 1

    fixture.serve()
    assert fixture.index("a.py")["lsp"]["enriched"] == 1

    assert _token_edges(fixture, "a.py", "foo") == bound
    (fresh,) = sg.lsp_provenance(fixture.root, source_path="a.py")
    assert json.loads(fresh["edge_identity"])[0] == "calls"
    _assert_evidence_agrees(fixture, "a.py")


# --- receipts ----------------------------------------------------------------


def test_noop_refresh_reuses_the_receipt_without_running_the_server(
    tmp_path, monkeypatch
):
    fixture = _Lsp(tmp_path, monkeypatch, "reuse")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    asked_before = len(fixture.asked())

    second = fixture.index("a.py")

    assert second["lsp"]["status"] == "reused"
    assert len(fixture.asked()) == asked_before
    # Re-indexing dropped and re-extracted the caller's edges; the merge
    # re-attached them from the receipt without asking anything.
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    assert len(sg.lsp_provenance(fixture.root, source_path="a.py")) == 1
    assert second["lsp"]["reused"] == 1


def test_changed_source_revokes_the_receipt_and_reruns_the_batch(tmp_path, monkeypatch):
    fixture = _Lsp(tmp_path, monkeypatch, "source_change")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    asked_before = len(fixture.asked())

    fixture.write(
        "a.py", "from missing import other\n\n\ndef caller():\n    return target_symbol()\n"
    )
    # The call moved down one line, so the old position is no longer answered.
    fixture.define("a.py:4:11", {"workspace_path": "b.py", "range": TARGET_SPAN})
    result = fixture.index("a.py")

    assert result["lsp"]["status"] == "enriched"
    assert len(fixture.asked()) > asked_before
    binding = sg.lsp_provenance(fixture.root, source_path="a.py")[0]
    assert binding["source_line"] == 5
    assert binding["source_hash"] == fixture.hash("a.py")
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    assert sg.lsp_health(fixture.root)["revoked"] >= 1


def test_server_identity_change_revokes_the_receipt_and_rebinds(tmp_path, monkeypatch):
    fixture = _Lsp(tmp_path, monkeypatch, "server_change")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    asked_before = len(fixture.asked())

    monkeypatch.setenv(SERVER_VERSION_ENV, "fake-lsp-2.0")
    result = fixture.index("a.py")

    assert result["lsp"]["status"] == "enriched"
    asked = fixture.asked()[asked_before:]
    # The already-bound call position is re-asked under the new server.
    assert CALL_KEY in {item["key"] for item in asked}
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    # The declared version leads; the observed bytes are always bound too.
    assert receipt["server_version"] == f"fake-lsp-2.0+{fixture.identity()}"
    assert receipt["edge_count"] == 1
    assert receipt["complete"] == 1
    _assert_evidence_agrees(fixture, "a.py")

    asked_before = len(fixture.asked())
    assert fixture.index("a.py")["lsp"]["status"] == "reused"
    assert len(fixture.asked()) == asked_before


def test_full_build_server_or_workspace_change_rebinds_already_bound_positions(
    tmp_path, monkeypatch
):
    """A non-reusable receipt re-asks bound positions, never silently drops them.

    An incremental full build does not re-extract an unchanged caller, so its
    bound edge is not "unresolved" any more. A server-identity change or an
    unrelated file joining the workspace invalidates the receipt; the bound
    position must be asked again and rebound, and the next no-op reused.
    """
    fixture = _Lsp(tmp_path, monkeypatch, "full_build_rebind")
    fixture.write("b.py", PY_TARGET)
    # This caller's *only* unresolved edge is the call the server resolves.
    fixture.write("a.py", "def caller():\n    return target_symbol()\n")
    fixture.define_line("a.py", 1, {"workspace_path": "b.py", "range": TARGET_SPAN})
    fixture.serve()
    sg.build_index(fixture.root)
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME

    for change in ("server", "workspace"):
        if change == "server":
            monkeypatch.setenv(SERVER_VERSION_ENV, "fake-lsp-2.0")
        else:
            fixture.write("unrelated.py", "def unrelated():\n    return 0\n")
        asked_before = len(fixture.asked())

        sg.build_index(fixture.root, incremental=True)

        assert len(fixture.asked()) > asked_before, change
        assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME, change
        receipt = sg.lsp_receipt(fixture.root, "a.py")
        assert receipt is not None and receipt["edge_count"] == 1, change
        assert receipt["complete"] == 1, change
        _assert_evidence_agrees(fixture, "a.py")

        asked_before = len(fixture.asked())
        sg.build_index(fixture.root, incremental=True)
        assert len(fixture.asked()) == asked_before, f"no-op re-asked after {change}"
        assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME


def test_changed_workspace_file_revokes_a_null_answer_receipt(tmp_path, monkeypatch):
    """A receipt describes the whole bounded tree, not only its own file.

    The caller's bytes never change here. It was asked while ``b.py``
    declared nothing it could name, so the server truthfully answered with no
    definition. Once ``b.py`` gains the declaration that answer is stale: a
    refresh must ask again rather than reuse it, and only a true no-op may
    reuse the new receipt.
    """
    fixture = _Lsp(tmp_path, monkeypatch, "tree_receipt")
    fixture.write("b.py", "def unrelated():\n    return 2\n")
    fixture.write("a.py", "def caller():\n    return target_symbol()\n")
    fixture.serve()
    sg.build_index(fixture.root)
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] is None
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    assert receipt is not None and receipt["complete"] == 1
    assert receipt["edge_count"] == 0

    fixture.write("b.py", PY_TARGET)
    fixture.define_line("a.py", 1, {"workspace_path": "b.py", "range": TARGET_SPAN})
    asked_before = len(fixture.asked())
    sg.build_index(fixture.root, incremental=True)

    assert fixture.asked_key("a.py", CALL_NAME) in {
        item["key"] for item in fixture.asked()[asked_before:]
    }
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    assert receipt["complete"] == 1
    assert receipt["edge_count"] == 1
    _assert_evidence_agrees(fixture, "a.py")

    asked_before = len(fixture.asked())
    sg.build_index(fixture.root, incremental=True)
    assert len(fixture.asked()) == asked_before
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME


def test_missing_server_reports_unavailable_and_keeps_no_receipt(tmp_path, monkeypatch):
    fixture = _Lsp(tmp_path, monkeypatch, "no_server")
    fixture.write("b.py", PY_TARGET)
    fixture.write("a.py", PY_CALLER)
    monkeypatch.setenv(PYTHON_SERVER_ENV, str(tmp_path / "absent-language-server"))
    fixture.index("b.py")

    result = fixture.index("a.py")

    assert result["lsp"]["status"] == "unavailable"
    assert result["lsp"]["reason"] == "server_missing"
    assert fixture.asked() == []
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] is None
    health = sg.lsp_health(fixture.root)
    assert health["unavailable"] >= 1
    assert health["green"] is False
    _assert_classified(health)


@pytest.mark.parametrize("mode", ["crash", "hang", "malformed", "garbage"])
def test_server_failure_is_not_cached_and_recovers_on_the_next_run(
    tmp_path, monkeypatch, mode
):
    """A crash, timeout, malformed frame or malformed payload is not a server
    answering "nothing"."""
    monkeypatch.setattr(sg, "LSP_REQUEST_TIMEOUT_S", 1.0)
    monkeypatch.setattr(sg, "LSP_BATCH_TIMEOUT_S", 10.0)
    fixture = _Lsp(tmp_path, monkeypatch, f"failure_{mode}")
    _python_pair(fixture)
    fixture.fail(CALL_KEY, mode)
    fixture.index("b.py")

    failed = fixture.index("a.py")

    assert failed["lsp"]["unavailable"] >= 1
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] is None
    # No reusable receipt: the failure must not block the next attempt.
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    health = sg.lsp_health(fixture.root)
    assert health["unavailable"] >= 1
    assert health["green"] is False
    _assert_classified(health)

    fixture.fail(CALL_KEY, None)
    asked_before = len(fixture.asked())
    recovered = fixture.index("a.py")

    assert recovered["lsp"]["status"] == "enriched"
    assert len(fixture.asked()) > asked_before
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    assert receipt["complete"] == 1
    assert receipt["edge_count"] == 1
    assert fixture.index("a.py")["lsp"]["status"] == "reused"


# --- bounded coverage --------------------------------------------------------


def test_per_file_query_cap_counts_the_remainder_and_never_reuses(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sg, "LSP_MAX_QUERIES_PER_FILE", 1)
    fixture = _Lsp(tmp_path, monkeypatch, "query_cap")
    _python_pair(fixture)
    fixture.index("b.py")

    first = fixture.index("a.py")

    assert first["lsp"]["attempted"] == 1
    assert first["lsp"]["skipped"] == 1
    health = sg.lsp_health(fixture.root)
    assert health["skipped"] == 1
    assert health["files_incomplete"] == 1
    assert health["green"] is False
    _assert_classified(health)
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    assert receipt is None or receipt["complete"] == 0
    _assert_evidence_agrees(fixture, "a.py")

    asked_before = len(fixture.asked())
    second = fixture.index("a.py")

    # A partial query set is never a reusable answer.
    assert second["lsp"]["status"] != "reused"
    assert len(fixture.asked()) > asked_before


def test_file_cap_defers_the_remainder_and_reused_files_do_not_occupy_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sg, "LSP_MAX_FILES_PER_BATCH", 1)
    fixture = _Lsp(tmp_path, monkeypatch, "file_cap")
    fixture.write("b.py", PY_TARGET)
    fixture.write("a1.py", "def first():\n    return target_symbol()\n")
    fixture.write("a2.py", "def second():\n    return target_symbol()\n")
    definition = {"workspace_path": "b.py", "range": TARGET_SPAN}
    fixture.define_line("a1.py", 1, definition)
    fixture.define_line("a2.py", 1, definition)
    fixture.serve()

    sg.build_index(fixture.root)

    bound = {
        rel for rel in ("a1.py", "a2.py")
        if fixture.edge(rel, CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    }
    assert len(bound) == 1
    health = sg.lsp_health(fixture.root)
    assert health["files_deferred"] == 1
    assert health["skipped"] == 1
    assert health["green"] is False

    sg.build_index(fixture.root, incremental=True)

    # The already-answered file is reused and does not occupy the cap, so the
    # deferred one is asked now.
    for rel in ("a1.py", "a2.py"):
        assert fixture.edge(rel, CALL_NAME)["dst_qualname"] == TARGET_QUALNAME, rel
        assert sg.lsp_receipt(fixture.root, rel)["complete"] == 1
    asked_before = len(fixture.asked())

    sg.build_index(fixture.root, incremental=True)

    assert len(fixture.asked()) == asked_before
    assert sg.lsp_health(fixture.root)["files_deferred"] == 1


# --- health ------------------------------------------------------------------


def test_zero_coverage_health_is_not_green_and_names_its_denominators(
    tmp_path, monkeypatch
):
    fixture = _Lsp(tmp_path, monkeypatch, "zero_coverage")
    fixture.write("plain.py", "def value():\n    return 1\n")

    fixture.index("plain.py")

    health = sg.lsp_health(fixture.root)
    for name in sg.LSP_HEALTH_COUNTERS:
        assert health[name] == 0, name
    for name in (
        "attempted", "skipped", "enriched", "reused", "revoked", "internal",
        "external", "ambiguous", "unresolved", "stale", "unavailable",
        "files_attempted", "files_skipped", "files_deferred",
        "batches", "latency_ms_total", "latency_ms_max",
    ):
        assert name in health, name
    assert health["classified"] == 0
    assert set(health["units"]) == {"edges", "files"}
    assert health["latency_ms_avg"] == -1
    assert health["bound_edges"] == 0
    assert health["schema_id"] == sg.LSP_ENRICHMENT_SCHEMA_ID
    assert health["green"] is False


def test_latency_is_reported_with_its_own_denominator(tmp_path, monkeypatch):
    fixture = _Lsp(tmp_path, monkeypatch, "latency")
    _python_pair(fixture)
    fixture.index("b.py")

    fixture.index("a.py")

    health = sg.lsp_health(fixture.root)
    assert health["batches"] == 1
    assert health["latency_ms_total"] >= 0
    assert health["latency_ms_max"] >= 0
    assert health["latency_ms_avg"] == health["latency_ms_total"] // health["batches"]
    assert health["bound_edges"] == 1


def test_provenance_tables_are_created_on_a_pre_migration_generation(
    tmp_path, monkeypatch
):
    """A generation published before this feature existed still enriches."""
    fixture = _Lsp(tmp_path, monkeypatch, "migration")
    _python_pair(fixture)
    fixture.index("b.py")

    raw = sqlite3.connect(str(sg.resolve_db_path(fixture.root)))
    try:
        raw.execute(f"DROP TABLE IF EXISTS {sg.LSP_PROVENANCE_TABLE}")
        raw.execute(f"DROP TABLE IF EXISTS {sg.LSP_RECEIPT_TABLE}")
        raw.commit()
    finally:
        raw.close()

    assert sg.lsp_provenance(fixture.root) == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    assert sg.lsp_health(fixture.root)["bound_edges"] == 0

    summary = fixture.index("a.py")

    assert summary["lsp"]["status"] == "enriched"
    assert len(sg.lsp_provenance(fixture.root, source_path="a.py")) == 1
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME


def _binding_identity(rows: tuple[dict, ...]) -> list[tuple]:
    """Exactly the parts of a binding a no-op refresh may not change.

    ``resolved_at`` is re-stamped whenever evidence is re-published, so it is
    deliberately excluded: this compares the evidence, not when it was last
    written down.
    """
    return [
        (
            row["source_path"], row["source_line"], row["source_column"],
            row["source_hash"], row["target_path"], row["target_hash"],
            row["target_qualname"], row["config_digest"],
        )
        for row in rows
    ]


def test_noop_full_build_reuses_the_receipt_of_a_fully_bound_file(
    tmp_path, monkeypatch
):
    """A no-op full refresh must not revoke what it never re-extracted."""
    fixture = _Lsp(tmp_path, monkeypatch, "noop_full_build")
    fixture.write("b.py", PY_TARGET)
    # This caller's *only* unresolved edge is the call the server resolves.
    fixture.write("a.py", "def caller():\n    return target_symbol()\n")
    fixture.define_line("a.py", 1, {"workspace_path": "b.py", "range": TARGET_SPAN})
    fixture.serve()

    sg.build_index(fixture.root)

    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    assert [row for row in fixture.edges("a.py") if row["dst_qualname"] is None] == []
    bindings = _binding_identity(sg.lsp_provenance(fixture.root, source_path="a.py"))
    assert len(bindings) == 1
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    assert receipt is not None and receipt["edge_count"] == 1
    asked_before = len(fixture.asked())
    published = _counters(sg.lsp_health(fixture.root))

    for refresh in (1, 2):
        sg.build_index(fixture.root, incremental=True)

        assert len(fixture.asked()) == asked_before, f"server re-asked on {refresh}"
        assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
        assert _binding_identity(
            sg.lsp_provenance(fixture.root, source_path="a.py")
        ) == bindings
        current = sg.lsp_receipt(fixture.root, "a.py")
        assert current is not None, f"receipt revoked on refresh {refresh}"
        assert current["source_hash"] == receipt["source_hash"]
        assert current["server_version"] == receipt["server_version"]
        assert current["config_digest"] == receipt["config_digest"]
        assert current["edge_count"] == receipt["edge_count"]

    # The verified target is still what ``callers``/``impact`` read.
    assert sg.impact(fixture.root, "target_symbol", budget=16)["impacted_files"]
    health = sg.lsp_health(fixture.root)
    # Reusing every receipt changed no evidence, so nothing was published.
    assert _counters(health) == published
    assert health["bound_edges"] == 1
    assert health["green"] is True


def test_unchanged_reindex_without_a_server_keeps_evidence_and_edges_in_step(
    tmp_path, monkeypatch
):
    """Identical bytes may not publish evidence the edges do not carry."""
    fixture = _Lsp(tmp_path, monkeypatch, "reindex_no_server")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    asked_before = len(fixture.asked())

    fixture.unserve()
    result = fixture.index("a.py")

    assert result["lsp"]["status"] == "skipped"
    assert result["lsp"]["reason"] == "server"
    assert len(fixture.asked()) == asked_before
    _assert_evidence_agrees(fixture, "a.py")
    # Reconciled by carrying the verified binding forward, not by dropping
    # it: the evidence a server already produced survives a no-op refresh.
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    assert len(sg.lsp_provenance(fixture.root, source_path="a.py")) == 1
    assert sg.lsp_receipt(fixture.root, "a.py")["edge_count"] == 1
    assert sg.lsp_health(fixture.root)["bound_edges"] == 1
    assert sg.impact(fixture.root, "target_symbol", budget=16)["impacted_files"]


def test_unchanged_reindex_fails_closed_on_a_binding_it_cannot_re_attach(
    tmp_path, monkeypatch
):
    """A binding this generation cannot verify is revoked, never re-attached."""
    fixture = _Lsp(tmp_path, monkeypatch, "reindex_fail_closed")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME

    raw = sqlite3.connect(str(sg.resolve_db_path(fixture.root)))
    try:
        raw.execute(
            f"UPDATE {sg.LSP_PROVENANCE_TABLE} SET target_hash=? "
            "WHERE source_path='a.py'",
            ("0" * 64,),
        )
        raw.commit()
    finally:
        raw.close()

    fixture.unserve()
    fixture.index("a.py")

    _assert_evidence_agrees(fixture, "a.py")
    call_edge = fixture.edge("a.py", CALL_NAME)
    assert call_edge["dst_qualname"] is None
    assert call_edge["evidence_label"] == "AMBIGUOUS"
    assert call_edge["confidence"] == 0.4
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    assert sg.lsp_receipt(fixture.root, "a.py") is None
    assert sg.lsp_health(fixture.root)["bound_edges"] == 0
    assert sg.lsp_health(fixture.root)["revoked"] >= 1


def test_edge_without_a_recorded_column_is_never_asked_at_a_guessed_one(
    tmp_path, monkeypatch
):
    """Exact call-site coordinates only: no recorded column, no question.

    An extractor that recorded no column for an edge leaves nothing exact to
    ask. Asking column 0 instead would query whatever token starts the line,
    so the edge is counted in its own denominator and left exactly as
    lexical extraction produced it.
    """
    fixture = _Lsp(tmp_path, monkeypatch, "no_column")
    _python_pair(fixture)
    fixture.unserve()
    sg.build_index(fixture.root)
    raw = sqlite3.connect(str(sg.resolve_db_path(fixture.root)))
    try:
        # Exactly what an extractor without call-site columns records.
        raw.execute(
            "UPDATE edges SET source_col=-1 WHERE file_path='a.py' AND dst_name=?",
            (CALL_NAME,),
        )
        raw.commit()
    finally:
        raw.close()
    fixture.define_line("a.py", 3, {"workspace_path": "b.py", "range": TARGET_SPAN})
    fixture.serve()

    sg.build_index(fixture.root, incremental=True)

    asked = {item["key"] for item in fixture.asked()}
    assert IMPORT_KEY in asked
    assert not [key for key in asked if key.startswith("a.py:3:")]
    call_edge = fixture.edge("a.py", CALL_NAME)
    assert call_edge["dst_qualname"] is None
    assert call_edge["evidence_label"] == "AMBIGUOUS"
    assert sg.lsp_provenance(fixture.root, source_path="a.py") == ()
    health = sg.lsp_health(fixture.root)
    assert health["unpositioned"] == 1
    assert health["attempted"] == 1
    _assert_classified(health)


def test_group_with_nothing_to_ask_reports_its_denominator_and_publishes_nothing(
    tmp_path, monkeypatch
):
    """A file a configured group refused to ask about is still a denominator.

    The pass's own typed summary carries it. The pass changed no evidence,
    so it publishes no generation of its own, and health is not green.
    """
    fixture = _Lsp(tmp_path, monkeypatch, "skipped_denominator")
    fixture.write("b.py", PY_TARGET)
    fixture.serve()
    staged = _count_staged_generations(monkeypatch)

    result = fixture.index("b.py")

    assert result["lsp"]["status"] == "skipped"
    assert result["lsp"]["reason"] == "no_unresolved"
    assert result["lsp"]["files"] == 1
    assert fixture.asked() == []
    # Only the index's own generation: the pass that asked nothing staged none.
    assert len(staged) == 1
    health = sg.lsp_health(fixture.root)
    assert health["skipped"] == 0
    assert health["attempted"] == 0
    assert health["batches"] == 0
    assert health["latency_ms_avg"] == -1
    assert health["green"] is False


def test_receipt_reuse_reports_reused_without_inflating_enriched(
    tmp_path, monkeypatch
):
    """A no-op refresh may not grow the numerator ``attempted`` denominates.

    It reports what it reused in its own typed summary and publishes nothing,
    so the durable counters stay exactly what the enriching pass recorded.
    """
    fixture = _Lsp(tmp_path, monkeypatch, "reuse_numerator")
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    first = sg.lsp_health(fixture.root)
    # Two positions (the import and the call) in one file.
    assert first["attempted"] == 2
    assert first["files_attempted"] == 1
    assert first["enriched"] == 1
    assert first["reused"] == 0
    asked_before = len(fixture.asked())

    for refresh in (1, 2, 3):
        summary = fixture.index("a.py")

        assert summary["lsp"]["status"] == "reused"
        assert summary["lsp"]["reused"] == 1, refresh
        assert summary["lsp"]["enriched"] == 0, refresh
        health = sg.lsp_health(fixture.root)
        assert len(fixture.asked()) == asked_before, f"re-asked on {refresh}"
        assert _counters(health) == _counters(first), refresh
        assert health["bound_edges"] == 1
        _assert_evidence_agrees(fixture, "a.py")


# --- publication cost, server identity, failed passes -----------------------


def _counters(health: dict) -> dict:
    """Every durable health counter, and nothing derived from them."""
    return {name: health[name] for name in sg.LSP_HEALTH_COUNTERS}


def _count_staged_generations(monkeypatch) -> list[bool]:
    """Record each staged generation any writer opens, and whether it cloned."""
    opened: list[bool] = []
    original = sg._staged_generation

    @contextlib.contextmanager
    def counted(canonical_path, *, copy_existing):
        opened.append(bool(copy_existing))
        with original(canonical_path, copy_existing=copy_existing) as staging_path:
            yield staging_path

    monkeypatch.setattr(sg, "_staged_generation", counted)
    return opened


@pytest.mark.parametrize("refresh", ["index_file", "build_index"])
def test_unchanged_refresh_stages_no_generation_for_enrichment(
    tmp_path, monkeypatch, refresh
):
    """Reusing every receipt must never copy the canonical database.

    The refresh's own merge stages exactly the one generation it always has.
    Enrichment that reused every receipt changed no evidence, so it stages
    none -- each configured group used to clone the whole canonical database
    (345 MB on this repository) just to record its reuse counters.
    """
    fixture = _Lsp(tmp_path, monkeypatch, f"no_clone_{refresh}")
    fixture.write("b.py", PY_TARGET)
    fixture.write("a.py", "def caller():\n    return target_symbol()\n")
    fixture.define_line("a.py", 1, {"workspace_path": "b.py", "range": TARGET_SPAN})
    fixture.serve()
    sg.build_index(fixture.root)
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    before = sg.lsp_health(fixture.root)
    assert before["green"] is True
    asked_before = len(fixture.asked())
    staged = _count_staged_generations(monkeypatch)

    if refresh == "index_file":
        assert fixture.index("a.py")["lsp"]["status"] == "reused"
    else:
        sg.build_index(fixture.root, incremental=True)

    assert staged == [True], staged
    assert len(fixture.asked()) == asked_before
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    after = sg.lsp_health(fixture.root)
    assert _counters(after) == _counters(before)
    assert after["green"] is True
    _assert_evidence_agrees(fixture, "a.py")


def test_server_replaced_at_the_same_path_revokes_its_receipts(tmp_path, monkeypatch):
    """Same command, same path, new bytes and nothing declared: never reused."""
    fixture = _Lsp(tmp_path, monkeypatch, "server_replaced")
    monkeypatch.delenv(SERVER_VERSION_ENV, raising=False)
    _python_pair(fixture)
    fixture.index("b.py")
    fixture.index("a.py")
    first = sg.lsp_receipt(fixture.root, "a.py")
    assert first["complete"] == 1
    assert first["server_version"] == fixture.identity()
    assert first["server_version"].startswith("sha256:")
    asked_before = len(fixture.asked())

    # An in-place upgrade: the command line and every path stay the same.
    fixture.script.write_text(FAKE_LSP_SERVER + "\n# rebuilt\n", encoding="utf-8")

    result = fixture.index("a.py")

    assert result["lsp"]["status"] == "enriched"
    assert CALL_KEY in {item["key"] for item in fixture.asked()[asked_before:]}
    receipt = sg.lsp_receipt(fixture.root, "a.py")
    assert receipt["server_version"] == fixture.identity() != first["server_version"]
    assert receipt["config_digest"] != first["config_digest"]
    assert {
        row["server_version"]
        for row in sg.lsp_provenance(fixture.root, source_path="a.py")
    } == {receipt["server_version"]}
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    _assert_evidence_agrees(fixture, "a.py")

    asked_before = len(fixture.asked())
    assert fixture.index("a.py")["lsp"]["status"] == "reused"
    assert len(fixture.asked()) == asked_before


def test_server_identity_binds_the_bytes_a_command_resolves_to(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    server = bin_dir / "langserver-1"
    server.write_bytes(b"#!/bin/sh\nexec real-server --stdio\n")
    link = bin_dir / "langserver"
    os.symlink(server, link)
    command = (str(link), "--stdio")

    first = sg._lsp_server_identity(command)
    assert first.startswith("sha256:")
    assert sg._lsp_server_identity(command) == first

    # Replaced behind the same symlinked path, the way a package manager does.
    staged = bin_dir / "langserver-1.new"
    staged.write_bytes(b"#!/bin/sh\nexec real-server-2 --stdio\n")
    os.replace(staged, server)
    assert sg._lsp_server_identity(command) != first

    # An npm install: the entry shim is identical across versions, and only
    # the package manifest says which version is installed.
    package = tmp_path / "node_modules" / "fake-langserver"
    (package / "lib").mkdir(parents=True)
    entry = package / "lib" / "cli.js"
    entry.write_text("require('../dist/server.js');\n", encoding="utf-8")
    manifest = package / "package.json"
    manifest.write_text('{"version": "1.0.0"}\n', encoding="utf-8")
    shim = (sys.executable, str(entry), "--stdio")
    before = sg._lsp_server_identity(shim)
    manifest.write_text('{"version": "1.10.0"}\n', encoding="utf-8")
    assert sg._lsp_server_identity(shim) != before

    # Bytes that cannot be read identify nothing, so no two runs ever match.
    missing = (str(bin_dir / "absent-langserver"), "--stdio")
    unidentified = sg._lsp_server_identity(missing)
    assert unidentified.startswith("unidentified:")
    assert sg._lsp_server_identity(missing) != unidentified


@pytest.mark.parametrize(
    ("fault", "changed"),
    [("raises", True), ("raises", False), ("lease_busy", True)],
)
def test_a_pass_that_raises_durably_fails_health_closed(
    tmp_path, monkeypatch, fault, changed
):
    """Green earned before a failed pass must not survive it.

    The failure is published by a staged generation of its own -- in the
    lease case once the other writer has gone -- and, exactly like
    ``unavailable``, it stays on the record: the next pass succeeding does
    not launder this one.
    """
    fixture = _Lsp(tmp_path, monkeypatch, f"failed_pass_{fault}_{changed}")
    _python_pair(fixture)
    sg.build_index(fixture.root)
    assert sg.lsp_health(fixture.root)["green"] is True
    if changed:
        # The call moves down one line, so this pass owes new evidence.
        fixture.write(
            "a.py",
            "from missing import other\n\n\ndef caller():\n    return target_symbol()\n",
        )
        fixture.define("a.py:4:11", {"workspace_path": "b.py", "range": TARGET_SPAN})
    if fault == "raises":
        def explode(*_args, **_kwargs):
            raise RuntimeError("injected enrichment fault")

        restore = ("_lsp_plan_group", sg._lsp_plan_group)
        monkeypatch.setattr(sg, "_lsp_plan_group", explode)
        expected = "RuntimeError"
    else:
        real_lease = sg.index_write_lease
        leases: list[Path] = []

        @contextlib.contextmanager
        def busy_when_the_pass_publishes(repo_root):
            leases.append(repo_root)
            if len(leases) == 2:  # 1: the index's own merge; 2: the pass
                yield False
                return
            with real_lease(repo_root) as acquired:
                yield acquired

        restore = ("index_write_lease", real_lease)
        monkeypatch.setattr(sg, "LSP_LEASE_WAIT_S", 0.0)
        monkeypatch.setattr(sg, "index_write_lease", busy_when_the_pass_publishes)
        expected = "SourceGraphBuildInProgressError"

    failed = fixture.index("a.py")

    assert failed["lsp"]["status"] == "error"
    assert failed["lsp"]["reason"] == expected
    health = sg.lsp_health(fixture.root)
    assert health["failed_passes"] == 1
    assert health["last_failure"]["reason"] == expected
    assert health["green"] is False
    if changed:
        # Nothing the failed pass computed was published.
        assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] is None
        assert sg.lsp_receipt(fixture.root, "a.py") is None
    else:
        assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    _assert_evidence_agrees(fixture, "a.py")

    monkeypatch.setattr(sg, *restore)
    recovered = fixture.index("a.py")

    assert recovered["lsp"]["status"] == ("enriched" if changed else "reused")
    assert fixture.edge("a.py", CALL_NAME)["dst_qualname"] == TARGET_QUALNAME
    health = sg.lsp_health(fixture.root)
    assert health["failed_passes"] == 1
    assert health["green"] is False


def test_published_evidence_moves_the_query_cache_generation(tmp_path, monkeypatch):
    """A cache keyed on generation identity may not keep serving old bindings."""
    fixture = _Lsp(tmp_path, monkeypatch, "cache_generation")
    _python_pair(fixture)
    fixture.index("b.py")
    indexed = fixture.index("a.py")

    assert indexed["lsp"]["enriched"] == 1
    assert fixture.meta("single_file_last_mutation")["operation"] == "lsp_enrich"

    # A no-op refresh publishes nothing of its own: the index's mutation stands.
    fixture.index("a.py")
    assert fixture.meta("single_file_last_mutation")["operation"] == "index"
