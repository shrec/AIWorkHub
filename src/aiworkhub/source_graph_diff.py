"""Source Graph diff core: read a worker candidate's change without a shell.

RM-2026-00071.  Managers and reviewers read a candidate through Source Graph
instead of ``git -C <worktree> diff``.  This module is the pure core only; the
MCP ``mode=diff`` wiring lives elsewhere.

Nothing here owns a rule of its own:

* the changed-path set of a registered worker worktree is exactly
  ``worker_workspace.changed_paths`` -- the rule accept/promotion uses, so a
  coordinator-seeded ``.gitignore`` rewrite, ignored caches and spill never
  appear;
* worktree registration and the recorded base OID come from the same
  process-free ``.git`` control-file verification the workspace uses;
* changed functions/classes come from the Source Graph extractor
  (``source_graph_ast.extract_file_from_bytes``), not a second parser;
* the directory walk skips ``source_graph.DEFAULT_EXCLUDE_DIR_NAMES``;
* per-file hashing is ``worker_workspace._hash_path`` fanned out with the
  shared ``parallelism`` width policy.

Every path is proven to lie inside the verified repository, and a request id
is proven to name a registered worktree, before any candidate byte is read.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import parallelism, source_graph, source_graph_ast, worker_workspace

SCHEMA_ID = "aiworkhub.source_graph_diff.v1"
DEFAULT_BYTE_BUDGET = 32 * 1024
MAX_FILE_BYTES = source_graph.SOURCE_GRAPH_AUTHENTICATED_FILE_BYTE_LIMIT
GIT_TIMEOUT_SECONDS = 60.0
_CURSOR_PREFIX = "sgd1"
_SYMBOL_KINDS = frozenset({"function", "method", "class", "async_function"})
_WORKTREES_RELATIVE = Path(".aiworkhub") / "runtime" / "worktrees"


class SourceGraphDiffError(ValueError):
    """A refused request; ``str(error)`` is a stable machine reason."""


# ---- authority checks (no candidate byte is read before these pass) --------


def _verified_repo(repo: Path) -> Path:
    try:
        root = Path(repo).resolve(strict=True)
    except OSError as exc:
        raise SourceGraphDiffError("repository_unavailable") from exc
    marker = root / ".git"
    if not root.is_dir() or not (marker.is_dir() or marker.is_file()):
        raise SourceGraphDiffError("repository_unverified")
    return root


def _inside_repo(root: Path, candidate: Path | str) -> Path:
    raw = Path(candidate)
    if not raw.is_absolute():
        raw = root / raw
    resolved = raw.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise SourceGraphDiffError("path_outside_repository")
    return resolved


def registered_worktree(repo: Path, request_id: str) -> tuple[Path, Path, str]:
    """Return ``(repo_root, worktree, admin_head_oid)`` or refuse.

    The worktree must sit at the canonical per-request location and be a
    linked worktree whose administrative record belongs to ``repo``.
    """
    root = _verified_repo(repo)
    if not isinstance(request_id, str) or not worker_workspace._REQUEST_ID_RE.fullmatch(
        request_id
    ):
        raise SourceGraphDiffError("invalid_request_id")
    path = _inside_repo(root, _WORKTREES_RELATIVE / request_id / "worktree")
    if not path.is_dir():
        raise SourceGraphDiffError(f"worktree_not_registered:{request_id}")
    try:
        head = worker_workspace._isolated_worktree_base_oid(root, path)
    except (OSError, worker_workspace.WorkspaceError) as exc:
        raise SourceGraphDiffError(f"worktree_not_registered:{request_id}") from exc
    return root, path, head


# ---- per-file fan-out ----------------------------------------------------------


def _fan_out(
    function: Callable[[tuple[Path, str]], Any], items: Sequence[tuple[Path, str]],
    max_workers: int | None,
) -> list[Any]:
    if max_workers is None:
        workers, _selection = parallelism.compute_worker_count(
            candidate_count=len(items), reserve=1
        )
    else:
        workers = max(1, int(max_workers))
    if workers <= 1 or len(items) < 2:
        return [function(item) for item in items]
    # hashlib and file reads release the GIL, so threads use real cores
    # without pickling paths into child processes.
    with parallelism.worker_pool_scope(), ThreadPoolExecutor(workers) as pool:
        return list(pool.map(function, items))


def _hash_one(item: tuple[Path, str]) -> str | None:
    root, relative = item
    return worker_workspace._hash_path(root / relative)


def _map_files(
    function: Callable[[tuple[Path, str]], Any],
    root: Path,
    relatives: Sequence[str],
    max_workers: int | None,
) -> dict[str, Any]:
    """The one pooled per-file map: sorted, de-duplicated, order-stable."""
    ordered = sorted(set(relatives))
    items = [(root, relative) for relative in ordered]
    return dict(zip(ordered, _fan_out(function, items, max_workers)))


def hash_files(
    root: Path, relatives: Sequence[str], *, max_workers: int | None = None
) -> dict[str, str | None]:
    """Hash ``relatives`` under ``root``; identical at any worker count."""
    return _map_files(_hash_one, root, relatives, max_workers)


# ---- byte sources ------------------------------------------------------------


class _Oversized:
    """Marks a side over ``MAX_FILE_BYTES``: reported and skipped, never read."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "OVERSIZED"


