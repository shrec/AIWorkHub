"""One-shot ``aiworkhub_worker_semantic_edit_apply`` (NF-2026-01667).

``file_path``/``start_line``/``end_line`` plus both exact anchor lines replace
``target_id``: the call runs the one ``prepare`` and then the one ``apply``, so
no gate is restated and a prepare refusal comes back unchanged.  The tests reach
the tool the way a model does, as a JSON-RPC ``tools/call``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import semantic_edit_applier
from aiworkhub import stdio_fastmcp
from aiworkhub import worker_ai_tools_mcp as worker_tools

_PATH = "src/module.py"
_PREPARE = "aiworkhub_worker_semantic_edit_prepare"
_APPLY = "aiworkhub_worker_semantic_edit_apply"
_BEFORE = b"before\ndef target():\n    return 1\nafter\n"
_AFTER = b"before\ndef target():\n    return 2\nafter\n"
_NEW = "def target():\n    return 2"
_ONE_SHOT = {
    "file_path": _PATH,
    "start_line": 2,
    "end_line": 3,
    "expected_first_line": "def target():",
    "expected_last_line": "return 1",
}
# Only the middle line changes, so both anchors still match after the edit.
_KEPT = b"before\ndef target():\n    value = 1\n    return value\nafter\n"
_KEPT_ONE_SHOT = {**_ONE_SHOT, "end_line": 4, "expected_last_line": "return value"}
_KEPT_NEW = "def target():\n    value = 2\n    return value"
_ABSENT = object()


class _Client:
    """The worker MCP tools reached the way a model reaches them."""

    def __init__(self, root: Path, *, ledger: Path | None = None) -> None:
        key = None
        if ledger is not None:
            key = ledger.with_suffix(".key")
            key.write_bytes(b"k" * 32)
        self.server = stdio_fastmcp.FallbackFastMCP("one-shot")
        worker_tools.register_tools(
            self.server,
            worker_tools.WorkerToolContext(
                task_id="TASK", runner="runner", topic="topic", request_id="request",
                repo=root, authority_repo=root, source_graph_targets=(_PATH,),
                session_topic="topic", audit_ledger_path=ledger,
                audit_hmac_key_path=key, allowed_writes=("src/*.py",),
            ),
        )

    def rpc(self, method: str, params: dict[str, Any]) -> Any:
        return stdio_fastmcp._dispatch(self.server.name, self.server._tools, method, params)

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        reply = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        assert "isError" not in reply, reply
        return json.loads(reply["content"][0]["text"])

    def prepare(self, **arguments: Any) -> dict[str, Any]:
        return self.call(_PREPARE, **arguments)

    def apply(self, **arguments: Any) -> dict[str, Any]:
        return self.call(_APPLY, **arguments)


def _write(root: Path, content: bytes = _BEFORE, path: str = _PATH) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _without(reply: dict[str, Any], *names: str) -> dict[str, Any]:
    return {key: value for key, value in reply.items() if key not in names}


def _applier_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Paths ``apply`` handed to the applier, which is what writes the file."""
    paths: list[str] = []
    real = semantic_edit_applier.replace_prepared_range

    def spy(root: Path, target: Any, new: str, **options: Any) -> Any:
        paths.append(target.path)
        return real(root, target, new, **options)

    monkeypatch.setattr(semantic_edit_applier, "replace_prepared_range", spy)
    return paths


# ---------------------------------------------------------------------------
# The one call, and the two-call path it leaves alone.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "no_target", [{}, {"target_id": ""}, {"target_id": None}], ids=["omitted", "empty", "null"],
)
def test_one_shot_edits_the_file_in_a_single_call(
    tmp_path: Path, no_target: dict[str, Any],
) -> None:
    target = _write(tmp_path)

    result = _Client(tmp_path).apply(
        new=_NEW, idempotency_key="edit-1", **no_target, **_ONE_SHOT,
    )

    assert result["ok"] is True, result
    assert result["tool"] == "semantic_edit_apply"
    assert result["mode"] == "one_shot"
    assert result["idempotent_replay"] is False
    assert result["path"] == _PATH
    assert (result["start_line"], result["end_line"]) == (2, 3)
    assert result["before_sha256"] == hashlib.sha256(_BEFORE).hexdigest()
    assert result["after_sha256"] == hashlib.sha256(_AFTER).hexdigest()
    assert result["file_bytes"] == len(_BEFORE)
    assert result["preimage_verified"] is True
    assert "fragment" not in result
    assert target.read_bytes() == _AFTER


