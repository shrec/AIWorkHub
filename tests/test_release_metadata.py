from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "aiworkhub_release_metadata", ROOT / "scripts" / "release_metadata.py"
)
assert SPEC is not None and SPEC.loader is not None
release_metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_metadata)


def _fixture(root: Path, *, canonical: str = "1.2.3", projected: str = "0.0.1") -> None:
    (root / "src" / "aiworkhub").mkdir(parents=True)
    (root / "vscode-extension").mkdir(parents=True)
    (root / "src" / "aiworkhub" / "_version.py").write_text(
        f'__version__ = "{canonical}"\n', encoding="utf-8"
    )
    (root / "vscode-extension" / "package.json").write_text(
        json.dumps({"name": "aiworkhub", "version": projected}) + "\n",
        encoding="utf-8",
    )
    (root / "vscode-extension" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "aiworkhub",
                "version": projected,
                "packages": {"": {"name": "aiworkhub", "version": projected}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "vscode-extension" / "extension.js").write_text(
        f'const EXPECTED_MCP_PACKAGE_VERSION = "{projected}";\n',
        encoding="utf-8",
    )
    (root / "server.json").write_text(
        json.dumps(
            {
                "$schema": "https://example.invalid/server.schema.json",
                "name": "io.github.shrec/aiworkhub",
                "description": "fixture",
                "repository": {"url": "https://example.invalid/repo", "source": "github"},
                "version": projected,
                "packages": [
                    {
                        "registryType": "pypi",
                        "identifier": "aiworkhub",
                        "version": projected,
                        "runtimeHint": "uvx",
                        "transport": {"type": "stdio"},
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{canonical}] - 2026-01-01\n",
        encoding="utf-8",
    )
    # The release documents the extension's own static test gates on. `sync`
    # propagates version LITERALS; this prose is written deliberately, so a
    # releasable fixture already names the canonical version here.
    (root / "vscode-extension" / "README.md").write_text(
        f"# AIWorkHub\n\n## What's new in {canonical} — 2026-01-01\n",
        encoding="utf-8",
    )
    (root / "vscode-extension" / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## {canonical} — 2026-01-01\n", encoding="utf-8"
    )
    # The root README's own release-current heading is gated the same way.
    (root / "README.md").write_text(
        f"# AIWorkHub\n\n## What's new in {canonical} — 2026-01-01\n",
        encoding="utf-8",
    )


def test_live_release_metadata_projections_match_canonical_version() -> None:
    result = release_metadata.check(ROOT)
    canonical = release_metadata.canonical_version(ROOT)
    assert result["ok"] is True
    assert result["canonical_source"] == "src/aiworkhub/_version.py"
    assert result["canonical_version"] == canonical
    assert set(result["projections"].values()) == {canonical}
    extension_manifest = json.loads(
        (ROOT / "vscode-extension" / "package.json").read_text(encoding="utf-8")
    )
    assert extension_manifest["publisher"] == "IvaneChkheidze"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = { attr = "aiworkhub._version.__version__" }' in pyproject


def test_live_server_json_is_the_registry_manifest_at_the_canonical_version() -> None:
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    canonical = release_metadata.canonical_version(ROOT)
    assert server["name"] == "io.github.shrec/aiworkhub"
    assert server["version"] == canonical
    assert len(server["description"]) <= 100
    assert server["repository"] == {
        "url": "https://github.com/shrec/AIWorkHub",
        "source": "github",
    }
    (package,) = server["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == "aiworkhub"
    assert package["version"] == canonical
    assert package["runtimeHint"] == "uvx"
    assert package["transport"] == {"type": "stdio"}


def test_live_server_json_environment_matches_the_launch_gate_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiworkhub import process_launcher

    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    variables = {item["name"]: item for item in server["packages"][0]["environmentVariables"]}
    gates = (process_launcher.ALLOW_WRITES_ENV, process_launcher.ALLOW_LAUNCH_ENV)
    assert set(variables) == {"AIWORKHUB_REPO_ROOT", *gates}
    assert variables["AIWORKHUB_REPO_ROOT"]["isRequired"] is True
    assert "default" not in variables["AIWORKHUB_REPO_ROOT"]
    for name in gates:
        monkeypatch.delenv(name, raising=False)
    assert process_launcher.launch_gates_open() is False
    for name in gates:
        assert variables[name]["isRequired"] is False
        assert variables[name]["format"] == "string"
        assert variables[name]["default"] == "0"
        monkeypatch.setenv(name, variables[name]["default"])
    assert process_launcher.launch_gates_open() is False
    for value, expected in (("1", True), ("true", False), (" 1", False)):
        for name in gates:
            monkeypatch.setenv(name, value)
        assert process_launcher.launch_gates_open() is expected
    first, second = gates
    for opened, closed in ((first, second), (second, first)):
        monkeypatch.setenv(opened, "1")
        monkeypatch.setenv(closed, "0")
        assert process_launcher.launch_gates_open() is False
        # An unset gate must stay closed too: the per-gate default can drift
        # independently of the explicit "0" asserted above.
        monkeypatch.delenv(closed)
        assert process_launcher.launch_gates_open() is False


def test_sync_updates_every_extension_projection(tmp_path: Path) -> None:
    _fixture(tmp_path)
    assert release_metadata.check(tmp_path)["ok"] is False
    result = release_metadata.sync(tmp_path)
    assert result["ok"] is True
    assert set(result["projections"].values()) == {"1.2.3"}


def test_tag_mismatch_fails_without_mutating_projections(tmp_path: Path) -> None:
    _fixture(tmp_path, canonical="1.2.3", projected="1.2.3")
    result = release_metadata.check(tmp_path, tag="v1.2.4")
    assert result["ok"] is False
    assert result["mismatches"] == {"release-tag": "1.2.4"}


def test_missing_changelog_release_section_fails(tmp_path: Path) -> None:
    _fixture(tmp_path, canonical="1.2.3", projected="1.2.3")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    result = release_metadata.check(tmp_path)
    assert result["ok"] is False
    assert result["mismatches"]["CHANGELOG.md"] == "missing [1.2.3] section"


def test_checksums_are_deterministic_sorted_and_basename_only(tmp_path: Path) -> None:
    second = tmp_path / "zeta.vsix"
    first = tmp_path / "alpha.whl"
    second.write_bytes(b"zeta")
    first.write_bytes(b"alpha")
    output = tmp_path / "SHA256SUMS"
    digests = release_metadata.sha256sums([second, first], output)
    assert list(digests) == ["alpha.whl", "zeta.vsix"]
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines == [
        f'{digests["alpha.whl"]}  alpha.whl',
        f'{digests["zeta.vsix"]}  zeta.vsix',
    ]


def test_checksums_reject_duplicate_basenames(tmp_path: Path) -> None:
    left = tmp_path / "left" / "artifact.bin"
    right = tmp_path / "right" / "artifact.bin"
    left.parent.mkdir()
    right.parent.mkdir()
    left.write_bytes(b"left")
    right.write_bytes(b"right")
    try:
        release_metadata.sha256sums([left, right], tmp_path / "SHA256SUMS")
    except ValueError as exc:
        assert "basenames must be unique" in str(exc)
    else:
        raise AssertionError("duplicate release artifact basenames were accepted")


def test_ci_and_release_enforce_metadata_reproducibility_and_checksums() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "scripts/release_metadata.py check" in ci
    assert 'python -m pip install -e ".[dev]"' in ci
    assert "ruff check src/aiworkhub scripts tests" in ci
    assert "mypy" in ci
    assert "python scripts/check_public_docs.py" in ci
    assert "Verify reproducible VSIX bytes" in ci
    assert "scripts/release_metadata.py check --tag" in release
    assert "pypa/gh-action-pypi-publish@release/v1" in release
    assert "python -m twine check dist/*" in release
    assert "Verify reproducible VSIX bytes" in release
    assert "release-assets/SHA256SUMS" in release
    assert "python -m venv" in release
    assert 'pip install "${wheels[0]}"' in release
    assert "import aiworkhub" in release
    assert "aiworkhub --help" in release
    assert "Verify fresh VSIX manifest and package" in release
    assert 'python -m pip install -e ".[dev]"' not in release
    assert "ruff check src/aiworkhub scripts tests" not in release
    assert "python scripts/check_public_docs.py" not in release
    assert "Run Python tests" not in release
    assert "Run static quality gates" not in release
    assert "Run extension tests" not in release
    assert "platform-qualification" not in release
    assert "--no-index" not in release


def test_check_covers_the_documents_the_extension_test_gates_on(tmp_path):
    """A local gate that passes while the release gate fails is worse than none.

    v0.10.78 was tagged with release_metadata check reporting ok, and CI then
    failed extension-static.test.js on all four platforms -- twice, once for the
    extension README's missing "What's new in <version>" heading and once for
    the extension CHANGELOG's missing "## <version> —" section. Neither was in
    the check, so the one instrument whose job is to make a release safe said it
    was.
    """
    root = Path(__file__).resolve().parents[1]
    version = release_metadata.canonical_version(root)

    assert release_metadata.narrative_mismatches(root, version) == {}

    unreleased = "99.99.99"
    gaps = release_metadata.narrative_mismatches(root, unreleased)
    assert set(gaps) == {
        "vscode-extension/README.md",
        "vscode-extension/CHANGELOG.md",
        "README.md",
    }
    for relative, _template in release_metadata.NARRATIVE_PROJECTIONS:
        assert relative in gaps


def test_an_unreadable_release_document_is_a_mismatch_not_a_pass(tmp_path):
    """Fail closed: a document that cannot be read has not named the version."""
    gaps = release_metadata.narrative_mismatches(tmp_path, "1.2.3")
    assert set(gaps) == {
        "vscode-extension/README.md",
        "vscode-extension/CHANGELOG.md",
        "README.md",
    }
    assert all("unreadable" in reason for reason in gaps.values())


def test_root_readme_release_projection_is_gated(tmp_path: Path) -> None:
    """The root README's own release-current heading is a checked projection."""
    _fixture(tmp_path, canonical="1.2.3", projected="1.2.3")
    assert release_metadata.check(tmp_path)["ok"] is True

    (tmp_path / "README.md").write_text(
        "# AIWorkHub\n\n## What's new in 0.9.9 — 2026-01-01\n", encoding="utf-8"
    )
    result = release_metadata.check(tmp_path)
    assert result["ok"] is False
    assert "missing" in result["mismatches"]["README.md"]
    assert "What's new in 1.2.3" in result["mismatches"]["README.md"]


def test_readme_may_carry_only_the_current_whats_new_section(tmp_path: Path) -> None:
    """Each release prepended a section; the extension README grew to 116 of them."""
    _fixture(tmp_path, canonical="1.2.3", projected="1.2.3")
    # History lives in the changelog, so several release sections are fine there.
    (tmp_path / "vscode-extension" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.2.3 — 2026-01-01\n\n## 1.2.2 — 2025-12-01\n",
        encoding="utf-8",
    )
    current = "## What's new in 1.2.3 — 2026-01-01\n\n- current\n"
    older = "## What's new in 1.2.2 — 2025-12-01\n\n- older\n"

    for relative in ("vscode-extension/README.md", "README.md"):
        readme = tmp_path / relative
        readme.write_text(f"# AIWorkHub\n\n{current}\n{older}", encoding="utf-8")
        gaps = release_metadata.narrative_mismatches(tmp_path, "1.2.3")
        assert set(gaps) == {relative}
        assert "more than one" in gaps[relative]
        assert release_metadata.check(tmp_path)["ok"] is False

        readme.write_text(f"# AIWorkHub\n\n{current}", encoding="utf-8")
        assert release_metadata.narrative_mismatches(tmp_path, "1.2.3") == {}
        assert release_metadata.check(tmp_path)["ok"] is True


def test_release_vsix_smoke_checks_packaged_assets_not_false_dist_prefix() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    packaged = (
        "extension/extension.js",
        "extension/media/app.js",
        "extension/runtime/aiworkhub/__init__.py",
    )
    assert "Verify fresh VSIX manifest and package" in release
    assert "(( ${#vsix[@]} == 1 ))" in release
    assert 'test -s "${vsix[0]}"' in release
    assert 'assert manifest["name"] == "aiworkhub"' in release
    for asset in packaged:
        assert f'assert "{asset}" in names' in release
    assert 'assert any(name.startswith("extension/dist/") for name in names)' not in release

    names = ["extension/package.json", *packaged]
    assert any(name.startswith("extension/dist/") for name in names) is False
    assert all(asset in names for asset in packaged)


def _drift_server_json(root: Path, *, top: str, package: str) -> None:
    path = root / "server.json"
    server = json.loads(path.read_text(encoding="utf-8"))
    server["version"] = top
    server["packages"][0]["version"] = package
    path.write_text(json.dumps(server, indent=2) + "\n", encoding="utf-8")


def test_check_reports_each_server_json_version_field_that_drifts(tmp_path: Path) -> None:
    _fixture(tmp_path, canonical="1.2.3", projected="1.2.3")
    assert release_metadata.check(tmp_path)["ok"] is True

    _drift_server_json(tmp_path, top="1.2.2", package="1.2.3")
    result = release_metadata.check(tmp_path)
    assert result["ok"] is False
    assert result["mismatches"] == {"server.json": "1.2.2"}

    _drift_server_json(tmp_path, top="1.2.3", package="1.2.1")
    result = release_metadata.check(tmp_path)
    assert result["ok"] is False
    assert result["mismatches"] == {"server.json#packages-0": "1.2.1"}

    _drift_server_json(tmp_path, top="1.2.2", package="1.2.1")
    result = release_metadata.check(tmp_path)
    assert result["ok"] is False
    assert result["mismatches"] == {
        "server.json": "1.2.2",
        "server.json#packages-0": "1.2.1",
    }


def test_sync_repairs_server_json_versions_and_changes_nothing_else(tmp_path: Path) -> None:
    _fixture(tmp_path, canonical="1.2.3", projected="1.2.3")
    path = tmp_path / "server.json"
    expected = json.loads(path.read_text(encoding="utf-8"))
    _drift_server_json(tmp_path, top="1.2.2", package="1.2.1")
    assert release_metadata.check(tmp_path)["ok"] is False

    result = release_metadata.sync(tmp_path)

    assert result["ok"] is True
    assert result["mismatches"] == {}
    # Exact bytes: key order, two-space indent, one trailing "\n", no CRLF translation.
    assert path.read_bytes().decode("utf-8") == json.dumps(expected, indent=2) + "\n"
    assert release_metadata.check(tmp_path)["ok"] is True


def test_server_json_without_a_package_fails_closed(tmp_path: Path) -> None:
    _fixture(tmp_path, canonical="1.2.3", projected="1.2.3")
    (tmp_path / "server.json").write_text(
        json.dumps({"name": "io.github.shrec/aiworkhub", "version": "1.2.3", "packages": []}),
        encoding="utf-8",
    )
    result = release_metadata.check(tmp_path)
    assert result["ok"] is False
    assert result["mismatches"] == {"server.json#packages-0": "missing"}
    with pytest.raises(ValueError, match=r"server\.json packages\[0\]"):
        release_metadata.sync(tmp_path)


def test_release_workflow_publishes_to_the_mcp_registry_by_oidc_after_pypi() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert release.count("\n  publish-mcp-registry:\n") == 1
    job = re.split(
        r"\n  [A-Za-z0-9_-]+:\n",
        release.split("\n  publish-mcp-registry:\n", 1)[1],
        maxsplit=1,
    )[0]
    assert "name: Publish to MCP Registry" in job
    assert "if: ${{ !cancelled() && vars.MCP_REGISTRY_PUBLISH_ENABLED == 'true' }}" in job
    assert "needs: [provenance, publish-pypi]" in job
    assert "needs.publish-pypi.result" in job
    assert "id-token: write" in job
    assert "contents: read" in job
    assert "ref: ${{ needs.provenance.outputs.release_sha }}" in job
    assert "persist-credentials: false" in job
    assert "releases/download/v1.8.1/mcp-publisher_linux_amd64.tar.gz" in job
    assert "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc" in job
    assert "sha256sum --check --strict" in job
    assert "registry_1.8.1_checksums.txt" not in job
    assert "./mcp-publisher login github-oidc" in job
    assert "./mcp-publisher publish" in job


def test_readme_carries_the_mcp_registry_ownership_marker() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "<!-- mcp-name: io.github.shrec/aiworkhub -->" in readme


NOTES_CHECKSUM_LINE = "Verify downloaded artifacts against the attached `SHA256SUMS`."

NOTES_CHANGELOG = """\
# Changelog

Intro prose that is not part of any release.

## [Unreleased]

- Pending work that must never be published.

## [1.2.3] - 2026-01-03

### Fixed

- Third release fix — with an em dash.

### Changed

- Third release change.

## [1.2.2] - 2026-01-02

- Second release note.

## [1.2.1-rc.1] - 2026-01-01

- Release candidate note.
"""

NOTES_BODY_1_2_3 = (
    "### Fixed\n\n"
    "- Third release fix — with an em dash.\n\n"
    "### Changed\n\n"
    "- Third release change."
)


def _run_notes(
    root: Path, tag: str, changelog: str | None = NOTES_CHANGELOG
) -> tuple[int, Path]:
    # Bytes, not write_text: text mode would turn "\n" into "\r\n" on Windows.
    if changelog is not None:
        (root / "CHANGELOG.md").write_bytes(changelog.encode("utf-8"))
    output = root / "release-assets" / "RELEASE_NOTES.md"
    argv = ["--root", str(root), "notes", "--tag", tag, "--output", str(output)]
    return release_metadata.main(argv), output


def _notes_error(
    root: Path,
    tag: str,
    capsys: pytest.CaptureFixture[str],
    changelog: str | None = NOTES_CHANGELOG,
) -> str:
    code, output = _run_notes(root, tag, changelog)
    result = json.loads(capsys.readouterr().out)
    assert code != 0
    assert result["ok"] is False
    assert not output.exists()
    return str(result["error"])


@pytest.mark.parametrize(
    ("tag", "body"),
    [
        ("v1.2.3", NOTES_BODY_1_2_3),
        ("v1.2.2", "- Second release note."),
        ("v1.2.1-rc.1", "- Release candidate note."),
    ],
)
def test_notes_are_exactly_the_tagged_section_plus_the_checksum_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tag: str, body: str
) -> None:
    code, output = _run_notes(tmp_path, tag)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert output.read_bytes() == f"{body}\n\n{NOTES_CHECKSUM_LINE}\n".encode("utf-8")


def test_notes_stop_at_the_next_release_heading_and_trim_only_the_edges(
    tmp_path: Path,
) -> None:
    changelog = (
        "# Changelog\n"
        "\n"
        "## [2.0.0] - 2026-02-01\n"
        "  \n"
        "\n"
        "First paragraph.\n"
        "\n"
        "\n"
        "Second paragraph after two blank lines.\n"
        "\t\n"
        "\n"
        "## [1.9.9]\n"
        "A heading without a date still ends the section.\n"
    )
    code, output = _run_notes(tmp_path, "v2.0.0", changelog)
    assert code == 0
    assert output.read_bytes() == (
        "First paragraph.\n\n\nSecond paragraph after two blank lines.\n\n"
        f"{NOTES_CHECKSUM_LINE}\n"
    ).encode("utf-8")


@pytest.mark.parametrize(
    ("tag", "other_heading"),
    [
        ("v9.9.9", "## [1.2.3] - 2026-01-03"),
        ("v1.2.3", "## [1.2.30] - 2026-01-03"),
        ("v1.2.3", "## [1x2x3] - 2026-01-03"),
        ("v1.2.3", "## [1.2.3-rc.1] - 2026-01-03"),
        ("v1.2.1-rc.1", "## [1.2.1-rcx1] - 2026-01-03"),
        ("v1.2.3", "## [Unreleased]"),
    ],
)
def test_notes_fail_unless_the_exact_version_heading_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tag: str, other_heading: str
) -> None:
    changelog = f"# Changelog\n\n{other_heading}\n\n- Not the requested release.\n"
    error = _notes_error(tmp_path, tag, capsys, changelog)
    assert f"## [{tag[1:]}]" in error


@pytest.mark.parametrize(
    "section",
    [
        pytest.param("\n\n## [1.2.2] - 2026-01-02\n\n- Older note.\n", id="blank-lines-only"),
        pytest.param("", id="heading-ends-the-file"),
        pytest.param("   \n\t\n", id="whitespace-only"),
    ],
)
def test_notes_fail_when_the_tagged_section_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], section: str
) -> None:
    changelog = (
        "# Changelog\n\n## [Unreleased]\n\n- Pending work.\n\n"
        f"## [1.2.3] - 2026-01-03\n{section}"
    )
    assert "empty" in _notes_error(tmp_path, "v1.2.3", capsys, changelog)


@pytest.mark.parametrize(
    "tag",
    ["Unreleased", "vUnreleased", "unreleased", "", "latest", "v1.2", "refs/tags/v1.2.3"],
)
def test_notes_reject_tags_that_are_not_release_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tag: str
) -> None:
    loose = "\n## [1.2]\n\n- Two-part version.\n\n## [latest]\n\n- Moving target.\n"
    error = _notes_error(tmp_path, tag, capsys, NOTES_CHANGELOG + loose)
    assert "not release-safe" in error


