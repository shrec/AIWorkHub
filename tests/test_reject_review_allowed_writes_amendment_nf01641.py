"""NF-2026-01641: allowed_writes_amendment on reject_review."""
from __future__ import annotations

import inspect
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store, task_templates  # noqa: E402

_NOW = "2026-10-24T00:00:00+00:00"
_RUNNER = "claude_task_mcp_runtime_wiring"
_TOPIC = "task_mcp"
_MANAGER = "nf01641-manager"
_REVIEW_REQUEST_ID = "c" * 32
_MAX = task_templates.MAX_PATHS_PER_FIELD

_INVALID_WRITES = "invalid_allowed_writes"
_BAD_PATH = "invalid_allowed_write_path"
_RESERVED = "allowed_write_reserved_path"
_DIRECTORY = "allowed_write_is_directory"
_CONTRADICTORY = "contradictory_task_path_contract"
_FORBIDDEN_CARD = {"forbidden": ["secrets/", "src/frozen.py", "src/secret_*.py"]}


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _insert_task(root: Path, task_id: str, extra_card: dict | None = None) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    card = {"origin_thread_id": "nf01641-thread", **(extra_card or {})}
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, _RUNNER, _TOPIC, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps(card), _NOW, _NOW, None, "nf01641-thread"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_review_request_id(root: Path, task_id: str) -> None:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row[0])
        terminal = card.get("terminal_review")
        terminal = terminal if isinstance(terminal, dict) else {}
        evidence = terminal.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        evidence["request_identity"] = {"request_id": _REVIEW_REQUEST_ID, "task_id": task_id}
        terminal["evidence"] = evidence
        card["terminal_review"] = terminal
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id)
        )
        conn.commit()
    finally:
        conn.close()


def _row_snapshot(root: Path, task_id: str) -> tuple:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        row = conn.execute(
            "SELECT status, worker_status, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    return (*row, events)


def _must_not_run(*_args, **_kwargs):
    pytest.fail("refusal must happen before the manager actor and write gate")


@pytest.fixture
def repo(monkeypatch, tmp_path) -> Path:
    root = _init_repo(tmp_path)
    (root / "docs_dir").mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    return root


def _review_ready_task(
    monkeypatch, root: Path, task_id: str, extra_card: dict | None = None
) -> None:
    _insert_task(root, task_id, extra_card)
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: _MANAGER)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    assert core.claim_start_exact(task_id, _RUNNER, _TOPIC)["ok"] is True
    assert core.mark_review(task_id, runner=_RUNNER, topic=_TOPIC)["ok"] is True
    _seed_review_request_id(root, task_id)


def test_apply_allowed_writes_amendment_appends_in_order_dedupes_and_records():
    card = {"allowed_writes": ["src/a.py"]}

    applied = task_store.apply_allowed_writes_amendment(
        card,
        ["src/b.py", "src/a.py", "./src/b.py", "src/c.py", "src/b.py"],
        actor="mgr",
        request_id="req-1",
    )

    assert applied == ["src/b.py", "src/c.py"]
    assert card["allowed_writes"] == ["src/a.py", "src/b.py", "src/c.py"]
    (entry,) = card["allowed_writes_amendments"]
    assert entry["paths"] == ["src/b.py", "src/c.py"]
    assert entry["actor"] == "mgr"
    assert entry["request_id"] == "req-1"
    assert datetime.fromisoformat(entry["at"]).utcoffset() == timedelta(0)


def test_apply_allowed_writes_amendment_existing_path_is_a_no_op():
    card = {"allowed_writes": ["src/a.py"]}

    applied = task_store.apply_allowed_writes_amendment(
        card, ["src/a.py", "./src/a.py", "src\\a.py"], actor="mgr", request_id="req-1"
    )

    assert applied == []
    assert card == {"allowed_writes": ["src/a.py"]}


