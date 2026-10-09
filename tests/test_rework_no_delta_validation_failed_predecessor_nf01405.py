"""NF-2026-01405: re-running a ``validation_failed`` predecessor IS the recovery.

Measured on needfix-NF-2026-01391. Attempt ``27aecbe8`` failed validation for
environmental reasons only and the manager rejected it to pending. Rework
``9af191df`` reproduced the candidate byte for byte with validation 3/3
passing, and ``process_launcher_evidence.rework_no_delta_refusal``
(NF-2026-01370) refused it as ``worker_failed rework_no_delta``. Every recovery
path then dead-ended -- ``validation_only_replay_workspace_invalid``,
``clean_root_rework_workspace_still_available``, and a relaunch reproduces the
same bytes again -- so a correct candidate was stranded.

``rework_no_delta`` exists to stop a rework that ignored the reject findings
from reaching review on the predecessor's bytes, which presumes those bytes
already passed validation once. A predecessor whose recorded terminal was
``validation_failed`` never had a passing validation, so re-running the exact
bytes through validation is the only thing that can decide them.

What is locked here:

* a byte-exact reproduction of a predecessor whose recorded terminal substatus
  was ``validation_failed`` mints no refusal, so the ordinary finalization
  gates decide it;
* that exemption changes no gate: a failing validation still ends
  ``validation_failed`` and never ``review_ready``;
* a byte-exact reproduction of a ``review_ready`` predecessor -- an ordinary
  manager reject -- is still refused ``rework_no_delta``;
* every other terminal, and an absent, mistyped or near-miss field, keeps
  today's refusal: unknown is never an exemption;
* ``task_store.recover_blocked_rework`` publishes the terminal it already
  authenticated onto the predecessor record it republishes, and only when that
  terminal event names the very request the record pins.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher as pl  # noqa: E402
from aiworkhub import process_launcher_evidence as evidence  # noqa: E402
from aiworkhub import task_store  # noqa: E402
from aiworkhub.process_launcher_acceptance import (  # noqa: E402
    changed_path_hashes,
)
from aiworkhub.worker_workspace import (  # noqa: E402
    ValidationRunError,
    WorkspaceError,
)

# One definition of what a seeded rework worktree and its predecessor metadata
# look like: NF-2026-01370 owns it, and the exemption under test is a property
# of the very same refusal, so the fixtures must be the same ones.
from test_rework_no_delta_nf1370 import (  # noqa: E402
    _PREDECESSOR,
    _metadata,
    _seed_rework_workspace,
)
from test_blocked_rework_recovery import (  # noqa: E402
    _get_card,
    _insert_blocked_task,
    _setup_repo,
)

_VALIDATION_FAILED = evidence.REWORK_NO_DELTA_EXEMPT_PREDECESSOR_TERMINAL


def _reproduction(tmp_path: Path, terminal_substatus=None) -> tuple:
    """A byte-exact reproduction of the sealed predecessor delta.

    The sealed identity is measured by ``changed_path_hashes`` -- the owner the
    retention path seals with -- over the untouched inherited bytes, so this
    really is the predecessor's delta and not an approximation of it.
    """
    workspace = _seed_rework_workspace(tmp_path, _PREDECESSOR)
    changed = sorted(_PREDECESSOR)
    sealed = changed_path_hashes(workspace, changed)
    assert set(sealed) == set(changed)
    metadata = _metadata(workspace, sealed)
    if terminal_substatus is not None:
        metadata["rework_predecessor"]["terminal_substatus"] = terminal_substatus
    return workspace, metadata, changed


def _refuse(workspace, metadata, changed) -> str:
    return evidence.rework_no_delta_refusal(
        workspace, metadata, changed, validation_only_replay=False
    )


def test_a_validation_failed_predecessor_reproduction_is_not_refused(tmp_path):
    workspace, metadata, changed = _reproduction(
        tmp_path, terminal_substatus=_VALIDATION_FAILED
    )

    assert _refuse(workspace, metadata, changed) == ""

    # The contrast proves the exemption rather than a broken fixture: the SAME
    # bytes with no recorded predecessor terminal are still refused by name.
    del metadata["rework_predecessor"]["terminal_substatus"]
    refusal = _refuse(workspace, metadata, changed)
    assert refusal.startswith(evidence.REWORK_NO_DELTA + ":")


def test_the_exempted_reproduction_is_still_decided_by_the_ordinary_gates(tmp_path):
    """No refusal is not a pass: the validation gate keeps its verdict."""
    workspace, metadata, changed = _reproduction(
        tmp_path, terminal_substatus=_VALIDATION_FAILED
    )
    assert _refuse(workspace, metadata, changed) == ""

    # A failing validation on those same bytes terminalises validation_failed
    # through the ordinary gate, exactly as it does for any other attempt.
    failed = pl._terminal_state_for_workspace_error(
        ValidationRunError(
            "validation_failed: 1 failed",
            [{"command": "pytest -q", "returncode": 1}],
        )
    )
    assert failed == "validation_failed"
    assert failed != "review_ready"
    # And the exemption never routes anything through the refusal's own
    # worker_failed terminal, because no refusal was minted to route.
    assert pl._terminal_state_for_workspace_error(
        WorkspaceError(evidence.REWORK_NO_DELTA + ":" + changed[0])
    ) == "worker_failed"


def test_a_review_ready_predecessor_reproduction_is_still_refused(tmp_path):
    """An ordinary manager reject of PASSING bytes keeps the whole refusal."""
    workspace, metadata, changed = _reproduction(
        tmp_path, terminal_substatus="review_ready"
    )

    refusal = _refuse(workspace, metadata, changed)
    assert refusal.startswith(evidence.REWORK_NO_DELTA + ":")
    for relative in changed:
        assert relative in refusal
    assert pl._terminal_state_for_workspace_error(WorkspaceError(refusal)) == (
        "worker_failed"
    )


@pytest.mark.parametrize(
    "recorded",
    [
        "worker_failed",
        "finalize_failed",
        "timed_out",
        "scope_rejected",
        "",
        None,
        # Near misses and wrong types are unknown values, not exemptions.
        "VALIDATION_FAILED",
        " validation_failed ",
        "validation_failed_again",
        ["validation_failed"],
        True,
    ],
)
def test_every_other_recorded_terminal_keeps_the_refusal(tmp_path, recorded):
    workspace, metadata, changed = _reproduction(tmp_path)
    metadata["rework_predecessor"]["terminal_substatus"] = recorded

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_a_predecessor_record_without_the_field_fails_closed(tmp_path):
    """Today's refusal is the default: the field is additive and optional."""
    workspace, metadata, changed = _reproduction(tmp_path)
    assert "terminal_substatus" not in metadata["rework_predecessor"]

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_the_exemption_is_read_from_the_record_that_seals_the_bytes(tmp_path):
    """One record publishes both, so they cannot describe two predecessors."""
    workspace, metadata, changed = _reproduction(
        tmp_path, terminal_substatus=_VALIDATION_FAILED
    )
    # A sibling card field claiming the same terminal is not the predecessor's
    # record and exempts nothing.
    metadata["rework_predecessor"]["terminal_substatus"] = "review_ready"
    metadata["terminal_substatus"] = _VALIDATION_FAILED
    metadata["recovery_predecessor"] = {"terminal_substatus": _VALIDATION_FAILED}

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


