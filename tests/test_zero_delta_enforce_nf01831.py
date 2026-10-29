"""NF-2026-01831: the zero-delta tripwire enforces after its advisory notice."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from aiworkhub import launch_zero_delta as zd
from aiworkhub.worker_workspace import WorkerWorkspace

ALLOWED = "out/result.json"
SEED = b"seed\n"
TIMEOUT = 1_200


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(zd.ZERO_DELTA_ENFORCE_SECONDS_ENV, raising=False)
    monkeypatch.delenv(zd.ZERO_DELTA_ELAPSED_SHARE_ENV, raising=False)


def _workspace(tmp_path: Path, *, content: bytes = SEED) -> WorkerWorkspace:
    worktree = tmp_path / "request" / "worktree"
    for directory in (tmp_path / "repo", worktree, tmp_path / "request" / "home"):
        directory.mkdir(parents=True, exist_ok=True)
    target = worktree / ALLOWED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    mode = target.stat().st_mode & 0o777
    baseline = {ALLOWED: f"file:{mode:o}:{hashlib.sha256(SEED).hexdigest()}"}
    return WorkerWorkspace(
        request_id="req",
        repo=tmp_path / "repo",
        path=worktree,
        home=tmp_path / "request" / "home",
        allowed_writes=(ALLOWED,),
        parent_baseline=baseline,
        workspace_baseline=baseline,
    )


def _evaluate(workspace: WorkerWorkspace, elapsed: float, **kwargs):
    return zd.evaluate_zero_delta_tripwire(
        workspace=workspace,
        elapsed_seconds=elapsed,
        timeout_seconds=TIMEOUT,
        required_outputs=[ALLOWED],
        **kwargs,
    )


def test_old_behaviour_never_enforced_is_now_enforced(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = _evaluate(workspace, 700.0)
    assert first.enforce is False
    assert first.settled is False
    assert first.notice is not None and first.notice["enforced"] is False
    late = _evaluate(workspace, 2800.0, notice_emitted=True)
    assert late.enforce is True
    assert late.settled is True
    assert late.notice is not None
    assert late.notice["enforced"] is True
    assert late.notice["reason"] == zd.ZERO_DELTA_ENFORCE_REASON == "stalled_no_edit"


def test_first_notice_is_advisory_and_keeps_existing_fields(tmp_path: Path) -> None:
    result = _evaluate(_workspace(tmp_path), 700.0)
    assert result.settled is False
    assert result.enforce is False
    notice = result.notice
    assert notice is not None
    assert notice["notice"] == zd.ZERO_DELTA_NOTICE
    assert notice["elapsed_seconds"] == 700.0
    assert notice["notice_after_seconds"] == 600.0
    assert notice["enforce_after_seconds"] == 2700.0
    assert notice["timeout_seconds"] == TIMEOUT
    assert notice["required_outputs"] == [ALLOWED]
    assert notice["allowed_writes"] == [ALLOWED]
    assert notice["changed_allowed_writes"] == []
    assert notice["enforced"] is False
    assert "reason" not in notice


def test_before_notice_threshold_nothing_happens(tmp_path: Path) -> None:
    result = _evaluate(_workspace(tmp_path), 100.0)
    assert result.notice is None and result.settled is False and not result.enforce


def test_between_notice_and_enforce_is_quiet_after_notice(tmp_path: Path) -> None:
    result = _evaluate(_workspace(tmp_path), 2000.0, notice_emitted=True)
    assert result.notice is None
    assert result.settled is False
    assert result.enforce is False


def test_change_after_warning_before_enforce_is_not_cancelled(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, content=b"edited\n")
    result = _evaluate(workspace, 2800.0, notice_emitted=True)
    assert result.settled is True
    assert result.enforce is False
    assert result.notice is None


def test_read_only_card_is_never_enforced(tmp_path: Path) -> None:
    result = _evaluate(_workspace(tmp_path), 9999.0, read_only=True, notice_emitted=True)
    assert result.settled is True and result.enforce is False and result.notice is None


def test_empty_allowed_writes_is_never_enforced(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    object.__setattr__(workspace, "allowed_writes", ())
    result = _evaluate(workspace, 9999.0, notice_emitted=True)
    assert result.settled is True and result.enforce is False and result.notice is None


def test_allow_unchanged_required_outputs_is_never_enforced(tmp_path: Path) -> None:
    result = _evaluate(
        _workspace(tmp_path),
        9999.0,
        allow_unchanged_required_outputs=[ALLOWED],
        notice_emitted=True,
    )
    assert result.settled is True and result.enforce is False and result.notice is None


def test_enforce_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(zd.ZERO_DELTA_ENFORCE_SECONDS_ENV, "1000")
    assert zd.zero_delta_enforce_after_seconds(TIMEOUT) == 1000.0


@pytest.mark.parametrize("raw", ["abc", "", "  ", "0", "-5", "nan", "inf", "-inf"])
def test_invalid_enforce_threshold_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(zd.ZERO_DELTA_ENFORCE_SECONDS_ENV, raw)
    assert zd.zero_delta_enforce_after_seconds(TIMEOUT) == (
        zd.ZERO_DELTA_DEFAULT_ENFORCE_SECONDS
    )


def test_enforce_threshold_never_below_notice_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(zd.ZERO_DELTA_ENFORCE_SECONDS_ENV, "1")
    notice_after = zd.zero_delta_notice_after_seconds(TIMEOUT)
    assert notice_after == 600.0
    assert zd.zero_delta_enforce_after_seconds(TIMEOUT) == notice_after
    assert math.isfinite(zd.zero_delta_enforce_after_seconds(TIMEOUT))


def test_notice_then_enforce_each_happen_once_per_threshold(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    kinds = []
    emitted = False
    for elapsed in (10.0, 650.0, 700.0, 2000.0, 2800.0):
        result = _evaluate(workspace, elapsed, notice_emitted=emitted)
        if result.notice is not None:
            kinds.append(result.notice["enforced"])
            emitted = True
    assert kinds == [False, True]
