from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOCIAL_PREVIEW = ROOT / "docs" / "assets" / "social-preview.svg"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
BYTE_BUDGET = 20480
FORBIDDEN_ELEMENTS = frozenset({"script", "foreignObject", "image", "style"})
URL_REFERENCE = re.compile(r"url\(\s*[\"']?([^\"')\s]*)", re.IGNORECASE)


def _local_name(qualified: str) -> str:
    return qualified.rpartition("}")[2]


def _svg(tag: str) -> str:
    return f"{{{SVG_NAMESPACE}}}{tag}"


def _parse() -> ET.Element:
    return ET.parse(SOCIAL_PREVIEW).getroot()


def test_social_preview_is_1280_by_640() -> None:
    root = _parse()
    assert root.tag == _svg("svg")
    assert root.get("width") == "1280"
    assert root.get("height") == "640"
    assert root.get("viewBox") == "0 0 1280 640"


def test_social_preview_has_no_external_references_or_scripts() -> None:
    for element in _parse().iter():
        tag = _local_name(element.tag)
        assert tag not in FORBIDDEN_ELEMENTS, f"<{tag}> is not allowed in the card"
        for qualified, value in element.attrib.items():
            name = _local_name(qualified)
            assert not name.lower().startswith("on"), f"<{tag}> event handler {name}"
            if name == "href":
                assert value.startswith("#"), f"<{tag}> href leaves the card: {value!r}"
            for target in URL_REFERENCE.findall(value):
                assert target.startswith("#"), f"<{tag}> {name} leaves the card: {target!r}"


def test_social_preview_is_labelled_and_names_the_project() -> None:
    root = _parse()
    title = root.find(_svg("title"))
    description = root.find(_svg("desc"))
    assert root.get("role") == "img"
    assert title is not None and "AIWorkHub" in (title.text or "")
    assert description is not None and (description.text or "").strip()


def test_social_preview_stays_under_byte_budget() -> None:
    assert SOCIAL_PREVIEW.stat().st_size < BYTE_BUDGET
