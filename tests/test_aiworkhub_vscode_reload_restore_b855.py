"""B855: VS Code panel-reload/restore, static-source-assertion test.

``extension.js`` is Node/VS Code API code with no test runner wired here
beyond ``node --check`` (a real VS Code host is not available in this
sandbox). This test reads the extension source directly and asserts, at the
source level, that:

  * ``vscode.window.registerWebviewPanelSerializer`` is registered for
    ``PANEL_VIEW_TYPE``;
  * its ``deserializeWebviewPanel`` calls ``reviveDashboardPanel``, and
    ``reviveDashboardPanel`` itself calls ``applyWebviewOptions``,
    ``getHtmlForWebview``, and ``getMcpClient``, then ends with a
    ``pushSnapshot`` call so a deserialized tab reaches Live without the user
    closing/reopening it;
  * a stale controller (the previous panel's ``ViewState``) is disposed
    before the new one is adopted, so repeated deserialize/open cycles leave
    exactly one live panel controller.
"""

from __future__ import annotations

import re
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_EXT_PATH = _TOOL_ROOT / "vscode-extension" / "extension.js"
_EXT_SOURCE = _EXT_PATH.read_text(encoding="utf-8")


def _function_body(source: str, signature_pattern: str, *, max_len: int = 4000) -> str:
    """Return a bounded slice of source starting at the first match of
    ``signature_pattern`` -- good enough for source-level substring
    assertions without a full JS parser."""
    match = re.search(signature_pattern, source)
    assert match, f"pattern not found: {signature_pattern!r}"
    return source[match.start() : match.start() + max_len]


def test_panel_serializer_is_registered_for_dashboard_view_type() -> None:
    assert "registerWebviewPanelSerializer(PANEL_VIEW_TYPE" in _EXT_SOURCE


def test_deserialize_calls_revive_dashboard_panel() -> None:
    serializer_block = _function_body(
        _EXT_SOURCE, r"registerWebviewPanelSerializer\(PANEL_VIEW_TYPE", max_len=900
    )
    assert "deserializeWebviewPanel" in serializer_block
    assert "reviveDashboardPanel(webviewPanel, context.extensionUri, context)" in serializer_block


def test_revive_dashboard_panel_rewires_full_controller() -> None:
    revive_block = _function_body(_EXT_SOURCE, r"function reviveDashboardPanel\(")
    assert "applyWebviewOptions(" in revive_block
    assert "getHtmlForWebview(" in revive_block
    assert "getMcpClient(" in revive_block
    assert "pushSnapshot(" in revive_block
    # A fresh ViewState/McpStdioClient binding is created for the revived
    # panel -- never reused from a possibly-stale prior controller.
    assert "new ViewState(" in revive_block
    assert "view.bindClient(client)" in revive_block


def test_revive_dashboard_panel_disposes_stale_controller_first() -> None:
    """Exactly one live panel controller must survive repeated
    deserialize/open cycles: a stale previous panel's ViewState (poll timer +
    McpStdioClient binding) must be disposed before the new one is adopted."""
    revive_block = _function_body(_EXT_SOURCE, r"function reviveDashboardPanel\(", max_len=800)
    dispose_index = revive_block.find(".dispose(")
    panel_assignment_index = revive_block.find("panel = ownedPanel;")
    assert dispose_index != -1, "reviveDashboardPanel must dispose a stale controller"
    assert panel_assignment_index != -1
    assert dispose_index < panel_assignment_index, (
        "the stale controller must be disposed BEFORE `panel` is reassigned to the "
        "newly deserialized webviewPanel, or the old controller's reference is lost"
    )


def test_open_dashboard_command_still_reveals_single_panel() -> None:
    """The command-driven open path (distinct from the reload/deserialize
    path) still guards against a second panel: if one already exists it is
    revealed, never duplicated."""
    open_block = _function_body(_EXT_SOURCE, r"async function openDashboardCommand\(", max_len=600)
    assert "if (panel)" in open_block
    assert "panel.reveal(" in open_block


def test_panel_module_variable_is_singular() -> None:
    """There is exactly one module-level `panel` reference the serializer,
    the open command, and pushSnapshot/refresh all share -- never a
    parallel/duplicate panel-tracking variable."""
    assert _EXT_SOURCE.count("let panel = null;") == 1
