from pathlib import Path

from aiworkhub.source_graph import SOURCE_GRAPH_MODES


DOC_PATHS = (Path("README.md"), Path("docs/SOURCE_GRAPH.md"))
EXACT_TARGET_MODES = ("file", "function", "class", "body", "bodygrep", "deps")


def test_source_graph_mode_count_is_canonical() -> None:
    assert len(SOURCE_GRAPH_MODES) == 37


def test_source_graph_documentation_covers_count_and_exact_target_modes() -> None:
    exact_target_modes = set(EXACT_TARGET_MODES)
    assert exact_target_modes.issubset(SOURCE_GRAPH_MODES)

    for doc_path in DOC_PATHS:
        text = doc_path.read_text(encoding="utf-8")
        assert "exactly 37" in text
        for mode in exact_target_modes:
            assert f"`{mode}`" in text, f"{doc_path} does not document {mode!r}"
