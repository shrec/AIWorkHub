"""B919: accept_review must fail closed on stale immutable/dependency inputs.

B914 proved the race: a retained-worktree validation passed against a
29-row dependency snapshot (input A) while the canonical dependency (B912)
had already advanced to 3,522 rows (input B) by the time
``ProcessManager.accept_review`` promoted the request. These tests exercise
the guard added to ``accept_review`` (and the claim-time manifest capture in
``_launch_isolated`` / ``_finalize_isolated_request``) directly against the
real ``process_launcher``/``task_engine`` source in this worktree.

This worktree is a sparse checkout: only the four allowed_writes files
(``core.py``, ``process_launcher.py``, ``task_engine.py``, ``task_store.py``)
exist under ``src/aiworkhub``. Every other sibling module the package
imports at module scope (``repository_state``, ``callback_store``,
``task_plan``, ``dependency_autolaunch``, ``storage_registry``,
``runtime_adapters``, ``worker_ai_tools_mcp``, ``worker_workspace``,
``provider_tool_guards``, ``task_fsm``) is genuinely absent here -- the same
category of pre-existing, out-of-scope worktree gap already documented by
``test_process_launcher_security.py``'s ``_ensure_deepseek_credentials_stub``.
``_ensure_aiworkhub_sibling_stubs`` below installs lenient placeholder
modules for exactly those absent siblings so the real, unmodified
``core.py``/``task_engine.py``/``process_launcher.py`` import and run
unchanged; the test bodies themselves monkeypatch the handful of names
(``WorkerWorkspace``, ``enforce_scope``, ``promote``, ...) that must behave
predictably for the scenario under test.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


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
    syscall outright, including on paths this same process just created and
    owns (see ``test_process_launcher_security.py``'s identically-named
    fixture). Probing once and neutralizing ``os.chmod``/``os.fchmod`` to a
    no-op only WHEN the syscall is genuinely blocked keeps this a no-op on a
    host where chmod works."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


class _LenientStub(types.ModuleType):
    """Auto-vivifying stand-in for an absent sibling module.

    Never raises AttributeError for a normal identifier: an ``*Error``/
    ``*Exception`` name becomes a fresh Exception subclass (so ``except
    X:`` still works), an ALL_CAPS name becomes its own name as a string
    (so it is still usable as a path/env-var-name literal), and anything
    else becomes a no-op callable. Each resolved name is cached on the
    instance so repeated access returns the identical object.
    """

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if name.endswith("Error") or name.endswith("Exception"):
            value: object = type(name, (Exception,), {})
        elif name.isupper():
            value = name
        else:
            def value(*_a: object, **_k: object) -> None:
                return None
        setattr(self, name, value)
        return value


_ABSENT_SIBLINGS = (
    "repository_state",
    "callback_store",
    "task_plan",
    "dependency_autolaunch",
    "storage_registry",
    "runtime_adapters",
    "worker_ai_tools_mcp",
    "worker_workspace",
    "provider_tool_guards",
    "task_fsm",
)

_AIWORKHUB_STUBBED_MODULES: set[str] = set()
_AIWORKHUB_SYNTHETIC_PACKAGE = False


def _ensure_aiworkhub_sibling_stubs() -> None:
    global _AIWORKHUB_SYNTHETIC_PACKAGE
    # Canonical/full checkouts have every sibling and must use the normal
    # package import path.  Creating then removing a synthetic package in
    # that case leaves already-collected tests holding different module
    # objects (locally masked when Copilot happens to be installed, exposed
    # in clean CI).  The shim exists only for the original sparse worktree.
    if all((_SRC / "aiworkhub" / f"{sub}.py").is_file() for sub in _ABSENT_SIBLINGS):
        return
    pkg = sys.modules.get("aiworkhub")
    if pkg is None:
        pkg = types.ModuleType("aiworkhub")
        pkg.__path__ = [str(_SRC / "aiworkhub")]  # type: ignore[attr-defined]
        pkg.COORDINATOR_TOKEN_ENV = "AIWORKHUB_COORDINATOR_TOKEN"
        pkg.COORDINATOR_TOKEN_FILE_ENV = "AIWORKHUB_COORDINATOR_TOKEN_FILE"
        pkg.coordinator_config = lambda *_a, **_k: {}
        pkg.refresh_coordinator_config = lambda *_a, **_k: {}
        sys.modules["aiworkhub"] = pkg
        _AIWORKHUB_SYNTHETIC_PACKAGE = True
    for sub in _ABSENT_SIBLINGS:
        full_name = f"aiworkhub.{sub}"
        if full_name in sys.modules:
            continue
        # This test originated in a sparse task worktree.  In the canonical
        # repository these sibling modules exist and must be imported for
        # real; installing a stub anyway poisons later test modules during
        # pytest collection and makes monkeypatch target a different module
        # object than the test is executing.
        if (_SRC / "aiworkhub" / f"{sub}.py").is_file():
            continue
        stub = _LenientStub(full_name)
        sys.modules[full_name] = stub
        setattr(pkg, sub, stub)
        _AIWORKHUB_STUBBED_MODULES.add(full_name)


# Preserve the pre-existing package state so this module's lenient import
# stubs cannot leak into later test modules collected in the same process.
_AIWORKHUB_BASELINE_MODULES = frozenset(
    name for name in sys.modules if name == "aiworkhub" or name.startswith("aiworkhub.")
)


def _restore_aiworkhub_sys_modules() -> None:
    if not _AIWORKHUB_SYNTHETIC_PACKAGE and not _AIWORKHUB_STUBBED_MODULES:
        return
    # Collection imports happen for the complete suite before fixture
    # teardown.  Remove every package/module introduced by this sparse-tree
    # compatibility import immediately, otherwise the synthetic package (it
    # intentionally has no __version__) poisons all later test collections.
    introduced = {
        name
        for name in sys.modules
        if (name == "aiworkhub" or name.startswith("aiworkhub."))
        and name not in _AIWORKHUB_BASELINE_MODULES
    }
    pkg = sys.modules.get("aiworkhub")
    for name in sorted(introduced, reverse=True):
        if pkg is not None and name != "aiworkhub":
            leaf = name[len("aiworkhub.") :].split(".", 1)[0]
            pkg.__dict__.pop(leaf, None)
        sys.modules.pop(name, None)
    _AIWORKHUB_STUBBED_MODULES.clear()


_ensure_aiworkhub_sibling_stubs()

try:
    from aiworkhub import process_launcher  # noqa: E402
except BaseException:
    # Collection failures never reach fixture teardown.
    _restore_aiworkhub_sys_modules()
    raise
else:
    # Pytest imports every test module during collection before any fixture
    # teardown runs. Keep the imported module reference above, but immediately
    # remove the temporary package/stubs so later modules collect cleanly.
    _restore_aiworkhub_sys_modules()


# ---------------------------------------------------------------------------
# _path_manifest: bounded, deterministic file/directory manifests
# ---------------------------------------------------------------------------


def test_path_manifest_file_is_bounded_and_deterministic(tmp_path: Path) -> None:
    base = tmp_path / "repo"
    (base / "dep").mkdir(parents=True)
    target = base / "dep" / "report.json"
    target.write_text('{"rows": 29}\n', encoding="utf-8")

    manifest = process_launcher._path_manifest(base, ["dep/report.json"])
    entry = manifest["dep/report.json"]

    assert entry["kind"] == "file"
    assert entry["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert entry["size"] == target.stat().st_size
    assert entry["line_count"] == 1

    # Deterministic + unaffected by growth of an unrelated declared path.
    again = process_launcher._path_manifest(base, ["dep/report.json"])
    assert again == manifest


def test_path_manifest_directory_never_content_hashes_children(tmp_path: Path) -> None:
    base = tmp_path / "repo"
    bucket = base / "dep" / "buckets"
    bucket.mkdir(parents=True)
    (bucket / "a.jsonl").write_text("x" * 10, encoding="utf-8")
    (bucket / "b.jsonl").write_text("y" * 20, encoding="utf-8")

    manifest = process_launcher._path_manifest(base, ["dep/buckets"])
    entry = manifest["dep/buckets"]
    assert entry["kind"] == "dir"
    assert entry["entry_count"] == 2

    # Rewriting a child's bytes (same size) must not change the listing
    # digest -- the manifest is bounded to name+size, never full content,
    # so it stays proportional to the declared path count.
    (bucket / "a.jsonl").write_text("z" * 10, encoding="utf-8")
    same_size_manifest = process_launcher._path_manifest(base, ["dep/buckets"])
    assert same_size_manifest["dep/buckets"] == entry

    # Population growth (B914: 29 rows -> 3,522 rows) changes entry_count.
    (bucket / "c.jsonl").write_text("w" * 5, encoding="utf-8")
    grown_manifest = process_launcher._path_manifest(base, ["dep/buckets"])
    assert grown_manifest["dep/buckets"]["entry_count"] == 3
    assert grown_manifest["dep/buckets"] != entry


def test_path_manifest_missing_path_is_bounded_kind_missing(tmp_path: Path) -> None:
    base = tmp_path / "repo"
    base.mkdir()
    manifest = process_launcher._path_manifest(base, ["dep/does_not_exist.json"])
    assert manifest["dep/does_not_exist.json"] == {"kind": "missing"}


# ---------------------------------------------------------------------------
# ProcessManager.accept_review: fail-closed guard on declared immutable inputs
# ---------------------------------------------------------------------------


class _FakeWorkspace:
    def __init__(self, repo: Path, request_id: str, path: Path, home: Path) -> None:
        self.repo = repo
        self.request_id = request_id
        self.path = path
        self.home = home

    @classmethod
    def from_metadata(cls, payload: dict) -> "_FakeWorkspace":
        return cls(
            repo=Path(payload["repo"]),
            request_id=str(payload["request_id"]),
            path=Path(payload["path"]),
            home=Path(payload["home"]),
        )

    def as_metadata(self) -> dict:
        return {
            "repo": str(self.repo),
            "request_id": self.request_id,
            "path": str(self.path),
            "home": str(self.home),
        }


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Build one ProcessManager plus the mutable fakes accept_review touches.

    Returns (manager, card, request_id, task_id, runner, topic, repo,
    workspace_dir, promote_calls, accept_review_calls).
    """
    repo = tmp_path / "repo"
    (repo / "dep").mkdir(parents=True)
    (repo / "dep" / "report.json").write_text('{"rows": 29}\n', encoding="utf-8")

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    output_relative = "out/result.txt"
    output_bytes = b"worker-result\n"
    (workspace_dir / "out").mkdir()
    (workspace_dir / "out" / "result.txt").write_bytes(output_bytes)

    # Claim-time (input A) snapshot -- exactly what _launch_isolated computes
    # from the canonical repo just before task_engine.claim_start_exact.
    immutable_input_manifest = process_launcher._path_manifest(repo, ["dep/report.json"])

    request_id = f"b919-{uuid.uuid4().hex[:8]}"
    task_id = "TASK_B919"
    runner = "claude_worker_b919"
    topic = "task_mcp"

    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "status": "review",
        "worker_status": "review",
        "claimed_by": runner,
        "allowed_writes": [output_relative],
        "required_outputs": [],
        "validation": [],
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                },
                "workspace": {
                    "repo": str(repo),
                    "request_id": request_id,
                    "path": str(workspace_dir),
                    "home": str(workspace_dir),
                },
                "changed_path_hashes": {
                    output_relative: hashlib.sha256(output_bytes).hexdigest(),
                },
                "immutable_inputs": ["dep/report.json"],
                "immutable_input_manifest": immutable_input_manifest,
            },
        },
    }

    def show(task_id_arg: str) -> dict:
        assert task_id_arg == task_id
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=show,
        collision_guard=lambda **_k: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        adapter_builder=lambda **_k: None,
    )
    (tmp_path / "processes").mkdir(mode=0o700, exist_ok=True)
    fixture_workspace = _FakeWorkspace(
        repo=repo,
        request_id=request_id,
        path=workspace_dir,
        home=workspace_dir,
    )
    artifact_receipt = manager._persist_attempt_artifacts(
        request_id,
        {
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "adapter_id": "claude_cli",
            "model": "claude_cli",
            "stdout_path": str(tmp_path / "processes" / f"{request_id}.stdout.log"),
            "workspace": fixture_workspace.as_metadata(),
        },
        fixture_workspace,
        target_state="review_ready",
        changed_paths=[output_relative],
        changed_path_hashes={
            output_relative: hashlib.sha256(output_bytes).hexdigest(),
        },
        review={"kind": "test_fixture"},
    )
    card["terminal_review"]["evidence"].update({
        "attempt_artifact_manifest": artifact_receipt,
        "evidence_record": manager._canonical_outcome_evidence(
            request_id,
            artifact_receipt,
            level=process_launcher.evidence_levels.EvidenceLevel.STATIC_EVIDENCE,
            message="Canonical stale-input test fixture evidence.",
        ),
    })
    manager._append_event({
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": "claude_cli",
        "state": "running",
    })

    monkeypatch.setattr(process_launcher, "WorkerWorkspace", _FakeWorkspace)
    # The canonical launcher now validates the exact configured GC worktree
    # shape before promotion.  This focused stale-input fixture deliberately
    # uses a compact synthetic workspace, so isolate the guard here; its real
    # path/shape contract is covered by worker_workspace lifecycle tests.
    monkeypatch.setattr(
        process_launcher,
        "assert_gc_safe_workspace_shape",
        lambda _request_id, _path, _home: None,
    )
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda _ws: [output_relative])
    monkeypatch.setattr(
        process_launcher,
        "validate_required_outputs",
        lambda _ws, _req, allow_empty=(), allow_unchanged=(): [],
    )
    monkeypatch.setattr(
        process_launcher,
        "run_validations",
        lambda _ws, _validation, **_kwargs: [],
    )
    monkeypatch.setattr(process_launcher, "cleanup_workspace", lambda *_a, **_k: None)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)

    promote_calls: list[list[str]] = []

    def _promote(_ws: object, changed: list[str]) -> list[str]:
        promote_calls.append(list(changed))
        (repo / "out").mkdir(parents=True, exist_ok=True)
        (repo / output_relative).write_bytes(output_bytes)
        return list(changed)

    monkeypatch.setattr(process_launcher, "promote", _promote)

    accept_review_calls: list[dict] = []

    def _accept_review(_repo: Path, _task_id: str, *, runner: str, topic: str, request_id: str, evidence: dict) -> dict:
        accept_review_calls.append({
            "runner": runner, "topic": topic, "request_id": request_id, "evidence": evidence,
        })
        card["status"] = "finished"
        card["worker_status"] = "done"
        card["accepted_request_id"] = request_id
        return {"ok": True, "returncode": 0, "command": [], "stdout": "", "stderr": ""}

    monkeypatch.setattr(process_launcher.task_engine, "accept_review", _accept_review)

    return (
        manager, card, request_id, task_id, runner, topic, repo,
        workspace_dir, promote_calls, accept_review_calls,
    )