def test_the_one_shot_receipt_is_the_two_call_receipt_plus_mode(tmp_path: Path) -> None:
    two_call_root, one_shot_root = tmp_path / "two_call", tmp_path / "one_shot"
    _write(two_call_root)
    _write(one_shot_root)

    two_call = _Client(two_call_root)
    prepared = two_call.prepare(**_ONE_SHOT)
    expected = two_call.apply(
        target_id=prepared["target_id"], new=_NEW, idempotency_key="edit-1",
    )
    result = _Client(one_shot_root).apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)

    assert "mode" not in expected
    assert result["mode"] == "one_shot"
    assert _without(result, "mode", "target_id") == _without(expected, "target_id")
    assert (one_shot_root / _PATH).read_bytes() == (two_call_root / _PATH).read_bytes() == _AFTER


def test_the_two_call_round_trip_is_unchanged(tmp_path: Path) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path)

    prepared = client.prepare(**_ONE_SHOT)
    assert prepared["ok"] is True, prepared
    assert prepared["fragment"] == "def target():\n    return 1\n"

    applied = client.apply(target_id=prepared["target_id"], new=_NEW, idempotency_key="edit-1")
    assert applied["ok"] is True, applied
    assert applied["target_id"] == prepared["target_id"]
    assert applied["idempotent_replay"] is False
    assert "mode" not in applied
    assert target.read_bytes() == _AFTER

    replay = client.apply(target_id=prepared["target_id"], new=_NEW, idempotency_key="edit-1")
    assert replay == {**applied, "idempotent_replay": True}
    assert target.read_bytes() == _AFTER


def test_blank_one_shot_placeholders_beside_a_target_id_are_left_out(tmp_path: Path) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path)
    prepared = client.prepare(**_ONE_SHOT)

    applied = client.apply(
        target_id=prepared["target_id"], new=_NEW, idempotency_key="edit-1",
        file_path=None, start_line=None, end_line=None,
        expected_first_line="", expected_last_line="  ",
    )

    assert applied["ok"] is True, applied
    assert "mode" not in applied
    assert target.read_bytes() == _AFTER


# ---------------------------------------------------------------------------
# Prepare refuses: the reply comes back unchanged and the file is not touched.
# ---------------------------------------------------------------------------

def test_a_file_changed_after_the_read_is_refused_by_the_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _write(tmp_path)
    applier_calls = _applier_calls(monkeypatch)
    # Another writer lands after the model read lines 2-3 and before its call.
    changed = b"before\ndef target():\n    return 99\nafter\n"
    target.write_bytes(changed)

    refused = _Client(tmp_path).apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)

    assert refused["ok"] is False
    assert refused["tool"] == "semantic_edit_prepare"
    assert refused["reason"] == f"semantic_edit_anchor_mismatch:{_PATH}"
    assert refused["actual_last_line"].strip() == "return 99"
    assert refused["candidates"] == []
    assert "mode" not in refused
    assert applier_calls == []
    assert target.read_bytes() == changed


@pytest.mark.parametrize(
    ("before", "lines"),
    [
        (b"BEFORE\ndef target():\n    return 1\nafter\n", (2, 3)),
        (b"inserted\n" + _BEFORE, (3, 4)),
        (b"a\nb\n" + _BEFORE, (4, 5)),
    ],
    ids=["other_line_changed", "line_added_above", "two_lines_added_above"],
)
def test_the_anchors_find_the_range_when_other_lines_moved(
    tmp_path: Path, before: bytes, lines: tuple[int, int],
) -> None:
    target = _write(tmp_path, before)

    result = _Client(tmp_path).apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)

    assert result["ok"] is True, result
    assert (result["start_line"], result["end_line"]) == lines
    assert target.read_bytes() == before.replace(b"    return 1\n", b"    return 2\n")


