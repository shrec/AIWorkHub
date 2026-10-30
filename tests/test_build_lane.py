"""Contained build lane (NF-2026-01842): docker is always faked."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import build_lane, validation_runner, worker_workspace

DIGEST = "a" * 64
LANE = {
    "image": f"toolchain@sha256:{DIGEST}",
    "argv": ["/bin/sh", "-c", "build"],
    "workdir": "/src",
    "cache_volume": "idf_cache",
    "timeout_seconds": 60,
}


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kills: list[list[str]] = []
        self.observed: list[list[str]] = []
        self.returncode = 0
        self.timeout = False
        self.delay = 0.0
        self.stdout = b""
        self.stderr = b""
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if argv[1] == "kill":
            self.kills.append(list(argv))
            return subprocess.CompletedProcess(argv, 0)
        mount = argv[argv.index("-v") + 1]
        snapshot = Path(mount[: -len(":/src:ro")])
        with self._guard:
            self.calls.append(list(argv))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.observed.append(
                sorted(p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file())
            )
        try:
            kwargs["stdout"].write(self.stdout)
            kwargs["stderr"].write(self.stderr)
            time.sleep(self.delay)
            if self.timeout:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, self.returncode)
        finally:
            with self._guard:
                self.active -= 1


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    root = tmp_path / "canon"
    root.mkdir()
    docker = tmp_path / "tools" / "docker"
    fake = FakeDocker()
    monkeypatch.setattr(build_lane, "_canonical_root", lambda: root)
    monkeypatch.setattr(build_lane.shutil, "which", lambda name: str(docker))
    monkeypatch.setattr(build_lane.subprocess, "run", fake.run)

    def write_config(lanes: dict[str, Any], base: Path = root) -> None:
        target = base / ".aiworkhub" / "config" / "build_lanes.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"lanes": lanes}), encoding="utf-8")

    def make_workspace(name: str = "ws", content: str = "one") -> SimpleNamespace:
        path = tmp_path / name
        (path / "src").mkdir(parents=True, exist_ok=True)
        (path / "src" / "main.c").write_text(content, encoding="utf-8")
        (path / ".git").mkdir(exist_ok=True)
        (path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (path / ".aiworkhub" / "runtime").mkdir(parents=True, exist_ok=True)
        (path / ".aiworkhub" / "runtime" / "state.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(path=path)

    write_config({"build": LANE})
    return SimpleNamespace(
        root=root,
        docker=docker,
        fake=fake,
        write_config=write_config,
        make_workspace=make_workspace,
        receipts=root / ".aiworkhub" / "runtime" / "build_lanes" / "receipts",
    )


def receipt_files(env: SimpleNamespace) -> list[Path]:
    return sorted(env.receipts.glob("*.json")) if env.receipts.exists() else []


def test_docker_argv_shape_and_snapshot_contents(env: SimpleNamespace) -> None:
    row = build_lane.run_lane_validation(env.make_workspace(), "lane:build")

    assert row["returncode"] == 0 and row["cache_hit"] is False
    (argv,) = env.fake.calls
    key = row["build_lane"]["key"]
    snapshot = argv[argv.index("-v") + 1][: -len(":/src:ro")]
    out = argv[argv.index("-v", argv.index("-v") + 4) + 1][: -len(":/out")]
    assert argv == [
        str(env.docker), "run", "--rm", "--pull", "never",
        "--name", f"aiwh-lane-{key[:12]}",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "4096",
        "-v", f"{snapshot}:/src:ro",
        "-v", "idf_cache:/cache",
        "-v", f"{out}:/out",
        "--workdir", "/src",
        LANE["image"], "/bin/sh", "-c", "build",
    ]
    assert "--privileged" not in argv and "-e" not in argv and "--env" not in argv
    assert argv.count("-v") == 3
    assert env.fake.observed == [["src/main.c"]]
    assert row["build_lane"]["network"] == "none"
    assert row["build_lane"]["lane_id"] == "build"
    scratch_root = env.root / ".aiworkhub" / "runtime" / "build_lanes" / "scratch"
    assert not Path(snapshot).exists() and list(scratch_root.iterdir()) == []


def test_bridge_network_only_when_explicit_and_is_recorded(env: SimpleNamespace) -> None:
    env.write_config({"build": {**LANE, "network": "bridge"}})
    row = build_lane.run_lane_validation(env.make_workspace(), "lane:build")

    argv = env.fake.calls[0]
    assert argv[argv.index("--network") + 1] == "bridge"
    assert row["build_lane"]["network"] == "bridge"
    assert json.loads(receipt_files(env)[0].read_text(encoding="utf-8"))["network"] == "bridge"


@pytest.mark.parametrize(
    ("lane", "reason"),
    [
        ({**LANE, "image": "toolchain:latest"}, "lane_image_not_digest_pinned"),
        ({**LANE, "image": "toolchain"}, "lane_image_not_digest_pinned"),
        ({**LANE, "image": "toolchain@sha256:abc"}, "lane_image_not_digest_pinned"),
        ({**LANE, "argv": []}, "lane_argv_invalid"),
        ({**LANE, "privileged": True}, "lane_config_unknown_keys"),
        ({**LANE, "network": "host"}, "lane_network_invalid"),
        ({**LANE, "cache_volume": "/var/run:/x"}, "lane_cache_volume_invalid"),
        ({**LANE, "timeout_seconds": 0}, "lane_timeout_invalid"),
        ({k: v for k, v in LANE.items() if k != "image"}, "lane_config_missing_keys"),
    ],
)
def test_invalid_lane_configuration_is_refused(
    env: SimpleNamespace, lane: dict[str, Any], reason: str
) -> None:
    env.write_config({"build": lane})
    with pytest.raises(build_lane.LaneRefused, match=reason):
        build_lane.load_lanes(env.root)

    row = build_lane.run_lane_validation(env.make_workspace(), "lane:build")
    assert row["infrastructure_blocker"].startswith(f"lane_refused:{reason}")
    assert row["returncode"] is None
    assert env.fake.calls == []


def test_lane_id_outside_the_allowed_pattern_is_refused(env: SimpleNamespace) -> None:
    env.write_config({"Bad Lane": LANE})
    with pytest.raises(build_lane.LaneRefused, match="invalid_lane_id"):
        build_lane.load_lanes(env.root)


@pytest.mark.parametrize("command", ["lane:", "lane:Build", "lane:build extra", "lane:a/b"])
def test_lane_command_must_be_exactly_one_valid_token(
    env: SimpleNamespace, command: str
) -> None:
    row = build_lane.run_lane_validation(env.make_workspace(), command)
    assert row["infrastructure_blocker"].startswith("lane_refused:invalid_lane_command")
    assert env.fake.calls == []


def test_unknown_lane_is_refused_everywhere(env: SimpleNamespace) -> None:
    workspace = env.make_workspace()
    row = build_lane.run_lane_validation(workspace, "lane:nope")
    assert row["infrastructure_blocker"] == "lane_refused:unknown_lane:nope"
    assert validation_runner.classify_validation_results([row]).state == (
        validation_runner.VALIDATION_ENVIRONMENT_BLOCKED
    )
    with pytest.raises(worker_workspace.WorkspaceError, match="validation_lane_refused"):
        worker_workspace.resolve_worker_validation_argv(workspace, "lane:nope")
    assert build_lane.preflight_requirements("lane:nope") == ("lane:unknown_lane:nope",)
    assert build_lane.preflight_requirements("lane:build") == ()
    assert env.fake.calls == []


def test_missing_config_is_a_refusal_not_a_pass(env: SimpleNamespace) -> None:
    (env.root / ".aiworkhub" / "config" / "build_lanes.json").unlink()
    row = build_lane.run_lane_validation(env.make_workspace(), "lane:build")
    assert row["infrastructure_blocker"] == "lane_refused:lane_config_missing"
    assert row["returncode"] is None


def test_lane_config_comes_from_canonical_root_not_candidate(env: SimpleNamespace) -> None:
    workspace = env.make_workspace()
    hostile = {**LANE, "image": f"evil@sha256:{'b' * 64}", "network": "bridge"}
    env.write_config({"build": hostile, "evil": hostile}, base=workspace.path)

    refused = build_lane.run_lane_validation(workspace, "lane:evil")
    assert refused["infrastructure_blocker"] == "lane_refused:unknown_lane:evil"

    row = build_lane.run_lane_validation(workspace, "lane:build")
    assert row["build_lane"]["image"] == LANE["image"]
    assert row["build_lane"]["network"] == "none"
    assert LANE["image"] in env.fake.calls[0]
    assert ".aiworkhub/config/build_lanes.json" in env.fake.observed[0]


def test_identical_bytes_hit_the_receipt_and_one_changed_byte_misses(
    env: SimpleNamespace,
) -> None:
    first = build_lane.run_lane_validation(env.make_workspace("ws", "one"), "lane:build")
    again = build_lane.run_lane_validation(env.make_workspace("ws", "one"), "lane:build")
    copy = build_lane.run_lane_validation(env.make_workspace("other", "one"), "lane:build")

    assert first["cache_hit"] is False
    assert again["cache_hit"] is True and copy["cache_hit"] is True
    assert again["build_lane"]["key"] == first["build_lane"]["key"] == copy["build_lane"]["key"]
    assert len(env.fake.calls) == 1
    assert [p.stem for p in receipt_files(env)] == [first["build_lane"]["key"]]

    changed = build_lane.run_lane_validation(env.make_workspace("ws", "onf"), "lane:build")
    assert changed["cache_hit"] is False
    assert changed["build_lane"]["key"] != first["build_lane"]["key"]
    assert len(env.fake.calls) == 2

    restored = build_lane.run_lane_validation(env.make_workspace("ws", "one"), "lane:build")
    assert restored["cache_hit"] is True and len(env.fake.calls) == 2


def test_a_completed_code_failure_is_cached_and_stays_a_code_failure(
    env: SimpleNamespace,
) -> None:
    env.fake.returncode = 2
    env.fake.stderr = b"error: boom\n"
    workspace = env.make_workspace()
    first = build_lane.run_lane_validation(workspace, "lane:build")
    second = build_lane.run_lane_validation(workspace, "lane:build")

    assert first["returncode"] == 2 and "boom" in first["stderr_tail"]
    assert second["cache_hit"] is True and second["returncode"] == 2
    assert len(env.fake.calls) == 1
    assert "launch_error" not in first
    assert validation_runner.classify_validation_results([second]).state == (
        validation_runner.VALIDATION_FAILED
    )


@pytest.mark.parametrize("code", [125, 126, 127])
def test_docker_infrastructure_exit_codes_are_not_cached_or_code_failures(
    env: SimpleNamespace, code: int
) -> None:
    env.fake.returncode = code
    workspace = env.make_workspace()
    first = build_lane.run_lane_validation(workspace, "lane:build")
    second = build_lane.run_lane_validation(workspace, "lane:build")

    assert len(env.fake.calls) == 2
    assert receipt_files(env) == []
    for row in (first, second):
        assert row["cache_hit"] is False
        assert row["infrastructure_blocker"] == f"docker_exit_{code}"
        assert row["launch_error"] == build_lane.INFRASTRUCTURE_LAUNCH_ERROR
        assert row["failure_class"] == "validation_environment"
        assert validation_runner.classify_validation_results([row]).state == (
            validation_runner.VALIDATION_ENVIRONMENT_BLOCKED
        )


def test_timeout_kills_the_named_container_and_is_infrastructure(
    env: SimpleNamespace,
) -> None:
    env.fake.timeout = True
    row = build_lane.run_lane_validation(env.make_workspace(), "lane:build")

    name = env.fake.calls[0][env.fake.calls[0].index("--name") + 1]
    assert env.fake.kills == [[str(env.docker), "kill", name]]
    assert row["timed_out"] is True and row["returncode"] is None
    assert row["infrastructure_blocker"].startswith("lane_timeout:build")
    assert receipt_files(env) == []
    assert validation_runner.classify_validation_results([row]).state == (
        validation_runner.VALIDATION_ENVIRONMENT_BLOCKED
    )


def test_daemon_failure_is_infrastructure_and_not_cached(env: SimpleNamespace) -> None:
    env.fake.returncode = 1
    env.fake.stderr = b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock.\n"
    row = build_lane.run_lane_validation(env.make_workspace(), "lane:build")

    assert row["infrastructure_blocker"] == "docker_daemon_failure"
    assert receipt_files(env) == []


def test_missing_or_repo_owned_docker_is_infrastructure(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = env.make_workspace()
    monkeypatch.setattr(build_lane.shutil, "which", lambda name: None)
    missing = build_lane.run_lane_validation(workspace, "lane:build")
    assert missing["infrastructure_blocker"] == "docker_unavailable"

    owned = str(workspace.path / "bin" / "docker")
    monkeypatch.setattr(build_lane.shutil, "which", lambda name: owned)
    refused = build_lane.run_lane_validation(workspace, "lane:build")
    assert refused["infrastructure_blocker"].startswith("docker_repo_owned_path")
    assert env.fake.calls == [] and receipt_files(env) == []


def test_corrupt_receipt_is_a_miss(env: SimpleNamespace) -> None:
    workspace = env.make_workspace()
    first = build_lane.run_lane_validation(workspace, "lane:build")
    receipt_files(env)[0].write_text("{not json", encoding="utf-8")

    again = build_lane.run_lane_validation(workspace, "lane:build")
    assert again["cache_hit"] is False and len(env.fake.calls) == 2
    assert again["build_lane"]["key"] == first["build_lane"]["key"]


def test_per_lane_lock_serializes_concurrent_runs(env: SimpleNamespace) -> None:
    env.fake.delay = 0.3
    rows: list[dict[str, Any]] = []
    workspaces = [env.make_workspace("a", "alpha"), env.make_workspace("b", "beta")]
    threads = [
        threading.Thread(target=lambda w=w: rows.append(build_lane.run_lane_validation(w, "lane:build")))
        for w in workspaces
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(rows) == 2 and len(env.fake.calls) == 2
    assert env.fake.max_active == 1
    assert max(r["build_lane"]["queue_wait_seconds"] for r in rows) >= 0.2


def test_concurrent_identical_bytes_build_once(env: SimpleNamespace) -> None:
    env.fake.delay = 0.3
    rows: list[dict[str, Any]] = []
    workspace = env.make_workspace()
    threads = [
        threading.Thread(target=lambda: rows.append(build_lane.run_lane_validation(workspace, "lane:build")))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(env.fake.calls) == 1
    assert sorted(r["cache_hit"] for r in rows) == [False, True]


def _workspace_object(tmp_path: Path, path: Path) -> worker_workspace.WorkerWorkspace:
    return worker_workspace.WorkerWorkspace(
        request_id="req",
        repo=tmp_path,
        path=path,
        home=tmp_path / "home",
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


def test_run_validations_dispatches_lane_commands_to_the_build_lane(
    env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(worker_workspace, "provision_validation_exec_scratch", lambda *a, **k: scratch)
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda *a, **k: None)
    workspace = _workspace_object(tmp_path, env.make_workspace().path)
    backend = worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND

    rows = worker_workspace.run_validations(
        workspace, ["lane:build"], backend=backend, adapter_id="vscode_lm"
    )
    assert [r["command"] for r in rows] == ["lane:build"]
    assert rows[0]["returncode"] == 0 and rows[0]["build_lane"]["lane_id"] == "build"

    env.fake.returncode = 125
    other = _workspace_object(tmp_path, env.make_workspace("other", "two").path)
    with pytest.raises(worker_workspace.ValidationEnvironmentBlocked):
        worker_workspace.run_validations(
            other, ["lane:build"], backend=backend, adapter_id="vscode_lm"
        )


def test_non_lane_commands_take_the_existing_path_unchanged(
    env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a non-lane command reached the build lane")

    for name in ("run_lane_validation", "resolve_lane_argv", "preflight_requirements"):
        monkeypatch.setattr(build_lane, name, forbidden)
    workspace = _workspace_object(tmp_path, env.make_workspace().path)

    resolution = worker_workspace.resolve_worker_validation_argv(workspace, "python -c pass")
    assert resolution["argv"][-2:] == ["-c", "pass"]
    assert resolution["argv"][0] != "lane:build"
    assert worker_workspace.run_validations(workspace, []) == []
    assert not build_lane.is_lane_command("python -c pass")


def _link_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret", encoding="utf-8")
    source = tmp_path / "tree"
    source.mkdir()
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    return source, outside, tmp_path / "dest"


def _snapshot_files(destination: Path) -> list[str]:
    return sorted(p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file())


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_ntfs_junction_is_recorded_as_a_link_and_never_copied(tmp_path: Path) -> None:
    source, outside, destination = _link_tree(tmp_path)
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(source / "junc"), str(outside)],
        capture_output=True,
        check=False,
    )
    assert made.returncode == 0, made.stderr
    destination.mkdir()

    digest = build_lane._snapshot(source, destination)

    assert _snapshot_files(destination) == ["keep.txt"]
    assert not (destination / "junc").exists()
    (source / "junc").rmdir()
    (source / "junc").mkdir()
    (source / "junc" / "secret.txt").write_text("host secret", encoding="utf-8")
    other = tmp_path / "dest2"
    other.mkdir()
    assert build_lane._snapshot(source, other) != digest
    assert build_lane._is_link(outside) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and FIFO semantics")
def test_symlinked_directory_and_fifo_are_not_copied_or_opened(tmp_path: Path) -> None:
    source, outside, destination = _link_tree(tmp_path)
    os.symlink(outside, source / "linkdir", target_is_directory=True)
    os.mkfifo(source / "pipe")
    destination.mkdir()

    digest = build_lane._snapshot(source, destination)

    assert _snapshot_files(destination) == ["keep.txt"]
    assert not (destination / "linkdir").exists() and not (destination / "pipe").exists()
    os.unlink(source / "linkdir")
    other = tmp_path / "dest2"
    other.mkdir()
    assert build_lane._snapshot(source, other) != digest


def test_memo_key_changes_with_workdir_and_cache_volume(env: SimpleNamespace) -> None:
    lane = build_lane.load_lanes(env.root)["build"]
    base = build_lane._memo_key(lane, "t" * 64)
    assert build_lane._memo_key(lane, "t" * 64) == base
    assert build_lane._memo_key(dataclasses.replace(lane, workdir="/src/sub"), "t" * 64) != base
    assert build_lane._memo_key(dataclasses.replace(lane, cache_volume="other_cache"), "t" * 64) != base
    assert build_lane._memo_key(dataclasses.replace(lane, timeout_seconds=5), "t" * 64) == base
