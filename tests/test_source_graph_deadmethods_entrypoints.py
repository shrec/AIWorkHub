"""Entrypoint applicability for Source Graph ``deadmethods`` (NF-2026-00568)."""

from __future__ import annotations

import re
import sqlite3
import textwrap
from pathlib import Path

import pytest

from aiworkhub.source_graph_analytics import _risk_views


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE edges (kind TEXT, dst_qualname TEXT)")
    yield connection
    connection.close()


def _write(repo_root: Path, rel: str, source: str) -> str:
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = textwrap.dedent(source).strip("\n") + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _def_line(text: str, name: str) -> int:
    pattern = re.compile(rf"^(?:async[ \t]+)?(?:def|class)[ \t]+{re.escape(name)}\b")
    for index, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line.strip()):
            return index
    return 1


def _scan(
    conn: sqlite3.Connection,
    repo_root: Path,
    rel: str,
    names: list[tuple[str, str]],
    source: str,
) -> dict:
    text = _write(repo_root, rel, source)
    rows = [
        {
            "file_path": rel,
            "kind": kind,
            "name": name,
            "qualname": f"{rel}.{name}",
            "line_start": _def_line(text, name),
            "line_end": len(text.splitlines()),
        }
        for name, kind in names
    ]
    return _risk_views(conn, repo_root, "deadmethods", rows, budget=50)


def _flagged(result: dict) -> set[str]:
    return {str(finding["qualname"]).rsplit(".", 1)[-1] for finding in result["findings"]}


def test_pytest_collected_functions_are_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "tests/test_mod.py",
        [("test_collected", "function"), ("_helper", "function")],
        """
        def test_collected():
            return _helper()

        def _helper():
            return 1
        """,
    )
    flagged = _flagged(result)
    assert "test_collected" not in flagged
    assert "_helper" in flagged


def test_test_prefix_outside_collect_files_is_still_reported(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg.py",
        [("test_not_collected", "function")],
        """
        def test_not_collected():
            return 1
        """,
    )
    assert "test_not_collected" in _flagged(result)


def test_pytest_fixture_in_collect_file_is_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "tests/conftest.py",
        [("db", "function")],
        """
        import pytest

        @pytest.fixture
        def db():
            return 1
        """,
    )
    assert "db" not in _flagged(result)


def test_mcp_tool_decorator_is_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/server.py",
        [("handle_status", "function"), ("orphan_prod", "function")],
        """
        @mcp.tool()
        def handle_status():
            return True

        def orphan_prod():
            return False
        """,
    )
    flagged = _flagged(result)
    assert "handle_status" not in flagged
    assert "orphan_prod" in flagged


def test_mcp_registration_call_is_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/tools.py",
        [("handle_plan", "function")],
        """
        def handle_plan():
            return 1

        mcp.tool(name="plan")(handle_plan)
        """,
    )
    assert "handle_plan" not in _flagged(result)


def test_name_only_handler_in_src_is_still_reported(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/handlers.py",
        [("handler", "function")],
        """
        def handler():
            return 1
        """,
    )
    assert "handler" in _flagged(result)


def test_dunder_main_cli_entrypoint_is_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg/cli.py",
        [("entry", "function"), ("unused", "function")],
        """
        def entry():
            return 0

        def unused():
            return 1

        if __name__ == "__main__":
            entry()
        """,
    )
    flagged = _flagged(result)
    assert "entry" not in flagged
    assert "unused" in flagged


def test_name_main_without_cli_evidence_is_still_reported(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg/util.py",
        [("main", "function")],
        """
        def main():
            return 0
        """,
    )
    assert "main" in _flagged(result)


def test_dunder_main_docstring_comment_and_string_decoys_do_not_hide_orphans(
    conn, tmp_path: Path
) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg/cli.py",
        [("entry", "function"), ("unused", "function")],
        '''
        """Example:
        if __name__ == "__main__":
            unused()
        """
        # if __name__ == "__main__":
        #     unused()
        SAMPLE = """
        if __name__ == "__main__":
            unused()
        """

        def entry():
            return 0

        def unused():
            return 1

        if __name__ == "__main__":
            entry()
        ''',
    )
    flagged = _flagged(result)
    assert "entry" not in flagged
    assert "unused" in flagged


