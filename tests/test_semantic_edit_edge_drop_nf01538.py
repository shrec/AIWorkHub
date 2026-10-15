from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiworkhub import semantic_edit
from aiworkhub import worker_ai_tools_mcp as worker_tools

BS = chr(92)


def _session(tmp_path: Path, name: str, text: str):
    repo = tmp_path / "worktree"
    target = repo / "src" / name
    target.parent.mkdir(parents=True)
    target.write_bytes(text.encode("utf-8"))
    ctx = worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(f"src/{name}",),
        session_topic="topic",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        allowed_writes=("src/*",),
    )
    return worker_tools.WorkerSemanticEditSession(ctx), target


def _apply(session, path: str, start: int, end: int, new: str) -> dict:
    prepared = session.prepare(file_path=f"src/{path}", start_line=start, end_line=end)
    assert prepared["ok"] is True
    return session.apply(
        target_id=prepared["target_id"], new=new, idempotency_key=f"k-{start}-{end}"
    )


def _ranges(start: int, end: int, new: str) -> list[dict]:
    return [{"start_line": start, "end_line": end, "new": new}]


NUMBERED = "".join(f"line{n:02d}\n" for n in range(1, 21))


def test_head_drop_over_a_fifteen_line_range_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    session, target = _session(tmp_path, "tests.txt", NUMBERED)

    result = _apply(session, "tests.txt", 6, 20, "line19\nline20 changed\n")

    assert result["ok"] is False
    assert result["reason"] == "semantic_edit_range_edge_dropped:0:head"
    assert result["range_index"] == 0
    assert result["edge"] == "head"
    assert result["dropped_lines"] == [f"{n}\tline{n:02d}" for n in range(6, 19)]
    assert "re-emit the dropped lines" in result["next_call"]
    assert "new=''" in result["next_call"]
    assert target.read_text(encoding="utf-8") == NUMBERED


def test_head_drop_of_three_comment_lines_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    text = (
        "def build():\n"
        "    # first note\n"
        "    # second note\n"
        "    # third note\n"
        "    value = 1\n"
        "    return value\n"
    )
    session, target = _session(tmp_path, "task_templates.py", text)

    result = _apply(
        session, "task_templates.py", 2, 6, "    value = 1\n    return value\n"
    )

    assert result["ok"] is False
    assert result["reason"] == "semantic_edit_range_edge_dropped:0:head"
    assert result["dropped_line_count"] == 3
    assert result["dropped_lines"] == [
        "2\t    # first note",
        "3\t    # second note",
        "4\t    # third note",
    ]
    assert target.read_text(encoding="utf-8") == text


