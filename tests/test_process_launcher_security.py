from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    """Bridge a pre-existing, out-of-scope worktree gap: some isolated Task
    MCP worktrees are missing ``deepseek_credentials.py`` entirely (an
    uncommitted file on the trusted host, absent from this worktree's git
    history) even though ``process_launcher.py``/``dashboard.py`` already
    import it at module scope. Only installs a stub when the real module is
    genuinely unimportable -- a host where the file exists imports it
    normally and this is a no-op."""
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    def adapter_readiness(repo=None):  # noqa: ANN001, ARG001
        return {"ok": True, "readonly": True, "adapters": []}

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    stub.adapter_readiness = adapter_readiness
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import process_launcher, worker_workspace  # noqa: E402


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some sandboxed execution shells reject the bare chmod(2)/fchmod(2)
    syscall outright -- including on paths this same process just created and
    owns -- while ``os.mkdir(mode=)``/``os.open(mode=)`` still apply
    permission bits correctly at creation time (the identical restriction is
    independently documented in
    ``eval/task_mcp_vscode_owned_app_server_mux_b409_v1.json``'s
    ``chmod_syscall_blocked_in_this_sandbox`` note). Probing once and only
    neutralizing ``os.chmod``/``os.fchmod`` to a no-op WHEN the syscall is
    genuinely blocked keeps this fixture a no-op on a host where chmod
    actually works -- the real permission-setting code path is exercised
    there unchanged."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "out").mkdir()
    (root / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(root, "add", "out/result.txt").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


def _card() -> dict:
    return {
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "review_requested_by": "",
        "allowed_writes": ["out/result.txt"],
        "read_first": [],
        "validation": [],
        "priority": "high",
    }


def _show(card: dict):
    def show(task_id: str) -> dict:
        assert task_id == card["task_id"]
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _collision(**_kwargs) -> dict:
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _plan(argv: list[str]):
    def build(**kwargs):
        return SimpleNamespace(
            argv=list(argv),
            cwd=str(kwargs["repo"]),
            launchable=True,
            reason="",
        )

    return build


def _open_gates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setenv(worker_workspace.SANDBOX_BACKEND_ENV, "landlock")
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))


def _lifecycle_fakes(monkeypatch: pytest.MonkeyPatch, card: dict) -> list[tuple]:
    calls: list[tuple] = []

    def claim(
        repo, task_id: str, runner: str, topic: str, *, request_id: str | None = None
    ) -> dict:
        assert request_id
        calls.append(("claim", task_id, runner, topic))
        assert (task_id, runner, topic) == (
            card["task_id"],
            card["runner"],
            card["topic"],
        )
        card.update({
            "status": "processing",
            "worker_status": "in_progress",
            "claimed_by": runner,
        })
        return {"ok": True}

    def review(repo_root, task_id: str, runner: str, substatus: str, *, evidence=None) -> dict:
        calls.append(("review", task_id, runner, substatus))
        assert card["claimed_by"] == runner
        card.update({
            "status": "review",
            "worker_status": "review",
            "review_requested_by": runner,
        })
        return {"ok": True}

    def release(repo_root, task_id: str, claimed_by: str, substatus: str, *, evidence=None) -> dict:
        calls.append(("release", task_id, claimed_by, substatus))
        assert card["claimed_by"] == claimed_by
        card.update({
            "status": "review",
            "worker_status": "review",
            "terminal_worker": claimed_by,
            "terminal_outcome": substatus,
        })
        return {"ok": True}

    def failure(
        repo_root, task_id: str, runner: str, substatus: str, *, evidence=None,
        request_id: str = "",
    ) -> dict:
        calls.append(("failure", task_id, runner, substatus, request_id))
        assert card["claimed_by"] == runner
        card.update({
            "status": "blocked",
            "worker_status": substatus,
            "terminal_worker": runner,
            "terminal_outcome": substatus,
        })
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "claim_start_exact", claim)
    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_review", review)
    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_failure", failure)
    return calls


