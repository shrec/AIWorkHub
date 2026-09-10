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
