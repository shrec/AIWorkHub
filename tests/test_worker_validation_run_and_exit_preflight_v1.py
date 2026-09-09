"""Bounded worker validation execution and the exit-contract rehearsal.

Both surfaces exist because of one measurement pass (2026-09-08), and the tests
below pin the exact properties that measurement demanded.

Validation output was the single largest thing in a worker's context: pooled
over 659 parseable worker runs (claude + codex, 22,204 tool calls, 194.7 MB of
tool-result bytes) VALIDATION was 42.7% of every byte on 16.1% of calls.  On
the Codex lane alone it was 82.0 MB, of which 170 single calls carried more
than 100 KB each (74.9 MB) and one pytest call returned 988,306 bytes; per-run
p90 was 666 KB.  34% of validation commands exited non-zero, and 550 IDENTICAL
commands were re-run inside one run (14.7 MB) across 210 of 478 runs.

Separately, 506 attempts ended ``validation_failed`` on contract checks the
worker could not see (172 required_aiworkhub_mcp_call_missing, 161
required_output_unchanged, 78 residual_contract_file_unchanged, 72
required_output_mismatch, 20 required_output_zero_bytes, 3
required_output_not_allowed); 334 of them had an empty ``evidence.validation``
and, of the 172 gate failures, 165 had every declared validation row rc=0.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_adapters  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_memo() -> None:
    """The memo and the one-shot preflight arming are process-local."""
    w._validation_memo_reset()
    yield
    w._validation_memo_reset()


def _mute_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    if hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    (path / "tests").mkdir(parents=True)
    return path


def _packet(
    tmp_path: Path,
    *,
    validation: list[str],
    required_outputs: list[str] | None = None,
    allowed_writes: list[str] | None = None,
    allow_unchanged: list[str] | None = None,
    workspace_baseline: dict[str, str] | None = None,
    parent_baseline: dict[str, str] | None = None,
    residual: list[dict[str, object]] | None = None,
) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_id": worker_workspace.WORKER_CONTRACT_PACKET_SCHEMA,
        "request_id": "req-validation",
        "allowed_writes": allowed_writes or [],
        "required_outputs": required_outputs or [],
        "allow_empty_required_outputs": [],
        "allow_unchanged_required_outputs": allow_unchanged or [],
        "validation": validation,
        "validation_roles": [],
        "read_only": False,
        "parent_baseline": parent_baseline or {},
        "workspace_baseline": workspace_baseline or {},
        "inherited_rework_paths": [],
        "residual_contract_manifest": residual or [],
    }
    path = worker_workspace.worker_contract_packet_path(home)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ctx(tmp_path: Path, packet_path: Path, worktree: Path) -> w.WorkerToolContext:
    return w.WorkerToolContext(
        task_id="TASK_VALIDATION",
        runner="claude_worker",
        topic="task_mcp",
        request_id="req-validation",
        repo=worktree,
        authority_repo=tmp_path,
        source_graph_targets=(),
        session_topic="task_mcp",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        contract_packet_path=packet_path,
    )


# ---------------------------------------------------------------------------
# The bounded digest: the log lands on disk, the model gets the record.
# ---------------------------------------------------------------------------

def test_a_twelve_thousand_char_log_reaches_the_model_as_a_2048_char_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point: 12,000 chars of output become a 2,048-char tail.

    Nothing is lost -- the full bytes are on disk and addressable by sha256 --
    they simply stop being re-sent on every later turn.
    """
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    emitter = worktree / "emit.py"
    emitter.write_text(
        "import sys\nsys.stdout.write('x' * 12000)\n", encoding="utf-8"
    )
    packet = _packet(tmp_path, validation=["python3 emit.py"])
    ctx = _ctx(tmp_path, packet, worktree)

    result = w.validation_run(ctx, index=0)
    row = result["results"][0]

    assert row["returncode"] == 0
    assert row["output_bytes"] == 12000
    assert len(row["diagnostic_tail"]) == w.MAX_VALIDATION_DIAGNOSTIC_TAIL_CHARS
    assert row["diagnostic_tail"] == "x" * 2048
    assert row["stdout_truncated"] is True

    full = Path(row["full_output_path"])
    raw = full.read_bytes()
    assert len(raw) == 12000
    assert hashlib.sha256(raw).hexdigest() == row["full_output_sha256"]

    # And the record the model receives is a small fraction of the log.
    assert len(json.dumps(row).encode("utf-8")) < 3500

    page = w.validation_output_page(
        ctx, full_output_sha256=row["full_output_sha256"], offset=0, limit=64
    )
    assert page["returned_bytes"] == 64
    assert page["total_bytes"] == 12000
    assert page["eof"] is False