def test_apply_allowed_writes_amendment_keeps_prior_history_and_order():
    history = [
        {"paths": ["src/old.py"], "actor": "m0", "request_id": "r0", "at": "2026-01-01T00:00:00+00:00"}
    ]
    card = {"allowed_writes": ["src/z.py", "src/a.py", "src/old.py"],
            "allowed_writes_amendments": list(history)}

    task_store.apply_allowed_writes_amendment(
        card, ["src/new.py"], actor="m1", request_id="r1"
    )

    assert card["allowed_writes"] == ["src/z.py", "src/a.py", "src/old.py", "src/new.py"]
    assert card["allowed_writes_amendments"][0] == history[0]
    assert [e["request_id"] for e in card["allowed_writes_amendments"]] == ["r0", "r1"]


def test_apply_allowed_writes_amendment_creates_missing_fields():
    card: dict = {}

    applied = task_store.apply_allowed_writes_amendment(
        card, ["src/a.py"], actor="mgr", request_id="req-1"
    )

    assert applied == ["src/a.py"]
    assert card["allowed_writes"] == ["src/a.py"]
    assert len(card["allowed_writes_amendments"]) == 1


def test_apply_allowed_writes_amendment_empty_input_is_a_no_op():
    card = {"allowed_writes": ["src/a.py"]}

    assert task_store.apply_allowed_writes_amendment(
        card, [], actor="mgr", request_id="req-1"
    ) == []
    assert card == {"allowed_writes": ["src/a.py"]}


def test_apply_allowed_writes_amendment_over_limit_raises_without_mutating_the_card():
    existing = [f"src/existing_{i}.py" for i in range(_MAX)]
    card = {"allowed_writes": list(existing)}

    with pytest.raises(ValueError, match="allowed_writes_amendment_exceeds_limit"):
        task_store.apply_allowed_writes_amendment(
            card, ["src/one_more.py"], actor="mgr", request_id="req-1"
        )

    assert card == {"allowed_writes": existing}


def test_apply_allowed_writes_amendment_at_the_limit_with_only_duplicates_succeeds():
    existing = [f"src/existing_{i}.py" for i in range(_MAX)]
    card = {"allowed_writes": list(existing)}

    applied = task_store.apply_allowed_writes_amendment(
        card, [existing[0]], actor="mgr", request_id="req-1"
    )

    assert applied == []
    assert card == {"allowed_writes": existing}


@pytest.mark.parametrize(
    "path",
    [
        "src/new.py",
        "./src/new.py",
        "docs/*.md",
        "Makefile",
        ".gitignore",
        ".github/workflows/ci.yml",
        ".aiworkhub/config.json",
    ],
)
def test_validator_accepts_ordinary_and_dot_prefixed_paths(repo, path):
    assert core._validate_allowed_writes_amendment_paths(_FORBIDDEN_CARD, [path]) is None


def test_validator_accepts_exactly_the_per_field_limit(repo):
    paths = [f"src/file_{i}.py" for i in range(_MAX)]

    assert core._validate_allowed_writes_amendment_paths({}, paths) is None