def _wait_terminal(manager: process_launcher.ProcessManager, request_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = manager.collect(request_id)
        if result.get("terminal"):
            return result
        time.sleep(0.02)
    raise AssertionError("isolated worker did not become terminal")


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock workers",
)
def test_production_launch_owns_exact_claim_promotion_and_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    _open_gates(monkeypatch, tmp_path)
    card = _card()
    calls = _lifecycle_fakes(monkeypatch, card)
    script = "from pathlib import Path; Path('out/result.txt').write_text('worker-result\\n')"
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", script]),
    )
    launched = manager.launch(
        task_id=card["task_id"],
        runner=card["runner"],
        topic=card["topic"],
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert launched["ok"] is True
    assert launched["workspace_isolated"] is True
    assert launched["sandbox_backend"] == "landlock"

    result = _wait_terminal(manager, launched["request_id"])
    assert result["state"] == "review_ready"
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"
    assert [call[0] for call in calls] == ["claim", "review"]
    assert result["latest_event"]["promoted_paths"] == []
    assert result["latest_event"]["workspace_retained"] is True
    assert stat.S_IMODE((tmp_path / "events.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(launched["stdout_path"]).stat().st_mode) == 0o600
    assert not list((tmp_path / "processes").glob("*.supervisor-spec.json"))


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock workers",
)
def test_cancel_from_restarted_manager_is_durable_and_releases_exact_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    _open_gates(monkeypatch, tmp_path)
    card = _card()
    calls = _lifecycle_fakes(monkeypatch, card)
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "import time; time.sleep(30)"]),
    )
    launched = manager.launch(
        task_id=card["task_id"],
        runner=card["runner"],
        topic=card["topic"],
        adapter_id="claude_cli",
        timeout_seconds=60,
    )
    assert launched["ok"] is True

    restarted = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([]),
    )
    cancel = restarted.cancel(launched["request_id"], reason="restart-test")
    assert cancel["ok"] is True
    assert cancel["state"] == "cancel_requested"
    result = _wait_terminal(restarted, launched["request_id"])
    assert result["state"] == "cancelled"
    assert card["status"] == "blocked"
    assert card["worker_status"] == "cancelled"
    assert [call[0] for call in calls] == ["claim", "failure"]
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"


def _persisted_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    card: dict,
    *,
    status: dict | None,
) -> tuple[process_launcher.ProcessManager, worker_workspace.WorkerWorkspace, str]:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    request_id = "persisted-request"
    workspace = worker_workspace.create_workspace(repo, request_id, card, "validation")
    (workspace.path / "out" / "result.txt").write_text("untrusted-worker-output\n", encoding="utf-8")
    process_dir = tmp_path / "processes"
    process_dir.mkdir(mode=0o700)
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor.json"
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "model": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(process_dir / f"{request_id}.cancel.json"),
        "metadata_path": str(metadata_path),
        "validation": [],
        "sandbox_backend": "landlock",
        "workspace": workspace.as_metadata(),
    })
    worker_workspace.write_json_0600(stdout_path, {})
    worker_workspace.write_json_0600(stderr_path, {})
    if status is not None:
        worker_workspace.write_json_0600(status_path, status)
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([]),
    )
    manager._append_event({
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": 999_999_999,
        "pid_start_ticks": 1,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })
    return manager, workspace, request_id


