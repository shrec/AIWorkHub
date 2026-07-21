"""Focused dark-theme regression test for Task MCP dashboard (B417).

Proves:
- HTML <meta name="color-scheme" content="dark">
- CSS :root { color-scheme: dark; } with dark palette variables
- Old white surface (#ffffff as --surface) is absent from the CSS
- Required element IDs are present and unchanged

Runs as plain Python (no pytest dependency) so it works in
isolated worktrees without pip-installed test frameworks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


_STATIC = Path(__file__).resolve().parents[1] / "src" / "aiworkhub" / "dashboard_static"
_HTML_PATH = _STATIC / "index.html"
_CSS_PATH = _STATIC / "dashboard.css"

_REQUIRED_IDS = frozenset([
    "task-table", "task-table-body", "status-filters", "task-search",
    "topic-filter", "runner-filter", "sort-order", "filtered-count",
    "detail-content", "detail-objective", "detail-metadata", "detail-result",
    "detail-validation", "detail-writes", "detail-actions", "detail-status",
    "detail-loading", "detail-error", "detail-empty",
    "metric-active", "metric-pending", "metric-processing", "metric-review",
    "metric-blocked", "metric-finished", "metric-stale",
    "metric-tokens", "metric-cost",
    "connection-state", "connection-label", "last-sync",
    "auto-refresh", "refresh-interval", "refresh-button",
    "table-loading", "table-empty",
    "source-alert", "source-alert-title", "source-alert-message",
    "toast",
    "topic-stats", "runner-stats", "usage-list",
    "return-list", "run-list", "warning-list",
    "tab-topics", "tab-runners", "tab-usage", "tab-returns", "tab-runs", "tab-warnings",
    "panel-topics", "panel-runners", "panel-usage", "panel-returns", "panel-runs", "panel-warnings",
])

OK = 0
FAIL = 0
ERROR = 0


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _hex_lightness(hex_color: str) -> float:
    """0-255 sRGB relative luminance approximation."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def run(name: str, fn):
    global OK, FAIL, ERROR
    try:
        fn()
        OK += 1
        print(f"PASS  {name}")
    except AssertionError as e:
        FAIL += 1
        print(f"FAIL  {name}: {e}")
    except Exception as e:
        ERROR += 1
        print(f"ERROR {name}: {e}")


# ── Tests ────────────────────────────────────────────────────────────────────

def test_html_declares_dark_color_scheme():
    html = _read(_HTML_PATH)
    match = re.search(
        r'<meta\s[^>]*name\s*=\s*["\']color-scheme["\'][^>]*content\s*=\s*["\']dark["\'][^>]*/?>',
        html,
    ) or re.search(
        r'<meta\s[^>]*content\s*=\s*["\']dark["\'][^>]*name\s*=\s*["\']color-scheme["\'][^>]*/?>',
        html,
    )
    assert match is not None, "index.html must have <meta name='color-scheme' content='dark'>"
    assert 'content="light"' not in html and "content='light'" not in html


def test_css_root_declares_dark_color_scheme():
    css = _read(_CSS_PATH)
    root_match = re.search(r':root\s*\{([^}]+)\}', css, re.DOTALL)
    assert root_match is not None, "CSS must have a :root block"
    root_body = root_match.group(1)
    m = re.search(r'color-scheme\s*:\s*([^;]+);', root_body)
    assert m is not None, "color-scheme property not found in :root"
    assert m.group(1).strip() == "dark", f"Expected color-scheme: dark, got: {m.group(1).strip()}"


def test_css_root_has_dark_palette_variables():
    css = _read(_CSS_PATH)
    root_match = re.search(r':root\s*\{([^}]+)\}', css, re.DOTALL)
    assert root_match is not None
    root_body = root_match.group(1)

    def var_value(name: str) -> str | None:
        m = re.search(rf'--{name}\s*:\s*([^;]+);', root_body)
        return m.group(1).strip() if m else None

    canvas = var_value("canvas")
    surface = var_value("surface")
    ink = var_value("ink")

    assert canvas is not None, "--canvas: is required"
    assert surface is not None, "--surface: is required"
    assert ink is not None, "--ink: is required"

    canvas_l = _hex_lightness(canvas)
    surface_l = _hex_lightness(surface)
    ink_l = _hex_lightness(ink)

    assert canvas_l < 60, f"--canvas {canvas} too bright (L={canvas_l:.0f})"
    assert surface_l < 60, f"--surface {surface} too bright (L={surface_l:.0f})"
    assert ink_l > 160, f"--ink {ink} too dark (L={ink_l:.0f})"


def test_no_white_surface_in_css():
    css = _read(_CSS_PATH)
    root_match = re.search(r':root\s*\{([^}]+)\}', css, re.DOTALL)
    assert root_match is not None
    root_body = root_match.group(1)
    m = re.search(r'--surface\s*:\s*([^;]+);', root_body)
    assert m is not None
    assert m.group(1).strip() != "#ffffff", f"Old white --surface: #ffffff still present"
    m2 = re.search(r'--canvas\s*:\s*([^;]+);', root_body)
    assert m2 is not None
    assert m2.group(1).strip() != "#f3f5f6", f"Old light --canvas: #f3f5f6 still present"


def test_all_required_element_ids_present():
    html = _read(_HTML_PATH)
    found_ids: set[str] = set()
    for m in re.finditer(r'\bid\s*=\s*["\']([^"\']+)["\']', html):
        found_ids.add(m.group(1))
    missing = _REQUIRED_IDS - found_ids
    assert not missing, f"Missing required IDs: {sorted(missing)}"


def test_status_filter_values_preserved():
    html = _read(_HTML_PATH)
    expected = {"all", "pending", "processing", "review", "blocked", "finished", "archived", "stale"}
    found = set(re.findall(r'data-status\s*=\s*["\']([^"\']+)["\']', html))
    assert found >= expected, f"Missing filter values: {expected - found}"


def test_summary_strip_status_classes_preserved():
    html = _read(_HTML_PATH)
    expected = {"status-all", "status-pending", "status-processing", "status-review",
                "status-blocked", "status-finished", "status-stale", "usage-total"}
    found = set()
    for m in re.finditer(r'class\s*=\s*["\']([^"\']+)["\']', html):
        found.update(cls.strip() for cls in m.group(1).split())
    assert expected <= found, f"Missing classes: {expected - found}"


if __name__ == "__main__":
    run("test_html_declares_dark_color_scheme", test_html_declares_dark_color_scheme)
    run("test_css_root_declares_dark_color_scheme", test_css_root_declares_dark_color_scheme)
    run("test_css_root_has_dark_palette_variables", test_css_root_has_dark_palette_variables)
    run("test_no_white_surface_in_css", test_no_white_surface_in_css)
    run("test_all_required_element_ids_present", test_all_required_element_ids_present)
    run("test_status_filter_values_preserved", test_status_filter_values_preserved)
    run("test_summary_strip_status_classes_preserved", test_summary_strip_status_classes_preserved)
    print(f"\n────\n{OK} passed, {FAIL} failed, {ERROR} errors")
    sys.exit(0 if FAIL == 0 and ERROR == 0 else 1)
