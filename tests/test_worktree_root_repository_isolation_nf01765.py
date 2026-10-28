"""NF-2026-01765: a worktree root owned by another registered repository.

Measured 2026-10-27: a transient-retry relaunch was provisioned under
``F:/Dev/QT/Mego/MegoProject/.aiworkhub/runtime/worktrees`` because the
launching process exported another registered repository's
AIWORKHUB_RUNTIME_ROOT / AIWORKHUB_WORKTREE_ROOT, and the card was then stuck:
every rework relaunch refused ``gc_workspace_shape_mismatch`` before it ever
consulted the predecessor's sealed delta.

``configured_worktree_root`` now refuses an override another repository owns
(``worktree_root_foreign_repository``), and a predecessor stranded under such a
root relaunches from its verified sealed delta without its tree being read.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, task_store, worker_workspace

OUTPUT = "out/result.txt"
BASE_CONTENT = b"canonical base\n"
CANDIDATE = b"sealed predecessor candidate\n"
FOREIGN_BYTES = b"bytes only the foreign tree holds\n"
CANDIDATE_HASH = hashlib.sha256(CANDIDATE).hexdigest()
TASK_ID = "needfix-NF-2026-01765-isolation"
PREDECESSOR = "d1765aa0d1765aa0d1765aa0d1765aa0"
SUCCESSOR = "e1765bb0e1765bb0e1765bb0e1765bb0"
CLAIM_EPOCH = 3
REPO_ID = "repo_01765000000000000000000000000a"
FOREIGN_ID = "repo_01765000000000000000000000000f"
REFUSAL = worker_workspace.FOREIGN_WORKTREE_ROOT_REFUSAL


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _register(root: Path, repo_id: str | None) -> Path:
    """Write ``root``'s tracked project.json; ``None`` writes no repo_id."""
    hub = root / ".aiworkhub"
    hub.mkdir(parents=True, exist_ok=True)
    payload = {} if repo_id is None else {"repo_id": repo_id}
    (hub / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    return root.resolve()


def _runtime_worktrees(root: Path) -> Path:
    return (root / ".aiworkhub" / "runtime" / "worktrees").resolve(strict=False)


@pytest.fixture(autouse=True)
def _no_ambient_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        worker_workspace.WORKTREE_ROOT_ENV,
        worker_workspace.RUNTIME_ROOT_ENV,
        "AIWORKHUB_REPO_ROOT",
        "AIWORKHUB_REPO",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "NF-2026-01765 Tests")
    (root / "out").mkdir()
    (root / OUTPUT).write_bytes(BASE_CONTENT)
    _git(root, "add", OUTPUT)
    _git(root, "commit", "-qm", "baseline")
    task_store.initialize_repository(root)
    return root


# --- configured_worktree_root ------------------------------------------------


@pytest.mark.parametrize("env", [worker_workspace.WORKTREE_ROOT_ENV, "runtime"])
def test_override_owned_by_another_repository_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    own = _register(tmp_path / "own", REPO_ID)
    foreign = _register(tmp_path / "foreign", FOREIGN_ID)
    if env == "runtime":
        monkeypatch.setenv(
            worker_workspace.RUNTIME_ROOT_ENV, str(foreign / ".aiworkhub" / "runtime")
        )
    else:
        monkeypatch.setenv(env, str(_runtime_worktrees(foreign)))

    with pytest.raises(worker_workspace.WorkspaceError) as refused:
        worker_workspace.configured_worktree_root(own)

    assert str(refused.value) == (
        f"{REFUSAL}:root={_runtime_worktrees(foreign)}:owner={foreign}:repo={own}"
    )


def test_nested_repository_under_an_enclosing_runtime_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _register(tmp_path / "outer", FOREIGN_ID)
    inner = _register(outer / ".aiworkhub" / "runtime" / "scratch" / "inner", REPO_ID)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(_runtime_worktrees(outer)))

    with pytest.raises(worker_workspace.WorkspaceError, match=f"^{REFUSAL}:"):
        worker_workspace.configured_worktree_root(inner)

    # The nested repository's own runtime stays its own.
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(_runtime_worktrees(inner)))
    assert worker_workspace.configured_worktree_root(inner) == _runtime_worktrees(inner)


@pytest.mark.parametrize("damage", ["empty_repo_id", "unreadable"])
def test_unknown_repository_identity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    own = _register(tmp_path / "own", None)
    foreign = _register(tmp_path / "foreign", None)
    if damage == "unreadable":
        (foreign / ".aiworkhub" / "project.json").write_bytes(b"{not json")
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(_runtime_worktrees(foreign)))

    with pytest.raises(worker_workspace.WorkspaceError, match=f"^{REFUSAL}:"):
        worker_workspace.configured_worktree_root(own)