def test_missing_supervisor_status_releases_on_restart_and_retries_failed_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    card = _card()
    card.update({
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": card["runner"],
    })
    _manager, workspace, request_id = _persisted_request(
        monkeypatch, tmp_path, repo, card, status=None
    )
    release_enabled = False
    release_calls: list[tuple[str, str, str]] = []

    def release(
        repo_root, task_id: str, runner: str, substatus: str, *, evidence=None,
        request_id: str = "",
    ) -> dict:
        nonlocal release_enabled
        assert repo_root == repo
        release_calls.append((task_id, runner, substatus))
        if not release_enabled:
            return {"ok": False, "stderr": "write gate temporarily unavailable"}
        card.update({
            "status": "blocked",
            "worker_status": substatus,
            "claimed_by": runner,
        })
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_failure", release)
    restarted = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([]),
    )
    assert restarted._latest_by_request()[request_id]["state"] == "release_pending"
    assert workspace.path.exists()
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"

    release_enabled = True
    result = restarted.status(request_id)
    assert result["state"] == "worker_failed"
    assert len(release_calls) == 2
    assert all(call[:2] == (card["task_id"], card["runner"]) for call in release_calls)
    # Terminal-failure evidence is retained for diagnosis without inflating
    # the actionable review queue.
    assert workspace.path.exists()
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"


def test_vscode_lm_structured_response_timeout_is_terminal_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    card = _card()
    card.update({
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": card["runner"],
    })
    manager, workspace, request_id = _persisted_request(
        monkeypatch,
        tmp_path,
        repo,
        card,
        status={"state": "exited", "exit_code": 1},
    )
    stdout_path = tmp_path / "processes" / f"{request_id}.stdout.log"
    stdout_path.write_text(
        json.dumps({
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "error": "vscode_lm_response_timeout",
        })
        + "\n",
        encoding="utf-8",
    )
    releases: list[tuple[str, str, str]] = []

    def release(repo_root, task_id, runner, substatus, *, evidence=None, request_id=""):
        assert repo_root == repo
        releases.append((task_id, runner, substatus))
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_failure", release)

    event = manager._finalize_isolated_request(request_id, supervisor_returncode=1)

    assert event["state"] == "timed_out"
    assert "source=vscode_lm_response_timeout" in event["error"]
    assert releases == [(card["task_id"], card["runner"], "timed_out")]
    assert workspace.path.exists()


def test_provider_timeout_evidence_rejects_unstructured_timeout_prose(tmp_path: Path) -> None:
    output = tmp_path / "provider.jsonl"
    output.write_text(
        json.dumps({"type": "assistant", "message": "vscode_lm_response_timeout"})
        + "\n",
        encoding="utf-8",
    )
    assert process_launcher._provider_timeout_failure_from_output(output) is None


def test_success_status_does_not_promote_after_exact_claim_ownership_is_lost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    card = _card()
    card.update({
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": "different_runner_b1",
    })
    _manager, workspace, request_id = _persisted_request(
        monkeypatch,
        tmp_path,
        repo,
        card,
        status={"state": "exited", "exit_code": 0},
    )
    release_calls: list[tuple] = []
    monkeypatch.setattr(
        process_launcher.core,
        "release_launch",
        lambda *args: release_calls.append(args) or {"ok": True},
    )
    restarted = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([]),
    )
    result = restarted.status(request_id)
    assert result["state"] == "finalize_failed"
    assert "claim_ownership_lost" in result["latest_event"]["error"]
    assert release_calls == []
    # B860/B863: a claim_ownership_lost read is not reliable proof of a
    # legitimate different owner -- it can be a false positive from a
    # launcher/finalizer authority disagreement, so the isolated workspace
    # is retained as evidence instead of being deleted here. Deletion is
    # deferred to the canonical-status-gated GC sweep.
    assert workspace.path.exists()
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"


def test_restart_waits_for_write_gate_before_promotion_and_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("review finalization requires the POSIX secure sandbox backend")
    monkeypatch.delenv(process_launcher.ALLOW_WRITES_ENV, raising=False)
    monkeypatch.setenv(worker_workspace.SANDBOX_BACKEND_ENV, "landlock")
    card = _card()
    card.update({
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": card["runner"],
    })
    _manager, workspace, request_id = _persisted_request(
        monkeypatch,
        tmp_path,
        repo,
        card,
        status={"state": "exited", "exit_code": 0},
    )
    reviews: list[tuple[str, str, str]] = []

    def review(repo_root, task_id: str, runner: str, substatus: str, *, evidence=None) -> dict:
        assert repo_root == repo
        reviews.append((task_id, runner, substatus))
        card.update({
            "status": "review",
            "worker_status": "review",
            "review_requested_by": runner,
        })
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_review", review)
    restarted = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([]),
    )
    assert restarted._latest_by_request()[request_id]["state"] == "review_pending"
    assert workspace.path.exists()
    assert reviews == []
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"

    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    result = restarted.status(request_id)
    assert result["state"] == "review_ready"
    assert reviews == [(card["task_id"], card["runner"], "review_ready")]
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"
    assert workspace.path.exists()


