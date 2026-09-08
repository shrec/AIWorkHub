"""Shared plumbing for the packaged operator recipe modules.

Every module in this package replaces a python heredoc a model was otherwise
going to type from scratch. Measured over 27 manager transcripts: 2,404 of
4,962 Bash calls (48%) were ad-hoc Python, and only 164 (3%) reused a checked-in
script. The modules here are the reusable half of that, so the shared parts --
locating the repository, opening the task queue read-only, bounding output --
live once here instead of being re-derived in seven files.

**Inside the installed package, on purpose.** These began as
``scripts/recipes/*.py`` and their recipes named that path in argv, which made
every one of them unrunnable in any repository except AIWorkHub's own checkout:
a managed project has no ``scripts/recipes/`` directory, while the data these
modules read -- ``.aiworkhub/tasking/task_queue.sqlite``,
``.aiworkhub/runtime/process_logs/``,
``.aiworkhub/runtime/task_reconciler_status.json`` -- is per-project and would
have been exactly right. Shipping them in the package makes ``python -m
aiworkhub.recipes.<name>`` valid wherever AIWorkHub is installed, which is
every repository it manages. There is no second copy: the ``scripts/recipes/``
tree was removed rather than left to drift.

Contract every module in this package keeps:

* **Read-only**, except ``repo_test_subset``, which says so in its own
  docstring and in the capability its recipe declares.
* **Bounded JSON on stdout.** One object, always with ``schema_id`` and ``ok``.
  Rows are capped by an explicit ``--limit``; long text is tailed with a flag
  saying so. Nothing prints an unbounded dump.
* **Explicit arguments only.** ``argparse`` with named flags -- never a
  free-form query string, and never SQL from an argument.
* **Stdlib only**, so a module runs under any interpreter that can import this
  package, with no import of the rest of ``aiworkhub`` on the fast path.
* **Exit 0 when the question was answered** (including "answered: nothing
  matched"), **exit 2 when it could not be** (missing store, unknown id, bad
  argument). A caller branches on the exit code without parsing prose.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

def resolve_repo_root(start: Path | None = None) -> Path:
    """The repository these scripts answer about: the working directory's own.

    Derived from the CWD, never from ``__file__``, and that is the whole reason
    this package exists. The checked-in ``scripts/recipes/`` version these
    modules replace read ``Path(__file__).resolve().parents[2]``, which is
    correct for exactly one repository -- AIWorkHub's own checkout. Installed
    as a wheel the same expression resolves to ``site-packages``, and in a
    project AIWorkHub MANAGES it names a directory that has nothing to do with
    that project. The argv was equally unportable: ``python
    scripts/recipes/<name>.py`` is a path that simply does not exist in a
    managed repository, so the recipe was unrunnable there while the DATA it
    reads (``.aiworkhub/tasking/task_queue.sqlite``,
    ``.aiworkhub/runtime/``) is per-project and would have been correct.

    ``recipe_runner.run_recipe`` spawns every recipe with ``cwd`` set to the
    target repository root, so the CWD is the answer by construction. The
    ancestor walk is for a human running ``python -m aiworkhub.recipes.<name>``
    from a subdirectory: the nearest ancestor holding ``.aiworkhub/`` is the
    repository that owns the data. With no such ancestor the CWD stands, and
    each reader then reports its own store as absent rather than guessing at
    another repository's.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".aiworkhub").is_dir():
            return candidate
    return current


REPO_ROOT = resolve_repo_root()

TASK_QUEUE_DB = REPO_ROOT / ".aiworkhub" / "tasking" / "task_queue.sqlite"
RUNTIME_DIR = REPO_ROOT / ".aiworkhub" / "runtime"
PROCESS_LOGS_DIR = RUNTIME_DIR / "process_logs" / "processes"
ATTEMPT_ARTIFACTS_DIR = PROCESS_LOGS_DIR / "attempt-artifacts"
WORKTREES_DIR = RUNTIME_DIR / "worktrees"
RECONCILER_STATUS = RUNTIME_DIR / "task_reconciler_status.json"

# A last-resort ceiling on one script's stdout. Each script bounds its own rows
# first; this exists so a pathological row can never turn a bounded answer into
# an unbounded one.
MAX_OUTPUT_CHARS = 262_144

