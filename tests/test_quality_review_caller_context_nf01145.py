"""NF-2026-01145: caller_context reads changed-path callers from the candidate.

A caller that lives in a changed file used to be dropped without counting, so a
packet whose callers were all in the changed file said ``complete: true`` with
no rows.  These tests pin the new contract: such callers are read from the
candidate root (``source: candidate``), and no dropped callers entry can leave
``complete`` true.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import process_launcher, quality_reviewer

CHANGED = "src/core.py"


def _manager(canonical: Path) -> SimpleNamespace:
    cls = process_launcher.ProcessManager
    return SimpleNamespace(
        repo=canonical,
        _QUALITY_REVIEW_CALLER_CONTEXT_LINES=cls._QUALITY_REVIEW_CALLER_CONTEXT_LINES,
        _QUALITY_REVIEW_CALLER_CONTEXT_MAX_ROWS=cls._QUALITY_REVIEW_CALLER_CONTEXT_MAX_ROWS,
        _QUALITY_REVIEW_CALLER_CONTEXT_ROW_MAX_BYTES=(
            cls._QUALITY_REVIEW_CALLER_CONTEXT_ROW_MAX_BYTES
        ),
        _QUALITY_REVIEW_CALLER_CONTEXT_TOTAL_MAX_BYTES=(
            cls._QUALITY_REVIEW_CALLER_CONTEXT_TOTAL_MAX_BYTES
        ),
    )


def _scoped(*entries: Any) -> dict[str, Any]:
    return {"correctness": {"packet": {"impact_evidence": list(entries)}}}


def _caller(identity: str, path: str, line: Any) -> dict[str, Any]:
    return {
        "identity": identity,
        "evidence_kind": "callers",
        "path": path,
        "line_start": line,
    }


def _write_lines(root: Path, rel: str, prefix: str, count: int = 30) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        "".join(f"{prefix} {index}\n" for index in range(1, count + 1)).encode("utf-8")
    )


def _run(
    canonical: Path, scoped: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    return process_launcher.ProcessManager._quality_review_caller_context(
        _manager(canonical), {CHANGED: "a" * 64}, scoped, **kwargs
    )


def test_changed_file_callers_are_read_from_candidate(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _write_lines(canonical, CHANGED, "canonical")
    _write_lines(candidate, CHANGED, "candidate")

    context = _run(
        canonical,
        _scoped(_caller("callers:a", CHANGED, 12), _caller("callers:b", CHANGED, 20)),
        candidate_root=candidate,
    )

    assert context["complete"] is True
    assert context["omitted"] == 0
    assert [row["identity"] for row in context["rows"]] == ["callers:a", "callers:b"]
    first = context["rows"][0]
    assert first["source"] == "candidate"
    assert first["path"] == CHANGED
    assert (first["line"], first["line_start"], first["line_end"]) == (12, 7, 17)
    assert first["text"] == "".join(f"candidate {index}\n" for index in range(7, 18))


def test_candidate_root_accepts_str(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    _write_lines(candidate, CHANGED, "candidate")

    context = _run(
        canonical, _scoped(_caller("callers:a", CHANGED, 3)), candidate_root=str(candidate)
    )

    assert context["complete"] is True
    assert context["rows"][0]["source"] == "candidate"


def test_missing_candidate_root_counts_changed_caller_as_omitted(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    _write_lines(canonical, CHANGED, "canonical")

    context = _run(canonical, _scoped(_caller("callers:a", CHANGED, 12)))

    assert context == {"rows": [], "complete": False, "omitted": 1}


def test_unreadable_candidate_file_counts_as_omitted(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    (candidate / "src").mkdir(parents=True)
    # Not utf-8, shorter than the line, and an absent file: all unreadable.
    (candidate / CHANGED).write_bytes(b"\xff\xfe\x00bad\n")

    undecodable = _run(
        canonical, _scoped(_caller("callers:a", CHANGED, 1)), candidate_root=candidate
    )
    assert undecodable == {"rows": [], "complete": False, "omitted": 1}

    (candidate / CHANGED).write_text("only line\n", encoding="utf-8")
    short = _run(
        canonical, _scoped(_caller("callers:a", CHANGED, 50)), candidate_root=candidate
    )
    assert short == {"rows": [], "complete": False, "omitted": 1}

    (candidate / CHANGED).unlink()
    absent = _run(
        canonical, _scoped(_caller("callers:a", CHANGED, 1)), candidate_root=candidate
    )
    assert absent == {"rows": [], "complete": False, "omitted": 1}


@pytest.mark.parametrize(
    "entry",
    [
        _caller("", "src/other.py", 1),
        _caller("callers:x", "", 1),
        _caller("callers:x", "/abs/other.py", 1),
        _caller("callers:x", "../outside.py", 1),
        _caller("callers:x", "src/other.py", "3"),
        _caller("callers:x", "src/other.py", True),
        _caller("callers:x", "src/other.py", 0),
        _caller("callers:x", "src/other.py", None),
    ],
)
def test_malformed_callers_entry_makes_context_incomplete(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    canonical = tmp_path / "canonical"
    _write_lines(canonical, "src/other.py", "canonical")

    context = _run(canonical, _scoped(entry, _caller("callers:ok", "src/other.py", 5)))

    assert context["complete"] is False
    assert context["omitted"] >= 1
    assert [row["identity"] for row in context["rows"]] == ["callers:ok"]


def test_caller_outside_changed_paths_keeps_canonical_row(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _write_lines(canonical, "src/caller.py", "canonical")
    _write_lines(candidate, "src/caller.py", "candidate")

    context = _run(
        canonical, _scoped(_caller("callers:1", "src/caller.py", 12)), candidate_root=candidate
    )

    assert context == {
        "rows": [{
            "identity": "callers:1",
            "path": "src/caller.py",
            "line": 12,
            "line_start": 7,
            "line_end": 17,
            "source": "canonical",
            "text": "".join(f"canonical {index}\n" for index in range(7, 18)),
        }],
        "complete": True,
        "omitted": 0,
    }


def test_candidate_rows_respect_row_cap(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    _write_lines(candidate, CHANGED, "candidate", count=200)
    cap = process_launcher.ProcessManager._QUALITY_REVIEW_CALLER_CONTEXT_MAX_ROWS
    entries = [_caller(f"callers:{index:03d}", CHANGED, index + 1) for index in range(cap + 3)]

    context = _run(canonical, _scoped(*entries), candidate_root=candidate)

    assert len(context["rows"]) == cap
    assert context["complete"] is False
    assert context["omitted"] == 3


def _row(source: str) -> dict[str, Any]:
    return {
        "identity": "callers:1",
        "path": CHANGED,
        "line": 2,
        "line_start": 1,
        "line_end": 3,
        "source": source,
        "text": "a\nb\nc\n",
    }


def test_quality_reviewer_accepts_candidate_and_rejects_unknown_source() -> None:
    accepted = quality_reviewer._caller_context_rows(
        {"rows": [_row("candidate"), {**_row("canonical"), "identity": "callers:2"}],
         "complete": True, "omitted": 0}
    )
    assert [row["source"] for row in accepted["rows"]] == ["candidate", "canonical"]

    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        quality_reviewer._caller_context_rows(
            {"rows": [_row("model")], "complete": True, "omitted": 0}
        )


def test_candidate_read_never_escapes_through_linked_directory(tmp_path: Path) -> None:
    # The leaf is a regular file; only its parent directory links outside the
    # candidate root, which a leaf-only is_symlink() check misses.
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    outside = tmp_path / "outside"
    canonical.mkdir()
    candidate.mkdir()
    _write_lines(outside, "core.py", "outside")
    link = candidate / "src"
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(outside), str(link))
        else:
            link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory link unavailable: {exc}")

    context = _run(
        canonical, _scoped(_caller("callers:a", CHANGED, 3)), candidate_root=candidate
    )

    assert context == {"rows": [], "complete": False, "omitted": 1}
