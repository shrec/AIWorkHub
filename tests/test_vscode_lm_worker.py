from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import vscode_lm_bridge, vscode_lm_worker  # noqa: E402


def test_main_stdout_is_safe_for_legacy_windows_code_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        vscode_lm_worker,
        "run",
        lambda _path: {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Move → 測試",
            "project_context_receipt": "",
        },
    )
    raw = io.BytesIO()
    legacy_stdout = io.TextIOWrapper(
        raw,
        encoding="cp1251",
        errors="strict",
        newline="",
    )

    with redirect_stdout(legacy_stdout):
        assert vscode_lm_worker.main(["--spec", str(tmp_path / "unused.json")]) == 0
        legacy_stdout.flush()

    output = raw.getvalue().decode("cp1251")
    assert "\\u2192" in output
    assert json.loads(output)["result"] == "Move → 測試"


def test_main_emits_structured_progress_security_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = vscode_lm_bridge.ProgressReceiptSecurityError(
        "bridge_progress_non_monotonic",
        "sequence",
        observed_sequence=4,
    )

    def fail(_path: Path) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(vscode_lm_worker, "run", fail)

    assert vscode_lm_worker.main(["--spec", str(tmp_path / "unused.json")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "bridge_progress_non_monotonic"
    assert result["diagnostics"]["progress_security"] == failure.receipt


def test_root_and_nested_existing_files_preserve_mode_bits(
    tmp_path: Path,
) -> None:
    root_target = tmp_path / "AGENTS.md"
    root_target.write_text("old\n", encoding="utf-8")
    root_target.chmod(0o755)
    root_original_mode = stat.S_IMODE(root_target.stat().st_mode)

    nested_dir = tmp_path / "sub"
    nested_dir.mkdir()
    nested_target = nested_dir / "file.py"
    nested_target.write_text("old\n", encoding="utf-8")
    nested_target.chmod(0o644)
    nested_original_mode = stat.S_IMODE(nested_target.stat().st_mode)

    vscode_lm_worker._write_atomic(tmp_path, "AGENTS.md", "new\n")
    vscode_lm_worker._write_atomic(tmp_path, "sub/file.py", "new\n")

    assert root_target.read_text(encoding="utf-8") == "new\n"
    assert nested_target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(root_target.stat().st_mode) == root_original_mode
    assert stat.S_IMODE(nested_target.stat().st_mode) == nested_original_mode


def test_root_write_failure_leaves_original_bytes_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("old\n", encoding="utf-8")

    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        raise OSError("forced fsync failure")

    monkeypatch.setattr(vscode_lm_worker.os, "fsync", failing_fsync)

    with pytest.raises(OSError):
        vscode_lm_worker._write_atomic(tmp_path, "AGENTS.md", "new\n")

    monkeypatch.setattr(vscode_lm_worker.os, "fsync", real_fsync)

    assert target.read_text(encoding="utf-8") == "old\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["AGENTS.md"]


def test_nested_write_failure_leaves_original_bytes_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    nested_dir = tmp_path / "sub"
    nested_dir.mkdir()
    target = nested_dir / "file.py"
    target.write_text("old\n", encoding="utf-8")

    real_replace = os.replace

    def failing_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("forced replace failure")

    monkeypatch.setattr(vscode_lm_worker.os, "replace", failing_replace)

    with pytest.raises(OSError):
        vscode_lm_worker._write_atomic(tmp_path, "sub/file.py", "new\n")

    monkeypatch.setattr(vscode_lm_worker.os, "replace", real_replace)

    assert target.read_text(encoding="utf-8") == "old\n"
    assert sorted(path.name for path in nested_dir.iterdir()) == ["file.py"]


def test_write_atomic_chmod_failure_preserves_original(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("old\n", encoding="utf-8")

    def failing_chmod(*_args: object, **_kwargs: object) -> None:
        raise OSError("forced chmod failure")

    monkeypatch.setattr(vscode_lm_worker.os, "chmod", failing_chmod)

    with pytest.raises(
        RuntimeError, match="bridge_output_chmod_failed:AGENTS.md"
    ):
        vscode_lm_worker._write_atomic(tmp_path, "AGENTS.md", "new\n")

    assert target.read_text(encoding="utf-8") == "old\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["AGENTS.md"]


def test_write_atomic_explicit_close_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("old\n", encoding="utf-8")
    real_close = os.close
    close_calls: list[int] = []

    def failing_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)
        raise OSError("forced close failure")

    monkeypatch.setattr(vscode_lm_worker.os, "close", failing_close)

    with pytest.raises(OSError, match="forced close failure"):
        vscode_lm_worker._write_atomic(tmp_path, "AGENTS.md", "new\n")

    assert len(close_calls) == 1
    assert target.read_text(encoding="utf-8") == "old\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["AGENTS.md"]


def test_nested_git_config_path_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with pytest.raises(RuntimeError):
        vscode_lm_worker._write_atomic(tmp_path, "sub/.git/config", "data\n")


def test_nested_output_keeps_atomic_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "docs" / "result.md"
    target.parent.mkdir()
    target.write_text("old\n", encoding="utf-8")
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def observed_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(vscode_lm_worker.os, "replace", observed_replace)
    vscode_lm_worker._write_atomic(tmp_path, "docs/result.md", "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert len(replacements) == 1
    assert replacements[0][0].parent == target.parent
    assert replacements[0][1] == target


def test_output_rejects_symlink_even_when_it_points_inside_workspace(tmp_path: Path) -> None:
    target = tmp_path / "real.md"
    target.write_text("real\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(target)

    with pytest.raises(RuntimeError, match="bridge_output_symlink:AGENTS.md"):
        vscode_lm_worker._write_atomic(tmp_path, "AGENTS.md", "forbidden\n")

    assert target.read_text(encoding="utf-8") == "real\n"


# Semantic edit V3 same-path multi-range regression tests.


def _make_v3_edit(
    workspace: Path,
    file_path: str,
    content: str,
    entries: list[dict[str, object]],
) -> dict[str, object]:
    """Create a minimal V3 edit payload for testing."""
    target = workspace / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    # Hash the same bytes that exist on disk. Path.write_text() performs
    # platform newline translation on Windows and would make the fixture's
    # LF-based digest stale before the worker sees it.
    target.write_bytes(content.encode("utf-8"))
    file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    edits = []
    for entry in entries:
        entry_hash = entry.get("current_sha256", file_hash)
        edits.append({
            "path": file_path,
            "current_sha256": entry_hash,
            "ranges": entry.get("ranges", []),
        })

    return {
        "schema_id": vscode_lm_worker.EDIT_RESPONSE_SCHEMA_ID,
        "edits": edits,
        "creates": [],
        "summary": "test",
    }


class TestV3SamePathMultiRange:
    """Tests for the same-path range-grouping normalization pipeline."""

    def test_disjoint_ranges_from_separate_entries_merge_and_apply(
        self, tmp_path: Path,
    ) -> None:
        """Two entries for the same file with disjoint ranges and same hash
        should merge successfully — this was previously rejected with
        ``vscode_lm_edit_response_duplicate_path``."""
        original = "line1\nline2\nline3\nline4\nline5\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 2, "end_line": 2, "new": "REPLACED_L2\n"},
                    ],
                },
                {
                    "ranges": [
                        {"start_line": 4, "end_line": 4, "new": "REPLACED_L4\n"},
                    ],
                },
            ],
        )

        planned, metrics = vscode_lm_worker._v3_planned_outputs(
            tmp_path, edit_payload, ["src/*.py"]
        )

        assert len(planned) == 1
        assert planned[0][0] == "src/module.py"
        assert planned[0][1] == "line1\nREPLACED_L2\nline3\nREPLACED_L4\nline5\n"
        assert len(metrics) == 1
        assert metrics[0]["path"] == "src/module.py"
        assert metrics[0]["entry_count"] == 2
        assert metrics[0]["range_count"] == 2

    def test_overlapping_ranges_across_entries_rejected(
        self, tmp_path: Path,
    ) -> None:
        """Overlapping ranges from separate entries for the same file
        must be rejected before any write occurs."""
        original = "line1\nline2\nline3\nline4\nline5\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 2, "end_line": 3, "new": "A\nB\n"},
                    ],
                },
                {
                    "ranges": [
                        {"start_line": 3, "end_line": 4, "new": "C\nD\n"},
                    ],
                },
            ],
        )

        with pytest.raises(
            RuntimeError, match="vscode_lm_semantic_edit_rejected"
        ) as exc_info:
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["src/*.py"]
            )
        # Overlap error propagated through from semantic_edit
        assert "overlap" in str(exc_info.value)
        assert "entry_index1=0:range_index1=0" in str(exc_info.value)
        assert "entry_index2=1:range_index2=0" in str(exc_info.value)

    def test_overlapping_ranges_report_actual_entry_provenance(
        self, tmp_path: Path,
    ) -> None:
        """Overlap evidence points at the conflicting range owners, not
        merely the first entry in the merged same-path group."""
        original = "line1\nline2\nline3\nline4\nline5\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 1, "end_line": 1, "new": "A\n"},
                    ],
                },
                {
                    "ranges": [
                        {"start_line": 4, "end_line": 5, "new": "D\nE\n"},
                    ],
                },
                {
                    "ranges": [
                        {"start_line": 3, "end_line": 4, "new": "C\nD2\n"},
                    ],
                },
            ],
        )

        with pytest.raises(RuntimeError) as exc_info:
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["src/*.py"]
            )

        message = str(exc_info.value)
        assert "vscode_lm_semantic_edit_rejected:src/module.py" in message
        assert "entry_index1=2:range_index1=0" in message
        assert "entry_index2=1:range_index2=0" in message
        assert "entry_index1=0" not in message

    def test_empty_ranges_rejected(
        self, tmp_path: Path,
    ) -> None:
        original = "line1\nline2\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [{"ranges": []}],
        )

        with pytest.raises(
            RuntimeError,
            match=r"vscode_lm_semantic_edit_ranges_invalid:src/module.py",
        ):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["src/*.py"]
            )

    def test_same_path_different_hash_rejected(
        self, tmp_path: Path,
    ) -> None:
        """Same path with a different original hash must fail-closed."""
        original = "line1\nline2\nline3\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 2, "end_line": 2, "new": "A\n"},
                    ],
                },
            ],
        )
        # Tamper with the second entry's hash
        edit_payload["edits"].append({  # type: ignore[attr-defined]
            "path": "src/module.py",
            "current_sha256": hashlib.sha256(b"different\n").hexdigest(),
            "ranges": [
                {"start_line": 3, "end_line": 3, "new": "B\n"},
            ],
        })

        with pytest.raises(
            RuntimeError, match="vscode_lm_edit_response_hash_conflict"
        ):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["src/*.py"]
            )

    def test_edit_and_create_same_path_rejected(
        self, tmp_path: Path,
    ) -> None:
        """A path appearing in both edits and creates is still rejected."""
        original = "line1\nline2\n"
        target = tmp_path / "src" / "module.py"
        target.parent.mkdir(parents=True)
        target.write_bytes(original.encode("utf-8"))
        file_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()

        edit_payload: dict[str, object] = {
            "schema_id": vscode_lm_worker.EDIT_RESPONSE_SCHEMA_ID,
            "edits": [{
                "path": "src/module.py",
                "current_sha256": file_hash,
                "ranges": [
                    {"start_line": 1, "end_line": 1, "new": "NEW\n"},
                ],
            }],
            "creates": [{
                "path": "src/module.py",
                "content": "overlap",
            }],
            "summary": "test",
        }

        with pytest.raises(
            RuntimeError, match="vscode_lm_edit_response_duplicate_path"
        ):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["src/*.py"]
            )

    def test_out_of_order_ranges_applied_deterministically(
        self, tmp_path: Path,
    ) -> None:
        """Ranges specified in non-monotonic order produce the same
        deterministic output regardless of entry order."""
        original = "A\nB\nC\nD\nE\n"

        def _run(entries: list[dict[str, object]]) -> str:
            edit_payload = _make_v3_edit(
                tmp_path, "f.txt", original, entries
            )
            planned, _metrics = vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["*.txt"]
            )
            return planned[0][1]

        # Reverse line order in ranges
        result1 = _run([{
            "ranges": [
                {"start_line": 4, "end_line": 4, "new": "D_new\n"},
                {"start_line": 2, "end_line": 2, "new": "B_new\n"},
            ],
        }])
        result2 = _run([{
            "ranges": [
                {"start_line": 2, "end_line": 2, "new": "B_new\n"},
                {"start_line": 4, "end_line": 4, "new": "D_new\n"},
            ],
        }])

        expected = "A\nB_new\nC\nD_new\nE\n"
        assert result1 == expected
        assert result2 == expected

    def test_unicode_and_newline_variants_survive_merge(
        self, tmp_path: Path,
    ) -> None:
        """UTF-8 content with varied newline conventions survives
        the same-path merge pipeline."""
        original = "αβγ\r\nδεζ\nηθι\r\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/uni.py",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 1, "end_line": 1, "new": "ΑΒΓ\r\n"},
                    ],
                },
                {
                    "ranges": [
                        {"start_line": 3, "end_line": 3, "new": "ΗΘΙ\r\n"},
                    ],
                },
            ],
        )

        planned, metrics = vscode_lm_worker._v3_planned_outputs(
            tmp_path, edit_payload, ["src/*.py"]
        )

        assert len(planned) == 1
        assert planned[0][1] == "ΑΒΓ\r\nδεζ\nΗΘΙ\r\n"
        assert metrics[0]["entry_count"] == 2

    def test_atomic_no_partial_write_on_invalid_range(
        self, tmp_path: Path,
    ) -> None:
        """When any range in the merged set is invalid the entire
        operation fails — no file is written."""
        original = "line1\nline2\nline3\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 1, "end_line": 1, "new": "OK\n"},
                    ],
                },
                {
                    "ranges": [
                        # Out-of-bounds range invalidates the batch
                        {"start_line": 99, "end_line": 99, "new": "BAD\n"},
                    ],
                },
            ],
        )

        with pytest.raises(RuntimeError, match="vscode_lm_semantic_edit_rejected"):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["src/*.py"]
            )

        # File must remain unchanged
        target = tmp_path / "src" / "module.py"
        assert target.read_text(encoding="utf-8") == original

    def test_single_entry_multiple_ranges_still_works(
        self, tmp_path: Path,
    ) -> None:
        """Non-regression: a single entry with multiple ranges
        (the pre-existing code path) must still work correctly."""
        original = "A\nB\nC\nD\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "f.txt",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 2, "end_line": 2, "new": "B_new\n"},
                        {"start_line": 4, "end_line": 4, "new": "D_new\n"},
                    ],
                },
            ],
        )

        planned, metrics = vscode_lm_worker._v3_planned_outputs(
            tmp_path, edit_payload, ["*.txt"]
        )

        assert planned[0][1] == "A\nB_new\nC\nD_new\n"
        assert metrics[0]["entry_count"] == 1
        assert metrics[0]["range_count"] == 2

    def test_duplicate_identical_ranges_rejected_as_overlap(
        self, tmp_path: Path,
    ) -> None:
        """Two entries that specify the exact same line range are
        rejected as overlap — no silent deduplication."""
        original = "line1\nline2\nline3\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 2, "end_line": 2, "new": "A\n"},
                    ],
                },
                {
                    "ranges": [
                        {"start_line": 2, "end_line": 2, "new": "B\n"},
                    ],
                },
            ],
        )

        with pytest.raises(
            RuntimeError, match="vscode_lm_semantic_edit_rejected"
        ) as exc_info:
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["src/*.py"]
            )
        assert "overlap" in str(exc_info.value)

    def test_stale_file_hash_rejected_during_apply(
        self, tmp_path: Path,
    ) -> None:
        """If the file was modified between the model's snapshot and
        apply time, the stale hash is detected and rejected."""
        original = "line1\nline2\nline3\n"
        target = tmp_path / "src" / "module.py"
        target.parent.mkdir(parents=True)
        target.write_bytes(original.encode("utf-8"))

        # Use a hash that doesn't match the actual file
        stale_hash = hashlib.sha256(b"something else\n").hexdigest()
        edit_payload: dict[str, object] = {
            "schema_id": vscode_lm_worker.EDIT_RESPONSE_SCHEMA_ID,
            "edits": [{
                "path": "src/module.py",
                "current_sha256": stale_hash,
                "ranges": [
                    {"start_line": 1, "end_line": 1, "new": "X\n"},
                ],
            }],
            "creates": [],
            "summary": "test",
        }

        with pytest.raises(
            RuntimeError, match="vscode_lm_edit_response_stale_hash"
        ):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["src/*.py"]
            )

        assert target.read_text(encoding="utf-8") == original

    def test_fragment_sha256_verification_preserved(
        self, tmp_path: Path,
    ) -> None:
        """fragment_sha256 validation in apply_line_ranges is preserved
        through the merge pipeline."""
        original = "line1\nline2\nline3\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [
                {
                    "ranges": [
                        {
                            "start_line": 2,
                            "end_line": 2,
                            "new": "REPLACED\n",
                            "fragment_sha256": hashlib.sha256(
                                b"line2\n"
                            ).hexdigest(),
                        },
                    ],
                },
            ],
        )

        planned, metrics = vscode_lm_worker._v3_planned_outputs(
            tmp_path, edit_payload, ["src/*.py"]
        )

        assert planned[0][1] == "line1\nREPLACED\nline3\n"
        assert metrics[0]["entry_count"] == 1

    def test_entry_count_reflects_original_entry_count(
        self, tmp_path: Path,
    ) -> None:
        """The ``entry_count`` metric must truthfully report how many
        original edit entries were merged for each file."""
        original = "A\nB\nC\nD\nE\nF\nG\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "f.txt",
            original,
            [
                {
                    "ranges": [
                        {"start_line": 2, "end_line": 2, "new": "b\n"},
                    ],
                },
                {
                    "ranges": [
                        {"start_line": 4, "end_line": 4, "new": "d\n"},
                    ],
                },
                {
                    "ranges": [
                        {"start_line": 6, "end_line": 6, "new": "f\n"},
                    ],
                },
            ],
        )

        _planned, metrics = vscode_lm_worker._v3_planned_outputs(
            tmp_path, edit_payload, ["*.txt"]
        )

        assert len(metrics) == 1
        assert metrics[0]["entry_count"] == 3
        assert metrics[0]["range_count"] == 3

    def test_final_hash_matches_written_bytes_for_provider_side_apply(
        self, tmp_path: Path,
    ) -> None:
        """Planned output bytes for a same-path merged edit must hash to
        exactly what will be written to disk, so a caller computing
        final_sha256/final_bytes from the planned content and tagging the
        apply as provider-side never falsely implies an MCP receipt."""
        original = "line1\nline2\nline3\n"
        edit_payload = _make_v3_edit(
            tmp_path,
            "src/module.py",
            original,
            [
                {"ranges": [{"start_line": 1, "end_line": 1, "new": "ONE\n"}]},
                {"ranges": [{"start_line": 3, "end_line": 3, "new": "THREE\n"}]},
            ],
        )

        planned, metrics = vscode_lm_worker._v3_planned_outputs(
            tmp_path, edit_payload, ["src/*.py"]
        )

        assert len(planned) == 1
        relative, content = planned[0]
        expected = "ONE\nline2\nTHREE\n"
        assert content == expected
        expected_hash = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        assert hashlib.sha256(content.encode("utf-8")).hexdigest() == expected_hash
        assert metrics[0]["path"] == relative
        assert metrics[0]["entry_count"] == 2