def test_every_top_level_dunder_main_guard_is_examined(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg/cli.py",
        [("entry_a", "function"), ("entry_b", "function"), ("unused", "function")],
        '''
        def entry_a():
            return 0

        def entry_b():
            return 1

        def unused():
            return 2

        if __name__ == "__main__":
            entry_a()

        if __name__ == "__main__":
            entry_b()
        ''',
    )
    flagged = _flagged(result)
    assert "entry_a" not in flagged
    assert "entry_b" not in flagged
    assert "unused" in flagged


def test_attribute_call_under_main_is_not_local_entrypoint(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg/cli.py",
        [("entry", "function")],
        """
        def entry():
            return 0

        if __name__ == "__main__":
            app.entry()
        """,
    )
    assert "entry" in _flagged(result)


def test_nested_function_call_under_main_is_not_executed(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg/cli.py",
        [("entry", "function")],
        """
        def entry():
            return 0

        if __name__ == "__main__":
            def nested():
                entry()
        """,
    )
    assert "entry" in _flagged(result)


def test_packaging_console_script_is_not_deadmethods(conn, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project.scripts]\napp = "pkg.cli:entry"\n',
        encoding="utf-8",
    )
    result = _scan(
        conn,
        tmp_path,
        "pkg/cli.py",
        [("entry", "function"), ("unused", "function")],
        """
        def entry():
            return 0

        def unused():
            return 1
        """,
    )
    flagged = _flagged(result)
    assert "entry" not in flagged
    assert "unused" in flagged


def test_cli_command_decorator_is_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg/app.py",
        [("build", "function")],
        """
        @app.command()
        def build():
            return 0
        """,
    )
    assert "build" not in _flagged(result)


def test_resolved_incoming_call_is_not_deadmethods(conn, tmp_path: Path) -> None:
    qualname = "src/pkg.py.reached"
    conn.execute(
        "INSERT INTO edges(kind, dst_qualname) VALUES ('calls', ?)",
        (qualname,),
    )
    text = _write(
        tmp_path,
        "src/pkg.py",
        """
        def reached():
            return 1
        """,
    )
    result = _risk_views(
        conn,
        tmp_path,
        "deadmethods",
        [
            {
                "file_path": "src/pkg.py",
                "kind": "function",
                "name": "reached",
                "qualname": qualname,
                "line_start": _def_line(text, "reached"),
                "line_end": len(text.splitlines()),
            }
        ],
        budget=50,
    )
    assert "reached" not in _flagged(result)


def test_deadmethods_metadata_states_unresolved_edge_limitation(
    conn, tmp_path: Path
) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/pkg.py",
        [("orphan_symbol", "function")],
        """
        def orphan_symbol():
            return 2
        """,
    )
    evidence = result["incoming_edge_evidence"]
    assert "dispatch" in evidence and "mcp" in evidence
    assert "not_execution_proof" in evidence
    limitation = result["applicability"]["limitation"]
    assert "unresolved" in limitation
    assert "proof" in limitation
    assert result["findings"][0]["reasons"] == [
        "no_resolved_incoming_calls_dynamic_dispatch_unobserved"
    ]


def test_indented_class_body_decorators_are_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/server.py",
        [
            ("handle_status", "method"),
            ("build", "method"),
            ("orphan_prod", "function"),
        ],
        """
        class Gateway:
            @mcp.tool()
            def handle_status(self):
                return True

            @app.command()
            def build(self):
                return 0

        def orphan_prod():
            return False
        """,
    )
    flagged = _flagged(result)
    assert "handle_status" not in flagged
    assert "build" not in flagged
    assert "orphan_prod" in flagged


