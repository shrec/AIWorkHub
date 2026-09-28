from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import ModuleType

import pytest

from aiworkhub import semantic_edit
from aiworkhub import semantic_edit_applier
from aiworkhub import process_launcher
from aiworkhub import worker_ai_tools_mcp as worker_tools


def test_prepare_returns_only_fragment_and_truthful_byte_accounting(tmp_path: Path) -> None:
    target = tmp_path / "src" / "module.py"
    target.parent.mkdir()
    target.write_bytes(b"before\ndef target():\n    return 1\nafter\n")

    prepared = semantic_edit.prepare_line_target(
        tmp_path,
        path="src/module.py",
        start_line=2,
        end_line=3,
        allowed_writes=("src/*.py",),
    )
    receipt = prepared.receipt(target_id="target")

    assert receipt["fragment"] == "def target():\n    return 1\n"
    assert receipt["fragment_bytes"] < receipt["file_bytes"]
    assert receipt["whole_file_bytes_not_returned_by_tool"] == (
        receipt["file_bytes"] - receipt["fragment_bytes"]
    )
    assert receipt["token_savings_claimed"] is False


def test_empty_file_has_one_hash_bound_virtual_line(tmp_path: Path) -> None:
    target = tmp_path / "out" / "result.txt"
    target.parent.mkdir()
    target.write_bytes(b"")

    prepared = semantic_edit.prepare_line_target(
        tmp_path,
        path="out/result.txt",
        start_line=1,
        end_line=1,
        allowed_writes=("out/*.txt",),
    )
    assert prepared.fragment == ""
    assert prepared.file_bytes == 0
    assert prepared.fragment_bytes == 0

    next_text, metrics = semantic_edit.apply_line_ranges(
        "",
        [{
            "start_line": 1,
            "end_line": 1,
            "new": "created\n",
            "fragment_sha256": hashlib.sha256(b"").hexdigest(),
        }],
    )
    assert next_text == "created\n"
    assert metrics["old_region_bytes"] == 0
    assert metrics["replacement_bytes"] == len(b"created\n")


def test_empty_file_rejects_every_nonvirtual_line_range(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_bytes(b"")

    with pytest.raises(semantic_edit.SemanticEditError, match="out_of_bounds:1:2:0"):
        semantic_edit.prepare_line_target(
            tmp_path,
            path="out.txt",
            start_line=1,
            end_line=2,
            allowed_writes=("out.txt",),
        )


def test_worker_semantic_edit_is_hash_bound_atomic_and_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    target = repo / "src" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before\ndef target():\n    return 1\nafter\n")
    ctx = worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=("src/module.py",),
        session_topic="topic",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        allowed_writes=("src/*.py",),
    )
    session = worker_tools.WorkerSemanticEditSession(ctx)

    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    assert prepared["ok"] is True
    result = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-1",
    )
    assert result["ok"] is True
    assert result["model_reemitted_old_bytes"] == 0
    assert result["file_bytes"] == len("before\ndef target():\n    return 1\nafter\n".encode())
    assert target.read_text(encoding="utf-8") == "before\ndef target():\n    return 2\nafter\n"

    # The idempotency contract requires an identical repeat: same key, same
    # target AND the same replacement bytes.  A true retry replays the first
    # receipt without rewriting the file.  (Before the SCAN-E3FF fix a replay
    # was matched on the key alone, so this call passed "ignored on replay"
    # and asserted the differing content was silently discarded.)
    replay = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-1",
    )
    assert replay["idempotent_replay"] is True
    assert replay["after_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert target.read_text(encoding="utf-8") == "before\ndef target():\n    return 2\nafter\n"


def test_worker_semantic_edit_rejects_stale_and_out_of_scope(tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    target = repo / "src" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"one\ntwo\n")
    ctx = worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=("src/module.py",),
        session_topic="topic",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        allowed_writes=("src/*.py",),
    )
    session = worker_tools.WorkerSemanticEditSession(ctx)
    denied = session.prepare(file_path="README.md", start_line=1, end_line=1)
    assert denied == {
        "ok": False,
        "tool": "semantic_edit_prepare",
        "reason": "semantic_edit_path_not_allowed:README.md",
    }

    prepared = session.prepare(file_path="src/module.py", start_line=1, end_line=1)
    target.write_bytes(b"changed\ntwo\n")
    stale = session.apply(
        target_id=prepared["target_id"], new="new", idempotency_key="edit-2"
    )
    assert stale["ok"] is False
    assert "semantic_edit_stale_file" in stale["reason"]
    assert target.read_text(encoding="utf-8") == "changed\ntwo\n"