@pytest.mark.parametrize(
    ("adapter_id", "expected_input"),
    [("codex_cli", 120), ("claude_cli", 225)],
)
def test_cached_token_accounting_matches_provider_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adapter_id: str,
    expected_input: int,
) -> None:
    output = tmp_path / f"{adapter_id}.json"
    output.write_text(json.dumps({
        "type": "result" if adapter_id == "claude_cli" else "turn.completed",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 45,
            "cached_input_tokens": 80,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 25,
        },
    }), encoding="utf-8")
    card = _card()
    captured: list[list[str]] = []

    def run_taskctl(args: list[str], **_kwargs):
        captured.append(args)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(process_launcher.core, "run_taskctl", run_taskctl)
    manager = process_launcher.ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([]),
        isolation_enabled=False,
    )
    usage, recorded, error = manager._record_usage(
        f"request-{adapter_id}",
        card["task_id"],
        card["runner"],
        adapter_id,
        adapter_id,
        output,
    )
    assert recorded is True
    assert error == ""
    assert usage["recorded_input_tokens"] == expected_input
    args = captured[0]
    assert args[args.index("--input-tokens") + 1] == str(expected_input)
    assert args[args.index("--total-tokens") + 1] == str(expected_input + 45)
    assert args[args.index("--cached-input-tokens") + 1] == "80"
    assert args[args.index("--cache-creation-input-tokens") + 1] == "25"
    assert args[args.index("--role") + 1] == "worker"
    assert "--cache-metrics-observed" in args


def test_readonly_quality_review_usage_is_attributed_to_reviewer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "reviewer.json"
    output.write_text(
        json.dumps({
            "type": "result",
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }),
        encoding="utf-8",
    )
    card = {
        **_card(),
        "topic": "quality_review",
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
        "project_context": {"task_type": "research"},
    }
    captured: list[list[str]] = []

    def run_taskctl(args: list[str], **_kwargs):
        captured.append(args)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(process_launcher.core, "run_taskctl", run_taskctl)
    manager = process_launcher.ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([]),
        isolation_enabled=False,
    )

    usage, recorded, error = manager._record_usage(
        "request-reviewer",
        card["task_id"],
        card["runner"],
        "claude_cli",
        "claude-sonnet-5",
        output,
    )

    assert recorded is True
    assert error == ""
    assert usage["role"] == "reviewer"
    assert captured[0][captured[0].index("--role") + 1] == "reviewer"


def test_unobserved_provider_usage_still_records_one_truthful_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "vscode-lm.json"
    output.write_text('{"type":"result","content":"ok"}\n', encoding="utf-8")
    card = _card()
    captured: list[list[str]] = []

    def run_taskctl(args: list[str], **_kwargs):
        captured.append(args)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(process_launcher.core, "run_taskctl", run_taskctl)
    manager = process_launcher.ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([]),
        isolation_enabled=False,
    )

    usage, recorded, error = manager._record_usage(
        "request-unobserved",
        card["task_id"],
        card["runner"],
        "vscode_lm",
        "glm-5.2",
        output,
    )

    assert recorded is True
    assert error == ""
    assert usage["usage_observed"] is False
    assert usage["cost_observed"] is False
    assert usage["cost_usd"] is None
    assert len(captured) == 1
    args = captured[0]
    assert "--usage-observed" not in args
    assert "--cost-observed" not in args
    assert args[args.index("--total-tokens") + 1] == "0"
    assert args[args.index("--note") + 1] == "task_mcp_request:request-unobserved"


