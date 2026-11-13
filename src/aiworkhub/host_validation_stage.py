"""Coordinator-run host validation stage on a FULL candidate tree (NF-2026-02240).

Opt-in per card through two fields; a card carrying neither never reaches any
work below and behaves exactly as before:

* ``host_commands`` -- argv lists, each accepted only when the declared
  validation-command parser reads it back unchanged;
* ``host_sweep`` -- ``{"tests": [pytest node ids or files], "processes": N}``.

Under the hub resource ``cpu_heavy`` (FIFO, runtime DB) the stage builds the
full candidate tree with ``worker_workspace.create_combined_validation_workspace``
(whole HEAD tree + canonical delta + the sealed candidate overlay) and runs
``workspace_prepare``; runs ``host_commands`` in order, stopping at the first
failure; joins files those commands created or changed inside
``allowed_writes`` to the sealed candidate (hashes re-recorded with an audit
event) and only reports the ones outside it; then runs the sweep sharded over N
processes, N bounded by the observed core count with 25% headroom. A failing
shard is judged by the existing suite_vs_base owner against the pinned base, so
a test that is red on base too is not a candidate failure.

Every command and sweep shard runs with the argv/env the card validation lane
builds (``validation_command``): the same interpreter resolution, ``-P``,
``PYTHONPATH`` rooted in the candidate tree and a pytest basetemp inside it.

Accept never blocks on the stage: without a final result it starts the stage
on a background thread under an exclusive running marker and refuses with
``host_validation_pending``. The result is one JSON file per request; ``error``
(transient) is re-run by the next accept, ``failed`` is final. The review
packet shows it as its own section, accept refuses anything but ``passed``,
and the ready tree stays for ``aiworkhub_manager_candidate_tree``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import hub_resource_queue

RESULT_SCHEMA_ID = "aiworkhub.host_validation_stage.v1"
SECTION_SCHEMA_ID = "aiworkhub.host_validation_section.v1"
TREE_SCHEMA_ID = "aiworkhub.host_candidate_tree.v1"
RESEAL_SCHEMA_ID = "aiworkhub.host_stage_generated_join.v1"
GENERATED_JOINED_EVENT = "host_stage_generated_joined"
GENERATED_REVERTED_EVENT = "host_stage_generated_join_reverted"
PENDING_REFUSAL = "host_validation_pending"
ERROR_REFUSAL = "host_validation_error"
DISCARD_REFUSED = "discard_refused_outside_root"
HOST_FIELDS = ("host_commands", "host_sweep")
MAX_HOST_COMMANDS = 16
MAX_ARGV_TOKENS = 64
MAX_TOKEN_CHARS = 4096
MAX_SWEEP_TESTS = 512
MAX_TEST_ID_CHARS = 1024
HEADROOM_FRACTION = 0.25
DEFAULT_RESOURCE_WAIT_SECONDS = 6 * 3600.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 3600.0
MAX_OUTPUT_TAIL_BYTES = 256 * 1024
SECTION_TAIL_CHARS = 1024
MAX_STATE_FILE_BYTES = 8 * 1024 * 1024
MAX_REPORTED_PATHS = 200
MARKER_WRITE_GRACE_SECONDS = 60.0
RESULT_FILE = "result.json"
TREE_FILE = "tree.json"
RUNNING_FILE = "running.lock"
# OS advisory lock serializing every judge-and-remove of RUNNING_FILE.
MARKER_LOCK_FILE = "running.guard"
# Per-command scratch (pytest basetemp) inside the tree; never a generated file.
HOST_SCRATCH_DIR = ".aiwh-host-stage"
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_DRIVE = re.compile(r"^[A-Za-z]:")
_SHA256 = re.compile(r"[0-9a-f]{64}")

Row = dict[str, Any]
Runner = Callable[[Sequence[str], Path], Row]


class HostStageError(ValueError):
    """A refused host-stage input or step; ``str(exc)`` is a stable code."""


@dataclass(frozen=True)
class HostStageSpec:
    commands: tuple[tuple[str, ...], ...]
    sweep_tests: tuple[str, ...]
    sweep_processes: int


@dataclass
class TreeHandle:
    """One materialized tree: path, build record, delta reader, cleanup, lane runner."""

    path: Path
    record: dict[str, Any]
    changed: Callable[[], Iterable[str]]
    cleanup: Callable[[], None]
    runner: Runner | None = None


@dataclass
class TreeOps:
    build_candidate: Callable[[], TreeHandle]
    build_base: Callable[[], TreeHandle]
    # Removes a tree an earlier run left, from its persisted tree state; a
    # returned code (e.g. DISCARD_REFUSED) means it was skipped and is recorded.
    discard: Callable[[Mapping[str, Any]], str | None] | None = None


# --------------------------------------------------------------------------- spec


def card_requests_host_stage(card: object) -> bool:
    return isinstance(card, Mapping) and any(card.get(key) is not None for key in HOST_FIELDS)


def _whitespace_or_nul(value: str) -> bool:
    return any(char.isspace() or char == "\0" for char in value)


def _commands(raw: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(raw, list) or len(raw) > MAX_HOST_COMMANDS:
        raise HostStageError(f"host_commands_invalid:list_of_at_most_{MAX_HOST_COMMANDS}")
    from . import worker_workspace

    commands: list[tuple[str, ...]] = []
    for index, argv in enumerate(raw):
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > MAX_ARGV_TOKENS
            or any(
                not isinstance(token, str)
                or not token
                or len(token) > MAX_TOKEN_CHARS
                or _whitespace_or_nul(token)
                for token in argv
            )
        ):
            raise HostStageError(f"host_command_invalid:{index}:argv")
        try:
            parsed = worker_workspace.validation_argv(" ".join(argv))
        except worker_workspace.WorkspaceError as exc:
            raise HostStageError(f"host_command_invalid:{index}:{exc}"[:200]) from exc
        if parsed != list(argv):
            raise HostStageError(f"host_command_invalid:{index}:prefix_form_unsupported")
        commands.append(tuple(argv))
    return tuple(commands)


def _test_path(test_id: str) -> str:
    return test_id.split("::", 1)[0].replace("\\", "/")


def _normalized_node_id(test_id: str) -> str:
    """``./tests//x.py::t`` and ``tests\\x.py::t`` both read ``tests/x.py::t``."""
    path, separator, rest = test_id.partition("::")
    return posixpath.normpath(path.replace("\\", "/")) + separator + rest