def test_accept_review_fails_closed_on_stale_immutable_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B919/B914: claim at input A (29 rows), canonical mutation to B (3,522
    rows), then accept_review must fail closed with stale_input/
    dependency_changed before copying any output or finishing the task."""
    (
        manager, card, request_id, task_id, runner, topic, repo,
        workspace_dir, promote_calls, accept_review_calls,
    ) = _fixture(monkeypatch, tmp_path)

    # Canonical dependency mutates after claim, before accept_review -- the
    # exact B914 race window.
    (repo / "dep" / "report.json").write_text('{"rows": 3522}\n', encoding="utf-8")

    result = manager.accept_review(request_id, task_id)

    assert result["ok"] is False
    assert result["error"].startswith("stale_input:dependency_changed:")
    assert "dep/report.json" in result["error"]

    # Fails BEFORE promotion/finalize: no output copied, no promote/accept
    # call, canonical task state untouched.
    assert promote_calls == []
    assert accept_review_calls == []
    assert not (repo / "out" / "result.txt").exists()
    assert card["status"] == "review"
    assert card["worker_status"] == "review"


def test_accept_review_fails_closed_on_stale_verification_claim_epoch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (
        manager, card, request_id, task_id, runner, topic, repo,
        workspace_dir, promote_calls, accept_review_calls,
    ) = _fixture(monkeypatch, tmp_path)
    card["claim_epoch"] = 4
    card["deterministic_verification"] = {
        "applicable": True,
        "pass": True,
        "claim_epoch": 3,
    }

    result = manager.accept_review(request_id, task_id)

    assert result["ok"] is False
    assert result["error"] == (
        "deterministic_verification_claim_epoch_mismatch:expected=4:observed=3"
    )
    assert promote_calls == []
    assert accept_review_calls == []


def test_accept_review_promotes_exactly_once_when_input_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unchanged immutable input/dependency hashes still accept and promote
    -- exactly once, including on an idempotent retry."""
    (
        manager, card, request_id, task_id, runner, topic, repo,
        workspace_dir, promote_calls, accept_review_calls,
    ) = _fixture(monkeypatch, tmp_path)

    result = manager.accept_review(request_id, task_id)
    assert result["ok"] is True
    assert promote_calls == [["out/result.txt"]]
    assert len(accept_review_calls) == 1
    assert (repo / "out" / "result.txt").read_bytes() == b"worker-result\n"
    assert card["status"] == "finished"

    # Idempotent retry of the same request against the now-finished task:
    # short-circuits to already_accepted, never promotes/finalizes again.
    retry = manager.accept_review(request_id, task_id)
    assert retry["ok"] is True
    assert retry.get("already_accepted") is True
    assert promote_calls == [["out/result.txt"]]
    assert len(accept_review_calls) == 1