OVERSIZED = _Oversized()


class _Unreadable:
    """Marks a side whose read raised ``OSError``: reported and skipped."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNREADABLE"


UNREADABLE = _Unreadable()
Blob = bytes | _Oversized | _Unreadable | None


def _normalize(raw: Blob) -> Blob:
    """Fold CRLF to LF in text content, as git's ``text`` clean filter does.

    The old side comes from the object store (LF) while the new side is the
    checkout, which ``core.autocrlf``/``eol=crlf`` writes as CRLF.  Folding both
    sides alike keeps a one-line edit one hunk and a moved file a rename.
    """
    if not isinstance(raw, bytes) or b"\x00" in raw:
        return raw
    return raw.replace(b"\r\n", b"\n")


def _read_side(path: Path) -> Blob:
    # A locked, unreadable or vanished file (removed between the walk and
    # the read) is reported and skipped, never a raw OSError.
    try:
        if path.is_symlink():
            return ("symlink:" + os.readlink(path)).encode("utf-8", "surrogateescape")
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return OVERSIZED
        return path.read_bytes()
    except OSError:
        return UNREADABLE


def _read_one(item: tuple[Path, str]) -> Blob:
    root, relative = item
    return _read_side(root / relative)


def _git_output(worktree: Path, args: Sequence[str], stdin: bytes | None, reason: str) -> bytes:
    """Run git without a shell; any failure is the module's typed refusal."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=worktree,
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceGraphDiffError(reason) from exc
    if completed.returncode != 0:
        raise SourceGraphDiffError(reason)
    return completed.stdout


def _base_entries(
    worktree: Path, base_oid: str, relatives: Sequence[str]
) -> dict[str, tuple[bytes, bytes, str, int]]:
    """``path -> (mode, type, object id, size)`` for entries present in ``base_oid``.

    Existence is decided by the tree listing itself: a path is present exactly
    when ``ls-tree`` names it, so no git message is ever parsed.
    """
    entries: dict[str, tuple[bytes, bytes, str, int]] = {}
    for start in range(0, len(relatives), 256):
        out = _git_output(
            worktree,
            ["--literal-pathspecs", "ls-tree", "-z", "-l", "--full-tree", base_oid,
             "--", *relatives[start:start + 256]],
            None, "git_ls_tree_failed",
        )
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, tab, name = record.partition(b"\t")
            fields = meta.split()
            if not tab or len(fields) != 4:
                raise SourceGraphDiffError("git_ls_tree_output_invalid")
            mode, kind, oid, size = fields
            entries[name.decode("utf-8", "surrogateescape")] = (
                mode, kind, oid.decode("ascii"), int(size) if size.isdigit() else -1,
            )
    return entries


