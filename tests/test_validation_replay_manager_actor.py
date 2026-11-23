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


def _card_with_hashes(hashes: dict) -> dict:
    card = _card(core.CLAUDE_MANAGER_RUNNER)
    card["rework_predecessor"]["changed_path_hashes"] = dict(hashes)
    card["validation_only_replay_authorization"]["changed_path_hashes"] = dict(hashes)
    return card


def test_a_sealed_deleted_none_digest_is_authorized() -> None:
    # NF-2026-02380: a None digest is a path sealed as deleted; the task store
    # verifies it is still absent, so the launch guard must let it through.
    hashes = {"src/a.py": "0" * 64, "src/deleted.py": None}
    grant = validation_only_replay_authorization(_card_with_hashes(hashes), "T1")
    assert grant is not None and grant["changed_path_hashes"] == hashes


@pytest.mark.parametrize(
    "hashes",
    [
        {"src/a.py": "abc"},
        {"src/a.py": "g" * 64},
        {"src/a.py": "A" * 64},
        {"src/a.py": 0},
        {"": None},
        {"   ": None},
    ],
    ids=["short", "non_hex", "upper_hex", "non_str", "empty_path", "blank_path"],
)
def test_malformed_digest_or_empty_path_still_refuses(hashes: dict) -> None:
    with pytest.raises(ValueError, match="validation_only_replay_hash_manifest_invalid"):
        validation_only_replay_authorization(_card_with_hashes(hashes), "T1")
