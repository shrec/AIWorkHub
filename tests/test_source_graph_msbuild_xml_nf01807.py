from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.source_graph_languages import language_for_path

MSBUILD_PROJECT_FILES = (
    "Main.vcxproj",
    "Main.vcxproj.filters",
    "Directory.Build.props",
)
MARKER = "NF01807UniqueMsbuildMarker"


@pytest.mark.parametrize(
    "name",
    ["Main.vcxproj", "Main.vcxproj.filters", "Directory.Build.props", "x.targets", "App.csproj"],
)
def test_msbuild_project_suffixes_are_indexed_as_xml(name: str) -> None:
    assert language_for_path(Path("a") / name) == "xml"


def test_iter_source_files_admits_msbuild_projects_and_skips_unindexed(tmp_path: Path) -> None:
    for name in MSBUILD_PROJECT_FILES:
        (tmp_path / name).write_text("<Project />\n", encoding="utf-8")
    (tmp_path / "notes.bin").write_bytes(b"\x00\x01")

    admitted = {path.name for path in sg.iter_source_files(tmp_path)}

    assert set(MSBUILD_PROJECT_FILES) <= admitted
    assert "notes.bin" not in admitted


def test_bodygrep_finds_literal_unique_to_vcxproj_after_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="msbuild_xml_nf01807")
    (repo / "Main.vcxproj").write_text(
        f"<Project>\n  <!-- {MARKER} -->\n</Project>\n", encoding="utf-8",
    )
    (repo / "Directory.Build.props").write_text("<Project />\n", encoding="utf-8")

    sg.build_index(repo, incremental=False)
    payload = sg.bodygrep_query(repo, MARKER)

    assert {row["file_path"] for row in payload["matches"]} == {"Main.vcxproj"}
