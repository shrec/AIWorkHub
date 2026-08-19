"""Regression for NF-2026-00315: promotion must REFUSE to write a version
constant or release-metadata projection that moves BACKWARDS relative to
canonical, naming the file and both versions -- a refusal, not a logged warning.

Measured twice: a candidate whose worktree was cut before a release still
carried the OLD constant (EXPECTED_MCP_PACKAGE_VERSION "0.9.82" against canonical
0.9.84, and its successor "0.9.85" against canonical 0.9.87), which on promotion
would silently revert ``src/aiworkhub/_version.py`` and every projection derived
from it and break every extension preflight.  Both were caught by hand.

The decisive tests here exercise the PRODUCTION promotion seam --
``ProcessManager._promote_accepted_candidate``, the sole write path in
``accept_review`` -- with a real candidate/canonical pair on disk, and observe
that the write NEVER happens: they fail if the guard is ever disconnected from
the write, because a disconnected guard would let ``promote`` run.  The lower
block keeps the pure-function coverage of the comparison semantics.
"""

import types

import pytest

from aiworkhub import process_launcher
from aiworkhub.process_launcher import (
    ProcessManager,
    PromotionVersionRegression,
    evaluate_promotion_version_regression,
    refuse_version_regression,
)

VERSION_REL = "src/aiworkhub/_version.py"
RUNTIME_REL = "vscode-extension/extension.js"


def _version_py(value: str) -> str:
    return f'__version__ = "{value}"\n'


def _runtime_js(value: str) -> str:
    return f'const EXPECTED_MCP_PACKAGE_VERSION = "{value}";\n'


def _manager(tmp_path) -> ProcessManager:
    # isolation_enabled=False keeps construction side-effect free (no reconcile,
    # no process log I/O); self.repo is the canonical tree under test.
    return ProcessManager(repo=tmp_path, isolation_enabled=False)


def _write(path, text) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _candidate(tmp_path, rel: str, text: str):
    root = tmp_path / "candidate"
    _write(root / rel, text)
    return types.SimpleNamespace(path=str(root))


def _record_promote(monkeypatch):
    """Replace the module-level ``promote`` with a recorder; return its log."""

    writes: list[list[str]] = []

    def _fake_promote(_workspace, changed):
        writes.append(list(changed))
        return list(changed)

    monkeypatch.setattr(process_launcher, "promote", _fake_promote)
    return writes


# --------------------------------------------------------------------------
# Production path: the guard runs inside the real promotion seam, before any
# byte is written.
# --------------------------------------------------------------------------


def test_production_promotion_refuses_backwards_version_before_writing(
    tmp_path, monkeypatch
):
    # Canonical already at 0.9.84; the stale-base candidate still carries 0.9.82.
    _write(tmp_path / VERSION_REL, _version_py("0.9.84"))
    workspace = _candidate(tmp_path, VERSION_REL, _version_py("0.9.82"))
    manager = _manager(tmp_path)
    writes = _record_promote(monkeypatch)

    with pytest.raises(PromotionVersionRegression) as excinfo:
        manager._promote_accepted_candidate(workspace, [VERSION_REL])

    message = str(excinfo.value)
    assert VERSION_REL in message
    assert "0.9.82" in message and "0.9.84" in message
    assert "BACKWARDS" in message
    # The whole point: the write never happened. A guard that is not on the path
    # would let ``promote`` run here, so this assertion catches a disconnection.
    assert writes == []


def test_production_promotion_refuses_backwards_runtime_projection(
    tmp_path, monkeypatch
):
    # The exact measured shape: EXPECTED_MCP_PACKAGE_VERSION 0.9.82 vs 0.9.84.
    _write(tmp_path / RUNTIME_REL, _runtime_js("0.9.84"))
    workspace = _candidate(tmp_path, RUNTIME_REL, _runtime_js("0.9.82"))
    manager = _manager(tmp_path)
    writes = _record_promote(monkeypatch)

    with pytest.raises(PromotionVersionRegression) as excinfo:
        manager._promote_accepted_candidate(workspace, [RUNTIME_REL])

    assert RUNTIME_REL in str(excinfo.value)
    assert writes == []