@pytest.mark.parametrize(
    "raw",
    [
        "Z:foo",
        "Z:\\foo",
        "Z:/foo",
        "\\\\server\\share\\x",
        "//server/share/x",
        "\\foo",
    ],
)
def test_normalize_relative_path_refuses_windows_drive_and_anchor_forms(raw: str) -> None:
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        semantic_edit.normalize_relative_path(raw)
    assert str(excinfo.value).startswith("semantic_edit_path_escape:")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/pkg/mod.py", "src/pkg/mod.py"),
        ("a\\b.txt", "a/b.txt"),
        ("docs/x:y.md", "docs/x:y.md"),
    ],
)
def test_normalize_relative_path_accepts_ordinary_relative_paths(
    raw: str, expected: str
) -> None:
    assert semantic_edit.normalize_relative_path(raw) == expected


def test_terminal_semantic_edit_evidence_is_bounded_and_byte_only(tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.jsonl"
    stdout.write_text(
        json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "edit_protocol": "aiworkhub.vscode_lm.semantic_edit_response.v3",
            "semantic_edit_metrics": [{
                "path": "src/module.py",
                "file_bytes": 1000,
                "range_count": 1,
                "old_region_bytes": 80,
                "replacement_bytes": 60,
                "model_reemitted_old_bytes": 0,
            }],
        }) + "\n",
        encoding="utf-8",
    )

    evidence = process_launcher._semantic_edit_evidence_from_output(stdout)

    assert evidence == {
        "schema_id": "aiworkhub.semantic_edit_runtime_evidence.v1",
        "observed": True,
        "file_count": 1,
        "range_count": 1,
        "file_bytes": 1000,
        "old_region_bytes": 80,
        "replacement_bytes": 60,
        "model_reemitted_old_bytes": 0,
        "token_savings_claimed": False,
    }


def test_terminal_semantic_edit_evidence_accepts_authenticated_cli_receipt(
    tmp_path: Path,
) -> None:
    missing_stdout = tmp_path / "missing.jsonl"
    evidence = process_launcher._semantic_edit_evidence_from_output(
        missing_stdout,
        worker_mcp_gate={
            "verification": {
                "semantic_edit_apply_receipts": [{
                    "file_bytes": 8453,
                    "range_count": 1,
                    "old_region_bytes": 26,
                    "replacement_bytes": 92,
                    "model_reemitted_old_bytes": 0,
                    "token_savings_claimed": False,
                }],
            },
        },
    )

    assert evidence["observed"] is True
    assert evidence["file_count"] == 1
    assert evidence["file_bytes"] == 8453
    assert evidence["replacement_bytes"] == 92
    assert evidence["token_savings_claimed"] is False


def test_verified_audit_exposes_only_semantic_edit_byte_receipt(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "worktree"
    target = repo / "src" / "module.py"
    target.parent.mkdir(parents=True)
    original = "before\ndef target():\n    return 1\nafter\n"
    target.write_bytes(original.encode("utf-8"))
    ledger = tmp_path / "audit.jsonl"
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 32)
    ctx = worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=tmp_path,
        source_graph_targets=("src/module.py",),
        session_topic="topic",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key_path,
        allowed_writes=("src/*.py",),
    )
    session = worker_tools.WorkerSemanticEditSession(ctx)
    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    applied = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-audit-1",
    )
    assert applied["ok"] is True

    verified = worker_tools.verify_audit_ledger(
        ledger,
        key_path,
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
    )

    assert verified["successful_call_count_by_tool"]["semantic_edit_prepare"] == 1
    assert verified["successful_call_count_by_tool"]["semantic_edit_apply"] == 1
    assert verified["semantic_edit_apply_receipts"] == [{
        "path_sha256": hashlib.sha256(b"src/module.py").hexdigest(),
        "file_bytes": len(original.encode("utf-8")),
        "range_count": 1,
        "old_region_bytes": len("def target():\n    return 1\n".encode("utf-8")),
        "replacement_bytes": len("def target():\n    return 2\n".encode("utf-8")),
        "model_reemitted_old_bytes": 0,
        "token_savings_claimed": False,
    }]
    serialized = json.dumps(verified, sort_keys=True)
    assert "edit-audit-1" not in serialized
    # The privacy boundary is unchanged: the ledger still carries no path TEXT,
    # no replacement text and no preimage/idempotency identity.  The added
    # field is a non-invertible digest of the repo-relative path, usable only
    # by a reader that already holds the path.
    assert "src/module.py" not in serialized
    assert "def target()" not in serialized
    assert verified["semantic_edit_apply_receipts"][0]["path_sha256"] == (
        process_launcher.semantic_edit_path_identifier("src/module.py")
    )