def test_edited_first_line_of_a_range_applies_through_the_worker(tmp_path: Path) -> None:
    text = (
        "def build():\n"
        "    # first note\n"
        "    value = 1\n"
        "    return value\n"
    )
    session, target = _session(tmp_path, "task_templates.py", text)

    result = _apply(
        session, "task_templates.py", 2, 4, "    # first note, edited\n    value = 2\n    return value\n"
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == (
        "def build():\n"
        "    # first note, edited\n"
        "    value = 2\n"
        "    return value\n"
    )


def test_tail_drop_is_refused_symmetrically_and_writes_nothing(tmp_path: Path) -> None:
    session, target = _session(tmp_path, "tail.txt", "a\nb\nc\nd\ne\n")

    result = _apply(session, "tail.txt", 1, 5, "x\nb\n")

    assert result["ok"] is False
    assert result["reason"] == "semantic_edit_range_edge_dropped:0:tail"
    assert result["edge"] == "tail"
    assert result["dropped_lines"] == ["3\tc", "4\td", "5\te"]
    assert target.read_text(encoding="utf-8") == "a\nb\nc\nd\ne\n"


def test_edge_drop_names_the_range_index_and_applies_no_range() -> None:
    text = "a\nb\nc\nd\ne\nf\n"
    ranges = [
        {"start_line": 1, "end_line": 1, "new": "A\n"},
        {"start_line": 3, "end_line": 6, "new": "e\nf\n"},
    ]
    with pytest.raises(semantic_edit.SemanticEditEdgeDropped) as caught:
        semantic_edit.apply_line_ranges(text, ranges)
    assert str(caught.value) == "semantic_edit_range_edge_dropped:1:head"
    assert caught.value.detail["dropped_lines"] == ["3\tc", "4\td"]


def test_legitimate_rewrite_still_applies_byte_exactly() -> None:
    text = "keep\nold one\nold two\nkeep too\n"
    next_text, metrics = semantic_edit.apply_line_ranges(
        text, _ranges(2, 3, "brand new\nlines here\nand more\n")
    )
    assert next_text == "keep\nbrand new\nlines here\nand more\nkeep too\n"
    assert metrics["replacement_bytes"] == len(b"brand new\nlines here\nand more\n")


def test_pure_deletion_still_applies() -> None:
    text = "a\nb\nc\nd\n"
    next_text, _metrics = semantic_edit.apply_line_ranges(text, _ranges(2, 3, ""))
    assert next_text == "a\nd\n"


def test_insert_that_reemits_the_anchor_line_first_still_applies() -> None:
    text = "a\nanchor\nz\n"
    next_text, _metrics = semantic_edit.apply_line_ranges(
        text, _ranges(2, 2, "anchor\ninserted one\ninserted two\n")
    )
    assert next_text == "a\nanchor\ninserted one\ninserted two\nz\n"


def test_insert_that_reemits_the_anchor_line_last_still_applies() -> None:
    text = "a\nanchor\nz\n"
    next_text, _metrics = semantic_edit.apply_line_ranges(
        text, _ranges(2, 2, "inserted\nanchor\n")
    )
    assert next_text == "a\ninserted\nanchor\nz\n"


def test_first_line_that_is_an_edited_version_of_old_line_one_still_applies() -> None:
    text = "def f(a):\n    return a\n    # tail\n"
    next_text, _metrics = semantic_edit.apply_line_ranges(
        text, _ranges(1, 3, "def f(a, b):\n    return a\n    # tail\n")
    )
    assert next_text == "def f(a, b):\n    return a\n    # tail\n"


def test_dropping_a_line_that_is_re_emitted_later_is_not_a_drop() -> None:
    text = "x\ny\nz\n"
    next_text, _metrics = semantic_edit.apply_line_ranges(
        text, _ranges(1, 3, "z\nx\ny\n")
    )
    assert next_text == "z\nx\ny\n"


def test_blank_leading_lines_do_not_hide_a_head_drop() -> None:
    text = "a\nb\nc\nd\n"
    with pytest.raises(semantic_edit.SemanticEditEdgeDropped):
        semantic_edit.apply_line_ranges(text, _ranges(1, 4, "\n\nc\nd\n"))


def test_crlf_ranges_report_content_without_terminators() -> None:
    text = "a\r\nb\r\nc\r\n"
    with pytest.raises(semantic_edit.SemanticEditEdgeDropped) as caught:
        semantic_edit.apply_line_ranges(text, _ranges(1, 3, "c\r\n"))
    assert caught.value.detail["dropped_lines"] == ["1\ta", "2\tb"]


def test_changed_backslash_level_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    old_comment = f"# match a{BS}.N here\n"
    doubled = f"# match a{BS}{BS}.N here\n"
    session, target = _session(tmp_path, "regex.txt", "alpha\n" + old_comment + "omega\n")

    result = _apply(session, "regex.txt", 1, 2, "alpha\n" + doubled)

    assert result["ok"] is False
    assert result["reason"] == "semantic_edit_escape_level_changed:0"
    assert result["range_index"] == 0
    assert result["lines"] == [{"old_line": 2, "direction": "gained"}]
    assert "original backslashes" in result["next_call"]
    assert target.read_text(encoding="utf-8") == "alpha\n" + old_comment + "omega\n"


def test_lost_backslash_level_is_refused_too() -> None:
    text = f"alpha\n# a{BS}{BS}.N\n"
    with pytest.raises(semantic_edit.SemanticEditEscapeLevelChanged) as caught:
        semantic_edit.apply_line_ranges(text, _ranges(1, 2, f"alpha\n# a{BS}.N\n"))
    assert caught.value.detail["lines"] == [{"old_line": 2, "direction": "lost"}]


def test_allow_escape_level_change_applies_and_reports() -> None:
    text = f"alpha\n# a{BS}.N\n"
    ranges = _ranges(1, 2, f"alpha\n# a{BS}{BS}.N\n")
    ranges[0]["allow_escape_level_change"] = True

    next_text, metrics = semantic_edit.apply_line_ranges(text, ranges)

    assert next_text == f"alpha\n# a{BS}{BS}.N\n"
    assert metrics["escape_level_changed_lines"] == [
        {"range_index": 0, "old_line": 2, "direction": "gained"}
    ]


def test_allow_escape_level_change_must_be_a_bool() -> None:
    ranges = _ranges(1, 1, "a\n")
    ranges[0]["allow_escape_level_change"] = "yes"
    with pytest.raises(semantic_edit.SemanticEditError, match="escape_policy_invalid:0"):
        semantic_edit.apply_line_ranges("a\n", ranges)


def test_fragment_hash_is_still_checked_before_the_edge_guard() -> None:
    ranges = _ranges(1, 3, "c\n")
    ranges[0]["fragment_sha256"] = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(semantic_edit.SemanticEditError, match="fragment_hash_mismatch:0"):
        semantic_edit.apply_line_ranges("a\nb\nc\n", ranges)


def _escape_session(tmp_path: Path):
    old_comment = f"# match a{BS}.N here\n"
    doubled = f"# match a{BS}{BS}.N here\n"
    session, target = _session(tmp_path, "regex.txt", "alpha\n" + old_comment + "omega\n")
    prepared = session.prepare(file_path="src/regex.txt", start_line=1, end_line=2)
    assert prepared["ok"] is True
    return session, target, prepared["target_id"], old_comment, doubled


def test_worker_apply_allow_escape_level_change_applies_what_is_refused_without_it(
    tmp_path: Path,
) -> None:
    session, target, target_id, old_comment, doubled = _escape_session(tmp_path)
    new = "alpha\n" + doubled

    refused = session.apply(target_id=target_id, new=new, idempotency_key="k-plain")
    assert refused["ok"] is False
    assert refused["reason"] == "semantic_edit_escape_level_changed:0"
    assert target.read_text(encoding="utf-8") == "alpha\n" + old_comment + "omega\n"

    allowed = session.apply(
        target_id=target_id,
        new=new,
        idempotency_key="k-allowed",
        allow_escape_level_change=True,
    )
    assert allowed["ok"] is True
    assert allowed["escape_level_changed_lines"] == [
        {"range_index": 0, "old_line": 2, "direction": "gained"}
    ]
    assert target.read_text(encoding="utf-8") == "alpha\n" + doubled + "omega\n"


def test_worker_apply_escape_allowance_does_not_leak_into_the_next_apply(
    tmp_path: Path,
) -> None:
    session, target, target_id, old_comment, doubled = _escape_session(tmp_path)
    new = "alpha\n" + doubled
    allowed = session.apply(
        target_id=target_id, new=new, idempotency_key="k-allowed",
        allow_escape_level_change=True,
    )
    assert allowed["ok"] is True
    assert semantic_edit._ESCAPE_LEVEL_CHANGE_DEFAULT.get() is False
    with pytest.raises(semantic_edit.SemanticEditEscapeLevelChanged):
        semantic_edit.apply_line_ranges(
            "alpha\n" + doubled, _ranges(1, 2, "alpha\n" + old_comment)
        )


def test_worker_apply_non_bool_escape_flag_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    session, target, target_id, old_comment, doubled = _escape_session(tmp_path)

    result = session.apply(
        target_id=target_id,
        new="alpha\n" + doubled,
        idempotency_key="k-bad",
        allow_escape_level_change="yes",
    )

    assert result["ok"] is False
    assert result["reason"] == "semantic_edit_escape_policy_invalid:0"
    assert target.read_text(encoding="utf-8") == "alpha\n" + old_comment + "omega\n"


def test_manager_apply_passes_allow_escape_level_change_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiworkhub import manager_ai_tools

    seen: list[dict] = []

    class _FakeSession:
        def apply(self, **kwargs):
            seen.append(kwargs)
            return {"ok": True, "tool": "semantic_edit_apply"}

    manager = {"session_id": "s", "provider": "p", "repo": "r"}
    monkeypatch.setattr(
        manager_ai_tools, "_manager_edit_session", lambda: (_FakeSession(), manager)
    )
    monkeypatch.setattr(manager_ai_tools.core, "writes_allowed", lambda: True)

    manager_ai_tools.semantic_edit_apply(
        target_id="t", new="x\n", idempotency_key="k", allow_escape_level_change=True
    )
    manager_ai_tools.semantic_edit_apply(target_id="t", new="x\n", idempotency_key="k")

    assert [call["allow_escape_level_change"] for call in seen] == [True, False]
