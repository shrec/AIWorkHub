from __future__ import annotations

import re
import subprocess
from pathlib import Path

from aiworkhub import quality_evidence

HEADER = ["# h1", "# h2", "# h3"]
CLASS_LINES = [
    "class Widget:",
    "    def run(self):",
    *[f"        x{n} = {n}" for n in range(1, 8)],
    "",
    "    def stop(self):",
    "        return None",
]
# Widget.run spans lines 5-12 in the baseline; line 10 is inside it.
OLD_LINES = [*HEADER, *CLASS_LINES]
SHIFT = 20


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "--", *files], check=True)
    return root


def _pair(tmp_path: Path, new_lines: list[str], doc: str, old_lines=None):
    old = "\n".join(OLD_LINES if old_lines is None else old_lines) + "\n"
    baseline = tmp_path / "base"
    (baseline / "src").mkdir(parents=True)
    (baseline / "src" / "m.py").write_text(old, encoding="utf-8")
    files = {"src/m.py": "\n".join(new_lines) + "\n", "docs/d.md": doc}
    return baseline, _git_repo(tmp_path / "cand", files)


def _shifted() -> list[str]:
    return [*HEADER, *[f"filler_{n} = {n}" for n in range(SHIFT)], *CLASS_LINES]


def _check(baseline: Path, candidate: Path):
    return quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["src/m.py"]
    )


def test_symbol_citation_to_moved_symbol_yields_no_row(tmp_path):
    baseline, candidate = _pair(tmp_path, _shifted(), "See `src/m.py::Widget.run`.\n")
    assert _check(baseline, candidate) == []


def test_symbol_citation_with_line_inside_current_span_yields_no_row(tmp_path):
    # run now spans 25-32 (5+20 .. 12+20); 28 is inside it.
    baseline, candidate = _pair(
        tmp_path, _shifted(), "See src/m.py::Widget.run:28 and src/m.py::Widget.run:26-30.\n"
    )
    assert _check(baseline, candidate) == []


def test_symbol_citation_with_line_outside_current_span_is_moved(tmp_path):
    baseline, candidate = _pair(tmp_path, _shifted(), "See src/m.py::Widget.run:10.\n")
    rows = _check(baseline, candidate)
    assert len(rows) == 1
    assert rows[0]["status"] == "moved"
    assert rows[0]["replacement"] == "src/m.py::Widget.run"
    assert rows[0]["citation"] == "src/m.py::Widget.run:10"


def test_renamed_symbol_yields_exactly_one_symbol_missing_row(tmp_path):
    renamed = [line.replace("def run(", "def go(") for line in _shifted()]
    baseline, candidate = _pair(tmp_path, renamed, "See src/m.py::Widget.run.\n")
    rows = _check(baseline, candidate)
    assert len(rows) == 1
    assert rows[0]["status"] == quality_evidence.CITATION_DRIFT_STATUS_SYMBOL_MISSING
    assert rows[0]["status"] == "symbol_missing"
    assert rows[0]["replacement"] is None
    assert rows[0]["target_path"] == "src/m.py"


def test_symbol_never_defined_yields_no_row(tmp_path):
    baseline, candidate = _pair(tmp_path, _shifted(), "See src/m.py::Widget.nope.\n")
    assert _check(baseline, candidate) == []


def test_line_citation_inside_def_moves_and_suggests_symbol_form(tmp_path):
    baseline, candidate = _pair(tmp_path, _shifted(), "See src/m.py:10 here.\n")
    rows = _check(baseline, candidate)
    assert len(rows) == 1
    assert rows[0]["status"] == "moved"
    assert rows[0]["replacement"] == "src/m.py:30"
    assert rows[0]["suggested_citation"] == "src/m.py::Widget.run"


def test_line_citation_outside_any_def_suggests_none(tmp_path):
    baseline, candidate = _pair(
        tmp_path,
        [*_shifted(), "TAIL = 1"],
        "See src/m.py:16 here.\n",
        old_lines=[*OLD_LINES, "TAIL = 1"],
    )
    rows = _check(baseline, candidate)
    assert len(rows) == 1
    assert rows[0]["status"] == "moved"
    assert rows[0]["replacement"] == "src/m.py:36"
    assert rows[0]["suggested_citation"] is None


def test_line_citation_to_non_python_target_has_no_suggestion(tmp_path):
    old = "a\nb\nc\n"
    baseline = tmp_path / "base"
    (baseline / "src").mkdir(parents=True)
    (baseline / "src" / "n.txt").write_text(old, encoding="utf-8")
    candidate = _git_repo(
        tmp_path / "cand", {"src/n.txt": "z\n" + old, "docs/d.md": "See src/n.txt:2.\n"}
    )
    rows = quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["src/n.txt"]
    )
    assert len(rows) == 1
    assert "suggested_citation" not in rows[0]


def test_double_colon_citation_never_produces_line_regex_row(tmp_path):
    relative = "src/m.py"
    line_pattern = (
        r"(?<![\w./\\-])" + re.escape(relative) + r":(\d+)(?:-(\d+))?(?!\d)"
    )
    assert re.search(line_pattern, "src/m.py::Widget.run:10") is None
    baseline, candidate = _pair(tmp_path, _shifted(), "See src/m.py::Widget.run:28.\n")
    rows = _check(baseline, candidate)
    assert [row for row in rows if "suggested_citation" in row] == []
    assert rows == []


def test_syntax_error_on_either_side_yields_no_symbol_rows(tmp_path):
    broken = [*_shifted(), "def broken(:"]
    baseline, candidate = _pair(tmp_path, broken, "See src/m.py::Widget.run.\n")
    assert _check(baseline, candidate) == []
    baseline2, candidate2 = _pair(
        tmp_path / "second",
        [line.replace("def run(", "def go(") for line in _shifted()],
        "See src/m.py::Widget.run.\n",
        old_lines=[*OLD_LINES, "def broken(:"],
    )
    assert _check(baseline2, candidate2) == []
