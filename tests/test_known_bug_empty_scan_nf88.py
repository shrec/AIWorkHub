"""NF88 regression: legitimate empty/unsupported-data scans must pass cleanly.

Measured defect: ``known_bug_scanner._scan_path`` applied the ``MAX_FILE_BYTES``
gate before rule-suffix applicability, so a huge unsupported JSON blob produced
a blocking ``file_exceeds_max_bytes`` skip even though no rule could ever match
it, and the builtin ``known_bug_patterns`` check failed a clean diff.
``quality_evidence`` additionally omitted ``skipped_paths`` from its summary and
labeled every failure ``high_confidence_known_bug_pattern``.

Every test here drives the real builtin evidence path
(``quality_evidence.run_builtin_static_checks``), not scanner helpers alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import known_bug_scanner, quality_evidence

UNSUPPORTED_JSON_BYTES = 10_417_919


def _write_unsupported_json(root: Path) -> str:
    """Write an exactly 10,417,919-byte unsupported JSON blob."""
    target = root / "blob.json"
    payload = json.dumps({"blob": "x" * (UNSUPPORTED_JSON_BYTES - 64)}).encode("utf-8")
    assert len(payload) < UNSUPPORTED_JSON_BYTES
    with target.open("wb") as handle:
        handle.write(payload)
        handle.write(b" " * (UNSUPPORTED_JSON_BYTES - len(payload) - 1))
        handle.write(b"\n")
    assert target.stat().st_size == UNSUPPORTED_JSON_BYTES
    return "blob.json"


def test_builtin_passes_clean_code_plus_oversized_unsupported_json(tmp_path, monkeypatch):
    """Clean supported code plus a huge unsupported blob passes, unread by the scan."""
    assert UNSUPPORTED_JSON_BYTES > known_bug_scanner.MAX_FILE_BYTES
    (tmp_path / "clean.py").write_text("value = 1\n", encoding="utf-8")
    blob = _write_unsupported_json(tmp_path)
    read_targets: list[str] = []
    original_read_text = Path.read_text

    def recording_read_text(self, *args, **kwargs):
        read_targets.append(self.name)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read_text)
    checks = quality_evidence.run_builtin_static_checks(
        tmp_path, changed_paths=["clean.py", blob]
    )
    check = next(row for row in checks if row.check_id == "builtin:known_bug_patterns")
    assert check.status == quality_evidence.STATUS_PASSED
    assert check.error == ""
    summary = json.loads(check.summary)
    assert summary["errors"] == 0
    assert summary["warnings"] == 0
    assert summary["findings"] == []
    assert summary["skipped_paths"] == []
    assert "blob.json" not in read_targets


def test_builtin_blocks_oversized_supported_code_with_truthful_skip(tmp_path):
    """Supported oversized code stays fail-closed; empty findings never hide the skip."""
    (tmp_path / "big.py").write_text(
        "# " + "x" * known_bug_scanner.MAX_FILE_BYTES, encoding="utf-8"
    )
    checks = quality_evidence.run_builtin_static_checks(
        tmp_path, changed_paths=["big.py"]
    )
    check = next(row for row in checks if row.check_id == "builtin:known_bug_patterns")
    assert check.status == quality_evidence.STATUS_FAILED
    assert check.error == "known_bug_scan_skipped_uninspected_source"
    summary = json.loads(check.summary)
    assert summary["findings"] == []
    skip = summary["skipped_paths"][0]
    assert skip["path"] == "big.py"
    assert skip["reason"] == "file_exceeds_max_bytes"
    assert skip["max_bytes"] == known_bug_scanner.MAX_FILE_BYTES


def test_builtin_real_error_finding_still_blocks(tmp_path):
    (tmp_path / "runner.py").write_text("subprocess.run(x, shell=True)\n", encoding="utf-8")
    checks = quality_evidence.run_builtin_static_checks(
        tmp_path, changed_paths=["runner.py"]
    )
    check = next(row for row in checks if row.check_id == "builtin:known_bug_patterns")
    assert check.status == quality_evidence.STATUS_FAILED
    assert check.error == "high_confidence_known_bug_pattern"
    summary = json.loads(check.summary)
    assert summary["errors"] >= 1
    assert summary["skipped_paths"] == []


def test_builtin_scanner_exception_is_never_reported_clean(tmp_path, monkeypatch):
    """A scanner read failure must surface, not silently pass the builtin check."""
    (tmp_path / "boom.py").write_text("value = 1\n", encoding="utf-8")
    original_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if self.name == "boom.py":
            raise OSError("simulated read failure")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    with pytest.raises(OSError):
        quality_evidence.run_builtin_static_checks(tmp_path, changed_paths=["boom.py"])
