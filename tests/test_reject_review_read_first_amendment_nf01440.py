"""NF-2026-01440: read_first_amendment on reject_review."""
from __future__ import annotations

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

_NOW = "2026-10-26T00:00:00+00:00"
_RUNNER = "claude_task_mcp_runtime_wiring"
_TOPIC = "task_mcp"
_MANAGER = "nf01440-manager"
_REVIEW_REQUEST_ID = "d" * 32
_MAX = task_templates.MAX_PATHS_PER_FIELD


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _insert_task(root: Path, task_id: str, extra_card: dict | None = None) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    card = {"origin_thread_id": "nf01440-thread", **(extra_card or {})}
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, _RUNNER, _TOPIC, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps(card), _NOW, _NOW, None, "nf01440-thread"),
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
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
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


@pytest.fixture
def repo(monkeypatch, tmp_path) -> Path:
    root = _init_repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "exists.py").write_text("x = 1\n", encoding="utf-8")
    (root / "docs_dir").mkdir()
    (root / "docs_dir" / "notes.md").write_text("notes\n", encoding="utf-8")
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


def _must_not_run(*_args, **_kwargs):
    raise AssertionError("must not run before the amendment is refused")


def test_apply_read_first_amendment_appends_dedupes_by_exact_string_and_records():
    card = {"read_first": ["src/a.py", "src/b.py:1-5"]}

    applied = task_store.apply_read_first_amendment(
        card,
        ["src/b.py:1-5", "src/b.py", "src/c.py", "src/c.py", "  src/d.py  "],
        actor="mgr",
        request_id="req-1",
    )

    assert applied == ["src/b.py", "src/c.py", "src/d.py"]
    assert card["read_first"] == ["src/a.py", "src/b.py:1-5", "src/b.py", "src/c.py", "src/d.py"]
    (entry,) = card["read_first_amendments"]
    assert entry["paths"] == applied
    assert (entry["actor"], entry["request_id"]) == ("mgr", "req-1")
    assert datetime.fromisoformat(entry["at"]).utcoffset() == timedelta(0)


def test_apply_read_first_amendment_with_only_duplicates_or_nothing_leaves_the_card_alone():
    card = {"read_first": ["src/a.py"]}
    assert task_store.apply_read_first_amendment(card, [], actor="m", request_id="r") == []
    assert task_store.apply_read_first_amendment(card, ["src/a.py"], actor="m", request_id="r") == []
    assert card == {"read_first": ["src/a.py"]}


def test_apply_read_first_amendment_over_limit_raises_without_mutating_the_card():
    existing = [f"src/existing_{i}.py" for i in range(_MAX)]
    card = {"read_first": list(existing)}

    with pytest.raises(ValueError, match="read_first_amendment_exceeds_limit"):
        task_store.apply_read_first_amendment(card, ["src/new.py"], actor="m", request_id="r")

    assert card == {"read_first": existing}
    assert task_store.apply_read_first_amendment(card, [existing[0]], actor="m", request_id="r") == []


def test_reject_review_appends_read_first_with_dedupe_and_records_history(monkeypatch, repo):
    task_id = "NF01440_APPLY"
    _review_ready_task(
        monkeypatch,
        repo,
        task_id,
        {"read_first": ["src/exists.py"], "allowed_writes": ["src/exists.py"]},
    )

    result = core.reject_review(
        task_id,
        "read these first",
        topic=_TOPIC,
        read_first_amendment=[
            "docs_dir/notes.md",
            "src/exists.py",
            "docs_dir/notes.md",
            "src/exists.py:3-9",
        ],
    )

    assert result["ok"] is True, result
    assert result["read_first_amendment_applied"] == ["docs_dir/notes.md", "src/exists.py:3-9"]
    assert "allowed_writes_amendment_applied" not in result
    status, worker_status, _card_json, _events = _row_snapshot(repo, task_id)
    assert (status, worker_status) == ("pending", "unclaimed")
    card = task_store.get_task(repo, task_id)
    assert card["read_first"] == ["src/exists.py", "docs_dir/notes.md", "src/exists.py:3-9"]
    assert card["allowed_writes"] == ["src/exists.py"]
    (entry,) = card["read_first_amendments"]
    assert entry["paths"] == ["docs_dir/notes.md", "src/exists.py:3-9"]
    assert entry["actor"] == _MANAGER
    assert entry["request_id"] == _REVIEW_REQUEST_ID
    assert datetime.fromisoformat(entry["at"]).utcoffset() == timedelta(0)


