#!/usr/bin/env python3
"""Run the canonical interpreter's pytest on exactly the given paths.

Replaces the ``python -m pytest ...`` invocation a manager types to check one
card's tests. 139 of 4,962 measured Bash calls ran tests by hand, and the
recurring mistake is the interpreter: ``python`` is not on PATH on this host,
and ``python3`` resolves outside the repository ``.venv``, so a hand-typed
command either fails to start or runs without the repository's dependencies.

**This is the one script in this directory that is not read-only.** It executes
repository code, which can write anywhere the test suite writes: caches,
temporary directories, and whatever a test itself creates. Its recipe declares
``CAPABILITY_WRITE`` for exactly that reason, so a caller must grant that
capability before the runner will execute it.

Interpreter selection stats the declared path and executes the declared path:
``.venv/bin/python`` (``.venv/Scripts/python.exe`` on Windows) is a symlink to
the system interpreter, and resolving it would discard the virtualenv and its
``site-packages`` entirely.

    python -m aiworkhub.recipes.repo_test_subset --paths P [P ...]
        [--timeout N] [--tail-chars N]

Always ``-q --tb=short``: short tracebacks keep every frame and the whole
assertion diff while dropping the repeated source listing, and no ``--maxfail``
is passed, because ending a run early would change WHAT is measured.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .. import platform_io
from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.repo_test_subset.v1"


def canonical_interpreter() -> Path:
    # ``platform_io.is_windows()`` rather than a local ``sys.platform`` test:
    # inside the package the platform question has one sanctioned owner, and
    # this module -- which already spawns pytest for up to 900 seconds -- has no
    # fast path to protect. As a script under ``scripts/`` it could not import
    # this facade; as a package module it can, so it does.
    relative = (
        Path(".venv") / "Scripts" / "python.exe"
        if platform_io.is_windows()
        else Path(".venv") / "bin" / "python"
    )
    candidate = common.REPO_ROOT / relative
    if candidate.is_file():
        return candidate
    return Path(sys.executable)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--paths", nargs="+", required=True, help="repository-relative test paths"
    )
    parser.add_argument(
        "--timeout", type=int, default=900, help="wall-clock seconds (default: 900)"
    )
    parser.add_argument(
        "--tail-chars",
        type=int,
        default=8000,
        help="captured output tail size (default: 8000)",
    )
    args = parser.parse_args(argv)

    timeout = max(10, min(int(args.timeout), 3600))
    tail_chars = max(0, min(int(args.tail_chars), 60_000))

    paths: list[str] = []
    missing: list[str] = []
    for raw in args.paths:
        candidate = Path(str(raw))
        if candidate.is_absolute() or ".." in candidate.parts:
            return common.fail(
                SCHEMA_ID, "unsafe_path", f"{raw!r} must be repository-relative"
            )
        if not (common.REPO_ROOT / candidate).exists():
            missing.append(str(raw))
        paths.append(candidate.as_posix())
    if missing:
        return common.fail(
            SCHEMA_ID, "path_not_found", f"no such path(s): {', '.join(missing)}"
        )

    interpreter = canonical_interpreter()
    argv_vector = [str(interpreter), "-m", "pytest", "-q", "--tb=short", *paths]
    started = time.monotonic()
    timed_out = False
    returncode: int | None = None
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv vector, shell=False
            argv_vector,
            cwd=str(common.REPO_ROOT),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    except OSError as exc:
        return common.fail(SCHEMA_ID, "spawn_failed", f"{type(exc).__name__}: {exc}")
    duration = time.monotonic() - started

    # Pytest's own counts line is the last non-empty line of ``-q`` output;
    # reporting it verbatim beats re-deriving the numbers from a regex that
    # would drift with pytest's formatting.
    summary_line = ""
    for line in reversed(stdout.strip().splitlines()):
        if line.strip():
            summary_line = line.strip()
            break

    stdout_tail, stdout_truncated = common.bounded_text(stdout, tail_chars)
    stderr_tail, stderr_truncated = common.bounded_text(stderr, tail_chars)
    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "interpreter": str(interpreter),
            "argv": argv_vector,
            "cwd": str(common.REPO_ROOT),
            "paths": paths,
            "returncode": returncode,
            "timed_out": timed_out,
            "timeout_seconds": timeout,
            "duration_seconds": round(duration, 3),
            "summary_line": summary_line,
            "stdout_tail": stdout_tail,
            "stdout_truncated": stdout_truncated,
            "stdout_chars": len(stdout),
            "stderr_tail": stderr_tail,
            "stderr_truncated": stderr_truncated,
            "stderr_chars": len(stderr),
        },
        ok=(returncode == 0 and not timed_out),
    )


if __name__ == "__main__":
    sys.exit(main())
