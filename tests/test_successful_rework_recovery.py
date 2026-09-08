from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import attempt_artifacts, core, process_launcher, task_store, worker_workspace
from aiworkhub import successful_rework_recovery as recovery


@pytest.fixture
def episode(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "codex")
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", repo)
    request_id = "b" * 32
    task_id = "SUCCESSFUL_REWORK"
    runtime = repo / ".aiworkhub" / "runtime"
    workspace = runtime / "worktrees" / request_id / "worktree"
    candidate = workspace / "src" / "consumer.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"exact candidate\n")
    workspace_meta = {"request_id": request_id, "repo": str(repo), "path": str(workspace)}
    # Real pre-NF697 attempt metadata has only these four identity fields.
    identity = {"request_id": request_id, "task_id": task_id, "runner": "worker", "topic": "rework"}
    hashes = {"src/consumer.py": hashlib.sha256(candidate.read_bytes()).hexdigest()}
    payloads = {
        "metadata": {"schema_id": "aiworkhub.attempt_metadata.v1", "request_identity": identity, "workspace": workspace_meta},
        "diff": {"schema_id": "aiworkhub.attempt_diff_index.v1", "changed_paths": sorted(hashes), "changed_path_hashes": hashes, "required_outputs": []},
        "validation": {"schema_id": "aiworkhub.attempt_validation.v1", "checks": []},
        "usage": {"schema_id": "aiworkhub.attempt_usage.v1"},
        "review": {"schema_id": "aiworkhub.attempt_review.v1", "kind": "worker_candidate", "target_state": "review_ready", "error": ""},
    }
    bundle = runtime / "process_logs" / "processes" / "attempt-artifacts" / request_id
    receipt = attempt_artifacts.persist_json_bundle(bundle, attempt_id=request_id, payloads=payloads)
    evidence = {"request_identity": identity, "workspace": workspace_meta, "changed_path_hashes": hashes, "changed_paths": sorted(hashes), "attempt_artifact_manifest": receipt}
    terminal = {"request_id": request_id, "claim_epoch": 3, "runner": "worker", "substatus": "review_ready", "evidence": evidence}
    card = {"task_id": task_id, "runner": "worker", "topic": "rework", "claim_epoch": 3, "launch_request_id": request_id,
            "allowed_writes": sorted(hashes), "terminal_review": terminal, "terminal_substatus": "review_ready"}
    db = Path(task_store._require_ready(repo)[1])
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, objective, card_json, created_at, updated_at) "
            "VALUES (?, 'worker', 'rework', 'review', 'review', '', '', ?, 'now', 'now')",
            (task_id, json.dumps(card)),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) VALUES (?, 'terminal_review', 'worker', ?, 'now')",
            (task_id, json.dumps(terminal)),
        )
    return SimpleNamespace(repo=repo, db=db, request_id=request_id, task_id=task_id, runtime=runtime,
                           workspace=workspace, candidate=candidate, evidence=evidence, terminal=terminal,
                           card=card, bundle=bundle, payloads=payloads)


def _seal(episode, **kwargs):
    return recovery.recover_descriptor(
        episode.repo, episode.task_id, episode.request_id, kwargs.pop("claim_epoch", 3),
        episode.evidence, terminal_episode=episode.terminal, **kwargs,
    )


def _snapshot(episode):
    with sqlite3.connect(episode.db) as conn:
        return (
            conn.execute("SELECT * FROM tasks WHERE task_id=?", (episode.task_id,)).fetchone(),
            conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY rowid", (episode.task_id,)).fetchall(),
        )


def _replace_card(episode, change):
    with sqlite3.connect(episode.db) as conn:
        card = json.loads(conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (episode.task_id,)).fetchone()[0])
        change(card)
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), episode.task_id))


def _republish(episode):
    episode.payloads["metadata"]["workspace"] = episode.evidence["workspace"]
    episode.payloads["diff"]["changed_path_hashes"] = episode.evidence["changed_path_hashes"]
    episode.payloads["diff"]["changed_paths"] = sorted(episode.evidence["changed_path_hashes"])
    episode.evidence["changed_paths"] = sorted(episode.evidence["changed_path_hashes"])
    episode.evidence["attempt_artifact_manifest"] = attempt_artifacts.persist_json_bundle(
        episode.bundle, attempt_id=episode.request_id, payloads=episode.payloads,
    )