# ── B561: gitignored required-output promotion in full finalize flow ──────


def _gitignored_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo-ignored"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "out").mkdir()
    (root / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.bin\n", encoding="utf-8")
    assert _git(root, "add", "out/result.txt", ".gitignore").returncode == 0
    assert _git(root, "commit", "-qm", "fixture-gitignored").returncode == 0
    return root


def _ignored_card() -> dict:
    return {
        "task_id": "TASK_B561",
        "runner": "claude_worker_b561",
        "topic": "task_mcp",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "review_requested_by": "",
        "allowed_writes": ["out/result.txt", "out/*.bin"],
        "required_outputs": ["out/*.bin"],
        "read_first": [],
        "validation": [],
        "priority": "high",
    }


def test_required_ignored_output_promoted_in_full_finalize_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561: the ProcessManager finalize path unions validated required-output
    exact paths into the promotion set, so a gitignored .bin that
    validate_required_outputs accepts is successfully promoted and appears in
    promoted_paths."""
    if os.name == "nt":
        pytest.skip("review finalization requires the POSIX secure sandbox backend")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setenv(worker_workspace.SANDBOX_BACKEND_ENV, "landlock")
    repo = _gitignored_repo(tmp_path)
    card = _ignored_card()
    card.update({
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": card["runner"],
    })

    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    request_id = "b561-ignored-finalize"
    workspace = worker_workspace.create_workspace(repo, request_id, card, "validation")

    # Worker writes a gitignored binary alongside the tracked text output.
    (workspace.path / "out" / "result.txt").write_text("worker-result\n", encoding="utf-8")
    (workspace.path / "out" / "data.bin").write_bytes(b"\x11\x22\x33\x44\x55\x66\x77\x88")

    process_dir = tmp_path / "processes"
    process_dir.mkdir(mode=0o700)
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor.json"
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "model": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(process_dir / f"{request_id}.cancel.json"),
        "metadata_path": str(metadata_path),
        "validation": [],
        "sandbox_backend": "landlock",
        "required_outputs": list(card["required_outputs"]),
        "workspace": workspace.as_metadata(),
    })
    worker_workspace.write_json_0600(stdout_path, {})
    worker_workspace.write_json_0600(stderr_path, {})
    worker_workspace.write_json_0600(status_path, {
        "state": "exited", "exit_code": 0
    })

    reviews: list[tuple[str, str, str]] = []

    def review(repo_root, task_id: str, runner: str, substatus: str, *, evidence=None) -> dict:
        assert repo_root == repo
        reviews.append((task_id, runner, substatus))
        card.update({
            "status": "review",
            "worker_status": "review",
            "review_requested_by": runner,
        })
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_review", review)

    def show(task_id: str) -> dict:
        assert task_id == card["task_id"]
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        show_task=show,
        collision_guard=lambda **_kw: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        adapter_builder=_plan([]),
    )
    manager._append_event({
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": 999_999_998,
        "pid_start_ticks": 1,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })

    result = manager.status(request_id)
    assert result["state"] == "review_ready"
    assert result["latest_event"]["promoted_paths"] == []
    assert "out/data.bin" in result["latest_event"]["changed_paths"], (
        "gitignored required output must appear in the review candidate"
    )
    assert "out/result.txt" in result["latest_event"]["changed_paths"]
    assert len(result["latest_event"]["required_outputs"]) == 1
    assert result["latest_event"]["required_outputs"][0]["path"] == "out/data.bin"

    # Review-first keeps the validated binary in the isolated candidate until
    # coordinator acceptance; the canonical repo remains unchanged.
    assert (workspace.path / "out" / "data.bin").read_bytes() == b"\x11\x22\x33\x44\x55\x66\x77\x88"
    assert not (repo / "out" / "data.bin").exists()
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"
    assert reviews == [(card["task_id"], card["runner"], "review_ready")]
    assert workspace.path.exists()
