"""Contained, digest-pinned container build lane (NF-2026-01842).

A card validation command ``lane:<lane_id>`` runs a configured container image
host-side on a read-only snapshot of the candidate tree.  Lanes are defined only
in the canonical repository's ``.aiworkhub/config/build_lanes.json``; the
candidate tree can neither add nor alter one.  Completed container runs are
memoized on the candidate tree digest so the worker advisory run, the finalizer
and acceptance of the same bytes build once.  Infrastructure failures are never
memoized and never reported as code failures.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import platform_io

LANE_PREFIX = "lane:"
LANE_CONFIG_RELATIVE = ".aiworkhub/config/build_lanes.json"
LANE_RUNTIME_RELATIVE = ".aiworkhub/runtime/build_lanes"
RECEIPT_SCHEMA = "aiworkhub.build_lane_receipt.v1"
LANE_ROW_SCHEMA = "aiworkhub.build_lane_row.v1"
INFRASTRUCTURE_LAUNCH_ERROR = "LaneInfrastructureError"
VALIDATION_ENVIRONMENT_CLASS = "validation_environment"

# Docker's own "the run itself failed" exit codes: 125 daemon/CLI error, 126
# contained command not invokable, 127 contained command not found.
INFRASTRUCTURE_EXIT_CODES = frozenset({125, 126, 127})
QUEUE_WAIT_SECONDS = 3600.0
MAX_LANE_TIMEOUT_SECONDS = 86_400
_LOCK_POLL_SECONDS = 0.05
_KILL_TIMEOUT_SECONDS = 30
_MAX_CONFIG_BYTES = 1_048_576
_OUTPUT_WINDOW = 4_096

_LANE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LANE_KEYS = frozenset(
    {"image", "argv", "workdir", "cache_volume", "timeout_seconds", "network"}
)
_LANE_REQUIRED_KEYS = frozenset({"image", "argv", "cache_volume", "timeout_seconds"})
_NETWORKS = frozenset({"none", "bridge"})
_DOCKER_ENV_KEYS = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "TEMP", "TMP",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
)
_DAEMON_FAILURE_MARKERS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "is the docker daemon running",
)

# A lane's /cache volume is shared by every run of that lane (toolchain and
# compiler caches), and concurrent writers corrupt it.  One run per lane at a
# time is therefore a correctness rule, not a throughput choice: the in-process
# lock covers threads, the lock file covers other coordinator processes.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class LaneRefused(Exception):
    """The command or lane configuration was refused; names the cause."""


class LaneInfrastructure(Exception):
    """The lane could not complete a container run; not a code failure."""


@dataclass(frozen=True)
class Lane:
    lane_id: str
    image: str
    argv: tuple[str, ...]
    workdir: str
    cache_volume: str
    timeout_seconds: int
    network: str


def is_lane_command(command: str) -> bool:
    return command.startswith(LANE_PREFIX)


def lane_id_from_command(command: str) -> str:
    stripped = command.strip()
    if not stripped.startswith(LANE_PREFIX):
        raise LaneRefused(f"not_a_lane_command:{stripped[:80]}")
    lane_id = stripped[len(LANE_PREFIX):]
    if not _LANE_ID_RE.fullmatch(lane_id):
        raise LaneRefused(f"invalid_lane_command:{stripped[:80]}")
    return lane_id


def _canonical_root() -> Path:
    from .core import repo_root

    return repo_root()


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value


def _validate_lane(lane_id: str, raw: Any) -> Lane:
    if not _LANE_ID_RE.fullmatch(lane_id):
        raise LaneRefused(f"invalid_lane_id:{lane_id[:80]}")
    if not isinstance(raw, dict):
        raise LaneRefused(f"lane_config_not_an_object:{lane_id}")
    unknown = sorted(str(key) for key in raw if key not in _LANE_KEYS)
    if unknown:
        raise LaneRefused(f"lane_config_unknown_keys:{lane_id}:{','.join(unknown)}")
    missing = sorted(_LANE_REQUIRED_KEYS - set(raw))
    if missing:
        raise LaneRefused(f"lane_config_missing_keys:{lane_id}:{','.join(missing)}")
    image = raw["image"]
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise LaneRefused(f"lane_image_not_digest_pinned:{lane_id}")
    argv = raw["argv"]
    if not isinstance(argv, list) or not argv or not all(_is_text(a) for a in argv):
        raise LaneRefused(f"lane_argv_invalid:{lane_id}")
    workdir = raw.get("workdir", "/src")
    if not _is_text(workdir) or not workdir.startswith("/"):
        raise LaneRefused(f"lane_workdir_invalid:{lane_id}")
    volume = raw["cache_volume"]
    if not isinstance(volume, str) or not _VOLUME_RE.fullmatch(volume):
        raise LaneRefused(f"lane_cache_volume_invalid:{lane_id}")
    timeout = raw["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= MAX_LANE_TIMEOUT_SECONDS
    ):
        raise LaneRefused(f"lane_timeout_invalid:{lane_id}")
    network = raw.get("network", "none")
    if network not in _NETWORKS:
        raise LaneRefused(f"lane_network_invalid:{lane_id}")
    return Lane(lane_id, image, tuple(argv), workdir, volume, timeout, network)


def load_lanes(root: Path | None = None) -> dict[str, Lane]:
    """Read lane definitions from the canonical repository only."""
    config = (root or _canonical_root()) / LANE_CONFIG_RELATIVE
    try:
        if config.stat().st_size > _MAX_CONFIG_BYTES:
            raise LaneRefused("lane_config_oversized")
        payload = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LaneRefused("lane_config_missing") from exc
    except (OSError, ValueError) as exc:
        raise LaneRefused(f"lane_config_unreadable:{type(exc).__name__}") from exc
    if not isinstance(payload, dict) or set(payload) != {"lanes"}:
        raise LaneRefused("lane_config_schema_invalid")
    lanes = payload["lanes"]
    if not isinstance(lanes, dict):
        raise LaneRefused("lane_config_schema_invalid")
    return {str(name): _validate_lane(str(name), raw) for name, raw in lanes.items()}


def load_lane(lane_id: str, root: Path | None = None) -> Lane:
    lanes = load_lanes(root)
    if lane_id not in lanes:
        raise LaneRefused(f"unknown_lane:{lane_id}")
    return lanes[lane_id]


def preflight_requirements(command: str) -> tuple[str, ...]:
    try:
        load_lane(lane_id_from_command(command))
    except LaneRefused as exc:
        return (f"lane:{exc}",)
    return ()


def resolve_lane_argv(command: str) -> dict[str, Any]:
    try:
        load_lane(lane_id_from_command(command))
    except LaneRefused as exc:
        from .worker_workspace import WorkspaceError

        raise WorkspaceError(f"validation_lane_refused:{exc}") from exc
    return {
        "argv": [command.strip()],
        "declared_head": command.strip(),
        "pythonpath_components": (),
        "cd_relative": None,
        "interpreter_authority": None,
        "is_pytest": False,
    }


def _is_link(path: Path) -> bool:
    """True for a symlink or any reparse point (an NTFS junction is not is_symlink())."""
    info = os.lstat(path)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _link_target(path: Path) -> str:
    try:
        return os.readlink(path)
    except (OSError, ValueError):
        return "<reparse>"


def _snapshot(source: Path, destination: Path) -> str:
    """Copy ``source`` (minus .git and .aiworkhub/runtime) and return its digest."""
    entries: dict[str, Path] = {}
    links: dict[str, str] = {}
    specials: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        here = Path(dirpath)
        kept: list[str] = []
        for name in sorted(dirnames):
            rel = (here / name).relative_to(source).as_posix()
            if rel == ".git" or rel == ".aiworkhub/runtime":
                continue
            if _is_link(here / name):
                links[rel] = _link_target(here / name)
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            rel = (here / name).relative_to(source).as_posix()
            if rel == ".git":
                continue
            if _is_link(here / name):
                links[rel] = _link_target(here / name)
                continue
            mode = os.lstat(here / name).st_mode
            if stat.S_ISREG(mode):
                entries[rel] = here / name
            else:
                specials[rel] = mode
    digest = hashlib.sha256()
    records = (
        [(rel, "f") for rel in entries]
        + [(rel, "l") for rel in links]
        + [(rel, "s") for rel in specials]
    )
    for rel, kind in sorted(records):
        digest.update(rel.encode("utf-8") + b"\x00")
        if kind == "l":
            digest.update(b"symlink:" + links[rel].encode("utf-8", "replace") + b"\n")
            continue
        if kind == "s":
            digest.update(f"special:{specials[rel]}\n".encode("ascii"))
            continue
        origin = entries[rel]
        target = destination / Path(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        content = hashlib.sha256()
        with origin.open("rb") as reader, target.open("wb") as writer:
            for chunk in iter(lambda: reader.read(1 << 20), b""):
                content.update(chunk)
                writer.write(chunk)
        executable = os.name != "nt" and bool(origin.stat().st_mode & 0o111)
        if executable:
            shutil.copymode(origin, target)
        digest.update(content.hexdigest().encode("ascii") + (b"x\n" if executable else b"\n"))
    return digest.hexdigest()


def _memo_key(lane: Lane, tree_digest: str) -> str:
    material = json.dumps(
        [
            lane.lane_id,
            lane.image,
            list(lane.argv),
            lane.network,
            tree_digest,
            lane.workdir,
            lane.cache_volume,
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _read_receipt(path: Path, key: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_id") != RECEIPT_SCHEMA:
        return None
    core = payload.get("core")
    if payload.get("key") != key or not isinstance(core, dict):
        return None
    returncode = core.get("returncode")
    if (
        isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or returncode in INFRASTRUCTURE_EXIT_CODES
        or core.get("infrastructure")
        or core.get("timed_out")
    ):
        return None
    return core


def _write_receipt(
    path: Path, key: str, lane: Lane, tree_digest: str, core: Mapping[str, Any]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_id": RECEIPT_SCHEMA,
        "key": key,
        "lane_id": lane.lane_id,
        "image": lane.image,
        "argv": list(lane.argv),
        "network": lane.network,
        "tree_digest": tree_digest,
        "created_at": round(time.time(), 3),
        "core": dict(core),
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


@contextmanager
def _lane_lock(lock_dir: Path, lane_id: str, max_wait: float) -> Iterator[float]:
    """Hold the per-lane lock; yields the seconds spent queued."""
    begun = time.monotonic()
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(lane_id, threading.Lock())
    if not thread_lock.acquire(timeout=max_wait):
        raise LaneInfrastructure(f"lane_queue_timeout:{lane_id}")
    fd = -1
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_dir / f"{lane_id}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            try:
                platform_io.lock_fd(fd, blocking=False)
                break
            except OSError as exc:
                if time.monotonic() - begun >= max_wait:
                    raise LaneInfrastructure(f"lane_queue_timeout:{lane_id}") from exc
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield round(time.monotonic() - begun, 3)
        finally:
            with contextlib.suppress(OSError):
                platform_io.unlock_fd(fd)
    finally:
        if fd >= 0:
            os.close(fd)
        thread_lock.release()


def _resolve_docker(*owned_roots: Path) -> str:
    found = shutil.which("docker")
    if not found:
        raise LaneInfrastructure("docker_unavailable")
    resolved = Path(found).resolve(strict=False)
    for root in owned_roots:
        if resolved.is_relative_to(root.resolve(strict=False)):
            raise LaneInfrastructure(f"docker_repo_owned_path:{resolved}")
    return found


def _docker_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _DOCKER_ENV_KEYS if key in os.environ}


def _docker_argv(
    docker: str, lane: Lane, key: str, snapshot: Path, out: Path
) -> list[str]:
    return [
        docker, "run", "--rm", "--pull", "never",
        "--name", f"aiwh-lane-{key[:12]}",
        "--network", lane.network,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "4096",
        "-v", f"{snapshot}:/src:ro",
        "-v", f"{lane.cache_volume}:/cache",
        "-v", f"{out}:/out",
        "--workdir", lane.workdir,
        lane.image, *lane.argv,
    ]


def _window(path: Path) -> tuple[str, str, bool]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(_OUTPUT_WINDOW)
            handle.seek(max(0, size - _OUTPUT_WINDOW))
            tail = handle.read(_OUTPUT_WINDOW)
    except OSError:
        return "", "", False
    return (
        head.decode("utf-8", errors="replace"),
        tail.decode("utf-8", errors="replace"),
        size > 2 * _OUTPUT_WINDOW,
    )


def _daemon_failure(returncode: int, stdout_text: str, stderr_text: str) -> bool:
    if returncode == 0 or stdout_text.strip() or len(stderr_text) > 2_048:
        return False
    lowered = stderr_text.lower()
    return any(marker in lowered for marker in _DAEMON_FAILURE_MARKERS)


def _execute(docker_argv: list[str], lane: Lane, scratch: Path) -> dict[str, Any]:
    name = docker_argv[docker_argv.index("--name") + 1]
    stdout_path, stderr_path = scratch / "stdout.log", scratch / "stderr.log"
    started = time.monotonic()
    returncode: int | None = None
    timed_out = False
    infrastructure = ""
    try:
        with stdout_path.open("wb") as out_handle, stderr_path.open("wb") as err_handle:
            completed = subprocess.run(  # noqa: S603 -- argv built only by AIWorkHub
                docker_argv,
                stdin=subprocess.DEVNULL,
                stdout=out_handle,
                stderr=err_handle,
                timeout=lane.timeout_seconds,
                check=False,
                shell=False,
                env=_docker_env(),
            )
        returncode = int(completed.returncode)
    except subprocess.TimeoutExpired:
        timed_out = True
        infrastructure = f"lane_timeout:{lane.lane_id}:{lane.timeout_seconds}s"
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603
                [docker_argv[0], "kill", name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_KILL_TIMEOUT_SECONDS,
                check=False,
                shell=False,
                env=_docker_env(),
            )
    except OSError as exc:
        infrastructure = f"docker_launch_failed:{type(exc).__name__}:{exc}"[:300]
    stdout_head, stdout_tail, stdout_truncated = _window(stdout_path)
    stderr_head, stderr_tail, stderr_truncated = _window(stderr_path)
    if returncode in INFRASTRUCTURE_EXIT_CODES:
        infrastructure = f"docker_exit_{returncode}"
    elif returncode is not None and _daemon_failure(returncode, stdout_tail, stderr_tail):
        infrastructure = "docker_daemon_failure"
    return {
        "docker_argv": docker_argv,
        "returncode": returncode,
        "timed_out": timed_out,
        "infrastructure": infrastructure,
        "duration_seconds": round(time.monotonic() - started, 6),
        "stdout_head": stdout_head,
        "stdout_tail": stdout_tail,
        "stdout_truncated": stdout_truncated,
        "stderr_head": stderr_head,
        "stderr_tail": stderr_tail,
        "stderr_truncated": stderr_truncated,
    }


def _row(
    command: str,
    lane: Lane | None,
    core: Mapping[str, Any],
    *,
    key: str = "",
    tree_digest: str = "",
    cache_hit: bool = False,
    queue_wait: float = 0.0,
) -> dict[str, Any]:
    argv = [str(token) for token in core.get("docker_argv") or [command]]
    infrastructure = str(core.get("infrastructure") or "")
    stderr_tail = str(core.get("stderr_tail") or "")
    stderr_head = str(core.get("stderr_head") or "")
    if infrastructure:
        stderr_tail = f"{VALIDATION_ENVIRONMENT_CLASS}:{infrastructure}\n{stderr_tail}"[-8_192:]
        stderr_head = f"{VALIDATION_ENVIRONMENT_CLASS}:{infrastructure}\n{stderr_head}"[:8_192]
    row: dict[str, Any] = {
        "command": command,
        "argv": argv,
        "declared_command": command,
        "declared_argv": [command],
        "executed_argv": argv,
        "argv_rewritten": True,
        "cwd": None,
        "env_override": None,
        "sandbox_backend": "docker",
        "execution_boundary": "docker_container_lane",
        "python_candidate_authority": None,
        "interpreter_authority": None,
        "toolchain_authority_receipt": {},
        "returncode": core.get("returncode"),
        "timed_out": bool(core.get("timed_out")),
        "timeout_seconds": lane.timeout_seconds if lane else 0,
        "duration_seconds": core.get("duration_seconds", 0.0),
        "stdout_head": str(core.get("stdout_head") or ""),
        "stdout_tail": str(core.get("stdout_tail") or ""),
        "stdout_truncated": bool(core.get("stdout_truncated")),
        "stderr_head": stderr_head,
        "stderr_tail": stderr_tail,
        "stderr_truncated": bool(core.get("stderr_truncated")),
        "cache_hit": cache_hit,
        "build_lane": {
            "schema_id": LANE_ROW_SCHEMA,
            "lane_id": lane.lane_id if lane else "",
            "image": lane.image if lane else "",
            "network": lane.network if lane else "",
            "cache_volume": lane.cache_volume if lane else "",
            "key": key,
            "tree_digest": tree_digest,
            "cache_hit": cache_hit,
            "queue_wait_seconds": queue_wait,
        },
    }
    if infrastructure:
        row["launch_error"] = INFRASTRUCTURE_LAUNCH_ERROR
        row["infrastructure_blocker"] = infrastructure
        row["failure_class"] = VALIDATION_ENVIRONMENT_CLASS
    if infrastructure or row["returncode"] != 0:
        from .worker_workspace import _validation_failure_receipt

        row["failure_receipt"] = _validation_failure_receipt(row)
    return row


def _infrastructure_row(
    command: str, lane: Lane | None, reason: str, **extra: Any
) -> dict[str, Any]:
    core = {"returncode": None, "infrastructure": reason, "docker_argv": [command]}
    return _row(command, lane, core, **extra)


def run_lane_validation(workspace: Any, command: str) -> dict[str, Any]:
    """Run one ``lane:<id>`` command and return a validation result row."""
    command = command.strip()
    lane: Lane | None = None
    try:
        lane = load_lane(lane_id_from_command(command))
        return _run_lane(workspace, command, lane)
    except LaneRefused as exc:
        return _infrastructure_row(command, lane, f"lane_refused:{exc}")
    except LaneInfrastructure as exc:
        return _infrastructure_row(command, lane, str(exc))
    except OSError as exc:
        return _infrastructure_row(
            command, lane, f"lane_io_error:{type(exc).__name__}:{exc}"[:300]
        )


def _run_lane(workspace: Any, command: str, lane: Lane) -> dict[str, Any]:
    root = _canonical_root()
    base = root / LANE_RUNTIME_RELATIVE
    candidate = Path(workspace.path)
    scratch = base / "scratch" / uuid.uuid4().hex
    snapshot, out = scratch / "src", scratch / "out"
    try:
        snapshot.mkdir(parents=True)
        out.mkdir()
        tree_digest = _snapshot(candidate, snapshot)
        key = _memo_key(lane, tree_digest)
        receipt = base / "receipts" / f"{key}.json"
        identity = {"key": key, "tree_digest": tree_digest}
        cached = _read_receipt(receipt, key)
        if cached is not None:
            return _row(command, lane, cached, cache_hit=True, **identity)
        with _lane_lock(base / "locks", lane.lane_id, QUEUE_WAIT_SECONDS) as wait:
            cached = _read_receipt(receipt, key)
            if cached is not None:
                return _row(command, lane, cached, cache_hit=True, queue_wait=wait, **identity)
            docker = _resolve_docker(root, candidate)
            core = _execute(_docker_argv(docker, lane, key, snapshot, out), lane, scratch)
            if not core["infrastructure"]:
                _write_receipt(receipt, key, lane, tree_digest, core)
            return _row(command, lane, core, queue_wait=wait, **identity)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
