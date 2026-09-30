from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import core, task_store, worker_workspace

LEADING_DOT_CASES = [
    (".aiworkhub/config/x.json", ".aiworkhub/config/x.json"),
    ("./src/a.py", "src/a.py"),
    ("././src/a.py", "src/a.py"),
    (r".\.aiworkhub\x.json", ".aiworkhub/x.json"),
]


@pytest.mark.parametrize("raw, expected", LEADING_DOT_CASES)
def test_task_contract_path_peels_only_leading_dot_slash_segments(raw, expected):
    assert core._task_contract_path(raw) == expected


@pytest.mark.parametrize("raw, expected", LEADING_DOT_CASES)
def test_normalize_write_path_peels_only_leading_dot_slash_segments(raw, expected):
    assert task_store._normalize_write_path(raw) == expected


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    tok = tmp_path / "coordinator.token"
    tok.write_text("coord-token\n", encoding="utf-8")
    os.chmod(tok, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(tok))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    return root


def _insert(
    root: Path,
    task_id: str,
    *,
    worker_status: str = "review",
    status: str = "review",
    topic: str = "coding",
    card: dict | None = None,
) -> None:
    """Seed a card into the tmp_path fixture repo's task DB (test idiom; not a context DB)."""
    now = datetime.now(timezone.utc).isoformat()
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, "claude_coding", topic, "solo", status, worker_status, "normal",
                "obj", json.dumps(card or {}), now, now, "claude_coding",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _terminal_rework_delta_card(
    root: Path, task_id: str, request_id: str, *, claim_epoch: int = 4
) -> dict:
    artifact_root = root / ".aiworkhub" / "runtime" / "rework_deltas"
    content = b"reviewed predecessor bytes\n"
    artifact = worker_workspace.seal_rework_delta_artifact(
        authority_repo=root,
        task_id=task_id,
        request_id=request_id,
        claim_epoch=claim_epoch,
        file_entries=[("out/result.txt", content)],
        artifact_dir=artifact_root,
    )
    descriptor = {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(root.resolve()),
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "artifact_path": artifact["path"],
        "artifact_sha256": artifact["digest"],
    }
    workspace = {
        "request_id": request_id,
        "repo": str(root),
        "path": f"/tmp/aiworkhub-worktrees/{request_id}/worktree",
        "home": f"/tmp/aiworkhub-worktrees/{request_id}/home",
        "allowed_writes": ["out/result.txt"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    return {
        "claim_epoch": claim_epoch,
        "terminal_review": {
            "claim_epoch": claim_epoch,
            "substatus": "validation_failed",
            "evidence": {
                "request_identity": {"request_id": request_id, "task_id": task_id},
                "workspace": workspace,
                "changed_path_hashes": {
                    "out/result.txt": hashlib.sha256(content).hexdigest()
                },
                "rework_delta": descriptor,
            },
        },
    }


def test_reject_review_persists_leading_dot_residual_path_when_covered_by_allowed_writes(coord):
    task_id = "T_LEADING_DOT_COVERED"
    request_id = "a" * 32
    residual_path = ".aiworkhub/config/development_rules.json"
    card = _terminal_rework_delta_card(coord, task_id, request_id)
    card["allowed_writes"] = [residual_path]
    _insert(coord, task_id, card=card)

    result = core.reject_review(
        task_id,
        "leading dot residual stays in scope",
        to="pending",
        residual_identities=[{"path": residual_path, "pointer": "/rows/0"}],
    )

    assert result["ok"] is True, result
    persisted = json.loads(result["stdout"])
    assert persisted["rework_predecessor"]["residual_identities"] == [
        {"path": residual_path, "pointer": "/rows/0"}
    ]
    assert persisted["review_feedback"]["residual_identities"] == [
        {"path": residual_path, "pointer": "/rows/0"}
    ]


def test_reject_review_refuses_residual_path_outside_allowed_writes(coord):
    task_id = "T_RESIDUAL_OUTSIDE_SCOPE"
    card = {
        "allowed_writes": ["src/aiworkhub/unrelated.py"],
        "terminal_review": {"marker": "unchanged", "evidence": {}},
    }
    _insert(coord, task_id, card=card)
    before = task_store.get_task(coord, task_id)

    result = core.reject_review(
        task_id,
        "residual path escapes the contract",
        to="pending",
        residual_identities=[
            {"path": ".aiworkhub/config/development_rules.json", "pointer": "/rows/0"}
        ],
    )

    assert result["ok"] is False
    assert result["stderr"] == "residual_artifact_outside_scope"

    after = task_store.get_task(coord, task_id)
    assert after == before


UNSAFE_RESIDUAL_CASES = [
    ("*", ["src/aiworkhub/unrelated.py"]),
    ("**", ["src/aiworkhub/unrelated.py"]),
    ("src/", ["src/x.py"]),
    ("src/a[1].txt", ["src/a1.txt"]),
    ("src/data/rows.json", ["src/*.py"]),
    ("src/x.py/../../../.aiworkhub/config/development_rules.json", ["src/x.py"]),
    ("/etc/app.json", ["*.json"]),
    ("C:/x/app.json", ["*.json"]),
    ("src/*.py", ["src/"]),
    ("src/pkg/", ["src/"]),
    ("src/./x.py", ["src/"]),
]


@pytest.mark.parametrize("residual_path, allowed_writes", UNSAFE_RESIDUAL_CASES)
def test_reject_review_refuses_unsafe_or_uncovered_residual_shapes(
    coord, residual_path, allowed_writes
):
    task_id = "T_RESIDUAL_UNSAFE"
    card = {
        "allowed_writes": allowed_writes,
        "terminal_review": {"marker": "unchanged", "evidence": {}},
    }
    _insert(coord, task_id, card=card)
    before = task_store.get_task(coord, task_id)

    result = core.reject_review(
        task_id,
        "residual path is unsafe or out of scope",
        to="pending",
        residual_identities=[{"path": residual_path, "pointer": "/rows/0"}],
    )

    assert result["ok"] is False
    assert result["stderr"] == "residual_artifact_outside_scope"

    after = task_store.get_task(coord, task_id)
    assert after == before


COVERED_RESIDUAL_CASES = [
    ("src/x.py", ["src/*.py"]),
    ("./src/x.py", ["src/x.py"]),
    ("src/pkg/a.py", ["src/pkg/"]),
]


@pytest.mark.parametrize("residual_path, allowed_writes", COVERED_RESIDUAL_CASES)
def test_reject_review_accepts_residual_path_covered_by_allowed_writes(
    coord, residual_path, allowed_writes
):
    task_id = "T_RESIDUAL_COVERED"
    request_id = "b" * 32
    card = _terminal_rework_delta_card(coord, task_id, request_id)
    card["allowed_writes"] = allowed_writes
    _insert(coord, task_id, card=card)

    result = core.reject_review(
        task_id,
        "residual path stays in scope",
        to="pending",
        residual_identities=[{"path": residual_path, "pointer": "/rows/0"}],
    )

    assert result["ok"] is True, result
    expected_path = core._task_contract_path(residual_path)
    persisted = json.loads(result["stdout"])
    assert persisted["rework_predecessor"]["residual_identities"] == [
        {"path": expected_path, "pointer": "/rows/0"}
    ]
    assert persisted["review_feedback"]["residual_identities"] == [
        {"path": expected_path, "pointer": "/rows/0"}
    ]


def test_reject_review_reports_caller_index_after_skipping_duplicate_residual(coord):
    task_id = "T_RESIDUAL_INDEX_AFTER_DUPLICATE"
    card = {
        "allowed_writes": ["src/ok.py"],
        "terminal_review": {"marker": "unchanged", "evidence": {}},
    }
    _insert(coord, task_id, card=card)
    before = task_store.get_task(coord, task_id)

    result = core.reject_review(
        task_id,
        "duplicate residual ahead of the bad one must not shift the reported index",
        to="pending",
        residual_identities=[
            {"path": "src/ok.py", "pointer": "/rows/0"},
            {"path": "src/ok.py", "pointer": "/rows/0"},
            {"path": "src/bad.py", "pointer": "/rows/0"},
        ],
    )

    assert result["ok"] is False
    assert result["stderr"] == "residual_artifact_outside_scope"
    assert result["invalid_index"] == 2

    after = task_store.get_task(coord, task_id)
    assert after == before


def test_task_contract_path_peels_quadratic_leading_dot_slash_run():
    raw = "./" * 50000 + "src/a.py"
    assert core._task_contract_path(raw) == "src/a.py"


def test_normalize_write_path_peels_quadratic_leading_dot_slash_run():
    raw = "./" * 50000 + "src/a.py"
    assert task_store._normalize_write_path(raw) == "src/a.py"


def test_normalize_write_path_peels_quadratic_interleaved_leading_dot_slash_run():
    raw = ".//" * 50000 + "src/a.py"
    assert task_store._normalize_write_path(raw) == "src/a.py"


def test_normalize_write_path_treats_leading_slash_as_repo_relative():
    assert task_store._normalize_write_path("/src/a.py") == "src/a.py"
    assert task_store._normalize_write_path("/./src/a.py") == "src/a.py"


def test_reject_review_refuses_when_allowed_writes_is_not_a_list(coord):
    task_id = "T_ALLOWED_WRITES_NOT_A_LIST"
    card = {
        "allowed_writes": "src/*.py",
        "terminal_review": {"marker": "unchanged", "evidence": {}},
    }
    _insert(coord, task_id, card=card)
    before = task_store.get_task(coord, task_id)

    result = core.reject_review(
        task_id,
        "allowed_writes is a string, not a list; must fail closed",
        to="pending",
        residual_identities=[{"path": "src/x.py", "pointer": "/rows/0"}],
    )

    assert result["ok"] is False
    assert result["stderr"] == "residual_artifact_outside_scope"

    after = task_store.get_task(coord, task_id)
    assert after == before