def test_reject_review_accepts_the_line_range_suffixes(monkeypatch, repo):
    task_id = "NF01440_SUFFIX"
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": []})

    result = core.reject_review(
        task_id,
        "ranges",
        topic=_TOPIC,
        read_first_amendment=["src/exists.py:12", "docs_dir/notes.md:3-40"],
    )

    assert result["ok"] is True, result
    assert task_store.get_task(repo, task_id)["read_first"] == [
        "src/exists.py:12",
        "docs_dir/notes.md:3-40",
    ]


def test_reject_review_without_a_read_first_amendment_changes_nothing_extra(monkeypatch, repo):
    task_id = "NF01440_NONE"
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": ["src/exists.py"]})

    result = core.reject_review(task_id, "plain rework", topic=_TOPIC, read_first_amendment=[])

    assert result["ok"] is True, result
    assert "read_first_amendment_applied" not in result
    card = task_store.get_task(repo, task_id)
    assert card["read_first"] == ["src/exists.py"]
    assert "read_first_amendments" not in card


def test_reject_review_with_only_duplicates_reports_nothing_applied(monkeypatch, repo):
    task_id = "NF01440_DUP"
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": ["src/exists.py"]})

    result = core.reject_review(
        task_id, "dup", topic=_TOPIC, read_first_amendment=["src/exists.py"]
    )

    assert result["ok"] is True, result
    assert "read_first_amendment_applied" not in result
    assert "read_first_amendments" not in task_store.get_task(repo, task_id)


@pytest.mark.parametrize(
    "bad_entry",
    [
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:win.ini",
        "../outside.py",
        "src/../../outside.py",
        "..\\outside.py",
        ".git/config",
        ".GIT/config",
        "./.git/HEAD",
        ".aiworkhub/runtime/state.json",
        ".aiworkhub/runtime",
        "src/missing.py",
        "src/missing.py:1-5",
        "src",
        "src/exists\x00.py",
    ],
)
def test_reject_review_refuses_an_invalid_entry_and_leaves_the_card_unchanged(
    monkeypatch, repo, bad_entry
):
    task_id = "NF01440_INVALID"
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": ["src/exists.py"]})
    before = _row_snapshot(repo, task_id)

    result = core.reject_review(
        task_id,
        "bad",
        topic=_TOPIC,
        read_first_amendment=["docs_dir/notes.md", bad_entry],
    )

    assert result["ok"] is False
    assert result["stderr"] == "read_first_amendment_invalid"
    assert result["read_first_entry"] == bad_entry[:240]
    assert _row_snapshot(repo, task_id) == before


@pytest.mark.parametrize("disposition", ["blocked", "archived", "superseded"])
def test_reject_review_refuses_the_amendment_unless_the_disposition_is_pending(
    monkeypatch, repo, disposition
):
    task_id = "NF01440_NOT_PENDING_" + disposition.upper()
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": ["src/exists.py"]})
    monkeypatch.setattr(core, "_verified_manager_actor", _must_not_run)
    monkeypatch.setattr(core, "_canonical_write_gate", _must_not_run)
    before = _row_snapshot(repo, task_id)

    result = core.reject_review(
        task_id,
        "park it",
        topic=_TOPIC,
        to=disposition,
        read_first_amendment=["docs_dir/notes.md"],
    )

    assert result["ok"] is False
    assert result["stderr"] == "read_first_amendment_requires_pending"
    assert _row_snapshot(repo, task_id) == before


def test_reject_review_refuses_a_merged_total_over_the_limit_without_state_change(
    monkeypatch, repo
):
    task_id = "NF01440_MERGED_LIMIT"
    existing = [f"src/existing_{i}.py" for i in range(_MAX - 1)]
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": list(existing)})
    before = _row_snapshot(repo, task_id)

    refused = core.reject_review(
        task_id,
        "widen",
        topic=_TOPIC,
        read_first_amendment=["src/exists.py", "docs_dir/notes.md"],
    )

    assert refused["ok"] is False
    assert refused["stderr"] == "invalid_read_first_amendment:read_first_amendment_exceeds_limit"
    assert _row_snapshot(repo, task_id) == before

    accepted = core.reject_review(
        task_id, "widen", topic=_TOPIC, read_first_amendment=["src/exists.py"]
    )

    assert accepted["ok"] is True, accepted
    assert task_store.get_task(repo, task_id)["read_first"] == [*existing, "src/exists.py"]


