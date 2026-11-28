"""NF-2026-01023: the private-session hop is gone; the relay runs directly.

NF-2026-01154 hopped every AppContainer launch into a private NewCredentials
logon session only so the sandbox drive letter stayed out of the owner's
session.  With no drive letter there is no hop.  What remains is the
``validation_relay_main`` entry finalization may still start: it runs the
validation directly, in-process, and answers with exactly one record.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import aiworkhub.worker_workspace as worker_workspace
from aiworkhub import windows_private_session as wps


def _spec(**overrides):
    spec = {
        "schema_id": wps._VALIDATION_RELAY_SCHEMA,
        "argv": ["python", "-m", "pytest", "-q"],
        "workspace": {"path": "w"},
        "adapter_id": "claude",
        "cwd": "C:\\work",
        "env": {"A": "1"},
        "timeout_seconds": 30,
    }
    spec.update(overrides)
    return json.dumps(spec).encode("utf-8")


def _run_main(monkeypatch, payload: bytes, runner):
    monkeypatch.setattr(
        worker_workspace.WorkerWorkspace, "from_metadata", staticmethod(lambda m: m)
    )
    monkeypatch.setattr(worker_workspace, "_run_appcontainer_validation", runner)
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=out))
    assert wps.validation_relay_main() == 0
    return json.loads(out.getvalue().decode("utf-8"))


def test_no_session_hop_remains():
    for name in (
        "spawn_in_private_logon_session",
        "reexec_supervisor_if_owner_session",
        "in_private_logon_session",
        "_CtypesSpawnApi",
    ):
        assert not hasattr(wps, name), name
    assert "validation_relay_main" in wps._VALIDATION_RELAY_ENTRY


def test_hopped_entry_point_calls_the_runner_directly():
    seen = {}

    def run_direct(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = wps.run_appcontainer_validation_hopped(
        ["a"], workspace="ws", adapter_id="x", cwd="c", env={"K": "V"},
        timeout_seconds=5, run_direct=run_direct,
    )
    assert result.returncode == 0
    assert seen == {
        "argv": ["a"], "workspace": "ws", "adapter_id": "x", "cwd": "c",
        "env": {"K": "V"}, "timeout_seconds": 5,
    }


def test_relay_main_runs_the_validation_in_process(monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 3, "out", "err")

    record = _run_main(monkeypatch, _spec(), runner)

    assert len(calls) == 1
    assert calls[0][1]["cwd"] == "C:\\work"
    assert record == {
        "schema_id": wps._VALIDATION_RELAY_SCHEMA,
        "kind": "completed",
        "args": ["python", "-m", "pytest", "-q"],
        "returncode": 3,
        "stdout": "out",
        "stderr": "err",
    }


@pytest.mark.parametrize(
    "error, kind",
    [
        (subprocess.TimeoutExpired(["p"], 9, output="o", stderr="e"), "timeout"),
        (OSError(2, "missing"), "oserror"),
    ],
)
def test_relay_main_maps_runner_failures(monkeypatch, error, kind):
    def runner(argv, **kwargs):
        raise error

    record = _run_main(monkeypatch, _spec(), runner)
    assert record["kind"] == kind


def test_relay_main_fails_closed_on_a_foreign_spec(monkeypatch):
    def runner(argv, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("ran a foreign spec")

    record = _run_main(monkeypatch, _spec(schema_id="other"), runner)
    assert record["kind"] == "oserror"
    assert "schema_mismatch" in record["message"]
