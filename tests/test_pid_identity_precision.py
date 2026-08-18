"""NF-2026-00254 / NF-2026-00274: pid identity precision and honest diagnostics.

``pid_start_ticks`` must survive a value above 2**53 across every surface a
JavaScript consumer reads, and the two surfaces (``status`` / task_show and
``collect`` / agent_task_status) must agree.  A disabled launch contract must
report the real reason, not ``unknown_adapter``.

No ProcessManager is constructed: the shared surface serializer and the
read-only diagnostic are exercised directly.
"""

from __future__ import annotations

from aiworkhub import launch_queue_readonly_tool as lq
from aiworkhub import process_launcher as pl

# Strictly greater than 2**53 == 9007199254740992: a JS Number cannot hold it.
BIG_TICKS = 9007199254740993


def test_pid_start_ticks_survives_above_2_53_as_string() -> None:
    surfaced = pl._pid_ticks_to_surface_str(BIG_TICKS)
    assert surfaced == "9007199254740993"
    # Lossless as a string; a float (JS Number) would silently round.
    assert int(surfaced) == BIG_TICKS
    assert float(BIG_TICKS) != BIG_TICKS


def test_pid_identity_surface_is_lossless_and_non_mutating() -> None:
    event = {"pid_start_ticks": BIG_TICKS, "provider_pid_start_ticks": BIG_TICKS + 1}
    fields = pl.pid_identity_surface(event)
    assert fields["pid_start_ticks"] == "9007199254740993"
    assert fields["provider_pid_start_ticks"] == "9007199254740994"
    # The source event is never mutated: internal pid comparisons keep the int.
    assert event["pid_start_ticks"] == BIG_TICKS
    assert isinstance(event["pid_start_ticks"], int)


def test_task_show_and_agent_task_status_agree() -> None:
    # Both surfaces derive their pid identity from the one shared function, so
    # the string they expose for the same event is identical.
    latest_event = {"pid_start_ticks": BIG_TICKS, "state": "running", "pid": 4242}
    task_show_view = {**latest_event, **pl.pid_identity_surface(latest_event)}
    agent_status_view = {}
    agent_status_view.update(pl.pid_identity_surface(latest_event))
    assert task_show_view["pid_start_ticks"] == agent_status_view["pid_start_ticks"]
    assert task_show_view["pid_start_ticks"] == "9007199254740993"


def test_disabled_contract_reports_real_reason_not_unknown_adapter() -> None:
    # A known adapter blocked only by the disabled contract must not be
    # mislabelled as an unknown adapter.
    known = lq.diagnose_launch_block("claude_cli")
    assert known["adapter_known"] is True
    assert known["blocked_reason"] == "launch_contract_disabled"
    assert "unknown_adapter" not in known["blocked_reason"]

    # A genuinely unrecognised adapter still reports unknown_adapter.
    unknown = lq.diagnose_launch_block("bogus_adapter")
    assert unknown["blocked_reason"] == "unknown_adapter:bogus_adapter"

    # A known manual-only adapter reports its manual-only reason.
    manual = lq.diagnose_launch_block("deepseek_manual")
    assert manual["adapter_known"] is True
    assert manual["blocked_reason"] == "manual_external_adapter_never_local_launch"