@pytest.mark.parametrize("env", [worker_workspace.WORKTREE_ROOT_ENV, "runtime"])
def test_owner_and_same_repo_id_worker_worktree_keep_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    owner = _register(tmp_path / "owner", REPO_ID)
    worker = _register(tmp_path / "elsewhere" / "worktree", REPO_ID)
    if env == "runtime":
        monkeypatch.setenv(
            worker_workspace.RUNTIME_ROOT_ENV, str(owner / ".aiworkhub" / "runtime")
        )
    else:
        monkeypatch.setenv(env, str(_runtime_worktrees(owner)))

    assert worker_workspace.configured_worktree_root(owner) == _runtime_worktrees(owner)
    assert worker_workspace.configured_worktree_root(worker) == _runtime_worktrees(owner)


@pytest.mark.parametrize("identity_env", ["AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO"])
def test_neutral_override_keeps_precedence_whatever_the_hub_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity_env: str
) -> None:
    own = _register(tmp_path / "own", REPO_ID)
    foreign = _register(tmp_path / "foreign", FOREIGN_ID)
    neutral = (tmp_path / "neutral-worktrees").resolve()
    monkeypatch.setenv(identity_env, str(foreign))
    monkeypatch.chdir(foreign)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(neutral))

    assert worker_workspace.configured_worktree_root(own) == neutral
    assert worker_workspace.configured_worktree_root(None) == neutral


def test_launch_under_a_foreign_root_provisions_nothing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = _register(tmp_path / "foreign", FOREIGN_ID)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(_runtime_worktrees(foreign)))

    with pytest.raises(worker_workspace.WorkspaceError, match=f"^{REFUSAL}:"):
        worker_workspace.create_workspace(
            repo, SUCCESSOR, {"allowed_writes": [OUTPUT]}, "deterministic_validation"
        )

    assert not _runtime_worktrees(foreign).exists()
    assert not _runtime_worktrees(repo).exists()


def test_neutral_override_launches_with_a_foreign_repo_root_exported(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = _register(tmp_path / "foreign", FOREIGN_ID)
    neutral = (tmp_path / "neutral-worktrees").resolve()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(foreign))
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(neutral))

    workspace = worker_workspace.create_workspace(
        repo, SUCCESSOR, {"allowed_writes": [OUTPUT]}, "deterministic_validation"
    )

    assert workspace.path == neutral / SUCCESSOR / "worktree"
    assert not _runtime_worktrees(foreign).exists()


def test_promotion_marker_is_best_effort_under_a_foreign_root(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    neutral = (tmp_path / "neutral-worktrees").resolve()
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(neutral))
    workspace = worker_workspace.create_workspace(
        repo, SUCCESSOR, {"allowed_writes": [OUTPUT]}, "deterministic_validation"
    )
    foreign = _register(tmp_path / "foreign", FOREIGN_ID)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(_runtime_worktrees(foreign)))

    # The refusal skips the marker; it never blocks the promotion itself.
    assert worker_workspace._promotion_begin(workspace) is None
    assert not _runtime_worktrees(foreign).exists()


def test_gc_shape_treats_a_refused_configured_root_as_no_candidate(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = _register(tmp_path / "foreign", FOREIGN_ID)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(_runtime_worktrees(foreign)))
    default = _runtime_worktrees(repo)

    assert worker_workspace.assert_gc_safe_workspace_shape(
        PREDECESSOR,
        default / PREDECESSOR / "worktree",
        default / PREDECESSOR / "home",
        repo=repo,
    ) == default
    stranded = _runtime_worktrees(foreign) / PREDECESSOR
    with pytest.raises(
        worker_workspace.WorkspaceError, match="^gc_workspace_shape_mismatch:"
    ):
        worker_workspace.assert_gc_safe_workspace_shape(
            PREDECESSOR, stranded / "worktree", stranded / "home", repo=repo
        )


# --- a rework predecessor stranded under a foreign root ----------------------


