"""Regression checks for NF-2026-01402: Python signatures keep their full arity.

``_sig_of`` used to render ``def name(a, b)`` from positional parameter names
only, so defaults, ``/``, ``*args``, keyword-only parameters, ``**kwargs`` and
annotations vanished and reviewers read optional parameters as required ones.
Every test isolates state under its own ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph_ast as sgast


def _signatures(tmp_path: Path, source: str) -> dict[str, str]:
    target = tmp_path / "pkg" / "sigs.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    extraction = sgast.extract_file(tmp_path, target, build_revision="test-rev")
    assert extraction.status == "ok"
    return {
        e.name: e.signature
        for e in extraction.entities
        if e.kind in {"function", "method", "class"}
    }


def test_defaulted_annotated_parameter_and_return_annotation(tmp_path):
    sigs = _signatures(tmp_path, (
        "from typing import Any\n"
        "def _current_chat_provider(card: dict[str, Any] | None = None) -> str:\n"
        "    return ''\n"
    ))
    assert sigs["_current_chat_provider"] == (
        "def _current_chat_provider(card: dict[str, Any] | None=None) -> str"
    )


def test_positional_only_varargs_keyword_only_and_kwargs(tmp_path):
    sigs = _signatures(tmp_path, (
        "def g(a, b=1, /, c=2, *args, d, e: int = 3, **kwargs) -> None:\n"
        "    pass\n"
        "def h(x, *, y):\n"
        "    pass\n"
    ))
    assert sigs["g"] == "def g(a, b=1, /, c=2, *args, d, e: int=3, **kwargs) -> None"
    assert sigs["h"] == "def h(x, *, y)"


def test_async_def_signature(tmp_path):
    sigs = _signatures(tmp_path, (
        "async def fetch(url: str, *, timeout: float = 1.5) -> bytes:\n"
        "    return b''\n"
    ))
    assert sigs["fetch"] == "async def fetch(url: str, *, timeout: float=1.5) -> bytes"


def test_class_signature_includes_bases_and_keywords(tmp_path):
    sigs = _signatures(tmp_path, (
        "from abc import ABCMeta\n"
        "class Base:\n"
        "    pass\n"
        "class Plugin(Base, metaclass=ABCMeta):\n"
        "    def run(self, *args, **kwargs):\n"
        "        pass\n"
    ))
    assert sigs["Base"] == "class Base"
    assert sigs["Plugin"] == "class Plugin(Base, metaclass=ABCMeta)"
    assert sigs["run"] == "def run(self, *args, **kwargs)"


def test_over_long_signature_is_cut_and_visibly_incomplete(tmp_path):
    params = ", ".join(f"parameter_{i:03d}: int = {i}" for i in range(60))
    sigs = _signatures(tmp_path, f"def wide({params}) -> None:\n    pass\n")
    signature = sigs["wide"]
    assert len(signature) == 500
    assert signature.endswith("...")
    assert signature.startswith("def wide(parameter_000: int=0, ")
    assert "-> None" not in signature


def test_python_extractor_generation_is_bumped():
    assert sgast.EXTRACTOR_ID.endswith(".v2")
    assert sgast.expected_extractor_ids(Path("x.py")) == frozenset({sgast.EXTRACTOR_ID})
