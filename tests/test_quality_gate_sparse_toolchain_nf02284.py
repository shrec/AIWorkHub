"""NF-2026-02284: a candidate-changed quality.json keeps the canonical toolchain.

A candidate that edits ``.aiworkhub/quality.json`` is not ``inherited_policy``,
so the accept path calls ``run_completion_quality_gate`` with
``policy_root=None``. The CONFIG then rightly comes from the candidate, but the
sparse union tree has no ``scripts/``, no declared-invariants module and no
gate tests. Canonical scripts/src/tests files the sparse tree lacks -- and the
candidate neither changed nor deleted -- are filled in from ``baseline_root``
before any check runs, so every check sees canonical + candidate bytes while
argv and PYTHONPATH stay exactly as declared.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import quality_evidence

PACKAGE = "nf02284_toolchain"
MODULE = f"{PACKAGE}.invariants"
CANONICAL_ONLY_CHECK = "canonical-only-check"
CANDIDATE_ONLY_CHECK = "candidate-only-check"
DECLARED_CHECK = "declared-invariants"
CONFIG = ".aiworkhub/quality.json"

# The declared invariants module needs the repo-wide ``scripts`` input named in
# its argv, and prints which copy of itself ran.
_INVARIANTS_SOURCE = (
    "import sys\n"
    "from pathlib import Path\n"
    "target = Path(sys.argv[1])\n"
    "assert (target / 'check_me.py').is_file(), f'missing input: {{target}}'\n"
    "print('invariants-copy=' + {marker!r})\n"
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _declare(root: Path, checks: list[dict]) -> None:
    _write(root, CONFIG, json.dumps({"checks": checks}))


_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git executable unavailable")


def _git(root: Path, *args: str) -> None:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    subprocess.run(
        [str(_GIT), "-C", str(root), *args],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
        timeout=60, env=env,
    )


def _git_track(root: Path) -> None:
    """Make ``root`` a git repo whose index tracks every file now present."""

    if _GIT is None:
        pytest.skip("git executable unavailable: the fill lists tracked files")
    _git(root, "init", "-q")
    files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )
    _git(root, "add", "--", *files)


def _declared_invariants_check() -> dict:
    return {
        "id": DECLARED_CHECK,
        "kind": "test",
        "command": ["{python}", "-m", MODULE, "scripts"],
    }


def _canonical_fixture(root: Path) -> None:
    """A full canonical repo: config, scripts/, src/ toolchain and tests/."""

    _declare(root, [
        _declared_invariants_check(),
        {"id": CANONICAL_ONLY_CHECK, "kind": "test",
         "command": [sys.executable, "-c", "raise SystemExit(0)"]},
    ])
    _write(root, "scripts/check_me.py", "VALUE = 1\n")
    _write(root, f"src/{PACKAGE}/__init__.py", "")
    _write(root, f"src/{PACKAGE}/invariants.py",
           _INVARIANTS_SOURCE.format(marker="canonical"))
    _write(root, "tests/test_placeholder.py", "def test_ok():\n    assert True\n")


def _sparse_union(root: Path, checks: list[dict] | None = None) -> None:
    """The sparse union: only the candidate's modified quality.json."""

    _declare(root, checks if checks is not None else [
        _declared_invariants_check(),
        # Added only by the candidate config: proves the candidate's config,
        # not the canonical one, decides which checks run.
        {"id": CANDIDATE_ONLY_CHECK, "kind": "test",
         "command": [sys.executable, "-c",
                     "from pathlib import Path; "
                     f"assert Path('{CONFIG}').is_file()"]},
    ])


def _check(gate: dict, check_id: str) -> dict:
    return next(row for row in gate["checks"] if row["check_id"] == check_id)


def _gate(union: Path, canonical: Path | None, changed: list[str] | None = None) -> dict:
    # The fill copies only tracked canonical files: index the fixture once.
    if canonical is not None and not (canonical / ".git").exists():
        _git_track(canonical)
    return quality_evidence.run_completion_quality_gate(
        union,
        changed_paths=changed if changed is not None else [CONFIG],
        combined_tree_scope=True,
        policy_root=None,
        baseline_root=canonical,
    )


def test_sparse_union_without_toolchain_fails_without_baseline(tmp_path: Path) -> None:
    """Control: the defect reproduces when no canonical toolchain is supplied."""

    union = tmp_path / "union"
    _sparse_union(union)
    gate = _gate(union, None)
    check = _check(gate, DECLARED_CHECK)
    assert check["status"] == "failed"
    assert "No module named" in check["summary"]
    assert gate["passed"] is False