class TestRunSpecPathIntegration:
    """End-to-end run(spec_path) tests for multi-range same-file edits."""

    def _make_spec_and_response(
        self,
        tmp_path: Path,
        original: str,
        file_path: str,
        entries: list[dict[str, object]],
        request_id: str,
        allowed_writes: list[str],
        *,
        spec_extra: dict[str, object] | None = None,
        response_extra: dict[str, object] | None = None,
    ) -> tuple[Path, Path]:
        """Pre-write workspace, response JSON, and spec JSON."""
        import json

        workspace = tmp_path / "ws"
        workspace.mkdir()
        target = workspace / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.encode("utf-8"))
        file_hash = hashlib.sha256(
            original.encode("utf-8")
        ).hexdigest()
        edits = []
        for entry in entries:
            edits.append({
                "path": file_path,
                "current_sha256": entry.get(
                    "current_sha256", file_hash
                ),
                "ranges": entry.get("ranges", []),
            })
        edit_payload = {
            "schema_id": vscode_lm_worker.EDIT_RESPONSE_SCHEMA_ID,
            "edits": edits,
            "creates": [],
            "summary": "integration test",
        }
        response = {
            "schema_id": vscode_lm_worker.RESPONSE_SCHEMA_ID,
            "request_id": request_id,
            "text": json.dumps(edit_payload),
            **(response_extra or {}),
        }
        response_path = tmp_path / "response.json"
        response_path.write_text(
            json.dumps(response), encoding="utf-8"
        )
        spec = {
            "schema_id": "aiworkhub.vscode_lm.worker_spec.v1",
            "workspace_path": str(workspace),
            "response_path": str(response_path),
            "request_id": request_id,
            "allowed_writes": allowed_writes,
            **(spec_extra or {}),
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(
            json.dumps(spec), encoding="utf-8"
        )
        return spec_path, target

    def test_run_duplicate_same_path_disjoint_entries(
        self, tmp_path: Path,
    ) -> None:
        """run(spec_path) merges two same-path entries with disjoint
        ranges and tags apply as provider-side without MCP receipt."""
        spec_path, target = self._make_spec_and_response(
            tmp_path,
            "line1\nline2\nline3\nline4\nline5\n",
            "src/mod.py",
            [
                {"ranges": [{
                    "start_line": 2, "end_line": 2,
                    "new": "L2\n",
                }]},
                {"ranges": [{
                    "start_line": 4, "end_line": 4,
                    "new": "L4\n",
                }]},
            ],
            "req-dup",
            ["src/*.py"],
        )

        result = vscode_lm_worker.run(spec_path)

        assert result["is_error"] is False
        assert result["changed_paths"] == ["src/mod.py"]
        metric = result["semantic_edit_metrics"][0]
        assert metric["apply_surface"] == (
            "vscode_lm_worker_provider_side"
        )
        assert metric["mcp_receipt"] is None
        assert metric["entry_count"] == 2
        assert (
            target.read_text(encoding="utf-8")
            == "line1\nL2\nline3\nL4\nline5\n"
        )

    def test_run_byte_identical_edit_is_truthful_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec_path, target = self._make_spec_and_response(
            tmp_path,
            "line1\nline2\nline3\n",
            "src/mod.py",
            [{"ranges": [{
                "start_line": 2, "end_line": 2,
                "new": "line2\n",
            }]}],
            "req-no-op",
            ["src/*.py"],
        )

        def _unexpected_write(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("byte-identical edit must not rewrite the file")

        monkeypatch.setattr(vscode_lm_worker, "_write_atomic", _unexpected_write)
        result = vscode_lm_worker.run(spec_path)

        assert result["is_error"] is False
        assert result["changed_paths"] == []
        assert result["semantic_edit_metrics"][0]["no_op"] is True
        assert target.read_text(encoding="utf-8") == "line1\nline2\nline3\n"

    def test_run_consolidated_disjoint_ranges(
        self, tmp_path: Path,
    ) -> None:
        """run(spec_path) succeeds with a single entry containing
        multiple disjoint ranges (corruption regression)."""
        spec_path, target = self._make_spec_and_response(
            tmp_path,
            "A\nB\nC\nD\nE\n",
            "config.txt",
            [{"ranges": [
                {"start_line": 2, "end_line": 2, "new": "B2\n"},
                {"start_line": 4, "end_line": 4, "new": "D2\n"},
            ]}],
            "req-cons",
            ["*.txt"],
        )

        result = vscode_lm_worker.run(spec_path)

        assert result["is_error"] is False
        assert result["changed_paths"] == ["config.txt"]
        metric = result["semantic_edit_metrics"][0]
        assert metric["apply_surface"] == (
            "vscode_lm_worker_provider_side"
        )
        assert metric["mcp_receipt"] is None
        assert (
            target.read_text(encoding="utf-8")
            == "A\nB2\nC\nD2\nE\n"
        )

    def test_run_invalid_later_range_zero_mutation(
        self, tmp_path: Path,
    ) -> None:
        """run(spec_path) with an invalid second range raises and
        leaves the file completely unchanged."""
        original = "one\ntwo\nthree\n"
        spec_path, target = self._make_spec_and_response(
            tmp_path,
            original,
            "app.txt",
            [
                {"ranges": [{
                    "start_line": 1, "end_line": 1,
                    "new": "ONE\n",
                }]},
                {"ranges": [{
                    "start_line": 99, "end_line": 99,
                    "new": "BAD\n",
                }]},
            ],
            "req-bad",
            ["*.txt"],
        )

        with pytest.raises(
            RuntimeError, match="vscode_lm_semantic_edit_rejected"
        ):
            vscode_lm_worker.run(spec_path)

        assert target.read_text(encoding="utf-8") == original

    _ATTEMPT_ABSENT = object()
    _ATTEMPT_REQUEST_ID = "req-attempt"
    _ATTEMPT_REPO_ID = "repo_" + "7" * 32
    _ATTEMPT_MODEL = "glm-5.2"

    @classmethod
    def _host_receipt(cls, **overrides: object) -> dict[str, object]:
        receipt: dict[str, object] = {
            "schema_id": vscode_lm_worker.REASONING_CONTEXT_ATTEMPT_SCHEMA_ID,
            "request_id": cls._ATTEMPT_REQUEST_ID,
            "repo_id": cls._ATTEMPT_REPO_ID,
            "requested_model": cls._ATTEMPT_MODEL,
            "host_model": {
                "id": "glm-5.2",
                "family": "glm-5.2",
                "name": "GLM-5.2",
                "vendor": "customendpoint",
                "version": "1.0.0",
            },
            "requested_profile": "canonical_high",
            "send_state": "sent",
            "send_turn_count": 2,
            "provider_request_acknowledged": True,
            "option_status": "applied",
            "option_key": "reasoningEffort",
            "option_value": "high",
            "context_capacity_tokens": 128000,
            "context_capacity_source": "model.maxInputTokens",
            "provider_internal_state": "unknown",
            "unknown_reason": None,
        }
        receipt.update(overrides)
        return receipt

    @staticmethod
    def _unexpected_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("receipt handling must not depend on writing files")

    def _attempt_spec(self) -> dict[str, object]:
        return {
            "request_id": self._ATTEMPT_REQUEST_ID,
            "repo_id": self._ATTEMPT_REPO_ID,
            "model": self._ATTEMPT_MODEL,
        }

    def _run_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        receipt: object = _ATTEMPT_ABSENT,
        spec_extra: dict[str, object] | None = None,
        response_extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        response_fields = dict(response_extra or {})
        if receipt is not self._ATTEMPT_ABSENT:
            response_fields["reasoning_context_attempt"] = receipt
        spec_path, _target = self._make_spec_and_response(
            tmp_path,
            "line1\nline2\nline3\n",
            "src/mod.py",
            [{"ranges": [{"start_line": 2, "end_line": 2, "new": "line2\n"}]}],
            self._ATTEMPT_REQUEST_ID,
            ["src/*.py"],
            spec_extra=(
                self._attempt_spec() if spec_extra is None else spec_extra
            ),
            response_extra=response_fields,
        )
        monkeypatch.setattr(vscode_lm_worker, "_write_atomic", self._unexpected_write)
        result = vscode_lm_worker.run(spec_path)
        assert result["is_error"] is False
        assert result["changed_paths"] == []
        return result

    def _assert_typed_unknown(self, attempt: object, reason: str) -> None:
        assert isinstance(attempt, dict)
        assert attempt["schema_id"] == vscode_lm_worker.REASONING_CONTEXT_ATTEMPT_SCHEMA_ID
        assert attempt["send_state"] == "unknown"
        assert attempt["option_status"] == "unknown"
        assert attempt["unknown_reason"] == reason
        assert reason in vscode_lm_worker._ATTEMPT_WORKER_UNKNOWN_REASONS
        assert attempt["option_key"] is None
        assert attempt["option_value"] is None
        assert attempt["provider_internal_state"] == "unknown"
        assert attempt["request_id"] == self._ATTEMPT_REQUEST_ID
        assert attempt["repo_id"] == self._ATTEMPT_REPO_ID
        assert attempt["requested_model"] == self._ATTEMPT_MODEL

    def test_run_forwards_well_formed_host_receipt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        receipt = self._host_receipt()

        result = self._run_attempt(tmp_path, monkeypatch, receipt=receipt)

        assert result["reasoning_context_attempt"] == receipt

    @pytest.mark.parametrize(
        "overrides",
        [
            {"option_status": "unsupported", "option_key": None, "option_value": None},
            {"option_status": "provider_default", "option_key": None, "option_value": None},
            {"option_status": "unverifiable", "option_key": None, "option_value": None},
            {"option_status": "capability_ceiling", "option_key": None, "option_value": None},
            {
                "option_status": "unknown",
                "option_key": None,
                "option_value": None,
                "unknown_reason": "option_changed_between_turns",
            },
            {"context_capacity_tokens": None, "context_capacity_source": "unknown"},
            {"context_capacity_source": "request.model_context"},
            {"host_model": {"id": None, "family": None, "name": None, "vendor": None, "version": None}},
            {"requested_profile": "canonical_maximum"},
            {"requested_profile": "canonical_medium_high"},
            {"send_turn_count": 64},
            {
                "option_status": "unknown",
                "option_key": None,
                "option_value": None,
                "unknown_reason": "option_shape_unrecognized",
            },
            {
                "option_status": "unknown",
                "option_key": None,
                "option_value": None,
                "unknown_reason": "sent_option_disagrees_with_effort_status",
            },
            {
                "option_status": "unknown",
                "option_key": None,
                "option_value": None,
                "send_turn_count": 64,
                "unknown_reason": "send_turn_count_out_of_bounds",
            },
            {
                "option_status": "unknown",
                "option_key": None,
                "option_value": None,
                "unknown_reason": "receipt_recorder_error",
            },
        ],
    )
    def test_run_forwards_host_typed_states_without_upgrading_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object],
    ) -> None:
        receipt = self._host_receipt(**overrides)

        result = self._run_attempt(tmp_path, monkeypatch, receipt=receipt)

        assert result["reasoning_context_attempt"] == receipt

    def test_run_marks_absent_receipt_as_typed_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run_attempt(tmp_path, monkeypatch)

        self._assert_typed_unknown(result["reasoning_context_attempt"], "receipt_absent")

    def test_run_marks_null_receipt_as_typed_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run_attempt(tmp_path, monkeypatch, receipt=None)

        self._assert_typed_unknown(result["reasoning_context_attempt"], "receipt_absent")

    def test_run_never_verifies_a_receipt_against_an_unpinned_spec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run_attempt(
            tmp_path,
            monkeypatch,
            receipt=self._host_receipt(),
            spec_extra={"repo_id": self._ATTEMPT_REPO_ID},
        )

        attempt = result["reasoning_context_attempt"]
        assert isinstance(attempt, dict)
        assert attempt["send_state"] == "unknown"
        assert attempt["unknown_reason"] == "receipt_identity_mismatch"
        assert attempt["requested_model"] is None

    def test_run_receipt_with_non_finite_capacity_is_typed_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run_attempt(
            tmp_path,
            monkeypatch,
            receipt=self._host_receipt(context_capacity_tokens=float("nan")),
        )

        self._assert_typed_unknown(result["reasoning_context_attempt"], "receipt_bounds_invalid")

    def test_run_receipt_for_a_foreign_request_is_typed_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        foreign = self._host_receipt(request_id="req-stale")

        result = self._run_attempt(tmp_path, monkeypatch, receipt=foreign)

        self._assert_typed_unknown(
            result["reasoning_context_attempt"], "receipt_identity_mismatch",
        )
        assert "req-stale" not in json.dumps(result["reasoning_context_attempt"])

    def test_run_terminal_error_still_raises_without_leaking_the_receipt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec_path, _target = self._make_spec_and_response(
            tmp_path,
            "line1\nline2\nline3\n",
            "src/mod.py",
            [{"ranges": [{"start_line": 2, "end_line": 2, "new": "line2\n"}]}],
            self._ATTEMPT_REQUEST_ID,
            ["src/*.py"],
            spec_extra=self._attempt_spec(),
            response_extra={
                "error": "provider_boom",
                "reasoning_context_attempt": self._host_receipt(
                    provider_request_acknowledged=False,
                ),
            },
        )
        monkeypatch.setattr(vscode_lm_worker, "_write_atomic", self._unexpected_write)

        with pytest.raises(RuntimeError) as failure:
            vscode_lm_worker.run(spec_path)

        assert str(failure.value) == "vscode_lm_request_failed:provider_boom"

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"schema_id": "aiworkhub.reasoning_context_attempt.v0"}, "receipt_schema_mismatch"),
            ({"request_id": "req-other"}, "receipt_identity_mismatch"),
            ({"repo_id": "repo_" + "8" * 32}, "receipt_identity_mismatch"),
            ({"requested_model": "deepseek-v4-pro"}, "receipt_identity_mismatch"),
            ({"requested_model": None}, "receipt_identity_mismatch"),
            ({"send_state": "acknowledged"}, "receipt_vocabulary_invalid"),
            ({"option_status": "honored"}, "receipt_vocabulary_invalid"),
            ({"requested_profile": "ultra"}, "receipt_vocabulary_invalid"),
            ({"context_capacity_source": "token_budget"}, "receipt_vocabulary_invalid"),
            ({"provider_internal_state": "high"}, "receipt_vocabulary_invalid"),
            (
                {
                    "option_status": "unknown",
                    "option_key": None,
                    "option_value": None,
                    "unknown_reason": "because",
                },
                "receipt_vocabulary_invalid",
            ),
            ({"context_capacity_tokens": -1}, "receipt_bounds_invalid"),
            ({"context_capacity_tokens": 0}, "receipt_bounds_invalid"),
            ({"context_capacity_tokens": float("nan")}, "receipt_bounds_invalid"),
            ({"context_capacity_tokens": float("inf")}, "receipt_bounds_invalid"),
            ({"context_capacity_tokens": 128000.5}, "receipt_bounds_invalid"),
            ({"context_capacity_tokens": True}, "receipt_bounds_invalid"),
            ({"context_capacity_tokens": 2**60}, "receipt_bounds_invalid"),
            ({"send_turn_count": -1}, "receipt_bounds_invalid"),
            ({"send_turn_count": True}, "receipt_bounds_invalid"),
            ({"send_turn_count": 2.0}, "receipt_bounds_invalid"),
            ({"send_turn_count": 65}, "receipt_bounds_invalid"),
            ({"option_value": "h" * 65}, "receipt_bounds_invalid"),
            ({"option_key": "reasoning\nEffort"}, "receipt_bounds_invalid"),
            ({"option_value": 5}, "receipt_bounds_invalid"),
            (
                {
                    "host_model": {
                        "id": "glm-5.2",
                        "family": "glm-5.2",
                        "name": "n" * 129,
                        "vendor": "customendpoint",
                        "version": "1.0.0",
                    },
                },
                "receipt_bounds_invalid",
            ),
            (
                {
                    "host_model": {
                        "id": "glm-5.2",
                        "family": 42,
                        "name": "GLM-5.2",
                        "vendor": "customendpoint",
                        "version": "1.0.0",
                    },
                },
                "receipt_malformed",
            ),
            ({"host_model": {"id": "glm-5.2"}}, "receipt_malformed"),
            ({"host_model": "glm-5.2"}, "receipt_malformed"),
            ({"provider_request_acknowledged": "yes"}, "receipt_malformed"),
            ({"option_value": "h" * 10_000}, "receipt_oversized"),
            ({"option_key": None, "option_value": None}, "receipt_inconsistent"),
            ({"option_value": None}, "receipt_inconsistent"),
            ({"option_status": "unsupported"}, "receipt_inconsistent"),
            ({"option_status": "provider_default"}, "receipt_inconsistent"),
            ({"send_turn_count": 0}, "receipt_inconsistent"),
            ({"provider_request_acknowledged": False}, "receipt_inconsistent"),
            (
                {"option_status": "unknown", "option_key": None, "option_value": None},
                "receipt_inconsistent",
            ),
            ({"unknown_reason": "option_changed_between_turns"}, "receipt_inconsistent"),
            # A success response proves a send happened, so a not_sent receipt contradicts it.
            (
                {
                    "send_state": "not_sent",
                    "send_turn_count": 0,
                    "provider_request_acknowledged": False,
                    "option_status": "not_sent",
                    "option_key": None,
                    "option_value": None,
                },
                "receipt_inconsistent",
            ),
            (
                {"send_state": "not_sent", "send_turn_count": 0, "provider_request_acknowledged": False},
                "receipt_inconsistent",
            ),
            ({"option_status": "not_sent"}, "receipt_inconsistent"),
            ({"context_capacity_tokens": None}, "receipt_inconsistent"),
            ({"context_capacity_source": "unknown"}, "receipt_inconsistent"),
            ({"send_state": ["sent"]}, "receipt_malformed"),
            ({"option_status": None}, "receipt_malformed"),
            ({"unknown_reason": ["option_changed_between_turns"]}, "receipt_malformed"),
            ({"schema_id": None}, "receipt_schema_mismatch"),
            ({"unknown_reason": "u" * 5000}, "receipt_oversized"),
        ],
    )
    def test_invalid_host_receipt_is_never_forwarded_as_applied(
        self, overrides: dict[str, object], reason: str,
    ) -> None:
        receipt = self._host_receipt(**overrides)

        attempt = vscode_lm_worker._reasoning_context_attempt_result(
            {"reasoning_context_attempt": receipt}, self._attempt_spec(),
        )

        self._assert_typed_unknown(attempt, reason)

    @pytest.mark.parametrize(
        ("receipt", "reason"),
        [
            ("applied", "receipt_malformed"),
            ([], "receipt_malformed"),
            (7, "receipt_malformed"),
            ({}, "receipt_malformed"),
            ({"schema_id": vscode_lm_worker.REASONING_CONTEXT_ATTEMPT_SCHEMA_ID}, "receipt_malformed"),
        ],
    )
    def test_non_object_or_partial_receipt_is_typed_unknown(
        self, receipt: object, reason: str,
    ) -> None:
        attempt = vscode_lm_worker._reasoning_context_attempt_result(
            {"reasoning_context_attempt": receipt}, self._attempt_spec(),
        )

        self._assert_typed_unknown(attempt, reason)

    def test_receipt_with_an_unknown_extra_key_is_typed_unknown(self) -> None:
        receipt = self._host_receipt()
        receipt["provider_text"] = "arbitrary model prose"

        attempt = vscode_lm_worker._reasoning_context_attempt_result(
            {"reasoning_context_attempt": receipt}, self._attempt_spec(),
        )

        self._assert_typed_unknown(attempt, "receipt_malformed")
        assert "arbitrary model prose" not in json.dumps(attempt)

    def test_verified_receipt_is_rebuilt_from_validated_scalars(self) -> None:
        receipt = self._host_receipt()

        attempt = vscode_lm_worker._reasoning_context_attempt_result(
            {"reasoning_context_attempt": receipt}, self._attempt_spec(),
        )

        assert attempt == receipt
        assert attempt is not receipt
        assert attempt["host_model"] is not receipt["host_model"]