def test_pytest_short_summary_lines_and_path_refs_survive_the_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text(
        "import sys\n"
        "sys.stdout.write('noise\\n' * 400)\n"
        "sys.stdout.write('tests/test_x.py:41: AssertionError\\n')\n"
        "sys.stdout.write('FAILED tests/test_x.py::test_a - AssertionError\\n')\n"
        "sys.stdout.write('ERROR tests/test_y.py::test_b - ImportError\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    packet = _packet(tmp_path, validation=["python3 emit.py"])
    ctx = _ctx(tmp_path, packet, worktree)

    row = w.validation_run(ctx, index=0)["results"][0]

    assert row["returncode"] == 1
    assert row["failure_class"] == "test_failed"
    assert row["short_summary_lines"] == [
        "FAILED tests/test_x.py::test_a - AssertionError",
        "ERROR tests/test_y.py::test_b - ImportError",
    ]
    assert "tests/test_x.py:41" in row["path_refs"]


def test_a_non_zero_exit_is_a_result_not_an_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """34% of measured validation commands exited non-zero.

    A raise would cost the worker the whole record and force it back to raw
    Bash, which is the behaviour this tool replaces.
    """
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    packet = _packet(tmp_path, validation=["python3 emit.py"])
    ctx = _ctx(tmp_path, packet, worktree)

    result = w.validation_run(ctx, index=0)

    assert result["ok"] is True
    assert result["all_passed"] is False
    assert result["results"][0]["returncode"] == 3
    assert result["results"][0]["failure_class"] == "nonzero_exit"


def test_the_worker_never_retypes_the_command_and_an_index_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only 39-49% of measured commands were the card's declared command."""
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text("print('ok')\n", encoding="utf-8")
    packet = _packet(tmp_path, validation=["python3 emit.py"])
    ctx = _ctx(tmp_path, packet, worktree)

    row = w.validation_run(ctx, index=0)["results"][0]
    assert row["command"] == "python3 emit.py"

    with pytest.raises(w.WorkerToolError, match="validation_index_out_of_range"):
        w.validation_run(ctx, index=7)
    with pytest.raises(w.WorkerToolError, match="validation_index_invalid"):
        w.validation_run(ctx, index="tests")

    # No caller-supplied argv exists on the registered surface at all.
    import inspect

    from aiworkhub import worker_ai_tools_mcp as module

    class _FakeMcp:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, description: str | None = None):
            def decorator(fn):
                self.registered[name] = fn
                return fn
            return decorator

    fake = _FakeMcp()
    module.register_tools(fake, ctx)
    params = set(
        inspect.signature(fake.registered["aiworkhub_worker_validation_run"]).parameters
    )
    assert params == {"index", "force"}


# ---------------------------------------------------------------------------
# The memo, and the half of its key that makes it correct.
# ---------------------------------------------------------------------------

def test_an_identical_rerun_on_unchanged_bytes_returns_the_cached_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """550 identical commands were re-run inside one run, costing 14.7 MB."""
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    counter = worktree / "runs.txt"
    (worktree / "emit.py").write_text(
        "from pathlib import Path\n"
        "p = Path(__file__).with_name('runs.txt')\n"
        "p.write_text(str(len(p.read_text()) + 1 if p.exists() else 1))\n"
        "print('done')\n",
        encoding="utf-8",
    )
    (worktree / "tests" / "test_x.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    packet = _packet(
        tmp_path, validation=["python3 emit.py"], allowed_writes=["tests/test_x.py"]
    )
    ctx = _ctx(tmp_path, packet, worktree)

    first = w.validation_run(ctx, index=0)["results"][0]
    second = w.validation_run(ctx, index=0)["results"][0]

    assert first["unchanged_since_last_run"] is False
    assert second["unchanged_since_last_run"] is True
    assert second["full_output_sha256"] == first["full_output_sha256"]
    assert counter.read_text() == "1"  # the command ran exactly once


