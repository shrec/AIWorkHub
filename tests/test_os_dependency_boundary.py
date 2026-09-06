from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_os_dependency_boundary as checker


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".aiworkhub/config/development_rules.json"


@pytest.mark.parametrize("module", ["fcntl", "msvcrt"])
def test_ast_import_scanner_matches_valid_import_forms(tmp_path, module):
    positives = [
        f"import {module}; import os\n",
        f"if True: import {module}\n",
        f"import {module}.submodule as dependency\n",
        f"from {module}.submodule import (\n    value,\n)\n",
        f"import os, {module}, sys\n",
    ]
    for index, source in enumerate(positives):
        root, config = _fixture(tmp_path / str(index), {"sample.py": source}, [])
        counts = checker.scan_repository(root, checker.load_manifest(config))
        assert counts[("src/aiworkhub/sample.py", f"import_{module}")] == 1


@pytest.mark.parametrize("module", ["fcntl", "msvcrt"])
def test_ast_import_scanner_ignores_non_import_lookalikes(tmp_path, module):
    negatives = [
        f"import os as {module}\n",
        f"import prefix_{module}\n",
        f"import {module}_suffix\n",
        f"# import os, {module}\n",
        f'example = "import os, {module}"\n',
        f'example = """\nimport {module}\n"""\n',
        f"from .{module} import value\n",
    ]
    for index, source in enumerate(negatives):
        root, config = _fixture(tmp_path / str(index), {"sample.py": source}, [])
        assert checker.scan_repository(root, checker.load_manifest(config)) == {}


@pytest.mark.parametrize("module", ["fcntl", "msvcrt"])
def test_ast_import_scanner_fails_closed_on_syntax_error(tmp_path, module):
    root, config = _fixture(tmp_path, {"sample.py": f"if True import {module}\n"}, [])
    with pytest.raises(
        ValueError,
        match=r"invalid Python syntax in scan input: src/aiworkhub/sample\.py:1:\d+",
    ):
        checker.scan_repository(root, checker.load_manifest(config))


def _fixture(tmp_path: Path, files: dict[str, str], baseline: list[dict[str, object]]) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "src/aiworkhub").mkdir(parents=True)
    for name, source in files.items():
        path = root / "src/aiworkhub" / name
        path.write_text(source, encoding="utf-8")
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    raw["os_dependency_boundary"]["baseline"] = baseline
    total = sum(int(entry["count"]) for entry in baseline)
    raw["os_dependency_boundary"]["measurement"].update(reference_total=total, current_total=total, accepted_predecessor_delta=0)
    config = root / "rules.json"
    config.write_text(json.dumps(raw), encoding="utf-8")
    return root, config


def test_current_tree_passes_and_baseline_is_sorted():
    manifest = checker.load_manifest(CONFIG)
    assert checker.check(ROOT, CONFIG) == []
    boundary = manifest.os_dependency_boundary
    assert boundary is not None
    keys = [(entry.path, entry.pattern) for entry in boundary.baseline]
    assert keys == sorted(keys)
    # 153 -> 145: the scanner used to match its patterns against raw source, so
    # a docstring DESCRIBING an OS dependency was counted as one. Twelve of the
    # recorded matches were prose, including development_rules.py -- the file
    # that declares the rule -- recorded as violating it. The boundary now
    # measures code only, so the number finally means what it says.
    assert sum(entry.count for entry in boundary.baseline) == 139


def test_new_identity_and_same_identity_growth_fail(tmp_path):
    baseline = [{"path": "src/aiworkhub/a.py", "pattern": "sys_platform", "count": 1}]
    root, config = _fixture(tmp_path, {"a.py": "sys.platform\nsys.platform\n", "b.py": "os.name == 'nt'\n"}, baseline)
    failures = checker.check(root, config)
    assert any("violation growth" in failure for failure in failures)
    assert any("new violation identity" in failure for failure in failures)