class TestV3UnsupportedTopLevel:
    """v3 rejects unsupported top-level keys rather than ignoring."""

    def test_v3_rejects_empty_deletes_key_presence(
        self, tmp_path: Path,
    ) -> None:
        original = "a\nb\n"
        edit_payload = _make_v3_edit(
            tmp_path, "f.txt", original,
            [{"ranges": [{
                "start_line": 1, "end_line": 1, "new": "A\n",
            }]}],
        )
        edit_payload["deletes"] = []

        with pytest.raises(
            RuntimeError,
            match=r"vscode_lm_edit_response_unsupported_top_level:"
            r"deletes",
        ):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["*.txt"]
            )

    def test_v3_rejects_empty_files_key_presence(
        self, tmp_path: Path,
    ) -> None:
        original = "a\nb\n"
        edit_payload = _make_v3_edit(
            tmp_path, "f.txt", original,
            [{"ranges": [{
                "start_line": 1, "end_line": 1, "new": "A\n",
            }]}],
        )
        edit_payload["files"] = {}

        with pytest.raises(
            RuntimeError,
            match=r"vscode_lm_edit_response_unsupported_top_level:"
            r"files",
        ):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["*.txt"]
            )

    def test_v3_allows_legacy_key_only_when_none(
        self, tmp_path: Path,
    ) -> None:
        original = "a\nb\n"
        edit_payload = _make_v3_edit(
            tmp_path, "f.txt", original,
            [{"ranges": [{
                "start_line": 1, "end_line": 1, "new": "A\n",
            }]}],
        )
        edit_payload["files"] = None

        planned, _metrics = vscode_lm_worker._v3_planned_outputs(
            tmp_path, edit_payload, ["*.txt"]
        )

        assert planned[0][1] == "A\nb\n"

    def test_v3_rejects_nonempty_deletes(
        self, tmp_path: Path,
    ) -> None:
        original = "a\nb\n"
        edit_payload = _make_v3_edit(
            tmp_path, "f.txt", original,
            [{"ranges": [{
                "start_line": 1, "end_line": 1, "new": "A\n",
            }]}],
        )
        edit_payload["deletes"] = ["f.txt"]

        with pytest.raises(
            RuntimeError,
            match=r"vscode_lm_edit_response_unsupported_top_level:"
            r"deletes",
        ):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["*.txt"]
            )

    def test_v3_rejects_nonempty_replacements(
        self, tmp_path: Path,
    ) -> None:
        original = "a\nb\n"
        edit_payload = _make_v3_edit(
            tmp_path, "f.txt", original,
            [{"ranges": [{
                "start_line": 1, "end_line": 1, "new": "A\n",
            }]}],
        )
        edit_payload["replacements"] = [
            {"old": "a", "new": "b"}
        ]

        with pytest.raises(
            RuntimeError,
            match=r"vscode_lm_edit_response_unsupported_top_level:"
            r"replacements",
        ):
            vscode_lm_worker._v3_planned_outputs(
                tmp_path, edit_payload, ["*.txt"]
            )