# The sentinel an optional filter uses when it is not applied. Recipe argv
# vectors render every declared slot, so an optional filter is spelled as a
# value rather than as an absent flag; "all" is that value everywhere here.
NO_FILTER = "all"


def emit(payload: dict[str, Any], *, ok: bool = True, code: int = 0) -> int:
    """Print one bounded JSON object and return the process exit code."""
    payload = {"ok": ok, **payload}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1)
    if len(text) > MAX_OUTPUT_CHARS:
        text = json.dumps(
            {
                "ok": False,
                "schema_id": payload.get("schema_id"),
                "error": "output_exceeded_bound",
                "output_chars": len(text),
                "max_output_chars": MAX_OUTPUT_CHARS,
                "detail": "lower --limit or --max-bytes and run again",
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=1,
        )
        code = 2
    sys.stdout.write(text + "\n")
    return code


def fail(schema_id: str, reason: str, detail: str = "") -> int:
    """Print one bounded refusal and return exit code 2."""
    return emit(
        {"schema_id": schema_id, "reason": reason, "detail": detail[:400]},
        ok=False,
        code=2,
    )


def task_queue_readonly() -> sqlite3.Connection:
    """Open the canonical task queue strictly read-only.

    ``mode=ro`` plus ``immutable=1``: the queue is a live WAL database that the
    MCP server writes continuously, and an immutable connection neither takes a
    lock nor replays the WAL, so a read here can never block or perturb a
    running launch. The cost is that rows committed since the last checkpoint
    may not be visible, which is the right trade for an audit read and the
    wrong one for anything that must be current -- no module here claims to be.

    The URI is built from :meth:`~pathlib.Path.as_uri`, not from an f-string,
    and that is load-bearing now that these modules run in repositories whose
    paths AIWorkHub does not choose. ``sqlite_readonly`` documents the trap:
    ``#`` begins a FRAGMENT in SQLite URI syntax, so ``f"file:{path}?mode=ro"``
    for any path containing ``#`` silently discards the whole query string --
    the database then opens READ-WRITE, create-if-missing, on a DIFFERENT FILE
    than the caller named. ``as_uri`` percent-encodes it to ``%23``.
    ``PRAGMA query_only=ON`` is the same second, independent guarantee that
    module applies.

    It does not simply call ``sqlite_readonly.connect_readonly``: that helper
    cannot express ``immutable=1``, and dropping it would make every read here
    take a lock and replay the WAL of a database a live launch is writing --
    which is precisely what these read-only modules must never do. This is the
    one ``sqlite3.connect`` in the package that the facade cannot own, and it
    is recorded as such in the ``os_dependency_boundary`` baseline.
    """
    if not TASK_QUEUE_DB.exists():
        raise FileNotFoundError(str(TASK_QUEUE_DB))
    uri = f"{TASK_QUEUE_DB.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_json(path: Path) -> Any:
    """Return parsed JSON from ``path``, or ``None`` when it is unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bounded_text(text: str, limit: int) -> tuple[str, bool]:
    """Return the last ``limit`` characters of ``text`` and whether it was cut."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[-limit:], True


def bounded_payload(raw: str | None, limit: int) -> Any:
    """Return a parsed event payload, replaced by a summary when oversized.

    An oversized payload is not silently trimmed into something that still
    looks like a complete object: it is replaced by its own keys and size, so a
    reader can see there is more and ask for that one row specifically.
    """
    if not raw:
        return None
    if len(raw) > limit:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"_oversized": True, "_chars": len(raw), "_parsed": False}
        keys = sorted(parsed) if isinstance(parsed, dict) else None
        return {"_oversized": True, "_chars": len(raw), "_keys": keys}
    try:
        return json.loads(raw)
    except ValueError:
        return {"_unparsable": True, "_chars": len(raw)}


def is_request_id(value: str) -> bool:
    """True for the 32-hex request id shape the launcher actually writes."""
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def pid_is_alive(pid: Any) -> bool | None:
    """Measure whether ``pid`` exists right now; ``None`` when unmeasurable.

    A status file saying "running" is not a running process. This is the only
    thing in these scripts that answers liveness, and it answers by asking the
    OS -- never by reading a state string.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        from aiworkhub._platform_process import process_is_alive

        return bool(process_is_alive(pid))
    except Exception:  # noqa: BLE001 - a script must work without the package
        pass
    try:
        import os

        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, AttributeError):
        return None
    return True
