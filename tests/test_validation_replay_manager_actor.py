"""A validation-only replay grant is honoured for either verified manager.

NF-2026-00244 attributes a coordinator's recover action to the verified
manager route (``codex`` or ``claude``), but the launch guard accepted only
``codex``, so every replay a Claude manager authorized was refused with
``validation_only_replay_actor_mismatch``.
"""

from __future__ import annotations

import pytest

from aiworkhub import core
from aiworkhub.launch_replay_guard import validation_only_replay_authorization

_HASHES = {"src/a.py": "0" * 64}


def _card(actor: str) -> dict:
    return {
        "claim_epoch": 2,
        "rework_predecessor": {"request_id": "R1", "changed_path_hashes": dict(_HASHES)},
        "validation_only_replay_authorization": {
            "one_episode_binding": True,
            "task_id": "T1",
            "actor": actor,
            "predecessor_request_id": "R1",
            "changed_path_hashes": dict(_HASHES),
            "next_claim_epoch": 2,
        },
    }


@pytest.mark.parametrize("actor", [core.CODEX_RUNNER, core.CLAUDE_MANAGER_RUNNER])
def test_either_verified_manager_actor_authorizes_the_replay(actor: str) -> None:
    grant = validation_only_replay_authorization(_card(actor), "T1")
    assert grant is not None and grant["actor"] == actor


@pytest.mark.parametrize("actor", ["", "worker", "glm-5.3", "claude_opus-5", "CODEX"])
def test_any_other_actor_is_still_refused(actor: str) -> None:
    with pytest.raises(ValueError, match="validation_only_replay_actor_mismatch"):
        validation_only_replay_authorization(_card(actor), "T1")


def test_the_other_bindings_still_apply_to_a_claude_grant() -> None:
    card = _card(core.CLAUDE_MANAGER_RUNNER)
    card["claim_epoch"] = 3
    with pytest.raises(ValueError, match="validation_only_replay_claim_epoch_mismatch"):
        validation_only_replay_authorization(card, "T1")
    with pytest.raises(ValueError, match="validation_only_replay_task_mismatch"):
        validation_only_replay_authorization(_card(core.CLAUDE_MANAGER_RUNNER), "T2")