def test_production_promotion_writes_when_version_unchanged(tmp_path, monkeypatch):
    # Equal is the normal case: silent, and the write proceeds.
    _write(tmp_path / VERSION_REL, _version_py("0.9.88"))
    workspace = _candidate(tmp_path, VERSION_REL, _version_py("0.9.88"))
    manager = _manager(tmp_path)
    writes = _record_promote(monkeypatch)

    promoted = manager._promote_accepted_candidate(workspace, [VERSION_REL])

    assert writes == [[VERSION_REL]]
    assert promoted == [VERSION_REL]


def test_production_promotion_allows_version_moving_forward(tmp_path, monkeypatch):
    # Candidate ahead of canonical: the release moved forward; the write is allowed.
    _write(tmp_path / VERSION_REL, _version_py("0.9.87"))
    workspace = _candidate(tmp_path, VERSION_REL, _version_py("0.9.88"))
    manager = _manager(tmp_path)
    writes = _record_promote(monkeypatch)

    manager._promote_accepted_candidate(workspace, [VERSION_REL])

    assert writes == [[VERSION_REL]]


def test_production_promotion_ignores_non_version_changed_files(tmp_path, monkeypatch):
    # A changed non-version file must never trip the guard, whatever its content.
    _write(tmp_path / "src/aiworkhub/foo.py", "x = 1\n")
    workspace = _candidate(tmp_path, "src/aiworkhub/foo.py", "x = 2\n")
    manager = _manager(tmp_path)
    writes = _record_promote(monkeypatch)

    manager._promote_accepted_candidate(workspace, ["src/aiworkhub/foo.py"])

    assert writes == [["src/aiworkhub/foo.py"]]


def test_production_promotion_fails_closed_on_unparseable_candidate_version(
    tmp_path, monkeypatch
):
    # A recognised version file whose candidate value cannot be parsed cannot be
    # proven safe: the guard refuses (unverifiable) and the write never happens.
    _write(tmp_path / VERSION_REL, _version_py("0.9.88"))
    workspace = _candidate(tmp_path, VERSION_REL, "__version__ = not_a_literal\n")
    manager = _manager(tmp_path)
    writes = _record_promote(monkeypatch)

    with pytest.raises(PromotionVersionRegression):
        manager._promote_accepted_candidate(workspace, [VERSION_REL])

    assert writes == []


# --------------------------------------------------------------------------
# Comparison semantics (pure): equal is silent, ahead is allowed, backwards and
# unparseable are named refusals.
# --------------------------------------------------------------------------


def _proj(file, candidate, canonical):
    return {
        "file": file,
        "candidate_version": candidate,
        "canonical_version": canonical,
    }


def test_promotion_regression_is_a_refusal_not_merely_a_warning():
    report = evaluate_promotion_version_regression(
        [_proj(VERSION_REL, "0.9.85", "0.9.87")]
    )
    assert report["refused"] is True
    assert report["ok"] is False
    regression = report["regressions"][0]
    assert regression["order"] == "regressed"
    assert regression["file"] == VERSION_REL
    assert regression["candidate_version"] == "0.9.85"
    assert regression["canonical_version"] == "0.9.87"


def test_promotion_silent_when_candidate_equals_canonical():
    report = refuse_version_regression(
        [
            _proj(VERSION_REL, "0.9.87", "0.9.87"),
            _proj("vscode-extension/package.json", "0.9.87", "0.9.87"),
        ]
    )  # must not raise
    assert report["ok"] is True
    assert report["refused"] is False
    assert report["reasons"] == []
    assert {record["order"] for record in report["checked"]} == {"equal"}


def test_promotion_allows_candidate_ahead_of_canonical():
    report = refuse_version_regression([_proj(VERSION_REL, "0.9.88", "0.9.87")])
    assert report["ok"] is True
    assert report["refused"] is False
    assert report["checked"][0]["order"] == "ahead"


def test_promotion_ahead_across_minor_boundary_is_allowed():
    report = evaluate_promotion_version_regression(
        [_proj(VERSION_REL, "0.10.0", "0.9.87")]
    )
    assert report["ok"] is True
    assert report["checked"][0]["order"] == "ahead"


def test_promotion_fails_closed_on_unparseable_version():
    with pytest.raises(PromotionVersionRegression) as excinfo:
        refuse_version_regression(
            [_proj("vscode-extension/package.json", "missing", "0.9.87")]
        )
    assert "vscode-extension/package.json" in str(excinfo.value)
