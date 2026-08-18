"""Read-only launch-queue diagnostics.

A disabled launch contract must name why a launch is blocked truthfully. The
canonical B286 queue-request path (``core``) and ``launch_queue_contract`` both
report ``unknown_adapter`` whenever an adapter is not in ``KNOWN_ADAPTERS`` --
and that same label was surfaced for a *known* adapter whose only real blocker
was the disabled/unimplemented contract, mislabelling the cause. This
side-effect-free diagnostic reports the actual primary reason instead, so a
known adapter blocked by the disabled contract is never called an unknown one.

Pure reads only: it consults the ``launch_queue_contract`` constants and env
gates and never launches, writes, or mutates anything.
"""

from __future__ import annotations

from typing import Any

from . import launch_queue_contract as _contract

SCHEMA_ID = "aiworkhub.launch_queue_readonly_diagnostic.v1"

UNKNOWN_ADAPTER = "unknown_adapter"
MANUAL_ONLY_REASON = "manual_external_adapter_never_local_launch"
LAUNCH_CONTRACT_DISABLED = "launch_contract_disabled"


def diagnose_launch_block(adapter_id: str) -> dict[str, Any]:
    """Return the truthful primary reason a launch for ``adapter_id`` is blocked.

    Read-only. Only a genuinely unrecognised adapter reports
    ``unknown_adapter``; a known adapter blocked by the disabled/unimplemented
    contract reports that (``launch_contract_disabled``), and a known
    manual-only adapter reports its manual-only reason -- never a false
    ``unknown_adapter``.
    """

    adapter = str(adapter_id or "")
    known = adapter in _contract.KNOWN_ADAPTERS
    manual_only = adapter in _contract.MANUAL_ONLY

    reasons: list[str] = []
    if not known:
        reasons.append(f"{UNKNOWN_ADAPTER}:{adapter}")
    if manual_only:
        reasons.append(MANUAL_ONLY_REASON)
    if not _contract.LAUNCH_IMPLEMENTED:
        reasons.append("launcher_not_implemented_in_contract")
    if not _contract.launch_flag_set():
        reasons.append(f"{_contract.ALLOW_LAUNCH_ENV}!='{_contract.ENABLED_VALUE}'")
    if not _contract.writes_allowed():
        reasons.append(f"{_contract.ALLOW_WRITES_ENV}!='{_contract.ENABLED_VALUE}'")

    # The primary reason names the actual cause. A known adapter is never
    # mislabelled unknown_adapter: the disabled/unimplemented contract (or its
    # manual-only status) is the true blocker and is reported as such.
    if not known:
        primary = f"{UNKNOWN_ADAPTER}:{adapter}"
    elif manual_only:
        primary = MANUAL_ONLY_REASON
    elif not _contract.LAUNCH_IMPLEMENTED:
        primary = LAUNCH_CONTRACT_DISABLED
    else:
        primary = "; ".join(reasons) or "launch_permitted"

    return {
        "schema_id": SCHEMA_ID,
        "readonly": True,
        "adapter_id": adapter,
        "adapter_known": known,
        "manual_only": manual_only,
        "launch_implemented": _contract.LAUNCH_IMPLEMENTED,
        "launch_enabled": _contract.launch_enabled(),
        "env_gates_open": _contract.env_gates_open(),
        "blocked_reason": primary,
        "reasons": reasons,
    }