def _base_blobs(worktree: Path, base_oid: str, relatives: Sequence[str]) -> dict[str, Blob]:
    if not relatives:
        return {}
    # Sizes come with the listing, so an oversized base blob is never read.
    blobs: dict[str, Blob] = {}
    wanted: list[tuple[str, str, bool]] = []
    entries = _base_entries(worktree, base_oid, relatives)
    for relative in relatives:
        entry = entries.get(relative)
        if entry is None or entry[1] != b"blob":
            blobs[relative] = None
        elif entry[3] > MAX_FILE_BYTES:
            blobs[relative] = OVERSIZED
        else:
            wanted.append((relative, entry[2], entry[0] == b"120000"))
    if wanted:
        # Object ids, not ``rev:path`` specs, so no path text reaches cat-file.
        spec = "".join(f"{oid}\n" for _relative, oid, _link in wanted).encode("ascii")
        out = _git_output(worktree, ["cat-file", "--batch"], spec, "git_cat_file_failed")
        offset = 0
        try:
            for relative, _oid, link in wanted:
                newline = out.index(b"\n", offset)
                size = int(out[offset:newline].split()[2])
                offset = newline + 1
                data = out[offset:offset + size]
                # A symlink blob holds its target; describe it as the worktree side does.
                blobs[relative] = b"symlink:" + data if link else data
                offset += size + 1
        except (ValueError, IndexError) as exc:
            raise SourceGraphDiffError("git_cat_file_output_invalid") from exc
    return {relative: blobs[relative] for relative in relatives}


# ---- classification, hunks and symbols ---------------------------------------


def _digest(raw: Blob) -> str | None:
    return hashlib.sha256(raw).hexdigest() if isinstance(raw, bytes) else None