@pytest.mark.parametrize(
    ("paths", "code"),
    [
        pytest.param(["../outside.py"], _BAD_PATH, id="parent-traversal"),
        pytest.param(["src/../../outside.py"], _BAD_PATH, id="nested-traversal"),
        pytest.param(["src\\..\\..\\outside.py"], _BAD_PATH, id="backslash-traversal"),
        pytest.param(["/etc/passwd"], _BAD_PATH, id="posix-absolute"),
        pytest.param(["C:/Windows/system32/x.py"], _BAD_PATH, id="windows-drive"),
        pytest.param(["C:\\Windows\\x.py"], _BAD_PATH, id="windows-drive-backslash"),
        pytest.param(["\\\\server\\share\\x.py"], _BAD_PATH, id="unc"),
        pytest.param(["src/x\x00.py"], _BAD_PATH, id="nul-byte"),
        pytest.param([".git/config"], _RESERVED, id="git-dir"),
        pytest.param([".git/hooks/pre-commit"], _RESERVED, id="git-hook"),
        pytest.param([".git/"], _RESERVED, id="git-dir-trailing-slash"),
        pytest.param([".GIT/config"], _RESERVED, id="git-dir-case-insensitive"),
        pytest.param([".aiworkhub/runtime/state.db"], _RESERVED, id="runtime-dir"),
        pytest.param(["./.aiworkhub/runtime/x.json"], _RESERVED, id="runtime-dir-dot-prefix"),
        pytest.param([".aiworkhub\\runtime\\x.json"], _RESERVED, id="runtime-dir-backslash"),
        pytest.param(["docs/"], _DIRECTORY, id="trailing-slash-directory"),
        pytest.param(["docs_dir"], _DIRECTORY, id="existing-directory"),
        pytest.param(["secrets/keys.py"], _CONTRADICTORY, id="forbidden-directory"),
        pytest.param(["src/frozen.py"], _CONTRADICTORY, id="forbidden-file"),
        pytest.param(["src/secret_keys.py"], _CONTRADICTORY, id="forbidden-glob"),
        pytest.param(["src/ok.py", "../bad.py"], _BAD_PATH, id="one-bad-entry-refuses-all"),
        pytest.param("src/a.py", _INVALID_WRITES, id="not-a-list"),
        pytest.param([""], _INVALID_WRITES, id="blank-entry"),
        pytest.param([None], _INVALID_WRITES, id="null-entry"),
        pytest.param(["x" * 1001], _INVALID_WRITES, id="entry-too-long"),
        pytest.param(
            [f"src/file_{i}.py" for i in range(_MAX + 1)], _INVALID_WRITES, id="over-limit"
        ),
    ],
)
def test_validator_refuses_with_task_create_error_codes(repo, paths, code):
    result = core._validate_allowed_writes_amendment_paths(_FORBIDDEN_CARD, paths)

    assert result is not None
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert result["stderr"] == code


def test_validator_refusal_details_name_the_cause(repo):
    reserved = core._validate_allowed_writes_amendment_paths({}, [".git/config"])
    runtime = core._validate_allowed_writes_amendment_paths({}, [".aiworkhub/runtime/a"])
    directory = core._validate_allowed_writes_amendment_paths({}, ["docs/"])
    forbidden = core._validate_allowed_writes_amendment_paths(
        {"forbidden": ["secrets/"]}, ["secrets/keys.py"]
    )

    assert (reserved["allowed_write"], reserved["reserved_root"]) == (".git/config", ".git")
    assert runtime["reserved_root"] == ".aiworkhub/runtime"
    assert directory["allowed_write"] == "docs/"
    assert forbidden["conflicts"] == [
        {"field": "allowed_writes", "path": "secrets/keys.py", "forbidden": "secrets/"}
    ]


def test_validator_ignores_forbidden_text_in_the_cards_objective_and_validation(repo):
    card = {
        "forbidden": ["secrets/"],
        "objective": "never read secrets/ from the worker",
        "validation": ["pytest -q tests/test_secrets/x.py"],
    }

    assert core._validate_allowed_writes_amendment_paths(card, ["src/new.py"]) is None


def test_reject_review_appends_paths_to_the_pending_card_and_records_them(monkeypatch, repo):
    task_id = "NF01641_APPLY"
    _review_ready_task(
        monkeypatch,
        repo,
        task_id,
        {"allowed_writes": ["src/existing.py"], "validation": ["pytest -q tests/existing.py"]},
    )

    result = core.reject_review(
        task_id,
        "scope too narrow",
        topic=_TOPIC,
        allowed_writes_amendment=[
            "tests/test_new.py",
            "src/existing.py",
            "./tests/test_new.py",
            ".github/workflows/ci.yml",
        ],
    )

    assert result["ok"] is True, result
    assert result["allowed_writes_amendment_applied"] == [
        "tests/test_new.py",
        ".github/workflows/ci.yml",
    ]
    status, worker_status, _card_json, _events = _row_snapshot(repo, task_id)
    assert (status, worker_status) == ("pending", "unclaimed")
    card = task_store.get_task(repo, task_id)
    assert card["allowed_writes"] == [
        "src/existing.py",
        "tests/test_new.py",
        ".github/workflows/ci.yml",
    ]
    assert card["validation"] == ["pytest -q tests/existing.py"]
    (entry,) = card["allowed_writes_amendments"]
    assert entry["paths"] == ["tests/test_new.py", ".github/workflows/ci.yml"]
    assert entry["actor"] == _MANAGER
    assert entry["request_id"] == _REVIEW_REQUEST_ID
    assert datetime.fromisoformat(entry["at"]).utcoffset() == timedelta(0)