def _node_id_lexically_valid(value: object) -> bool:
    """One relative pytest node id or file: never an option, absolute, drive or ``..``."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TEST_ID_CHARS
        or value.startswith(("-", "@"))
        or "\0" in value
    ):
        return False
    path_part = _test_path(value)
    pure = PurePosixPath(path_part)
    return (
        bool(path_part)
        and not pure.is_absolute()
        and ".." not in pure.parts
        and not _DRIVE.match(path_part)
    )


def _node_id_path(root: Path, node_id: object) -> Path | None:
    """The file of ``node_id`` under ``root``; ``None`` unless valid and contained."""
    if not _node_id_lexically_valid(node_id):
        return None
    assert isinstance(node_id, str)
    try:
        return _contained(root, _test_path(node_id))
    except HostStageError:
        return None


def _declared_covers(declared: str, node_id: str) -> bool:
    """Whether a reported node id is the declared test or lies within it."""
    declared, node_id = _normalized_node_id(declared), _normalized_node_id(node_id)
    if node_id == declared:
        return True
    declared_path, node_path = _test_path(declared).rstrip("/"), _test_path(node_id)
    if "::" in declared:
        # Its parametrized/nested ids, or a file-level error of its own file.
        return node_id.startswith((declared + "::", declared + "[")) or (
            "::" not in node_id and node_path == declared_path
        )
    return node_path == declared_path or node_path.startswith(declared_path + "/")


def _node_id_admitted(root: Path, node_id: object, declared: Sequence[str]) -> bool:
    """A reported failing id stays in ``root`` and inside one of the shard's tests."""
    return (
        _node_id_path(root, node_id) is not None
        and isinstance(node_id, str)
        and any(_declared_covers(test, node_id) for test in declared)
    )


def _sweep(raw: object) -> tuple[tuple[str, ...], int]:
    if not isinstance(raw, Mapping) or set(raw) - {"tests", "processes"}:
        raise HostStageError("host_sweep_invalid:keys_tests_and_processes_only")
    tests, processes = raw.get("tests"), raw.get("processes")
    if not isinstance(tests, list) or not tests or len(tests) > MAX_SWEEP_TESTS:
        raise HostStageError(f"host_sweep_tests_invalid:1_to_{MAX_SWEEP_TESTS}")
    for index, value in enumerate(tests):
        if not _node_id_lexically_valid(value) or _whitespace_or_nul(value):
            raise HostStageError(f"host_sweep_test_invalid:{index}")
    if len(set(tests)) != len(tests):
        raise HostStageError("host_sweep_tests_duplicate")
    if type(processes) is not int or processes < 1:
        raise HostStageError("host_sweep_processes_invalid:positive_int")
    return tuple(tests), processes


def parse_host_spec(card: object) -> HostStageSpec | None:
    """The card's host-stage spec, ``None`` for a card that declares none."""
    if not card_requests_host_stage(card):
        return None
    assert isinstance(card, Mapping)
    raw_commands, raw_sweep = card.get("host_commands"), card.get("host_sweep")
    commands = () if raw_commands is None else _commands(raw_commands)
    tests, processes = ((), 0) if raw_sweep is None else _sweep(raw_sweep)
    if not commands and not tests:
        raise HostStageError("host_stage_empty")
    return HostStageSpec(commands=commands, sweep_tests=tests, sweep_processes=processes)