@pytest.mark.parametrize(
    "raw",
    [
        ".Git/config",
        ".GIT/config",
        "nested/.Git/config",
        "nested/.GIT/hooks/pre-commit",
    ],
)
def test_relative_path_rejects_mixed_case_git_component(raw: str) -> None:
    with pytest.raises(RuntimeError, match="bridge_output_path_escape:"):
        vscode_lm_worker._relative_path(raw)

def test_write_atomic_preserves_exact_bytes_no_newline_translation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path
    relative = "test_file.txt"
    content = "line1\nline2\nline3\r\nline4\r\n"
    expected_bytes = content.encode("utf-8")
    expected_hash = hashlib.sha256(expected_bytes).hexdigest()
    vscode_lm_worker._write_atomic(workspace, relative, content)
    target = workspace / relative
    assert target.exists()
    actual_bytes = target.read_bytes()
    assert actual_bytes == expected_bytes
    actual_hash = hashlib.sha256(actual_bytes).hexdigest()
    assert actual_hash == expected_hash
    assert actual_bytes.decode('utf-8') == content


def _strict_terminal_fixture(
    tmp_path: Path,
    *,
    request_id: str = "f" * 32,
) -> tuple[Path, Path, str, str]:
    workspace = tmp_path / "strict" / "worktree"
    home = tmp_path / "strict" / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    response_path = home / ".aiworkhub_vscode_lm_response.json"
    token = "a" * 64
    repo_id = "repo_" + "b" * 32
    spec_path = home / ".aiworkhub_vscode_lm_worker.json"
    vscode_lm_bridge._atomic_json(  # noqa: SLF001 - exact worker contract
        spec_path,
        {
            "schema_id": "aiworkhub.vscode_lm.worker_spec.v1",
            "request_id": request_id,
            "repo_id": repo_id,
            "workspace_path": str(workspace),
            "response_path": str(response_path),
            "cancel_path": str(response_path),
            "cancel_token": token,
            "terminal_decision_required": True,
            "timeout_seconds": 30,
            "allowed_writes": [],
        },
    )
    return spec_path, response_path, repo_id, token