def test_changed_policy_resolves_module_and_inputs_from_canonical_toolchain(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    _canonical_fixture(canonical)
    _sparse_union(union)

    gate = _gate(union, canonical)

    check = _check(gate, DECLARED_CHECK)
    assert "No module named" not in check["summary"]
    assert check["status"] == "passed", check["summary"]
    assert "invariants-copy=canonical" in check["summary"]
    # argv is passed verbatim: the input resolves inside the validation tree.
    assert check["executed_command"][-3:] == ["-m", MODULE, "scripts"]
    assert check["provenance"].endswith(":sparse_inputs_filled=4")
    assert gate["passed"] is True, gate["blocking_checks"]
    assert gate["verification_scope"] == "repository_policy"
    # The check saw the filled input while it ran; the sealed tree keeps none.
    assert not (union / "scripts").exists()
    # The candidate's config is never replaced by the canonical one.
    assert CANDIDATE_ONLY_CHECK in (union / CONFIG).read_text(encoding="utf-8")


def test_candidate_config_still_decides_which_checks_run(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    _canonical_fixture(canonical)
    _sparse_union(union)

    gate = _gate(union, canonical)

    ids = {row["check_id"] for row in gate["checks"]}
    assert CANDIDATE_ONLY_CHECK in ids
    assert CANONICAL_ONLY_CHECK not in ids
    candidate_only = _check(gate, CANDIDATE_ONLY_CHECK)
    # It ran with the validation tree as cwd (its config file is there).
    assert candidate_only["status"] == "passed", candidate_only["summary"]
    assert _check(gate, DECLARED_CHECK)["status"] == "passed"


def test_check_importing_canonical_only_module_sees_candidate_bytes(
    tmp_path: Path,
) -> None:
    """(1) pkg.b exists only in canonical and imports the candidate-changed pkg.a."""

    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    check = {"id": DECLARED_CHECK, "kind": "test",
             "command": ["{python}", "-m", f"{PACKAGE}.b"]}
    _declare(canonical, [check])
    _write(canonical, f"src/{PACKAGE}/__init__.py", "")
    _write(canonical, f"src/{PACKAGE}/a.py", "VALUE = 'canonical'\n")
    _write(canonical, f"src/{PACKAGE}/b.py",
           f"from {PACKAGE} import a\nprint('a-copy=' + a.VALUE)\n")
    _sparse_union(union, [check])
    _write(union, f"src/{PACKAGE}/a.py", "VALUE = 'candidate'\n")

    gate = _gate(union, canonical, [CONFIG, f"src/{PACKAGE}/a.py"])

    row = _check(gate, DECLARED_CHECK)
    assert row["status"] == "passed", row["summary"]
    assert "a-copy=candidate" in row["summary"]
    assert "a-copy=canonical" not in row["summary"]
    # The candidate's file was never overwritten by the fill.
    assert (union / "src" / PACKAGE / "a.py").read_text(encoding="utf-8") == (
        "VALUE = 'candidate'\n"
    )


def test_candidate_deleted_test_is_not_refilled_and_check_fails(tmp_path: Path) -> None:
    """(2) A deletion is a changed path: it stays deleted and the check fails."""

    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    check = {"id": DECLARED_CHECK, "kind": "test",
             "command": [sys.executable, "-c",
                         "import sys; from pathlib import Path; "
                         "sys.exit(0 if Path(sys.argv[1]).is_file() else 3)",
                         "tests/test_x.py"]}
    _declare(canonical, [check])
    _write(canonical, "tests/test_x.py", "def test_x():\n    assert True\n")
    _write(canonical, "tests/test_y.py", "def test_y():\n    assert True\n")
    _sparse_union(union, [check])

    gate = _gate(union, canonical, [CONFIG, "tests/test_x.py"])

    row = _check(gate, DECLARED_CHECK)
    assert row["status"] == "failed", row["summary"]
    assert row["executed_command"][-1] == "tests/test_x.py"
    assert not (union / "tests" / "test_x.py").exists()
    # The unchanged canonical sibling was filled for the run, then removed.
    assert row["provenance"].endswith(":sparse_inputs_filled=1")
    assert not (union / "tests" / "test_y.py").exists()
    assert gate["passed"] is False


def test_declared_option_value_is_passed_verbatim(tmp_path: Path) -> None:
    """(3) ``-k tests`` names a pytest expression, not a path to rewrite."""

    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    check = {"id": DECLARED_CHECK, "kind": "test",
             "command": [sys.executable, "-c",
                         "import sys; print('argv=' + repr(sys.argv[1:]))",
                         "-k", "tests"]}
    _declare(canonical, [check])
    _write(canonical, "tests/test_placeholder.py", "def test_ok():\n    assert True\n")
    _sparse_union(union, [check])

    gate = _gate(union, canonical)

    row = _check(gate, DECLARED_CHECK)
    assert row["status"] == "passed", row["summary"]
    assert row["executed_command"][-2:] == ["-k", "tests"]
    assert "argv=['-k', 'tests']" in row["summary"]


def test_full_tree_without_toolchain_root_fills_nothing(tmp_path: Path) -> None:
    """(4) toolchain_root=None is the full-tree path: nothing is filled."""

    canonical = tmp_path / "canonical"
    full = tmp_path / "full"
    _canonical_fixture(canonical)
    _canonical_fixture(full)
    (full / "tests" / "test_placeholder.py").unlink()
    before = sorted(path.relative_to(full).as_posix() for path in full.rglob("*"))

    empty = quality_evidence.SparseFillResult()
    assert quality_evidence._fill_sparse_toolchain_inputs(full, None, []) == empty
    assert quality_evidence._fill_sparse_toolchain_inputs(full, full, []) == empty
    results = quality_evidence.run_declared_checks(
        full, changed_paths=[CONFIG], policy_root=canonical, toolchain_root=None,
    )

    row = next(item for item in results if item.check_id == DECLARED_CHECK)
    assert row.status == "passed", row.summary
    assert "sparse_inputs_filled" not in row.provenance
    after = sorted(
        path.relative_to(full).as_posix()
        for path in full.rglob("*")
        if "__pycache__" not in path.parts
    )
    assert after == before
    assert not (full / "tests" / "test_placeholder.py").exists()


_CANONICAL_TRACKED = (
    "scripts/check_me.py",
    f"src/{PACKAGE}/__init__.py",
    f"src/{PACKAGE}/invariants.py",
    "tests/test_placeholder.py",
)


def _tracked_canonical(tmp_path: Path) -> tuple[Path, Path]:
    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    _canonical_fixture(canonical)
    _git_track(canonical)
    _sparse_union(union)
    return canonical, union


@requires_git
def test_untracked_and_ignored_canonical_files_are_not_filled(tmp_path: Path) -> None:
    """Defect 1: only ``git ls-files`` entries are canonical, never host leftovers."""

    canonical, union = _tracked_canonical(tmp_path)
    _write(canonical, ".gitignore", "*.secret\n")
    _write(canonical, "scripts/untracked_local.py", "LEAK = 1\n")
    _write(canonical, "src/token.secret", "do-not-copy\n")

    gate = _gate(union, canonical)

    check = _check(gate, DECLARED_CHECK)
    assert check["status"] == "passed", check["summary"]
    assert check["provenance"].endswith(":sparse_inputs_filled=4")
    assert not (union / "scripts" / "untracked_local.py").exists()
    assert not (union / "src" / "token.secret").exists()
    # NF-2026-02294: the fill is removed once the checks finish.
    for relative in _CANONICAL_TRACKED:
        assert not (union / relative).exists(), relative


@requires_git
def test_git_listing_failure_fills_nothing_and_names_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 1: a failed listing never falls back to walking the canonical tree."""

    canonical, union = _tracked_canonical(tmp_path)

    def _no_git(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(quality_evidence.subprocess, "run", _no_git)
    result = quality_evidence._fill_sparse_toolchain_inputs(union, canonical, [CONFIG])

    assert result == quality_evidence.SparseFillResult(
        error="git_ls_files_unavailable:FileNotFoundError"
    )
    for top in quality_evidence.SPARSE_TOOLCHAIN_INPUT_ROOTS:
        assert not (union / top).exists(), top


@requires_git
def test_symlinked_validation_dir_is_not_written_through(tmp_path: Path) -> None:
    """Defect 2: a link inside the validation tree never redirects a fill."""

    canonical, union = _tracked_canonical(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, union / "scripts", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable here: {exc}")

    result = quality_evidence._fill_sparse_toolchain_inputs(union, canonical, [CONFIG])

    assert list(outside.iterdir()) == []
    assert "scripts/check_me.py" not in result.filled
    assert result.refused == 1
    assert result.error == ""
    assert set(result.filled) == set(_CANONICAL_TRACKED) - {"scripts/check_me.py"}


@requires_git
def test_failed_copy_leaves_no_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 3: a copy failing after the exclusive create is unlinked, then raised."""

    canonical, union = _tracked_canonical(tmp_path)

    def _fail_midway(_reader: object, writer: object, *_args: object) -> None:
        writer.write(b"partial")  # type: ignore[attr-defined]
        raise OSError("injected mid-copy failure")

    monkeypatch.setattr(quality_evidence.shutil, "copyfileobj", _fail_midway)
    with pytest.raises(OSError, match="injected mid-copy failure"):
        quality_evidence._fill_sparse_toolchain_inputs(union, canonical, [CONFIG])

    for relative in _CANONICAL_TRACKED:
        assert not (union / relative).exists(), relative


@requires_git
@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
def test_executable_bit_is_preserved(tmp_path: Path) -> None:
    """Defect 4: the filled file keeps the canonical file mode."""

    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    _canonical_fixture(canonical)
    _write(canonical, "scripts/run_check.sh", "#!/bin/sh\nexit 0\n")
    (canonical / "scripts" / "run_check.sh").chmod(0o755)
    _git_track(canonical)
    _sparse_union(union)

    result = quality_evidence._fill_sparse_toolchain_inputs(union, canonical, [CONFIG])

    assert "scripts/run_check.sh" in result.filled
    source_mode = stat.S_IMODE((canonical / "scripts" / "run_check.sh").stat().st_mode)
    filled_mode = stat.S_IMODE((union / "scripts" / "run_check.sh").stat().st_mode)
    assert filled_mode == source_mode
    assert filled_mode & stat.S_IXUSR


@requires_git
def test_vscode_extension_files_are_filled_and_candidate_change_is_kept(
    tmp_path: Path,
) -> None:
    """NF-2026-00970: ``npm --prefix vscode-extension test`` needs the extension tree.

    A sparse tree lacking ``vscode-extension/package.json`` failed ENOENT for
    every card that changed an extension file. The canonical copy fills both
    tracked extension files when absent, and a candidate-changed one is kept.
    """

    canonical = tmp_path / "canonical"
    _canonical_fixture(canonical)
    _write(canonical, "vscode-extension/package.json", '{"name": "canonical"}\n')
    _write(canonical, "vscode-extension/test/x.test.js", "// canonical\n")
    _git_track(canonical)

    absent = tmp_path / "absent"
    _sparse_union(absent)
    result = quality_evidence._fill_sparse_toolchain_inputs(absent, canonical, [CONFIG])
    assert result.error == ""
    assert "vscode-extension/package.json" in result.filled
    assert "vscode-extension/test/x.test.js" in result.filled
    for relative in ("vscode-extension/package.json", "vscode-extension/test/x.test.js"):
        assert (absent / relative).read_bytes() == (canonical / relative).read_bytes()

    changed = tmp_path / "changed"
    _sparse_union(changed)
    _write(changed, "vscode-extension/test/x.test.js", "// candidate\n")
    result = quality_evidence._fill_sparse_toolchain_inputs(
        changed, canonical, [CONFIG, "vscode-extension/test/x.test.js"],
    )
    assert result.error == ""
    assert "vscode-extension/package.json" in result.filled
    assert "vscode-extension/test/x.test.js" not in result.filled
    assert (changed / "vscode-extension" / "test" / "x.test.js").read_text(
        encoding="utf-8",
    ) == "// candidate\n"


@requires_git
def test_docs_assets_raster_is_filled_for_checks_and_removed_afterwards(
    tmp_path: Path,
) -> None:
    """NF-2026-00983: extension-static.test.js asserts repoRoot/docs/assets rasters.

    A sparse tree lacking ``docs/assets`` failed "missing local raster asset".
    The tracked raster is filled for the checks only; other ``docs`` files are
    not fill roots, and cleanup removes the raster and the directories it made.
    """

    raster = "docs/assets/demo/diagram.png"
    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    check = {"id": DECLARED_CHECK, "kind": "test",
             "command": [sys.executable, "-c",
                         "import sys; from pathlib import Path; "
                         "p = Path(sys.argv[1]); "
                         "sys.exit(0 if p.is_file() and p.stat().st_size > 1000 else 3)",
                         raster]}
    _declare(canonical, [check])
    (canonical / "docs" / "assets" / "demo").mkdir(parents=True)
    (canonical / raster).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 2048)
    _write(canonical, "docs/GUIDE.md", "# not a fill root\n")
    _sparse_union(union, [check])

    gate = _gate(union, canonical)

    row = _check(gate, DECLARED_CHECK)
    assert row["status"] == "passed", row["summary"]
    assert row["provenance"].endswith(":sparse_inputs_filled=1")
    assert gate["passed"] is True, gate["blocking_checks"]
    assert not (union / raster).exists()
    assert not (union / "docs").exists()
    assert not (union / "docs" / "GUIDE.md").exists()