# ---------------------------------------------------------------------------
# Delivered-range registry (worker_validation-5).  The fragment is bytes the
# caller already holds; ``apply`` needs only the hashes.  CONTRACT: apply must
# still verify BOTH preimage hashes on the live file when the receipt it was
# given carried no fragment.
# ---------------------------------------------------------------------------

def _delivery_session(tmp_path: Path) -> tuple[worker_tools.WorkerSemanticEditSession, Path]:
    repo = tmp_path / "worktree"
    target = repo / "src" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before\ndef target():\n    return 1\nafter\n")
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 32)
    ctx = worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=tmp_path,
        source_graph_targets=("src/module.py",),
        session_topic="topic",
        audit_ledger_path=tmp_path / "audit.jsonl",
        audit_hmac_key_path=key_path,
        allowed_writes=("src/*.py",),
    )
    return worker_tools.WorkerSemanticEditSession(ctx), target


def test_an_already_delivered_range_comes_back_hash_only(tmp_path: Path) -> None:
    session, _target = _delivery_session(tmp_path)

    first = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    assert first["fragment"] == "def target():\n    return 1\n"
    assert "fragment_omitted" not in first

    second = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    assert "fragment" not in second
    assert second["fragment_omitted"] is True
    assert second["delivered_by"] == first["target_id"]
    assert second["fragment_bytes_avoided"] == first["fragment_bytes"]
    # Everything apply verifies is present either way.
    assert second["current_sha256"] == first["current_sha256"]
    assert second["fragment_sha256"] == first["fragment_sha256"]
    assert (second["start_line"], second["end_line"]) == (2, 3)

    # A containing range counts as delivered; a wider one does not.
    inner = session.prepare(file_path="src/module.py", start_line=2, end_line=2)
    assert inner["fragment_omitted"] is True
    wider = session.prepare(file_path="src/module.py", start_line=1, end_line=4)
    assert "fragment" in wider

    # The override always wins.
    forced = session.prepare(
        file_path="src/module.py", start_line=2, end_line=3, include_fragment=True,
    )
    assert forced["fragment"] == first["fragment"]


def test_apply_from_a_hash_only_receipt_still_verifies_both_preimages(
    tmp_path: Path,
) -> None:
    session, target = _delivery_session(tmp_path)
    session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    hash_only = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    assert "fragment" not in hash_only

    applied = session.apply(
        target_id=hash_only["target_id"],
        new="def target():\n    return 2",
        idempotency_key="delivered-1",
    )
    assert applied["ok"] is True
    assert applied["preimage_verified"] is True
    assert applied["preimage_verified_range_count"] == 1
    assert applied["preimage_unverified_range_count"] == 0
    assert applied["before_sha256"] == hash_only["current_sha256"]
    assert target.read_text(encoding="utf-8") == (
        "before\ndef target():\n    return 2\nafter\n"
    )


def test_an_edit_invalidates_every_earlier_delivery_of_that_file(
    tmp_path: Path,
) -> None:
    """The registry key carries the file sha, so a write re-arms the fragment."""
    session, _target = _delivery_session(tmp_path)
    first = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    session.apply(
        target_id=first["target_id"],
        new="def target():\n    return 2",
        idempotency_key="invalidate-1",
    )

    after = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    assert after["fragment"] == "def target():\n    return 2\n"
    assert "fragment_omitted" not in after
    assert after["current_sha256"] != first["current_sha256"]


def test_a_source_graph_body_reply_counts_as_a_delivery(tmp_path: Path) -> None:
    """A body the server already served is not re-sent by ``prepare``."""
    session, target = _delivery_session(tmp_path)
    body = "def target():\n    return 1"
    payload = {
        "mode": "body",
        "matches": [{
            "file_path": "src/module.py",
            "line_start": 2,
            "line_end": 3,
            "source": body,
            "freshness": {
                "state": "fresh",
                "disk_source_hash": hashlib.sha256(target.read_bytes()).hexdigest(),
            },
        }],
    }
    registered = session.note_source_graph_delivery({
        "ok": True,
        "receipt_id": "abc123",
        "content": json.dumps(payload, sort_keys=True),
    })
    assert registered == 1

    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    assert "fragment" not in prepared
    assert prepared["fragment_omitted"] is True
    assert prepared["delivered_by"] == "source_graph:abc123"

    # A paged (base64) reply is not a delivery: those bytes were never
    # readable in one page.
    other, _other_target = _delivery_session(tmp_path / "second")
    assert other.note_source_graph_delivery({
        "ok": True,
        "receipt_id": "abc123",
        "content_encoding": "base64",
        "content": json.dumps(payload, sort_keys=True),
    }) == 0