@pytest.mark.parametrize(
    "wrong",
    [
        {"expected_first_line": "def other():"},
        {"expected_last_line": "return 2"},
        {"expected_first_line": "def other():", "expected_last_line": "return 2"},
    ],
    ids=["first", "last", "both"],
)
def test_a_wrong_anchor_is_refused_and_the_file_is_untouched(
    tmp_path: Path, wrong: dict[str, str],
) -> None:
    target = _write(tmp_path)

    refused = _Client(tmp_path).apply(
        new=_NEW, idempotency_key="edit-1", **{**_ONE_SHOT, **wrong},
    )

    assert refused["ok"] is False
    assert refused["tool"] == "semantic_edit_prepare"
    assert refused["reason"] == f"semantic_edit_anchor_mismatch:{_PATH}"
    assert "mode" not in refused
    assert target.read_bytes() == _BEFORE


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"file_path": "docs/readme.md"}, "semantic_edit_path_not_allowed:docs/readme.md"),
        ({"start_line": 3, "end_line": 2}, "semantic_edit_line_range_invalid"),
        ({"start_line": 5, "end_line": 6}, "semantic_edit_line_range_out_of_bounds:5:6:4"),
    ],
    ids=["out_of_scope_path", "reversed_range", "out_of_bounds_range"],
)
def test_a_prepare_refusal_comes_back_unchanged(
    tmp_path: Path, overrides: dict[str, Any], reason: str,
) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path)
    arguments = {**_ONE_SHOT, **overrides}

    refused = client.apply(new=_NEW, idempotency_key="edit-1", **arguments)

    assert refused == {"ok": False, "tool": "semantic_edit_prepare", "reason": reason}
    assert refused == client.prepare(**arguments)
    assert target.read_bytes() == _BEFORE


def test_a_write_landing_between_prepare_and_apply_is_refused_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _write(tmp_path)
    raced = b"before\ndef target():\n    return 7\nafter\n"
    real = semantic_edit_applier.replace_prepared_range

    def racing(root: Path, prepared: Any, new: str, **options: Any) -> Any:
        target.write_bytes(raced)
        return real(root, prepared, new, **options)

    monkeypatch.setattr(semantic_edit_applier, "replace_prepared_range", racing)

    refused = _Client(tmp_path).apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)

    assert refused["ok"] is False
    assert refused["reason"].startswith(f"semantic_edit_stale_file:{_PATH}")
    assert refused["mode"] == "one_shot"
    assert target.read_bytes() == raced


def test_an_empty_file_has_no_anchorable_line_so_one_shot_refuses_it(tmp_path: Path) -> None:
    empty = _write(tmp_path, b"", path="src/empty.py")
    client = _Client(tmp_path)
    arguments = {
        "new": "x = 1\n", "idempotency_key": "edit-1", "file_path": "src/empty.py",
        "start_line": 1, "end_line": 1,
    }

    wrong = client.apply(expected_first_line="x", expected_last_line="x", **arguments)
    blank = client.apply(expected_first_line="", expected_last_line="", **arguments)

    assert wrong["ok"] is False
    assert wrong["reason"] == "semantic_edit_anchor_mismatch:src/empty.py"
    assert blank["invalid_fields"] == ["expected_first_line", "expected_last_line"]
    assert empty.read_bytes() == b""


# ---------------------------------------------------------------------------
# Input contract: every missing field is named, and target_id excludes them all.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "left_out", [_ABSENT, None, "", " \t"], ids=["absent", "null", "empty", "blank"],
)
@pytest.mark.parametrize("name", list(_ONE_SHOT))
def test_a_missing_one_shot_field_is_named_and_nothing_is_written(
    tmp_path: Path, name: str, left_out: Any,
) -> None:
    target = _write(tmp_path)
    arguments = {key: value for key, value in _ONE_SHOT.items() if key != name}
    if left_out is not _ABSENT:
        arguments[name] = left_out

    refused = _Client(tmp_path).apply(new=_NEW, idempotency_key="edit-1", **arguments)

    assert refused["ok"] is False
    assert refused["tool"] == "semantic_edit_apply"
    assert refused["reason"] == "semantic_edit_apply_input_invalid"
    assert refused["invalid_fields"] == [name]
    assert "target_id" in refused["next_call"]
    assert "expected_first_line" in refused["next_call"]
    assert target.read_bytes() == _BEFORE


