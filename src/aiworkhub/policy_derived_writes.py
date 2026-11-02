"""Widen a card's write scope to the files rendered from the tool-use POLICY.

NF-2026-01946: a card that edits ``agent_tool_instructions`` changes text that
is rendered byte-for-byte into every ``DERIVED_ARTIFACTS`` path. Measured on
NF01928 r3, a card that listed only the module could not write them: the
worker's sparse tree did not hold them, accept revalidation failed on the b816
eval byte counts, and a manager correction was refused as outside
allowed_writes. Task creation therefore appends them here, once, so preview,
convert and direct create all store the same scope.

Pure: no filesystem or repository access.
"""

from __future__ import annotations

from collections.abc import Sequence

from .agent_tool_instructions import DERIVED_ARTIFACTS

POLICY_SOURCE = "src/aiworkhub/agent_tool_instructions.py"


def expand_policy_derived_writes(
    allowed_writes: Sequence[str], *, read_only: bool
) -> tuple[list[str], list[str]]:
    """Return ``(allowed_writes, appended)`` for one card.

    Only a writable card that lists ``POLICY_SOURCE`` exactly is widened. The
    caller's entries keep their order and come first; each missing
    ``DERIVED_ARTIFACTS`` path follows in tuple order, and ``appended`` names
    exactly those. Any other card comes back unchanged with ``appended`` empty.
    """
    writes = list(allowed_writes)
    if read_only or POLICY_SOURCE not in writes:
        return writes, []
    present = set(writes)
    appended = [path for path in DERIVED_ARTIFACTS if path not in present]
    return [*writes, *appended], appended
