"""NF-2026-01699: Source Graph file mode keeps trivial edges out of its edge window."""

import json

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import worker_ai_tools_mcp as wtm
from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.source_graph_ast import EDGE_KINDS
from source_graph_content_helpers import content_payload

EDGE_WINDOW = 16
TRIVIAL_CANDIDATE_KINDS = ("imports", "annotates")
NEVER_DROPPED_KINDS = tuple(kind for kind in EDGE_KINDS if kind not in TRIVIAL_CANDIDATE_KINDS)

STDLIB_MODULES = (
    "abc", "argparse", "ast", "base64", "bisect", "collections", "contextlib", "copy",
    "csv", "dataclasses", "datetime", "enum", "functools", "hashlib", "io", "itertools",
    "json", "logging", "os", "re", "sys",
)
TRIVIAL_IMPORT_TARGETS = frozenset(
    {"__future__.annotations", "pathlib.Path", "typing.Any", *STDLIB_MODULES}
)
PRIMITIVE_ANNOTATIONS = frozenset({"str", "int", "bool", "float", "bytes", "Any", "object"})

MOD = "pkg/mod.py"
STRUCTURAL = "pkg/structural.py"
HELPERS_SOURCE = "def helper(value):\n    return value\n"
MOD_SOURCE = "\n".join([
    "from __future__ import annotations",
    "",
    *[f"import {name}" for name in STDLIB_MODULES],
    "from pathlib import Path",
    "from typing import Any",
    "",
    "from pkg.helpers import helper",
    "",
    "",
    "class Base:",
    "    pass",
    "",
    "",
    "class Child(Base):",
    "    def configure(self, name: str, size: int, flag: bool, ratio: float,",
    "                  blob: bytes, extra: Any, ref: object) -> None:",
    "        self.name = name",
    "",
    "",
    "def run(path: str, count: int) -> bool:",
    "    value = helper(path)",
    "    return os.path.exists(value) and count > 0",
]) + "\n"
STRUCTURAL_SOURCE = "\n".join([
    "import json",
    "",
    "",
    "class Wrapper(object):",
    "    def render(self, value):",
    "        text = str(value)",
    "        self.last = int(text)",
    "        return json.dumps(text)",
]) + "\n"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("trivial_edges")
    bootstrap_repository(root, repo_name="trivial_edges")
    package = root / "pkg"
    package.mkdir()
    for name, source in (
        ("helpers.py", HELPERS_SOURCE),
        ("mod.py", MOD_SOURCE),
        ("structural.py", STRUCTURAL_SOURCE),
    ):
        (package / name).write_bytes(source.encode("utf-8"))
    sg.build_index(root, incremental=False)
    return root


def _all_edges(root, relative):
    conn = sg.connect(sg.resolve_db_path(root), read_only=True)
    try:
        return sg.context(conn, relative)["edges"]
    finally:
        conn.close()


def _trivial_in_fixture(edge):
    if edge["kind"] == "imports":
        return edge["dst_name"] in TRIVIAL_IMPORT_TARGETS
    return edge["kind"] == "annotates" and edge["dst_name"] in PRIMITIVE_ANNOTATIONS


def _without(mapping, *keys):
    return {key: value for key, value in mapping.items() if key not in keys}


def test_default_window_surfaces_structural_and_repository_edges(repo):
    all_edges = _all_edges(repo, MOD)
    kept = [edge for edge in all_edges if not _trivial_in_fixture(edge)]
    trivial_count = len(all_edges) - len(kept)

    assert trivial_count > EDGE_WINDOW
    # Before the change the window held only these stdlib imports.
    assert [edge["kind"] for edge in all_edges[:EDGE_WINDOW]] == ["imports"] * EDGE_WINDOW
    assert all(_trivial_in_fixture(edge) for edge in all_edges[:EDGE_WINDOW])

    context = sg.file_query(repo, MOD)["contexts"][0]
    assert context["edges"] == kept[:EDGE_WINDOW]
    assert context["trivial_edges_dropped"] == trivial_count
    assert {"defines", "calls", "inherits", "writes", "references"} <= {
        edge["kind"] for edge in context["edges"]
    }
    assert any(
        edge["kind"] == "imports" and edge["dst_qualname"] == "pkg/helpers.py.helper"
        for edge in context["edges"]
    )


