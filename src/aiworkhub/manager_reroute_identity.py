"""Manager reroute identity helpers split out of ``core`` (fix batch 02).

* NF-2026-00791 -- verify a manager-rejected predecessor that changed nothing.
* NF-2026-00802 -- the generic VS Code chat manager identity, keyed on the
  chat's ``conversationId`` and never on editor focus.

``core`` imports this module at load time, so every core-private helper is
resolved lazily inside the function body to avoid an import cycle (and so a
monkeypatched ``core`` attribute is honoured).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from . import manager_route_probe

_VSCODE_LM_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def vscode_lm_manager_identity() -> dict[str, str] | None:
    """A generic VS Code chat (Language Model API) manager route (NF-2026-00802).

    The identity is keyed on the chat's own ``conversationId``, which the
    owning extension injects as ``AIWORKHUB_VSCODE_LM_CONVERSATION_ID`` when it
    starts this MCP for that conversation.  It is never derived from editor
    focus, the active window or the last-used chat: a focus switch must not
    move callbacks to another conversation, so ``window_id`` stays empty.
    Without VS Code provenance, or inside a Claude/Codex chain that owns its
    own identity, it fails closed.
    """

    conversation_id = os.environ.get("AIWORKHUB_VSCODE_LM_CONVERSATION_ID", "").strip()
    if not _VSCODE_LM_CONVERSATION_ID_RE.fullmatch(conversation_id):
        return None
    if not (
        os.environ.get("VSCODE_IPC_HOOK_CLI")
        or os.environ.get("VSCODE_ESM_ENTRYPOINT")
        or os.environ.get("VSCODE_AGENT_FOLDER")
        or os.environ.get("VSCODE_PID")
    ):
        return None
    if os.environ.get("CODEX_THREAD_ID", "").strip():
        return None
    if manager_route_probe.claude_ancestry_present():
        return None
    return {
        "provider": "vscode_lm",
        "session_id": conversation_id,
        "thread_id": conversation_id,
        "conversation_id": conversation_id,
        "window_id": "",
        "identity_key": "conversation_id",
    }


def verified_zero_delta_predecessor_receipt(
    card: Mapping[str, Any], *, task_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify a manager-rejected predecessor that changed nothing (NF-2026-00791).

    There are no candidate bytes a reroute could discard, but the empty map is
    never taken from the card: the retained worktree is re-proven unchanged
    against its sealed provisioning baselines by ``core._verify_retained_zero_diff``.
    A missing, out-of-root or drifted worktree fails closed with its own reason.
    """
    from . import core
    from .capped_continuation import consumable_predecessor_task_id

    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, dict):
        return None, "reroute_retained_candidate_invalid"
    request_id = str(predecessor.get("request_id") or "").strip()
    claim_epoch = predecessor.get("claim_epoch")
    workspace = predecessor.get("workspace")
    if (
        predecessor.get("schema_id") != "aiworkhub.rework_predecessor.v1"
        or str(predecessor.get("task_id") or "")
        != consumable_predecessor_task_id(card, task_id)
        or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
        or type(claim_epoch) is not int
        or claim_epoch < 1
        or not isinstance(workspace, dict)
        or predecessor.get("changed_path_hashes") != {}
    ):
        return None, "reroute_retained_candidate_identity_mismatch"
    raw_path = workspace.get("path")
    if type(raw_path) is not str or not raw_path.strip():
        return None, "reroute_retained_candidate_workspace_invalid"
    retained = Path(raw_path.strip())
    # The live card's CURRENT claim epoch must equal the retained candidate's:
    # a stale predecessor from an earlier claim episode never authorizes.
    card_claim_epoch = card.get("claim_epoch")
    if type(card_claim_epoch) is not int or card_claim_epoch != claim_epoch:
        return None, "reroute_retained_candidate_identity_mismatch"
    raw_base_oid = workspace.get("base_oid")
    if (
        type(raw_base_oid) is not str
        or _GIT_OID_RE.fullmatch(raw_base_oid) is None
    ):
        return None, "reroute_retained_candidate_identity_mismatch"
    authority_repo = core.repo_root().resolve(strict=False)
    # The capability and out-of-root checks run inside the verifier before any
    # hashing; the symlink/dir probe below only runs once they have passed.
    zero_diff_error = core._verify_retained_zero_diff(
        request_id=request_id,
        task_id=task_id,
        authority_repo=authority_repo,
        retained=retained,
        workspace=workspace,
        card_claim_epoch=card_claim_epoch,
        claim_epoch=claim_epoch,
    )
    if zero_diff_error:
        return None, f"reroute_retained_zero_delta_unverified:{zero_diff_error}"
    if retained.is_symlink() or not retained.is_dir():
        return None, "reroute_retained_candidate_workspace_invalid"
    return {
        "retained_candidate_preserved": True,
        "retained_zero_delta_verified": True,
        "retained_predecessor_request_id": request_id,
        "retained_predecessor_sha256": core._canonical_receipt_digest(predecessor),
        "retained_base_oid": raw_base_oid,
        "retained_claim_epoch": claim_epoch,
        "retained_changed_path_count": 0,
    }, None
