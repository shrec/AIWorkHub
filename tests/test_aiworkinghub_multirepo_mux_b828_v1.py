from __future__ import annotations

import json
from pathlib import Path

from geoai_task_mcp.repository_mux import RepositoryDescriptor, RepositoryWindowMux
from geoai_task_mcp.repository_state import bootstrap_repository, inspect_repository


def _repo(tmp_path: Path, name: str, repo_id: str):
    root = tmp_path / name
    root.mkdir()
    return bootstrap_repository(
        root,
        repo_id=repo_id,
        repo_name=name,
        created_at="2026-07-20T00:00:00+00:00",
    )


def test_composite_identity_prevents_same_thread_task_collision(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path, "alpha", "repo_alpha000000000000000000000000001")
    repo_b = _repo(tmp_path, "beta", "repo_beta0000000000000000000000000002")
    mux = RepositoryWindowMux()
    win_a = "window_a"
    win_b = "window_b"

    mux.open_repository(win_a, repo_a, claim_episode="episode_same_claim_0001")
    mux.open_repository(win_b, repo_b, claim_episode="episode_same_claim_0001")

    left = mux.route_identity(win_a, "thread.same", "TASK.SAME")
    right = mux.route_identity(win_b, "thread.same", "TASK.SAME")

    assert left.thread_id == right.thread_id == "thread.same"
    assert left.task_id == right.task_id == "TASK.SAME"
    assert left.repo_id != right.repo_id
    assert left.canonical() != right.canonical()
    assert left.digest() != right.digest()


def test_each_window_owns_one_child_and_switch_drains_old_session(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path, "alpha", "repo_alpha000000000000000000000000001")
    repo_b = _repo(tmp_path, "beta", "repo_beta0000000000000000000000000002")
    mux = RepositoryWindowMux()

    first = mux.open_repository("window_a", repo_a, claim_episode="episode_first_000000000001")
    same = mux.open_repository("window_a", repo_a, claim_episode="episode_ignored_0000000002")
    assert same is first
    assert not first.stopped

    second = mux.open_repository("window_a", repo_b, claim_episode="episode_second_00000000002")

    assert first.stopped
    assert second is mux.active_session("window_a")
    assert second.child_id != first.child_id
    assert mux.stopped_children == (first.child_id,)
    assert second.repository.repo_id == repo_b.manifest.repo_id


def test_multi_root_requires_explicit_selection_and_reports_storage_ready(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path, "alpha", "repo_alpha000000000000000000000000001")
    repo_b = _repo(tmp_path, "beta", "repo_beta0000000000000000000000000002")

    state_a = inspect_repository(repo_a.root)
    state_b = inspect_repository(repo_b.root)
    desc_a = RepositoryDescriptor.from_state(state_a)
    desc_b = RepositoryDescriptor.from_state(state_b)

    assert desc_a.display() == {
        "repo_id": "repo_alpha000000000000000000000000001",
        "repo_name": "alpha",
        "storage_ready": True,
    }
    assert desc_b.display()["repo_id"] != desc_a.display()["repo_id"]
    assert desc_b.display()["storage_ready"] is True


def test_callbacks_remain_durable_for_origin_after_window_close(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path, "alpha", "repo_alpha000000000000000000000000001")
    repo_b = _repo(tmp_path, "beta", "repo_beta0000000000000000000000000002")
    mux = RepositoryWindowMux()
    mux.open_repository("window_a", repo_a, claim_episode="episode_origin_00000000001")
    mux.open_repository("window_b", repo_b, claim_episode="episode_origin_00000000001")

    route_a = mux.persist_callback(
        "window_a",
        "thread.same",
        "TASK.SAME",
        "callback.done",
        {"claim_episode": "episode_origin_00000000001", "repo": "alpha"},
    )
    route_b = mux.persist_callback(
        "window_b",
        "thread.same",
        "TASK.SAME",
        "callback.done",
        {"claim_episode": "episode_origin_00000000001", "repo": "beta"},
    )
    mux.close_window("window_a")

    assert mux.deliver_callback(route_a).payload["repo"] == "alpha"
    assert mux.deliver_callback(route_b).payload["repo"] == "beta"
    assert route_a.canonical() != route_b.canonical()
    assert mux.callbacks_for_repo_thread_episode(
        repo_a.manifest.repo_id,
        "thread.same",
        "episode_origin_00000000001",
    )[0].payload["repo"] == "alpha"


def test_crash_restart_rehydrates_selected_repo_without_cross_reads(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path, "alpha", "repo_alpha000000000000000000000000001")
    repo_b = _repo(tmp_path, "beta", "repo_beta0000000000000000000000000002")
    marker_a = repo_a.durable_paths["tasking"] / "TASK.SAME.json"
    marker_b = repo_b.durable_paths["tasking"] / "TASK.SAME.json"
    marker_a.write_text(json.dumps({"repo": "alpha"}) + "\n", encoding="utf-8")
    marker_b.write_text(json.dumps({"repo": "beta"}) + "\n", encoding="utf-8")

    mux = RepositoryWindowMux()
    selected_uri = str(repo_b.root)
    mux.open_repository("window_b", selected_uri, claim_episode="episode_before_crash_001")

    restarted = RepositoryWindowMux()
    session = restarted.open_repository("window_b", selected_uri, claim_episode="episode_after_restart_001")

    assert session.repository.repo_id == repo_b.manifest.repo_id
    assert (session.repository.root / ".aiworkinghub" / "tasking" / "TASK.SAME.json").read_text(
        encoding="utf-8"
    ).strip() == '{"repo": "beta"}'
    assert marker_a.read_text(encoding="utf-8").strip() == '{"repo": "alpha"}'
    assert session.identity("thread.same", "TASK.SAME").repo_id == repo_b.manifest.repo_id