def test_recovery_authenticates_legacy_epoch_and_materializes_exact_bytes(episode, tmp_path):
    descriptor = _seal(episode)
    target = tmp_path / "materialized"
    target.mkdir()
    worker_workspace.materialize_rework_delta_artifact(
        {"path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"]},
        episode.repo, episode.request_id, episode.task_id, 3, target,
        episode.evidence["changed_path_hashes"], ("src/consumer.py",),
    )
    assert (target / "src/consumer.py").read_bytes() == b"exact candidate\n"
    assert descriptor["claim_epoch"] == 3


@pytest.mark.parametrize("tamper", ["candidate", "manifest", "repo", "request", "claim", "episode_claim", "runner", "outcome"])
def test_recovery_rejects_tampering_before_sealing(episode, monkeypatch, tamper):
    calls = []
    monkeypatch.setattr(worker_workspace, "seal_rework_delta_artifact", lambda *args: calls.append(args))
    claim = 3
    if tamper == "candidate":
        episode.candidate.write_bytes(b"tampered\n")
    elif tamper == "manifest":
        (episode.bundle / "manifest.json").write_text("{}")
    elif tamper in {"repo", "request", "runner"}:
        episode.evidence["request_identity"][{"repo": "repo", "request": "request_id", "runner": "runner"}[tamper]] = "wrong"
    elif tamper == "claim":
        claim = 999
    elif tamper == "episode_claim":
        episode.terminal["claim_epoch"] = 999
    else:
        episode.payloads["review"]["target_state"] = "validation_failed"
        _republish(episode)
    with pytest.raises(recovery.SuccessfulReworkRecoveryError):
        _seal(episode, claim_epoch=claim)
    assert calls == []


def test_native_blocked_shape_recovers_current_episode_and_preserves_history(episode):
    rejected = core.reject_review(episode.task_id, "Add missing initializer", to="blocked")
    assert rejected["ok"], rejected
    blocked = task_store.get_task(episode.repo, episode.task_id)
    assert "terminal_review" not in blocked
    before_events = _snapshot(episode)[1]
    assert task_store.recover_blocked_rework(episode.repo, episode.task_id, feedback_reason="Add missing initializer") == (True, "recovered")
    card = task_store.get_task(episode.repo, episode.task_id)
    assert card["status"] == "pending"
    assert card["claim_epoch"] == 4
    assert card["rework_predecessor"]["request_id"] == episode.request_id
    assert card["rework_predecessor"]["claim_epoch"] == 3
    assert card["rework_predecessor"]["changed_path_hashes"] == episode.evidence["changed_path_hashes"]
    after_events = _snapshot(episode)[1]
    assert after_events[:-1] == before_events
    assert json.loads(after_events[-1][4])["successful_rework_delta"]["sealed"] is True
    again = _snapshot(episode)
    assert task_store.recover_blocked_rework(episode.repo, episode.task_id, feedback_reason="Add missing initializer") == (True, "already_recovered")
    assert _snapshot(episode) == again


def test_review_rejection_recovers_the_exact_current_successful_payload(episode):
    before = _snapshot(episode)[1]
    result = core.reject_review(episode.task_id, "Fix current candidate", predecessor_request_id=episode.request_id)
    assert result["ok"], result.get("stderr", result)
    card = task_store.get_task(episode.repo, episode.task_id)
    assert card["status"] == "pending"
    assert card["rework_predecessor"]["rework_delta"]["request_id"] == episode.request_id
    assert card["rework_predecessor"]["claim_epoch"] == 3
    assert _snapshot(episode)[1][:len(before)] == before


@pytest.mark.parametrize("field,value", [("launch_request_id", "c" * 32), ("claim_epoch", 999)])
def test_blocked_recovery_rejects_stale_identity_without_mutation(episode, monkeypatch, field, value):
    assert core.reject_review(episode.task_id, "Fix", to="blocked")["ok"]
    _replace_card(episode, lambda card: card.update({field: value}))
    before = _snapshot(episode)
    calls = []
    monkeypatch.setattr(worker_workspace, "seal_rework_delta_artifact", lambda *args: calls.append(args))
    assert task_store.recover_blocked_rework(episode.repo, episode.task_id, feedback_reason="Fix")[0] is False
    assert _snapshot(episode) == before
    assert calls == []


