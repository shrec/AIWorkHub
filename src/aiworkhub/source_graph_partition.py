"""Partitioned indexing and composed views for the canonical Source Graph.

Owner mandate (card NF-302-SG-PARTITION): a reviewer must get a
candidate-accurate index WITHOUT the old full-index clone. Cloning the entire
canonical database with the SQLite backup API and re-indexing only the changed
files made preparation cost proportional to repository size -- a dead end, not
an inefficiency. This module replaces that clone with two composable pieces:

  * A **partition** is an index over a bounded, explicitly declared scope -- a
    set of files -- built and stored independently of the base index. Building
    a partition costs time and bytes proportional to that scope ALONE. It never
    reads, copies, or writes the base index. :func:`build_partition` reuses the
    exact-file :func:`aiworkhub.source_graph.index_file` /
    :func:`aiworkhub.source_graph.remove_file` primitives against a fresh,
    empty database bound context-locally through
    ``source_graph.database_path_override``.

  * A **composed view** binds one base index (opened read-only) plus one or
    more partitions and answers queries as if it were a single index.
    Precedence is explicit and enforced by ``TEMP`` views installed over the
    live connection:

      - a file present in a partition resolves from the partition; the base
        rows for that file are invisible (partition wins per file);
      - a file absent from every partition resolves from the base;
      - a file declared in a partition's scope but deleted resolves to nothing
        (excluded from both sides).

    The declared scope -- every changed path, including deletions that leave no
    partition row -- is what hides the base rows, so a delete is precedence-
    correct even though it stores no row.

Cross-boundary edge resolution (:func:`resolve_cross_boundary`) links the two
sides in BOTH directions and NAMES the cases it cannot resolve rather than
guessing: see that function's docstring for the exact resolved/unresolved
contract.

Immutability and identity: a :class:`ComposedView` is a frozen value bound to
``packet_sha256`` / ``target_request_id`` / ``target_task_id`` and to a base
fingerprint (size + mtime). :meth:`ComposedView.verify_binding` refuses a view
whose identity does not match a caller's packet, and every open re-checks that
the base has not shifted underneath the view (:class:`PartitionBaseShiftError`).

    Base generation pinning (NF-2026-00946): the canonical base is published by
    atomic replacement, so a marker that named only the mutable canonical path made
    every ordinary publication break every in-flight reviewer/rework overlay. A
    marked partition therefore pins the exact base generation it was built against
    as a hard link (same inode, O(1)) next to the partition file. A cross-device
    link (``EXDEV`` only, NF-2026-01011) falls back to a verified byte copy whose
    recorded generation is the pin file's own identity, not the source inode;
    other unsupported link errors are recorded and a later shift fails with an
    explicit ``composed_base_shifted_unpinned`` reason. Reads compose with that
    pinned generation and verify its identity (device, inode, size, mtime_ns), so
    a newer canonical generation never leaks into a sealed review and a replaced
    or mutated pin fails closed. Pins no partition in their directory references
    any more are pruned on the next marker write.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sqlite3
import stat as stat_mod
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import source_graph as sg
from . import sqlite_readonly

COMPOSED_VIEW_META_KEY = "composed_view"
COMPOSED_VIEW_SCHEMA_ID = "aiworkhub.source_graph.composed_view.v1"
BASE_PIN_SCHEMA_ID = "aiworkhub.source_graph.base_pin.v1"
_SCOPE_TABLE = "_partition_scope"
_COMPOSED_TABLES: tuple[str, ...] = ("files", "entities", "edges")
_BASE_PIN_NAME = re.compile(r"\.sg-base-pin\.[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\.sqlite")
_BASE_PIN_REQUIRED_TABLES = frozenset(
    {"meta", "files", "entities", "edges", "entities_fts"}
)
# ``link`` errnos meaning "this filesystem/platform/sandbox cannot hard-link",
# as opposed to a genuine I/O failure that must propagate. ``EXDEV`` is the
# only member recovered by a verified byte copy; the rest stay unsupported.
_BASE_PIN_UNSUPPORTED_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "EXDEV", "EPERM", "EACCES", "EROFS", "EMLINK", "ENOSYS", "ENOTSUP",
        "EOPNOTSUPP",
    )
    if hasattr(errno, name)
)


class PartitionError(RuntimeError):
    """Base class for partition / composed-view failures."""


class PartitionBindingError(PartitionError):
    """A composed view was asked to serve a packet it is not bound to."""


class PartitionBaseShiftError(PartitionError):
    """The base index changed underneath an open (or opening) composed view."""


class PartitionBasePinError(PartitionBaseShiftError):
    """The pinned base generation is missing, forged, replaced or unverifiable."""


# ---------------------------------------------------------------------------
# Partition build -- scope-only, never touches the base index
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PartitionBuildReport:
    """What one :func:`build_partition` produced -- the measurable deliverable.

    ``bytes_written`` and ``wall_seconds`` are the cost of preparing the
    partition; both scale with ``scope`` (the changed files), never with the
    base index size, because the base is never read here. ``base_pin`` is the
    pin status (``pinned``, ``unsupported:<reason>`` or ``none``) and
    ``pin_seconds`` the measured cost of recording the marker and pinning the
    base generation -- a hard link plus a stat, or an ``EXDEV`` byte copy.
    """

    partition_db_path: str
    scope: tuple[str, ...]
    files_indexed: int
    files_deleted: int
    files_skipped: int
    entities: int
    edges: int
    bytes_written: int
    wall_seconds: float
    base_pin: str = "none"
    pin_seconds: float = 0.0
    failed_files: tuple[dict[str, str], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "partition_db_path": self.partition_db_path,
            "scope": list(self.scope),
            "files_indexed": self.files_indexed,
            "files_deleted": self.files_deleted,
            "files_skipped": self.files_skipped,
            "entities": self.entities,
            "edges": self.edges,
            "bytes_written": self.bytes_written,
            "wall_seconds": self.wall_seconds,
            "base_pin": self.base_pin,
            "pin_seconds": self.pin_seconds,
            "failed_files": [dict(item) for item in self.failed_files],
        }


def _admits(
    policy: sg.SourceGraphIgnorePolicy,
    relative: str,
    *,
    repo_root: Path | None = None,
) -> bool:
    """Mirror the canonical :func:`source_graph.iter_source_files` admission.

    The base index admits a file only when the repository policy admits it, so
    the partition gates on exactly the SAME rule -- otherwise a changed path the
    base deliberately omits (a disabled language, an excluded directory, a
    repository exclude glob) would still get a partition row and the two sides
    would no longer be built alike. This checks the policy's
    ``indexed_extensions`` (which already reflects disabled languages), the
    excluded directory basenames, and the repository exclude globs at both the
    directory and file level. A path the policy rejects is skipped, exactly as a
    non-indexable ``.txt`` is -- never a build failure. Chosen over admitting a
    declared path unconditionally so the partition can never diverge from the
    base's contents. When ``repo_root`` is given, a parent directory that is a
    nested linked git worktree is refused exactly as the walk prunes it.
    """

    posix = Path(relative).as_posix()
    if Path(relative).suffix.casefold() not in policy.indexed_extensions:
        return False
    parts = posix.split("/")
    for depth, name in enumerate(parts[:-1]):
        if name in policy.exclude_dirs or name.endswith(".egg-info"):
            return False
        if repo_root is not None and sg._is_nested_linked_worktree_dir(
            repo_root, repo_root.joinpath(*parts[: depth + 1])
        ):
            return False
        if sg._glob_ignored(
            "/".join(parts[: depth + 1]), policy.exclude_globs, is_dir=True
        ):
            return False
    return not sg._glob_ignored(posix, policy.exclude_globs)


def admitted_changed_path(
    repo_root: Path,
    relative: str,
    policy: sg.SourceGraphIgnorePolicy,
) -> bool:
    """The ONE admit decision for a packet path: will it get a partition row?

    True exactly when ``relative`` names an existing regular file below the
    resolved ``repo_root`` that :func:`_admits` accepts WITH ``repo_root`` (so a
    nested linked worktree parent is refused on every side alike).
    :func:`build_partition` indexes exactly these paths and the reviewer
    candidate readiness check requires a row for exactly these paths, so the
    builder and readiness can never disagree about which paths need a row.
    """

    root = Path(repo_root).resolve()
    if not (root / relative).resolve(strict=False).is_file():
        return False
    return _admits(policy, relative, repo_root=root)


def build_partition(
    repo_root: Path,
    changed_paths: Sequence[Mapping[str, str]],
    partition_db_path: Path,
    *,
    base_db_path: Path | None = None,
) -> PartitionBuildReport:
    """Build an index over ``changed_paths`` ALONE into ``partition_db_path``.

    ``changed_paths`` is the packet-shaped ``[{"path": rel, "sha256": hex}]``
    list. For each entry:

      * an existing file the repository policy admits is indexed exactly
        (hash-bound) through :func:`source_graph.index_file`;
      * an existing file the policy does NOT admit -- a non-indexable suffix, a
        disabled language, an excluded directory or exclude glob -- is skipped
        and never blocks the build. Admission is deliberately routed through the
        SAME :func:`source_graph.load_ignore_policy` the base uses (see
        :func:`_admits`), so the partition and the base are built by one rule
        and a path the base omits never gets a partition row;
      * a path that no longer exists is a deletion -- removed via
        :func:`source_graph.remove_file` so the partition carries no row for
        it, while its scope entry still hides the base row at query time.

    The database is created fresh and empty; the base index is NEVER copied or
    content-hashed here. When ``base_db_path`` is given, a composed-view
    marker (base generation + full declared scope) is written and that exact
    base generation is pinned by hard link beside the partition (see
    :func:`write_composed_marker`), so a later read-only open composes the two
    indexes even after the canonical base is atomically republished; without
    it the partition stands alone. The generation identity is device + inode +
    size + mtime_ns, with no content hash: an in-place rewrite preserving all
    four is a documented, accepted blind spot, not a detected shift. Source
    Graph contract failures (hash mismatch, unreadable file, parser error) for
    one file are recorded in ``failed_files`` and the build continues; the path
    stays in scope so its stale base row remains hidden. Path-safety
    violations raise :class:`PartitionError` and abort the whole build.
    """

    repo_root = Path(repo_root).resolve()
    partition_db_path = Path(partition_db_path)
    policy = sg.load_ignore_policy(repo_root)
    started = time.monotonic()

    # Create the empty schema up front so the composed marker can always be
    # written and readiness checks always find the schema, even for a scope of
    # only deletions or non-indexable paths.
    schema_conn = sg.connect(partition_db_path)
    schema_conn.close()

    scope: list[str] = []
    failed_files: list[dict[str, str]] = []
    files_indexed = files_deleted = files_skipped = 0
    entities = edges = 0
    try:
        with sg.database_path_override(partition_db_path):
            for changed in changed_paths:
                relative = str(changed.get("path") or "")
                expected_hash = str(changed.get("sha256") or "")
                if not relative:
                    raise PartitionError("partition_changed_path_missing")
                scope.append(relative)
                raw = repo_root / relative
                candidate = raw.resolve(strict=False)
                if not candidate.is_relative_to(repo_root):
                    raise PartitionError(
                        f"quality_review_candidate_path_escapes_workspace:{relative}"
                    )
                # ``resolve`` canonicalizes the final component, so a symlink must
                # be tested on the UNRESOLVED path (as worker_ai_tools_mcp does on
                # db_path); a post-resolve ``is_symlink`` can never fire.
                if raw.is_symlink():
                    raise PartitionError(
                        f"quality_review_candidate_path_symlink:{relative}"
                    )
                if candidate.is_file():
                    if not admitted_changed_path(repo_root, relative, policy):
                        files_skipped += 1
                        continue
                    try:
                        # ``admitted_changed_path`` is the one admit decision for
                        # a packet path, so the gitignore refusal is not reapplied.
                        result = sg.index_file(
                            repo_root, relative, expected_hash, admit_gitignored=True,
                        )
                    except sqlite3.Error:
                        raise
                    except Exception as exc:
                        # The path stays in ``scope`` so the stale base row is
                        # still hidden; only this file's partition row is lost.
                        failed_files.append({
                            "path": relative,
                            "step": "index_file",
                            "reason": f"{type(exc).__name__}: {exc}"[:160],
                        })
                        try:
                            sg.remove_file(repo_root, relative)
                        except Exception:
                            pass
                        continue
                    files_indexed += 1
                    entities += int(result.get("entities") or 0)
                    edges += int(result.get("edges") or 0)
                elif candidate.exists():
                    raise PartitionError(
                        f"quality_review_candidate_path_not_file:{relative}"
                    )
                else:
                    sg.remove_file(repo_root, relative)
                    files_deleted += 1
    finally:
        # NF-2026-02391: the writer lease is keyed by this partition's own DB,
        # so its lock file is private to this build; remove it so the overlay
        # directory does not gain one lock file per review.
        with sg.database_path_override(partition_db_path):
            lease_path = sg._index_write_lease_path(repo_root)
        try:
            lease_path.unlink(missing_ok=True)
        except OSError:
            pass

    pin_started = time.monotonic()
    pin = write_composed_marker(partition_db_path, base_db_path, scope)
    pin_seconds = max(0.0, time.monotonic() - pin_started)
    if pin is None:
        base_pin = "none"
    elif pin.get("status") == "pinned":
        base_pin = "pinned"
    else:
        base_pin = f"unsupported:{pin.get('reason') or 'unknown'}"
    try:
        bytes_written = int(partition_db_path.stat().st_size)
    except OSError:
        bytes_written = 0
    return PartitionBuildReport(
        partition_db_path=str(partition_db_path),
        scope=tuple(scope),
        files_indexed=files_indexed,
        files_deleted=files_deleted,
        files_skipped=files_skipped,
        entities=entities,
        edges=edges,
        bytes_written=bytes_written,
        wall_seconds=max(0.0, time.monotonic() - started),
        base_pin=base_pin,
        pin_seconds=pin_seconds,
        failed_files=tuple(failed_files),
    )


# ---------------------------------------------------------------------------
# Composed-view marker -- how a read-only open discovers its base + scope
# ---------------------------------------------------------------------------

def _base_fingerprint(base_db_path: Path) -> dict[str, Any]:
    """Cheap base identity: path + size + mtime_ns, with NO content hash.

    Content-hashing a 100+ MB base on every open would reintroduce exactly the
    base-size-proportional cost this module exists to remove, so the shift check
    is deliberately size + mtime_ns only. The accepted, documented blind spot:
    an in-place base replacement that preserves BOTH size and mtime_ns is not
    detected as a shift. The security review rated this low residual risk -- the
    base is published atomically (a new inode with a fresh mtime), not rewritten
    in place -- and it is recorded here so the next reader need not rediscover it.
    """

    resolved = Path(base_db_path).resolve()
    stat = resolved.stat()
    return {
        "db_path": str(resolved),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _generation(observed: os.stat_result) -> dict[str, int]:
    """Identity of one base generation: device + inode + size + mtime_ns."""

    return {
        "dev": int(observed.st_dev),
        "ino": int(observed.st_ino),
        "size": int(observed.st_size),
        "mtime_ns": int(observed.st_mtime_ns),
    }


def _pin_generation_from(base: Mapping[str, Any]) -> dict[str, int]:
    return {key: int(base.get(key)) for key in ("dev", "ino", "size", "mtime_ns")}


def _pin_name(generation: Mapping[str, int]) -> str:
    return (
        f".sg-base-pin.{generation['dev']}.{generation['ino']}."
        f"{generation['size']}.{generation['mtime_ns']}.sqlite"
    )


def _store_marker(partition_db_path: Path, payload: Mapping[str, Any]) -> None:
    conn = sqlite3.connect(str(partition_db_path), timeout=30.0)
    try:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                COMPOSED_VIEW_META_KEY,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# Wall-clock budget for re-snapshotting a canonical base that was republished
# between the generation stat and the pin link (NF-2026-02385). Not an attempt
# count: every retry must observe a NEW generation (progress), and only a base
# still moving after this long fails closed with the measured reason.
_BASE_PIN_RACE_BUDGET_SECONDS = 10.0


class _BasePinRaced(PartitionBasePinError):
    """The base moved between the generation snapshot and the pin; retryable.

    ``generation_name`` is the pin name of the snapshot that raced, so the
    retry loop can tell a publisher making progress from a race that repeats.
    """

    def __init__(self, reason: str, generation_name: str) -> None:
        super().__init__(reason)
        self.generation_name = generation_name


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, str(exc.errno))


def _link_base_pin(
    source: Path, pin_path: Path, generation: Mapping[str, int]
) -> str | None:
    """Hard-link ``source`` to ``pin_path``; ``None`` on success.

    Returns an errno name when the filesystem, platform or sandbox cannot hard
    link. ``EXDEV`` is returned so the caller can fall back to a verified byte
    copy; every other unsupported errno is recorded as unsupported rather than
    pretending to pin.

    ``link`` follows the canonical PATH, so the inode is first linked under a
    PRIVATE name no other caller touches and its identity checked there. If
    the base was republished after ``generation`` was taken, only that private
    link is removed and :class:`_BasePinRaced` asks the caller to re-snapshot.
    The verified inode is then published under the canonical pin name. The
    name encodes the generation, so an existing entry with the same identity
    IS the same inode -- a concurrent pinner's valid pin -- and is joined,
    never unlinked (NF-2026-02385). Only an entry squatting the name with a
    different identity is atomically replaced by the verified inode.
    """

    private = pin_path.with_name(
        f".sg-base-link.{os.getpid()}.{os.urandom(8).hex()}.pinlink"
    )
    try:
        try:
            os.link(source, private)
        except NotImplementedError:
            return "link_unsupported"
        except OSError as exc:
            if exc.errno in _BASE_PIN_UNSUPPORTED_ERRNOS:
                return _errno_name(exc)
            raise PartitionBasePinError(
                f"composed_base_pin_failed:{_errno_name(exc)}"
            ) from exc
        try:
            linked = os.stat(private, follow_symlinks=False)
        except OSError as exc:
            raise PartitionBasePinError(
                f"composed_base_pin_missing:{private.name}"
            ) from exc
        if not stat_mod.S_ISREG(linked.st_mode) or _generation(linked) != generation:
            raise _BasePinRaced(
                f"composed_base_pin_raced:{pin_path.name}", pin_path.name
            )
        try:
            os.link(private, pin_path)
        except FileExistsError:
            pass
        except OSError as exc:
            raise PartitionBasePinError(
                f"composed_base_pin_failed:{_errno_name(exc)}"
            ) from exc
        if _pin_matches(pin_path, generation):
            return None
        # Absent (a squatter removed concurrently) or squatted by another
        # identity: atomically install the verified inode under the name.
        try:
            os.replace(private, pin_path)
        except OSError as exc:
            raise PartitionBasePinError(
                f"composed_base_pin_failed:{_errno_name(exc)}"
            ) from exc
        if _pin_matches(pin_path, generation):
            return None
        raise _BasePinRaced(f"composed_base_pin_raced:{pin_path.name}", pin_path.name)
    finally:
        _discard_uncommitted_pin(private)


def _pin_matches(pin_path: Path, generation: Mapping[str, int]) -> bool:
    """True when ``pin_path`` is a regular file of exactly ``generation``.

    A directory squatting the name is refused as ``composed_base_pin_not_file``
    because it can be neither joined nor replaced.
    """

    try:
        observed = os.stat(pin_path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PartitionBasePinError(
            f"composed_base_pin_missing:{pin_path.name}"
        ) from exc
    if stat_mod.S_ISDIR(observed.st_mode):
        raise PartitionBasePinError(f"composed_base_pin_not_file:{pin_path.name}")
    return stat_mod.S_ISREG(observed.st_mode) and _generation(observed) == generation


def _write_pin_copy(source: Path, dest: Path) -> None:
    """Copy ``source`` bytes to a new file at ``dest``. Raises ``OSError``."""

    with source.open("rb") as src, dest.open("xb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())


def _discard_uncommitted_pin(path: Path | None) -> None:
    """Best-effort removal of a pin attempt that must not be left behind."""

    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


# Windows refuses to rename a file another handle holds without delete
# sharing: ERROR_ACCESS_DENIED (5) / ERROR_SHARING_VIOLATION (32). SQLite opens
# without delete sharing, so a concurrent reviewer's pin prune reading this
# partition's marker for a few milliseconds makes the publish fail transiently.
_PUBLISH_SHARING_WINERRORS = frozenset({5, 32})
_PARTITION_PUBLISH_BUDGET_SECONDS = 5.0


def publish_partition(temporary: Path, final: Path) -> None:
    """Atomically publish a built partition: ``os.replace(temporary, final)``.

    A Windows sharing violation from a concurrent marker reader is retried
    within ``_PARTITION_PUBLISH_BUDGET_SECONDS`` of wall clock (NF-2026-02385);
    every other error propagates unchanged, and a sharing violation outlasting
    the budget raises ``PermissionError`` whose message names the measured
    attempts and elapsed time.
    """

    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            os.replace(temporary, final)
            return
        except PermissionError as exc:
            elapsed = time.monotonic() - started
            if getattr(exc, "winerror", None) not in _PUBLISH_SHARING_WINERRORS:
                raise
            if elapsed >= _PARTITION_PUBLISH_BUDGET_SECONDS:
                raise PermissionError(
                    exc.errno,
                    f"partition_publish_sharing_violation:attempts={attempts}"
                    f":elapsed_ms={int(elapsed * 1000)}:{exc.strerror}",
                    str(final),
                ) from exc
        time.sleep(min(0.005 * attempts, 0.1))


def _copy_base_pin(source: Path, pin_dir: Path) -> tuple[Path, dict[str, int]]:
    """Byte-copy ``source`` to a pin named for the copy's own generation.

    EXDEV-only fallback. The returned generation is taken from the pin file
    after the copy, never from the source inode. The copy must be a regular
    file whose size matches the source size captured at copy time. A failed
    copy raises :class:`PartitionBasePinError` and leaves no pin file.
    """

    try:
        before = os.stat(source, follow_symlinks=False)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
        raise PartitionBasePinError(f"composed_base_pin_failed:{code}") from exc
    if stat_mod.S_ISLNK(before.st_mode):
        raise PartitionBasePinError(f"composed_base_symlink:{source}")
    if not stat_mod.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise PartitionBasePinError(f"composed_base_not_file:{source}")
    source_size = int(before.st_size)
    tmp_path = pin_dir / f".sg-base-copy.{os.getpid()}.{time.time_ns()}.tmp"
    final_path: Path | None = None
    replaced = False
    committed = False
    try:
        _write_pin_copy(source, tmp_path)
        observed = os.stat(tmp_path, follow_symlinks=False)
        if not stat_mod.S_ISREG(observed.st_mode):
            raise PartitionBasePinError(
                f"composed_base_pin_not_file:{tmp_path.name}"
            )
        if int(observed.st_size) != source_size:
            raise PartitionBasePinError("composed_base_pin_failed:size")
        try:
            after = os.stat(source, follow_symlinks=False)
        except OSError as exc:
            code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            raise PartitionBasePinError(
                f"composed_base_pin_failed:{code}"
            ) from exc
        if (
            not stat_mod.S_ISREG(after.st_mode)
            or int(after.st_size) != source_size
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            # The source moved during the copy: transient, so the caller
            # re-snapshots the generation it moved to (NF-2026-02385).
            raise _BasePinRaced(
                f"composed_base_pin_raced:{source.name}",
                _pin_name(_generation(before)),
            )
        generation = _generation(observed)
        final_path = pin_dir / _pin_name(generation)
        try:
            os.lstat(final_path)
        except FileNotFoundError:
            pass
        else:
            raise PartitionBasePinError(
                f"composed_base_pin_raced:{final_path.name}"
            )
        os.replace(tmp_path, final_path)
        replaced = True
        confirmed = os.stat(final_path, follow_symlinks=False)
        if (
            not stat_mod.S_ISREG(confirmed.st_mode)
            or _generation(confirmed) != generation
            or int(confirmed.st_size) != source_size
            or final_path.name != _pin_name(generation)
        ):
            raise PartitionBasePinError(
                f"composed_base_pin_raced:{final_path.name}"
            )
        committed = True
        return final_path, generation
    except PartitionBasePinError:
        raise
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
        raise PartitionBasePinError(f"composed_base_pin_failed:{code}") from exc
    finally:
        if not committed:
            if replaced:
                _discard_uncommitted_pin(final_path)
            else:
                _discard_uncommitted_pin(tmp_path)


def _verify_pinned_generation(pin_path: Path) -> None:
    """Re-verify the exact pinned inode as a complete Source Graph generation.

    The caller verified the canonical path just before this build and the pin
    is taken afterwards, so the inode it now holds is re-checked for the Source
    Graph schema and this runtime's recorded build revision before any review
    may compose with it -- a few metadata pages, never the index body.
    """

    try:
        conn = sqlite_readonly.connect_readonly(pin_path)
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            if not _BASE_PIN_REQUIRED_TABLES.issubset(tables):
                raise PartitionBasePinError(
                    f"composed_base_pin_schema_incomplete:{pin_path.name}"
                )
            row = conn.execute(
                "SELECT value FROM meta WHERE key='last_build'"
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise PartitionBasePinError(
            f"composed_base_pin_unreadable:{pin_path.name}"
        ) from exc
    if row is None:
        return
    try:
        recorded = json.loads(str(row[0]))
    except (json.JSONDecodeError, TypeError):
        recorded = None
    revision = recorded.get("build_revision") if isinstance(recorded, dict) else ""
    if revision != sg.BUILD_REVISION:
        raise PartitionBasePinError(
            f"composed_base_pin_wrong_revision:{str(revision)[:96]}"
        )


# SQLite primary result codes for a marker that exists but cannot be read
# right now (BUSY, LOCKED, IOERR, CANTOPEN, PROTOCOL): its pin references are
# UNKNOWN, never "none".
_MARKER_TRANSIENT_SQLITE_CODES = frozenset({5, 6, 10, 14, 15})
_MARKER_TRANSIENT_TEXT = ("locked", "busy", "unable to open", "disk i/o")


def _marker_pin_reference(db_path: Path) -> tuple[bool, str | None]:
    """``(known, pin_name)`` for one partition file in a pin directory.

    ``known`` is ``False`` when the marker could not be read for a transient
    reason, e.g. a concurrent build holds the partition's write lock: what it
    references is then unknown and no pin may be treated as unreferenced on
    its account (fail closed, NF-2026-02385). A file that is not a database or
    has no marker references nothing.
    """

    try:
        row = _select_marker_row(db_path)
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(code, int):
            transient = (code & 0xFF) in _MARKER_TRANSIENT_SQLITE_CODES
        else:
            text = str(exc).lower()
            transient = any(word in text for word in _MARKER_TRANSIENT_TEXT)
        return (not transient), None
    except (OSError, sqlite3.Error):
        return True, None
    marker = _parse_marker_row(row)
    base = (marker or {}).get("base")
    pin = base.get("pin") if isinstance(base, dict) else None
    if isinstance(pin, dict) and isinstance(pin.get("name"), str):
        return True, pin["name"]
    return True, None


def _scan_pin_references(
    pin_dir: Path, *, exclude: str = ""
) -> tuple[list[os.DirEntry[str]], set[str], bool]:
    """``(pins, referenced, complete)`` for ``pin_dir``.

    ``referenced`` holds the pin names named by the marker of a live
    ``*.sqlite`` partition or an in-flight ``*.tmp`` build (other than
    ``exclude``); ``complete`` is ``False`` when any such marker could not be
    read, or the directory could not be listed, so callers must not remove a
    pin on the strength of an incomplete scan.
    """

    try:
        entries = list(os.scandir(pin_dir))
    except OSError:
        return [], set(), False
    referenced: set[str] = set()
    pins: list[os.DirEntry[str]] = []
    complete = True
    for entry in entries:
        if _BASE_PIN_NAME.fullmatch(entry.name):
            pins.append(entry)
            continue
        if entry.name == exclude or not entry.name.endswith((".sqlite", ".tmp")):
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        known, name = _marker_pin_reference(Path(entry.path))
        if not known:
            complete = False
        elif name is not None:
            referenced.add(name)
    return pins, referenced, complete


def _prune_base_pins(pin_dir: Path, *, keep: str) -> int:
    """Remove pins in ``pin_dir`` that no partition there still references.

    Retention is bounded by the partitions themselves: a surviving pin is named
    by the marker of a live ``*.sqlite`` partition or an in-flight ``*.tmp``
    build in the same directory (markers are written BEFORE the link, so an
    in-flight build is never pruned), and the directory owner's cleanup removes
    both together. A marker that cannot be read right now -- a concurrent
    reviewer's build holding its lock -- makes the whole scan inconclusive and
    nothing is pruned this round. Best effort: a pin that cannot be removed now
    is retried on the next marker write. Returns the number of pins removed.
    """

    pins, referenced, complete = _scan_pin_references(pin_dir)
    if not complete:
        return 0
    referenced.add(keep)
    removed = 0
    for entry in pins:
        if entry.name in referenced:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                continue
            os.unlink(entry.path)
            removed += 1
        except OSError:
            continue
    return removed


def _discard_unshared_pin(pin_dir: Path, name: str, *, own: str) -> bool:
    """Remove pin ``name`` only when no other partition may reference it."""

    _pins, referenced, complete = _scan_pin_references(pin_dir, exclude=own)
    if not complete or name in referenced:
        return False
    _discard_uncommitted_pin(pin_dir / name)
    return True


def write_composed_marker(
    partition_db_path: Path,
    base_db_path: Path | None,
    scope: Sequence[str],
) -> dict[str, Any] | None:
    """Persist the base generation and full declared scope; pin that base.

    ``base_db_path`` of ``None`` records a standalone partition (no base to
    compose); a later read-only open then serves partition rows only and this
    returns ``None``. Otherwise the base generation is hard-linked beside the
    partition (constant cost, independent of base size) and re-verified. An
    ``EXDEV`` link falls back to a verified byte copy; the marker says
    ``pinned`` only after that copy passes the schema/revision check, and the
    recorded generation is the pin file's, not the source inode. Any other
    unsupported link errno is recorded with that reason. A symlinked, missing,
    empty or raced base fails closed.

    A canonical base republished between the generation snapshot and the link
    is a transient race, not a failure (NF-2026-02385): the base is
    re-snapshotted and the NEW generation pinned. Liveness is by progress --
    each retry must observe a generation the publisher actually moved to, and
    a race that repeats an already-raced generation fails at once -- bounded
    by ``_BASE_PIN_RACE_BUDGET_SECONDS`` of wall clock. Only a base still
    moving past that budget fails closed, and its reason keeps the measured
    attempts and elapsed time.
    """

    partition_db_path = Path(partition_db_path)
    scope_list = list(dict.fromkeys(str(item) for item in scope))
    if base_db_path is None:
        _store_marker(
            partition_db_path,
            {"schema_id": COMPOSED_VIEW_SCHEMA_ID, "base": None, "scope": scope_list},
        )
        return None
    raw_base = Path(base_db_path)
    if raw_base.is_symlink():
        raise PartitionBasePinError(f"composed_base_symlink:{raw_base}")
    resolved = raw_base.resolve()
    started = time.monotonic()
    raced: set[str] = set()
    attempts = 0
    while True:
        attempts += 1
        try:
            return _pin_base_generation(partition_db_path, resolved, scope_list)
        except _BasePinRaced as exc:
            elapsed = time.monotonic() - started
            measured = (
                f":attempts={attempts}:elapsed_ms={int(elapsed * 1000)}"
                f":budget_ms={int(_BASE_PIN_RACE_BUDGET_SECONDS * 1000)}"
            )
            if exc.generation_name in raced:
                raise PartitionBasePinError(
                    f"{exc}:no_progress{measured}"
                ) from exc
            if elapsed >= _BASE_PIN_RACE_BUDGET_SECONDS:
                raise PartitionBasePinError(f"{exc}{measured}") from exc
            raced.add(exc.generation_name)


def _pin_base_generation(
    partition_db_path: Path, resolved: Path, scope_list: list[str]
) -> dict[str, Any]:
    """One snapshot-and-pin attempt; raises :class:`_BasePinRaced` to retry."""

    try:
        observed = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise PartitionBaseShiftError(f"composed_base_missing:{resolved}") from exc
    if not stat_mod.S_ISREG(observed.st_mode) or observed.st_size <= 0:
        raise PartitionBasePinError(f"composed_base_not_file:{resolved}")
    generation = _generation(observed)
    name = _pin_name(generation)
    pin: dict[str, Any] = {
        "schema_id": BASE_PIN_SCHEMA_ID,
        "status": "pinned",
        "name": name,
    }
    base_info: dict[str, Any] = {"db_path": str(resolved), **generation, "pin": pin}
    payload = {
        "schema_id": COMPOSED_VIEW_SCHEMA_ID,
        "base": base_info,
        "scope": scope_list,
    }
    # Record the reference before linking so a concurrent prune in the same
    # directory always sees this pin as referenced.
    _store_marker(partition_db_path, payload)
    pin_dir = partition_db_path.resolve().parent
    pin_path = pin_dir / name
    unsupported = _link_base_pin(resolved, pin_path, generation)
    if unsupported is None:
        try:
            _verify_pinned_generation(pin_path)
        except PartitionBasePinError:
            # The pin name is shared by every partition in this directory
            # that pinned the same generation; a concurrent pinner may have
            # verified and recorded it already, so only an unshared pin is
            # removed here (an unknown reference keeps it for the next prune).
            _discard_unshared_pin(pin_dir, name, own=partition_db_path.name)
            raise
    elif unsupported == "EXDEV":
        # Cross-device only. EPERM, EACCES and the other unsupported link
        # errnos are not copyable: this fallback is the measured EXDEV case,
        # and a failed copy raises instead of being recorded as unsupported.
        copied_path, copied_generation = _copy_base_pin(resolved, pin_dir)
        name = copied_path.name
        # Keep the copy referenced during verification, but do not claim
        # pinned until the schema/revision check has passed.
        base_info["pin"] = {
            "schema_id": BASE_PIN_SCHEMA_ID,
            "status": "copy",
            "name": name,
        }
        _store_marker(partition_db_path, payload)
        try:
            _verify_pinned_generation(copied_path)
            confirmed = os.stat(copied_path, follow_symlinks=False)
        except PartitionBasePinError:
            copied_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            copied_path.unlink(missing_ok=True)
            code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            raise PartitionBasePinError(
                f"composed_base_pin_failed:{code}"
            ) from exc
        if (
            not stat_mod.S_ISREG(confirmed.st_mode)
            or _generation(confirmed) != copied_generation
            or name != _pin_name(copied_generation)
            or int(confirmed.st_size) != int(copied_generation["size"])
        ):
            copied_path.unlink(missing_ok=True)
            raise PartitionBasePinError(f"composed_base_pin_raced:{name}")
        pin = {
            "schema_id": BASE_PIN_SCHEMA_ID,
            "status": "pinned",
            "name": name,
            # Why this pin is a full byte copy rather than a hard link: the
            # errno ``_link_base_pin`` returned (NF-2026-01931). The policy is
            # unchanged; the receipt only names the cause.
            "link_fallback": {"mode": "copy", "reason": unsupported},
        }
        base_info.clear()
        base_info.update(
            {"db_path": str(resolved), **copied_generation, "pin": pin}
        )
        payload["base"] = base_info
        _store_marker(partition_db_path, payload)
    else:
        pin = {
            "schema_id": BASE_PIN_SCHEMA_ID,
            "status": "unsupported",
            "reason": unsupported,
        }
        base_info["pin"] = pin
        _store_marker(partition_db_path, payload)
    _prune_base_pins(pin_dir, keep=name)
    return pin


def _parse_marker_row(row: Any) -> dict[str, Any] | None:
    """Decode one ``meta`` marker row; ``None`` when absent or malformed."""

    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _select_marker_row(db_path: Path) -> Any:
    """Read the raw marker row read-only. Raises ``OSError``/``sqlite3.Error``."""

    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT value FROM meta WHERE key=?", (COMPOSED_VIEW_META_KEY,)
        ).fetchone()
    finally:
        conn.close()


def read_composed_marker(db_path: Path) -> dict[str, Any] | None:
    """Read the composed-view marker; ``None`` when absent or unreadable."""

    try:
        row = _select_marker_row(db_path)
    except (OSError, sqlite3.Error):
        return None
    return _parse_marker_row(row)


def _verify_base_unshifted(base: Mapping[str, Any]) -> Path:
    """Return the base path only if its fingerprint still matches; else raise."""

    base_path = Path(str(base.get("db_path") or ""))
    try:
        stat = base_path.stat()
    except OSError as exc:
        raise PartitionBaseShiftError(f"composed_base_missing:{base_path}") from exc
    if int(stat.st_size) != int(base.get("size", -1)) or int(
        stat.st_mtime_ns
    ) != int(base.get("mtime_ns", -1)):
        raise PartitionBaseShiftError(f"composed_base_shifted:{base_path}")
    return base_path


def _resolve_pinned_base(base: Mapping[str, Any], partition_db_path: Path) -> Path:
    """Return the base generation a marked partition must compose with.

    A legacy marker (no ``pin`` record) keeps the original fingerprint check
    against the canonical path. A pinned marker resolves ONLY to the pin file
    beside the partition, whose name must be the exact canonical form of the
    recorded generation and whose live identity (regular file, device, inode,
    size, mtime_ns) must still match -- a forged name, an escaping or symlinked
    pin, or a replaced/mutated pinned generation fails closed with a typed
    :class:`PartitionBasePinError`. An ``unsupported`` pin keeps the canonical
    fingerprint check but names why pinning was unavailable when it shifts.
    """

    pin = base.get("pin")
    if pin is None:
        return _verify_base_unshifted(base)
    if not isinstance(pin, dict) or pin.get("schema_id") != BASE_PIN_SCHEMA_ID:
        raise PartitionBasePinError("composed_base_pin_invalid:schema")
    status = pin.get("status")
    if status == "unsupported":
        try:
            return _verify_base_unshifted(base)
        except PartitionBaseShiftError as exc:
            reason = str(pin.get("reason") or "unknown")[:80]
            raise PartitionBasePinError(
                f"composed_base_shifted_unpinned:{reason}:{base.get('db_path')}"
            ) from exc
    if status != "pinned":
        raise PartitionBasePinError("composed_base_pin_invalid:status")
    name = str(pin.get("name") or "")
    try:
        expected = _pin_generation_from(base)
    except (TypeError, ValueError) as exc:
        raise PartitionBasePinError("composed_base_pin_invalid:generation") from exc
    if not _BASE_PIN_NAME.fullmatch(name) or name != _pin_name(expected):
        raise PartitionBasePinError(f"composed_base_pin_invalid:{name[:96]}")
    pin_path = Path(partition_db_path).resolve().parent / name
    try:
        observed = os.stat(pin_path, follow_symlinks=False)
    except OSError as exc:
        raise PartitionBasePinError(f"composed_base_pin_missing:{name}") from exc
    if not stat_mod.S_ISREG(observed.st_mode):
        raise PartitionBasePinError(f"composed_base_pin_not_file:{name}")
    if _generation(observed) != expected:
        raise PartitionBasePinError(f"composed_base_pin_shifted:{name}")
    return pin_path


# ---------------------------------------------------------------------------
# Composed schema installation over a live connection
# ---------------------------------------------------------------------------

def _install_composed_schema(
    conn: sqlite3.Connection,
    base_path: Path,
    partition_aliases: Sequence[str],
    scope: Sequence[str],
    *,
    attach: Mapping[str, Path] | None = None,
) -> None:
    """Attach the base read-only and install precedence ``TEMP`` views.

    ``partition_aliases`` names every attached schema that contributes
    partition rows, in precedence order (earlier wins). ``attach`` maps any
    alias that is not already an open schema to the partition file to attach
    for it. The connection may have ``query_only`` set; it is toggled off only
    long enough to build the ``TEMP`` scope table and views, then restored.
    """

    conn.execute("PRAGMA query_only=OFF")
    conn.execute(
        "ATTACH DATABASE ? AS base",
        (f"{Path(base_path).resolve().as_uri()}?mode=ro",),
    )
    for alias, path in (attach or {}).items():
        conn.execute(
            f"ATTACH DATABASE ? AS {alias}",
            (f"{Path(path).resolve().as_uri()}?mode=ro",),
        )
    conn.execute(
        f"CREATE TEMP TABLE {_SCOPE_TABLE}(file_path TEXT PRIMARY KEY)"
    )
    conn.executemany(
        f"INSERT OR IGNORE INTO {_SCOPE_TABLE}(file_path) VALUES(?)",
        [(str(item),) for item in dict.fromkeys(scope)],
    )
    for table in _COMPOSED_TABLES:
        parts = [f"SELECT * FROM {alias}.{table}" for alias in partition_aliases]
        parts.append(
            f"SELECT * FROM base.{table} "
            f"WHERE file_path NOT IN (SELECT file_path FROM temp.{_SCOPE_TABLE})"
        )
        conn.execute(
            f"CREATE TEMP VIEW {table} AS " + " UNION ALL ".join(parts)
        )
    conn.execute("PRAGMA query_only=ON")


def attach_composed_base_if_marked(
    conn: sqlite3.Connection, db_path: Path
) -> bool:
    """Compose ``conn`` (opened on a partition DB) with its recorded base.

    Called from :func:`source_graph.connect` for every read-only open. A DB
    without the composed marker (every ordinary Source Graph database) is left
    untouched and this returns ``False``. A marked DB attaches its PINNED base
    generation read-only (see :func:`_resolve_pinned_base`) and installs the
    precedence views, so an atomic canonical publication after the build never
    breaks the overlay nor leaks the newer generation into it; a missing,
    forged, replaced or shifted base fails closed with
    :class:`PartitionBaseShiftError` rather than silently serving partition
    rows alone.
    """

    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (COMPOSED_VIEW_META_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    try:
        marker = json.loads(str(row[0]))
    except (json.JSONDecodeError, TypeError):
        return False
    base = marker.get("base") if isinstance(marker, dict) else None
    if not isinstance(base, dict):
        return False
    base_path = _resolve_pinned_base(base, db_path)
    scope = [str(item) for item in (marker.get("scope") or [])]
    # The partition IS this connection's own ``main`` schema.
    _install_composed_schema(conn, base_path, ["main"], scope)
    return True


def is_composed(conn: sqlite3.Connection) -> bool:
    """True when a ``base`` schema is attached (a composed view is active)."""

    try:
        return any(
            str(row[1]) == "base" for row in conn.execute("PRAGMA database_list")
        )
    except sqlite3.Error:
        return False


def _partition_schemas(conn: sqlite3.Connection) -> list[str]:
    """Attached schemas that contribute partition rows, in precedence order."""

    schemas = [str(row[1]) for row in conn.execute("PRAGMA database_list")]
    return [name for name in schemas if name not in ("base", "temp")]


def _schema_has_fts(conn: sqlite3.Connection, schema: str) -> bool:
    try:
        return conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master "
            "WHERE type='table' AND name='entities_fts' LIMIT 1"
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def composed_find(
    conn: sqlite3.Connection, term: str, *, limit: int = 24
) -> list[dict[str, Any]]:
    """FTS ``find`` across a composed view with per-file precedence.

    ``find`` in ``source_graph`` joins ``entities_fts`` to ``entities`` by row
    id; the composed ``entities`` ``TEMP`` view unions two independent id
    spaces, so that join cannot be run against it. Instead each partition
    schema and the base run the same FTS expressions against their OWN
    ``entities_fts``/``entities`` pair, partition results are emitted first
    (partition wins), and base rows for any file in scope are excluded.
    """

    term = (term or "").strip()
    if not term:
        return []
    limit = max(1, min(int(limit), sg.MAX_BUDGET_ROWS))
    expressions = [sg._fts_phrase(term)]
    tokens = sg._query_tokens(term)
    if len(tokens) > 1:
        expressions.append(sg._fts_terms(term, operator="AND"))
        # Same OR gate as the canonical ``find``: a long identifier-like query
        # still broadens to any-token rather than missing on the overlay.
        if (
            not sg._looks_like_identifier_query(term)
            or len(tokens) > sg._IDENTIFIER_AND_TOKEN_LIMIT
        ):
            expressions.append(sg._fts_terms(term, operator="OR"))

    ordered = [(schema, False) for schema in _partition_schemas(conn)]
    ordered.append(("base", True))
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for schema, is_base in ordered:
        if not _schema_has_fts(conn, schema):
            continue
        base_filter = (
            f" AND e.file_path NOT IN (SELECT file_path FROM temp.{_SCOPE_TABLE})"
            if is_base
            else ""
        )
        rows: list[sqlite3.Row] = []
        for expression in expressions:
            try:
                # FTS5's ``MATCH`` must name the table with the bare
                # ``entities_fts`` token (a schema-qualified ``base.entities_fts
                # MATCH`` is rejected as an unknown column); it binds to the one
                # FTS table in this query's FROM clause, so per-schema queries
                # never cross-contaminate even with both indexes attached.
                rows = conn.execute(
                    f"SELECT e.file_path, e.kind, e.name, e.qualname, e.line_start, "
                    f"e.line_end, e.signature, e.evidence_label, e.confidence "
                    f"FROM {schema}.entities_fts "
                    f"JOIN {schema}.entities e "
                    f"ON e.id = {schema}.entities_fts.entity_id "
                    f"WHERE entities_fts MATCH ?{base_filter} "
                    "ORDER BY CASE WHEN lower(e.qualname)=lower(?) THEN 0 "
                    "WHEN lower(e.name)=lower(?) THEN 1 "
                    "WHEN lower(e.name) LIKE lower(?) THEN 2 ELSE 3 END, "
                    "e.file_path, e.line_start LIMIT ?",
                    (expression, term, term, f"{term}%", limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                break
        for row in rows:
            data = dict(row)
            key = (str(data.get("file_path")), str(data.get("qualname")))
            if key in seen:
                continue
            seen.add(key)
            out.append(data)
            if len(out) >= limit:
                return out
    if not out:
        # The canonical finder's substring pass, run against the composed
        # ``entities`` view (partition wins, scoped base rows hidden), so a
        # query that hits canonically never reads as zero on the overlay.
        out = [dict(row) for row in sg._find_like_rows(conn, term, limit)]
    return out


# ---------------------------------------------------------------------------
# Cross-boundary edge resolution
# ---------------------------------------------------------------------------

_RESOLVABLE_EDGE_KINDS: tuple[str, ...] = ("calls", "inherits")


def _composed_entity_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, list[dict[str, str]]], set[str]]:
    """Return ``(name -> [candidate,...], set(all composed qualnames))``.

    A candidate records the ``side`` (``partition`` or ``base``), ``file_path``
    and ``qualname`` of one composed definition, so a resolver can name which
    side of the boundary it resolved into.
    """

    by_name: dict[str, list[dict[str, str]]] = {}
    qualnames: set[str] = set()
    kinds = "('function','method','class','struct','union','enum','namespace')"
    for schema in _partition_schemas(conn):
        try:
            rows = conn.execute(
                f"SELECT file_path, name, qualname FROM {schema}.entities "
                f"WHERE kind IN {kinds}"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            by_name.setdefault(str(row[1]), []).append(
                {"side": "partition", "file_path": str(row[0]), "qualname": str(row[2])}
            )
            qualnames.add(str(row[2]))
    base_rows = conn.execute(
        f"SELECT file_path, name, qualname FROM base.entities "
        f"WHERE kind IN {kinds} "
        f"AND file_path NOT IN (SELECT file_path FROM temp.{_SCOPE_TABLE})"
    ).fetchall()
    for row in base_rows:
        by_name.setdefault(str(row[1]), []).append(
            {"side": "base", "file_path": str(row[0]), "qualname": str(row[2])}
        )
        qualnames.add(str(row[2]))
    return by_name, qualnames


def resolve_cross_boundary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Resolve ``calls``/``inherits`` edges that cross the partition/base line.

    RESOLVED cases (each proven by a test):

      * ``partition_to_base`` -- a call from a CHANGED file to an unchanged
        symbol whose only definition lives in the base.
      * ``base_to_partition`` -- a call from an UNCHANGED file whose recorded
        target no longer names any composed entity (its definition MOVED into
        the partition) and whose name has exactly one partition definition.
      * ``partition_to_partition`` -- a changed-file call whose target's sole
        definition is in another changed file.

    An edge is a resolution candidate only when its current ``dst_qualname`` is
    absent from the composed entity set (unresolved, or resolved to a
    definition the composition hid). Resolution is by unique name match.

    NAMED LIMITATIONS (returned in ``unresolved`` rather than guessed):

      * ``ambiguous`` -- the name has more than one composed definition; a
        lexical resolver cannot pick one without type/import evidence this
        function deliberately does not infer.
      * ``no_candidate`` -- the name matches no composed definition (an external
        symbol, a builtin, or dynamic dispatch).
    """

    by_name, composed_qualnames = _composed_entity_index(conn)
    placeholders = ",".join("?" for _ in _RESOLVABLE_EDGE_KINDS)
    resolved: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []

    def consider(side: str, edge: sqlite3.Row) -> None:
        dst_qualname = edge["dst_qualname"]
        if dst_qualname is not None and str(dst_qualname) in composed_qualnames:
            return  # already resolves to a live composed definition
        dst_name = str(edge["dst_name"])
        candidates = by_name.get(dst_name, [])
        record = {
            "src_qualname": str(edge["src_qualname"]),
            "dst_name": dst_name,
            "src_file": str(edge["file_path"]),
            "kind": str(edge["kind"]),
        }
        if len(candidates) == 1:
            target = candidates[0]
            resolved.append({
                **record,
                "dst_qualname": target["qualname"],
                "dst_file": target["file_path"],
                "direction": f"{side}_to_{target['side']}",
            })
        elif not candidates:
            unresolved.append({**record, "reason": "no_candidate"})
        else:
            unresolved.append({**record, "reason": "ambiguous"})

    for schema in _partition_schemas(conn):
        try:
            edges = conn.execute(
                f"SELECT file_path, kind, src_qualname, dst_name, dst_qualname "
                f"FROM {schema}.edges WHERE kind IN ({placeholders})",
                _RESOLVABLE_EDGE_KINDS,
            ).fetchall()
        except sqlite3.Error:
            edges = []
        for edge in edges:
            consider("partition", edge)
    base_edges = conn.execute(
        f"SELECT file_path, kind, src_qualname, dst_name, dst_qualname "
        f"FROM base.edges WHERE kind IN ({placeholders}) "
        f"AND file_path NOT IN (SELECT file_path FROM temp.{_SCOPE_TABLE})",
        _RESOLVABLE_EDGE_KINDS,
    ).fetchall()
    for edge in base_edges:
        consider("base", edge)

    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
    }