def test_a_call_with_neither_target_id_nor_one_shot_fields_names_all_five(
    tmp_path: Path,
) -> None:
    target = _write(tmp_path)

    refused = _Client(tmp_path).apply(new=_NEW, idempotency_key="edit-1")

    assert refused["ok"] is False
    assert refused["reason"] == "semantic_edit_apply_input_invalid"
    assert refused["invalid_fields"] == list(_ONE_SHOT)
    assert target.read_bytes() == _BEFORE


@pytest.mark.parametrize(
    ("overrides", "invalid"),
    [
        ({"new": None}, ["new"]),
        ({"idempotency_key": ""}, ["idempotency_key"]),
        ({"idempotency_key": "   "}, ["idempotency_key"]),
        ({"idempotency_key": "k" * 257}, ["idempotency_key"]),
        ({"idempotency_key": "é" * 129}, ["idempotency_key"]),
        ({"new": None, "idempotency_key": ""}, ["new", "idempotency_key"]),
    ],
    ids=["new", "empty_key", "blank_key", "long_key", "long_utf8_key", "both"],
)
def test_the_text_inputs_are_refused_as_the_two_call_apply_refuses_them(
    tmp_path: Path, overrides: dict[str, Any], invalid: list[str],
) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path)
    text = {"new": _NEW, "idempotency_key": "edit-1", **overrides}

    one_shot = client.apply(**text, **_ONE_SHOT)
    two_call = client.apply(target_id="prepared", **text)

    assert one_shot["reason"] == two_call["reason"] == "semantic_edit_apply_input_invalid"
    assert one_shot["invalid_fields"] == two_call["invalid_fields"] == invalid
    assert target.read_bytes() == _BEFORE


@pytest.mark.parametrize("name", list(_ONE_SHOT))
def test_a_target_id_beside_a_one_shot_field_is_refused_once(
    tmp_path: Path, name: str,
) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path)
    prepared = client.prepare(**_ONE_SHOT)

    refused = client.apply(
        target_id=prepared["target_id"], new=_NEW, idempotency_key="edit-1",
        **{name: _ONE_SHOT[name]},
    )

    assert refused["ok"] is False
    assert refused["tool"] == "semantic_edit_apply"
    assert refused["reason"] == "semantic_edit_apply_input_invalid"
    assert refused["invalid_fields"] == ["target_id", name]
    assert target.read_bytes() == _BEFORE

    applied = client.apply(target_id=prepared["target_id"], new=_NEW, idempotency_key="edit-1")
    assert applied["ok"] is True, applied
    assert target.read_bytes() == _AFTER


def test_a_target_id_beside_every_one_shot_field_names_each_of_them(tmp_path: Path) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path)
    prepared = client.prepare(**_ONE_SHOT)

    refused = client.apply(
        target_id=prepared["target_id"], new=_NEW, idempotency_key="edit-1", **_ONE_SHOT,
    )

    assert refused["reason"] == "semantic_edit_apply_input_invalid"
    assert refused["invalid_fields"] == ["target_id", *_ONE_SHOT]
    assert target.read_bytes() == _BEFORE


# ---------------------------------------------------------------------------
# Idempotency: the same contract the two-call apply already has.
# ---------------------------------------------------------------------------