@pytest.mark.parametrize("flow", ["review", "blocked"])
@pytest.mark.parametrize("change", ["request", "epoch", "card"])
def test_card_preimage_cas_rejects_change_during_retained_io(episode, monkeypatch, flow, change):
    if flow == "blocked":
        assert core.reject_review(episode.task_id, "Fix", to="blocked")["ok"]
    before_events = _snapshot(episode)[1]
    seal = worker_workspace.seal_rework_delta_artifact
    concurrent = []

    def racing_seal(*args):
        result = seal(*args)
        field, value = {"request": ("launch_request_id", "c" * 32), "epoch": ("claim_epoch", 4), "card": ("title", "Concurrent revision")}[change]
        _replace_card(episode, lambda card: card.update({field: value}))
        concurrent.append(_snapshot(episode)[0])
        return result

    monkeypatch.setattr(worker_workspace, "seal_rework_delta_artifact", racing_seal)
    if flow == "review":
        result = core.reject_review(episode.task_id, "Fix", predecessor_request_id=episode.request_id)
        assert result["ok"] is False
    else:
        assert task_store.recover_blocked_rework(episode.repo, episode.task_id, feedback_reason="Fix") == (False, "successful_rework_episode_changed")
    assert _snapshot(episode) == (concurrent[0], before_events)


def test_manager_gate_precedes_artifact_mutation(episode, monkeypatch):
    calls = []
    blocked = {"ok": False, "stderr": "manager denied"}
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: blocked)
    monkeypatch.setattr(worker_workspace, "seal_rework_delta_artifact", lambda *args: calls.append(args))
    before = _snapshot(episode)
    assert core.reject_review(episode.task_id, "Fix", predecessor_request_id=episode.request_id) == blocked
    assert calls == [] and _snapshot(episode) == before


def test_external_runtime_root_is_supported(episode, tmp_path, monkeypatch):
    external = tmp_path / "external-runtime"
    episode.runtime.rename(external)
    episode.workspace = external / "worktrees" / episode.request_id / "worktree"
    episode.bundle = external / "process_logs" / "processes" / "attempt-artifacts" / episode.request_id
    episode.evidence["workspace"]["path"] = str(episode.workspace)
    monkeypatch.setattr(worker_workspace, "configured_runtime_root", lambda repo: external)
    _republish(episode)
    descriptor = _seal(episode)
    assert Path(descriptor["artifact_path"]).is_relative_to(external)


def test_verified_artifact_is_parsed_from_same_bytes(episode, monkeypatch):
    read = recovery._read_regular
    reads = []

    def substitute_after_read(path, root, limit, **kwargs):
        data = read(path, root, limit, **kwargs)
        reads.append(path)
        if path == episode.bundle / "review.json":
            path.write_text('{"target_state":"validation_failed"}')
        return data

    monkeypatch.setattr(recovery, "_read_regular", substitute_after_read)
    # The verified snapshot is still valid; replacing the disk path after its
    # bounded read cannot change the payload subsequently parsed.
    assert _seal(episode)["sealed"] is True
    assert reads.count(episode.bundle / "review.json") == 1
    with pytest.raises(recovery.SuccessfulReworkRecoveryError):
        _seal(episode)


def test_oversize_candidate_is_rejected_before_content_read(episode, monkeypatch):
    with episode.candidate.open("wb") as stream:
        stream.truncate(worker_workspace.MAX_REWORK_OVERLAY_CONTENT_BYTES + 1)
    calls = []
    monkeypatch.setattr(worker_workspace, "seal_rework_delta_artifact", lambda *args: calls.append(args))
    with pytest.raises(recovery.SuccessfulReworkRecoveryError, match="content_too_large"):
        _seal(episode)
    assert calls == []


def test_oversize_manifest_bound_artifact_is_rejected(episode):
    episode.payloads["review"]["padding"] = "x" * (worker_workspace.MAX_REWORK_OVERLAY_CONTENT_BYTES + 1)
    _republish(episode)
    with pytest.raises(recovery.SuccessfulReworkRecoveryError, match="content_too_large"):
        _seal(episode)


