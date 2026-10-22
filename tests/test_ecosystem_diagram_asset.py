from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ECOSYSTEM = ROOT / "site" / "assets" / "aiworkhub-ecosystem.svg"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
NAMESPACES = {"svg": SVG_NAMESPACE}
BYTE_BUDGET = 120_000
FORBIDDEN_ELEMENTS = frozenset({"script", "foreignObject", "image", "style"})
NON_LOCAL_URL = re.compile(r"url\((?!\s*[\"']?#)", re.IGNORECASE)
PANEL_TITLES = (
    "MANAGER SEAT",
    "MULTI-MODEL WORKFORCE",
    "MEMORY LAYER",
    "SOURCE GRAPH",
    "QUALITY GATES",
    "NEEDFIX LOOP",
)
SPINE_NODES = (
    "TASK MCP CONTROL PLANE",
    "OWNER",
    "CARD",
    "WORKER",
    "GATES",
    "REVIEW",
    "ACCEPT",
    "CANONICAL TREE",
    "LEARNING COMMIT",
)


def _root() -> ET.Element:
    return ET.parse(ECOSYSTEM).getroot()


def test_ecosystem_poster_is_1920_by_1200() -> None:
    root = _root()
    assert root.tag == f"{{{SVG_NAMESPACE}}}svg"
    assert root.get("width") == "1920"
    assert root.get("height") == "1200"
    assert root.get("viewBox") == "0 0 1920 1200"


def test_ecosystem_poster_is_labelled_and_names_the_project() -> None:
    root = _root()
    title = root.find("svg:title", NAMESPACES)
    description = root.find("svg:desc", NAMESPACES)
    assert root.get("role") == "img"
    assert title is not None and "AIWorkHub" in (title.text or "")
    assert description is not None and (description.text or "").strip()


def test_ecosystem_poster_has_no_external_references_or_scripts() -> None:
    for element in _root().iter():
        tag = element.tag.rsplit("}", 1)[-1]
        assert tag not in FORBIDDEN_ELEMENTS, f"<{tag}> is not allowed in the poster"
        for qualified, value in element.attrib.items():
            name = qualified.rsplit("}", 1)[-1]
            assert not name.lower().startswith("on"), f"<{tag}> event handler {name}"
            if name == "href":
                assert value.startswith("#"), f"<{tag}> href leaves the poster: {value!r}"
    raw = ECOSYSTEM.read_text(encoding="utf-8")
    assert NON_LOCAL_URL.search(raw) is None
    assert "@import" not in raw
    assert "://" not in raw.replace(SVG_NAMESPACE, "").replace("http://www.w3.org/1999/xlink", "")


def test_ecosystem_poster_stays_under_byte_budget() -> None:
    assert ECOSYSTEM.stat().st_size <= BYTE_BUDGET


def test_ecosystem_poster_shows_every_panel_and_spine_node() -> None:
    text = " ".join(_root().itertext())
    for label in PANEL_TITLES + SPINE_NODES:
        assert label in text, f"missing {label!r}"