def test_indented_pytest_fixture_is_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "tests/test_mod.py",
        [("db", "method")],
        """
        import pytest

        class TestSuite:
            @pytest.fixture
            def db(self):
                return 1
        """,
    )
    assert "db" not in _flagged(result)


def test_multiline_decorators_are_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/server.py",
        [
            ("handle_status", "function"),
            ("build", "function"),
            ("orphan_prod", "function"),
        ],
        """
        @mcp.tool(
            name="status",
        )
        def handle_status():
            return True

        @app.command(
            name="build",
        )
        def build():
            return 0

        def orphan_prod():
            return False
        """,
    )
    flagged = _flagged(result)
    assert "handle_status" not in flagged
    assert "build" not in flagged
    assert "orphan_prod" in flagged


def test_indented_multiline_decorators_are_not_deadmethods(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/server.py",
        [("handle_status", "method"), ("build", "method"), ("orphan_prod", "function")],
        """
        class Gateway:
            @mcp.tool(
                name="status",
            )
            def handle_status(self):
                return True

            @app.command(
                name="build",
            )
            def build(self):
                return 0

        def orphan_prod():
            return False
        """,
    )
    flagged = _flagged(result)
    assert "handle_status" not in flagged
    assert "build" not in flagged
    assert "orphan_prod" in flagged


def test_entrypoint_evidence_after_24000_bytes_is_not_deadmethods(
    conn, tmp_path: Path
) -> None:
    padding = "# pad\n" * 4001
    late = textwrap.dedent(
        """
        @mcp.tool()
        def handle_late():
            return True

        def entry():
            return 0

        def orphan_prod():
            return False

        if __name__ == "__main__":
            entry()
        """
    ).strip("\n") + "\n"
    source = padding + late
    assert len(padding) > 24000
    result = _scan(
        conn,
        tmp_path,
        "src/server.py",
        [
            ("handle_late", "function"),
            ("entry", "function"),
            ("orphan_prod", "function"),
        ],
        source,
    )
    flagged = _flagged(result)
    assert "handle_late" not in flagged
    assert "entry" not in flagged
    assert "orphan_prod" in flagged


def test_mcp_registration_string_docstring_and_comment_decoys_do_not_hide_orphans(
    conn, tmp_path: Path
) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/tools.py",
        [("unused", "function"), ("handle_plan", "function")],
        '''
        """Example:
        mcp.tool(name="plan")(unused)
        """
        # mcp.tool(name="plan")(unused)
        SAMPLE = """
        mcp.tool(name="plan")(unused)
        """

        def unused():
            return 1

        def handle_plan():
            return 0

        mcp.tool(name="plan")(handle_plan)
        ''',
    )
    flagged = _flagged(result)
    assert "handle_plan" not in flagged
    assert "unused" in flagged


def test_nested_mcp_registration_is_not_executed(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/tools.py",
        [("unused", "function")],
        """
        def unused():
            return 1

        def factory():
            mcp.tool(name="plan")(unused)
            def nested():
                mcp.tool(name="plan")(unused)
            (lambda: mcp.tool(name="plan")(unused))
        """,
    )
    assert "unused" in _flagged(result)


