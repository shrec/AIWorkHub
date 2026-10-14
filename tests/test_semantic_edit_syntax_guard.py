from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aiworkhub import semantic_edit, semantic_edit_applier


def _write(root: Path, name: str, text: str) -> Path:
    target = root / name
    target.write_text(text, encoding="utf-8")
    return target


def _prepare(root: Path, name: str, start: int, end: int):
    return semantic_edit.prepare_line_target(
        root,
        path=name,
        start_line=start,
        end_line=end,
        allowed_writes=[name],
    )


def test_python_dropped_except_header_is_refused_and_bytes_unchanged(tmp_path: Path):
    before = "try:\n    x = 1\nexcept Exception:\n    y = 2\n"
    _write(tmp_path, "x.py", before)
    target = _prepare(tmp_path, "x.py", 3, 3)
    before_bytes = (tmp_path / "x.py").read_bytes()
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        semantic_edit_applier.replace_prepared_range(
            tmp_path, target, "    y = 99\n", allowed_writes=["x.py"]
        )
    assert "semantic_edit_syntax_regression" in str(excinfo.value)
    assert (tmp_path / "x.py").read_bytes() == before_bytes


def test_valid_python_replacement_applies(tmp_path: Path):
    before = "try:\n    x = 1\nexcept Exception:\n    y = 2\n"
    _write(tmp_path, "x.py", before)
    target = _prepare(tmp_path, "x.py", 2, 2)
    next_text, _metrics = semantic_edit_applier.replace_prepared_range(
        tmp_path, target, "    x = 2\n", allowed_writes=["x.py"]
    )
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == (
        "try:\n    x = 2\nexcept Exception:\n    y = 2\n"
    )
    compile(next_text, "x.py", "exec", dont_inherit=True)


def test_invalid_preimage_still_applies(tmp_path: Path):
    before = "def f(:\n    pass\n"
    _write(tmp_path, "x.py", before)
    target = _prepare(tmp_path, "x.py", 1, 1)
    next_text, _metrics = semantic_edit_applier.replace_prepared_range(
        tmp_path, target, "def g():\n", allowed_writes=["x.py"]
    )
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "def g():\n    pass\n"


def test_js_guard_disabled_without_node(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = semantic_edit.syntax_regression(
        "foo.js", "async function f() {\n}\n", "broken\n{\n"
    )
    assert result == ""


def test_js_dropped_function_header_is_flagged_when_node_available():
    if shutil.which("node") is None:
        pytest.skip("node not available")
    before = "async function f() {\n  await g();\n}\n"
    after = "  await g();\n}\n"
    result = semantic_edit.syntax_regression("foo.js", before, after)
    assert result.startswith("semantic_edit_syntax_regression")
