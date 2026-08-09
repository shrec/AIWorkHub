from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiworkhub import vscode_lm_worker as worker


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "payload",
    [
        "TODO",
        "# FIXME",
        "implementation omitted",
        "rest of the code unchanged",
        "<new validate_required_outputs code>",
        "<new _handle_quality_review_launch code>",
        "<test file content>",
    ],
)
def test_explicit_non_substantive_markers_fail_with_byte_metrics(payload: str) -> None:
    with pytest.raises(
        RuntimeError,
        match=r"vscode_lm_edit_fidelity_rejected:.*old_bytes=2048:new_bytes=",
    ):
        worker._check_edit_fidelity(
            "x" * 2048,
            payload,
            path="src/app.py",
            operation="v2_replacement:0",
        )


@pytest.mark.parametrize(
    "payload",
    [
        "return x + 1",
        "See the docs... then continue.",
        "a < b and c > d",
        "List<T>",
        "<T>",
        "<Component />",
        "<Foo>bar</Foo>",
    ],
)
def test_valid_concise_payloads_are_not_false_positives(payload: str) -> None:
    receipt = worker._check_edit_fidelity(
        "x" * 2048,
        payload,
        path="src/app.tsx",
        operation="v3_range:0",
    )
    assert receipt["new_bytes"] == len(payload.encode("utf-8"))


def test_large_punctuation_only_shrink_rejected_but_v3_delete_allowed() -> None:
    old = "!" * 2048
    with pytest.raises(RuntimeError, match="suspicious_shrink_without_alphanumeric"):
        worker._check_edit_fidelity(
            old,
            "...",
            path="src/app.py",
            operation="v3_range:0",
        )
    assert worker._check_edit_fidelity(
        old,
        "",
        path="src/app.py",
        operation="v3_range:0",
    )["new_bytes"] == 0


def test_v1_and_create_payloads_use_the_same_gate(tmp_path: Path) -> None:
    existing = tmp_path / "src" / "app.py"
    existing.parent.mkdir()
    existing.write_text("x" * 2048, encoding="utf-8")
    with pytest.raises(RuntimeError, match="explicit_non_substantive_marker"):
        worker._v1_planned_outputs(
            tmp_path,
            {"files": [{"path": "src/app.py", "content": "TODO"}]},
            ["src/app.py"],
        )
    with pytest.raises(RuntimeError, match="explicit_non_substantive_marker"):
        worker._v2_planned_outputs(
            tmp_path,
            {"edits": [], "creates": [{"path": "new.py", "content": "placeholder"}]},
            ["new.py"],
        )


def test_v2_rejected_late_edit_cannot_partially_mutate(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("needle\n", encoding="utf-8")
    second.write_text("needle\n", encoding="utf-8")
    edit = {
        "edits": [
            {
                "path": "first.py",
                "current_sha256": _sha("needle\n"),
                "replacements": [
                    {"old": "needle", "new": "valid", "expected_count": 1}
                ],
            },
            {
                "path": "second.py",
                "current_sha256": _sha("needle\n"),
                "replacements": [
                    {"old": "needle", "new": "TODO", "expected_count": 1}
                ],
            },
        ],
        "creates": [],
    }
    with pytest.raises(RuntimeError, match="explicit_non_substantive_marker"):
        worker._v2_planned_outputs(tmp_path, edit, ["first.py", "second.py"])
    assert first.read_text(encoding="utf-8") == "needle\n"
    assert second.read_text(encoding="utf-8") == "needle\n"


def test_v3_ranges_and_creates_are_gated_and_delete_remains_valid(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    old = "x" * 2048 + "\n"
    target.write_text(old, encoding="utf-8")
    base = {"path": "app.py", "current_sha256": _sha(old)}
    with pytest.raises(RuntimeError, match="explicit_non_substantive_marker"):
        worker._v3_planned_outputs(
            tmp_path,
            {
                "edits": [{**base, "ranges": [{"start_line": 1, "end_line": 1, "new": "TODO"}]}],
                "creates": [],
            },
            ["app.py"],
        )
    planned, _metrics = worker._v3_planned_outputs(
        tmp_path,
        {
            "edits": [{**base, "ranges": [{"start_line": 1, "end_line": 1, "new": ""}]}],
            "creates": [],
        },
        ["app.py"],
    )
    assert planned == [("app.py", "")]
    with pytest.raises(RuntimeError, match="explicit_non_substantive_marker"):
        worker._v3_planned_outputs(
            tmp_path,
            {"edits": [], "creates": [{"path": "new.py", "content": "your code here"}]},
            ["new.py"],
        )