def test_force_bypasses_the_memo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text(
        "from pathlib import Path\n"
        "p = Path(__file__).with_name('runs.txt')\n"
        "p.write_text('x' * ((len(p.read_text()) + 1) if p.exists() else 1))\n",
        encoding="utf-8",
    )
    (worktree / "tests" / "test_x.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    packet = _packet(
        tmp_path, validation=["python3 emit.py"], allowed_writes=["tests/test_x.py"]
    )
    ctx = _ctx(tmp_path, packet, worktree)

    w.validation_run(ctx, index=0)
    forced = w.validation_run(ctx, index=0, force=True)["results"][0]

    assert forced["unchanged_since_last_run"] is False
    assert (worktree / "runs.txt").read_text() == "xx"


def test_the_memo_is_invalidated_by_an_edit_to_an_allowed_writes_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The memo key MUST include the current bytes.

    Without it a stale pass could be served after an edit -- which is exactly
    the failure a green-then-edited worker would carry into review.
    """
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text(
        "from pathlib import Path\n"
        "p = Path(__file__).with_name('runs.txt')\n"
        "p.write_text('x' * ((len(p.read_text()) + 1) if p.exists() else 1))\n",
        encoding="utf-8",
    )
    target = worktree / "tests" / "test_x.py"
    target.write_text("def test_a():\n    pass\n", encoding="utf-8")
    packet = _packet(
        tmp_path, validation=["python3 emit.py"], allowed_writes=["tests/test_x.py"]
    )
    ctx = _ctx(tmp_path, packet, worktree)

    first = w.validation_run(ctx, index=0)["results"][0]
    cached = w.validation_run(ctx, index=0)["results"][0]
    assert cached["unchanged_since_last_run"] is True

    target.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    after_edit = w.validation_run(ctx, index=0)["results"][0]

    assert after_edit["unchanged_since_last_run"] is False
    assert (worktree / "runs.txt").read_text() == "xx"
    assert first["command_sha256"] == after_edit["command_sha256"]


def test_a_new_file_under_an_allowed_writes_glob_also_invalidates_the_memo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text("print('ok')\n", encoding="utf-8")
    packet = _packet(
        tmp_path, validation=["python3 emit.py"], allowed_writes=["tests/*.py"]
    )
    ctx = _ctx(tmp_path, packet, worktree)

    w.validation_run(ctx, index=0)
    assert w.validation_run(ctx, index=0)["results"][0]["unchanged_since_last_run"]

    (worktree / "tests" / "test_new.py").write_text("def test_b():\n    pass\n", encoding="utf-8")
    assert (
        w.validation_run(ctx, index=0)["results"][0]["unchanged_since_last_run"]
        is False
    )


# ---------------------------------------------------------------------------
# The Bash disallow list: raw pytest/ruff/mypy are refused the way grep is.
# ---------------------------------------------------------------------------

def test_a_build_worker_cannot_run_a_raw_pytest_through_bash() -> None:
    denied = set(runtime_adapters.claude_disallowed_tools(read_only=False))
    allowed = set(runtime_adapters.claude_allowed_tools(read_only=False))

    # Denied the same way grep/rg/find already are.
    assert {"Bash(grep *)", "Bash(rg *)", "Bash(find *)"} <= denied
    assert {"Bash(pytest *)", "Bash(ruff *)", "Bash(mypy *)"} <= denied
    # And the substitute is in hand.
    assert (
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_validation_run" in allowed
    )
    # It is a LAUNCH rule only. ``.claude/settings.json`` is tracked, and a
    # prefix rule cannot match ``<python> -m pytest`` anyway, so the tree deny
    # would constrain every human and manager session while buying nothing
    # against the spelling this repository actually uses.
    from aiworkhub import provider_tool_guards

    assert not (
        set(runtime_adapters.CLAUDE_WORKER_VALIDATION_SHELL_DENIES)
        & set(provider_tool_guards.claude_settings_deny(read_only=False))
    )
    # Every other role rule still agrees across the two surfaces.  The raw file
    # EDITOR is subtracted for exactly the same reason as the validation
    # spellings above -- ``Edit`` is denied at the launch, where the build-worker
    # role exists, and must not be written into the tracked settings file that
    # every human and interactive session on this repository also checks out.
    assert set(provider_tool_guards.claude_settings_deny(read_only=False)) == (
        set(runtime_adapters.claude_disallowed_tools(read_only=False))
        - set(runtime_adapters.CLAUDE_WORKER_VALIDATION_SHELL_DENIES)
        - set(runtime_adapters.CLAUDE_WORKER_RAW_EDITOR_DENIES)
    )


# ---------------------------------------------------------------------------
# Exit preflight.
# ---------------------------------------------------------------------------

def _baseline_of(path: Path) -> str:
    import stat

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    mode = stat.S_IMODE(path.stat().st_mode)
    return f"file:{mode:o}:{digest}"


def test_preflight_names_each_of_the_six_measured_failure_classes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One actionable line per item, in the finalizer's own vocabulary."""
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)

    unchanged = worktree / "tests" / "test_unchanged.py"
    unchanged.write_text("def test_a():\n    pass\n", encoding="utf-8")
    empty = worktree / "tests" / "test_empty.py"
    empty.write_text("", encoding="utf-8")
    residual_path = worktree / "residual.txt"
    residual_path.write_text("inherited\n", encoding="utf-8")

    packet = _packet(
        tmp_path,
        validation=[],
        allowed_writes=[
            "tests/test_unchanged.py",
            "tests/test_empty.py",
            "tests/test_missing.py",
            "residual.txt",
        ],
        required_outputs=[
            "tests/test_unchanged.py",
            "tests/test_empty.py",
            "tests/test_missing.py",
            "src/not_allowed.py",
        ],
        workspace_baseline={
            "tests/test_unchanged.py": _baseline_of(unchanged),
        },
        residual=[{
            "path": "residual.txt",
            "pointers": [],
            "scope": "whole_file",
            "predecessor_file_hash": _baseline_of(residual_path),
        }],
    )
    ctx = _ctx(tmp_path, packet, worktree)

    result = w.exit_preflight(ctx)
    classes = {finding["failure_class"] for finding in result["findings"]}

    assert "required_output_unchanged" in classes
    assert "required_output_zero_bytes" in classes
    assert "required_output_not_allowed" in classes
    assert "required_output_mismatch" in classes
    assert "residual_contract_file_unchanged" in classes
    # The gate class is reported from the ledger; without a provisioned ledger
    # the preflight says so rather than inventing a verdict.
    assert result["worker_mcp_gate"]["checked"] is False
    assert (
        "validation_required_aiworkhub_mcp_call_missing"
        in w.EXIT_PREFLIGHT_FAILURE_CLASSES
    )
    assert set(w.EXIT_PREFLIGHT_FAILURE_CLASSES) == {
        "required_output_unchanged",
        "required_output_mismatch",
        "required_output_zero_bytes",
        "required_output_not_allowed",
        "residual_contract_file_unchanged",
        "validation_required_aiworkhub_mcp_call_missing",
    }

    unchanged_line = next(
        f for f in result["findings"]
        if f["failure_class"] == "required_output_unchanged"
    )
    assert unchanged_line["path"] == "tests/test_unchanged.py"
    assert "byte-identical to baseline" in unchanged_line["action"]
    assert "allow_unchanged was not granted" in unchanged_line["action"]


