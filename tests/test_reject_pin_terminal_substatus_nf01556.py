"""NF-2026-01556: core.reject_review pins terminal_substatus on rework_predecessor."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _p in (_SRC, _TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from aiworkhub import core, task_store  # noqa: E402
from test_aiworkhub_coordinator_disposition_b961 import (  # noqa: E402
    _row,
    _strict_delta_descriptor,
    _strict_retained_workspace,
)
from test_validation_amendment_nf01151 import (  # noqa: E402
    _init_repo,
    _insert_task,
)

_RUNNER = "claude_task_mcp_runtime_wiring"
_TOPIC = "task_mcp"


@pytest.fixture
def repo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01556-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    return root


def _set_review_card(root: Path, task_id: str, patch: dict) -> None:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        assert row is not None
        card = json.loads(row[0])
        card.update(patch)
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _claim_and_mark_review(root: Path, task_id: str) -> None:
    _insert_task(
        root,
        task_id,
        _RUNNER,
        _TOPIC,
        extra_card={"allowed_writes": ["out/result.txt"]},
    )
    assert core.claim_start_exact(task_id, _RUNNER, _TOPIC)["ok"] is True
    assert core.mark_review(task_id, runner=_RUNNER, topic=_TOPIC)["ok"] is True


def test_reject_review_validation_failed_pins_terminal_substatus(
    repo_env: Path,
) -> None:
    task_id = "NF01556_VAL_FAILED"
    request_id = "req-nf01556-val-failed"
    _claim_and_mark_review(repo_env, task_id)

    workspace = _strict_retained_workspace(
        repo_env, task_id, request_id, ["out/result.txt"]
    )
    epoch = json.loads(_row(repo_env, task_id)["card_json"])["claim_epoch"]
    delta, hashes = _strict_delta_descriptor(
        repo_env, task_id, request_id, "out/result.txt", b"result\n", claim_epoch=epoch
    )
    _set_review_card(
        repo_env,
        task_id,
        {
            "terminal_review": {
                "substatus": "validation_failed",
                "claim_epoch": epoch,
                "evidence": {
                    "request_identity": {"request_id": request_id, "task_id": task_id},
                    "workspace": workspace,
                    "rework_delta": delta,
                    "changed_path_hashes": hashes,
                },
            }
        },
    )

    res = core.reject_review(
        task_id,
        "environmental validation failure; rerun",
        topic=_TOPIC,
        to="pending",
    )
    assert res["ok"] is True, res
    card = json.loads(_row(repo_env, task_id)["card_json"])
    predecessor = card["rework_predecessor"]
    assert predecessor["request_id"] == request_id
    assert predecessor["changed_path_hashes"] == hashes
    assert predecessor["terminal_substatus"] == "validation_failed"


def test_reject_review_normal_or_older_predecessor_never_pins_validation_failed(
    repo_env: Path,
) -> None:
    # (a) Normal review with substatus="review_ready"
    task_ready = "NF01556_REVIEW_READY"
    req_ready = "req-nf01556-ready"
    _claim_and_mark_review(repo_env, task_ready)
    ws_ready = _strict_retained_workspace(
        repo_env, task_ready, req_ready, ["out/result.txt"]
    )
    _set_review_card(
        repo_env,
        task_ready,
        {
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {"request_id": req_ready, "task_id": task_ready},
                    "workspace": ws_ready,
                    "changed_path_hashes": {"out/result.txt": "b" * 64},
                },
            }
        },
    )
    res_ready = core.reject_review(
        task_ready, "rework findings", topic=_TOPIC, to="pending"
    )
    assert res_ready["ok"] is True, res_ready
    pred_ready = json.loads(_row(repo_env, task_ready)["card_json"])[
        "rework_predecessor"
    ]
    assert pred_ready.get("terminal_substatus") in (None, "review_ready")
    assert pred_ready.get("terminal_substatus") != "validation_failed"

    # (b) Normal review with absent substatus
    task_absent = "NF01556_SUBSTATUS_ABSENT"
    req_absent = "req-nf01556-absent"
    _claim_and_mark_review(repo_env, task_absent)
    ws_absent = _strict_retained_workspace(
        repo_env, task_absent, req_absent, ["out/result.txt"]
    )
    _set_review_card(
        repo_env,
        task_absent,
        {
            "terminal_review": {
                "evidence": {
                    "request_identity": {
                        "request_id": req_absent,
                        "task_id": task_absent,
                    },
                    "workspace": ws_absent,
                    "changed_path_hashes": {"out/result.txt": "c" * 64},
                },
            }
        },
    )
    res_absent = core.reject_review(
        task_absent, "rework findings", topic=_TOPIC, to="pending"
    )
    assert res_absent["ok"] is True, res_absent
    pred_absent = json.loads(_row(repo_env, task_absent)["card_json"])[
        "rework_predecessor"
    ]
    assert "terminal_substatus" not in pred_absent

    # (c) Resolved older predecessor (pred_request_id != review_request_id) gets no substatus
    task_older = "NF01556_OLDER_PREDECESSOR"
    req_older = "req-nf01556-older"
    req_current = "req-nf01556-current"
    _claim_and_mark_review(repo_env, task_older)
    epoch_older = json.loads(_row(repo_env, task_older)["card_json"])["claim_epoch"]
    ws_older = _strict_retained_workspace(
        repo_env, task_older, req_older, ["out/result.txt"]
    )
    delta_older, hashes_older = _strict_delta_descriptor(
        repo_env,
        task_older,
        req_older,
        "out/result.txt",
        b"older-bytes\n",
        claim_epoch=epoch_older,
    )
    ws_current = _strict_retained_workspace(
        repo_env, task_older, req_current, ["out/result.txt"]
    )
    _set_review_card(
        repo_env,
        task_older,
        {
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": req_older,
                "claim_epoch": epoch_older,
                "workspace": ws_older,
                "changed_path_hashes": hashes_older,
                "rework_delta": delta_older,
                "residual_identities": [],
                "pinned_at": "2000-01-01T00:00:00+00:00",
            },
            "terminal_review": {
                "substatus": "validation_failed",
                "claim_epoch": epoch_older,
                "evidence": {
                    "request_identity": {
                        "request_id": req_current,
                        "task_id": task_older,
                    },
                    "workspace": ws_current,
                    "changed_path_hashes": {"out/result.txt": "d" * 64},
                },
            },
        },
    )
    res_older = core.reject_review(
        task_older,
        "keep older predecessor",
        topic=_TOPIC,
        to="pending",
        predecessor_request_id=req_older,
    )
    assert res_older["ok"] is True, res_older
    pred_older = json.loads(_row(repo_env, task_older)["card_json"])[
        "rework_predecessor"
    ]
    assert pred_older["pinned_at"] != "2000-01-01T00:00:00+00:00"
    assert pred_older["request_id"] == req_older
    assert "terminal_substatus" not in pred_older
