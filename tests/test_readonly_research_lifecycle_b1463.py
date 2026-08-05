from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from aiworkhub import process_launcher, worker_workspace


REQUEST_ID = "a" * 32
TASK_ID = "DEEPSEEK_READONLY_RESEARCH_B1463_V1"
RUNNER = "deepseek_readonly_research_b1463_v1"
TOPIC = "research_lifecycle"


def _init_git(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "AIWorkHub Tests"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=path, check=True)


def _research_card(workspace: worker_workspace.WorkerWorkspace) -> dict:
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "review",
        "worker_status": "review",
        "claimed_by": RUNNER,
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
        "validation": [],
        "project_context": {"task_type": "research"},
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "changed_paths": [],
                "changed_path_hashes": {},
                "required_outputs": [],
                "validation": [],
                "worker_mcp_gate": {"gated": False, "satisfied": True},
                "workspace": workspace.as_metadata(),
                "request_identity": {
                    "request_id": REQUEST_ID,
                    "task_id": TASK_ID,
                    "runner": RUNNER,
                    "topic": TOPIC,
                },
            },
        },
    }


def _manager(repo: Path, process_dir: Path, card: dict) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=process_dir.parent / "events.jsonl",
        process_dir=process_dir,
        show_task=lambda _task_id: {
            "returncode": 0,
            "stdout": json.dumps(card),
            "stderr": "",
        },
        collision_guard=lambda **_kwargs: {
            "returncode": 0,
            "stdout": '{"collision_free":true}',
            "stderr": "",
        },
        adapter_builder=lambda **_kwargs: SimpleNamespace(
            argv=[], cwd=str(repo), launchable=True, reason=""
        ),
        isolation_enabled=True,
    )


def _result_log(path: Path, text: str = "independent evidence trace") -> dict:
    payload = (
        "PROJECT_CONTEXT_RECEIPT: "
        + json.dumps({"schema_id": "aiworkhub.task_mcp.worker_context_receipt.v1"})
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": text,
                "changed_paths": [],
            }
        )
        + "\n"
    )
    path.write_text(payload, encoding="utf-8")
    return process_launcher._readonly_research_result_evidence(path)


def test_research_result_requires_supported_non_receipt_output(tmp_path: Path) -> None:
    output = tmp_path / "research.stdout.log"
    output.write_text(
        'PROJECT_CONTEXT_RECEIPT: {"acknowledged":true}\n', encoding="utf-8"
    )
    assert process_launcher._readonly_research_result_evidence(output) == {
        "schema_id": "aiworkhub.readonly_research_result.v1",
        "meaningful_output": False,
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "result_event_count": 0,
        "result_chars": 0,
        "reason": "research_result_missing",
    }

    evidence = _result_log(output, "ქართული კვლევის შედეგი")
    assert evidence["meaningful_output"] is True
    assert evidence["result_event_count"] == 1
    assert evidence["result_chars"] == len("ქართული კვლევის შედეგი")
    assert evidence["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_no_write_no_output_contract_is_readonly_for_code_inspection(
    tmp_path: Path,
) -> None:
    workspace = worker_workspace.WorkerWorkspace(
        request_id=REQUEST_ID,
        repo=tmp_path,
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )

    assert process_launcher._metadata_is_readonly_research(
        {
            "project_context": {"task_context_policy": {"task_type": "code"}},
            "read_only": True,
            "required_outputs": [],
        },
        workspace,
    )
    assert process_launcher._card_is_readonly_research(
        {
            "project_context": {"task_type": "code"},
            "read_only": True,
            "allowed_writes": [],
            "required_outputs": [],
        }
    )


def test_successful_readonly_research_reaches_review_ready(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "worktrees"
    workspace_path = root / REQUEST_ID / "worktree"
    home = root / REQUEST_ID / "home"
    _init_git(workspace_path)
    home.mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=REQUEST_ID,
        repo=repo,
        path=workspace_path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    card = _research_card(workspace)
    card.update(status="processing", worker_status="claimed")
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    manager = _manager(repo, process_dir, card)
    stdout_path = process_dir / f"{REQUEST_ID}.stdout.log"
    stderr_path = process_dir / f"{REQUEST_ID}.stderr.log"
    status_path = process_dir / f"{REQUEST_ID}.supervisor.json"
    metadata_path = process_dir / f"{REQUEST_ID}.request.json"
    _result_log(stdout_path)
    stderr_path.write_text("", encoding="utf-8")
    worker_workspace.write_json_0600(status_path, {"state": "exited", "exit_code": 0})
    worker_workspace.write_json_0600(
        metadata_path,
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "supervisor_status_path": str(status_path),
            "cancel_path": str(process_dir / f"{REQUEST_ID}.cancel.json"),
            "project_context": {
                "task_context_policy": {"task_type": "research"},
                "bundle_sha256": "",
            },
            "required_outputs": [],
            "read_only": True,
            "validation": [],
            "residual_contract_manifest": [],
            "workspace": workspace.as_metadata(),
        },
    )
    captured: list[dict] = []
    monkeypatch.setattr(
        manager,
        "_review_terminal_exact",
        lambda _metadata, substatus, **kwargs: captured.append(
            {"substatus": substatus, **kwargs}
        )
        or {"ok": True},
    )
    monkeypatch.setattr(manager, "_record_usage", lambda *_a, **_k: ({}, False, ""))
    manager._append_event(
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": "deepseek_vscode_lm",
            "state": "running",
            "pid": 999_999_999,
            "pid_start_ticks": 1,
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    )

    event = manager._finalize_isolated_request(REQUEST_ID, supervisor_returncode=0)

    assert event is not None and event["state"] == "review_ready"
    assert event["changed_paths"] == []
    assert event["quality_gate"]["applicable"] is False
    assert captured[0]["substatus"] == "review_ready"
    evidence = captured[0]["evidence"]
    assert evidence["changed_path_hashes"] == {}
    assert evidence["research_result"]["meaningful_output"] is True