def test_a_repeated_one_shot_call_returns_the_first_result_and_edits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path)
    applier_calls = _applier_calls(monkeypatch)

    first = client.apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)
    assert first["ok"] is True, first
    assert first["idempotent_replay"] is False

    # "return 1" is gone, so a fresh prepare would refuse these anchors now.
    for _ in range(2):
        again = client.apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)
        assert again == {**first, "idempotent_replay": True}

    assert again["mode"] == "one_shot"
    assert again["after_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert applier_calls == [_PATH]
    assert target.read_bytes() == _AFTER


def test_a_replay_survives_an_edit_that_kept_both_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _write(tmp_path, _KEPT)
    client = _Client(tmp_path)
    applier_calls = _applier_calls(monkeypatch)

    first = client.apply(new=_KEPT_NEW, idempotency_key="edit-1", **_KEPT_ONE_SHOT)
    # A fresh prepare would bind the same anchors under a new target_id, and
    # apply would then call the reused key a conflict.
    again = client.apply(new=_KEPT_NEW, idempotency_key="edit-1", **_KEPT_ONE_SHOT)

    assert first["ok"] is True, first
    assert again == {**first, "idempotent_replay": True}
    assert applier_calls == [_PATH]
    assert target.read_bytes() == _KEPT.replace(b"value = 1", b"value = 2")


@pytest.mark.parametrize("variant", ["other_replacement", "other_file"])
def test_a_reused_key_with_other_inputs_is_a_conflict_not_a_second_edit(
    tmp_path: Path, variant: str,
) -> None:
    target = _write(tmp_path, _KEPT)
    elsewhere = _write(tmp_path, _KEPT, path="src/other.py")
    client = _Client(tmp_path)
    first = client.apply(new=_KEPT_NEW, idempotency_key="edit-1", **_KEPT_ONE_SHOT)
    assert first["ok"] is True, first
    edited = target.read_bytes()

    if variant == "other_replacement":
        reused = client.apply(
            new=_KEPT_NEW.replace("2", "3"), idempotency_key="edit-1", **_KEPT_ONE_SHOT,
        )
    else:
        reused = client.apply(
            new=_KEPT_NEW, idempotency_key="edit-1",
            **{**_KEPT_ONE_SHOT, "file_path": "src/other.py"},
        )

    assert reused["ok"] is False
    assert reused["reason"] == "semantic_edit_idempotency_key_conflict"
    assert target.read_bytes() == edited
    assert elsewhere.read_bytes() == _KEPT


@pytest.mark.parametrize(
    ("attempt", "reason"),
    [
        ({"expected_last_line": "return 0"}, f"semantic_edit_anchor_mismatch:{_PATH}"),
        ({"new": "def target(:\n    return 2"}, f"semantic_edit_syntax_regression:{_PATH}:"),
    ],
    ids=["prepare_refusal", "apply_refusal"],
)
def test_a_refused_attempt_does_not_burn_its_idempotency_key(
    tmp_path: Path, attempt: dict[str, str], reason: str,
) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path)

    refused = client.apply(**{"new": _NEW, "idempotency_key": "edit-1", **_ONE_SHOT, **attempt})
    assert refused["ok"] is False
    assert refused["reason"].startswith(reason)
    assert target.read_bytes() == _BEFORE

    retried = client.apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)
    assert retried["ok"] is True, retried
    assert retried["idempotent_replay"] is False
    assert target.read_bytes() == _AFTER


# ---------------------------------------------------------------------------
# Everything apply already does still happens, once, after prepare.
# ---------------------------------------------------------------------------

def test_allow_escape_level_change_reaches_the_applier(tmp_path: Path) -> None:
    two, one = chr(92) * 2, chr(92)
    before = f'before\ndef pattern():\n    value = r"{two}d+"\n    return value\nafter\n'.encode()
    target = _write(tmp_path, before)
    arguments = {
        "file_path": _PATH, "start_line": 2, "end_line": 4,
        "expected_first_line": "def pattern():", "expected_last_line": "return value",
        "new": f'def pattern():\n    value = r"{one}d+"\n    return value',
        "idempotency_key": "edit-1",
    }
    client = _Client(tmp_path)

    refused = client.apply(**arguments)
    assert refused["ok"] is False
    assert refused["reason"].startswith("semantic_edit_escape_level_changed")
    assert refused["mode"] == "one_shot"
    assert target.read_bytes() == before

    allowed = client.apply(allow_escape_level_change=True, **arguments)
    assert allowed["ok"] is True, allowed
    assert target.read_bytes() == before.replace(two.encode(), one.encode())


