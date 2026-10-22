"""A ``path:line`` Source Graph target must answer about the symbol around that line.

Before NF-2026-01646 both modes searched for the whole ``path:line`` string as words:
``context`` on ``src/aiworkhub/worker_workspace.py:6490`` answered with the module's
import entities instead of the function holding that line, and ``calls`` on
``vscode-extension/extension.js:1097`` answered with call edges recorded in unrelated
C++ files. These tests pin what each mode returns for an indexed file plus a line, and
that every other query is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository

SHAPES = "pkg/shapes.py"
BIG = "pkg/big.py"
FAN_OUT = "pkg/fan_out.py"
BIG_FUNCTION_COUNT = 240
FAN_OUT_CALLEES = ("first", "second", "third", "fourth", "fifth", "sixth")

SHAPES_SOURCE = (
    "import os\n"
    "import sys\n"
    "\n"
    "LIMIT = 10\n"
    "\n"
    "\n"
    "def alpha(value):\n"
    "    total = value + LIMIT\n"
    "    return _helper(total)\n"
    "\n"
    "\n"
    "def _helper(value):\n"
    "    return os.path.join(str(value), \"x\")\n"
    "\n"
    "\n"
    "REGISTRY = {\"alpha\": alpha}\n"
    "\n"
    "\n"
    "class Shape:\n"
    "    kind = \"shape\"\n"
    "\n"
    "    def area(self, scale):\n"
    "        def inner(factor):\n"
    "            return _helper(factor)\n"
    "        return inner(scale) * 2\n"
    "\n"
    "    def describe(self):\n"
    "        return str(self.area(1))\n"
    "\n"
    "\n"
    "def omega():\n"
    "    return sys.argv\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _line_of(root: Path, rel: str, text: str) -> int:
    lines = (root / rel).read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        if line.strip() == text:
            return number
    raise AssertionError(f"{text!r} is not a line of {rel}")


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("line_target") / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="line_target")
    _write(root / SHAPES, SHAPES_SOURCE)
    # A caller in another file: its edge belongs to ``run``, never to ``alpha``'s lines.
    _write(root / "pkg" / "other.py", (
        "from pkg.shapes import alpha\n"
        "\n"
        "\n"
        "def run(value):\n"
        "    return alpha(value)\n"
    ))
    # More entities than ``context`` ever lists for one file (it stops at 200).
    _write(root / BIG, "\n\n".join(
        f"def handler_{index:03d}(value):\n    result = value + {index}\n    return result\n"
        for index in range(BIG_FUNCTION_COUNT)
    ))
    _write(root / FAN_OUT, (
        "\n\n".join(f"def {name}(value):\n    return value\n" for name in FAN_OUT_CALLEES)
        + "\n\ndef fan_out(value):\n"
        + "".join(f"    {name}(value)\n" for name in FAN_OUT_CALLEES)
        + "    return value\n"
    ))
    sg.build_index(root, incremental=False)
    return root


def _context(repo: Path, query: str) -> dict:
    return sg.context_query(repo, query, 64)


def _lead(repo: Path, rel: str, text: str) -> dict:
    payload = _context(repo, f"{rel}:{_line_of(repo, rel, text)}")
    return payload["contexts"][0]["entities"][0]


def _calls(repo: Path, query: str, *, target: str | None = None, budget: int = 64) -> dict:
    return sg.analytics_query(repo, "calls", query, budget, target=target)


def _edges(payload: dict) -> list[dict]:
    return list(payload["outgoing_calls"]) + list(payload["incoming_calls"])


def test_context_line_inside_a_function_leads_with_that_function(repo: Path) -> None:
    line = _line_of(repo, SHAPES, "total = value + LIMIT")
    payload = _context(repo, f"{SHAPES}:{line}")

    context = payload["contexts"][0]
    lead = context["entities"][0]
    assert (lead["kind"], lead["name"]) == ("function", "alpha")
    assert lead["line_start"] <= line <= lead["line_end"]
    assert lead["line_resolution"] == "enclosing"
    assert lead["source"].startswith("def alpha(value):")
    assert "total = value + LIMIT" in lead["source"]
    assert lead["source_lines"] == [lead["line_start"], lead["line_end"]]
    assert payload["matches"][0]["qualname"] == lead["qualname"]
    assert payload["line_resolution"] == "enclosing"
    assert context["line_resolution"] == "enclosing"
    assert payload["line_target"] == {"file_path": SHAPES, "line": line}
    assert payload["candidate_files"] == [SHAPES]

    # What follows the symbol lies inside it, not at the top of the file.
    inside = range(lead["line_start"], lead["line_end"] + 1)
    assert all(entity["line_start"] in inside for entity in context["entities"])
    assert context["edges"]
    assert all(edge["line"] in inside for edge in context["edges"])
    assert "_helper" in {edge["dst_name"] for edge in context["edges"]}


@pytest.mark.parametrize(
    ("text", "kind", "qualname_tail"),
    [
        ("return _helper(factor)", "function", "Shape.area.inner"),
        ("return inner(scale) * 2", "method", "Shape.area"),
        ("return str(self.area(1))", "method", "Shape.describe"),
        ("kind = \"shape\"", "class", "Shape"),
        ("return sys.argv", "function", "omega"),
    ],
)
def test_context_prefers_the_innermost_enclosing_definition(
    repo: Path, text: str, kind: str, qualname_tail: str,
) -> None:
    lead = _lead(repo, SHAPES, text)

    assert lead["kind"] == kind
    assert lead["qualname"].endswith(f".{qualname_tail}")
    assert lead["line_resolution"] == "enclosing"
    assert text in lead["source"]


def test_context_reaches_a_symbol_past_the_entities_a_file_context_lists(repo: Path) -> None:
    plain = _context(repo, BIG)["contexts"][0]["entities"]
    assert len(plain) <= 8
    assert "handler_230" not in {entity["name"] for entity in plain}

    lead = _lead(repo, BIG, "result = value + 230")

    assert lead["name"] == "handler_230"
    assert lead["line_resolution"] == "enclosing"
    assert "result = value + 230" in lead["source"]


def test_context_module_level_line_returns_the_nearest_preceding_definition(repo: Path) -> None:
    line = _line_of(repo, SHAPES, "REGISTRY = {\"alpha\": alpha}")
    payload = _context(repo, f"{SHAPES}:{line}")

    lead = payload["contexts"][0]["entities"][0]
    assert lead["name"] == "_helper"
    assert lead["line_end"] < line
    assert lead["line_resolution"] == "nearest_preceding"
    assert "os.path.join" in lead["source"]
    assert payload["matches"][0]["qualname"] == lead["qualname"]
    assert payload["line_resolution"] == "nearest_preceding"


def test_context_line_before_any_definition_falls_back_to_the_file(repo: Path) -> None:
    line = _line_of(repo, SHAPES, "LIMIT = 10")
    payload = _context(repo, f"{SHAPES}:{line}")

    assert payload["line_resolution"] == "none"
    assert payload["line_target"] == {"file_path": SHAPES, "line": line}
    assert payload["candidate_files"] == [SHAPES]
    entities = payload["contexts"][0]["entities"]
    assert entities
    assert not any("line_resolution" in entity for entity in entities)


def test_context_without_an_indexed_line_target_is_unchanged(repo: Path) -> None:
    whole_file = _context(repo, SHAPES)
    assert whole_file["candidate_files"] == [SHAPES]

    for query in (
        SHAPES, "alpha", "nowhere/missing.py:7", f"{SHAPES}:0", f"{SHAPES}:x", f"{SHAPES}:",
    ):
        payload = _context(repo, query)
        assert "line_resolution" not in payload, query
        assert "line_target" not in payload, query
        assert not any(
            "line_resolution" in entity
            for item in payload["contexts"] for entity in item["entities"]
        ), query


def test_context_line_target_accepts_windows_separators(repo: Path) -> None:
    line = _line_of(repo, SHAPES, "total = value + LIMIT")

    lead = _context(repo, f"pkg\\shapes.py:{line}")["contexts"][0]["entities"][0]

    assert lead["name"] == "alpha"
    assert lead["line_resolution"] == "enclosing"


def test_context_line_target_withholds_source_from_a_file_changed_since_indexing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="line_target_stale")
    _write(root / "mod.py", "def handler(value):\n    return value + 1\n")
    sg.build_index(root, incremental=False)
    # The index still holds the old line numbers; slicing the new file with them would
    # show code from a different generation.
    _write(root / "mod.py", "# one new first line\ndef handler(value):\n    return value + 1\n")

    lead = _context(root, "mod.py:2")["contexts"][0]["entities"][0]

    assert lead["name"] == "handler"
    assert lead["source"] == ""
    assert "source_lines" not in lead


def test_calls_line_target_returns_only_the_enclosing_symbols_edges(repo: Path) -> None:
    line = _line_of(repo, SHAPES, "return _helper(total)")
    payload = _calls(repo, f"{SHAPES}:{line}")

    assert payload["scope"] == "line_target_symbol"
    symbol = payload["query_symbol"]
    assert symbol["resolution"] == "enclosing_symbol_at_line"
    assert symbol["resolved"].endswith("shapes.py.alpha")
    assert symbol["file_path"] == SHAPES
    assert symbol["line"] == line
    assert payload["outgoing_calls"]
    assert {row["caller_symbol"] for row in payload["outgoing_calls"]} == {symbol["resolved"]}
    assert {row["caller_file"] for row in payload["outgoing_calls"]} == {SHAPES}
    assert "_helper" in {row["callee_symbol"] for row in payload["outgoing_calls"]}
    # ``run`` in pkg/other.py calls ``alpha``. That caller is alpha's incoming
    # edge even though it lives in another file; omitting it claimed no callers.
    assert {row["caller_file"] for row in payload["incoming_calls"]} == {"pkg/other.py"}
    assert {row["callee_symbol"] for row in payload["incoming_calls"]} == {"alpha"}
    assert {row["dst_qualname"] for row in payload["incoming_calls"]} <= {symbol["resolved"], None}
    assert "pkg/other.py" not in json.dumps(payload["outgoing_calls"])
    assert payload["query_symbol"]["freshness"] == "fresh"
    assert payload["query_symbol"]["line_binding_unreliable"] is False
    assert payload["files"] == [SHAPES]
    assert payload["coverage"]["returned"] == len(_edges(payload))
    assert payload["next_cursor"] is None
    assert not payload["truncated"]


def test_calls_line_target_with_no_calls_is_an_empty_answer_for_that_symbol(repo: Path) -> None:
    line = _line_of(repo, BIG, "result = value + 230")
    payload = _calls(repo, f"{BIG}:{line}")

    assert payload["scope"] == "line_target_symbol"
    assert payload["query_symbol"]["resolved"].endswith("big.py.handler_230")
    assert payload["outgoing_calls"] == []
    assert payload["incoming_calls"] == []


def test_calls_line_target_outside_every_definition_names_no_symbol(repo: Path) -> None:
    line = _line_of(repo, SHAPES, "LIMIT = 10")
    payload = _calls(repo, f"{SHAPES}:{line}")

    assert payload["scope"] == "line_target_unresolved"
    assert payload["query_symbol"]["resolution"] == "no_enclosing_symbol_at_line"
    assert payload["query_symbol"]["resolved"] is None
    assert _edges(payload) == []
    assert payload["files"] == [SHAPES]
    assert payload["coverage"]["eligible"] == 0


def test_calls_line_target_is_never_widened_beyond_an_explicit_target(repo: Path) -> None:
    query = f"{SHAPES}:{_line_of(repo, SHAPES, 'return _helper(total)')}"

    inside = _calls(repo, query, target="pkg")
    outside = _calls(repo, query, target="pkg/other.py")

    assert inside["scope"] == "line_target_symbol"
    assert inside["query_symbol"]["resolved"].endswith("shapes.py.alpha")
    assert outside["scope"] == "line_target_unresolved"
    assert outside["query_symbol"]["resolution"] == "line_target_outside_target_scope"
    assert outside["files"] == []
    assert _edges(outside) == []


def test_calls_line_target_says_when_the_edge_list_was_cut(repo: Path) -> None:
    query = f"{FAN_OUT}:{_line_of(repo, FAN_OUT, 'third(value)')}"

    full = _calls(repo, query)
    cut = _calls(repo, query, budget=3)

    assert full["query_symbol"]["resolved"].endswith("fan_out.py.fan_out")
    assert len(full["outgoing_calls"]) == len(FAN_OUT_CALLEES)
    assert not full["truncated"]
    assert cut["truncated"] is True
    assert len(cut.get("outgoing_calls", [])) < len(FAN_OUT_CALLEES)


def test_calls_line_target_accepts_windows_separators(repo: Path) -> None:
    line = _line_of(repo, SHAPES, "return _helper(total)")

    payload = _calls(repo, f"pkg\\shapes.py:{line}")

    assert payload["scope"] == "line_target_symbol"
    assert payload["query_symbol"]["resolved"].endswith("shapes.py.alpha")


def test_calls_without_an_indexed_line_target_keeps_its_symbol_resolution(repo: Path) -> None:
    exact = _calls(repo, "alpha", target=SHAPES)
    assert exact["scope"] == "query_symbol_matches"
    assert exact["query_symbol"]["resolution"] == "exact_symbol_match"
    assert exact["query_symbol"]["resolved"].endswith("shapes.py.alpha")

    for query in ("nowhere/missing.py:7", f"{SHAPES}:0", f"{SHAPES}:x"):
        payload = _calls(repo, query, target=SHAPES)
        assert payload["scope"] == "file_level_call_edges", query
        assert payload["query_symbol"]["resolution"] == "no_exact_symbol_in_scope", query
        assert "line" not in payload["query_symbol"], query


def test_calls_line_target_returns_callers_and_cuts_both_directions(tmp_path: Path) -> None:
    """Callers of a line-target symbol are real edges, and a cut in either direction is flagged."""

    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="line_target_incoming")
    _write(root / "pkg" / "target.py", "def target(value):\n    return value\n")
    for index in range(5):
        _write(
            root / "pkg" / f"caller_{index}.py",
            "from pkg.target import target\n\n\n"
            f"def caller_{index}(value):\n"
            "    return target(value)\n",
        )
    sg.build_index(root, incremental=False)
    query = f"pkg/target.py:{_line_of(root, 'pkg/target.py', 'return value')}"

    full = _calls(root, query)
    cut = _calls(root, query, budget=2)

    resolved = full["query_symbol"]["resolved"]
    assert resolved.endswith("target.py.target")
    assert full["outgoing_calls"] == []
    assert len(full["incoming_calls"]) == 5
    assert {row["callee_symbol"] for row in full["incoming_calls"]} == {"target"}
    assert {row["dst_qualname"] for row in full["incoming_calls"]} <= {resolved, None}
    assert {row["caller_file"] for row in full["incoming_calls"]} == {
        f"pkg/caller_{index}.py" for index in range(5)
    }
    assert not full["truncated"]
    assert not full["query_symbol"]["edges_truncated"]
    # Outgoing fits in the budget; incoming does not. The flag must still fire.
    assert cut["outgoing_calls"] == []
    assert len(cut["incoming_calls"]) == 2
    assert cut["query_symbol"]["edges_truncated"] is True
    assert cut["truncated"] is True
    assert {row["callee_symbol"] for row in cut["incoming_calls"]} == {"target"}
    assert {row["dst_qualname"] for row in cut["incoming_calls"]} <= {resolved, None}


def test_calls_line_target_marks_a_stale_file_binding_unreliable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="line_target_calls_stale")
    _write(root / "mod.py", "def handler(value):\n    return value + 1\n")
    sg.build_index(root, incremental=False)
    # The index still holds the old line numbers. Binding without saying so
    # would map an edited file onto the wrong generation.
    _write(root / "mod.py", "# one new first line\ndef handler(value):\n    return value + 1\n")

    payload = _calls(root, "mod.py:2")

    assert payload["scope"] == "line_target_symbol"
    assert payload["query_symbol"]["resolved"].endswith("mod.py.handler")
    assert payload["query_symbol"]["freshness"] == "stale"
    assert payload["query_symbol"]["line_binding_unreliable"] is True
    assert payload["freshness"] == "stale"
    assert payload["line_binding_unreliable"] is True


def test_context_line_target_source_lines_match_the_fitted_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="line_target_fit")
    # Longer than the line-target window, and long enough that budget=1's byte
    # cap shortens ``source`` after a pre-fit range would have been sealed.
    body = "\n".join(
        f"    value = value + {index}  # {'x' * 80}" for index in range(80)
    )
    _write(root / "wide.py", f"def target_deep(value):\n{body}\n    return value\n")
    sg.build_index(root, incremental=False)
    line = _line_of(root, "wide.py", "return value")
    window = "\n".join(
        (root / "wide.py").read_text(encoding="utf-8").splitlines()[42:82]
    )[:4000]

    payload = sg.context_query(root, f"wide.py:{line}", 1)
    lead = payload["contexts"][0]["entities"][0]
    source = lead["source"]

    assert lead["name"] == "target_deep"
    assert lead["line_resolution"] == "enclosing"
    assert isinstance(source, str) and source
    assert len(source) < len(window)
    first, last = lead["source_lines"]
    assert first == 43
    assert last == first + source.count("\n")
    assert last < first + window.count("\n")
