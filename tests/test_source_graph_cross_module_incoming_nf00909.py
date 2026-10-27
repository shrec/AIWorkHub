"""Production callers must not vanish behind a same-named test double.

Before NF-2026-00909 ``calls`` attributed an unresolved call edge (callee name
known, owner not) to the queried symbol only when the WHOLE repository held
exactly one definition of that name. A fake method of the same name in a test
file therefore made every production caller of the real method disappear, and
the empty ``incoming_calls`` read as "nobody calls this". Production code never
calls into test files, so a non-test caller now judges uniqueness among non-test
definitions only, and an edge that still cannot be attributed is reported as
such instead of a silent ``[]``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("cross_module_incoming") / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="cross_module_incoming")
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "core.py", (
        "class Manager:\n"
        "    def close_it(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def lonely_helper():\n"
        "    return 2\n"
    ))
    # A module-level seam whose first parameter is ``self``: the call cannot be
    # bound to a class, so it is recorded as an unresolved ``close_it`` edge.
    _write(root / "pkg" / "seam.py", (
        "def run(self):\n"
        "    return self.close_it()\n"
        "\n"
        "\n"
        "def stop(self):\n"
        "    return self.shut_down()\n"
    ))
    # Two production definitions sharing one name: no caller may be guessed.
    _write(root / "pkg" / "alpha.py", (
        "class Alpha:\n"
        "    def shut_down(self):\n"
        "        return 'a'\n"
    ))
    _write(root / "pkg" / "beta.py", (
        "class Beta:\n"
        "    def shut_down(self):\n"
        "        return 'b'\n"
    ))
    _write(root / "tests" / "test_fake.py", (
        "class FakeManager:\n"
        "    def close_it(self):\n"
        "        return 0\n"
    ))
    _write(root / "tests" / "test_other.py", (
        "def test_uses_some_object(obj):\n"
        "    obj.close_it()\n"
    ))
    sg.build_index(root, incremental=False)
    return root


def _calls(repo: Path, query: str, target: str | None) -> dict:
    return sg.analytics_query(repo, "calls", query, 64, target=target)


def test_production_caller_survives_a_same_named_test_double(repo: Path) -> None:
    payload = _calls(repo, "close_it", "pkg/core.py")

    assert payload["scope"] == "query_symbol_matches"
    assert payload["query_symbol"]["file_path"] == "pkg/core.py"
    callers = {row["caller_file"] for row in payload["incoming_calls"]}
    assert "pkg/seam.py" in callers
    assert any(
        row["caller_file"] == "pkg/seam.py"
        and str(row["caller_symbol"]).endswith("run")
        and row["callee_symbol"] == "close_it"
        for row in payload["incoming_calls"]
    )


def test_test_caller_of_an_ambiguous_name_is_counted_not_attributed(repo: Path) -> None:
    payload = _calls(repo, "close_it", "pkg/core.py")

    # ``obj.close_it()`` in a test may target the real method or the fake one,
    # so it is not published as a caller of pkg/core.py -- but it is counted.
    assert "tests/test_other.py" not in {
        row["caller_file"] for row in payload["incoming_calls"]
    }
    assert payload["incoming_completeness"] == "unresolved_same_name_edges_not_attributed"
    assert payload["unresolved_same_name_incoming_count"] >= 1


def test_two_production_definitions_attribute_nothing_and_say_so(repo: Path) -> None:
    payload = _calls(repo, "shut_down", "pkg/alpha.py")

    assert payload["scope"] == "query_symbol_matches"
    assert payload["query_symbol"]["file_path"] == "pkg/alpha.py"
    assert payload["incoming_calls"] == []
    assert payload["incoming_completeness"] == "unresolved_same_name_edges_not_attributed"
    assert payload["unresolved_same_name_incoming_count"] >= 1


def test_unique_symbol_without_callers_is_complete_and_empty(repo: Path) -> None:
    payload = _calls(repo, "lonely_helper", "pkg/core.py")

    assert payload["scope"] == "query_symbol_matches"
    assert payload["incoming_calls"] == []
    assert payload["incoming_completeness"] == "complete"
    assert payload["unresolved_same_name_incoming_count"] == 0


def test_line_target_gets_the_same_attribution_and_signal(repo: Path) -> None:
    payload = _calls(repo, "pkg/core.py:3", None)

    assert payload["scope"] == "line_target_symbol"
    assert str(payload["query_symbol"]["resolved"]).endswith("close_it")
    assert "pkg/seam.py" in {row["caller_file"] for row in payload["incoming_calls"]}
    assert "tests/test_other.py" not in {
        row["caller_file"] for row in payload["incoming_calls"]
    }
    assert payload["incoming_completeness"] == "unresolved_same_name_edges_not_attributed"
    assert payload["unresolved_same_name_incoming_count"] >= 1


@pytest.mark.parametrize(("path", "expected"), [
    ("tests/test_fake.py", True),
    ("pkg/test/helpers.py", True),
    ("pkg/test_thing.py", True),
    ("pkg/thing_test.py", True),
    ("web/app.test.js", True),
    ("web/app.spec.js", True),
    ("tests\\nested\\conftest.py", True),
    ("pkg/core.py", False),
    ("pkg/testing_utils.py", False),
    ("src/aiworkhub/process_launcher_accept_review.py", False),
    ("", False),
])
def test_test_source_path_predicate(path: str, expected: bool) -> None:
    assert sg._is_test_source_path(path) is expected
