from __future__ import annotations

from pathlib import Path

from aiworkhub.process_launcher import ProcessManager


def test_collect_rehydrates_request_truth_after_bounded_gc_event(tmp_path: Path) -> None:
    task_id = "TERMINAL_COLLECT_TRUTH"
    request_id = "a" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    stdout_path.write_bytes(b"exact provider error\n")
    stderr_path.write_bytes(b"exact stderr\n")
    metadata_path.write_text("{}", encoding="utf-8")
    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "blocked",
            "worker_status": "worker_failed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact event-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 123,
            "pid_start_ticks": 456,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
        }
    )
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "worker_failed",
            "exit_code": 1,
            "error": "vscode_lm_edit_response_stale_hash:src/app.py",
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
        }
    )
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)

    assert result["state"] == "worker_failed"
    assert result["adapter_id"] == "deepseek_vscode_lm"
    assert result["model"] == "deepseek-v4-pro"
    assert result["exit_code"] == 1
    assert result["latest_event"]["error"] == (
        "vscode_lm_edit_response_stale_hash:src/app.py"
    )
    assert result["latest_event"]["metadata_path"] == str(metadata_path)
    assert result["stdout_tail"] == "exact provider error\n"
    assert result["stderr_tail"] == "exact stderr\n"