@pytest.mark.parametrize("budget", [16, 64])
def test_include_trivial_edges_returns_the_unfiltered_slice(repo, budget):
    all_edges = _all_edges(repo, MOD)
    limit = max(1, min(EDGE_WINDOW, budget // 2))

    unfiltered = sg.file_query(repo, MOD, budget, include_trivial_edges=True)
    context = unfiltered["contexts"][0]
    assert context["edges"] == all_edges[:limit]
    assert "trivial_edges_dropped" not in context

    default = sg.file_query(repo, MOD, budget)
    window = default["contexts"][0]
    kept = [edge for edge in all_edges if not _trivial_in_fixture(edge)]
    assert window["edges"] == kept[:limit]
    assert window["edges"] != context["edges"]
    assert _without(default, "contexts") == _without(unfiltered, "contexts")
    assert _without(window, "edges", "trivial_edges_dropped") == _without(context, "edges")


def test_structural_edges_are_never_dropped(repo):
    all_edges = _all_edges(repo, STRUCTURAL)
    structural = [edge for edge in all_edges if edge["kind"] in NEVER_DROPPED_KINDS]
    assert any(
        edge["kind"] == "inherits" and edge["dst_name"] == "object" for edge in structural
    )
    assert len(all_edges) - len(structural) == 1
    assert len(structural) <= EDGE_WINDOW

    context = sg.file_query(repo, STRUCTURAL)["contexts"][0]
    assert context["edges"] == structural
    assert context["trivial_edges_dropped"] == 1


@pytest.mark.parametrize("kind", NEVER_DROPPED_KINDS)
@pytest.mark.parametrize("dst_name", ["str", "object", "Any", "os", "os.path", "__future__"])
def test_predicate_keeps_every_non_candidate_kind(kind, dst_name):
    edge = {"kind": kind, "dst_name": dst_name, "dst_qualname": None}
    assert sg._is_trivial_edge(edge, "python") is False


@pytest.mark.parametrize(
    ("kind", "dst_name", "dst_qualname", "language", "expected"),
    [
        ("annotates", "str", None, "python", True),
        ("annotates", "bytes", None, "python", True),
        ("annotates", "None", None, "python", True),
        ("annotates", "Any", None, "python", True),
        ("annotates", "NoReturn", None, "python", True),
        ("annotates", "Helper", None, "python", False),
        ("annotates", "Path", None, "python", False),
        ("annotates", "str", "pkg/shadow.py.str", "python", False),
        ("annotates", "str", None, "javascript", False),
        ("imports", "os", None, "python", True),
        ("imports", "os.path", None, "python", True),
        ("imports", "pathlib.Path", None, "python", True),
        ("imports", "__future__.annotations", None, "python", True),
        ("imports", "pkg.helpers.helper", "pkg/helpers.py.helper", "python", False),
        ("imports", "pkg.helpers.helper", None, "python", False),
        ("imports", ".helpers.helper", None, "python", False),
        ("imports", "..helper", None, "python", False),
        ("imports", "logging", "logging/__init__.py.logging", "python", False),
        ("imports", "os", None, "javascript", False),
        ("imports", "", None, "python", False),
    ],
)
def test_predicate_classifies_candidate_edges(kind, dst_name, dst_qualname, language, expected):
    edge = {"kind": kind, "dst_name": dst_name, "dst_qualname": dst_qualname}
    assert sg._is_trivial_edge(edge, language) is expected


def test_mcp_file_mode_serves_the_filtered_window(repo):
    ctx = wtm.WorkerToolContext(
        task_id="nf01699-task",
        runner="test",
        topic="source_graph_file_trivial_edges",
        request_id="nf01699-request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(MOD,),
        allowed_writes=(MOD,),
        session_topic="source_graph_file_trivial_edges",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=None,
    )
    reply = wtm.source_graph_query(
        ctx, mode="file", target=MOD, query=MOD,
        workflow_stage="implementation", compact_replay=False,
    )
    assert reply["ok"] is True
    served = content_payload(reply)["contexts"][0]
    assert served["trivial_edges_dropped"] > 0
    assert served["edges"]
    assert not any(_trivial_in_fixture(edge) for edge in served["edges"])
