"""NF-2026-02303: reroute accepts the catalog worker_id or the execution_runner."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, workforce_catalog  # noqa: E402

# One owner for the pristine-card fixtures: the NF-2026-01709 module.
from test_reroute_claude_cli_alias_nf01709 import (  # noqa: E402,F401
    _ADAPTER,
    _ALIAS,
    _FROM_RUNNER,
    _TO_RUNNER,
    _claude_route,
    _insert_pristine,
    _row,
    repo,
)

_WORKER_ID = "claude-sonnet-5"


def _route(**overrides) -> dict:
    return _claude_route(worker_id=_WORKER_ID, **overrides)


def _reroute(task_id: str, to_runner: str) -> dict:
    return core.reroute_launch_identity(
        task_id,
        from_runner=_FROM_RUNNER,
        to_runner=to_runner,
        to_adapter_id=_ADAPTER,
        to_model=_ALIAS,
        reason="owner quota reroute",
    )


@pytest.mark.parametrize("name", [_WORKER_ID, _TO_RUNNER], ids=["worker_id", "execution_runner"])
def test_worker_id_and_execution_runner_both_select_the_route(monkeypatch, repo, name):
    task_id = "NF02303_ACCEPT_" + ("WORKER" if name == _WORKER_ID else "RUNNER")
    _insert_pristine(repo, task_id)
    monkeypatch.setattr(
        workforce_catalog, "build_catalog", lambda *_: {"workers": [_route()]}
    )

    result = _reroute(task_id, name)

    assert result["ok"] is True, result
    # The card keeps the launchable identity, never the worker_id alias.
    assert result["to_runner"] == _TO_RUNNER
    assert _row(repo, task_id)["runner"] == _TO_RUNNER


def test_unknown_name_is_route_not_found_and_lists_accepted_runners(monkeypatch, repo):
    task_id = "NF02303_UNKNOWN"
    _insert_pristine(repo, task_id)
    # The catalog carries a different route on the same adapter and model, so
    # the requested (statically valid) runner names no catalog row at all.
    other = _claude_route(execution_runner="claude_other-9", worker_id="claude-other-9")
    monkeypatch.setattr(
        workforce_catalog, "build_catalog", lambda *_: {"workers": [other]}
    )
    before = _row(repo, task_id)

    result = _reroute(task_id, _TO_RUNNER)

    assert result["ok"] is False
    assert result["stderr"].startswith("reroute_target_rejected:route_not_found:")
    assert "accepted_execution_runners=claude-other-9,claude_other-9" in result["stderr"]
    assert "implementation_worker_role_required" not in result["stderr"]
    assert _row(repo, task_id) == before


def test_matched_non_implementation_route_keeps_the_role_error(monkeypatch, repo):
    task_id = "NF02303_ROLE"
    _insert_pristine(repo, task_id)
    monkeypatch.setattr(
        workforce_catalog,
        "build_catalog",
        lambda *_: {"workers": [_route(implementation_worker=False)]},
    )
    before = _row(repo, task_id)

    result = _reroute(task_id, _WORKER_ID)

    assert result["ok"] is False
    assert result["stderr"] == "reroute_target_rejected:implementation_worker_role_required"
    assert _row(repo, task_id) == before


def test_model_mismatch_still_names_accepted_runners():
    # The static identity check rejects a model mismatch before the matcher
    # runs in the full flow, so pin the matcher itself: a route whose model
    # differs is filtered out, yet the accepted names must still be listed.
    from aiworkhub import process_launcher
    from aiworkhub.reroute_route_match import match_reroute_route

    route, error = match_reroute_route(
        [_route(model="opus")],
        to_runner=_WORKER_ID,
        to_adapter_id=_ADAPTER,
        canonical_model=_ALIAS,
        normalize_model=process_launcher._normalize_workforce_model,
    )

    assert route is None
    assert error.startswith("reroute_target_rejected:route_not_found:")
    assert f"accepted_execution_runners={_WORKER_ID},{_TO_RUNNER}" in error
