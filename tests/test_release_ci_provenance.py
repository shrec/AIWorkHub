from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_release_ci_provenance.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SPEC = importlib.util.spec_from_file_location("release_ci_provenance", SCRIPT)
assert SPEC and SPEC.loader
provenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provenance)


def run(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "head_sha": "a" * 40,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/ci.yml",
    }
    value.update(changes)
    return value


def require(runs: list[object]) -> None:
    with patch.object(provenance, "_request_json", return_value={"workflow_runs": runs}):
        provenance.require_successful_push_ci(
            repository="owner/repo",
            sha="a" * 40,
            workflow=".github/workflows/ci.yml",
            token="secret",
        )


def test_accepts_only_exact_successful_completed_push_workflow():
    require([run()])


def test_accepts_documented_workflow_path_with_ref_suffix():
    require([run(path=".github/workflows/ci.yml@main")])


@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": "b" * 40},
        {"event": "workflow_dispatch"},
        {"status": "in_progress"},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"path": ".github/workflows/other.yml"},
        {"path": ".github/workflows/other.yml@main"},
    ],
)
def test_rejects_every_identity_or_status_mismatch(change):
    with pytest.raises(provenance.ProvenanceError):
        require([run(**change)])


def test_api_failure_and_bounded_pagination_fail_closed():
    arguments = {
        "repository": "owner/repo",
        "sha": "a" * 40,
        "workflow": ".github/workflows/ci.yml",
        "token": "secret",
    }
    with patch.object(provenance.urllib.request, "urlopen", side_effect=OSError("offline")):
        with pytest.raises(provenance.ProvenanceError, match="API request failed"):
            provenance.require_successful_push_ci(**arguments)
    full = {"workflow_runs": [run(head_sha="b" * 40)] * provenance.PER_PAGE}
    with patch.object(provenance, "_request_json", return_value=full) as request:
        with pytest.raises(provenance.ProvenanceError, match="bounded pages"):
            provenance.require_successful_push_ci(**arguments)
    assert request.call_count == provenance.MAX_PAGES


def test_token_is_environment_only_and_not_in_argv_or_output():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--token" not in source
    env = {key: value for key, value in os.environ.items() if key != "GITHUB_REPOSITORY"}
    env["GITHUB_TOKEN"] = "do-not-print"
    command = [sys.executable, str(SCRIPT), "--sha", "a" * 40]
    result = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert "do-not-print" not in result.stdout + result.stderr
    assert not any(arg.startswith("--token") or arg == "do-not-print" for arg in command)
    assert result.returncode != 0


def test_release_workflow_binds_manual_input_to_tag_commit_and_keeps_smoke_gates():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "refs/tags/${{ inputs.tag }}" in text
    assert 'tag_commit="$(git rev-parse "$tag_ref^{commit}")"' in text
    assert "refs/tags/${{ inputs.tag }}^{commit}" not in text
    for block in text.split("run: |")[1:]:
        assert "${{ inputs.tag }}" not in block.split("\n      - ", 1)[0]
    assert "ref: ${{ github.event_name == 'workflow_dispatch' && inputs.tag || github.ref }}" not in text
    assert "ref: refs/tags/${{ inputs.tag }}" in text
    assert "check_release_ci_provenance.py" in text
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text
    assert "--sha \"$RELEASE_SHA\"" in text
    assert "python -m venv" in text
    assert 'pip install "${wheels[0]}"' in text
    assert "--no-index" not in text
    assert "import aiworkhub" in text
    assert "aiworkhub --help" in text
    assert "Verify fresh VSIX manifest and package" in text
    for forbidden in (
        "Run Python tests",
        "Run static quality gates",
        "Run extension tests",
        "platform-qualification",
    ):
        assert forbidden not in text


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def require_waiting(
    payloads: list[dict[str, object]], *, wait_seconds: float, poll_seconds: float = 30.0
) -> FakeClock:
    clock = FakeClock()
    with (
        patch.object(provenance, "_request_json", side_effect=payloads),
        patch.object(provenance.time, "monotonic", clock.monotonic),
        patch.object(provenance.time, "sleep", clock.sleep),
    ):
        provenance.require_successful_push_ci(
            repository="owner/repo",
            sha="a" * 40,
            workflow=".github/workflows/ci.yml",
            token="secret",
            wait_seconds=wait_seconds,
            poll_seconds=poll_seconds,
        )
    return clock


def test_wait_accepts_in_progress_run_that_then_succeeds():
    clock = require_waiting(
        [
            {"workflow_runs": [run(status="in_progress", conclusion=None)]},
            {"workflow_runs": [run()]},
        ],
        wait_seconds=300.0,
    )
    assert clock.sleeps == [30.0]


def test_wait_rejects_queued_forever_at_deadline_after_bounded_sleeps():
    queued = {"workflow_runs": [run(status="queued", conclusion=None)]}
    clock = FakeClock()
    with (
        patch.object(provenance, "_request_json", return_value=queued) as request,
        patch.object(provenance.time, "monotonic", clock.monotonic),
        patch.object(provenance.time, "sleep", clock.sleep),
    ):
        with pytest.raises(provenance.ProvenanceError, match="timeout") as error:
            provenance.require_successful_push_ci(
                repository="owner/repo",
                sha="a" * 40,
                workflow=".github/workflows/ci.yml",
                token="secret",
                wait_seconds=100.0,
                poll_seconds=30.0,
            )
    assert "no completed successful push CI run matched workflow and exact SHA" in str(
        error.value
    )
    assert clock.sleeps == [30.0, 30.0, 30.0]
    assert request.call_count == 4


def test_wait_rejects_completed_failure_without_sleeping():
    clock = FakeClock()
    with (
        patch.object(
            provenance,
            "_request_json",
            return_value={"workflow_runs": [run(conclusion="failure")]},
        ),
        patch.object(provenance.time, "monotonic", clock.monotonic),
        patch.object(provenance.time, "sleep", clock.sleep),
    ):
        with pytest.raises(provenance.ProvenanceError, match="completed without success"):
            provenance.require_successful_push_ci(
                repository="owner/repo",
                sha="a" * 40,
                workflow=".github/workflows/ci.yml",
                token="secret",
                wait_seconds=3000.0,
            )
    assert clock.sleeps == []


def test_wait_accepts_run_registered_after_first_scan():
    clock = require_waiting(
        [
            {"workflow_runs": [run(head_sha="b" * 40)]},
            {"workflow_runs": [run()]},
        ],
        wait_seconds=300.0,
    )
    assert clock.sleeps == [30.0]


def test_wait_main_defaults_to_bounded_wait_and_rejects_negative(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    assert provenance.DEFAULT_WAIT_SECONDS > 0
    with patch.object(provenance, "require_successful_push_ci") as require_ci:
        assert provenance.main(["--sha", "a" * 40]) == 0
        assert require_ci.call_args.kwargs["wait_seconds"] == provenance.DEFAULT_WAIT_SECONDS
        assert require_ci.call_args.kwargs["poll_seconds"] == provenance.DEFAULT_POLL_SECONDS
        assert provenance.main(["--sha", "a" * 40, "--wait-seconds", "-1"]) == 1
        assert provenance.main(["--sha", "a" * 40, "--poll-seconds", "0"]) == 1
        assert require_ci.call_count == 1
    assert "release CI provenance rejected" in capsys.readouterr().err