def _strict_provider_response(
    *, request_id: str, repo_id: str, token: str,
) -> dict[str, object]:
    return {
        "schema_id": vscode_lm_worker.RESPONSE_SCHEMA_ID,
        "request_id": request_id,
        "repo_id": repo_id,
        "model": {"id": "glm-5.2"},
        "text": json.dumps({
            "schema_id": vscode_lm_worker.EDIT_RESPONSE_SCHEMA_ID,
            "summary": "read-only complete",
            "edits": [],
            "creates": [],
        }),
        "error": "",
        "decision": {"action": "response", "cancel_token": token},
    }


def test_worker_requires_exact_token_bound_response_decision(tmp_path: Path) -> None:
    request_id = "f" * 32
    spec_path, response_path, repo_id, token = _strict_terminal_fixture(
        tmp_path, request_id=request_id,
    )
    forged = _strict_provider_response(
        request_id=request_id, repo_id=repo_id, token=token,
    )
    forged["decision"] = {"action": "response", "cancel_token": "0" * 64}
    vscode_lm_bridge._atomic_json(response_path, forged)  # noqa: SLF001

    with pytest.raises(RuntimeError, match="terminal_decision_contract_mismatch"):
        vscode_lm_worker.run(spec_path)

    assert response_path.is_file()