def test_a_one_shot_the_ledger_did_not_record_is_not_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _write(tmp_path)
    client = _Client(tmp_path, ledger=tmp_path / "audit.jsonl")
    real = worker_tools._append_audit

    def apply_row_lost(ctx: Any, *, tool: str, **row: Any) -> bool:
        return False if tool == "semantic_edit_apply" else real(ctx, tool=tool, **row)

    monkeypatch.setattr(worker_tools, "_append_audit", apply_row_lost)
    lost = client.apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)
    monkeypatch.setattr(worker_tools, "_append_audit", real)
    retried = client.apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)

    assert lost["ok"] is False
    assert lost["reason"] == "semantic_edit_apply_not_durable"
    assert lost["mode"] == "one_shot"
    assert retried["ok"] is False
    assert retried["reason"] == f"semantic_edit_anchor_mismatch:{_PATH}"
    assert "idempotent_replay" not in retried
    assert target.read_bytes() == _AFTER


def test_a_one_shot_leaves_the_prepare_then_apply_ledger_trail(tmp_path: Path) -> None:
    _write(tmp_path)
    ledger = tmp_path / "audit.jsonl"
    client = _Client(tmp_path, ledger=ledger)

    bare = client.apply(new=_NEW, idempotency_key="edit-1")
    wrong = client.apply(
        new=_NEW, idempotency_key="edit-1", **{**_ONE_SHOT, "expected_last_line": "return 0"},
    )
    edited = client.apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)
    replayed = client.apply(new=_NEW, idempotency_key="edit-1", **_ONE_SHOT)

    assert [reply["ok"] for reply in (bare, wrong, edited, replayed)] == [False, False, True, True]
    assert replayed["idempotent_replay"] is True
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [(row["tool"], row["ok"], row["violation"]) for row in rows] == [
        ("semantic_edit_apply", False, "semantic_edit_apply_input_invalid"),
        ("semantic_edit_prepare", False, f"semantic_edit_anchor_mismatch:{_PATH}"),
        ("semantic_edit_prepare", True, ""),
        ("semantic_edit_apply", True, ""),
    ]


def test_the_apply_tool_advertises_the_one_shot_inputs(tmp_path: Path) -> None:
    client = _Client(tmp_path)

    listed = {tool["name"]: tool for tool in client.rpc("tools/list", {})["tools"]}
    schema = listed[_APPLY]["inputSchema"]
    assert schema["required"] == ["new", "idempotency_key"]
    assert set(schema["properties"]) == {
        "target_id", "new", "idempotency_key", "allow_escape_level_change", *_ONE_SHOT,
    }
    assert {name: schema["properties"][name]["type"] for name in _ONE_SHOT} == {
        "file_path": "string", "start_line": "integer", "end_line": "integer",
        "expected_first_line": "string", "expected_last_line": "string",
    }

    function = client.server._tools[_APPLY]
    assert inspect.signature(function).parameters["target_id"].default == ""
    doc = inspect.getdoc(function) or ""
    assert "ONE call without prepare" in doc
    assert all(name in doc for name in _ONE_SHOT)
    assert "both anchors must be non-blank lines" in doc


def test_the_next_call_says_both_anchors_must_be_non_blank(tmp_path: Path) -> None:
    target = _write(tmp_path)

    # A blank anchor counts as left out, so the shape must say to avoid one.
    blank = _Client(tmp_path).apply(
        new=_NEW, idempotency_key="edit-1", **{**_ONE_SHOT, "expected_last_line": "  "},
    )

    assert blank["ok"] is False
    assert blank["reason"] == "semantic_edit_apply_input_invalid"
    assert blank["invalid_fields"] == ["expected_last_line"]
    assert "Both one-call anchors must be non-blank lines" in blank["next_call"]
    assert target.read_bytes() == _BEFORE