def test_preflight_reports_the_missing_live_mcp_call_from_the_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """172 attempts died here, 165 of them with every validation row green."""
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    home = tmp_path / "home"
    runtime = w.generate_worker_mcp_runtime(
        home=home,
        request_id="req-validation",
        task_id="TASK_VALIDATION",
        runner="claude_worker",
        topic="task_mcp",
        repo=worktree,
        authority_repo=tmp_path,
        source_graph_targets=[],
        session_topic="task_mcp",
        package_import_root=w.resolve_host_package_import_root(),
    )
    packet = _packet(tmp_path, validation=[])
    ctx = w.WorkerToolContext(
        task_id="TASK_VALIDATION",
        runner="claude_worker",
        topic="task_mcp",
        request_id="req-validation",
        repo=worktree,
        authority_repo=tmp_path,
        source_graph_targets=(),
        session_topic="task_mcp",
        audit_ledger_path=runtime.audit_ledger_path,
        audit_hmac_key_path=runtime.audit_hmac_key_path,
        contract_packet_path=packet,
    )

    result = w.exit_preflight(ctx)
    gate_findings = [
        f for f in result["findings"]
        if f["failure_class"] == "validation_required_aiworkhub_mcp_call_missing"
    ]

    assert result["worker_mcp_gate"]["checked"] is True
    assert {f["tool"] for f in gate_findings} == {
        "source_graph", "session_current_state", "ai_memory", "kb",
    }
    source_graph_line = next(f for f in gate_findings if f["tool"] == "source_graph")
    assert "no authenticated live" in source_graph_line["action"]
    assert "even when every" in source_graph_line["action"]