def test_reject_review_applies_both_amendments_in_one_transaction(monkeypatch, repo):
    task_id = "NF01641_BOTH"
    _review_ready_task(
        monkeypatch,
        repo,
        task_id,
        {"allowed_writes": ["src/existing.py"], "validation": ["pytest -q tests/existing.py"]},
    )

    result = core.reject_review(
        task_id,
        "widen scope and evidence",
        topic=_TOPIC,
        validation_amendment=["pytest -q tests/test_new_nf01641.py"],
        allowed_writes_amendment=["tests/test_new_nf01641.py"],
    )

    assert result["ok"] is True, result
    assert result["validation_amendment_applied"] == ["pytest -q tests/test_new_nf01641.py"]
    assert result["allowed_writes_amendment_applied"] == ["tests/test_new_nf01641.py"]
    card = task_store.get_task(repo, task_id)
    assert card["validation"] == [
        "pytest -q tests/existing.py",
        "pytest -q tests/test_new_nf01641.py",
    ]
    assert card["allowed_writes"] == ["src/existing.py", "tests/test_new_nf01641.py"]


def test_reject_review_amendment_of_an_existing_path_is_a_no_op(monkeypatch, repo):
    task_id = "NF01641_NOOP"
    _review_ready_task(monkeypatch, repo, task_id, {"allowed_writes": ["src/existing.py"]})

    result = core.reject_review(
        task_id,
        "nothing to widen",
        topic=_TOPIC,
        allowed_writes_amendment=["src/existing.py", "./src/existing.py"],
    )

    assert result["ok"] is True, result
    assert result["allowed_writes_amendment_applied"] == []
    card = task_store.get_task(repo, task_id)
    assert card["allowed_writes"] == ["src/existing.py"]
    assert "allowed_writes_amendments" not in card


@pytest.mark.parametrize("omitted", ["absent", "none", "empty"])
def test_reject_review_without_an_amendment_leaves_the_card_as_before(
    monkeypatch, repo, omitted
):
    task_id = "NF01641_OMIT_" + omitted.upper()
    _review_ready_task(monkeypatch, repo, task_id, {"allowed_writes": ["src/existing.py"]})
    kwargs = {
        "absent": {},
        "none": {"allowed_writes_amendment": None},
        "empty": {"allowed_writes_amendment": []},
    }[omitted]

    result = core.reject_review(task_id, "repair", topic=_TOPIC, **kwargs)

    assert result["ok"] is True, result
    assert "allowed_writes_amendment_applied" not in result
    card = task_store.get_task(repo, task_id)
    assert card["allowed_writes"] == ["src/existing.py"]
    assert "allowed_writes_amendments" not in card


def test_reject_review_signature_defaults_the_amendment_to_none():
    parameter = inspect.signature(core.reject_review).parameters["allowed_writes_amendment"]

    assert parameter.default is None


@pytest.mark.parametrize("disposition", ["blocked", "archived", "superseded"])
def test_reject_review_refuses_the_amendment_unless_the_disposition_is_pending(
    monkeypatch, repo, disposition
):
    task_id = "NF01641_NOT_PENDING_" + disposition.upper()
    _review_ready_task(monkeypatch, repo, task_id, {"allowed_writes": ["src/existing.py"]})
    monkeypatch.setattr(core, "_verified_manager_actor", _must_not_run)
    monkeypatch.setattr(core, "_canonical_write_gate", _must_not_run)
    before = _row_snapshot(repo, task_id)

    result = core.reject_review(
        task_id,
        "park it",
        topic=_TOPIC,
        to=disposition,
        allowed_writes_amendment=["src/new.py"],
    )

    assert result["ok"] is False
    assert result["stderr"] == "allowed_writes_amendment_requires_pending"
    assert _row_snapshot(repo, task_id) == before


