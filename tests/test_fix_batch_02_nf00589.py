"""Fix batch 02 (needfix-NF-2026-00589) regression tests.

Covers the rows of the batch that were fixed inside this card's scope:

* NF-2026-00791 -- a manager-rejected zero-delta attempt can be
  identity-rerouted once the retained worktree's empty diff is re-proven.
* NF-2026-00802 -- a generic VS Code chat manager identity is keyed on the
  conversationId, never on editor focus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import core, manager_reroute_identity

TASK_ID = "nf00791-zero-delta"
REQUEST_ID = "0123456789abcdef0123456789abcdef"
PINNED_AT = "2026-11-20T00:00:00+00:00"


def _zero_delta_card(workspace_path: Path, **feedback_overrides):
    feedback = {
        "schema_id": "aiworkhub.rework_feedback_delta.v1",
        "predecessor_request_id": REQUEST_ID,
        "predecessor_changed_paths": [],
        "instruction": "redo the work: the attempt changed nothing",
        "reason_identity": {"bytes": 10, "sha256": "ab" * 32, "truncated": False},
    }
    feedback.update(feedback_overrides)
    return {
        "task_id": TASK_ID,
        "runner": "claude",
        "status": "pending",
        "claim_epoch": 1,
        "allowed_writes": ["src/app.py"],
        "rejection_disposition": {
            "schema_id": "aiworkhub.rejection_disposition.v1",
            "to": "pending",
            "failure_category_source": "manager_explicit",
            "failure_category": "candidate_code",
            "request_id": REQUEST_ID,
            "pinned_at": PINNED_AT,
        },
        "review_feedback": feedback,
        "rework_predecessor": {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "task_id": TASK_ID,
            "request_id": REQUEST_ID,
            "claim_epoch": 1,
            "pinned_at": PINNED_AT,
            "changed_path_hashes": {},
            "workspace": {
                "request_id": REQUEST_ID,
                "task_id": TASK_ID,
                "path": str(workspace_path),
                "base_oid": "a" * 40,
            },
        },
    }


@pytest.fixture
def zero_diff_calls(tmp_path, monkeypatch):
    calls: list[dict] = []
    verdict = {"value": ""}

    # The real keyword-only signature, so a renamed argument fails the test.
    def fake_verify(
        *,
        request_id,
        task_id,
        authority_repo,
        retained,
        workspace,
        card_claim_epoch,
        claim_epoch,
    ):
        calls.append(
            {
                "request_id": request_id,
                "task_id": task_id,
                "authority_repo": authority_repo,
                "retained": retained,
                "workspace": workspace,
                "card_claim_epoch": card_claim_epoch,
                "claim_epoch": claim_epoch,
            }
        )
        return verdict["value"]

    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(core, "_verify_retained_zero_diff", fake_verify)
    return calls, verdict


def test_nf00791_zero_delta_rejection_authorizes_reroute(tmp_path, zero_diff_calls):
    calls, _verdict = zero_diff_calls
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    card = _zero_delta_card(workspace)

    receipt, error = core._verified_manager_rejection_receipt(card, task_id=TASK_ID)

    assert error is None, error
    assert receipt is not None
    assert receipt["schema_id"] == "aiworkhub.manager_rejection_reroute_authorization.v1"
    assert receipt["request_id"] == REQUEST_ID
    # The empty diff is re-derived from the retained worktree, never trusted.
    assert len(calls) == 1
    assert calls[0]["request_id"] == REQUEST_ID
    assert calls[0]["retained"] == workspace


def test_nf00791_zero_delta_drift_or_missing_worktree_fails_closed(
    tmp_path, zero_diff_calls
):
    _calls, verdict = zero_diff_calls
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    verdict["value"] = "zero_diff_tree_drift"
    _receipt, error = core._verified_manager_rejection_receipt(
        _zero_delta_card(workspace), task_id=TASK_ID
    )
    assert error == "reroute_retained_zero_delta_unverified:zero_diff_tree_drift"

    verdict["value"] = ""
    _receipt, error = core._verified_manager_rejection_receipt(
        _zero_delta_card(tmp_path / "absent"), task_id=TASK_ID
    )
    assert error == "reroute_retained_candidate_workspace_invalid"


def test_nf00791_empty_hashes_with_declared_paths_still_refused(
    tmp_path, zero_diff_calls
):
    calls, _verdict = zero_diff_calls
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    card = _zero_delta_card(workspace, predecessor_changed_paths=["src/app.py"])
    _receipt, error = core._verified_manager_rejection_receipt(card, task_id=TASK_ID)
    assert error == "reroute_manager_rejection_feedback_invalid"
    assert calls == []


_CONVERSATION_ID = "conv-1234abcd-5678"
_identity = manager_reroute_identity.vscode_lm_manager_identity


@pytest.fixture
def vscode_lm_env(monkeypatch):
    for name in (
        "AIWORKHUB_VSCODE_LM_CONVERSATION_ID",
        "VSCODE_IPC_HOOK_CLI",
        "VSCODE_ESM_ENTRYPOINT",
        "VSCODE_AGENT_FOLDER",
        "VSCODE_PID",
        "CODEX_THREAD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(core.manager_route_probe, "claude_ancestry_present", lambda: False)
    monkeypatch.setenv("VSCODE_IPC_HOOK_CLI", "/tmp/vscode-ipc.sock")
    return monkeypatch


def test_nf00802_vscode_lm_identity_is_keyed_on_conversation_id(vscode_lm_env):
    vscode_lm_env.setenv("AIWORKHUB_VSCODE_LM_CONVERSATION_ID", _CONVERSATION_ID)
    identity = _identity()
    assert identity is not None
    assert identity["provider"] == "vscode_lm"
    assert identity["session_id"] == _CONVERSATION_ID
    assert identity["thread_id"] == _CONVERSATION_ID
    assert identity["conversation_id"] == _CONVERSATION_ID
    assert identity["identity_key"] == "conversation_id"
    # Never a focus-tracked window route.
    assert identity["window_id"] == ""

    # A focus switch (other window env) does not change the identity.
    vscode_lm_env.setenv("VSCODE_PID", "4242")
    assert _identity() == identity


def test_nf00802_vscode_lm_identity_fails_closed(vscode_lm_env):
    assert _identity() is None  # no conversationId
    vscode_lm_env.setenv("AIWORKHUB_VSCODE_LM_CONVERSATION_ID", "bad id/../x")
    assert _identity() is None
    vscode_lm_env.setenv("AIWORKHUB_VSCODE_LM_CONVERSATION_ID", _CONVERSATION_ID)
    vscode_lm_env.setenv("CODEX_THREAD_ID", "11111111-2222-3333-4444-555555555555")
    assert _identity() is None
    vscode_lm_env.delenv("CODEX_THREAD_ID")
    vscode_lm_env.delenv("VSCODE_IPC_HOOK_CLI")
    assert _identity() is None


def test_nf00802_core_live_identity_consults_vscode_lm_helper():
    # core owns no copy: it calls the single definition in the split module.
    assert core.manager_reroute_identity is manager_reroute_identity
    assert not hasattr(core, "_vscode_lm_manager_identity")
    assert not hasattr(core, "_verified_zero_delta_predecessor_receipt")


def test_nf00802_core_live_manager_identity_returns_conversation_identity(
    vscode_lm_env,
):
    # Only the upstream route probes are silenced; the wiring in
    # core._live_manager_identity itself is exercised.
    vscode_lm_env.setattr(core, "_manager_chat_seat_identity", lambda: None)
    vscode_lm_env.setattr(core, "_claude_manager_identity", lambda: None)
    vscode_lm_env.setattr(core, "_codex_manager_identity", lambda: None)
    vscode_lm_env.setenv("AIWORKHUB_VSCODE_LM_CONVERSATION_ID", _CONVERSATION_ID)

    identity = core._live_manager_identity()

    assert identity is not None
    assert identity["provider"] == "vscode_lm"
    assert identity["session_id"] == _CONVERSATION_ID
    assert identity["conversation_id"] == _CONVERSATION_ID
    assert identity["identity_key"] == "conversation_id"
    assert identity["window_id"] == ""


def test_nf00791_live_card_claim_epoch_mismatch_refuses(tmp_path, zero_diff_calls):
    calls, _verdict = zero_diff_calls
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    card = _zero_delta_card(workspace)
    card["claim_epoch"] = 2  # live card advanced; predecessor sealed at epoch 1

    receipt, error = manager_reroute_identity.verified_zero_delta_predecessor_receipt(
        card, task_id=TASK_ID
    )

    assert receipt is None
    assert error == "reroute_retained_candidate_identity_mismatch"
    assert calls == []


def test_nf00791_live_card_claim_epoch_is_forwarded(tmp_path, zero_diff_calls):
    calls, _verdict = zero_diff_calls
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    receipt, error = manager_reroute_identity.verified_zero_delta_predecessor_receipt(
        _zero_delta_card(workspace), task_id=TASK_ID
    )

    assert error is None, error
    assert receipt is not None
    assert receipt["retained_base_oid"] == "a" * 40
    assert calls[0]["card_claim_epoch"] == 1
    assert calls[0]["claim_epoch"] == 1


@pytest.mark.parametrize(
    "base_oid", ["not-hex", "A" * 40, "a" * 39, "a" * 40 + "\n", None, 7]
)
def test_nf00791_non_hex_base_oid_refuses(tmp_path, zero_diff_calls, base_oid):
    calls, _verdict = zero_diff_calls
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    card = _zero_delta_card(workspace)
    card["rework_predecessor"]["workspace"]["base_oid"] = base_oid

    receipt, error = manager_reroute_identity.verified_zero_delta_predecessor_receipt(
        card, task_id=TASK_ID
    )

    assert receipt is None
    assert error == "reroute_retained_candidate_identity_mismatch"
    assert calls == []


def test_nf00791_workspace_probe_runs_after_verifier(tmp_path, zero_diff_calls):
    calls, verdict = zero_diff_calls
    # The verifier (capability + out-of-root) refuses first, so the missing
    # directory is never probed and its verdict is the one reported.
    verdict["value"] = "zero_diff_out_of_root"
    _receipt, error = manager_reroute_identity.verified_zero_delta_predecessor_receipt(
        _zero_delta_card(tmp_path / "absent"), task_id=TASK_ID
    )
    assert error == "reroute_retained_zero_delta_unverified:zero_diff_out_of_root"
    assert len(calls) == 1