@pytest.mark.parametrize("replacement", ["absent", "file", "directory", "symlink"])
def test_authenticated_deletion_is_preserved_and_replacements_fail_closed(episode, tmp_path, replacement):
    relative = "src/deleted.py"
    episode.evidence["changed_path_hashes"][relative] = None
    _republish(episode)
    deleted = episode.workspace / relative
    if replacement == "file":
        deleted.write_text("unexpected")
    elif replacement == "directory":
        deleted.mkdir()
    elif replacement == "symlink":
        deleted.symlink_to(tmp_path / "missing")
    if replacement != "absent":
        with pytest.raises(recovery.SuccessfulReworkRecoveryError):
            _seal(episode)
        return
    descriptor = _seal(episode)
    target = tmp_path / "materialized"
    (target / "src").mkdir(parents=True)
    (target / relative).write_text("canonical old file")
    worker_workspace.materialize_rework_delta_artifact(
        {"path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"]},
        episode.repo, episode.request_id, episode.task_id, 3, target,
        episode.evidence["changed_path_hashes"], tuple(episode.evidence["changed_path_hashes"]),
    )
    assert not (target / relative).exists()
    assert (target / "src/consumer.py").read_bytes() == b"exact candidate\n"


def test_successful_two_generation_payload_is_independent_of_prior_seal(episode, tmp_path):
    prior = _seal(episode)
    second_request = "c" * 32
    second = tmp_path / "second"
    (second / "src").mkdir(parents=True)
    (second / "src/consumer.py").write_bytes(b"exact candidate\n")
    (second / "src/new.py").write_bytes(b"new generation\n")
    metadata = {"task_id": episode.task_id, "claim_epoch": 4, "rework_predecessor": {"changed_path_hashes": episode.evidence["changed_path_hashes"]}}
    paths, hashes, descriptor = recovery.successful_candidate_evidence(
        SimpleNamespace(repo=episode.repo, path=second), metadata, second_request, ["src/new.py"],
    )
    assert paths == ["src/consumer.py", "src/new.py"]
    Path(prior["artifact_path"]).unlink()
    target = tmp_path / "third"
    target.mkdir()
    worker_workspace.materialize_rework_delta_artifact(
        {"path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"]},
        episode.repo, second_request, episode.task_id, 4, target, hashes, tuple(paths),
    )
    assert (target / "src/consumer.py").read_bytes() == b"exact candidate\n"
    assert (target / "src/new.py").read_bytes() == b"new generation\n"


@pytest.mark.parametrize("sealed", [None, {"sealed": False}, {"sealed": True, "request_id": "wrong"}])
def test_successful_publication_refuses_failed_or_wrong_seal(episode, monkeypatch, sealed):
    monkeypatch.setattr(process_launcher, "_terminal_rework_delta_evidence", lambda *args, **kwargs: deepcopy(sealed))
    with pytest.raises(worker_workspace.WorkspaceError, match="successful_rework_delta_missing"):
        recovery.successful_candidate_evidence(
            SimpleNamespace(repo=episode.repo, path=episode.workspace),
            {"task_id": episode.task_id, "claim_epoch": 3}, episode.request_id, ["src/consumer.py"],
        )


def test_default_review_rejection_also_seals_current_success(episode):
    result = core.reject_review(episode.task_id, "Fix current candidate")
    assert result["ok"], result
    card = task_store.get_task(episode.repo, episode.task_id)
    assert card["rework_predecessor"]["rework_delta"]["sealed"] is True
    assert card["rework_predecessor"]["request_id"] == episode.request_id


def test_invalid_successful_seal_cannot_silently_reset_to_clean_root(episode):
    _replace_card(episode, lambda card: card["terminal_review"]["evidence"].update(rework_delta={"sealed": False}))
    before = _snapshot(episode)
    result = core.reject_review(episode.task_id, "Fix current candidate")
    assert result["ok"] is False
    assert "successful_rework_invalid_delta" in result["stderr"]
    assert _snapshot(episode) == before


def test_recovery_without_feedback_creates_no_artifact_or_task_change(episode, monkeypatch):
    assert core.reject_review(episode.task_id, "Fix", to="blocked")["ok"]
    _replace_card(episode, lambda card: card.pop("reject_review", None))
    before = _snapshot(episode)
    calls = []
    monkeypatch.setattr(worker_workspace, "seal_rework_delta_artifact", lambda *args: calls.append(args))
    assert task_store.recover_blocked_rework(episode.repo, episode.task_id) == (False, "no_residual_feedback")
    assert calls == [] and _snapshot(episode) == before


