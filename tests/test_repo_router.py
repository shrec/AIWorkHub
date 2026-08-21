from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiworkhub import shared_router


THREAD_ID = "019f5097-6dbe-7172-870a-945afc5f3bfa"
REPO_A = "repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
REPO_B = "repo_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
REPO_C = "repo_cccccccccccccccccccccccccccccccc"


def _record(repo_id: str, *, window_id: str, thread_id: str = "") -> dict:
    return {
        "repo_id": repo_id,
        "repo_root": f"/{repo_id}",
        "window_id": window_id,
        "extension_host_alive": True,
        "stale": False,
        "targets": {
            "codex": {
                "route": {"repo_id": repo_id, "thread_id": thread_id}
            }
        },
    }


def test_repository_route_transfer_does_not_require_target_thread(tmp_path: Path):
    records = [
        _record(REPO_A, window_id="window-owner", thread_id=THREAD_ID),
        _record(REPO_B, window_id="window-target"),
    ]

    result = shared_router.transfer_manager_route(
        provider="codex",
        thread_id=THREAD_ID,
        window_id="window-owner",
        source_repo_id=REPO_A,
        target_repo_id=REPO_B,
        repositories=records,
        home=tmp_path,
    )

    assert result["ok"] is True
    assert result["repo_id"] == REPO_B
    assert result["previous_repo_id"] == REPO_A
    assert result["epoch"] == 1
    payload = shared_router._read_ownership(shared_router._ownership_path(tmp_path))
    assert payload["revision"] == 1
    assert payload["routes"][f"codex:{THREAD_ID}"]["repo_id"] == REPO_B


def test_repository_route_transfer_rejects_foreign_or_stale_source(tmp_path: Path):
    records = [
        _record(REPO_A, window_id="window-foreign", thread_id=THREAD_ID),
        _record(REPO_B, window_id="window-target"),
    ]

    result = shared_router.transfer_manager_route(
        provider="codex",
        thread_id=THREAD_ID,
        window_id="window-owner",
        source_repo_id=REPO_A,
        target_repo_id=REPO_B,
        repositories=records,
        home=tmp_path,
    )

    assert result == {"ok": False, "error": "route_transfer_source_not_owned"}
    assert not shared_router._ownership_path(tmp_path).exists()


def test_repository_route_transfer_rejects_foreign_target(tmp_path: Path):
    foreign_thread = "019f5097-6dbe-7172-870a-945afc5f3bfb"
    records = [
        _record(REPO_A, window_id="window-owner", thread_id=THREAD_ID),
        _record(REPO_B, window_id="window-foreign", thread_id=foreign_thread),
    ]

    result = shared_router.transfer_manager_route(
        provider="codex",
        thread_id=THREAD_ID,
        window_id="window-owner",
        source_repo_id=REPO_A,
        target_repo_id=REPO_B,
        repositories=records,
        home=tmp_path,
    )

    assert result == {
        "ok": False,
        "error": "route_transfer_target_owned_by_foreign_manager",
    }


def test_repository_route_transfer_has_one_concurrent_winner(tmp_path: Path):
    records = [
        _record(REPO_A, window_id="window-owner", thread_id=THREAD_ID),
        _record(REPO_B, window_id="window-b"),
        _record(REPO_C, window_id="window-c"),
    ]

    def switch(target: str) -> dict:
        return shared_router.transfer_manager_route(
            provider="codex",
            thread_id=THREAD_ID,
            window_id="window-owner",
            source_repo_id=REPO_A,
            target_repo_id=target,
            repositories=records,
            home=tmp_path,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(switch, (REPO_B, REPO_C)))

    assert sum(result["ok"] is True for result in results) == 1
    loser = next(result for result in results if result["ok"] is False)
    assert loser["error"] == "route_ownership_epoch_conflict"


def test_repository_route_rollback_is_exact_epoch_cas(tmp_path: Path):
    records = [
        _record(REPO_A, window_id="window-owner", thread_id=THREAD_ID),
        _record(REPO_B, window_id="window-target"),
    ]
    transfer = shared_router.transfer_manager_route(
        provider="codex",
        thread_id=THREAD_ID,
        window_id="window-owner",
        source_repo_id=REPO_A,
        target_repo_id=REPO_B,
        repositories=records,
        home=tmp_path,
    )

    stale = shared_router.rollback_manager_route(
        provider="codex",
        thread_id=THREAD_ID,
        window_id="window-owner",
        failed_repo_id=REPO_B,
        restore_repo_id=REPO_A,
        expected_epoch=transfer["epoch"] + 1,
        home=tmp_path,
    )
    restored = shared_router.rollback_manager_route(
        provider="codex",
        thread_id=THREAD_ID,
        window_id="window-owner",
        failed_repo_id=REPO_B,
        restore_repo_id=REPO_A,
        expected_epoch=transfer["epoch"],
        home=tmp_path,
    )

    assert stale == {"ok": False, "error": "route_rollback_epoch_conflict"}
    assert restored["ok"] is True
    assert restored["repo_id"] == REPO_A
    assert restored["epoch"] == 2


def test_repository_route_ownership_projection_fences_source_and_publishes_target(
    tmp_path: Path,
):
    records = [
        _record(REPO_A, window_id="window-owner", thread_id=THREAD_ID),
        _record(REPO_B, window_id="window-target"),
    ]
    transfer = shared_router.transfer_manager_route(
        provider="codex",
        thread_id=THREAD_ID,
        window_id="window-owner",
        source_repo_id=REPO_A,
        target_repo_id=REPO_B,
        repositories=records,
        home=tmp_path,
    )

    projected, error = shared_router._apply_manager_route_ownership(
        records, home=tmp_path
    )
    by_repo = {record["repo_id"]: record for record in projected}

    assert transfer["ok"] is True
    assert error == ""
    source_route = by_repo[REPO_A]["targets"]["codex"]["route"]
    target_route = by_repo[REPO_B]["targets"]["codex"]["route"]
    assert source_route["thread_id"] == ""
    assert source_route["fenced_by_repo_id"] == REPO_B
    assert target_route["thread_id"] == THREAD_ID
    assert target_route["owner_window_id"] == "window-owner"
    assert target_route["ownership_epoch"] == 1