def _seal_delta(repo: Path, entries: dict[str, bytes]) -> dict[str, Any]:
    """Seal ``entries`` as ``seal_rework_delta_artifact`` does, minus its POSIX
    mode hardening (refused by chmod-denying validation sandboxes)."""
    payload = {
        "schema_id": worker_workspace.REWORK_DELTA_ARTIFACT_SCHEMA_ID,
        "authority_repo": str(repo.resolve(strict=False)),
        "task_id": TASK_ID,
        "request_id": PREDECESSOR,
        "claim_epoch": CLAIM_EPOCH,
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
            for path, data in sorted(entries.items())
        ],
    }
    packet = {
        **payload,
        "canonical_digest": worker_workspace._rework_delta_canonical_digest(payload),
    }
    encoded = json.dumps(packet, indent=2, ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    artifact_dir = worker_workspace.configured_runtime_root(repo) / "rework_deltas"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / f"{digest}.json"
    artifact.write_bytes(encoded)
    return {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(repo.resolve(strict=False)),
        "task_id": TASK_ID,
        "request_id": PREDECESSOR,
        "claim_epoch": CLAIM_EPOCH,
        "artifact_path": str(artifact),
        "artifact_sha256": digest,
    }


def _stranded_predecessor(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, sealed: bool
) -> tuple[dict[str, Any], Path]:
    """Provision the predecessor under a root that only later turns out to be
    another registered repository's, as the measured relaunch was; its tree
    then holds bytes the sealed delta does not."""
    foreign_root = tmp_path / "foreign"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(_runtime_worktrees(foreign_root)))
    workspace = worker_workspace.create_workspace(
        repo, PREDECESSOR, {"allowed_writes": [OUTPUT]}, "deterministic_validation"
    )
    (workspace.path / OUTPUT).write_bytes(FOREIGN_BYTES)
    predecessor: dict[str, Any] = {
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "request_id": PREDECESSOR,
        "task_id": TASK_ID,
        "claim_epoch": CLAIM_EPOCH,
        "workspace": workspace.as_metadata(),
        "changed_path_hashes": {OUTPUT: CANDIDATE_HASH},
    }
    if sealed:
        descriptor = _seal_delta(repo, {OUTPUT: CANDIDATE})
        predecessor["rework_delta"] = descriptor
        predecessor["delta_artifact"] = {
            "path": descriptor["artifact_path"],
            "digest": descriptor["artifact_sha256"],
        }
    _register(foreign_root, FOREIGN_ID)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str((tmp_path / "neutral-worktrees").resolve())
    )
    return predecessor, workspace.path


def _forbid_foreign_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the foreign predecessor tree was read")

    for name in (
        "_materialize_rework_predecessor_from_worktree",
        "is_workspace_husk",
        "changed_paths",
    ):
        monkeypatch.setattr(worker_workspace, name, refuse)


def _rework_card(predecessor: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "claim_epoch": CLAIM_EPOCH + 1,
        "allowed_writes": [OUTPUT],
        "required_outputs": [OUTPUT],
        "rework_predecessor": predecessor,
    }


def test_reproduction_stranded_predecessor_relaunches_from_its_sealed_delta(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    predecessor, stranded = _stranded_predecessor(
        repo, tmp_path, monkeypatch, sealed=True
    )
    # Before the fix the shape check ran ahead of the sealed delta.
    with pytest.raises(
        worker_workspace.WorkspaceError, match="^gc_workspace_shape_mismatch:"
    ):
        worker_workspace.assert_gc_safe_workspace_shape(
            PREDECESSOR,
            stranded,
            Path(predecessor["workspace"]["home"]),
            repo=repo,
        )
    _forbid_foreign_reads(monkeypatch)

    successor = worker_workspace.create_workspace(
        repo, SUCCESSOR, _rework_card(predecessor), "deterministic_validation"
    )

    assert (successor.path / OUTPUT).read_bytes() == CANDIDATE
    assert successor.path == (tmp_path / "neutral-worktrees").resolve() / SUCCESSOR / "worktree"
    assert (stranded / OUTPUT).read_bytes() == FOREIGN_BYTES


def test_reroute_verifies_a_stranded_predecessor_from_its_sealed_delta(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    predecessor, _stranded = _stranded_predecessor(
        repo, tmp_path, monkeypatch, sealed=True
    )
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    _forbid_foreign_reads(monkeypatch)

    receipt, error = core._verified_retained_predecessor_receipt(
        _rework_card(predecessor), task_id=TASK_ID
    )

    assert error is None
    assert receipt is not None and receipt["retained_candidate_preserved"] is True
    assert receipt["retained_predecessor_request_id"] == PREDECESSOR


@pytest.mark.parametrize("damage", ["delta_absent", "artifact_removed"])
def test_stranded_predecessor_without_a_verified_delta_keeps_the_refusal(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    predecessor, stranded = _stranded_predecessor(
        repo, tmp_path, monkeypatch, sealed=damage != "delta_absent"
    )
    if damage == "artifact_removed":
        Path(predecessor["rework_delta"]["artifact_path"]).unlink()
    monkeypatch.setattr(core, "repo_root", lambda: repo)

    with pytest.raises(
        worker_workspace.WorkspaceError, match="^gc_workspace_shape_mismatch:"
    ):
        worker_workspace.create_workspace(
            repo, SUCCESSOR, _rework_card(predecessor), "deterministic_validation"
        )
    assert core._verified_retained_predecessor_receipt(
        _rework_card(predecessor), task_id=TASK_ID
    ) == (None, "reroute_retained_candidate_workspace_invalid")
    assert (stranded / OUTPUT).read_bytes() == FOREIGN_BYTES
