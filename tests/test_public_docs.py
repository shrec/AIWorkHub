from pathlib import Path

from scripts import check_public_docs


ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path: Path, readme: str) -> None:
    for document in check_public_docs.PUBLIC_DOCS:
        target = tmp_path / document.relative_to(check_public_docs.ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Document\n")
    (tmp_path / "README.md").write_text(readme)


def test_live_public_docs_have_valid_local_links_and_no_internal_leaks() -> None:
    assert check_public_docs.check(ROOT) == []


def test_broken_local_link_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path, "# Product\n\n[Missing](docs/nope.md)\n")
    assert check_public_docs.check(tmp_path) == [
        "README.md: broken local link 'docs/nope.md'"
    ]


def test_legacy_paths_task_ids_and_wrong_tool_name_are_rejected(tmp_path: Path) -> None:
    _fixture(
        tmp_path,
        "# Product\n\nB416 `AITools/taskctl.py` aiworkhub_task_completion_inbox\n",
    )
    errors = check_public_docs.check(tmp_path)
    assert len(errors) == 3
    assert all(error.startswith("README.md: contains ") for error in errors)