def test_preflight_never_marks_a_gate_satisfied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-closed depends on this: the finalizer re-runs the same checks."""
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    clean = worktree / "tests" / "test_clean.py"
    clean.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    packet = _packet(
        tmp_path,
        validation=[],
        allowed_writes=["tests/test_clean.py"],
        required_outputs=["tests/test_clean.py"],
        workspace_baseline={"tests/test_clean.py": "file:644:" + "0" * 64},
    )
    ctx = _ctx(tmp_path, packet, worktree)

    result = w.exit_preflight(ctx)

    # A completely clean required-output check still asserts nothing.
    assert result["findings"] == []
    assert result["satisfies_nothing"] is True
    assert result["read_only"] is True
    assert "satisfied" not in result
    assert "pass" not in result
    assert result["acceptance_evidence"] == "coordinator_post_exit_finalizer"
    assert not any(value is True for key, value in result.items() if "satisf" in key
                   and key != "satisfies_nothing")


def test_preflight_without_a_sealed_packet_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    ctx = w.WorkerToolContext(
        task_id="T", runner="r", topic="t", request_id="req",
        repo=worktree, authority_repo=tmp_path, source_graph_targets=(),
        session_topic="t", audit_ledger_path=None, audit_hmac_key_path=None,
    )

    result = w.exit_preflight(ctx)

    assert result["ok"] is False
    assert result["reason"] == "worker_contract_packet_not_bound"
    assert result["satisfies_nothing"] is True
    with pytest.raises(w.WorkerToolError):
        w.validation_run(ctx, index=0)


# ---------------------------------------------------------------------------
# The one-retry bound on the automatically appended preflight.
# ---------------------------------------------------------------------------

def test_a_green_validation_attaches_the_preflight_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool result IS a turn, so a green worker gets exactly one more.

    The one-shot arming per (task, request) is the bound: a second green run
    attaches nothing, so the appended result can never drive a retry loop.
    """
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text("print('ok')\n", encoding="utf-8")
    unchanged = worktree / "tests" / "test_unchanged.py"
    unchanged.write_text("def test_a():\n    pass\n", encoding="utf-8")
    packet = _packet(
        tmp_path,
        validation=["python3 emit.py"],
        allowed_writes=["tests/test_unchanged.py"],
        required_outputs=["tests/test_unchanged.py"],
        workspace_baseline={"tests/test_unchanged.py": _baseline_of(unchanged)},
    )
    ctx = _ctx(tmp_path, packet, worktree)

    first = w.validation_run(ctx, index="all")
    assert first["all_passed"] is True
    assert first["exit_preflight"]["auto_attached"] is True
    assert first["exit_preflight"]["auto_attach_retries_remaining"] == 0
    # It carries the real finding, which is why the extra turn is worth taking.
    assert any(
        f["failure_class"] == "required_output_unchanged"
        for f in first["exit_preflight"]["findings"]
    )

    second = w.validation_run(ctx, index="all", force=True)
    assert second["all_passed"] is True
    assert "exit_preflight" not in second

    third = w.validation_run(ctx, index="all", force=True)
    assert "exit_preflight" not in third

    # Explicitly asking still works -- the bound is on the AUTOMATIC append.
    assert w.exit_preflight(ctx)["ok"] is True


def test_a_failing_validation_does_not_spend_the_single_auto_attach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    packet = _packet(
        tmp_path, validation=["python3 emit.py"], allowed_writes=["emit.py"]
    )
    ctx = _ctx(tmp_path, packet, worktree)

    red = w.validation_run(ctx, index="all")
    assert red["all_passed"] is False
    assert "exit_preflight" not in red

    # Editing a declared path moves the memo key, so the repaired run really
    # executes rather than replaying the red receipt.
    (worktree / "emit.py").write_text("print('ok')\n", encoding="utf-8")
    green = w.validation_run(ctx, index="all")
    assert green["all_passed"] is True
    assert "exit_preflight" in green


