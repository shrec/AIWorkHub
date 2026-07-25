from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import core


_SRC = Path(__file__).resolve().parents[1] / "src"


def _probe_callback_cwd(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    child_env.update(env)
    child_env["PYTHONPATH"] = os.pathsep.join(
        [str(_SRC), child_env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from aiworkhub import callback_bridge; print(callback_bridge.CALLBACK_CWD)",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=child_env,
    )


def test_core_repo_root_prefers_canonical_root_and_accepts_matching_legacy_symlink(
    tmp_path, monkeypatch
):
    real = tmp_path / "real_repo"
    real.mkdir()
    link = tmp_path / "repo_link"
    link.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(real))
    monkeypatch.setenv("AIWORKHUB_REPO", str(link))

    assert core.repo_root() == real.resolve()


def test_core_repo_root_refuses_mismatched_legacy_binding(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical_repo"
    legacy = tmp_path / "stale_legacy_repo"
    canonical.mkdir()
    legacy.mkdir()

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(canonical))
    monkeypatch.setenv("AIWORKHUB_REPO", str(legacy))

    with pytest.raises(RuntimeError, match="repo_root_env_mismatch"):
        core.repo_root()


def test_callback_bridge_uses_same_canonical_repo_binding(tmp_path, monkeypatch):
    real = tmp_path / "real_repo"
    real.mkdir()
    link = tmp_path / "repo_link"
    link.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(real))
    monkeypatch.setenv("AIWORKHUB_REPO", str(link))

    # CALLBACK_CWD is computed at import time. Probe it in a fresh process so
    # this test never reloads the suite's canonical module and invalidates
    # exception/type identities held by already-collected tests.
    result = _probe_callback_cwd({
        "AIWORKHUB_REPO_ROOT": str(real),
        "AIWORKHUB_REPO": str(link),
    })
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(real.resolve())


def test_callback_bridge_refuses_mismatched_legacy_binding(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical_repo"
    legacy = tmp_path / "stale_legacy_repo"
    canonical.mkdir()
    legacy.mkdir()

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(canonical))
    monkeypatch.setenv("AIWORKHUB_REPO", str(legacy))

    result = _probe_callback_cwd({
        "AIWORKHUB_REPO_ROOT": str(canonical),
        "AIWORKHUB_REPO": str(legacy),
    })
    assert result.returncode != 0
    assert "repo_root_env_mismatch" in result.stderr
