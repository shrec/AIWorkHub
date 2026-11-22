"""NF-2026-02364: the worker lane's combined snapshot, written only beneath its own root.

The worker MCP server runs inside the provider's Landlock ruleset, which grants
mutation only beneath the request-owned home/temp areas.  The host builder
(``worker_workspace.create_combined_validation_workspace``) runs ``git worktree
add`` and so must write ``<repo>/.git/worktrees`` and
``<repo>/.aiworkhub/runtime/worktrees``; inside that ruleset it always failed
with ``PermissionError`` and the worker fell back to its sparse worktree.

``create_worker_snapshot`` builds the same union under a writable root it is
handed: ``HEAD`` (minus the card's forbidden paths) through read-only Git
plumbing against the canonical repository (``rev-parse``, ``ls-tree``,
``cat-file --batch``), then the canonical worktree delta, then the candidate
paths -- with the host builder's own delta probe, overlay, merge and
unexpected/missing-delta rules.  A private ``git init`` and one baseline
commit INSIDE the new root (after the canonical overlay) let ``changed_paths``
observe exactly the candidate delta.  It never runs ``git worktree``, never
writes the canonical ``.git`` or ``.aiworkhub/runtime``, and
``cleanup_worker_snapshot`` removes only a root it created.  Every failure is a
``SnapshotStepError`` naming the step, so ``failure_reason`` can report
``full_snapshot_failed:<step>:<Class>:<errno>:<path>``.  The host lane keeps
``create_combined_validation_workspace`` unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import tempfile
import threading
import uuid
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import IO, Any, Iterable, Iterator, Mapping

from .worker_workspace import (
    MAX_SEED_FILES,
    WorkerWorkspace,
    WorkspaceError,
    _canonical_worktree_delta_paths,
    _hash_path,
    _overlay_regular_path,
    _relative_repo_path,
    _require_beneath,
    _rmtree_workspace_owned,
    _same_regular_file_content,
    changed_paths,
)
from . import platform_io

COMBINED_TREE_SCHEMA_ID = "aiworkhub.combined_tree.v1"  # the host record's schema
MATERIALIZER = "worker_snapshot_tree.v1"
SNAPSHOT_ROOT_DIRNAME = "aiworkhub-wsnap"
SNAPSHOT_ID_PREFIX = "wsnap_"
OWNER_MARKER = ".aiworkhub_worker_snapshot"
GIT_TIMEOUT_SECONDS = 600
MAX_TREE_ENTRIES = 500_000
MAX_TREE_BYTES = 8 * 1024 * 1024 * 1024
MAX_REASON_PATH_CHARS = 160
_IN_MEMORY_BLOB_BYTES = 16 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_SYMLINK_MODE = "120000"
_GITLINK_MODE = "160000"
_EXECUTABLE_MODE = "100755"
# An inherited repository location would point every Git call at another repo.
_REPO_LOCATION_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    }
)
# The baseline commit is private to the snapshot: fixed identity and date.
_BASELINE_IDENTITY = {
    "GIT_AUTHOR_NAME": "AIWorkHub Worker Snapshot",
    "GIT_AUTHOR_EMAIL": "worker-snapshot@aiworkhub.invalid",
    "GIT_AUTHOR_DATE": "946684800 +0000",
    "GIT_COMMITTER_NAME": "AIWorkHub Worker Snapshot",
    "GIT_COMMITTER_EMAIL": "worker-snapshot@aiworkhub.invalid",
    "GIT_COMMITTER_DATE": "946684800 +0000",
}
# Byte-exact files and no background work in the private repository, so
# ``changed_paths`` compares exactly the bytes this module wrote.
_PRIVATE_REPO_CONFIG = (
    "[core]\n\tautocrlf = false\n\tsafecrlf = false\n\tfsmonitor = false\n"
    "[gc]\n\tauto = 0\n"
)


class SnapshotStepError(WorkspaceError):
    """One named step of the worker snapshot failed; ``cause`` is the original error."""

    def __init__(self, step: str, cause: BaseException) -> None:
        super().__init__(f"worker_snapshot_failed:{step}:{type(cause).__name__}")
        self.step = step
        self.cause = cause


@contextlib.contextmanager
def _step(name: str) -> Iterator[None]:
    try:
        yield
    except SnapshotStepError:
        raise
    except Exception as exc:  # noqa: BLE001 - every cause is named by its step
        raise SnapshotStepError(name, exc) from exc


def _reason_path(filename: object, repo: Path | None) -> str:
    if not isinstance(filename, (str, bytes, os.PathLike)):
        return "-"
    try:
        text = os.fsdecode(filename)
    except (TypeError, ValueError):
        return "-"
    if not text:
        return "-"
    if repo is not None:
        try:
            text = (
                Path(text).resolve(strict=False)
                .relative_to(Path(repo).resolve(strict=False))
                .as_posix()
            )
        except (OSError, ValueError, RuntimeError):
            pass
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    return text[:MAX_REASON_PATH_CHARS] or "-"


def failure_reason(exc: BaseException, *, repo: Path | None) -> str:
    """``full_snapshot_failed:<step>:<Class>:<errno-or-dash>:<filename-or-dash>``.

    ``step`` is the builder step, or ``run`` for anything raised after the
    snapshot was built.  The filename is repo-relative when beneath ``repo``
    and bounded to ``MAX_REASON_PATH_CHARS``; no exception text or file
    content is ever included.
    """

    step, cause = (exc.step, exc.cause) if isinstance(exc, SnapshotStepError) else ("run", exc)
    code: object = "-"
    filename: object = None
    if isinstance(cause, OSError):
        if isinstance(cause.errno, int):
            code = cause.errno
        filename = cause.filename
    return (
        f"full_snapshot_failed:{step}:{type(cause).__name__}:{code}:"
        f"{_reason_path(filename, repo)}"
    )


def default_writable_root() -> Path:
    """Inside the worker sandbox ``TMPDIR`` is the request-owned temp Landlock grants."""

    return Path(tempfile.gettempdir()) / SNAPSHOT_ROOT_DIRNAME


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _REPO_LOCATION_ENV}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", **_BASELINE_IDENTITY)
    return env


def _git(cwd: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(),
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise WorkspaceError(f"worker_snapshot_git_failed:{args[0]}:{completed.returncode}")
    return completed.stdout


def _create_snapshot_dir(root: Path, source_request_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise WorkspaceError("worker_snapshot_root_not_directory")
    base = root.resolve()
    digest = hashlib.sha256(source_request_id.encode("utf-8")).hexdigest()[:16]
    snapshot_dir = base / f"{SNAPSHOT_ID_PREFIX}{digest}_{uuid.uuid4().hex[:12]}"
    snapshot_dir.mkdir(mode=0o700)
    (snapshot_dir / OWNER_MARKER).write_text(snapshot_dir.name, encoding="utf-8")
    (snapshot_dir / "worktree").mkdir()
    (snapshot_dir / "home").mkdir()
    return snapshot_dir


def _owned_snapshot_dir(snapshot_dir: Path) -> bool:
    marker = snapshot_dir / OWNER_MARKER
    try:
        return (
            snapshot_dir.name.startswith(SNAPSHOT_ID_PREFIX)
            and not snapshot_dir.is_symlink()
            and snapshot_dir.is_dir()
            and not marker.is_symlink()
            and marker.is_file()
            and marker.read_text(encoding="utf-8") == snapshot_dir.name
        )
    except (OSError, UnicodeDecodeError):
        return False


def cleanup_worker_snapshot(repo: Path, path: Path, home: Path) -> dict[str, Any] | None:
    """Remove one snapshot this module created; a removal failure is returned, not raised.

    Same ``(repo, path, home)`` signature as ``cleanup_workspace``; ``repo`` is
    never touched.  A tree without this module's owner marker is refused.
    """

    _ = repo
    path, home = Path(path), Path(home)
    snapshot_dir = path.parent
    if (
        path.name != "worktree"
        or home.name != "home"
        or home.parent != snapshot_dir
        or not _owned_snapshot_dir(snapshot_dir)
    ):
        raise WorkspaceError("refusing_unsafe_worker_snapshot_cleanup")
    try:
        _rmtree_workspace_owned(snapshot_dir)
    except (OSError, WorkspaceError) as exc:
        return {"residue": snapshot_dir.name, "error": type(exc).__name__}
    return None


def _safe_tree_path(name: str) -> str:
    pure = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or any(part.lower().rstrip(". ") == ".git" for part in pure.parts)
    ):
        raise WorkspaceError("worker_snapshot_tree_path_unsafe")
    return pure.as_posix()


def _tree_entries(repo: Path, head: str) -> list[tuple[str, str, str]]:
    raw = _git(repo, "ls-tree", "-r", "-z", "--full-tree", head)
    entries: list[tuple[str, str, str]] = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, separator, name = record.partition(b"\t")
        fields = meta.decode("ascii", "replace").split(" ")
        if not separator or len(fields) != 3:
            raise WorkspaceError("worker_snapshot_tree_listing_malformed")
        mode, _kind, oid = fields
        if mode != _GITLINK_MODE and mode != _SYMLINK_MODE and not mode.startswith("100"):
            raise WorkspaceError(f"worker_snapshot_tree_mode_unsupported:{mode[:12]}")
        entries.append((mode, oid, _safe_tree_path(name.decode("utf-8", "surrogateescape"))))
        if len(entries) > MAX_TREE_ENTRIES:
            raise WorkspaceError(f"worker_snapshot_tree_entry_limit_exceeded:{MAX_TREE_ENTRIES}")
    return entries


def _open_new(target: Path, *, executable: bool) -> IO[bytes]:
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    # Creation mode only (Git's own 0777/0666 under the umask); never a chmod.
    fd = os.open(target, flags, 0o777 if executable else 0o666)
    return os.fdopen(fd, "wb")


def _prepare_target(worktree: Path, relative: str) -> Path:
    target = worktree / relative
    _require_beneath(worktree, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _place(worktree: Path, relative: str, mode: str, data: bytes) -> None:
    target = _prepare_target(worktree, relative)
    if mode == _SYMLINK_MODE:
        try:
            os.symlink(data.decode("utf-8", "surrogateescape"), target)
            return
        except OSError:
            if not platform_io.is_windows():
                raise
            # Git's own ``core.symlinks=false`` checkout: a file holding the link text.
    with _open_new(target, executable=mode == _EXECUTABLE_MODE) as handle:
        handle.write(data)


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, _COPY_CHUNK_BYTES))
        if not chunk:
            raise WorkspaceError("worker_snapshot_blob_stream_truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_object(
    stream: IO[bytes], size: int, worktree: Path, targets: list[tuple[str, str]]
) -> None:
    if size <= _IN_MEMORY_BLOB_BYTES:
        data = _read_exact(stream, size)
        for mode, relative in targets:
            _place(worktree, relative, mode, data)
        return
    first_mode, first = next(
        ((m, r) for m, r in targets if m != _SYMLINK_MODE), (targets[0][0], targets[0][1])
    )
    if first_mode == _SYMLINK_MODE:
        raise WorkspaceError("worker_snapshot_symlink_target_too_large")
    target = _prepare_target(worktree, first)
    with _open_new(target, executable=first_mode == _EXECUTABLE_MODE) as handle:
        remaining = size
        while remaining:
            chunk = _read_exact(stream, min(remaining, _COPY_CHUNK_BYTES))
            handle.write(chunk)
            remaining -= len(chunk)
    for mode, relative in targets:
        if relative == first:
            continue
        if mode == _SYMLINK_MODE:
            raise WorkspaceError("worker_snapshot_symlink_target_too_large")
        with target.open("rb") as source, _open_new(
            _prepare_target(worktree, relative), executable=mode == _EXECUTABLE_MODE
        ) as handle:
            while chunk := source.read(_COPY_CHUNK_BYTES):
                handle.write(chunk)


def _feed(stdin: IO[bytes], request: bytes) -> None:
    try:
        stdin.write(request)
        stdin.close()
    except OSError:  # the reader killed the child; its error is the one reported
        pass


def _stream_blobs(repo: Path, worktree: Path, blobs: Mapping[str, list[tuple[str, str]]]) -> None:
    order = sorted(blobs)
    if not order:
        return
    request = "".join(f"{oid}\n" for oid in order).encode("ascii")
    total = 0
    with subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_git_env(),
    ) as proc:
        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise WorkspaceError("worker_snapshot_git_pipes_unavailable")
        feeder = threading.Thread(target=_feed, args=(proc.stdin, request), daemon=True)
        feeder.start()
        try:
            for oid in order:
                fields = proc.stdout.readline().decode("ascii", "replace").split()
                if (
                    len(fields) != 3
                    or fields[0] != oid
                    or fields[1] != "blob"
                    or not fields[2].isdigit()
                ):
                    raise WorkspaceError("worker_snapshot_blob_unreadable")
                size = int(fields[2])
                total += size
                if total > MAX_TREE_BYTES:
                    raise WorkspaceError(
                        f"worker_snapshot_tree_byte_limit_exceeded:{MAX_TREE_BYTES}"
                    )
                _write_object(proc.stdout, size, worktree, list(blobs[oid]))
                if proc.stdout.read(1) != b"\n":
                    raise WorkspaceError("worker_snapshot_blob_stream_desync")
        except BaseException:
            proc.kill()
            raise
        finally:
            feeder.join()
    if proc.returncode != 0:
        raise WorkspaceError(f"worker_snapshot_git_failed:cat-file:{proc.returncode}")


def _materialize_head(repo: Path, worktree: Path, head: str) -> None:
    blobs: dict[str, list[tuple[str, str]]] = {}
    for mode, oid, relative in _tree_entries(repo, head):
        if mode == _GITLINK_MODE:  # a submodule checks out as its empty directory
            _prepare_target(worktree, relative).mkdir(exist_ok=True)
            continue
        blobs.setdefault(oid, []).append((mode, relative))
    _stream_blobs(repo, worktree, blobs)


def _init_private_repo(worktree: Path) -> None:
    _git(worktree, "-c", "init.defaultBranch=snapshot", "init", "-q")
    with (worktree / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write(_PRIVATE_REPO_CONFIG)


def _baseline_commit(worktree: Path) -> str:
    # ``-f``: a HEAD-tracked path matching .gitignore is still baseline content.
    _git(worktree, "add", "-A", "-f", "--", ".")
    tree = _git(worktree, "write-tree").decode("ascii").strip()
    commit = _git(
        worktree, "commit-tree", tree, "-m", "aiworkhub worker snapshot baseline"
    ).decode("ascii").strip()
    _git(worktree, "update-ref", "HEAD", commit)
    return commit


def create_worker_snapshot(
    source_workspace: WorkerWorkspace,
    card: Mapping[str, Any],
    candidate_changed_paths: Iterable[str],
    *,
    writable_root: Path | None = None,
) -> tuple[WorkerWorkspace, dict[str, Any]]:
    """The worker lane's ``create_snapshot``: canonical state plus one candidate delta.

    Same contract as ``create_combined_validation_workspace`` -- union
    ``allowed_writes``, the same overlays and refusals, the same combined-tree
    record -- but every write lands beneath ``writable_root`` (default
    ``default_writable_root()``).  The caller owns cleanup through
    :func:`cleanup_worker_snapshot`.
    """

    repo = source_workspace.repo.resolve()
    with _step("candidate"):
        candidate = sorted({_relative_repo_path(value) for value in candidate_changed_paths})
        if not candidate:
            raise WorkspaceError("combined_tree_candidate_empty")
    root = Path(writable_root) if writable_root is not None else default_writable_root()
    with _step("create_root"):
        snapshot_dir = _create_snapshot_dir(root, source_workspace.request_id)
    try:
        return _populate(source_workspace, card, candidate, repo, snapshot_dir)
    except BaseException:
        try:
            _rmtree_workspace_owned(snapshot_dir)
        except (OSError, WorkspaceError):
            pass  # the build failure is the reported cause
        raise


def _populate(
    source_workspace: WorkerWorkspace,
    card: Mapping[str, Any],
    candidate: list[str],
    repo: Path,
    snapshot_dir: Path,
) -> tuple[WorkerWorkspace, dict[str, Any]]:
    worktree = snapshot_dir / "worktree"
    with _step("resolve_head"):
        head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    with _step("canonical_delta"):
        canonical_delta = _canonical_worktree_delta_paths(repo)
        union_allowed = sorted(set(source_workspace.allowed_writes) | set(canonical_delta))
        if len(union_allowed) > MAX_SEED_FILES:
            raise WorkspaceError(f"combined_tree_path_limit_exceeded:{len(union_allowed)}")
        union_card = dict(card)
        union_card.pop("rework_predecessor", None)  # never re-inherit it (NF-2026-01735)
        union_card["allowed_writes"] = union_allowed
    with _step("init"):
        _init_private_repo(worktree)
    with _step("materialize_head"):
        _materialize_head(repo, worktree, head)
    with _step("remove_forbidden"):
        from . import worker_materialize

        def failure(detail: str) -> WorkspaceError:
            return WorkspaceError("combined_tree_full_union_failed:" + detail)

        worker_materialize.checkout_policy(union_card, repo, error=failure).remove_forbidden(
            worktree, failure=failure
        )
    with _step("canonical_overlay"):
        for relative in canonical_delta:
            _overlay_regular_path(repo, worktree, relative)
    with _step("baseline_commit"):
        _baseline_commit(worktree)
    keys = [path for path in union_allowed if not any(ch in path for ch in "*?[")]
    snapshot = WorkerWorkspace(
        request_id=snapshot_dir.name,
        repo=repo,
        path=worktree,
        home=snapshot_dir / "home",
        allowed_writes=tuple(union_allowed),
        parent_baseline={relative: _hash_path(repo / relative) for relative in keys},
        workspace_baseline={relative: _hash_path(worktree / relative) for relative in keys},
        base_oid=head,
    )
    with _step("merge_plan"):
        from .promotion_merge import merged_path_hashes, plan_merges  # NF-2026-01381 part A

        merges = plan_merges(source_workspace, candidate)
    with _step("candidate_overlay"):
        for relative in candidate:
            if relative in merges:
                _prepare_target(worktree, relative).write_bytes(merges[relative])
            else:
                _overlay_regular_path(source_workspace.path, worktree, relative)
    with _step("observe"):
        # The private repository is no detached worktree: diff its baseline HEAD.
        observed = changed_paths(replace(snapshot, base_oid=None))
        unexpected = sorted(set(observed) - set(candidate))
        not_observed = sorted(set(candidate) - set(observed))
        already_in_canonical = [
            relative
            for relative in not_observed
            if _same_regular_file_content(source_workspace.path / relative, repo / relative)
        ]
        missing = sorted(set(not_observed) - set(already_in_canonical))
    if unexpected:
        raise SnapshotStepError(
            "unexpected_delta",
            WorkspaceError("combined_tree_unexpected_delta:" + ",".join(unexpected[:20])),
        )
    if missing:
        raise SnapshotStepError(
            "candidate_not_materialized",
            WorkspaceError("combined_tree_candidate_not_materialized:" + ",".join(missing[:20])),
        )
    return snapshot, {
        "schema_id": COMBINED_TREE_SCHEMA_ID,
        "materializer": MATERIALIZER,
        "candidate_paths": candidate,
        "canonical_delta_paths": canonical_delta,
        "observed_candidate_paths": observed,
        "candidate_paths_already_in_canonical": already_in_canonical,
        "merged_paths": merged_path_hashes(merges),
    }