def _classify(
    old: Mapping[str, Blob], new: Mapping[str, Blob],
    raw_old: Mapping[str, Blob], raw_new: Mapping[str, Blob],
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for relative in sorted(set(old) | set(new)):
        before, after = old.get(relative), new.get(relative)
        if before is None and after is None:
            continue
        oversized = isinstance(before, _Oversized) or isinstance(after, _Oversized)
        unreadable = isinstance(before, _Unreadable) or isinstance(after, _Unreadable)
        if before == after and not (oversized or unreadable):
            if raw_old.get(relative) != raw_new.get(relative):
                # Only CRLF/LF differs: the file is promoted, so report it.
                rows[relative] = {
                    "status": "modified", "path": relative, "old_path": relative,
                    "line_endings_only": True,
                }
            continue
        status = "added" if before is None else "deleted" if after is None else "modified"
        rows[relative] = {"status": status, "path": relative, "old_path": relative}
        if unreadable:
            rows[relative]["unreadable"] = True
        elif oversized:
            rows[relative]["oversized"] = True
    # Renames match on the normalized bytes both sides are diffed with.
    added_by_digest: dict[str, list[str]] = {}
    for relative, row in rows.items():
        digest = _digest(new.get(relative)) if row["status"] == "added" else None
        if digest is not None:
            added_by_digest.setdefault(digest, []).append(relative)
    for relative in sorted(rows):
        row = rows[relative]
        digest = _digest(old.get(relative)) if row["status"] == "deleted" else None
        candidates = added_by_digest.get(digest) if digest is not None else None
        if not candidates:
            continue
        destination = candidates.pop(0)
        rows[destination] = {"status": "renamed", "path": destination, "old_path": relative}
        del rows[relative]
    return [rows[key] for key in sorted(rows)]


def _decode(raw: bytes | None) -> str | None:
    if raw is None:
        return ""
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", "replace")


_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+\Z")


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only, keeping terminators, as git and the AST count lines."""
    return _LINE_RE.findall(text)


def _file_hunks(row: Mapping[str, Any], before: bytes | None, after: bytes | None) -> list[dict[str, Any]]:
    old_text, new_text = _decode(before), _decode(after)
    hidden = before is not None and after is not None and before != after
    if old_text is None or new_text is None or (hidden and old_text == new_text):
        # Equal decoded text over different bytes means the change is only in
        # invalid UTF-8, which "replace" hides; report it as binary, not silence.
        return [{"path": row["path"], "header": "binary", "text": "Binary files differ\n"}]
    lines = list(difflib.unified_diff(
        _split_lines(old_text), _split_lines(new_text),
        fromfile=f"a/{row['old_path']}", tofile=f"b/{row['path']}", n=3,
    ))
    hunks: list[dict[str, Any]] = []
    for line in lines[2:]:
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        if line.startswith("@@"):
            hunks.append({"path": row["path"], "header": line.rstrip("\n"), "text": line})
        elif hunks:
            hunks[-1]["text"] += line
    return hunks


def _entities(
    root: Path, relative: str, raw: bytes | None
) -> tuple[dict[str, tuple[str, str, str]], str | None]:
    """Return ``(symbols, failure_reason)``.

    Only a real extraction error (parse/decode) is a failure. A language
    without a symbol extractor (``file_evidence_only``, unsupported) simply
    has no symbols, on both sides, and carries no marker.
    """
    if raw is None:
        return {}, None
    extraction = source_graph_ast.extract_file_from_bytes(
        root, root / relative, raw, build_revision=source_graph.BUILD_REVISION
    )
    if extraction.status in {"file_evidence_only", "unsupported_fail_closed"}:
        return {}, None
    if extraction.status != "ok":
        detail = str(getattr(extraction, "error", "") or "")[:160]
        return {}, f"{extraction.status}:{detail}" if detail else extraction.status
    # AST line numbers count only \n, \r\n and \r; str.splitlines would also
    # break on \f, \v, \x1c-\x1e, \x85, U+2028/9 and shift every later body.
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.removesuffix("\n") for line in _split_lines(text)]
    prefix = f"{relative}."
    found: dict[str, tuple[str, str, str]] = {}
    for entity in extraction.entities:
        if entity.kind not in _SYMBOL_KINDS or not entity.qualname.startswith(prefix):
            continue
        body = "\n".join(lines[entity.line_start - 1:entity.line_end])
        found[entity.qualname[len(prefix):]] = (entity.kind, entity.qualname, body)
    return found, None


def _changed_symbols(
    root_old: Path, root_new: Path, row: dict[str, Any],
    before: bytes | None, after: bytes | None,
) -> list[dict[str, Any]]:
    old, old_failure = _entities(root_old, row["old_path"], before)
    new, new_failure = _entities(root_new, row["path"], after)
    if old_failure or new_failure:
        # One side is unreadable: its symbols are unknown, so no delta is
        # claimed rather than reporting the other side as wholly added/deleted.
        side, reason = ("base", old_failure) if old_failure else ("candidate", new_failure)
        row["symbols_unavailable"] = {"side": side, "reason": reason}
        return []
    changes: list[dict[str, Any]] = []
    for symbol in sorted(set(old) | set(new)):
        if symbol in old and symbol in new and old[symbol][2] == new[symbol][2]:
            continue
        change = "added" if symbol not in old else "deleted" if symbol not in new else "modified"
        kind, qualname, _body = new.get(symbol) or old[symbol]
        changes.append({
            "path": row["path"], "symbol": symbol, "qualname": qualname,
            "kind": kind, "change": change,
        })
    return changes


# ---- byte-budgeted pagination -----------------------------------------------


def _fingerprint(files: Sequence[Mapping[str, Any]], hunks: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in files:
        # A non-UTF-8 POSIX file name arrives surrogate-escaped; encode it
        # as tolerantly as the hunk text below.
        digest.update(
            f"{row['status']}\0{row['old_path']}\0{row['path']}\0".encode(
                "utf-8", "surrogateescape"
            )
        )
    for hunk in hunks:
        digest.update(hunk["text"].encode("utf-8", "surrogateescape"))
    return digest.hexdigest()[:16]


def _ascii_digits(text: str) -> bool:
    # str.isdigit() alone admits '²' and other digits int() then rejects.
    return text.isascii() and text.isdigit()


def _parse_cursor(cursor: str | None, fingerprint: str) -> tuple[int, int]:
    if cursor is None or cursor == "":
        return 0, 0
    if not isinstance(cursor, str):
        raise SourceGraphDiffError("cursor_invalid")
    parts = cursor.split(":")
    if (
        len(parts) != 4 or parts[0] != _CURSOR_PREFIX
        or not _ascii_digits(parts[1]) or not _ascii_digits(parts[2])
    ):
        raise SourceGraphDiffError("cursor_invalid")
    if parts[3] != fingerprint:
        raise SourceGraphDiffError("cursor_stale")
    return int(parts[1]), int(parts[2])


def _paginate(
    hunks: Sequence[Mapping[str, Any]], byte_budget: int, cursor: str | None, fingerprint: str
) -> dict[str, Any]:
    if byte_budget < 1:
        raise SourceGraphDiffError("byte_budget_invalid")
    index, offset = _parse_cursor(cursor, fingerprint)
    if index > len(hunks):
        raise SourceGraphDiffError("cursor_invalid")
    remaining = byte_budget
    page: list[dict[str, Any]] = []
    while index < len(hunks) and remaining > 0:
        data = hunks[index]["text"].encode("utf-8", "surrogateescape")
        if offset > len(data):
            raise SourceGraphDiffError("cursor_invalid")
        end = min(len(data), offset + remaining)
        while end < len(data) and end > offset and (data[end] & 0xC0) == 0x80:
            end -= 1
        if end == offset:
            if page:
                break
            end = offset + 1
            while end < len(data) and (data[end] & 0xC0) == 0x80:
                end += 1
        page.append({
            "index": index, "path": hunks[index]["path"], "header": hunks[index]["header"],
            "offset": offset, "text": data[offset:end].decode("utf-8", "surrogateescape"),
            "complete": end == len(data),
        })
        remaining -= end - offset
        offset = end
        if offset >= len(data):
            index, offset = index + 1, 0
    truncated = index < len(hunks)
    next_cursor = f"{_CURSOR_PREFIX}:{index}:{offset}:{fingerprint}" if truncated else None
    return {
        "hunks": page,
        "hunk_total": len(hunks),
        "byte_budget": byte_budget,
        "truncated": truncated,
        "truncation_marker": (
            f"[source_graph_diff truncated at {byte_budget} bytes; resume with cursor]"
            if truncated else None
        ),
        "next_cursor": next_cursor,
    }


def _repo_path(prefix: str, relative: str) -> str:
    return f"{prefix}/{relative}" if prefix not in ("", ".") else relative


def _assemble(
    root_old: Path, root_new: Path, old: Mapping[str, Blob], new: Mapping[str, Blob],
    *, byte_budget: int, cursor: str | None, repo_prefixes: tuple[str, str],
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    raw_old, raw_new = old, new
    old = {relative: _normalize(raw) for relative, raw in raw_old.items()}
    new = {relative: _normalize(raw) for relative, raw in raw_new.items()}
    files = _classify(old, new, raw_old, raw_new)
    hunks: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in files:
        before, after = old.get(row["old_path"]), new.get(row["path"])
        if row.get("line_endings_only"):
            # Normalized text is identical: reported, but there is no hunk.
            continue
        if row.get("oversized") or row.get("unreadable"):
            # One oversized or unreadable file is reported and skipped; the
            # rest still diff.
            prefix, relative = (
                (repo_prefixes[0], row["old_path"])
                if after is None or isinstance(before, _Unreadable)
                else (repo_prefixes[1], row["path"])
            )
            skipped.append(
                {"path": _repo_path(prefix, relative), "reason": "unreadable"}
                if row.get("unreadable") else {
                    "path": _repo_path(prefix, relative),
                    "reason": "file_over_byte_limit", "byte_limit": MAX_FILE_BYTES,
                }
            )
            continue
        hunks.extend(_file_hunks(row, before, after))
        symbols.extend(_changed_symbols(root_old, root_new, row, before, after))
    fingerprint = _fingerprint(files, hunks)
    return {
        "schema_id": SCHEMA_ID,
        **extra,
        "files": files,
        "symbols": symbols,
        "skipped": skipped,
        "fingerprint": fingerprint,
        **_paginate(hunks, byte_budget, cursor, fingerprint),
    }


# ---- public entry points -------------------------------------------------------


def diff_registered_worktree(
    repo: Path,
    request_id: str,
    *,
    workspace_metadata: Mapping[str, Any] | None = None,
    base_oid: str | None = None,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
    cursor: str | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Diff a registered worker worktree against its recorded ``base_oid``.

    ``workspace_metadata`` is the coordinator's ``WorkerWorkspace`` record; its
    ``workspace_baseline`` is what lets ``changed_paths`` drop seeded noise.
    """
    root, path, head = registered_worktree(repo, request_id)
    if workspace_metadata is not None:
        workspace = worker_workspace.WorkerWorkspace.from_metadata(dict(workspace_metadata))
        if (
            workspace.request_id != request_id
            or workspace.repo != root
            or workspace.path != path
        ):
            raise SourceGraphDiffError("workspace_metadata_mismatch")
    else:
        workspace = worker_workspace.WorkerWorkspace(
            request_id=request_id, repo=root, path=path, home=path.parent / "home",
            allowed_writes=(), parent_baseline={}, workspace_baseline={},
        )
    recorded = base_oid or workspace.base_oid or head
    if base_oid and workspace.base_oid and base_oid != workspace.base_oid:
        raise SourceGraphDiffError("base_oid_mismatch")
    workspace = dataclasses.replace(workspace, base_oid=recorded)
    try:
        relatives = worker_workspace.changed_paths(workspace, git_timeout=GIT_TIMEOUT_SECONDS)
    except worker_workspace.WorkspaceError as exc:
        raise SourceGraphDiffError(f"changed_paths_failed:{exc}") from exc
    for relative in relatives:
        # The parent must stay inside; a symlink leaf is read, never followed.
        _inside_repo(path, Path(relative).parent)
    old = _base_blobs(path, recorded, relatives)
    new = _map_files(_read_one, path, relatives, max_workers)
    return _assemble(
        path, path, old, new, byte_budget=byte_budget, cursor=cursor,
        repo_prefixes=("", ""), extra={"request_id": request_id, "base_oid": recorded},
    )


def _walk(root: Path) -> list[str]:
    rows: list[str] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in source_graph.DEFAULT_EXCLUDE_DIR_NAMES)
        base = Path(current)
        rows.extend((base / name).relative_to(root).as_posix() for name in names)
    return rows