def test_one_line_prepare_accepts_equal_bounds_and_decimal_strings(tmp_path: Path) -> None:
    target = tmp_path / "src" / "module.py"
    target.parent.mkdir()
    target.write_bytes(b"alpha\nbeta\ngamma\n")

    prepared = semantic_edit.prepare_line_target(
        tmp_path,
        path="src/module.py",
        start_line=2,
        end_line=2,
        allowed_writes=("src/*.py",),
    )
    assert prepared.start_line == 2
    assert prepared.end_line == 2
    assert prepared.fragment == "beta\n"

    string_prepared = semantic_edit.prepare_line_target(
        tmp_path,
        path="src/module.py",
        start_line="2",
        end_line="2",
        allowed_writes=("src/*.py",),
    )
    assert string_prepared.fragment == "beta\n"
    assert string_prepared.start_line == 2
    assert string_prepared.fragment_sha256 == prepared.fragment_sha256

    with pytest.raises(semantic_edit.SemanticEditError, match="line_range_invalid"):
        semantic_edit.prepare_line_target(
            tmp_path,
            path="src/module.py",
            start_line="2",
            end_line="1",
            allowed_writes=("src/*.py",),
        )
    with pytest.raises(semantic_edit.SemanticEditError, match="out_of_bounds"):
        semantic_edit.prepare_line_target(
            tmp_path,
            path="src/module.py",
            start_line="9",
            end_line="9",
            allowed_writes=("src/*.py",),
        )

    next_text, metrics = semantic_edit.apply_line_ranges(
        "alpha\nbeta\ngamma\n",
        [{
            "start_line": "2",
            "end_line": "2",
            "new": "BETA\n",
            "fragment_sha256": prepared.fragment_sha256,
        }],
    )
    assert next_text == "alpha\nBETA\ngamma\n"
    assert metrics["preimage_verified"] is True

    with pytest.raises(semantic_edit.SemanticEditError, match="fragment_hash_mismatch"):
        semantic_edit.apply_line_ranges(
            "alpha\nbeta\ngamma\n",
            [{
                "start_line": "2",
                "end_line": "2",
                "new": "BETA\n",
                "fragment_sha256": "0" * 64,
            }],
        )
    with pytest.raises(semantic_edit.SemanticEditError, match="ranges_overlap"):
        semantic_edit.apply_line_ranges(
            "alpha\nbeta\ngamma\n",
            [
                {"start_line": "1", "end_line": "2", "new": "x\n"},
                {"start_line": "2", "end_line": "2", "new": "y\n"},
            ],
        )


def test_the_package_has_one_protocol_line_coercion() -> None:
    """NF-2026-01022: the VS Code LM bridge binds the owner, not a copy of it."""

    from aiworkhub import vscode_lm_worker

    assert vscode_lm_worker.coerce_protocol_line is semantic_edit.coerce_protocol_line
    for raw, expected in (("55", 55), (" 55 ", 55), (55, 55), (True, True), ("x", "x")):
        assert semantic_edit.coerce_protocol_line(raw) == expected
        assert isinstance(semantic_edit.coerce_protocol_line(raw), type(expected))


# ---------------------------------------------------------------------------
# NF-2026-01077: a line is what ``\n`` ends.
#
# Source Graph, git and editors number lines on ``\n`` only.  ``str.splitlines``
# also breaks on a bare CR, \v, \f, \x1c-\x1e, U+0085, U+2028 and U+2029, so a
# range taken from any of them selected -- and replaced -- the wrong fragment of a
# file that holds one of those characters.
# ---------------------------------------------------------------------------

# Every character ``str.splitlines`` breaks on besides ``\n``.
_OTHER_LINE_BREAKS = (
    "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85",
    "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}",
)


def _write_break_in_line_two(root: Path, char: str) -> str:
    """Write ``src/module.txt`` byte for byte with ``char`` inside its line 2."""
    text = f"one\ntwo{char}2\nthree\nfour\n"
    target = root / "src" / "module.txt"
    target.parent.mkdir()
    target.write_bytes(text.encode("utf-8"))
    return text


def _prepare_lines(root: Path, start: int, end: int) -> semantic_edit.PreparedLineTarget:
    return semantic_edit.prepare_line_target(
        root,
        path="src/module.txt",
        start_line=start,
        end_line=end,
        allowed_writes=("src/*.txt",),
    )