def test_accept_revalidates_exact_readonly_research_stdout(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    root = tmp_path / "worktrees"
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(root))
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_path = root / REQUEST_ID / "worktree"
    home = root / REQUEST_ID / "home"
    _init_git(workspace_path)
    home.mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=REQUEST_ID,
        repo=repo,
        path=workspace_path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    card = _research_card(workspace)
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{REQUEST_ID}.stdout.log"
    result_evidence = _result_log(stdout_path)
    card["terminal_review"]["evidence"]["research_result"] = result_evidence
    manager = _manager(repo, process_dir, card)
    manager._append_event(
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": "deepseek_vscode_lm",
            "state": "review_ready",
            "stdout_path": str(stdout_path),
        }
    )
    accepted: list[dict] = []
    monkeypatch.setattr(
        process_launcher.task_engine,
        "accept_review",
        lambda *_args, **kwargs: accepted.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(process_launcher, "cleanup_workspace", lambda *_a, **_k: None)

    result = manager.accept_review(REQUEST_ID, TASK_ID)

    assert result["ok"] is True
    assert result["promoted_paths"] == []
    assert result["research_result"] == result_evidence
    assert accepted[0]["evidence"]["quality_gate"]["applicable"] is False

    stdout_path.write_text(
        stdout_path.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8"
    )
    manager._append_event(
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": "deepseek_vscode_lm",
            "state": "review_ready",
            "stdout_path": str(stdout_path),
        }
    )
    mismatch = manager.accept_review(REQUEST_ID, TASK_ID)
    assert mismatch["ok"] is False
    assert mismatch["error"] == "revalidation_failed:research_result_evidence_mismatch"


def test_accepts_authenticated_readonly_quality_review_without_changed_hashes(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    root = tmp_path / "worktrees"
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(root))
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_path = root / REQUEST_ID / "worktree"
    home = root / REQUEST_ID / "home"
    _init_git(workspace_path)
    home.mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=REQUEST_ID,
        repo=repo,
        path=workspace_path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    card = _research_card(workspace)
    card["topic"] = "quality_review"
    evidence = card["terminal_review"]["evidence"]
    evidence.pop("changed_path_hashes")
    evidence["request_identity"]["topic"] = "quality_review"
    evidence["quality_review"] = {
        "target_request_id": "b" * 32,
        "target_task_id": "TARGET_TASK",
    }
    receipt = {
        "schema_id": "aiworkhub.quality_reviewer_receipt.v1",
        "reviewer": {"request_id": REQUEST_ID, "task_id": TASK_ID},
        "report": {
            "lens": "correctness",
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
    }
    evidence["quality_review_receipt"] = receipt
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    metadata_path = process_dir / f"{REQUEST_ID}.request.json"
    worker_workspace.write_json_0600(
        metadata_path,
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": "quality_review",
            "adapter_id": "codex_cli",
            "quality_review": evidence["quality_review"],
            "workspace": workspace.as_metadata(),
        },
    )
    manager = _manager(repo, process_dir, card)
    manager._append_event(
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": "quality_review",
            "adapter_id": "codex_cli",
            "state": "review_ready",
            "metadata_path": str(metadata_path),
        }
    )
    accepted: list[dict] = []
    monkeypatch.setattr(
        process_launcher,
        "_verified_quality_review_receipt",
        lambda _metadata, _workspace, _request_id: receipt,
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "accept_review",
        lambda *_args, **kwargs: accepted.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(process_launcher, "cleanup_workspace", lambda *_a, **_k: None)

    result = manager.accept_review(REQUEST_ID, TASK_ID)

    assert result["ok"] is True
    assert result["promoted_paths"] == []
    assert result["quality_review_receipt"] == receipt
    final_evidence = accepted[0]["evidence"]
    assert final_evidence["promoted_paths"] == []
    assert final_evidence["quality_gate"]["reason"] == (
        "quality_review_no_repository_change"
    )


def test_readonly_quality_review_requires_canonical_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    root = tmp_path / "worktrees"
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(root))
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_path = root / REQUEST_ID / "worktree"
    home = root / REQUEST_ID / "home"
    _init_git(workspace_path)
    home.mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=REQUEST_ID,
        repo=repo,
        path=workspace_path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    card = _research_card(workspace)
    card["topic"] = "quality_review"
    card["terminal_review"]["evidence"]["request_identity"]["topic"] = (
        "quality_review"
    )
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    manager = _manager(repo, process_dir, card)
    manager._append_event(
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": "quality_review",
            "adapter_id": "codex_cli",
            "state": "review_ready",
        }
    )

    result = manager.accept_review(REQUEST_ID, TASK_ID)

    assert result["ok"] is False
    assert result["error"] == "revalidation_failed:quality_review_metadata_missing"