# ---------------------------------------------------------------------------
# The advisory boundary: acceptance must never read any of this.
# ---------------------------------------------------------------------------

def test_validation_receipts_can_never_satisfy_the_completion_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ledger row is written with an advisory authority on purpose.

    ``verify_audit_ledger`` only counts an entry whose authority_source is
    "canonical" (or one of the two named semantic-edit / review-packet
    authorities), so no number of validation runs can stand in for a live
    Source Graph call.
    """
    _mute_chmod(monkeypatch)
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text("print('ok')\n", encoding="utf-8")
    home = tmp_path / "home"
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id="req-validation", task_id="TASK_VALIDATION",
        runner="claude_worker", topic="task_mcp", repo=worktree,
        authority_repo=tmp_path, source_graph_targets=[], session_topic="task_mcp",
        package_import_root=w.resolve_host_package_import_root(),
    )
    packet = _packet(tmp_path, validation=["python3 emit.py"])
    ctx = w.WorkerToolContext(
        task_id="TASK_VALIDATION", runner="claude_worker", topic="task_mcp",
        request_id="req-validation", repo=worktree, authority_repo=tmp_path,
        source_graph_targets=(), session_topic="task_mcp",
        audit_ledger_path=runtime.audit_ledger_path,
        audit_hmac_key_path=runtime.audit_hmac_key_path,
        contract_packet_path=packet,
    )

    w.validation_run(ctx, index=0)
    w.exit_preflight(ctx)

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
        request_id=ctx.request_id,
    )

    assert verification["ok"] is True
    assert verification["live_source_graph_calls"] == 0
    successful = verification["successful_call_count_by_tool"]
    assert successful.get("validation_command", 0) == 0
    assert successful.get("validation_receipt", 0) == 0
    assert verification["call_count_by_tool"]["validation_command"] >= 1
    conformance = w.receipt_conformance_report(verification)
    assert conformance["blocking"] is False


# ---------------------------------------------------------------------------
# The packet is sealed by the launcher, and the resolution is not a second path.
# ---------------------------------------------------------------------------

def test_the_worker_resolver_composes_the_coordinators_own_helpers() -> None:
    """No duplicate resolution exists to drift from run_validations."""
    source = Path(worker_workspace.__file__).read_text(encoding="utf-8")

    assert source.count("def _parse_validation_command_detailed(") == 1
    assert source.count("def _normalize_validation_interpreter_argv(") == 1
    assert source.count("def _normalize_pytest_validation_argv(") == 1
    assert (
        source.count(
            "def _normalize_trusted_validation_executable_argv_with_authority("
        )
        == 1
    )
    assert source.count("def _candidate_pythonpath_components(") == 1
    assert source.count("def resolve_worker_validation_argv(") == 1

    whole = source[source.index("def resolve_worker_validation_argv("):]
    whole = whole[: whole.index("\n\n\n")]
    # Executable statements only -- the docstring names sandbox_argv to explain
    # why it is deliberately absent from the code.
    code = whole[whole.index('"""', whole.index('"""') + 3) + 3:]
    for helper in (
        "_parse_validation_command_detailed(",
        "_normalize_validation_interpreter_argv(",
        "_normalize_pytest_validation_argv(",
        "_candidate_pythonpath_components(",
        "_normalize_trusted_validation_executable_argv_with_authority(",
    ):
        assert helper in code, helper
    # It must never wrap a second sandbox: the caller is already inside one.
    assert "sandbox_argv" not in code


def test_bare_python_validation_uses_the_verified_running_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker child must never depend on PATH to resolve bare python."""
    _mute_chmod(monkeypatch)
    monkeypatch.setenv("PATH", "")
    worktree = _worktree(tmp_path)
    (worktree / "emit.py").write_text("print('ok')\n", encoding="utf-8")
    packet = _packet(tmp_path, validation=["python emit.py"])
    ctx = _ctx(tmp_path, packet, worktree)

    row = w.validation_run(ctx, index=0)["results"][0]

    assert row["returncode"] == 0
    assert row["argv"][0] == sys.executable
    assert row["declared_head"] == "python"