def test_notes_fail_when_there_is_no_changelog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "CHANGELOG.md" in _notes_error(tmp_path, "v1.2.3", capsys, changelog=None)


def test_notes_are_lf_only_utf8_even_for_a_crlf_changelog(tmp_path: Path) -> None:
    code, output = _run_notes(tmp_path, "v1.2.3", NOTES_CHANGELOG.replace("\n", "\r\n"))
    assert code == 0
    assert output.read_bytes() == f"{NOTES_BODY_1_2_3}\n\n{NOTES_CHECKSUM_LINE}\n".encode("utf-8")


def test_live_changelog_yields_notes_for_the_live_canonical_version(tmp_path: Path) -> None:
    canonical = release_metadata.canonical_version(ROOT)
    output = tmp_path / "RELEASE_NOTES.md"
    assert release_metadata.main(["notes", "--tag", f"v{canonical}", "--output", str(output)]) == 0

    text = output.read_bytes().decode("utf-8")
    suffix = f"\n\n{NOTES_CHECKSUM_LINE}\n"
    assert text.endswith(suffix)
    assert "\r" not in text
    body = text[: -len(suffix)]
    assert body.strip()
    assert not any(line.startswith("## [") for line in body.split("\n"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    live_section = changelog.split(f"## [{canonical}]", 1)[1].split("\n## [", 1)[0]
    assert body in live_section


def test_release_workflow_publishes_the_changelog_section_as_the_release_body() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert release.count("\n  github-release:\n") == 1
    job = re.split(
        r"\n  [A-Za-z0-9_-]+:\n",
        release.split("\n  github-release:\n", 1)[1],
        maxsplit=1,
    )[0]
    command = (
        'python scripts/release_metadata.py notes --tag "$RELEASE_TAG" '
        "--output release-assets/RELEASE_NOTES.md"
    )
    assert "RELEASE_TAG: ${{ needs.provenance.outputs.release_tag }}" in job
    assert job.count(command) == 1
    assert job.index(command) < job.index("- name: Create GitHub Release")
    assert "body_path: release-assets/RELEASE_NOTES.md" in job
    assert re.search(r"^\s*body:", job, re.MULTILINE) is None
    assert "RELEASE_NOTES.md" not in job.split("files: |", 1)[1]
    assert "See the repository commit history for details." not in release