def test_worker_consumes_durable_response_decision_without_deleting_it(
    tmp_path: Path,
) -> None:
    request_id = "e" * 32
    spec_path, response_path, repo_id, token = _strict_terminal_fixture(
        tmp_path, request_id=request_id,
    )
    response = _strict_provider_response(
        request_id=request_id, repo_id=repo_id, token=token,
    )
    vscode_lm_bridge._atomic_json(response_path, response)  # noqa: SLF001

    result = vscode_lm_worker.run(spec_path)

    assert result["is_error"] is False
    assert result["changed_paths"] == []
    assert json.loads(response_path.read_text(encoding="utf-8")) == response


def test_worker_rejects_cancel_won_decision_without_writes(tmp_path: Path) -> None:
    request_id = "d" * 32
    spec_path, response_path, repo_id, token = _strict_terminal_fixture(
        tmp_path, request_id=request_id,
    )
    cancel = {
        "schema_id": vscode_lm_worker.RESPONSE_SCHEMA_ID,
        "request_id": request_id,
        "repo_id": repo_id,
        "model": {},
        "text": "",
        "error": "vscode_lm_request_cancelled",
        "decision": {"action": "cancel", "cancel_token": token},
    }
    vscode_lm_bridge._atomic_json(response_path, cancel)  # noqa: SLF001

    with pytest.raises(
        RuntimeError,
        match="vscode_lm_request_failed:vscode_lm_request_cancelled",
    ):
        vscode_lm_worker.run(spec_path)

    assert response_path.is_file()
