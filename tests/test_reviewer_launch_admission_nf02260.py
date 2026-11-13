"""NF-2026-02260: reviewer launch admission helpers moved out of process_launcher.

``_reviewer_launch_owner_join``, ``_reviewer_admission_ended_at``,
``_reviewer_admission_wait_seconds`` (plus the NF-2026-02248 timeout-evidence
and admission-wait helpers and the state only they use) now live in
``reviewer_launch_admission``.  These tests pin that the definitions have one
owner, that ``process_launcher`` and ``ProcessManager`` re-expose the exact same
objects under their original names, and that the moved behaviour is unchanged.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import process_launcher
from aiworkhub import reviewer_launch_admission as rla

PM = process_launcher.ProcessManager
MOVED = (
    "_reviewer_launch_owner_join",
    "_reviewer_admission_ended_at",
    "_reviewer_admission_wait_seconds",
    "_publish_reviewer_launch_timeout_evidence",
    "_await_reviewer_admission",
)


class _Launcher:
    """A launcher stand-in that never finishes on its own."""

    def __init__(self, ident: int | None) -> None:
        self.ident = ident

    def is_alive(self) -> bool:
        return True

    def join(self, _timeout: float | None = None) -> None:
        return None


def _owner(**overrides: Any) -> SimpleNamespace:
    progress: list[tuple[Any, ...]] = []
    owner = SimpleNamespace(
        _QUALITY_REVIEW_LAUNCH_OWNER_SECONDS=0.0,
        _reviewer_provider_committed=lambda _rid: False,
        _reviewer_source_graph_prewarm_live=lambda _rid: False,
        _reviewer_admission_wait_seconds=lambda _launcher: None,
        _reviewer_admission_ended_at=lambda _launcher: None,
        _publish_reviewer_progress=lambda *args, **kwargs: progress.append((args, kwargs)),
        progress=progress,
    )
    for key, value in overrides.items():
        setattr(owner, key, value)
    return owner


@pytest.mark.parametrize("name", MOVED)
def test_moved_helper_has_one_owner_and_is_importable_from_process_launcher(name: str) -> None:
    fn = getattr(rla, name)
    assert fn.__module__ == "aiworkhub.reviewer_launch_admission"
    assert getattr(process_launcher, name) is fn
    bound = PM.__dict__[name]
    assert getattr(bound, "__func__", bound) is fn
    source = Path(str(process_launcher.__file__)).read_text(encoding="utf-8")
    assert f"def {name}(" not in source


def test_state_is_the_same_object_on_process_manager() -> None:
    assert PM._REVIEWER_ADMISSION_WAIT_WINDOWS is rla._REVIEWER_ADMISSION_WAIT_WINDOWS
    assert PM._REVIEWER_ADMISSION_ENDED_AT is rla._REVIEWER_ADMISSION_ENDED_AT
    assert rla._REVIEWER_ADMISSION_WAIT_GRACE_SECONDS == float(
        rla.launch_admission.DEFAULT_POLL_SECONDS
    )


def test_admission_wait_seconds_reports_only_a_live_bounded_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ident = -2260
    clock = {"now": 100.0}
    monkeypatch.setattr(rla.time, "monotonic", lambda: clock["now"])
    monkeypatch.setitem(rla._REVIEWER_ADMISSION_WAIT_WINDOWS, ident, (90.0, 110.0))
    launcher = _Launcher(ident)
    assert PM._reviewer_admission_wait_seconds(launcher) == pytest.approx(10.0)
    clock["now"] = 110.0 + rla._REVIEWER_ADMISSION_WAIT_GRACE_SECONDS
    assert PM._reviewer_admission_wait_seconds(launcher) is not None
    clock["now"] += 0.5
    assert PM._reviewer_admission_wait_seconds(launcher) is None
    assert PM._reviewer_admission_wait_seconds(_Launcher(None)) is None
    assert PM._reviewer_admission_wait_seconds(_Launcher(ident - 1)) is None


def test_admission_ended_at_reads_the_registered_moment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ident = -2261
    launcher = _Launcher(ident)
    assert PM._reviewer_admission_ended_at(launcher) is None
    monkeypatch.setitem(rla._REVIEWER_ADMISSION_ENDED_AT, ident, 42.5)
    assert PM._reviewer_admission_ended_at(launcher) == 42.5
    assert PM._reviewer_admission_ended_at(_Launcher(None)) is None


@pytest.mark.parametrize("registered", [True, False])
def test_await_reviewer_admission_registers_and_clears_its_window(
    monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    ident = threading.get_ident()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(rla.launch_admission, "configured_wait_seconds", lambda: 30.0)

    def _admit(*, kind: str, probe: Any, wait_seconds: float) -> dict[str, Any]:
        seen["window"] = rla._REVIEWER_ADMISSION_WAIT_WINDOWS.get(ident)
        seen["kind"], seen["wait"] = kind, wait_seconds
        return probe()

    monkeypatch.setattr(process_launcher.launch_admission, "await_memory_admission", _admit)
    if registered:
        monkeypatch.setitem(rla._REVIEWER_ADMISSION_ENDED_AT, ident, None)
    result = PM._await_reviewer_admission(SimpleNamespace(), lambda: {"admit": True})
    assert result == {"admit": True}
    assert seen["kind"] == "reviewer" and seen["wait"] == 30.0
    started, deadline = seen["window"]
    assert deadline == pytest.approx(started + 30.0)
    assert ident not in rla._REVIEWER_ADMISSION_WAIT_WINDOWS
    if registered:
        assert rla._REVIEWER_ADMISSION_ENDED_AT[ident] >= started
    else:
        assert ident not in rla._REVIEWER_ADMISSION_ENDED_AT


def test_owner_join_completes_and_clears_its_registration() -> None:
    launcher = threading.Thread(target=lambda: None)
    launcher.start()
    owner = _owner(_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS=5.0)
    assert PM._reviewer_launch_owner_join(owner, launcher, "r" * 32) == "completed"
    assert launcher.ident not in rla._REVIEWER_ADMISSION_ENDED_AT


def test_owner_join_returns_provider_committed() -> None:
    owner = _owner(_reviewer_provider_committed=lambda _rid: True)
    assert rla._reviewer_launch_owner_join(owner, _Launcher(-2262), "r") == "provider_committed"
    assert -2262 not in rla._REVIEWER_ADMISSION_ENDED_AT


def test_owner_join_times_out_past_the_outer_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rla.launch_admission, "configured_wait_seconds", lambda: 0.0)
    monkeypatch.setattr(rla, "_REVIEWER_ADMISSION_WAIT_GRACE_SECONDS", 0.0)
    assert rla._reviewer_launch_owner_join(_owner(), _Launcher(-2263), "r") == "timeout"


def test_owner_join_dispatches_admission_through_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's (monkeypatchable) wait accessor explains a live owner once."""

    monkeypatch.setattr(rla.launch_admission, "configured_wait_seconds", lambda: 60.0)
    waits = iter([1.0])
    owner = _owner(_reviewer_admission_wait_seconds=lambda _launcher: next(waits, None))
    assert rla._reviewer_launch_owner_join(owner, _Launcher(-2264), "rid") == "timeout"
    assert owner.progress == [(("rid", "admission_waiting", "waited_seconds=1.0"), {})]


def test_timeout_evidence_publishes_adapter_phase_and_no_spawn() -> None:
    latest = {"rid": {"adapter_id": "claude_cli", "preparation_phase": "packet_prepared"}}
    owner = _owner(_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS=300.0)
    owner._latest_by_request = lambda: latest
    PM._publish_reviewer_launch_timeout_evidence(owner, "rid", "")
    assert owner.progress == [(
        (
            "rid",
            "launch_timeout_evidence",
            '{"adapter_id":"claude_cli","last_phase":"packet_prepared",'
            '"owner_ceiling_seconds":300.0,"provider_spawn_committed":false,'
            '"retryable":true}',
        ),
        {"adapter_id": None},
    )]
