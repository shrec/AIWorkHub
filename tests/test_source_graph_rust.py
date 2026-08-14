"""Bounded truthful Rust lexical Source Graph regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph_ast as sgast
from aiworkhub import source_graph_languages as languages


def _extract(tmp_path: Path, source: str) -> sgast.FileExtraction:
    target = tmp_path / "src" / "lib.rs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return sgast.extract_file(tmp_path, target, build_revision="rust-test")


@pytest.mark.parametrize(
    ("statement", "targets"),
    [
        (
            "use std::collections::{HashMap, HashSet};",
            {"std::collections::HashMap", "std::collections::HashSet"},
        ),
        (
            "use crate::{outer::{A, B}, C};",
            {"crate::outer::A", "crate::outer::B", "crate::C"},
        ),
        (
            "use crate::{outer::{self, *}, C};",
            {"crate::outer", "crate::outer::*", "crate::C"},
        ),
        (
            "use std::{a::{b::{c::{D, E}}, F}};",
            {"std::a::b::c::D", "std::a::b::c::E", "std::a::F"},
        ),
    ],
)
def test_rust_use_trees_expand_to_exact_leaves(
    tmp_path: Path, statement: str, targets: set[str],
) -> None:
    extraction = _extract(tmp_path, f"{statement}\n")
    imports = {edge.dst_name for edge in extraction.edges if edge.kind == "imports"}
    assert imports == targets
    assert all("{" not in target and "}" not in target for target in imports)


def test_rust_use_visibility_and_aliases_are_observed(tmp_path: Path) -> None:
    extraction = _extract(
        tmp_path,
        "pub use std::io::Result;\n"
        "pub(crate) use std::fmt::{Display as Show, Debug};\n",
    )
    imports = {edge.dst_name for edge in extraction.edges if edge.kind == "imports"}
    assert imports == {
        "std::io::Result", "std::fmt::Display as Show", "std::fmt::Debug",
    }
    assert all(
        edge.evidence_label == sgast.EXTRACTED
        and edge.extractor == sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID
        for edge in extraction.edges if edge.kind == "imports"
    )


def test_rust_lexical_authority_and_new_function_remain_truthful(tmp_path: Path) -> None:
    extraction = _extract(
        tmp_path,
        "struct Engine {}\n"
        "impl Engine {\n"
        "    fn new() -> Self { Engine {} }\n"
        "}\n"
        "fn run() { external_call(); }\n",
    )
    assert any(entity.name == "new" for entity in extraction.entities)
    external = next(
        edge for edge in extraction.edges
        if edge.kind == "calls" and edge.dst_name == "external_call"
    )
    assert external.dst_qualname is None
    assert external.evidence_label == sgast.INFERRED
    rust = {row["id"]: row for row in languages.public_registry()}["rust"]
    assert rust["active_capability"] == "semantic_lexical"
    assert sgast.expected_extractor_ids(tmp_path / "lib.rs") == frozenset({
        sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID,
    })


def test_malformed_rust_use_tree_fails_closed(tmp_path: Path) -> None:
    extraction = _extract(tmp_path, "use crate::{outer::{A, B};\n")
    assert not [edge for edge in extraction.edges if edge.kind == "imports"]