@pytest.mark.parametrize("swap_before", ["parent_open", "file_open"])
def test_parent_swap_uses_pinned_directory_or_fails_closed(episode, tmp_path, monkeypatch, swap_before):
    if recovery.platform_io.is_windows():
        pytest.skip("POSIX openat race; Windows relative HANDLE path is tested separately")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "consumer.py").write_bytes(b"outside fixture bytes\n")
    parent = episode.candidate.parent
    original_open = recovery.os.open
    swapped = []

    def racing_open(path, flags, *args, **kwargs):
        trigger = "src" if swap_before == "parent_open" else "consumer.py"
        if path == trigger and "dir_fd" in kwargs and not swapped:
            parent.rename(episode.workspace / "src.original")
            parent.symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(recovery.os, "open", racing_open)
    if swap_before == "parent_open":
        with pytest.raises(recovery.SuccessfulReworkRecoveryError):
            recovery.capture_candidate_paths(episode.workspace, ["src/consumer.py"])
    else:
        assert recovery.capture_candidate_paths(episode.workspace, ["src/consumer.py"]) == [
            ("src/consumer.py", b"exact candidate\n")
        ]
    assert swapped == [True]


@pytest.mark.parametrize("replacement", ["symlink", "hardlink"])
def test_final_file_substitution_is_rejected_on_opened_identity(episode, tmp_path, monkeypatch, replacement):
    if recovery.platform_io.is_windows():
        pytest.skip("POSIX race injection; Windows HANDLE primitive is tested separately")
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside fixture bytes\n")
    original_open = recovery.os.open
    swapped = []

    def racing_open(path, flags, *args, **kwargs):
        if path == "consumer.py" and "dir_fd" in kwargs and not swapped:
            episode.candidate.unlink()
            if replacement == "symlink":
                episode.candidate.symlink_to(outside)
            else:
                recovery.os.link(outside, episode.candidate)
            swapped.append(True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(recovery.os, "open", racing_open)
    with pytest.raises(recovery.SuccessfulReworkRecoveryError):
        recovery.capture_candidate_paths(episode.workspace, ["src/consumer.py"])
    assert swapped == [True]


def test_parent_descriptor_chain_closes_after_failure(episode, monkeypatch):
    if recovery.platform_io.is_windows():
        pytest.skip("POSIX descriptor ownership")
    original_open = recovery.os.open
    opened = []

    def opening(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(recovery.os, "open", opening)
    with pytest.raises(recovery.SuccessfulReworkRecoveryError, match="content_too_large"):
        recovery._read_regular(episode.candidate, episode.workspace, 1)
    assert len(opened) > 2
    for fd in opened:
        with pytest.raises(OSError):
            recovery.os.fstat(fd)


def test_windows_reader_traverses_relative_handles_and_closes_them(episode, monkeypatch):
    calls, closes = [], []
    next_handle = iter(range(100, 200))

    class Authority:
        def __init__(self, handle):
            self.handle = self.value = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closes.append(self.handle)

    def root_authority(path):
        calls.append(("root", path))
        return Authority(next(next_handle))

    def directory(parent, name):
        handle = next(next_handle)
        calls.append(("directory", parent, name, handle))
        return Authority(handle)

    opened = []

    def file_descriptor(parent, name):
        calls.append(("file", parent, name))
        fd = recovery.os.open(episode.candidate, recovery.os.O_RDONLY)
        opened.append(fd)
        return fd

    monkeypatch.setattr(recovery.platform_io, "is_windows", lambda: True)
    monkeypatch.setattr(recovery.runtime_temp, "WindowsDirectoryAuthority", root_authority)
    monkeypatch.setattr(recovery.platform_io, "open_windows_relative_child_directory", directory)
    monkeypatch.setattr(recovery.platform_io, "open_windows_relative_regular_file_descriptor", file_descriptor)
    assert recovery._read_regular(episode.candidate, episode.workspace, 100) == b"exact candidate\n"
    assert [item[2] for item in calls if item[0] == "directory"] == list(episode.candidate.parts[1:-1])
    assert calls[-1] == ("file", calls[-2][3], "consumer.py")
    assert closes == list(reversed([100] + [item[3] for item in calls if item[0] == "directory"]))
    with pytest.raises(OSError):
        recovery.os.fstat(opened[0])