@pytest.mark.parametrize("char", _OTHER_LINE_BREAKS, ids=repr)
def test_prepare_numbers_lines_on_lf_only(tmp_path: Path, char: str) -> None:
    text = _write_break_in_line_two(tmp_path, char)

    third = _prepare_lines(tmp_path, 3, 3)
    assert third.fragment == "three\n"
    assert third.fragment_sha256 == hashlib.sha256(b"three\n").hexdigest()
    assert third.current_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert _prepare_lines(tmp_path, 2, 2).fragment == f"two{char}2\n"
    assert _prepare_lines(tmp_path, 2, 3).fragment == f"two{char}2\nthree\n"
    # Four lines, not five or more: the character splits nothing.
    with pytest.raises(semantic_edit.SemanticEditError, match="out_of_bounds:5:5:4"):
        _prepare_lines(tmp_path, 5, 5)


@pytest.mark.parametrize("char", _OTHER_LINE_BREAKS, ids=repr)
def test_apply_line_ranges_changes_only_the_lf_numbered_line(char: str) -> None:
    text = f"one\ntwo{char}2\nthree\nfour\n"

    next_text, metrics = semantic_edit.apply_line_ranges(
        text,
        [{
            "start_line": 3,
            "end_line": 3,
            "new": "THREE",
            "fragment_sha256": hashlib.sha256(b"three\n").hexdigest(),
        }],
    )
    assert next_text == f"one\ntwo{char}2\nTHREE\nfour\n"
    assert metrics["old_region_bytes"] == len(b"three\n")
    assert metrics["preimage_verified"] is True

    # The line holding the character is replaced whole: no stray tail is left.
    whole_line, _metrics = semantic_edit.apply_line_ranges(
        text, [{"start_line": 2, "end_line": 2, "new": "TWO"}]
    )
    assert whole_line == "one\nTWO\nthree\nfour\n"


# Text shapes for the ``\n``-only splitter: every break ``str.splitlines`` would
# also honour, plus the edges a splitter tends to get wrong.
_SPLIT_LINES_CORPUS = (
    "",
    "a",
    "\n",
    "\n\n",
    "a\n",
    "a\nb",
    "a\nb\n",
    "a\n\nb\n",
    "a\r\nb\r\n",
    "a\r\nb",
    "a\rb\rc\r",
    "a\rb\nc",
    "a\x85b\nc\n",
    "a\N{LINE SEPARATOR}b\nc\n",
    "a\N{PARAGRAPH SEPARATOR}b\nc\n",
    "a\vb\nc\n",
    "a\fb\nc\n",
    "a\x1cb\x1dc\x1ed\n",
    "\r\n\r\n",
    "\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\v\f\x1c\r",
    "a\r\nb\rc\x85d\N{LINE SEPARATOR}e\N{PARAGRAPH SEPARATOR}f\vg\fh\x1ci\nj",
)


@pytest.mark.parametrize("text", _SPLIT_LINES_CORPUS, ids=repr)
def test_split_lines_keeps_line_ends_and_round_trips(text: str) -> None:
    lines = semantic_edit._split_lines(text)

    assert "".join(lines) == text
    assert all(line.count("\n") == 1 and line.endswith("\n") for line in lines[:-1])
    assert (lines == []) is (text == "")


@pytest.mark.parametrize("text", _SPLIT_LINES_CORPUS, ids=repr)
def test_split_lines_agrees_with_source_graph_diff(text: str) -> None:
    """Two splitters, one rule.  ``source_graph_diff`` numbers the lines a reviewer
    reads and this module the lines an edit replaces, so they must never differ;
    moving the diff onto this splitter is the follow-up NF-2026-01085."""
    from aiworkhub import source_graph_diff  # heavy import: only this test pays for it

    assert semantic_edit._split_lines(text) == source_graph_diff._split_lines(text)


@pytest.mark.parametrize("module", [semantic_edit, semantic_edit_applier], ids=lambda m: m.__name__)
def test_no_editor_module_calls_str_splitlines(module: ModuleType) -> None:
    """``_split_lines`` is the one splitter: a ``splitlines`` call is a second rule."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    lines = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "splitlines"
    )
    assert lines == [], f"{module.__name__} uses str.splitlines at line(s) {lines}"


def test_the_applier_has_no_line_splitter_of_its_own() -> None:
    assert (
        getattr(semantic_edit_applier, "_split_lines", semantic_edit._split_lines)
        is semantic_edit._split_lines
    )
