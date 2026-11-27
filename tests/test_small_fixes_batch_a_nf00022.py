"""Focused regressions for small-fixes batch A (NF-2026-00022 and siblings).

(1) NF-2026-00022 terminal_authority: a Windows pending-delete PermissionError
    on the O_EXCL create is retried inside the bounded loop.
(2) NF-2026-00670 app_server_mux._chmod_dir_nofollow: a true no-op on Windows.
(3) NF-2026-00562 source_graph: the extraction ceiling derives from observed
    CPU capacity, never a hardcoded constant.
(4) NF-2026-00579 skill_registry_store.advance_record: the compare-and-swap
    token is required.
(5) NF-2026-00020 is covered by the existing symlink-swap test.
"""

from __future__ import annotations

import pytest

import aiworkhub.skill_registry as sr
from aiworkhub import app_server_mux as mux
from aiworkhub import skill_registry_store as store
from aiworkhub import source_graph as sg
from aiworkhub import terminal_authority as ta


def test_windows_pending_delete_permission_error_retries_bounded_create(
    tmp_path, monkeypatch,
):
    real_open = ta.os.open
    excl_calls = {"count": 0}

    def flaky_open(path, flags, *args, **kwargs):
        if flags & ta.os.O_EXCL:
            excl_calls["count"] += 1
            if excl_calls["count"] == 1:
                raise PermissionError(13, "pending delete", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ta.os, "open", flaky_open)
    monkeypatch.setattr(ta.os, "chmod", lambda *_a, **_k: None)
    monkeypatch.setattr(ta, "chmod_fd", lambda *_a, **_k: None)
    monkeypatch.setattr(
        ta, "windows_harden_owner_only_key_dacl", lambda _path: (True, "")
    )

    key = ta.load_or_create_key(tmp_path / "keys" / "k.key", _platform_name="nt")

    assert len(key) == 32
    assert excl_calls["count"] == 2


def test_posix_permission_error_on_create_is_not_retried(tmp_path, monkeypatch):
    real_open = ta.os.open
    excl_calls = {"count": 0}

    def denied_open(path, flags, *args, **kwargs):
        if flags & ta.os.O_EXCL:
            excl_calls["count"] += 1
            raise PermissionError(13, "denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ta.os, "open", denied_open)
    monkeypatch.setattr(ta.os, "chmod", lambda *_a, **_k: None)

    with pytest.raises(PermissionError):
        ta.load_or_create_key(tmp_path / "keys" / "k.key", _platform_name="posix")
    assert excl_calls["count"] == 1


def test_chmod_dir_nofollow_absorbs_windows_directory_open_error(
    tmp_path, monkeypatch,
):
    target = str(tmp_path)
    open_calls: list[str] = []
    chmod_calls: list[int] = []

    def windows_dir_open(path, *_args, **_kwargs):
        open_calls.append(str(path))
        if str(path) == target:
            # Windows cannot open a directory as a file descriptor.
            raise PermissionError(13, "Access is denied", str(path))
        raise AssertionError(f"unexpected os.open({path!r})")

    monkeypatch.setattr(mux.os, "open", windows_dir_open)
    monkeypatch.setattr(mux, "chmod_fd", lambda fd, _mode: chmod_calls.append(fd))

    assert mux._chmod_dir_nofollow(tmp_path, 0o700) is None
    assert open_calls == [target]
    assert chmod_calls == []


@pytest.mark.parametrize("capacity", [3, 6, 20])
def test_extract_worker_ceiling_follows_observed_cpu_capacity(
    monkeypatch, capacity,
):
    monkeypatch.setattr(sg.parallelism, "get_cpu_capacity", lambda: capacity)
    expected = capacity - sg.SOURCE_GRAPH_EXTRACT_RESERVE
    assert not hasattr(sg, "MAX_SOURCE_GRAPH_EXTRACT_WORKERS")
    assert sg._source_graph_extract_worker_ceiling() == expected

    # The env override still clamps to the derived ceiling and the candidates.
    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "999")
    assert sg._source_graph_extract_workers(100) == expected
    assert sg._source_graph_extract_workers(2) == min(2, expected)

    monkeypatch.delenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV)
    workers = sg._source_graph_extract_workers(
        100, sg.MIN_PARALLEL_EXTRACTION_BYTES
    )
    assert workers == expected
    telemetry = sg._source_graph_extraction_telemetry(
        workers, 100, sg.MIN_PARALLEL_EXTRACTION_BYTES
    )
    assert telemetry["ceiling"] == expected


def _record() -> sr.SkillRecord:
    return sr.SkillRecord.from_mapping(
        {
            "identity": "commit-msg-check",
            "version": "1.0.0",
            "scope": "repository",
            "task_family": "commit",
            "path_or_symbol": "src/aiworkhub/skill_registry.py",
            "risk": "medium",
            "stage": "post-edit",
            "triggers": ["commit"],
            "confidence": 0.9,
        }
    )


def test_advance_record_requires_a_compare_and_swap_token(tmp_path):
    record = _record()
    store.put_record(tmp_path, record)

    with pytest.raises(TypeError):
        store.advance_record(tmp_path, record)  # type: ignore[call-arg]
    for missing in (None, ""):
        with pytest.raises(store.SkillStoreConflictError):
            store.advance_record(tmp_path, record, expected_state_digest=missing)

    token = store.state_digest(
        store.get_record(tmp_path, record.identity, record.version)
    )
    result = store.advance_record(tmp_path, record, expected_state_digest=token)
    assert result["identity"] == record.identity