def test_accept_review_guard_is_noop_without_declared_immutable_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing tasks that declare no immutable_inputs are unaffected: the
    scope/required-output/validation/idempotence gates behave exactly as
    before this guard existed."""
    (
        manager, card, request_id, task_id, runner, topic, repo,
        workspace_dir, promote_calls, accept_review_calls,
    ) = _fixture(monkeypatch, tmp_path)

    evidence = card["terminal_review"]["evidence"]
    evidence["immutable_inputs"] = []
    evidence["immutable_input_manifest"] = {}
    # Mutate the dependency file too: an undeclared path is not the guard's
    # concern and must never block promotion.
    (repo / "dep" / "report.json").write_text('{"rows": 3522}\n', encoding="utf-8")

    result = manager.accept_review(request_id, task_id)

    assert result["ok"] is True
    assert promote_calls == [["out/result.txt"]]
    assert len(accept_review_calls) == 1


def test_accept_review_requires_explicit_manager_confirmation_for_destructive_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (
        manager, card, request_id, task_id, runner, topic, repo,
        workspace_dir, promote_calls, accept_review_calls,
    ) = _fixture(monkeypatch, tmp_path)
    blocker = process_launcher.quality_evidence.EvidenceCheck(
        check_id="builtin:destructive_diff:src/engine.py",
        kind="static_analysis",
        status=process_launcher.quality_evidence.STATUS_FAILED,
        affected_paths=("src/engine.py",),
        summary="baseline_lines=4000; candidate_lines=80; public_symbols=40->1",
        provenance="builtin:manager_accept_destructive_diff",
        error="explicit confirmation required",
    )
    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "run_destructive_diff_checks",
        lambda *_args, **_kwargs: [blocker],
    )
    # Automatic destructive-change risk now requires a combined-tree
    # revalidation before the independent-reviewer gate.  Keep this focused
    # synthetic fixture out of Git/worktree mechanics so the assertion below
    # reaches the reviewer requirement it owns.
    monkeypatch.setattr(
        process_launcher,
        "create_combined_validation_workspace",
        lambda workspace, _card, changed: (
            workspace,
            {
                "schema_id": "aiworkhub.combined_tree.v1",
                "candidate_paths": list(changed),
            },
        ),
    )
    real_quality_gate = process_launcher.quality_evidence.run_completion_quality_gate

    def _quality_gate(*args: object, **kwargs: object) -> dict:
        if "risk_signals" in kwargs:
            return real_quality_gate(*args, **kwargs)
        return {"passed": True, "blocking_checks": [], "checks": []}

    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "run_completion_quality_gate",
        _quality_gate,
    )

    blocked = manager.accept_review(request_id, task_id)

    assert blocked["ok"] is False
    assert "destructive_diff_requires_manager_confirmation" in blocked["error"]
    assert promote_calls == []
    assert accept_review_calls == []
    assert card["status"] == "review"

    risk_blocked = manager.accept_review(
        request_id,
        task_id,
        confirm_destructive_change=True,
    )

    assert risk_blocked["ok"] is False
    assert "quality_gate_failed" in risk_blocked["error"]
    assert "required_reviewer_missing" in risk_blocked["error"]
    assert promote_calls == []
    assert accept_review_calls == []


def test_accept_review_rejects_caller_supplied_reviewer_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (
        manager, card, request_id, task_id, runner, topic, repo,
        workspace_dir, promote_calls, accept_review_calls,
    ) = _fixture(monkeypatch, tmp_path)
    combined_calls: list[list[str]] = []

    def _combined(source, _card, changed):
        combined_calls.append(list(changed))
        return source, {
            "schema_id": "aiworkhub.combined_tree.v1",
            "candidate_paths": list(changed),
            "canonical_delta_paths": [],
            "observed_candidate_paths": list(changed),
        }

    monkeypatch.setattr(
        process_launcher,
        "create_combined_validation_workspace",
        _combined,
    )

    accepted = manager.accept_review(
        request_id,
        task_id,
        requested_risk_tier="medium",
        reviewer_reports=[
            {
                "lens": "correctness",
                "provider": "deepseek_v4pro",
                "read_only": True,
                "can_mutate_repo": False,
                "findings": [],
            }
        ],
    )

    assert accepted == {
        "ok": False,
        "error": "unverified_reviewer_reports_forbidden",
        "request_id": request_id,
        "task_id": task_id,
    }
    assert combined_calls == []
    assert promote_calls == []
    assert accept_review_calls == []


def test_target_acceptance_consumes_already_accepted_reviewer_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (
        manager, card, request_id, task_id, runner, topic, repo,
        workspace_dir, promote_calls, accept_review_calls,
    ) = _fixture(monkeypatch, tmp_path)
    reviewer_request_id = "review-request-accepted"
    reviewer_task_id = "REVIEW_TASK_ACCEPTED"
    receipt = {
        "schema_id": process_launcher.quality_reviewer.RECEIPT_SCHEMA_ID,
        "packet_sha256": "a" * 64,
        "target": {
            "request_id": request_id,
            "task_id": task_id,
            "claim_epoch": 0,
        },
        "reviewer": {
            "request_id": reviewer_request_id,
            "task_id": reviewer_task_id,
            "provider": "deepseek_v4pro",
        },
        "report": {
            "lens": "correctness",
            "provider": "deepseek_v4pro",
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
    }
    reviewer_card = {
        "task_id": reviewer_task_id,
        "topic": "quality_review",
        "status": "finished",
        "worker_status": "done",
        "accepted_request_id": reviewer_request_id,
        "terminal_review": {
            "evidence": {"quality_review_receipt": copy.deepcopy(receipt)}
        },
        "accept_evidence": {"quality_review_receipt": copy.deepcopy(receipt)},
    }

    def show(task_id_arg: str) -> dict:
        selected = reviewer_card if task_id_arg == reviewer_task_id else card
        return {
            "returncode": 0,
            "stdout": json.dumps(selected),
            "stderr": "",
        }

    manager._show_task = show
    manager._append_event({
        "request_id": reviewer_request_id,
        "task_id": reviewer_task_id,
        "runner": "reviewer",
        "topic": "quality_review",
        "adapter_id": "deepseek_v4pro",
        "state": "accepted",
        "accepted": True,
        "quality_review_receipt": receipt,
    })
    monkeypatch.setattr(
        process_launcher,
        "create_combined_validation_workspace",
        lambda workspace, _card, changed: (
            workspace,
            {
                "schema_id": "aiworkhub.combined_tree.v1",
                "candidate_paths": list(changed),
            },
        ),
    )
    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "run_completion_quality_gate",
        lambda *_args, **_kwargs: {
            "passed": True,
            "blocking_checks": [],
            "checks": [],
        },
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "disposition_reviewer_children",
        lambda *_args, **_kwargs: {
            "ok": True,
            "stdout": json.dumps({"finalized": []}),
        },
    )

    result = manager.accept_review(
        request_id,
        task_id,
        requested_risk_tier="medium",
        reviewer_request_ids=[reviewer_request_id],
    )

    assert result["ok"] is True
    assert promote_calls == [["out/result.txt"]]
    assert len(accept_review_calls) == 1
    assert result["reviewer_finalization"] == [{
        "task_id": reviewer_task_id,
        "finished": True,
        "cleanup_error": "",
    }]