def test_reject_review_refuses_more_entries_than_the_per_call_bound(monkeypatch, repo):
    task_id = "NF01440_TOO_MANY"
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": []})
    before = _row_snapshot(repo, task_id)

    result = core.reject_review(
        task_id,
        "many",
        topic=_TOPIC,
        read_first_amendment=[f"src/exists.py:{i + 1}" for i in range(_MAX + 1)],
    )

    assert result["ok"] is False
    assert result["stderr"] == "read_first_amendment_invalid"
    assert _row_snapshot(repo, task_id) == before


def test_server_forwards_read_first_amendment_only_when_non_empty(monkeypatch):
    server = pytest.importorskip("aiworkhub.server")
    calls: list[dict] = []

    def fake_reject_review(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "returncode": 126, "command": [], "stdout": "", "stderr": "stub"}

    monkeypatch.setattr(core, "reject_review", fake_reject_review)

    server.aiworkhub_task_reject_review("T1", "reason")
    server.aiworkhub_task_reject_review("T1", "reason", read_first_amendment=None)
    server.aiworkhub_task_reject_review("T1", "reason", read_first_amendment=[])
    server.aiworkhub_task_reject_review(
        "T1", "reason", read_first_amendment=["src/new.py", "docs/a.md:1-9"]
    )

    base = {"task_id": "T1", "reason": "reason", "to": "pending"}
    assert calls == [
        base,
        base,
        base,
        {**base, "read_first_amendment": ["src/new.py", "docs/a.md:1-9"]},
    ]


def _seed_git_dir(root: Path) -> None:
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 strips trailing dots from path parts")
def test_reject_review_refuses_a_trailing_dot_alias_of_the_git_directory(monkeypatch, repo):
    task_id = "NF01440_GIT_DOT"
    _seed_git_dir(repo)
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": ["src/exists.py"]})
    before = _row_snapshot(repo, task_id)

    result = core.reject_review(
        task_id, "alias", topic=_TOPIC, read_first_amendment=[".git./config"]
    )

    assert result["ok"] is False
    assert result["stderr"] == "read_first_amendment_invalid"
    assert _row_snapshot(repo, task_id) == before


def test_reject_review_refuses_a_symlink_that_resolves_into_the_git_directory(monkeypatch, repo):
    task_id = "NF01440_GIT_LINK"
    _seed_git_dir(repo)
    try:
        (repo / "src" / "l").symlink_to(repo / ".git" / "HEAD")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted here")
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": ["src/exists.py"]})
    before = _row_snapshot(repo, task_id)

    result = core.reject_review(task_id, "link", topic=_TOPIC, read_first_amendment=["src/l"])

    assert result["ok"] is False
    assert result["stderr"] == "read_first_amendment_invalid"
    assert result["read_first_entry"] == "src/l"
    assert _row_snapshot(repo, task_id) == before


def test_reject_review_stores_exactly_the_validated_whitespace_trimmed_entries(
    monkeypatch, repo
):
    task_id = "NF01440_TRIM"
    _review_ready_task(monkeypatch, repo, task_id, {"read_first": []})

    result = core.reject_review(
        task_id,
        "trim",
        topic=_TOPIC,
        read_first_amendment=["  docs_dir/notes.md  ", "\tsrc/exists.py:2-4 "],
    )

    assert result["ok"] is True, result
    assert result["read_first_amendment_applied"] == ["docs_dir/notes.md", "src/exists.py:2-4"]
    card = task_store.get_task(repo, task_id)
    assert card["read_first"] == ["docs_dir/notes.md", "src/exists.py:2-4"]
    assert card["read_first_amendments"][0]["paths"] == card["read_first"]


def test_validator_returns_the_normalized_entries_on_success(monkeypatch, repo):
    monkeypatch.setattr(core, "repo_root", lambda: repo)

    entries, error = core._validate_read_first_amendment_paths(["  src/exists.py  "])

    assert (entries, error) == (["src/exists.py"], None)
