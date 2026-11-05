"""NF-2026-02052: finalization reuses the worker's verified full-snapshot run."""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher_validation as validation
from aiworkhub import worker_validation_reuse as reuse_mod
from aiworkhub.worker_workspace import (
    cleanup_workspace,
    create_combined_validation_workspace,
    create_workspace,
)

_COMMAND = "python -c \"print('probe')\""
_CARD = {
    "allowed_writes": ["pkg/feature.py"],
    "read_first": [],
    "immutable_inputs": [],
    "required_outputs": [],
    "validation": [_COMMAND],
}
_CANDIDATE = ["pkg/feature.py"]
_REQUEST = "0123456789abcdef0123456789abcdef"


class _Ran(Exception):
    """Raised by the fake snapshot factory: finalization would have run."""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _route(metadata):
    return {"backend": metadata.get("backend")}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    from aiworkhub.process_launcher import PROCESS_DIR_ENV

    monkeypatch.delenv(PROCESS_DIR_ENV, raising=False)
    ws = create_workspace(repo, _REQUEST, _CARD, "validation")
    (ws.path / "pkg" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    private = tmp_path / "private" / "task_mcp_worker_runtime"
    private.mkdir(parents=True)
    key = private / "audit_hmac.key"
    key.write_bytes(b"k" * 32)
    ledger = private / "audit_ledger.jsonl"
    ledger.write_bytes(b"")
    metadata = {
        **_CARD,
        "request_id": ws.request_id,
        "claim_epoch": 3,
        "worker_mcp": {"audit_hmac_key_path": str(key), "audit_ledger_path": str(ledger)},
        "workspace": ws.as_metadata(),
    }
    yield SimpleNamespace(ws=ws, repo=repo, key=key, ledger=ledger, metadata=metadata)
    cleanup_workspace(ws.repo, ws.path, ws.home)


class _Counters:
    def __init__(self) -> None:
        self.snapshots = 0
        self.runs = 0
        self.paths: list[Path] = []

    def create_snapshot(self, *_args):
        self.snapshots += 1
        raise _Ran

    def run_validations(self, workspace, commands, **_route):
        self.runs += 1
        self.paths.append(Path(workspace.path))
        return [{"command": c, "returncode": 0, "timed_out": False} for c in commands]


def _real_runner(counters: _Counters, *, returncode: int = 0):
    def run_validations(workspace, commands, **_kw):
        counters.runs += 1
        counters.paths.append(Path(workspace.path))
        return [
            {"command": c, "returncode": returncode, "timed_out": False} for c in commands
        ]

    def runner(workspace, authority, route_metadata, candidate, *, reuse=None):
        return validation.run_full_snapshot_validations(
            workspace, authority, route_metadata, candidate,
            create_snapshot=create_combined_validation_workspace,
            cleanup_workspace=cleanup_workspace,
            run_validations=run_validations,
            route_resolver=_route,
            baseline_comparer=lambda *a, **k: [],
            reuse=reuse,
        )

    return runner


def _worker_run(env, *, returncode: int = 0) -> _Counters:
    counters = _Counters()
    out = reuse_mod.run_worker_full_snapshot(
        metadata=env.metadata,
        commands=[_COMMAND],
        candidate_digest="d" * 64,
        key_path=env.key,
        ledger_path=env.ledger,
        run_full_snapshot=_real_runner(counters, returncode=returncode),
    )
    assert out["ok"] is True and counters.runs == 1
    return counters


def _finalize(env, metadata=None):
    """What ``_run_finalization_validations`` does: load the record, pass it as reuse."""

    counters = _Counters()
    metadata = env.metadata if metadata is None else metadata
    result = validation.run_full_snapshot_validations(
        env.ws, metadata, metadata, _CANDIDATE,
        create_snapshot=counters.create_snapshot,
        cleanup_workspace=lambda *a: None,
        run_validations=counters.run_validations,
        route_resolver=_route,
        baseline_comparer=lambda *a, **k: [],
        reuse=reuse_mod.load_verified_reuse(metadata),
    )
    return counters, result


def _assert_falls_back(env, metadata=None) -> None:
    counters = _Counters()
    with pytest.raises(_Ran):
        metadata = env.metadata if metadata is None else metadata
        validation.run_full_snapshot_validations(
            env.ws, metadata, metadata, _CANDIDATE,
            create_snapshot=counters.create_snapshot,
            cleanup_workspace=lambda *a: None,
            run_validations=counters.run_validations,
            route_resolver=_route,
            baseline_comparer=lambda *a, **k: [],
            reuse=reuse_mod.load_verified_reuse(metadata),
        )
    assert counters.snapshots == 1


def _rewrite(env, mutate) -> None:
    path = reuse_mod.record_path(env.ledger)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    record = envelope["record"]
    mutate(record)
    path.write_text(
        json.dumps(reuse_mod.sign_record(record, env.key.read_bytes())), encoding="utf-8"
    )


def test_verified_identity_equal_record_is_reused_without_running(env) -> None:
    _worker_run(env)
    counters, (rows, snapshot) = _finalize(env)
    assert counters.snapshots == 0 and counters.runs == 0
    assert snapshot["reused"] is True
    assert snapshot["reused_from"] == validation.WORKER_VALIDATION_RUN_REUSE_SOURCE
    assert rows and all(row["reused_from"] == "worker_validation_run" for row in rows)


def test_worker_lane_runs_on_combined_snapshot_and_writes_only_private_dir(env) -> None:
    counters = _worker_run(env)
    assert counters.paths and all(
        path.resolve() != env.ws.path.resolve() for path in counters.paths
    )
    record = reuse_mod.record_path(env.ledger)
    assert record.is_file() and record.parent == env.ledger.parent
    assert not list(env.ws.path.rglob(reuse_mod.RECORD_FILENAME))
    assert not list(Path(env.ws.home).rglob(reuse_mod.RECORD_FILENAME))


def test_worker_context_lane_finds_bound_metadata(env, monkeypatch) -> None:
    from aiworkhub.process_launcher import PROCESS_DIR_DEFAULT_REL

    process_dir = env.repo / PROCESS_DIR_DEFAULT_REL
    process_dir.mkdir(parents=True)
    (process_dir / f"{_REQUEST}.request.json").write_text(
        json.dumps(env.metadata), encoding="utf-8"
    )
    counters = _Counters()
    monkeypatch.setattr(
        reuse_mod, "_default_full_snapshot_runner", lambda: _real_runner(counters)
    )
    ctx = SimpleNamespace(
        audit_hmac_key_path=env.key, audit_ledger_path=env.ledger,
        request_id=_REQUEST, authority_repo=env.repo,
    )
    out = reuse_mod.run_for_worker_context(ctx, [_COMMAND], "d" * 64)
    assert out["ok"] is True and out["reuse_record_written"] is True
    assert out["results"][0]["lane"] == reuse_mod.FULL_SNAPSHOT_LANE
    assert counters.runs == 1
    other = SimpleNamespace(**{**vars(ctx), "audit_hmac_key_path": env.key.parent / "x"})
    assert reuse_mod.run_for_worker_context(other, [_COMMAND], "d")["ok"] is False


def test_forged_record_falls_back(env) -> None:
    _worker_run(env)
    path = reuse_mod.record_path(env.ledger)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["record"]["claim_epoch"] = 3
    envelope["hmac_sha256"] = "0" * 64
    path.write_text(json.dumps(envelope), encoding="utf-8")
    _assert_falls_back(env)


def test_unsigned_record_falls_back(env) -> None:
    _worker_run(env)
    path = reuse_mod.record_path(env.ledger)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({"record": envelope["record"]}), encoding="utf-8")
    _assert_falls_back(env)


