from __future__ import annotations

import importlib.util
import json
from pathlib import Path


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
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{canonical}] - 2026-01-01\n",
        encoding="utf-8",
    )


def test_live_release_metadata_projections_match_canonical_version() -> None:
    result = release_metadata.check(ROOT)
    assert result["ok"] is True
    assert result["canonical_source"] == "src/aiworkhub/_version.py"
    assert result["canonical_version"] == "0.8.9"
    extension_manifest = json.loads(
        (ROOT / "vscode-extension" / "package.json").read_text(encoding="utf-8")
    )
    assert extension_manifest["publisher"] == "IvaneChkheidze"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = { attr = "aiworkhub._version.__version__" }' in pyproject


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
    assert 'python -m pip install -e ".[dev]"' in release
    assert "ruff check src/aiworkhub scripts tests" in release
    assert "mypy" in release
    assert "python scripts/check_public_docs.py" in release
    assert "pypa/gh-action-pypi-publish@release/v1" in release
    assert "python -m twine check dist/*" in release
    assert "Verify reproducible VSIX bytes" in release
    assert "release-assets/SHA256SUMS" in release
