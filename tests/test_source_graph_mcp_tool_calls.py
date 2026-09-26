"""JS callTool names bind only to the one Python function a *_TOOLS map names."""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _edges(repo: Path, tool_name: str) -> set[str]:
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        rows = conn.execute(
            "SELECT dst_qualname FROM edges WHERE kind='calls' AND dst_name=? "
            "AND extractor=? ORDER BY dst_qualname",
            (tool_name, sg._MCP_TOOL_PROTOCOL_EXTRACTOR),
        ).fetchall()
    finally:
        conn.close()
    return {str(row["dst_qualname"]) for row in rows}
    return [str(row["dst_qualname"]) for row in rows]


def test_string_call_tool_binds_the_registered_function(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    (repo / "server.py").write_text(
        "def health_view():\n"
        "    return 1\n"
        "READONLY_TOOLS = {\n"
        "    \"aiworkhub_dashboard_health\": health_view,\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "app.js").write_text(
        "function check() {\n"
        "  return client.callTool(\"aiworkhub_dashboard_health\", {});\n"
        "}\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    assert _edges(repo, "aiworkhub_dashboard_health") == {"server.py.health_view"}


def test_const_and_property_call_tool_bind(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    (repo / "server.py").write_text(
        "def health_view():\n"
        "    return 1\n"
        "def snapshot_view():\n"
        "    return 2\n"
        "HEALTH = \"aiworkhub_dashboard_health\"\n"
        "READONLY_TOOLS: dict[str, object] = {HEALTH: health_view}\n"
        "SNAPSHOT_TOOLS = {\n"
        "    \"aiworkhub_dashboard_snapshot\": snapshot_view,\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "app.js").write_text(
        "const DASHBOARD_TOOLS = Object.freeze({\n"
        "  health: \"aiworkhub_dashboard_health\",\n"
        "  snapshot: \"aiworkhub_dashboard_snapshot\",\n"
        "});\n"
        "function check() {\n"
        "  return client.callTool(DASHBOARD_TOOLS.health, {});\n"
        "}\n"
        "function load() {\n"
        "  return client.callTool(\n"
        "    DASHBOARD_TOOLS.snapshot,\n"
        "  );\n"
        "}\n"
        "function raw() {\n"
        "  return send(\"tools/call\", { name: DASHBOARD_TOOLS.health });\n"
        "}\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    assert _edges(repo, "aiworkhub_dashboard_health") == {"server.py.health_view"}
    assert _edges(repo, "aiworkhub_dashboard_snapshot") == {"server.py.snapshot_view"}


def test_unregistered_and_ambiguous_tool_names_stay_unbound(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    (repo / "server.py").write_text(
        "def health_view():\n"
        "    return 1\n"
        "def other_view():\n"
        "    return 2\n"
        "READONLY_TOOLS = {\n"
        "    \"aiworkhub_dashboard_health\": health_view,\n"
        "}\n"
        "OTHER_TOOLS = {\n"
        "    \"aiworkhub_dashboard_health\": other_view,\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "app.js").write_text(
        "function check() {\n"
        "  client.callTool(\"aiworkhub_dashboard_health\", {});\n"
        "  return client.callTool(\"aiworkhub_dashboard_missing\", {});\n"
        "}\n"
        "function render() {\n"
        "  return document.createElement(\"div\");\n"
        "}\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    assert _edges(repo, "aiworkhub_dashboard_health") == set()
    assert _edges(repo, "aiworkhub_dashboard_missing") == set()
    assert _edges(repo, "createElement") == set()