def test_reject_review_refuses_invalid_amendments_before_any_state_change(monkeypatch, repo):
    task_id = "NF01641_REFUSED"
    _review_ready_task(
        monkeypatch,
        repo,
        task_id,
        {"allowed_writes": ["src/existing.py"], "forbidden": ["secrets/"]},
    )
    monkeypatch.setattr(core, "_verified_manager_actor", _must_not_run)
    monkeypatch.setattr(core, "_canonical_write_gate", _must_not_run)
    before = _row_snapshot(repo, task_id)
    refused = [
        (["../outside.py"], _BAD_PATH),
        (["/etc/passwd"], _BAD_PATH),
        (["C:/Windows/x.py"], _BAD_PATH),
        ([".git/config"], _RESERVED),
        ([".aiworkhub/runtime/state.db"], _RESERVED),
        ([".gi*/config"], _RESERVED),
        ([".[g]it/config"], _RESERVED),
        ([".aiwork*"], _RESERVED),
        (["docs/"], _DIRECTORY),
        (["docs_dir"], _DIRECTORY),
        (["secrets/keys.py"], _CONTRADICTORY),
        (["src/good.py", "../bad.py"], _BAD_PATH),
        ([f"src/file_{i}.py" for i in range(_MAX + 1)], _INVALID_WRITES),
        ("src/new.py", _INVALID_WRITES),
    ]

    for paths, code in refused:
        result = core.reject_review(
            task_id, "widen", topic=_TOPIC, allowed_writes_amendment=paths
        )

        assert result["ok"] is False, paths
        assert result["stderr"] == code, paths
        assert _row_snapshot(repo, task_id) == before, paths


def test_reject_review_refuses_a_merged_total_over_the_limit_without_state_change(
    monkeypatch, repo
):
    task_id = "NF01641_MERGED_LIMIT"
    existing = [f"src/existing_{i}.py" for i in range(_MAX - 1)]
    _review_ready_task(monkeypatch, repo, task_id, {"allowed_writes": list(existing)})
    before = _row_snapshot(repo, task_id)

    refused = core.reject_review(
        task_id,
        "widen",
        topic=_TOPIC,
        allowed_writes_amendment=["src/one.py", "src/two.py"],
    )

    assert refused["ok"] is False
    assert refused["stderr"] == (
        "invalid_allowed_writes_amendment:allowed_writes_amendment_exceeds_limit"
    )
    assert _row_snapshot(repo, task_id) == before

    accepted = core.reject_review(
        task_id, "widen", topic=_TOPIC, allowed_writes_amendment=["src/one.py"]
    )

    assert accepted["ok"] is True, accepted
    assert task_store.get_task(repo, task_id)["allowed_writes"] == [*existing, "src/one.py"]


def test_server_forwards_allowed_writes_amendment_only_when_non_empty(monkeypatch, repo):
    server = pytest.importorskip("aiworkhub.server")
    calls: list[dict] = []

    def fake_reject_review(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "returncode": 126, "command": [], "stdout": "", "stderr": "stub"}

    monkeypatch.setattr(core, "reject_review", fake_reject_review)

    server.aiworkhub_task_reject_review("T1", "reason")
    server.aiworkhub_task_reject_review("T1", "reason", allowed_writes_amendment=None)
    server.aiworkhub_task_reject_review("T1", "reason", allowed_writes_amendment=[])
    server.aiworkhub_task_reject_review(
        "T1", "reason", allowed_writes_amendment=["src/new.py", "tests/test_new.py"]
    )

    base = {"task_id": "T1", "reason": "reason", "to": "pending"}
    assert calls == [
        base,
        base,
        base,
        {**base, "allowed_writes_amendment": ["src/new.py", "tests/test_new.py"]},
    ]


def test_server_tool_declares_and_documents_the_parameter():
    server = pytest.importorskip("aiworkhub.server")
    tool = server.aiworkhub_task_reject_review

    parameter = inspect.signature(tool).parameters["allowed_writes_amendment"]

    assert parameter.default is None
    assert "allowed_writes_amendment" in (tool.__doc__ or "")