def test_offsetting_decrease_cannot_hide_growth_but_genuine_decrease_passes(tmp_path):
    baseline = [
        {"path": "src/aiworkhub/a.py", "pattern": "sys_platform", "count": 2},
        {"path": "src/aiworkhub/b.py", "pattern": "os_name_eq", "count": 1},
    ]
    root, config = _fixture(tmp_path, {"a.py": "sys.platform\n", "b.py": "os.name == 'nt'\nos.name == 'posix'\n"}, baseline)
    assert any("violation growth" in failure for failure in checker.check(root, config))
    (root / "src/aiworkhub/b.py").write_text("", encoding="utf-8")
    assert checker.check(root, config) == []


def test_missing_malformed_or_symlinked_inputs_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        checker.load_manifest(tmp_path / "missing.json")
    malformed = tmp_path / "bad.json"
    malformed.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception):
        checker.load_manifest(malformed)


def test_declared_scan_root_symlink_fails_closed_before_scanning(tmp_path):
    root, config = _fixture(tmp_path, {}, [])
    scan_root = root / "src/aiworkhub"
    scan_root.rmdir()
    alternate = root / "alternate"
    alternate.mkdir()
    (alternate / "would_scan.py").write_text("sys.platform\n", encoding="utf-8")
    scan_root.symlink_to(alternate, target_is_directory=True)

    with pytest.raises(ValueError, match="scan root must not be a symlink"):
        checker.check(root, config)


def test_nested_directory_symlink_fails_closed_without_following_it(tmp_path):
    root, config = _fixture(tmp_path, {}, [])
    tracked_directory = root / "tracked"
    tracked_directory.mkdir()
    (tracked_directory / "violation.py").write_text("sys.platform\n", encoding="utf-8")
    nested = root / "src/aiworkhub/nested"
    nested.mkdir()
    (nested / "linked").symlink_to(tracked_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink in scan input"):
        checker.check(root, config)


def test_generator_cli_rejects_symlinked_root_with_exit_2(tmp_path):
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(ROOT, target_is_directory=True)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/generate_os_dependency_boundary_baseline.py"),
            "--root",
            str(linked_root),
            "--check",
            str(CONFIG),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == "baseline generation failed closed: repository root must not be a symlink\n"


def test_checker_cli_rejects_nonexistent_root_with_exit_2(tmp_path):
    missing_root = tmp_path / "missing-root"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_os_dependency_boundary.py"),
            "--root",
            str(missing_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout.startswith("os-dependency boundary check failed closed: ")
    assert str(missing_root) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.stderr == ""


def test_exemption_widening_is_rejected():
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    raw["os_dependency_boundary"]["sanctioned_modules"].append("src/aiworkhub/anything.py")
    from aiworkhub.development_rules import ManifestValidationError, parse_manifest
    with pytest.raises(ManifestValidationError):
        parse_manifest(copy.deepcopy(raw))


def test_prose_that_describes_a_dependency_is_not_a_dependency(tmp_path):
    """A docstring naming a pattern must not be counted as using it."""
    source = (
        '"""This module explains why os.name == "nt" branches are avoided."""\n'
        "# also mentioned in a comment: sys.platform\n"
        "VALUE = 1\n"
    )
    root, config = _fixture(tmp_path, {"prose.py": source}, [])
    assert checker.scan_repository(root, checker.load_manifest(config)) == {}


def test_real_code_beside_prose_is_still_counted(tmp_path):
    """Stripping prose must not become stripping evidence."""
    source = (
        '"""Explains os.name == "nt" at length."""\n'
        "import os\n"
        "IS_WINDOWS = os.name == 'nt'\n"
    )
    root, config = _fixture(tmp_path, {"mixed.py": source}, [])
    counts = checker.scan_repository(root, checker.load_manifest(config))
    assert counts[("src/aiworkhub/mixed.py", "os_name_eq")] == 1


def test_an_untokenizable_file_fails_closed(tmp_path):
    """Never fall back to scanning raw text -- that restores the phantom counts."""
    root, config = _fixture(tmp_path, {"broken.py": 'x = "unterminated\n'}, [])
    with pytest.raises(ValueError, match="invalid Python syntax in scan input"):
        checker.scan_repository(root, checker.load_manifest(config))