def test_unrelated_pyproject_sections_and_colon_strings_do_not_hide_orphans(
    conn, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "demo"
            description = "pkg.cli:unused"

            [tool.custom]
            foo = "pkg.cli:unused"

            [project.optional-dependencies]
            dev = ["pkg.cli:unused"]

            [project.scripts]
            app = "pkg.cli:entry"

            [project.gui-scripts]
            gui = "pkg.cli:gui_entry"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    result = _scan(
        conn,
        tmp_path,
        "pkg/cli.py",
        [("entry", "function"), ("gui_entry", "function"), ("unused", "function")],
        """
        def entry():
            return 0

        def gui_entry():
            return 0

        def unused():
            return 1
        """,
    )
    flagged = _flagged(result)
    assert "entry" not in flagged
    assert "gui_entry" not in flagged
    assert "unused" in flagged


def test_setup_cfg_console_scripts_ignore_unrelated_sections(conn, tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text(
        textwrap.dedent(
            """
            [metadata]
            description = pkg.cli:unused

            [options.entry_points]
            console_scripts =
                app = pkg.cli:entry
            gui_scripts =
                gui = pkg.cli:gui_entry
            pytest11 =
                plugin = pkg.cli:unused
            """
        ).lstrip(),
        encoding="utf-8",
    )
    result = _scan(
        conn,
        tmp_path,
        "pkg/cli.py",
        [("entry", "function"), ("gui_entry", "function"), ("unused", "function")],
        """
        def entry():
            return 0

        def gui_entry():
            return 0

        def unused():
            return 1
        """,
    )
    flagged = _flagged(result)
    assert "entry" not in flagged
    assert "gui_entry" not in flagged
    assert "unused" in flagged


def _scan_same_leaf_packaging_mains(conn: sqlite3.Connection, repo_root: Path) -> dict:
    text = _write(
        repo_root,
        "pkg/cli.py",
        """
        class Runner:
            def main():
                return 0

        class Other:
            def main():
                return 1
        """,
    )
    rows = []
    current = ""
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("class "):
            current = stripped.split()[1].split("(")[0].rstrip(":")
        if stripped.startswith("def main"):
            rows.append(
                {
                    "file_path": "pkg/cli.py",
                    "kind": "method",
                    "name": "main",
                    "qualname": f"pkg/cli.py.{current}.main",
                    "line_start": index,
                    "line_end": index + 1,
                }
            )
    return _risk_views(conn, repo_root, "deadmethods", rows, budget=50)


def test_pyproject_dotted_object_ref_exempts_exact_symbol_not_same_leaf_sibling(
    conn, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project.scripts]\napp = "pkg.cli:Runner.main"\n',
        encoding="utf-8",
    )
    result = _scan_same_leaf_packaging_mains(conn, tmp_path)
    flagged = {str(finding["qualname"]) for finding in result["findings"]}
    assert "pkg/cli.py.Runner.main" not in flagged
    assert "pkg/cli.py.Other.main" in flagged


def test_setup_cfg_dotted_object_ref_exempts_exact_symbol_not_same_leaf_sibling(
    conn, tmp_path: Path
) -> None:
    (tmp_path / "setup.cfg").write_text(
        textwrap.dedent(
            """
            [options.entry_points]
            console_scripts =
                app = pkg.cli:Runner.main
            """
        ).lstrip(),
        encoding="utf-8",
    )
    result = _scan_same_leaf_packaging_mains(conn, tmp_path)
    flagged = {str(finding["qualname"]) for finding in result["findings"]}
    assert "pkg/cli.py.Runner.main" not in flagged
    assert "pkg/cli.py.Other.main" in flagged


def _scan_same_leaf_call_identities(
    conn: sqlite3.Connection,
    repo_root: Path,
    rel: str,
    source: str,
) -> dict:
    text = _write(repo_root, rel, source)
    rows: list[dict] = []
    current = ""
    current_indent = -1
    for index, line in enumerate(text.splitlines(), start=1):
        raw_indent = len(line) - len(line.lstrip(" \t"))
        stripped = line.strip()
        if stripped.startswith("class "):
            current = stripped.split()[1].split("(")[0].rstrip(":")
            current_indent = raw_indent
            continue
        if current and stripped and raw_indent <= current_indent:
            current = ""
            current_indent = -1
        def_match = re.match(r"^(?:async[ \t]+)?def[ \t]+(\w+)\b", stripped)
        if def_match is None:
            continue
        name = def_match.group(1)
        identity = f"{current}.{name}" if current else name
        rows.append(
            {
                "file_path": rel,
                "kind": "method" if current else "function",
                "name": name,
                "qualname": f"{rel}.{identity}",
                "line_start": index,
                "line_end": index + 1,
            }
        )
    return _risk_views(conn, repo_root, "deadmethods", rows, budget=50)


def test_dunder_main_same_leaf_excludes_invoked_identity_only(conn, tmp_path: Path) -> None:
    result = _scan_same_leaf_call_identities(
        conn,
        tmp_path,
        "pkg/cli.py",
        """
        class Worker:
            def entry():
                return 1

        def entry():
            return 0

        if __name__ == "__main__":
            entry()
        """,
    )
    flagged = {str(finding["qualname"]) for finding in result["findings"]}
    assert "pkg/cli.py.entry" not in flagged
    assert "pkg/cli.py.Worker.entry" in flagged


def test_dunder_main_attribute_same_leaf_excludes_invoked_identity_only(
    conn, tmp_path: Path
) -> None:
    result = _scan_same_leaf_call_identities(
        conn,
        tmp_path,
        "pkg/cli.py",
        """
        class Worker:
            def entry():
                return 1

        def entry():
            return 0

        if __name__ == "__main__":
            Worker.entry()
        """,
    )
    flagged = {str(finding["qualname"]) for finding in result["findings"]}
    assert "pkg/cli.py.Worker.entry" not in flagged
    assert "pkg/cli.py.entry" in flagged


def test_mcp_registration_same_leaf_excludes_registered_identity_only(
    conn, tmp_path: Path
) -> None:
    result = _scan_same_leaf_call_identities(
        conn,
        tmp_path,
        "pkg/tools.py",
        """
        class Worker:
            def handle():
                return 1

        def handle():
            return 0

        mcp.tool()(handle)
        """,
    )
    flagged = {str(finding["qualname"]) for finding in result["findings"]}
    assert "pkg/tools.py.handle" not in flagged
    assert "pkg/tools.py.Worker.handle" in flagged


def test_mcp_registration_attribute_same_leaf_excludes_registered_identity_only(
    conn, tmp_path: Path
) -> None:
    result = _scan_same_leaf_call_identities(
        conn,
        tmp_path,
        "pkg/tools.py",
        """
        class Worker:
            def handle():
                return 1

        def handle():
            return 0

        mcp.tool()(Worker.handle)
        """,
    )
    flagged = {str(finding["qualname"]) for finding in result["findings"]}
    assert "pkg/tools.py.Worker.handle" not in flagged
    assert "pkg/tools.py.handle" in flagged


def test_comment_trivia_between_mcp_cli_pytest_decorators_still_excludes(
    conn, tmp_path: Path
) -> None:
    mcp_cli = _scan(
        conn,
        tmp_path,
        "src/server.py",
        [
            ("handle_status", "function"),
            ("build", "function"),
            ("orphan_prod", "function"),
        ],
        """
        @mcp.tool()
        # status handler
        def handle_status():
            return True

        @app.command()
        # build command
        def build():
            return 0

        def orphan_prod():
            return False
        """,
    )
    flagged = _flagged(mcp_cli)
    assert "handle_status" not in flagged
    assert "build" not in flagged
    assert "orphan_prod" in flagged
    pytest_result = _scan(
        conn,
        tmp_path,
        "tests/conftest.py",
        [("db", "function"), ("orphan_helper", "function")],
        """
        import pytest

        @pytest.fixture
        # db fixture
        def db():
            return 1

        def orphan_helper():
            return 2
        """,
    )
    pytest_flagged = _flagged(pytest_result)
    assert "db" not in pytest_flagged
    assert "orphan_helper" in pytest_flagged


def test_comment_only_decorator_lookalikes_do_not_exclude(conn, tmp_path: Path) -> None:
    result = _scan(
        conn,
        tmp_path,
        "src/server.py",
        [("handle_status", "function"), ("build", "function")],
        """
        # @mcp.tool()
        def handle_status():
            return True

        # @app.command()
        def build():
            return 0
        """,
    )
    flagged = _flagged(result)
    assert "handle_status" in flagged
    assert "build" in flagged
