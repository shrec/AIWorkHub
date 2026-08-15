"""Contract tests for the bounded three-OS GitHub Actions CI matrix.

These tests pin the CI contract so that removing a platform-owned regression
manifest, a collection guard, or the full Linux suite causes a hard failure.

Acceptance criteria:
1. The Windows job executes explicit nt-specific lock/process/temp/validation
   regressions.
2. The macOS job executes explicit Darwin identity/launch/PATH/RSS/cleanup
   regressions.
3. A contract test fails when an OS manifest or collection guard is removed.
4. CI remains bounded through explicit manifests/shards and the existing
   install/VSIX checks and full Linux suite remain intact.
"""

from __future__ import annotations

import re
from pathlib import Path

CI_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

# Windows-owned nt regressions: lock/process/temp/validation.
WINDOWS_NT_MANIFEST = (
    "tests/test_windows_launch_lock_regression.py",
    "tests/test_windows_pid_identity.py",
    "tests/test_windows_vscode_lm_launch_portability.py",
    "tests/test_terminal_authority_windows.py",
    "tests/test_completion_inbox_process_enrichment_windows_v1.py",
    "tests/test_completion_inbox_finalize_failed_windows_v3.py",
    "tests/test_aiworkhub_windows_secure_broker_b1464.py",
    "tests/test_runtime_temp.py",
    "tests/test_validation_pytest_runtime_b755_v1.py",
    "tests/test_validation_exec_scratch_b753_v1.py",
    "tests/test_validation_tmpdir_prefix_b773_v1.py",
    "tests/test_validation_cwd_prefix_b892.py",
    "tests/test_validation_executable_resolution_b1460.py",
    "tests/test_platform_io_blocking_lock.py",
)

# macOS-owned Darwin regressions: identity/launch/PATH/RSS/cleanup.
MACOS_DARWIN_MANIFEST = (
    "tests/test_process_launcher.py",
    "tests/test_aiworkhub_atomic_launch_lifecycle_b1462.py",
    "tests/test_validation_executable_resolution_b1460.py",
    "tests/test_evidence_instruments.py",
    "tests/test_finalized_workspace_gc_b512_v1.py",
    "tests/test_runtime_temp.py",
    "tests/test_validation_cwd_prefix_b892.py",
)

# The pre-existing full Linux qualification suite that must remain unchanged.
LINUX_SHARED_SUITE = (
    "tests/test_platform_io.py",
    "tests/test_cross_platform_runtime.py",
    "tests/test_release_qualification_e2e.py",
    "tests/test_quality_calibration.py",
    "tests/test_source_graph_tree_sitter_semantic.py",
)

_INSTALL_VSIX_MARKERS = (
    "Set up Node.js",
    "npm install",
    "npm run package",
    "Package fresh VSIX",
)

_STEP_BOUNDARY = re.compile(r"^\s*-\s+(?:name|uses|run|shell|if|env):", re.MULTILINE)


def _ci_text() -> str:
    return CI_PATH.read_text(encoding="utf-8")


def _step_body(text: str, platform: str) -> str:
    """Return the run-block body of the step gated on ``matrix.platform``."""
    marker = f"if: matrix.platform == '{platform}'"
    start = text.index(marker)
    run_start = text.index("run: |", start) + len("run: |")
    match = _STEP_BOUNDARY.search(text, run_start)
    end = match.start() if match else len(text)
    return text[run_start:end]


def test_ci_workflow_is_readable() -> None:
    assert CI_PATH.is_file(), f"missing CI workflow: {CI_PATH}"
    assert _ci_text().strip(), "ci.yml is empty"


def test_matrix_declares_all_three_os_platforms() -> None:
    text = _ci_text()
    for platform in ("linux", "windows", "macos"):
        assert (
            f"platform: {platform}" in text
        ), f"cross-platform matrix is missing the '{platform}' platform entry"


def test_windows_job_runs_explicit_nt_manifest() -> None:
    body = _step_body(_ci_text(), "windows")
    missing = [path for path in WINDOWS_NT_MANIFEST if path not in body]
    assert not missing, (
        "Windows nt manifest is missing required test files: "
        + ", ".join(missing)
    )


def test_macos_job_runs_explicit_darwin_manifest() -> None:
    body = _step_body(_ci_text(), "macos")
    missing = [path for path in MACOS_DARWIN_MANIFEST if path not in body]
    assert not missing, (
        "macOS Darwin manifest is missing required test files: "
        + ", ".join(missing)
    )


def test_os_manifests_are_bounded_and_explicit() -> None:
    # Each manifest must be an explicit array assignment, not a bare wildcard
    # or the whole-suite collection, keeping the matrix bounded.
    for name, var in (("windows", "WINDOWS_NT_MANIFEST"), ("macos", "MACOS_DARWIN_MANIFEST")):
        body = _step_body(_ci_text(), name)
        assert f"{var}=(" in body, f"{name} step is missing an explicit {var} manifest array"


def test_platform_manifest_files_exist_on_disk() -> None:
    root = Path(__file__).resolve().parents[1]
    for path in (*WINDOWS_NT_MANIFEST, *MACOS_DARWIN_MANIFEST):
        assert (root / path).is_file(), f"manifest entry does not exist: {path}"


def test_collection_guard_fails_on_zero_collected_tests() -> None:
    # Both OS-owned steps must refuse to pass when zero platform tests collect.
    for platform in ("windows", "macos"):
        body = _step_body(_ci_text(), platform)
        assert "--collect-only" in body, (
            f"{platform} step is missing the preflight collection guard"
        )
        assert "refusing to pass silently" in body, (
            f"{platform} step is missing the zero-collected-tests failure guard"
        )


def test_linux_full_suite_is_not_replaced() -> None:
    text = _ci_text()
    # The original Linux qualification shard must still reference every shared
    # suite file (it runs on all three OSes and is the Linux coverage anchor).
    missing = [path for path in LINUX_SHARED_SUITE if path not in text]
    assert not missing, (
        "Linux full-suite coverage was replaced or reduced; missing: "
        + ", ".join(missing)
    )


def test_install_and_vsix_checks_remain_intact() -> None:
    text = _ci_text()
    missing = [marker for marker in _INSTALL_VSIX_MARKERS if marker not in text]
    assert not missing, (
        "Existing install/VSIX checks were removed: " + ", ".join(missing)
    )