# --------------------------------------------------------------------------- #
# the predecessor record carries the terminal the recovery authenticated
# --------------------------------------------------------------------------- #

_REQUEST_ID = "a" * 32
_OTHER_REQUEST_ID = "b" * 32


def _blocked_with_predecessor(repo: Path, task_id: str, predecessor_request_id: str):
    _insert_blocked_task(
        repo,
        task_id,
        terminal_substatus=_VALIDATION_FAILED,
        terminal_evidence={
            "validation": [
                {"command": "pytest -q", "returncode": 1, "stdout": "", "stderr": ""}
            ],
            "required_outputs": [],
            "changed_path_hashes": {"src/aiworkhub/task_store.py": "c" * 64},
            "request_identity": {"request_id": _REQUEST_ID, "task_id": task_id},
        },
        extra_card={
            # The card's own server-written launch request is the authority the
            # recovery verifies the terminal event against (NF-2026-01169).
            "launch_request_id": _REQUEST_ID,
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": predecessor_request_id,
                "changed_path_hashes": {"src/aiworkhub/task_store.py": "c" * 64},
            }
        },
    )


def test_recovery_publishes_the_authenticated_terminal_on_the_predecessor(tmp_path):
    repo = _setup_repo(tmp_path)
    _blocked_with_predecessor(repo, "NF01405_STAMPED", _REQUEST_ID)

    assert task_store.recover_blocked_rework(
        repo, "NF01405_STAMPED", actor="coordinator", feedback_reason="NeedFix: rerun",
    ) == (True, "recovered")

    predecessor = _get_card(repo, "NF01405_STAMPED")["rework_predecessor"]
    assert predecessor["terminal_substatus"] == _VALIDATION_FAILED
    # The sealed identity is untouched: the field is additive, not a rewrite.
    assert predecessor["changed_path_hashes"] == {
        "src/aiworkhub/task_store.py": "c" * 64,
    }
    assert predecessor["request_id"] == _REQUEST_ID


def test_recovery_stamps_nothing_when_the_terminal_names_another_request(tmp_path):
    """An inherited predecessor keeps whatever it already carried."""
    repo = _setup_repo(tmp_path)
    _blocked_with_predecessor(repo, "NF01405_INHERITED", _OTHER_REQUEST_ID)

    assert task_store.recover_blocked_rework(
        repo,
        "NF01405_INHERITED",
        actor="coordinator",
        feedback_reason="NeedFix: rerun",
    ) == (True, "recovered")

    predecessor = _get_card(repo, "NF01405_INHERITED")["rework_predecessor"]
    assert "terminal_substatus" not in predecessor
    assert predecessor["request_id"] == _OTHER_REQUEST_ID
