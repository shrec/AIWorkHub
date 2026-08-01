"""Bounded, repository-isolated build scratch pools.

This is the repository-neutral successor to UltrafastSecp256k1's proven build
hygiene allocator. Build products live outside the source tree, admissions are
quota/reservation bounded, live leases are never auto-pruned, and cleanup is a
digest-bound explicit operation. Rogue in-repository build trees are reported
but never deleted by this module.
"""

from __future__ import annotations

import contextlib
import argparse
import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from . import repository_state

try:  # pragma: no cover - platform branch
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # pragma: no cover - platform branch
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


SCHEMA_ID = "aiworkhub.workspace_hygiene.v1"
DEFAULT_QUOTA_BYTES = 50 * 1024**3
DEFAULT_MAX_SLOTS = 8
DEFAULT_TTL_SECONDS = 3 * 24 * 3600
ROGUE_BUILD_PATTERNS = (
    "build", "build-*", "cmake-build-*", "out", "dist", "target",
)


class WorkspaceHygieneError(RuntimeError):
    pass


def default_scratch_root() -> Path:
    configured = os.environ.get("AIWORKHUB_BUILD_SCRATCH_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(base) / "AIWorkHub" / "build-scratch"
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "aiworkhub" / "build-scratch"


def _repository_id(repo_root: Path) -> str:
    try:
        return repository_state.inspect_repository(repo_root).manifest.repo_id
    except repository_state.RepositoryStateError as exc:
        raise WorkspaceHygieneError(f"repository_storage_not_ready:{exc}") from exc


def repository_pool(repo_root: Path | str, *, scratch_root: Path | None = None) -> Path:
    root = Path(repo_root).resolve()
    base = (scratch_root or default_scratch_root()).resolve()
    if base == root or root in base.parents:
        raise WorkspaceHygieneError("scratch_root_must_be_outside_repository")
    return base / _repository_id(root)


def directory_size_bytes(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if not (Path(dirpath) / name).is_symlink()
        ]
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                info = candidate.lstat()
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode):
                total += int(info.st_size)
    return total


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@contextlib.contextmanager
def _pool_lock(pool: Path) -> Iterator[None]:
    pool.mkdir(parents=True, exist_ok=True)
    lock_path = pool / ".pool.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def compute_fingerprint(repo_root: Path | str, *, toolchain: str = "") -> dict[str, str]:
    root = Path(repo_root).resolve()
    manifest = root / repository_state.PROJECT_MANIFEST_REL
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    value = {
        "repo_id": _repository_id(root),
        "manifest_hash": manifest_hash,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "toolchain": str(toolchain or "")[:500],
    }
    value["combined_hash"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return value


def _slot_rows(pool: Path, *, refresh_sizes: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    slots = pool / "slots"
    if not slots.is_dir():
        return rows
    for child in sorted(slots.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        meta = _read_json(child / "meta.json")
        if not meta or meta.get("schema_id") != SCHEMA_ID:
            rows.append({"slot_id": child.name, "path": str(child), "malformed": True})
            continue
        actual = (
            directory_size_bytes(child / "workdir")
            if refresh_sizes
            else int(meta.get("size_bytes") or 0)
        )
        row = dict(meta)
        row["slot_dir"] = str(child)
        row["path"] = str(child / "workdir")
        row["actual_bytes"] = actual
        row["effective_bytes"] = max(actual, int(meta.get("reserved_bytes") or 0))
        row["leased"] = bool(meta.get("lease_hash"))
        rows.append(row)
    return rows


def inventory(
    repo_root: Path | str,
    *,
    scratch_root: Path | None = None,
    refresh_sizes: bool = True,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    pool = repository_pool(root, scratch_root=scratch_root)
    rows = _slot_rows(pool, refresh_sizes=refresh_sizes)
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "repo_id": _repository_id(root),
        "scratch_root": str(pool.parent),
        "pool": str(pool),
        "sizes_refreshed": bool(refresh_sizes),
        "slot_count": len(rows),
        "total_bytes": sum(int(row.get("actual_bytes") or 0) for row in rows),
        "effective_bytes": sum(int(row.get("effective_bytes") or 0) for row in rows),
        "slots": rows,
    }


def allocate(
    repo_root: Path | str,
    *,
    owner: str,
    task_id: str,
    fingerprint: dict[str, str],
    reserved_bytes: int = 0,
    quota_bytes: int = DEFAULT_QUOTA_BYTES,
    max_slots: int = DEFAULT_MAX_SLOTS,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    scratch_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    pool = repository_pool(root, scratch_root=scratch_root)
    reservation = max(0, int(reserved_bytes))
    if quota_bytes < 0 or max_slots < 1 or reservation > quota_bytes:
        raise WorkspaceHygieneError("invalid_or_exhausted_workspace_quota")
    with _pool_lock(pool):
        rows = _slot_rows(pool)
        reusable = next((
            row for row in rows
            if not row.get("leased")
            and row.get("fingerprint", {}).get("combined_hash") == fingerprint.get("combined_hash")
        ), None)
        current = sum(int(row.get("effective_bytes") or 0) for row in rows)
        if reusable is None and (len(rows) + 1 > max_slots or current + reservation > quota_bytes):
            raise WorkspaceHygieneError("workspace_quota_exhausted_no_live_slot_was_touched")
        slot_dir = (
            Path(str(reusable["slot_dir"])) if reusable
            else pool / "slots" / f"{fingerprint['combined_hash'][:12]}-{secrets.token_hex(3)}"
        )
        workdir = slot_dir / "workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        now = time.time()
        meta = dict(reusable or {})
        for private in ("slot_dir", "path", "actual_bytes", "effective_bytes", "leased", "malformed"):
            meta.pop(private, None)
        meta.update({
            "schema_id": SCHEMA_ID,
            "slot_id": slot_dir.name,
            "repo_id": _repository_id(root),
            "owner": str(owner)[:200],
            "task_id": str(task_id)[:300],
            "fingerprint": fingerprint,
            "created_at": float(meta.get("created_at") or now),
            "last_used_at": now,
            "ttl_seconds": max(1, int(ttl_seconds)),
            "reserved_bytes": reservation,
            "lease_hash": hashlib.sha256(token.encode()).hexdigest(),
        })
        _atomic_json(slot_dir / "meta.json", meta)
    return {
        "ok": True, "slot_id": slot_dir.name, "path": str(workdir),
        "reused": reusable is not None, "lease_token": token,
    }


def release(
    repo_root: Path | str,
    slot_id: str,
    lease_token: str,
    *,
    scratch_root: Path | None = None,
) -> dict[str, Any]:
    pool = repository_pool(Path(repo_root).resolve(), scratch_root=scratch_root)
    with _pool_lock(pool):
        slot_dir = pool / "slots" / str(slot_id)
        if slot_dir.parent.resolve() != (pool / "slots").resolve() or slot_dir.is_symlink():
            raise WorkspaceHygieneError("workspace_slot_invalid")
        meta = _read_json(slot_dir / "meta.json")
        if not meta:
            raise WorkspaceHygieneError("workspace_slot_not_found")
        expected = str(meta.get("lease_hash") or "")
        observed = hashlib.sha256(str(lease_token).encode()).hexdigest()
        if not expected or not hmac.compare_digest(expected, observed):
            raise WorkspaceHygieneError("workspace_lease_mismatch")
        size = directory_size_bytes(slot_dir / "workdir")
        meta.update({
            "last_used_at": time.time(), "size_bytes": size,
            "reserved_bytes": 0, "lease_hash": "",
        })
        _atomic_json(slot_dir / "meta.json", meta)
    return {"ok": True, "slot_id": slot_id, "released": True, "size_bytes": size}


def _rogue_build_dirs(repo_root: Path) -> list[dict[str, Any]]:
    seen: set[Path] = set()
    rows: list[dict[str, Any]] = []
    for pattern in ROGUE_BUILD_PATTERNS:
        for candidate in repo_root.glob(pattern):
            if candidate in seen or candidate.is_symlink() or not candidate.is_dir():
                continue
            seen.add(candidate)
            rows.append({
                "path": candidate.relative_to(repo_root).as_posix(),
                "size_bytes": directory_size_bytes(candidate),
                "action": "report_only",
            })
    return sorted(rows, key=lambda row: str(row["path"]))


def cleanup_preview(
    repo_root: Path | str,
    *,
    scratch_root: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    pool = repository_pool(root, scratch_root=scratch_root)
    timestamp = time.time() if now is None else float(now)
    candidates = []
    protected = []
    for row in _slot_rows(pool):
        age = timestamp - float(row.get("last_used_at") or row.get("created_at") or timestamp)
        public = {
            "slot_id": str(row.get("slot_id") or ""),
            "size_bytes": int(row.get("actual_bytes") or 0),
            "age_seconds": max(0, int(age)),
        }
        if not row.get("leased") and age > int(row.get("ttl_seconds") or DEFAULT_TTL_SECONDS):
            candidates.append(public)
        else:
            public["reason"] = "live_lease" if row.get("leased") else "within_ttl"
            protected.append(public)
    basis = {
        "schema_id": SCHEMA_ID,
        "repo_id": _repository_id(root),
        "candidates": candidates,
    }
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "ok": True, "schema_id": SCHEMA_ID, "dry_run": True,
        "preview_digest": digest, "candidates": candidates,
        "candidate_bytes": sum(row["size_bytes"] for row in candidates),
        "protected": protected, "rogue_build_dirs": _rogue_build_dirs(root),
    }


def apply_cleanup(
    repo_root: Path | str,
    *,
    preview_digest: str,
    confirm: bool,
    scratch_root: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise WorkspaceHygieneError("workspace_cleanup_requires_explicit_confirmation")
    if os.environ.get("AIWORKHUB_ALLOW_WRITES", "0") != "1":
        raise WorkspaceHygieneError("workspace_cleanup_write_gate_closed")
    root = Path(repo_root).resolve()
    pool = repository_pool(root, scratch_root=scratch_root)
    with _pool_lock(pool):
        preview = cleanup_preview(root, scratch_root=scratch_root, now=now)
        if not hmac.compare_digest(str(preview_digest), str(preview["preview_digest"])):
            raise WorkspaceHygieneError("workspace_cleanup_preview_stale")
        removed = []
        for row in preview["candidates"]:
            slot = pool / "slots" / row["slot_id"]
            if slot.is_symlink() or slot.parent.resolve() != (pool / "slots").resolve():
                raise WorkspaceHygieneError("workspace_cleanup_slot_escape")
            shutil.rmtree(slot)
            removed.append(row)
    return {
        "ok": True, "schema_id": SCHEMA_ID, "applied": True,
        "removed": removed, "removed_bytes": sum(row["size_bytes"] for row in removed),
        "rogue_build_dirs_deleted": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiworkhub-build-hygiene")
    parser.add_argument("--repo", default=os.environ.get("AIWORKHUB_REPO_ROOT") or ".")
    parser.add_argument("--scratch-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory")
    fingerprint = commands.add_parser("fingerprint")
    fingerprint.add_argument("--toolchain", default="")
    allocate_cmd = commands.add_parser("allocate")
    allocate_cmd.add_argument("--owner", required=True)
    allocate_cmd.add_argument("--task-id", required=True)
    allocate_cmd.add_argument("--toolchain", default="")
    allocate_cmd.add_argument("--reserved-bytes", type=int, default=0)
    allocate_cmd.add_argument("--quota-bytes", type=int, default=DEFAULT_QUOTA_BYTES)
    allocate_cmd.add_argument("--max-slots", type=int, default=DEFAULT_MAX_SLOTS)
    release_cmd = commands.add_parser("release")
    release_cmd.add_argument("slot_id")
    release_cmd.add_argument("--lease-token", required=True)
    commands.add_parser("cleanup-preview")
    apply_cmd = commands.add_parser("cleanup-apply")
    apply_cmd.add_argument("--preview-digest", required=True)
    apply_cmd.add_argument("--confirm", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.command == "inventory":
            result = inventory(repo, scratch_root=args.scratch_root)
        elif args.command == "fingerprint":
            result = compute_fingerprint(repo, toolchain=args.toolchain)
        elif args.command == "allocate":
            result = allocate(
                repo,
                owner=args.owner,
                task_id=args.task_id,
                fingerprint=compute_fingerprint(repo, toolchain=args.toolchain),
                reserved_bytes=args.reserved_bytes,
                quota_bytes=args.quota_bytes,
                max_slots=args.max_slots,
                scratch_root=args.scratch_root,
            )
        elif args.command == "release":
            result = release(
                repo, args.slot_id, args.lease_token, scratch_root=args.scratch_root,
            )
        elif args.command == "cleanup-preview":
            result = cleanup_preview(repo, scratch_root=args.scratch_root)
        else:
            result = apply_cleanup(
                repo,
                preview_digest=args.preview_digest,
                confirm=args.confirm,
                scratch_root=args.scratch_root,
            )
    except WorkspaceHygieneError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
