from pathlib import Path

from aiworkhub import known_bug_scanner, quality_evidence


def test_cuda_intrinsic_mismatch_is_error(tmp_path: Path):
    (tmp_path / "kernel.cu").write_text(
        "// rotl32(8)\nuint32_t f(uint32_t x) { return __byte_perm(x, 0, 0x0321); }\n",
        encoding="utf-8",
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["kernel.cu"])
    assert report["passed"] is False
    assert report["findings"][0]["rule_id"] == "cuda.byte_perm_rotation_mismatch"


def test_warning_is_visible_but_nonblocking(tmp_path: Path):
    (tmp_path / "legacy.cpp").write_text(
        "void f(char *a, const char *b) { strcpy(a, b); }\n", encoding="utf-8"
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["legacy.cpp"])
    assert report["passed"] is True and report["warnings"] == 1


def test_comment_is_not_scanned(tmp_path: Path):
    (tmp_path / "safe.py").write_text("# subprocess.run(x, shell=True)\n", encoding="utf-8")
    assert known_bug_scanner.scan_changed_paths(tmp_path, ["safe.py"])["findings"] == []


def test_completion_builtin_blocks_high_confidence_pattern(tmp_path: Path):
    (tmp_path / "runner.py").write_text("subprocess.run(x, shell=True)\n", encoding="utf-8")
    checks = quality_evidence.run_builtin_static_checks(tmp_path, changed_paths=["runner.py"])
    check = next(row for row in checks if row.check_id == "builtin:known_bug_patterns")
    assert check.status == quality_evidence.STATUS_FAILED
    assert "python.shell_true" in check.summary