def test_other_request_id_falls_back(env) -> None:
    _worker_run(env)
    _rewrite(env, lambda record: record.update(request_id="other-request"))
    _assert_falls_back(env)


def test_other_claim_epoch_falls_back(env) -> None:
    _worker_run(env)
    _assert_falls_back(env, {**env.metadata, "claim_epoch": 4})


def test_failed_row_falls_back(env) -> None:
    _worker_run(env, returncode=1)
    _assert_falls_back(env)


def test_row_count_mismatch_falls_back(env) -> None:
    _worker_run(env)
    _rewrite(env, lambda record: record["validations"].append(dict(record["validations"][0])))
    _assert_falls_back(env)


def test_candidate_edited_after_worker_run_falls_back(env) -> None:
    _worker_run(env)
    (env.ws.path / "pkg" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
    _assert_falls_back(env)


def test_sparse_worktree_record_falls_back(env) -> None:
    _worker_run(env)
    _rewrite(env, lambda record: record.update(lane="sparse_worktree"))
    _assert_falls_back(env)


def test_missing_record_falls_back(env) -> None:
    _assert_falls_back(env)


def test_finalization_wires_the_verified_record() -> None:
    from aiworkhub import process_launcher

    source = inspect.getsource(process_launcher.ProcessManager._finalize_isolated_request)
    assert "_launcher_validation.worker_validation_run_reuse(metadata)" in source


def _full(env, counters: _Counters, *, digest: str = "d" * 64, force: bool = False):
    return reuse_mod.run_worker_full_snapshot(
        metadata=env.metadata,
        commands=[_COMMAND],
        candidate_digest=digest,
        key_path=env.key,
        ledger_path=env.ledger,
        run_full_snapshot=_real_runner(counters),
        force=force,
    )


def test_unchanged_candidate_is_memoized_unless_forced_or_changed(env) -> None:
    counters = _Counters()
    first = _full(env, counters)
    assert first["ok"] is True and counters.runs == 1
    assert all(row["unchanged_since_last_run"] is False for row in first["results"])
    second = _full(env, counters)
    assert counters.runs == 1
    assert second["ok"] is True and second["memoized"] is True
    assert second["reuse_record_written"] is True
    assert second["tree_identity_sha256"] == first["tree_identity_sha256"]
    assert second["results"] and all(
        row["unchanged_since_last_run"] is True for row in second["results"]
    )
    assert reuse_mod.record_path(env.ledger).is_file()
    _full(env, counters, force=True)
    assert counters.runs == 2
    _full(env, counters, digest="e" * 64)
    assert counters.runs == 3


def test_unexpected_runner_exception_is_named_not_raised(env) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("/canonical/host/secret")

    out = reuse_mod.run_worker_full_snapshot(
        metadata=env.metadata, commands=[_COMMAND], candidate_digest="d" * 64,
        key_path=env.key, ledger_path=env.ledger, run_full_snapshot=boom,
    )
    assert out == {"ok": False, "reason": "full_snapshot_failed:RuntimeError"}


def test_workspace_error_reason_carries_no_exception_text(env) -> None:
    from aiworkhub.worker_workspace import WorkspaceError

    secret = "/canonical/host/private/path"

    def scope(_workspace):
        raise WorkspaceError(secret)

    out = reuse_mod.run_worker_full_snapshot(
        metadata=env.metadata, commands=[_COMMAND], candidate_digest="d" * 64,
        key_path=env.key, ledger_path=env.ledger, enforce_scope=scope,
        run_full_snapshot=_real_runner(_Counters()),
    )
    assert out["ok"] is False
    assert out["reason"] == "workspace_unavailable:WorkspaceError"
    assert secret not in out["reason"]


def test_request_id_must_be_hex_before_becoming_a_filename(tmp_path: Path) -> None:
    assert reuse_mod.metadata_candidates(tmp_path, "../x") == []
    assert reuse_mod.metadata_candidates(tmp_path, _REQUEST.upper()) == []
    assert reuse_mod.metadata_candidates(tmp_path, _REQUEST)