# ---------------------------------------------------------------------------
# ComposedView -- an immutable, identity-bound binding of base + partitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ComposedView:
    """An immutable binding of one base index plus one or more partitions.

    The view is a value: it holds paths and an identity, never an open handle,
    so the base cannot shift underneath it silently -- every :meth:`open`
    re-verifies the base fingerprint. Identity binding
    (``packet_sha256`` / ``target_request_id`` / ``target_task_id``) lets a
    reviewer refuse a view that does not match its own packet.
    """

    base_db_path: Path
    partition_db_paths: tuple[Path, ...]
    scope: frozenset[str]
    base_fingerprint: dict[str, Any] = field(default_factory=dict)
    packet_sha256: str = ""
    target_request_id: str = ""
    target_task_id: str = ""

    @classmethod
    def bind(
        cls,
        base_db_path: Path,
        partition_db_paths: Sequence[Path],
        *,
        packet_sha256: str = "",
        target_request_id: str = "",
        target_task_id: str = "",
    ) -> "ComposedView":
        """Bind a base and partitions, reading each partition's declared scope.

        Partition scopes must be DISJOINT: two partitions that both claim a
        file would make precedence ambiguous, so that is rejected rather than
        silently last-wins.
        """

        base = Path(base_db_path).resolve()
        partitions = tuple(Path(item).resolve() for item in partition_db_paths)
        scope: set[str] = set()
        for partition in partitions:
            marker = read_composed_marker(partition)
            declared = [
                str(item)
                for item in ((marker or {}).get("scope") or [])
            ]
            overlap = scope.intersection(declared)
            if overlap:
                raise PartitionError(
                    f"partition_scope_overlap:{','.join(sorted(overlap))}"
                )
            scope.update(declared)
        return cls(
            base_db_path=base,
            partition_db_paths=partitions,
            scope=frozenset(scope),
            base_fingerprint=_base_fingerprint(base),
            packet_sha256=packet_sha256,
            target_request_id=target_request_id,
            target_task_id=target_task_id,
        )

    def verify_binding(
        self,
        packet_sha256: str,
        target_request_id: str,
        target_task_id: str,
    ) -> None:
        """Raise :class:`PartitionBindingError` unless the identity matches.

        This is the reviewer's refusal: a view whose binding does not match the
        reviewer's own packet must never answer that reviewer's queries.
        """

        if (
            self.packet_sha256 != packet_sha256
            or self.target_request_id != target_request_id
            or self.target_task_id != target_task_id
        ):
            raise PartitionBindingError("composed_view_binding_mismatch")

    def assert_base_unshifted(self) -> None:
        """Raise :class:`PartitionBaseShiftError` if the base moved/changed."""

        _verify_base_unshifted(self.base_fingerprint)

    @contextmanager
    def open(self) -> Iterator[sqlite3.Connection]:
        """Yield a read-only connection that answers as one composed index.

        The base is opened read-only and can never be written through this
        handle; each partition is attached read-only; precedence ``TEMP`` views
        make unqualified ``files``/``entities``/``edges`` resolve with
        partition-wins semantics.
        """

        self.assert_base_unshifted()
        conn = sqlite3.connect("file::memory:", uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            aliases = [f"p{index}" for index in range(len(self.partition_db_paths))]
            attach = {
                alias: path
                for alias, path in zip(aliases, self.partition_db_paths)
            }
            _install_composed_schema(
                conn, self.base_db_path, aliases, self.scope, attach=attach
            )
            yield conn
        finally:
            conn.close()

    # -- Bounded query helpers over the composed connection ------------------

    def find(self, term: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self.open() as conn:
            return composed_find(conn, term, limit=limit)

    def context(self, file_path: str) -> dict[str, Any]:
        with self.open() as conn:
            return sg.context(conn, file_path)

    def func(self, name: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self.open() as conn:
            return sg.func(conn, name, limit=limit)

    def struct(self, name: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self.open() as conn:
            return sg.struct(conn, name, limit=limit)

    def resolve_cross_boundary(self) -> dict[str, Any]:
        with self.open() as conn:
            return resolve_cross_boundary(conn)


__all__ = [
    "BASE_PIN_SCHEMA_ID",
    "COMPOSED_VIEW_META_KEY",
    "COMPOSED_VIEW_SCHEMA_ID",
    "ComposedView",
    "PartitionBaseShiftError",
    "PartitionBasePinError",
    "PartitionBindingError",
    "PartitionBuildReport",
    "PartitionError",
    "attach_composed_base_if_marked",
    "build_partition",
    "composed_find",
    "is_composed",
    "read_composed_marker",
    "resolve_cross_boundary",
    "write_composed_marker",
]