def diff_directories(
    repo: Path,
    old_dir: Path | str,
    new_dir: Path | str,
    *,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
    cursor: str | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Diff two directories that both lie inside the verified repository."""
    root = _verified_repo(repo)
    old_root, new_root = _inside_repo(root, old_dir), _inside_repo(root, new_dir)
    if not old_root.is_dir() or not new_root.is_dir():
        raise SourceGraphDiffError("directory_unavailable")
    old_paths, new_paths = _walk(old_root), _walk(new_root)
    old_hashes = hash_files(old_root, old_paths, max_workers=max_workers)
    new_hashes = hash_files(new_root, new_paths, max_workers=max_workers)
    changed = sorted(
        relative for relative in set(old_hashes) | set(new_hashes)
        if old_hashes.get(relative) != new_hashes.get(relative)
    )
    old = _map_files(_read_one, old_root, [r for r in changed if r in old_hashes], max_workers)
    new = _map_files(_read_one, new_root, [r for r in changed if r in new_hashes], max_workers)
    old_dir = old_root.relative_to(root).as_posix()
    new_dir = new_root.relative_to(root).as_posix()
    return _assemble(
        old_root, new_root, old, new, byte_budget=byte_budget, cursor=cursor,
        repo_prefixes=(old_dir, new_dir),
        extra={"old_dir": old_dir, "new_dir": new_dir},
    )