def test_bare_python_module_validation_reaches_pytest_without_path_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pin the exact command shape that stopped the single-writer worker."""
    _mute_chmod(monkeypatch)
    monkeypatch.setenv("PATH", "")
    worktree = _worktree(tmp_path)
    (worktree / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    packet = _packet(
        tmp_path, validation=["python -m pytest -q tests/test_ok.py"]
    )
    ctx = _ctx(tmp_path, packet, worktree)

    row = w.validation_run(ctx, index=0)["results"][0]

    assert row["returncode"] == 0, row.get("reason")
    assert Path(row["argv"][0]).is_absolute()
    assert row["argv"][1:4] == ["-P", "-m", "pytest"]
    assert row["declared_head"] == "python"


def test_create_workspace_seals_the_contract_packet(tmp_path: Path) -> None:
    packet = worker_workspace.worker_contract_packet_path(tmp_path / "home")
    (tmp_path / "home").mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id="req-seal",
        repo=tmp_path,
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=("tests/test_x.py",),
        parent_baseline={"tests/test_x.py": "file:644:" + "a" * 64},
        workspace_baseline={"tests/test_x.py": "file:644:" + "b" * 64},
    )
    card = {
        "validation": [".venv/bin/python -m pytest -q tests/test_x.py"],
        "required_outputs": ["tests/test_x.py"],
        "allow_unchanged_required_outputs": [],
        "allowed_writes": ["tests/test_x.py"],
    }

    written = worker_workspace.seal_worker_contract_packet(workspace, card)

    assert written == packet
    payload = json.loads(packet.read_text(encoding="utf-8"))
    assert payload["validation"] == card["validation"]
    assert payload["required_outputs"] == ["tests/test_x.py"]
    assert payload["workspace_baseline"] == workspace.workspace_baseline
    assert payload["parent_baseline"] == workspace.parent_baseline
    assert payload["residual_contract_manifest"] == []


def test_provisioning_binds_the_sealed_packet_into_the_worker_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher's seal reaches the sandboxed server as an env binding."""
    _mute_chmod(monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id="req-provision",
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=("tests/test_x.py",),
        parent_baseline={},
        workspace_baseline={},
    )
    worker_workspace.seal_worker_contract_packet(
        workspace, {"validation": ["python3 -c pass"], "required_outputs": []}
    )

    runtime = worker_workspace.provision_worker_mcp_runtime(
        workspace,
        request_id="req-provision",
        task_id="TASK_PROVISION",
        runner="claude_worker",
        topic="task_mcp",
        backend="landlock",
        source_graph_targets=[],
        session_topic="task_mcp",
        allowed_writes=["tests/test_x.py"],
    )

    bound = runtime.env[w.ENV_CONTRACT_PACKET_PATH]
    assert bound == str(worker_workspace.worker_contract_packet_path(home))

    ctx = w.load_context_from_env({
        w.ENV_TASK_ID: "TASK_PROVISION",
        w.ENV_RUNNER: "claude_worker",
        w.ENV_TOPIC: "task_mcp",
        w.ENV_REQUEST_ID: "req-provision",
        w.ENV_REPO: str(worktree),
        w.ENV_AUTHORITY_REPO: str(repo),
        w.ENV_CONTRACT_PACKET_PATH: bound,
    })
    assert ctx.contract_packet_path == Path(bound)
    assert w._contract_packet(ctx)["validation"] == ["python3 -c pass"]


def test_the_residual_manifest_is_merged_into_the_sealed_packet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The manifest is built after predecessor materialization, not at seal."""
    _mute_chmod(monkeypatch)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    residual = worktree / "residual.txt"
    residual.write_text("inherited\n", encoding="utf-8")
    workspace = worker_workspace.WorkerWorkspace(
        request_id="req-residual",
        repo=tmp_path,
        path=worktree,
        home=home,
        allowed_writes=("residual.txt",),
        parent_baseline={},
        workspace_baseline={},
    )
    card = {
        "validation": [],
        "rework_predecessor": {
            "residual_identities": [{"path": "residual.txt", "pointer": ""}]
        },
    }
    worker_workspace.seal_worker_contract_packet(workspace, card)

    manifest = worker_workspace.build_residual_contract_manifest(workspace, card)

    sealed = json.loads(
        worker_workspace.worker_contract_packet_path(home).read_text(encoding="utf-8")
    )
    assert manifest
    assert sealed["residual_contract_manifest"] == manifest