def card_host_fields(
    host_commands: list[list[str]] | None, host_sweep: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Validated card fields for task creation; ``{}`` when neither is declared."""
    fields = {
        key: value
        for key, value in (("host_commands", host_commands), ("host_sweep", host_sweep))
        if value is not None
    }
    spec = parse_host_spec(fields)
    if spec is None:
        return {}
    out: dict[str, Any] = {}
    if host_commands is not None:
        out["host_commands"] = [list(argv) for argv in spec.commands]
    if host_sweep is not None:
        out["host_sweep"] = {"tests": list(spec.sweep_tests), "processes": spec.sweep_processes}
    return out


def sweep_process_bound(cpu_capacity: int | None = None) -> int:
    """Most sweep processes allowed: observed cores minus 25% headroom, at least 1."""
    from . import parallelism

    capacity = parallelism.get_cpu_capacity() if cpu_capacity is None else int(cpu_capacity)
    headroom = max(1, math.ceil(capacity * HEADROOM_FRACTION))
    return max(1, capacity - headroom)


# ------------------------------------------------------------------- execution


def _tail(path: Path) -> tuple[str, bool]:
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size > MAX_OUTPUT_TAIL_BYTES:
            handle.seek(size - MAX_OUTPUT_TAIL_BYTES)
        data = handle.read(MAX_OUTPUT_TAIL_BYTES)
    return data.decode("utf-8", "replace"), size > MAX_OUTPUT_TAIL_BYTES


def _terminate_tree(proc: subprocess.Popen[bytes]) -> None:
    from . import platform_io

    if platform_io.is_windows():
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            platform_io.signal_process_group(proc.pid, graceful=False)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_argv(
    argv: Sequence[str],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> Row:
    """Run one argv headless in its own process group; output kept as bounded tails.

    The process is owned by a ``with`` block and its whole tree is terminated
    on timeout or interruption; ``env`` is the lane-built environment.
    """
    from . import platform_io

    started = time.monotonic()
    row: Row = {"argv": list(argv), "cwd": str(cwd), "timeout_seconds": timeout_seconds}
    with tempfile.TemporaryDirectory(prefix="aiwh-host-stage-") as scratch:
        out_path, err_path = Path(scratch) / "stdout", Path(scratch) / "stderr"
        timed_out = False
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            try:
                proc = subprocess.Popen(
                    list(argv),
                    cwd=str(cwd),
                    env=None if env is None else dict(env),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    **platform_io.background_process_launch_kwargs(),
                )
            except OSError as exc:
                row.update(returncode=None, timed_out=False, spawn_error=type(exc).__name__)
                row["duration_seconds"] = round(time.monotonic() - started, 3)
                return row
            with proc:
                try:
                    returncode: int | None = proc.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate_tree(proc)
                    returncode = proc.wait()
                except BaseException:
                    _terminate_tree(proc)
                    proc.wait()
                    raise
        stdout_tail, stdout_truncated = _tail(out_path)
        stderr_tail, stderr_truncated = _tail(err_path)
    row.update(
        returncode=returncode,
        timed_out=timed_out,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    return row


def validation_command(workspace: Any, argv: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """``(argv, env)`` for one host command exactly as the card validation lane builds it.

    Same owner helpers, same order as ``worker_workspace.run_validations``:
    interpreter resolution (a declared ``.venv`` python resolves in the tree,
    else the canonical repository), the console-script ``pytest`` spelling,
    the trusted pytest runtime root plus the candidate ``src`` and the tree
    itself on ``PYTHONPATH`` (absolute, rooted in ``workspace.path``), the
    trusted-executable resolver that injects ``-P``, a fresh pytest basetemp
    inside the tree's own scratch, and the lane's ``sanitized_env("validation")``.
    """
    from . import runtime_temp, worker_workspace as ww

    tree = Path(workspace.path)
    tokens, _interpreter = ww._normalize_validation_interpreter_argv(workspace, list(argv))
    is_pytest = ww._is_pytest_validation_command(tokens)
    components: tuple[str, ...] = ()
    if is_pytest:
        components = (str(ww.resolve_trusted_pytest_runtime_root()),)
        scratch = tree / HOST_SCRATCH_DIR / uuid.uuid4().hex[:12]
        scratch.mkdir(parents=True)
        tokens = runtime_temp.pytest_basetemp_argv(
            ww._normalize_pytest_validation_argv(tokens), scratch
        )
    if ww._is_python_validation_command(tokens):
        components = ww._candidate_pythonpath_components(workspace, components)
    if is_pytest and "." not in components:
        components = (*components, ".")
    tokens, _roots, _authority = ww._normalize_trusted_validation_executable_argv_with_authority(
        tokens, Path(workspace.repo)
    )
    # The lane's own allowlisted environment, never the hub's ambient
    # ``os.environ``: no provider key, token or inherited PYTEST_ADDOPTS reaches
    # a host command. HOME is the tree's request HOME, else a private one in
    # the stage scratch (never reported as generated).
    home = getattr(workspace, "home", None) or tree / HOST_SCRATCH_DIR / "home"
    env = ww.sanitized_env(
        "validation", home=Path(home), repo=Path(workspace.repo), isolated_task_queue_db=True
    )
    if components:
        env["PYTHONPATH"] = os.pathsep.join(
            str(component) if Path(component).is_absolute() else str(tree / component)
            for component in components
        )
    else:
        env.pop("PYTHONPATH", None)
    return tokens, env


def host_runner(workspace: Any) -> Runner:
    """The runner of one tree: lane-built argv/env, then :func:`run_argv`."""

    def run(argv: Sequence[str], cwd: Path) -> Row:
        try:
            built, env = validation_command(workspace, argv)
        except Exception as exc:  # noqa: BLE001 -- a failed row naming the cause, never a pass
            return {
                "argv": list(argv),
                "returncode": None,
                "timed_out": False,
                "argv_error": f"{type(exc).__name__}:{exc}"[:300],
            }
        row = run_argv(built, cwd, env=env)
        row["declared_argv"] = list(argv)
        return row

    return run


def _handle_runner(tree: TreeHandle) -> Runner:
    """A tree's own lane runner; there is no bare :func:`run_argv` (``env=None``) fallback."""
    if tree.runner is None:
        raise HostStageError("host_validation_error:no_runner")
    return tree.runner


def _row_failed(row: Mapping[str, Any]) -> bool:
    return bool(row.get("timed_out")) or row.get("returncode") != 0


_SUMMARY_KEYS = (
    "argv", "declared_argv", "returncode", "timed_out", "duration_seconds", "spawn_error",
    "argv_error",
)


def _row_summary(row: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    summary = {key: row.get(key) for key in _SUMMARY_KEYS if key in row}
    summary["output_tail"] = (
        str(row.get("stdout_tail") or "") + str(row.get("stderr_tail") or "")
    )[-SECTION_TAIL_CHARS:]
    summary.update(extra)
    return summary


def _run_parallel(jobs: list[tuple[list[str], Path]], runner: Runner, workers: int) -> list[Row]:
    """Run jobs at most ``workers`` at a time; rows come back in job order."""
    if len(jobs) <= 1 or workers <= 1:
        return [runner(argv, cwd) for argv, cwd in jobs]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="host-sweep") as pool:
        return list(pool.map(lambda job: runner(job[0], job[1]), jobs))


def pytest_argv(python: str, tests: Sequence[str]) -> list[str]:
    """The declared shard command; each tree's runner resolves it like the lane does."""
    return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", *tests]


def run_sweep(
    tree: Path,
    tests: Sequence[str],
    processes: int,
    *,
    runner: Runner,
    python: str,
    build_base: Callable[[], TreeHandle],
    base_oid: str,
    cpu_capacity: int | None = None,
    base_runner: Callable[[TreeHandle], Runner] | None = None,
) -> dict[str, Any]:
    """Sharded sweep on ``tree``; failing shards judged against the base tree.

    ``base_runner`` picks the base tree's own runner; without one ``runner``
    runs the base shards too.
    """
    from . import process_launcher_validation as plv
    from .worker_workspace import WorkspaceError

    for test in tests:
        # Declared ids are lexically checked at card creation; here each one's
        # file must also resolve inside this tree (no symlinked escape). The
        # same id escapes on every rerun, so this is a final failure, never a
        # transient error that accept would re-run.
        if _node_id_path(tree, test) is None:
            return {
                "requested_processes": int(processes),
                "test_count": len(tests),
                "shards": [],
                "detail": f"sweep_test_outside_tree:{test}"[:200],
                "passed": False,
            }
    bound = sweep_process_bound(cpu_capacity)
    effective = max(1, min(int(processes), bound, len(tests)))
    shards = [tuple(tests[index::effective]) for index in range(effective)]
    rows = _run_parallel([(pytest_argv(python, shard), tree) for shard in shards], runner, effective)
    record: dict[str, Any] = {
        "requested_processes": int(processes),
        "process_bound": bound,
        "processes": effective,
        "test_count": len(tests),
        "shards": [
            _row_summary(row, shard=index, test_count=len(shards[index]))
            for index, row in enumerate(rows)
        ],
    }
    failing = [index for index, row in enumerate(rows) if _row_failed(row)]
    unparsed: list[int] = []
    candidate_ids: dict[int, frozenset[str]] = {}
    for index in failing:
        try:
            ids = plv.pytest_failed_node_ids(rows[index])
        except WorkspaceError:
            unparsed.append(index)
            continue
        # A reported id that is not one of this shard's declared tests, or whose
        # file leaves the tree, is never judged against base nor put on a base
        # pytest argv: the whole shard fails closed as unparsed.
        if all(_node_id_admitted(tree, node_id, shards[index]) for node_id in ids):
            candidate_ids[index] = ids
        else:
            unparsed.append(index)
    base_rows: dict[int, Row] = {}
    if candidate_ids:
        base = build_base()
        try:
            order: list[int] = []
            jobs: list[tuple[list[str], Path]] = []
            for index, ids in candidate_ids.items():
                on_base_paths = {node_id: _node_id_path(base.path, node_id) for node_id in ids}
                if any(path is None for path in on_base_paths.values()):
                    unparsed.append(index)
                    continue
                # A failing test whose file is absent on base cannot be red there.
                present = sorted(
                    node_id for node_id, path in on_base_paths.items() if path and path.exists()
                )
                if present:
                    order.append(index)
                    jobs.append((pytest_argv(python, present), base.path))
            on_base = base_runner(base) if base_runner is not None else runner
            for index, row in zip(order, _run_parallel(jobs, on_base, effective)):
                base_rows[index] = row
        finally:
            base.cleanup()
        for index in set(unparsed) & set(candidate_ids):
            del candidate_ids[index]
    new: set[str] = set()
    pre_existing: set[str] = set()
    new_count = pre_existing_count = 0
    for index in candidate_ids:
        judged = dict(rows[index])
        try:
            plv._record_pytest_suite_vs_base(
                judged, base_rows.get(index, {"returncode": 0}), base_oid
            )
        except WorkspaceError as exc:
            if str(exc) != "baseline_pytest_new_failures":
                unparsed.append(index)
                continue
        comparison = judged["baseline_comparison"]
        new.update(comparison["new_failures"])
        pre_existing.update(comparison["pre_existing"])
        new_count += int(comparison["new_count"])
        pre_existing_count += int(comparison["pre_existing_count"])
    if not failing:
        outcome = "sweep_passed"
    elif new_count:
        outcome = "baseline_new_failures"
    elif unparsed:
        outcome = "sweep_unparsed"
    else:
        outcome = "baseline_no_new_failures"
    record["comparison"] = {
        "schema_id": "aiworkhub.baseline_comparison.v1",
        "kind": "pytest_suite_vs_base",
        "outcome": outcome,
        "base_oid": base_oid,
        "new_count": new_count,
        "pre_existing_count": pre_existing_count,
        "new_failures": sorted(new)[: plv.PYTEST_FAILED_ID_CAP],
        "pre_existing": sorted(pre_existing)[: plv.PYTEST_FAILED_ID_CAP],
        "unparsed_shards": sorted(set(unparsed)),
    }
    record["passed"] = outcome in ("sweep_passed", "baseline_no_new_failures")
    return record


# ------------------------------------------------------------- generated files


def _hash_path(path: Path) -> str | None:
    """sha256 of a regular file, ``None`` when absent, a marker for anything else."""
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return "not_regular_file"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def snapshot(tree: Path, paths: Iterable[str]) -> dict[str, str | None]:
    return {relative: _hash_path(tree / relative) for relative in paths}


def detect_generated(
    before: Mapping[str, str | None], tree: Path, changed_after: Iterable[str]
) -> dict[str, str | None]:
    """Paths whose bytes the host commands changed, with their new digest.

    The stage's own per-command scratch (:data:`HOST_SCRATCH_DIR`) is never one.
    """
    after = set(changed_after)
    generated: dict[str, str | None] = {}
    for relative in sorted(after | set(before)):
        if relative.replace("\\", "/").split("/", 1)[0] == HOST_SCRATCH_DIR:
            continue
        now = _hash_path(tree / relative)
        if relative not in before or before[relative] != now:
            generated[relative] = now
    return generated


def partition_generated(
    generated: Mapping[str, str | None], allowed_writes: Iterable[str]
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """``(joinable, outside_allowed_writes, refused)``; only regular in-scope files join."""
    allowed = set(allowed_writes)
    joinable: dict[str, str] = {}
    outside: list[str] = []
    refused: dict[str, str] = {}
    for relative, digest in sorted(generated.items()):
        if relative not in allowed:
            outside.append(relative)
        elif digest is None:
            refused[relative] = "deletion_not_joined"
        elif digest == "not_regular_file":
            refused[relative] = "not_regular_file"
        else:
            joinable[relative] = digest
    return joinable, outside, refused


def _contained(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or ".." in pure.parts or _DRIVE.match(relative):
        raise HostStageError(f"host_generated_path_invalid:{relative}"[:200])
    target = root.joinpath(*pure.parts)
    resolved_root = root.resolve()
    if resolved_root not in target.resolve().parents:
        raise HostStageError(f"host_generated_path_escapes:{relative}"[:200])
    return target


def _missing_parents(root: Path, target: Path) -> list[Path]:
    """Directories between ``root`` and ``target`` that do not exist yet, outermost first."""
    missing: list[Path] = []
    parent = target.parent
    while parent != root and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    return list(reversed(missing))


def join_and_reseal(
    candidate_root: Path,
    tree: Path,
    joinable: Mapping[str, str],
    *,
    reseal: Callable[..., dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Reseal first, copy afterwards; any failure leaves the candidate at its sealed state.

    Every file is digest-checked and staged beside its target (nothing visible
    yet), then ``reseal(changes, event)`` records the new sealed hashes, and
    only then are the staged files moved into place. A reseal failure discards
    the staging; a move failure restores every replaced file and reverts the
    reseal with ``reseal(previous, reverted_event, previously_absent)``.
    """
    staged: list[tuple[str, Path, Path]] = []
    created: list[Path] = []
    replaced: list[tuple[Path, bytes | None]] = []
    joined: dict[str, str] = {}
    resealed: dict[str, Any] | None = None
    try:
        for relative, digest in sorted(joinable.items()):
            source, target = _contained(tree, relative), _contained(candidate_root, relative)
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise HostStageError(f"host_generated_changed_during_join:{relative}"[:200])
            missing = _missing_parents(candidate_root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            created.extend(missing)
            staging = target.with_name(f".{target.name}.host-stage-{uuid.uuid4().hex[:8]}")
            staged.append((relative, staging, target))
            staging.write_bytes(data)
            joined[relative] = digest
        resealed = reseal(joined, GENERATED_JOINED_EVENT)
        for _relative, staging, target in staged:
            previous = target.read_bytes() if target.is_file() else None
            os.replace(staging, target)
            replaced.append((target, previous))
    except BaseException:
        for target, previous in reversed(replaced):
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
        for _relative, staging, _target in staged:
            staging.unlink(missing_ok=True)
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:  # not empty: something else lives there, keep it
                pass
        if resealed is not None:
            # Files are back at their sealed bytes; put the sealed hashes back too:
            # entries that existed (a sealed deletion stays a deletion) are
            # restored, entries this join added are dropped.
            reseal(
                resealed["previous"], GENERATED_REVERTED_EVENT, resealed["previously_absent"]
            )
        raise
    assert resealed is not None
    return joined, resealed


def reseal_generated(
    db_path: Path,
    task_id: str,
    request_id: str,
    changes: Mapping[str, str | None],
    *,
    event: str = GENERATED_JOINED_EVENT,
    absent: Iterable[str] = (),
) -> dict[str, Any]:
    """Re-record sealed candidate hashes plus one audit event.

    ``changes`` sets entries exactly as the sealed evidence spells them: a
    sha256, or ``None`` -- the sealed record of a path the candidate DELETES,
    which stays a deletion. Removing an entry is never inferred from a value;
    it is the separate ``absent`` set. The payload's ``previous`` (entries that
    existed, deletions included) and ``previously_absent`` restore the seal
    exactly when passed back as ``changes`` and ``absent``.
    """
    from . import task_store

    dropped = {str(relative) for relative in absent}
    if dropped & set(changes):
        raise HostStageError("host_reseal_change_and_drop_overlap")
    for relative, digest in changes.items():
        if digest is not None and (type(digest) is not str or not _SHA256.fullmatch(digest)):
            raise HostStageError(f"host_reseal_digest_invalid:{relative}"[:200])
    with task_store._write_connection(Path(db_path)) as conn:
        task_store._begin_immediate(conn)
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            conn.rollback()
            raise HostStageError("host_reseal_task_missing")
        card = json.loads(row["card_json"])
        terminal = card.get("terminal_review") if isinstance(card, dict) else None
        evidence = terminal.get("evidence") if isinstance(terminal, dict) else None
        identity = evidence.get("request_identity") if isinstance(evidence, dict) else None
        hashes = evidence.get("changed_path_hashes") if isinstance(evidence, dict) else None
        if (
            not isinstance(identity, dict)
            or str(identity.get("request_id") or "") != request_id
            or not isinstance(hashes, dict)
        ):
            conn.rollback()
            raise HostStageError("host_reseal_sealed_evidence_mismatch")
        assert isinstance(evidence, dict)
        previous = {relative: hashes[relative] for relative in sorted(changes) if relative in hashes}
        previously_absent = sorted(relative for relative in changes if relative not in hashes)
        updated = {key: value for key, value in hashes.items() if key not in dropped}
        updated.update(changes)
        evidence["changed_path_hashes"] = dict(sorted(updated.items()))
        if isinstance(evidence.get("changed_paths"), list):
            evidence["changed_paths"] = sorted(
                (set(evidence["changed_paths"]) | set(changes)) - dropped
            )
        now = _now()
        conn.execute(
            "UPDATE tasks SET card_json = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(card, ensure_ascii=False, sort_keys=True), now, task_id),
        )
        payload = {
            "schema_id": RESEAL_SCHEMA_ID,
            "request_id": request_id,
            "joined": dict(sorted(changes.items())),
            "dropped": sorted(dropped),
            "previous": previous,
            "previously_absent": previously_absent,
        }
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, event, "coordinator", json.dumps(payload, sort_keys=True), now),
        )
        conn.commit()
    return payload


# ------------------------------------------------------------------ state files


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stage_dir(repo_root: str | os.PathLike[str], request_id: str) -> Path:
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise HostStageError("host_stage_request_id_invalid")
    return Path(repo_root) / ".aiworkhub" / "runtime" / "host_stage" / request_id


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
    try:
        staging.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), encoding="utf-8"
        )
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    """``None`` when absent; an ``unreadable`` record (never a pass) otherwise."""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STATE_FILE_BYTES + 1)
        if len(raw) > MAX_STATE_FILE_BYTES:
            return {"state": "unreadable", "reason": "too_large"}
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"state": "unreadable", "reason": type(exc).__name__}
    return payload if isinstance(payload, dict) else {"state": "unreadable", "reason": "not_object"}


def read_result(repo_root: str | os.PathLike[str], request_id: str) -> dict[str, Any] | None:
    try:
        directory = stage_dir(repo_root, request_id)
    except HostStageError:
        return {"state": "unreadable", "reason": "request_id_invalid"}
    return _read_json(directory / RESULT_FILE)


def _write_tree_state(directory: Path, request_id: str, state: str, tree: TreeHandle | None) -> None:
    _write_json(
        directory / TREE_FILE,
        {
            "schema_id": TREE_SCHEMA_ID,
            "request_id": request_id,
            "state": state,
            "path": str(tree.path) if tree is not None else None,
            "record": tree.record if tree is not None else None,
            "updated_at": _now(),
        },
    )


def _marker_live(marker: Path, is_alive: Callable[[int], bool] | None) -> bool:
    """A marker is live while its pid runs; a pid-less one only while being written."""
    from . import platform_io

    payload = _read_json(marker)
    if payload is None:
        return False
    pid = payload.get("pid")
    if type(pid) is not int or pid <= 0:
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            return False
        return age < MARKER_WRITE_GRACE_SECONDS
    return bool((is_alive or platform_io.process_is_alive)(pid))


def running_marker_live(
    directory: Path, *, is_alive: Callable[[int], bool] | None = None
) -> bool:
    return _marker_live(directory / RUNNING_FILE, is_alive)


@contextmanager
def _marker_lock(directory: Path) -> Iterator[None]:
    """Serialize every removal of one request's running marker on an OS lock.

    The kernel drops an advisory lock when its holder dies, so this lock never
    goes stale. A removal judges the marker and unlinks it while holding it: no
    other remover runs in between, and a fresh claim (O_CREAT|O_EXCL) can never
    create over a file that still exists -- so the file judged is the file
    removed, and a marker a live run just created is never deleted as stale.
    """
    from . import platform_io

    fd = platform_io.open_lock_file(directory / MARKER_LOCK_FILE)
    try:
        platform_io.lock_fd(fd, blocking=True)
        try:
            yield
        finally:
            platform_io.unlock_fd(fd)
    finally:
        os.close(fd)


def owns_running(directory: Path, token: str) -> bool:
    """Whether the running marker exists and carries exactly ``token``."""
    payload = _read_json(directory / RUNNING_FILE)
    return bool(token) and payload is not None and payload.get("token") == token


def claim_running(
    directory: Path, *, is_alive: Callable[[int], bool] | None = None
) -> str | None:
    """Create the exclusive running marker (O_CREAT|O_EXCL); its token, or ``None``.

    ``None`` means a live run holds it. An existing marker is judged under
    :func:`_marker_lock` and removed only when its pid is dead; the create is
    then retried, so a run that claimed in between wins and this returns ``None``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / RUNNING_FILE
    token = uuid.uuid4().hex
    for _attempt in range(2):
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            with _marker_lock(directory):
                if _marker_live(marker, is_alive):
                    return None
                marker.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": _now(), "token": token}, handle)
        return token
    return None


def release_running(directory: Path, token: str) -> None:
    """Remove the running marker only while it is still this run's own."""
    if not (directory / RUNNING_FILE).exists():
        return
    with _marker_lock(directory):
        if owns_running(directory, token):
            (directory / RUNNING_FILE).unlink(missing_ok=True)


# ----------------------------------------------------------------- orchestration


def _discard_previous_tree(
    directory: Path, request_id: str, tree_ops: TreeOps
) -> dict[str, Any] | None:
    """Remove the tree an earlier run of this request left, before building anew."""
    state = _read_json(directory / TREE_FILE)
    path = state.get("path") if isinstance(state, dict) else None
    if not isinstance(path, str) or not Path(path).exists():
        return None
    assert isinstance(state, dict)
    if tree_ops.discard is None:
        return {"path": path, "discarded": False, "reason": "no_discard_operation"}
    refusal = tree_ops.discard(state)
    if refusal:
        return {"path": path, "discarded": False, "reason": refusal}
    _write_tree_state(directory, request_id, "discarded", None)
    return {"path": path, "discarded": True}


def run_host_stage(
    *,
    repo_root: str | os.PathLike[str],
    request_id: str,
    task_id: str,
    spec: HostStageSpec,
    tree_ops: TreeOps,
    base_oid: str,
    candidate_root: Path,
    allowed_writes: Iterable[str],
    db_path: Path,
    runner: Runner | None = None,
    python: str = "python",
    cpu_capacity: int | None = None,
    resource_wait_seconds: float = DEFAULT_RESOURCE_WAIT_SECONDS,
    resource_kwargs: Mapping[str, Any] | None = None,
    marker_token: str | None = None,
) -> dict[str, Any]:
    """Run the whole stage once under ``cpu_heavy`` and the running marker; persist it.

    ``marker_token`` is a marker the caller already claimed; it must still be
    the live marker's own token, otherwise (gone, or now another run's) the
    stage returns ``pending`` without work and never releases that marker.
    Without one the stage claims its own and, when another run holds it,
    returns ``pending`` without doing any work. The marker is always released
    on the way out. ``runner`` overrides every tree's own validation-lane
    runner (tests).
    """
    pending = {"schema_id": RESULT_SCHEMA_ID, "request_id": request_id, "state": "pending"}
    directory = stage_dir(repo_root, request_id)
    if marker_token is not None and not owns_running(directory, marker_token):
        return pending
    token = marker_token if marker_token is not None else claim_running(directory)
    if token is None:
        return pending
    try:
        return _run_host_stage_claimed(
            directory=directory,
            request_id=request_id,
            task_id=task_id,
            spec=spec,
            tree_ops=tree_ops,
            base_oid=base_oid,
            candidate_root=candidate_root,
            allowed_writes=tuple(allowed_writes),
            db_path=db_path,
            runner=runner,
            python=python,
            cpu_capacity=cpu_capacity,
            resource_wait_seconds=resource_wait_seconds,
            resource_kwargs=resource_kwargs,
        )
    finally:
        release_running(directory, token)


def _run_host_stage_claimed(
    *,
    directory: Path,
    request_id: str,
    task_id: str,
    spec: HostStageSpec,
    tree_ops: TreeOps,
    base_oid: str,
    candidate_root: Path,
    allowed_writes: tuple[str, ...],
    db_path: Path,
    runner: Runner | None,
    python: str,
    cpu_capacity: int | None,
    resource_wait_seconds: float,
    resource_kwargs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_id": RESULT_SCHEMA_ID,
        "request_id": request_id,
        "task_id": task_id,
        "state": "error",
        "started_at": _now(),
    }
    try:
        with hub_resource_queue.held(
            db_path,
            hub_resource_queue.CPU_HEAVY,
            request_id,
            max_wait_seconds=resource_wait_seconds,
            **dict(resource_kwargs or {}),
        ) as lease:
            result["resource"] = lease.as_record()
            result["previous_tree"] = _discard_previous_tree(directory, request_id, tree_ops)
            _write_tree_state(directory, request_id, "building", None)
            try:
                tree = tree_ops.build_candidate()
            except BaseException:
                _write_tree_state(directory, request_id, "failed", None)
                raise
            try:
                tree_runner = runner or _handle_runner(tree)
                _write_tree_state(directory, request_id, "running", tree)
                result["tree"] = {"path": str(tree.path), "record": tree.record}
                before = snapshot(tree.path, tree.changed())
                command_rows: list[Row] = []
                for argv in spec.commands:
                    command_rows.append(tree_runner(list(argv), tree.path))
                    if _row_failed(command_rows[-1]):
                        break
                commands_ok = len(command_rows) == len(spec.commands) and not any(
                    _row_failed(row) for row in command_rows
                )
                result["host_commands"] = [_row_summary(row) for row in command_rows]
                generated = detect_generated(before, tree.path, tree.changed()) if spec.commands else {}
                joinable, outside, refused = partition_generated(generated, allowed_writes)
                joined: dict[str, str] = {}
                if joinable and commands_ok:
                    joined, result["reseal"] = join_and_reseal(
                        candidate_root,
                        tree.path,
                        joinable,
                        reseal=lambda changes, event, absent=(): reseal_generated(
                            db_path, task_id, request_id, changes, event=event, absent=absent
                        ),
                    )
                result["generated"] = {
                    "joined": joined,
                    "outside_allowed_writes": outside[:MAX_REPORTED_PATHS],
                    "outside_count": len(outside),
                    "refused": refused,
                    "not_joined": sorted(joinable) if joinable and not commands_ok else [],
                }
                sweep = None
                if spec.sweep_tests and commands_ok:
                    sweep = run_sweep(
                        tree.path,
                        spec.sweep_tests,
                        spec.sweep_processes,
                        runner=tree_runner,
                        python=python,
                        build_base=tree_ops.build_base,
                        base_oid=base_oid,
                        cpu_capacity=cpu_capacity,
                        base_runner=None if runner else _handle_runner,
                    )
                result["sweep"] = sweep
                if sweep is not None and sweep.get("detail"):
                    result["detail"] = sweep["detail"]
                passed = commands_ok and (sweep is None or bool(sweep["passed"]))
                result["state"] = "passed" if passed else "failed"
            except BaseException:
                _write_tree_state(directory, request_id, "failed", tree)
                tree.cleanup()
                raise
            _write_tree_state(directory, request_id, "ready", tree)
    except Exception as exc:  # noqa: BLE001 -- recorded as a named error, never a pass
        result["state"] = "error"
        result["error"] = f"{type(exc).__name__}:{exc}"[:500]
    result["finished_at"] = _now()
    _write_json(directory / RESULT_FILE, result)
    return result


def workspace_tree_ops(
    workspace: Any, card: Mapping[str, Any], candidate_paths: Iterable[str], *, state_dir: Path
) -> TreeOps:
    """Real trees: the combined full candidate tree and a full pinned-base tree."""
    from . import worker_materialize, worker_workspace as ww, workspace_prepare

    paths = sorted(candidate_paths)

    def _prepare(tree_path: Path) -> Any:
        receipt = workspace_prepare.prepare_once(workspace.repo, tree_path, state_dir=state_dir)
        return dict(receipt) if isinstance(receipt, Mapping) else str(receipt)

    def _handle(tree: Any, record: dict[str, Any]) -> TreeHandle:
        record["workspace"] = {
            key: str(getattr(tree, key)) for key in ("repo", "path", "home")
        }
        return TreeHandle(
            path=Path(tree.path),
            record=record,
            changed=lambda: list(ww.changed_paths(tree)),
            cleanup=lambda: ww.cleanup_workspace(tree.repo, tree.path, tree.home),
            runner=host_runner(tree),
        )

    def discard(state: Mapping[str, Any]) -> str | None:
        record = state.get("record")
        where = record.get("workspace") if isinstance(record, Mapping) else None
        if not isinstance(where, Mapping) or not all(
            isinstance(where.get(key), str) and where.get(key) for key in ("repo", "path", "home")
        ):
            raise HostStageError("host_stage_previous_tree_unrecorded")
        # A tampered tree.json never steers a recursive delete: the removed
        # path/home must resolve under the runtime worktrees root, and repo (the
        # git -C target, never deleted) under it too or be this stage's own repo.
        root = ww.configured_worktree_root(Path(workspace.repo)).resolve()
        resolved = {key: Path(where[key]).resolve() for key in ("repo", "path", "home")}
        if not (
            root in resolved["path"].parents
            and root in resolved["home"].parents
            and (
                root in resolved["repo"].parents
                or resolved["repo"] == Path(workspace.repo).resolve()
            )
        ):
            return DISCARD_REFUSED
        ww.cleanup_workspace(resolved["repo"], resolved["path"], resolved["home"])
        return None

    def build_candidate() -> TreeHandle:
        combined, record = ww.create_combined_validation_workspace(workspace, card, paths)
        handle = _handle(combined, dict(record))
        try:
            handle.record["workspace_prepare"] = _prepare(handle.path)
        except BaseException:
            handle.cleanup()
            raise
        return handle

    def build_base() -> TreeHandle:
        base_card = dict(card)
        base_card.pop("rework_predecessor", None)
        base = ww.create_workspace(
            workspace.repo,
            f"hostbase_{uuid.uuid4().hex}",
            base_card,
            "validation",
            pinned_base_oid=workspace.base_oid,
        )
        handle = _handle(base, {"base_oid": workspace.base_oid})
        try:
            worker_materialize.expand_full_tree(
                base.path,
                base_card,
                workspace.repo,
                run=ww._run,
                timeout=ww.WORKTREE_CREATE_TIMEOUT_SECONDS,
                failure=lambda detail: ww.WorkspaceError(
                    "host_stage_base_full_tree_failed:" + detail
                ),
            )
            handle.record["workspace_prepare"] = _prepare(handle.path)
        except BaseException:
            handle.cleanup()
            raise
        return handle

    return TreeOps(build_candidate=build_candidate, build_base=build_base, discard=discard)


def _sealed_paths(evidence: object) -> list[str]:
    hashes = evidence.get("changed_path_hashes") if isinstance(evidence, Mapping) else None
    return sorted(str(path) for path in hashes) if isinstance(hashes, Mapping) else []


def _start_background(
    directory: Path, request_id: str, token: str, stage_kwargs: Mapping[str, Any]
) -> None:
    """Run the stage on one thread that owns the claimed running marker."""

    def body() -> None:
        try:
            run_host_stage(**stage_kwargs, marker_token=token)
        finally:
            release_running(directory, token)

    thread = threading.Thread(target=body, name=f"host-stage-{request_id[:24]}", daemon=True)
    try:
        thread.start()
    except BaseException:
        release_running(directory, token)
        raise


def accept_step(
    repo_root: Path,
    card: Mapping[str, Any],
    request_id: str,
    task_id: str,
    workspace: Any,
    evidence: Mapping[str, Any],
) -> str:
    """``""`` when acceptance may continue, otherwise a stable refusal; never blocks.

    Without a final result (none yet, or a transient ``error``) the stage is
    started in the background under an exclusive running marker and accept
    refuses with ``host_validation_pending``; while that marker is live every
    accept refuses the same way and starts nothing. A later accept reads the
    persisted result: only ``passed`` lets acceptance continue.
    """
    if not card_requests_host_stage(card):
        return ""
    result = read_result(repo_root, request_id)
    if result is not None and result.get("state") != "error":
        return accept_gate(repo_root, card, request_id)
    try:
        spec = parse_host_spec(card)
    except HostStageError as exc:
        return f"host_validation_stage_spec_invalid:{exc}"[:200]
    paths = _sealed_paths(evidence)
    if spec is None or not paths:
        return "host_validation_stage_candidate_empty"
    try:
        directory = stage_dir(repo_root, request_id)
        if running_marker_live(directory):
            return PENDING_REFUSAL
        stage_kwargs: dict[str, Any] = {
            "repo_root": repo_root,
            "request_id": request_id,
            "task_id": task_id,
            "spec": spec,
            "tree_ops": workspace_tree_ops(workspace, card, paths, state_dir=directory),
            "base_oid": str(workspace.base_oid or ""),
            "candidate_root": Path(workspace.path),
            "allowed_writes": tuple(workspace.allowed_writes),
            "db_path": hub_resource_queue.default_db_path(repo_root),
        }
        token = claim_running(directory)
        if token is None:
            return PENDING_REFUSAL
        _start_background(directory, request_id, token, stage_kwargs)
    except Exception as exc:  # noqa: BLE001 -- a named refusal, never an escaping raise
        return f"{ERROR_REFUSAL}:{type(exc).__name__}:{exc}"[:200]
    return PENDING_REFUSAL


def accept_gate(repo_root: Path, card: Mapping[str, Any], request_id: str) -> str:
    """Refuse acceptance of a host-stage card unless this request's stage passed."""
    if not card_requests_host_stage(card):
        return ""
    result = read_result(repo_root, request_id)
    if result is None:
        return "host_validation_stage_missing"
    if result.get("state") != "unreadable" and result.get("request_id") != request_id:
        return "host_validation_stage_identity_mismatch"
    state = str(result.get("state") or "unknown")
    return "" if state == "passed" else f"host_validation_stage_{state}"


# ------------------------------------------------------------------- read views


def task_card(repo_root: str | os.PathLike[str], task_id: str) -> Mapping[str, Any] | None:
    """The stored card of ``task_id`` for the opt-in check; ``None`` when unknown."""
    if not isinstance(task_id, str) or not task_id:
        return None
    from . import task_store

    try:
        detail = task_store.get_task(repo_root, task_id)
    except Exception:  # noqa: BLE001 -- no card, no provable opt-in; accept still gates
        return None
    if not isinstance(detail, Mapping):
        return None
    nested = detail.get("card")
    return nested if card_requests_host_stage(nested) else detail


def review_packet_section(
    repo_root: str | os.PathLike[str], request_id: str, card: object = None
) -> dict[str, Any] | None:
    """Bounded review-packet section; ``None`` unless the card opted in and a stage ran."""
    if not card_requests_host_stage(card):
        return None
    result = read_result(repo_root, request_id)
    if result is None:
        return None
    sweep = result.get("sweep") if isinstance(result.get("sweep"), dict) else None
    return {
        "schema_id": SECTION_SCHEMA_ID,
        "state": str(result.get("state") or "unknown"),
        "error": result.get("error") or result.get("detail") or result.get("reason"),
        "resource": result.get("resource"),
        "host_commands": list(result.get("host_commands") or []),
        "generated": result.get("generated"),
        "sweep": (
            {
                key: sweep.get(key)
                for key in ("requested_processes", "process_bound", "processes", "test_count", "passed", "comparison")
            }
            if sweep is not None
            else None
        ),
        "candidate_tree": candidate_tree(repo_root, request_id),
        "finished_at": result.get("finished_at"),
    }


def candidate_tree(repo_root: str | os.PathLike[str], request_id: str) -> dict[str, Any]:
    """Read-only path and state of one request's full candidate tree."""
    base: dict[str, Any] = {"schema_id": TREE_SCHEMA_ID, "readonly": True, "request_id": request_id}
    try:
        state = _read_json(stage_dir(repo_root, request_id) / TREE_FILE)
    except HostStageError as exc:
        return {**base, "ok": False, "error": str(exc)}
    if state is None:
        return {**base, "ok": False, "error": "candidate_tree_not_found"}
    path = state.get("path")
    status = str(state.get("state") or "unknown")
    if status == "ready" and not (isinstance(path, str) and Path(path).is_dir()):
        status = "missing_on_disk"
    return {
        **base,
        "ok": status != "unreadable",
        "state": status,
        "ready": status == "ready",
        "path": path,
        "updated_at": state.get("updated_at"),
    }
