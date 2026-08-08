from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import vscode_lm_worker  # noqa: E402


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
    target.write_text(content, encoding="utf-8")
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
        target.write_text(original, encoding="utf-8")
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
        target.write_text(original, encoding="utf-8")

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
    ) -> tuple[Path, Path]:
        """Pre-write workspace, response JSON, and spec JSON."""
        import json

        workspace = tmp_path / "ws"
        workspace.mkdir()
        target = workspace / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original, encoding="utf-8")
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
