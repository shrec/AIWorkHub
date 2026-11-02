"""NF-2026-02000: builtin structural check for changed MSBuild project files.

A worker edit dropped a ``ResourceCompile`` item and duplicated ``ClInclude``
entries in a ``.vcxproj`` and the builtin gate passed it. Every test drives
``run_builtin_static_checks`` itself, against a real git base where needed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aiworkhub import msbuild_project_check, quality_evidence

NS = "http://schemas.microsoft.com/developer/msbuild/2003"


def _project(*groups: str) -> str:
    body = "".join(f"  <ItemGroup>\n{group}  </ItemGroup>\n" for group in groups)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<Project DefaultTargets="Build" xmlns="{NS}">\n{body}</Project>\n'
    )


BASE = _project(
    '    <ClCompile Include="Src\\A.cpp" />\n    <ClCompile Include="src\\b.cpp" />\n',
    '    <ClInclude Include="inc\\a.h" />\n',
    '    <ResourceCompile Include="app.rc" />\n',
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, timeout=60,
    )


def _write(root: Path, relative: str, text: str) -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return relative


def _committed(root: Path, relative: str, text: str) -> str:
    if shutil.which("git") is None:
        pytest.skip("git executable not available")
    _git(root, "init", "-q")
    _write(root, relative, text)
    _git(root, "add", relative)
    _git(root, "commit", "-q", "-m", "base")
    return relative


def _check(root: Path, relative: str) -> quality_evidence.EvidenceCheck:
    checks = quality_evidence.run_builtin_static_checks(root, changed_paths=[relative])
    return next(row for row in checks if row.check_id == f"builtin:syntax:{relative}")


def test_dropped_resource_compile_fails_naming_type_and_include(tmp_path):
    rel = _committed(tmp_path, "app/App.vcxproj", BASE)
    _write(tmp_path, rel, BASE.replace('    <ResourceCompile Include="app.rc" />\n', ""))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_FAILED
    assert "ResourceCompile" in check.summary
    assert "app.rc" in check.summary
    assert "missing_project_item" in check.error
    assert check.provenance == "builtin:diff_scoped_syntax"


def test_duplicate_cl_include_fails_naming_duplicate(tmp_path):
    rel = _committed(tmp_path, "App.vcxproj", BASE)
    _write(tmp_path, rel, BASE.replace(
        '    <ClInclude Include="inc\\a.h" />\n',
        '    <ClInclude Include="inc\\a.h" />\n    <ClInclude Include="INC/A.H" />\n',
    ))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_FAILED
    assert "duplicate items" in check.summary
    assert "ClInclude" in check.summary and "inc\\\\a.h" in check.summary
    assert check.error == "duplicate_project_item"


def test_same_include_under_two_item_types_is_not_duplicate(tmp_path):
    rel = _write(tmp_path, "App.vcxproj.filters", _project(
        '    <ClInclude Include="a.h" />\n    <None Include="a.h" />\n'
    ))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_PASSED, check.summary


def test_malformed_project_fails_not_well_formed(tmp_path):
    rel = _write(tmp_path, "Lib.csproj", "<Project><ItemGroup><Compile Include='a.cs'></Project>")
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_FAILED
    assert "not well-formed XML" in check.error


def test_entity_declarations_are_refused(tmp_path):
    rel = _write(
        tmp_path, "x.props",
        '<!DOCTYPE p [<!ENTITY a "aaaa">]><Project><ItemGroup /></Project>',
    )
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_FAILED
    assert "not well-formed XML" in check.error


def test_new_project_file_checks_only_wellformed_and_duplicates(tmp_path):
    _committed(tmp_path, "Other.vcxproj", BASE)
    rel = _write(tmp_path, "New.vcxproj", _project('    <ClCompile Include="only.cpp" />\n'))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert "base: none" in check.summary
    dup = _write(tmp_path, "Dup.vbproj", _project(
        '    <Compile Include="m.vb" />\n    <Compile Include="M.vb" />\n'
    ))
    assert _check(tmp_path, dup).status == quality_evidence.STATUS_FAILED


def test_adding_and_reordering_items_passes(tmp_path):
    rel = _committed(tmp_path, "App.vcxproj", BASE)
    reordered = _project(
        '    <ResourceCompile Include="app.rc" />\n',
        '    <ClInclude Include="inc\\a.h" />\n    <ClInclude Include="inc\\new.h" />\n',
        '    <ClCompile Include="src\\b.cpp" />\n    <ClCompile Include="Src\\A.cpp" />\n',
    )
    _write(tmp_path, rel, reordered)
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert "base: HEAD" in check.summary


def test_include_compare_ignores_case_and_slash_direction(tmp_path):
    rel = _committed(tmp_path, "App.vcxproj", BASE)
    _write(tmp_path, rel, BASE.replace('Include="Src\\A.cpp"', 'Include="src/a.cpp"'))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert msbuild_project_check.normalize_include("Src\\A.cpp") == (
        msbuild_project_check.normalize_include("src/a.cpp")
    )


def test_semicolon_list_include_expands_to_items(tmp_path):
    rel = _committed(tmp_path, "Lib.fsproj", _project('    <Compile Include="a.fs;b.fs" />\n'))
    _write(tmp_path, rel, _project('    <Compile Include="a.fs" />\n'))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_FAILED
    assert "b.fs" in check.summary


def test_unchanged_and_non_project_paths_are_not_parsed(tmp_path, monkeypatch):
    calls: list[str] = []
    original = msbuild_project_check.check_project_file

    def recording(root, relative, **kwargs):
        calls.append(relative)
        return original(root, relative, **kwargs)

    monkeypatch.setattr(msbuild_project_check, "check_project_file", recording)
    _write(tmp_path, "Untouched.vcxproj", "<not xml")
    readme = _write(tmp_path, "README.md", "<not xml")
    checks = quality_evidence.run_builtin_static_checks(tmp_path, changed_paths=[readme])
    assert calls == []
    assert not any("Untouched.vcxproj" in row.check_id for row in checks)


@pytest.mark.parametrize(
    "name",
    ["a.vcxproj", "a.VCXPROJ.filters", "a.csproj", "a.vbproj", "a.fsproj", "a.props", "a.targets"],
)
def test_project_suffixes_are_recognized(name):
    assert msbuild_project_check.is_project_path(f"dir/{name}")


@pytest.mark.parametrize("name", ["a.filters", "a.xml", "a.vcxproj.user", "props"])
def test_other_suffixes_are_not_project_paths(name):
    assert not msbuild_project_check.is_project_path(name)


def _raw_project(body: str) -> str:
    return f'<?xml version="1.0" encoding="utf-8"?>\n<Project xmlns="{NS}">\n{body}</Project>\n'


def test_same_include_with_different_item_conditions_is_not_duplicate(tmp_path):
    rel = _write(tmp_path, "App.vcxproj", _project(
        "    <ClCompile Include=\"a.cpp\" Condition=\"'$(Configuration)|$(Platform)'=='Debug|x64'\" />\n"
        "    <ClCompile Include=\"a.cpp\" Condition=\"'$(Configuration)|$(Platform)'=='Release|x64'\" />\n"
    ))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_PASSED, check.summary


def test_same_include_in_item_groups_with_different_conditions_is_not_duplicate(tmp_path):
    rel = _write(tmp_path, "App.vcxproj", _raw_project(
        "  <ItemGroup Condition=\"'$(Configuration)'=='Debug'\">\n"
        '    <ClCompile Include="a.cpp" />\n  </ItemGroup>\n'
        "  <ItemGroup Condition=\"'$(Configuration)'=='Release'\">\n"
        '    <ClCompile Include="a.cpp" />\n  </ItemGroup>\n'
    ))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_PASSED, check.summary


def test_same_include_in_when_and_otherwise_is_not_duplicate(tmp_path):
    rel = _write(tmp_path, "Lib.csproj", _raw_project(
        "  <Choose>\n    <When Condition=\"'$(X)'=='1'\">\n"
        '      <ItemGroup><Compile Include="a.cs" /></ItemGroup>\n    </When>\n'
        '    <Otherwise>\n      <ItemGroup><Compile Include="a.cs" /></ItemGroup>\n'
        "    </Otherwise>\n  </Choose>\n"
    ))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_PASSED, check.summary


def test_same_include_with_equivalent_condition_is_still_duplicate(tmp_path):
    rel = _write(tmp_path, "App.vcxproj", _project(
        "    <ClCompile Include=\"a.cpp\" Condition=\"'$(Configuration)' == 'Debug'\" />\n"
        "    <ClCompile Include=\"A.CPP\" Condition=\"  '$(CONFIGURATION)'   ==\t'debug' \" />\n"
    ))
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_FAILED
    assert check.error == "duplicate_project_item"
    assert "ClCompile" in check.summary and "a.cpp" in check.summary


def test_unborn_head_repository_has_no_base(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git executable not available")
    _git(tmp_path, "init", "-q")
    rel = _write(tmp_path, "App.vcxproj", BASE)
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert "base: none" in check.summary


def test_unresolvable_head_fails_closed(tmp_path, monkeypatch):
    # Keep git from walking up into an enclosing repository once HEAD is garbage.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    rel = _committed(tmp_path, "App.vcxproj", BASE)
    (tmp_path / ".git" / "HEAD").write_text("garbage-not-a-ref\n", encoding="utf-8")
    check = _check(tmp_path, rel)
    assert check.status == quality_evidence.STATUS_FAILED, check.summary
    assert check.error.startswith("base_unreadable")
