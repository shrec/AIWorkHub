"""NF-2026-01928: a NEW file is made by the semantic editor, never by Write.

Until now a new file could only be made with a built-in whole-file Write:
``scripts/manager_semantic_edit.py`` and the worker ``semantic_edit_prepare``
/``semantic_edit_apply`` only replace a hash-verified range of a file that
already exists. The create mode is one shared definition
(``semantic_edit_applier.create_new_file``) behind both callers, and these
tests pin what it guarantees: the bytes land exactly, nothing that exists is
ever overwritten, the edit's containment checks apply unchanged, and a worker
create is held to ``allowed_writes`` and leaves the same ledger receipt an
apply does.

Run: python3 -m pytest -q tests/test_semantic_edit_create_nf01928.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from aiworkhub import semantic_edit, semantic_edit_applier, stdio_fastmcp  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as worker_tools  # noqa: E402

CLI = _ROOT / "scripts" / "manager_semantic_edit.py"
_PREPARE = "aiworkhub_worker_semantic_edit_prepare"
_APPLY = "aiworkhub_worker_semantic_edit_apply"
# CRLF, a bare CR, non-ASCII and no trailing newline: any translation shows.
_CONTENT = "alpha\r\nbeta\rgamma\nβ—\n\tno trailing newline".encode("utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "existing.py").write_bytes(b"one\ntwo\n")
    return root


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows without the privilege a junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlinks are unavailable here")
    import _winapi

    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError:
        pytest.skip("neither a directory symlink nor a junction can be made here")


def _cli(root: Path, *args: str, data: bytes = _CONTENT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), "--repo", str(root), *args],
        input=data, capture_output=True,
    )


def _refusal(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 1, result
    return json.loads(result.stderr.decode("utf-8"))["reason"]


# -- manager CLI ------------------------------------------------------------


def test_cli_create_writes_stdin_byte_exact_and_prints_its_sha256(tmp_path):
    root = _repo(tmp_path)

    result = _cli(root, "--create", "--path", "notes/new.txt")

    assert result.returncode == 0, result.stderr
    assert (root / "notes" / "new.txt").read_bytes() == _CONTENT
    receipt = json.loads(result.stdout.decode("utf-8"))
    assert receipt["ok"] is True
    assert receipt["schema_id"] == "aiworkhub.manager_semantic_edit_receipt.v1"
    assert receipt["path"] == "notes/new.txt"
    assert receipt["created"] is True
    assert receipt["after_sha256"] == hashlib.sha256(_CONTENT).hexdigest()
    assert receipt["file_bytes"] == receipt["replacement_bytes"] == len(_CONTENT)
    assert receipt["old_region_bytes"] == 0
    assert receipt["model_reemitted_old_bytes"] == 0


def test_cli_create_reads_new_file_byte_exact(tmp_path):
    root = _repo(tmp_path)
    source = tmp_path / "content.bin"
    source.write_bytes(_CONTENT)

    result = _cli(root, "--create", "--path", "new.txt", "--new-file", str(source), data=b"")

    assert result.returncode == 0, result.stderr
    assert (root / "new.txt").read_bytes() == _CONTENT


def test_a_second_cli_create_on_the_same_path_is_refused_unchanged(tmp_path):
    root = _repo(tmp_path)
    assert _cli(root, "--create", "--path", "notes/new.txt").returncode == 0

    again = _cli(root, "--create", "--path", "notes/new.txt", data=b"other\n")

    assert _refusal(again) == "semantic_edit_create_target_exists:notes/new.txt"
    assert (root / "notes" / "new.txt").read_bytes() == _CONTENT


def test_cli_create_refuses_an_existing_file(tmp_path):
    root = _repo(tmp_path)

    refused = _cli(root, "--create", "--path", "src/existing.py", data=b"x = 1\n")

    assert _refusal(refused) == "semantic_edit_create_target_exists:src/existing.py"
    assert (root / "src" / "existing.py").read_bytes() == b"one\ntwo\n"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("../outside.txt", "semantic_edit_path_escape:../outside.txt"),
        ("src/../../outside.txt", "semantic_edit_path_escape:src/../../outside.txt"),
        (".git/hooks/pre-commit", "semantic_edit_path_escape:.git/hooks/pre-commit"),
    ],
)
def test_cli_create_refuses_a_dot_dot_path(tmp_path, path, reason):
    root = _repo(tmp_path)

    assert _refusal(_cli(root, "--create", "--path", path)) == reason
    assert not (tmp_path / "outside.txt").exists()


def test_cli_create_refuses_an_absolute_path_outside_the_repo_root(tmp_path):
    root = _repo(tmp_path)
    outside = tmp_path / "elsewhere" / "new.txt"

    reason = _refusal(_cli(root, "--create", "--path", str(outside)))

    assert reason.startswith("semantic_edit_path_escape:")
    assert not outside.exists()


def test_cli_create_refuses_a_symlink_or_junction_parent(tmp_path):
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _link_dir(root / "linked", outside)

    reason = _refusal(_cli(root, "--create", "--path", "linked/new.txt"))

    assert reason == "semantic_edit_symlink_forbidden:linked/new.txt"
    assert list(outside.iterdir()) == []


def test_cli_create_refuses_a_file_used_as_a_parent_directory(tmp_path):
    root = _repo(tmp_path)

    reason = _refusal(_cli(root, "--create", "--path", "src/existing.py/new.txt"))

    assert reason == "semantic_edit_target_invalid:src/existing.py/new.txt"


def test_cli_create_refuses_a_python_file_that_does_not_parse(tmp_path):
    root = _repo(tmp_path)

    reason = _refusal(_cli(root, "--create", "--path", "src/bad.py", data=b"def (:\n"))

    assert reason.startswith("semantic_edit_syntax_regression:src/bad.py:")
    assert not (root / "src" / "bad.py").exists()


def test_cli_create_refuses_content_that_is_not_utf8(tmp_path):
    root = _repo(tmp_path)

    result = _cli(root, "--create", "--path", "new.txt", data=b"\xff\xfe")

    assert result.returncode == 1
    assert json.loads(result.stderr.decode("utf-8"))["error"] == "replacement_not_utf8"
    assert not (root / "new.txt").exists()


@pytest.mark.parametrize(
    "extra",
    [("--start", "1"), ("--end", "1"), ("--expected-first-line", "x")],
)
def test_cli_create_takes_no_range_arguments(tmp_path, extra):
    root = _repo(tmp_path)

    result = _cli(root, "--create", "--path", "new.txt", *extra)

    assert result.returncode == 2
    assert not (root / "new.txt").exists()


def test_cli_edit_still_requires_a_range(tmp_path):
    root = _repo(tmp_path)

    result = _cli(root, "--path", "src/existing.py", data=b"ONE\n")

    assert result.returncode == 2
    assert (root / "src" / "existing.py").read_bytes() == b"one\ntwo\n"


def test_the_shared_create_refuses_a_path_outside_allowed_writes(tmp_path):
    root = _repo(tmp_path)

    with pytest.raises(
        semantic_edit.SemanticEditError, match="semantic_edit_path_not_allowed:docs/x.md"
    ):
        semantic_edit_applier.create_new_file(
            root, "docs/x.md", "x\n", allowed_writes=["src/*.py"]
        )
    assert not (root / "docs").exists()


# -- worker semantic edit tools -----------------------------------------------


class _Worker:
    """The worker MCP tools reached the way a model reaches them."""

    def __init__(self, root: Path, tmp_path: Path) -> None:
        self.ledger = tmp_path / "ledger.jsonl"
        self.key = tmp_path / "ledger.key"
        self.key.write_bytes(b"k" * 32)
        self.server = stdio_fastmcp.FallbackFastMCP("create")
        worker_tools.register_tools(
            self.server,
            worker_tools.WorkerToolContext(
                task_id="TASK", runner="runner", topic="topic", request_id="request",
                repo=root, authority_repo=root, source_graph_targets=("src/existing.py",),
                session_topic="topic", audit_ledger_path=self.ledger,
                audit_hmac_key_path=self.key,
                allowed_writes=("src/*.py", "src/existing.py", "notes/new.txt"),
            ),
        )

    def rpc(self, method: str, params: dict[str, Any]) -> Any:
        return stdio_fastmcp._dispatch(self.server.name, self.server._tools, method, params)

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        reply = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        assert "isError" not in reply, reply
        return json.loads(reply["content"][0]["text"])

    def create(self, path: str, new: str, key: str) -> dict[str, Any]:
        prepared = self.call(_PREPARE, file_path=path, start_line=0, end_line=0, create=True)
        assert prepared["ok"] is True, prepared
        return self.call(_APPLY, target_id=prepared["target_id"], new=new, idempotency_key=key)

    def verify(self) -> dict[str, Any]:
        return worker_tools.verify_audit_ledger(
            self.ledger, self.key,
            task_id="TASK", runner="runner", topic="topic", request_id="request",
        )


def test_worker_create_writes_the_new_file_and_an_apply_receipt(tmp_path):
    root = _repo(tmp_path)
    worker = _Worker(root, tmp_path)
    content = _CONTENT.decode("utf-8")

    prepared = worker.call(_PREPARE, file_path="notes/new.txt", start_line=0, end_line=0,
                           create=True)
    assert prepared["ok"] is True, prepared
    assert (prepared["path"], prepared["create"]) == ("notes/new.txt", True)
    assert not (root / "notes" / "new.txt").exists(), "prepare must create nothing"

    applied = worker.call(_APPLY, target_id=prepared["target_id"], new=content,
                          idempotency_key="create-1")

    assert applied["ok"] is True, applied
    assert (root / "notes" / "new.txt").read_bytes() == _CONTENT
    assert applied["schema_id"] == "aiworkhub.semantic_edit_apply_receipt.v1"
    assert applied["created"] is True
    assert applied["path"] == "notes/new.txt"
    assert applied["before_sha256"] == ""
    assert applied["after_sha256"] == hashlib.sha256(_CONTENT).hexdigest()
    assert applied["file_bytes"] == applied["replacement_bytes"] == len(_CONTENT)


def test_worker_create_leaves_the_same_ledger_entry_kind_as_an_apply(tmp_path):
    root = _repo(tmp_path)
    worker = _Worker(root, tmp_path)

    edited = worker.call(
        _APPLY, file_path="src/existing.py", start_line=2, end_line=2,
        expected_first_line="two", expected_last_line="two", new="TWO\n",
        idempotency_key="edit-1",
    )
    assert edited["ok"] is True, edited
    assert worker.create("src/fresh.py", "x = 1\n", "create-1")["ok"] is True

    verification = worker.verify()
    assert verification["entries_tampered"] == 0
    assert verification["successful_call_count_by_tool"]["semantic_edit_apply"] == 2
    receipts = verification["semantic_edit_apply_receipts"]
    assert [row["path_sha256"] for row in receipts] == [
        hashlib.sha256(path.encode("utf-8")).hexdigest()
        for path in ("src/existing.py", "src/fresh.py")
    ]
    assert receipts[1]["replacement_bytes"] == len(b"x = 1\n")
    assert receipts[1]["range_count"] == 1
    assert set(receipts[0]) == set(receipts[1])


def test_worker_create_outside_allowed_writes_is_refused(tmp_path):
    root = _repo(tmp_path)
    worker = _Worker(root, tmp_path)

    refused = worker.call(_PREPARE, file_path="docs/x.md", start_line=0, end_line=0,
                          create=True)

    assert refused["ok"] is False
    assert refused["reason"] == "semantic_edit_path_not_allowed:docs/x.md"
    assert not (root / "docs").exists()


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("src/existing.py", "semantic_edit_create_target_exists:src/existing.py"),
        ("../src/x.py", "semantic_edit_path_escape:../src/x.py"),
    ],
)
def test_worker_create_applies_the_edit_containment_checks(tmp_path, path, reason):
    root = _repo(tmp_path)
    worker = _Worker(root, tmp_path)

    refused = worker.call(_PREPARE, file_path=path, start_line=0, end_line=0, create=True)

    assert (refused["ok"], refused["reason"]) == (False, reason)
    assert (root / "src" / "existing.py").read_bytes() == b"one\ntwo\n"


def test_worker_create_refuses_a_symlink_or_junction_parent(tmp_path):
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "src" / "existing.py").unlink()
    (root / "src").rmdir()
    _link_dir(root / "src", outside)
    worker = _Worker(root, tmp_path)

    refused = worker.call(_PREPARE, file_path="src/new.py", start_line=0, end_line=0,
                          create=True)

    assert (refused["ok"], refused["reason"]) == (
        False, "semantic_edit_symlink_forbidden:src/new.py"
    )
    assert list(outside.iterdir()) == []


def test_a_path_that_appeared_after_prepare_is_not_overwritten(tmp_path):
    root = _repo(tmp_path)
    worker = _Worker(root, tmp_path)
    prepared = worker.call(_PREPARE, file_path="src/late.py", start_line=0, end_line=0,
                           create=True)
    (root / "src" / "late.py").write_bytes(b"theirs = 1\n")

    applied = worker.call(_APPLY, target_id=prepared["target_id"], new="mine = 1\n",
                          idempotency_key="create-late")

    assert (applied["ok"], applied["reason"]) == (
        False, "semantic_edit_create_target_exists:src/late.py"
    )
    assert (root / "src" / "late.py").read_bytes() == b"theirs = 1\n"


def test_a_repeated_create_apply_replays_instead_of_writing_again(tmp_path):
    root = _repo(tmp_path)
    worker = _Worker(root, tmp_path)
    prepared = worker.call(_PREPARE, file_path="src/once.py", start_line=0, end_line=0,
                           create=True)
    arguments = {"target_id": prepared["target_id"], "new": "a = 1\n",
                 "idempotency_key": "create-once"}

    first = worker.call(_APPLY, **arguments)
    again = worker.call(_APPLY, **arguments)

    assert first["ok"] is True and first["idempotent_replay"] is False
    assert again["ok"] is True and again["idempotent_replay"] is True
    assert (root / "src" / "once.py").read_bytes() == b"a = 1\n"


@pytest.mark.parametrize(
    ("arguments", "invalid_fields"),
    [
        ({"start_line": 1, "end_line": 1}, ["start_line", "end_line"]),
        ({"expected_first_line": "x"}, ["expected_first_line"]),
    ],
)
def test_worker_create_names_each_bad_input(tmp_path, arguments, invalid_fields):
    root = _repo(tmp_path)
    worker = _Worker(root, tmp_path)
    call = {"file_path": "src/new.py", "start_line": 0, "end_line": 0, "create": True}

    refused = worker.call(_PREPARE, **{**call, **arguments})

    assert refused["ok"] is False
    assert refused["reason"] == "semantic_edit_create_input_invalid"
    assert refused["invalid_fields"] == invalid_fields
    assert "create: true" in refused["next_call"]
    assert not (root / "src" / "new.py").exists()


def test_create_is_an_option_on_the_existing_prepare_tool(tmp_path):
    """No new tool name, so no tool allowlist changes; apply's inputs are unchanged."""
    worker = _Worker(_repo(tmp_path), tmp_path)

    listed = {tool["name"]: tool for tool in worker.rpc("tools/list", {})["tools"]}

    assert not any("create" in name for name in listed if "semantic_edit" in name)
    prepare = listed[_PREPARE]["inputSchema"]
    assert prepare["properties"]["create"]["type"] == "boolean"
    assert "create" not in prepare.get("required", [])
    assert "create" not in listed[_APPLY]["inputSchema"]["properties"]
