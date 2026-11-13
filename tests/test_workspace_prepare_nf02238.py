"""NF-2026-02238: the repo ``workspace_prepare`` hook."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from aiworkhub import workspace_prepare as wp


def _repo(tmp_path: Path, document: object | None) -> Path:
    repo = tmp_path / "repo"
    config = repo / ".aiworkhub" / "config"
    config.mkdir(parents=True)
    if document is not None:
        text = document if isinstance(document, str) else json.dumps(document)
        (config / "project.json").write_text(text, encoding="utf-8")
    return repo


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return worktree


def test_load_hook_absent_config_or_key_is_not_configured(tmp_path):
    assert wp.load_hook(_repo(tmp_path, None)) is None
    assert wp.load_hook(_repo(tmp_path / "b", {"other": 1})) is None
    assert wp.load_hook(_repo(tmp_path / "c", {"workspace_prepare": None})) is None


def test_load_hook_returns_the_argv(tmp_path):
    repo = _repo(tmp_path, {"workspace_prepare": ["python", "prep.py", "{worktree}"]})
    assert wp.load_hook(repo) == ["python", "prep.py", "{worktree}"]


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ("{broken", "config_not_json"),
        ([1, 2], "config_not_an_object"),
        ({"workspace_prepare": "make prep"}, "not_an_argv_list"),
        ({"workspace_prepare": []}, "argv_length"),
        ({"workspace_prepare": ["x"] * (wp.MAX_ARGV + 1)}, "argv_length"),
        ({"workspace_prepare": ["x", 3]}, "argument_not_a_string"),
        ({"workspace_prepare": [" "]}, "empty_executable"),
        ({"workspace_prepare": ["x", "a\x00b"]}, "nul_character"),
        ({"workspace_prepare": ["x", "a" * (wp.MAX_ARG_CHARS + 1)]}, "argument_too_long"),
    ],
)
def test_load_hook_refuses_a_bad_config(tmp_path, document, reason):
    with pytest.raises(wp.WorkspacePrepareError) as info:
        wp.load_hook(_repo(tmp_path, document))
    assert str(info.value).startswith(f"workspace_prepare_invalid:{reason}")


def test_load_hook_refuses_an_oversized_config(tmp_path):
    repo = _repo(tmp_path, None)
    (repo / ".aiworkhub" / "config" / "project.json").write_bytes(
        b" " * (wp.MAX_CONFIG_BYTES + 1)
    )
    with pytest.raises(wp.WorkspacePrepareError, match="config_too_large"):
        wp.load_hook(repo)


def test_substitute_replaces_every_placeholder(tmp_path):
    root = str(tmp_path)
    assert wp.substitute(["a", "{worktree}/x", "--r={worktree}"], tmp_path) == [
        "a",
        f"{root}/x",
        f"--r={root}",
    ]


def test_hook_env_is_an_allowlist_without_credentials(tmp_path):
    env = wp.hook_env(
        {
            "PATH": "/bin",
            "HOME": "/home/u",
            "ANTHROPIC_API_KEY": "secret",
            "GITHUB_TOKEN": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY": "secret",
        },
        worktree=tmp_path,
    )
    assert env == {"PATH": "/bin", "HOME": "/home/u", "AIWORKHUB_WORKTREE": str(tmp_path)}


def _probe_argv(code: str) -> list[str]:
    return [sys.executable, "-c", code, "{worktree}"]


def test_run_hook_runs_in_the_worktree_with_substitution(tmp_path):
    worktree = _worktree(tmp_path)
    code = (
        "import os, sys\n"
        "print('cwd=' + os.path.realpath(os.getcwd()))\n"
        "print('arg=' + os.path.realpath(sys.argv[1]))\n"
        "print('key=' + str(os.environ.get('ANTHROPIC_API_KEY')))\n"
    )
    env = wp.hook_env({**os.environ, "ANTHROPIC_API_KEY": "secret"}, worktree=worktree)
    receipt = wp.run_hook(wp.substitute(_probe_argv(code), worktree), worktree, env=env)
    assert receipt["status"] == "succeeded", receipt
    assert receipt["returncode"] == 0
    lines = receipt["output_tail"].splitlines()
    real = os.path.realpath(worktree)
    assert f"cwd={real}" in lines
    assert f"arg={real}" in lines
    assert "key=None" in lines
    assert receipt["output_bytes"] > 0
    assert receipt["schema_id"] == wp.RECEIPT_SCHEMA_ID


def test_run_hook_keeps_only_a_bounded_tail(tmp_path):
    worktree = _worktree(tmp_path)
    code = "import sys\nsys.stdout.write('x' * 20000 + 'END')\n"
    receipt = wp.run_hook(_probe_argv(code), worktree)
    assert receipt["status"] == "succeeded"
    assert receipt["output_bytes"] >= 20003
    assert len(receipt["output_tail"]) <= wp.TAIL_BYTES
    assert receipt["output_tail"].endswith("END")


def test_run_hook_non_zero_is_failed(tmp_path):
    worktree = _worktree(tmp_path)
    code = "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n"
    receipt = wp.run_hook(_probe_argv(code), worktree)
    assert receipt["status"] == "failed"
    assert receipt["returncode"] == 3
    assert "boom" in receipt["output_tail"]


def test_run_hook_timeout_kills_the_process(tmp_path):
    worktree = _worktree(tmp_path)
    receipt = wp.run_hook(
        _probe_argv("import time\ntime.sleep(60)\n"), worktree, timeout=0.5
    )
    assert receipt["status"] == "timed_out"
    assert receipt["duration_ms"] < 50_000


def test_run_hook_spawn_failure_is_recorded(tmp_path):
    worktree = _worktree(tmp_path)
    receipt = wp.run_hook([str(tmp_path / "no-such-binary")], worktree)
    assert receipt["status"] == "spawn_failed"
    assert receipt["returncode"] is None
    assert receipt["output_tail"]


def test_prepare_once_not_configured(tmp_path):
    receipt = wp.prepare_once(
        _repo(tmp_path, None), _worktree(tmp_path), state_dir=tmp_path / "state"
    )
    assert receipt["status"] == "not_configured"


def test_prepare_once_runs_once_per_worktree(tmp_path):
    repo = _repo(tmp_path, {"workspace_prepare": ["prep", "{worktree}"]})
    worktree = _worktree(tmp_path)
    calls = []

    def run(argv, cwd):
        calls.append((argv, cwd))
        return {"schema_id": wp.RECEIPT_SCHEMA_ID, "status": "succeeded", "returncode": 0}

    first = wp.prepare_once(repo, worktree, state_dir=tmp_path / "state", run=run)
    second = wp.prepare_once(repo, worktree, state_dir=tmp_path / "state", run=run)
    assert calls == [(["prep", str(worktree)], worktree)]
    assert first["status"] == "succeeded"
    assert second["reused"] is True
    other = tmp_path / "other"
    other.mkdir()
    wp.prepare_once(repo, other, state_dir=tmp_path / "state", run=run)
    assert len(calls) == 2


def test_prepare_once_non_zero_blocks_with_a_typed_error_and_records_the_tail(tmp_path):
    code = "import sys\nprint('prep failed here')\nsys.exit(4)\n"
    repo = _repo(tmp_path, {"workspace_prepare": [sys.executable, "-c", code]})
    worktree = _worktree(tmp_path)
    state = tmp_path / "state"
    with pytest.raises(wp.WorkspacePrepareError) as info:
        wp.prepare_once(repo, worktree, state_dir=state)
    message = str(info.value)
    assert message.startswith("workspace_prepare_failed:exit=4:")
    assert "prep failed here" in message
    assert info.value.receipt["returncode"] == 4
    recorded = json.loads(wp.receipt_path(state, worktree).read_text(encoding="utf-8"))
    assert recorded["status"] == "failed"
    assert "prep failed here" in recorded["output_tail"]
    # A failed receipt is never reused as success: the hook runs again.
    with pytest.raises(wp.WorkspacePrepareError):
        wp.prepare_once(repo, worktree, state_dir=state)


def _succeeded(argv, cwd):
    return {"schema_id": wp.RECEIPT_SCHEMA_ID, "status": "succeeded", "returncode": 0}


def test_prepare_once_receipt_write_oserror_is_a_typed_refusal(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"workspace_prepare": ["prep", "{worktree}"]})
    worktree = _worktree(tmp_path)

    def failing_replace(src, dst):
        raise PermissionError(13, "denied", os.fspath(dst))

    monkeypatch.setattr(wp.os, "replace", failing_replace)
    with pytest.raises(wp.WorkspacePrepareError) as info:
        wp.prepare_once(repo, worktree, state_dir=tmp_path / "state", run=_succeeded)
    assert str(info.value) == "workspace_prepare_failed:PermissionError"
    assert info.value.reason == "failed"
    assert info.value.receipt["status"] == "succeeded"
    # The temp file is cleaned up and no receipt is left to be reused.
    assert list((tmp_path / "state").iterdir()) == []


def test_prepare_once_state_dir_oserror_is_a_typed_refusal(tmp_path):
    repo = _repo(tmp_path, {"workspace_prepare": ["prep", "{worktree}"]})
    worktree = _worktree(tmp_path)
    blocker = tmp_path / "state"
    blocker.write_text("a file where the state directory should be", encoding="utf-8")
    with pytest.raises(wp.WorkspacePrepareError) as info:
        wp.prepare_once(repo, worktree, state_dir=blocker / "nested", run=_succeeded)
    assert str(info.value).startswith("workspace_prepare_failed:")
    assert "Error" in str(info.value)


def test_prepare_once_temp_sink_oserror_is_a_typed_refusal(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"workspace_prepare": [sys.executable, "-c", "pass"]})
    worktree = _worktree(tmp_path)

    def no_temp(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(wp.tempfile, "TemporaryFile", no_temp)
    with pytest.raises(wp.WorkspacePrepareError) as info:
        wp.prepare_once(repo, worktree, state_dir=tmp_path / "state")
    assert str(info.value) == "workspace_prepare_failed:OSError"
    assert info.value.receipt is None
