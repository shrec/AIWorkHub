"""The execution surface for persisted tool recipes.

``tool_recipes`` describes and validates invocations and says of itself that it
"deliberately contains no execution engine ... it never runs anything";
``tool_recipes_store`` persists those descriptions. Nothing ever ran one, and
that had a measured consequence: the dashboard's Tool Recipes panel reports
``no_sample`` for invocation, cache and context whenever its payload carries no
``receipts`` key, and no code path could produce one. "No evidence" was not a
fact about this repository's activity -- it was a fact about the missing
module you are reading.

This module is that missing surface, and it is deliberately the ONLY one. It
adds execution without moving any policy:

* **Validation is not re-implemented.** :func:`tool_recipes.validate_invocation`
  gates platform and capabilities and renders the argv; this module calls it
  and refuses whatever it refuses, with the same stable ``reason``.
* **Capability policy is not re-implemented.** ``validate_invocation`` and
  ``discover`` apply one predicate -- every capability the manifest requires
  must be in the caller-granted set -- and this module reuses it rather than
  writing a second one. That is what makes the runner read-only by default: a
  recipe declaring :data:`~aiworkhub.tool_recipes.CAPABILITY_WRITE` or
  :data:`~aiworkhub.tool_recipes.CAPABILITY_NETWORK` simply does not validate
  unless the caller granted that capability.
* **The shell-safety posture is structural, and stays structural.** The argv is
  an execve vector built entirely from manifest literals and typed slots.
  Nothing here ever composes a command string, and :func:`subprocess.run` is
  called with a list and ``shell=False``; there is no code path that could pass
  a string. ``argv[0]`` is a manifest literal, so the program identity is fixed
  by the manifest and resolved here to an absolute path -- never chosen by a
  parameter.

What it returns is BOUNDED. A recipe's whole stdout can be megabytes (one
observed validation run wrote 713 KB), and a digest that inlined it would move
the cost from "the model wrote the script" to "the model read the output".
The full streams go to a request-private file under
``.aiworkhub/runtime/recipe_runs/`` and the caller gets tails capped at
:data:`MAX_TAIL_CHARS` with explicit truncation flags, plus the file's path,
byte count and SHA-256 so the full text is retrievable and verifiable without
being carried.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from . import tool_recipes as tr
from . import tool_recipes_store as store

SCHEMA_ID = "aiworkhub.recipe_runner.v1"

# Where the full, unbounded output of one run lands. Under ``runtime/`` beside
# ``process_logs`` and ``worktrees``, which is where every other per-request
# runtime artifact in this repository already lives.
RECIPE_RUNS_REL = (".aiworkhub", "runtime", "recipe_runs")

# The digest's per-stream cap. Both tails are the LAST characters of the
# stream, because a failure's diagnosis is at the end.
MAX_TAIL_CHARS = 4096

# Timeout policy. A manifest's ``resource_bounds.max_runtime_seconds`` is the
# declared bound and wins whenever it is positive; the default applies only
# when a manifest declares none. The ceiling exists so a manifest cannot
# declare a bound that makes the run effectively unbounded.
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 1800.0
MIN_TIMEOUT_SECONDS = 1.0

# Stable refusal reasons owned by this module. The validation/capability
# refusals reuse ``tool_recipes``' own reason codes verbatim rather than
# renaming them here, so a caller branches on one vocabulary.
REASON_EXECUTABLE_UNAVAILABLE = "executable_unavailable"
REASON_SPAWN_FAILED = "spawn_failed"
REASON_OUTPUT_UNWRITABLE = "output_unwritable"
REASON_NONDETERMINISTIC_ARGV = "nondeterministic_argv"
REASON_INVALID_REPOSITORY = "invalid_repository"

# The canonical toolchain affordance variable NAMES, copied (not imported)
# from ``worker_workspace.py:6608-6610``. ``worker_workspace`` is a ~7000-line
# module that imports most of the launcher; importing it to read three string
# constants would pull the entire worker stack into every recipe run. The
# names are the contract -- a worker already reads them from its environment --
# and this comment is what keeps the two copies findable together.
CANONICAL_PYTHON_ENV = "AIWORKHUB_CANONICAL_PYTHON"
CANONICAL_RUFF_ENV = "AIWORKHUB_CANONICAL_RUFF"
CANONICAL_MYPY_ENV = "AIWORKHUB_CANONICAL_MYPY"

# The environment a validation run is allowed to inherit, matching the shape
# ``worker_workspace`` builds for a sandboxed child (``worker_workspace.py:
# 5786-5824``): locale, an unbuffered non-bytecode-writing Python, Git's
# lock-free read mode, and a temp directory. Nothing else is inherited, so a
# recipe run never sees a provider token, an API key or a session secret.
_INHERITED_ENV_NAMES = ("LANG", "LC_ALL", "HOME", "USER", "LOGNAME", "TMPDIR")
_WINDOWS_ENV_NAMES = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERNAME",
)
_POSIX_PATH = "/usr/local/bin:/usr/bin:/bin"


class RecipeRunError(tr.RecipeErrorBase):
    """A fail-closed run failure carrying a stable ``reason`` string.

    A SIBLING of :class:`~aiworkhub.tool_recipes.RecipeError`, not a subclass.
    The ``(reason, message)`` construction is inherited from
    :class:`~aiworkhub.tool_recipes.RecipeErrorBase` so it exists once, but the
    two names stay independently catchable: ``manager_recipe_tools.run``
    catches the validation error first and this one second with a different
    reply shape, so a subclass relationship would route every run failure into
    the wrong branch.
    """


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_windows() -> bool:
    return os.name == "nt"


def _canonical_interpreter_relative() -> PurePosixPath:
    """The repository's declared interpreter path, per platform.

    The exact spelling ``worker_workspace`` verifies and advertises. Resolving
    the symlink would discard the virtualenv, so the DECLARED path is what runs
    -- ``.venv/bin/python`` points at the system interpreter here, and running
    the target instead of the path loses ``site-packages`` entirely.
    """
    return (
        PurePosixPath(".venv/Scripts/python.exe")
        if _is_windows()
        else PurePosixPath(".venv/bin/python")
    )


def _canonical_interpreter(repo_root: Path) -> Path | None:
    candidate = repo_root / Path(*_canonical_interpreter_relative().parts)
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    except OSError:
        return None
    return None


def resolve_executable(repo_root: Path, program: str) -> Path:
    """Resolve a manifest's literal ``argv[0]`` to an absolute program path.

    The program identity comes from the manifest and only from the manifest;
    this turns that fixed name into something ``subprocess`` can spawn without
    a shell and without a PATH search at exec time.

    ``python`` resolves to the repository's own declared interpreter when it is
    present, because a recipe that runs repository code must run it under the
    repository's dependencies -- ``python`` is not even on PATH on this host.
    Every other program resolves through the restricted PATH the run will use,
    and an unresolvable one is refused rather than handed to the OS to fail
    with a bare ENOENT.
    """
    if program == "python":
        interpreter = _canonical_interpreter(repo_root)
        if interpreter is not None:
            return interpreter
        return Path(sys.executable)
    search_path = os.environ.get("PATH", "") if _is_windows() else _POSIX_PATH
    found = shutil.which(program, path=search_path or None)
    if found is None:
        found = shutil.which(program)
    if found is None:
        raise RecipeRunError(
            REASON_EXECUTABLE_UNAVAILABLE,
            f"executable {program!r} is not available on this host",
        )
    return Path(found)


def build_environment(repo_root: Path) -> dict[str, str]:
    """Return the restricted environment a recipe run executes under.

    An allowlist, never a filtered copy of ``os.environ``: a filter has to
    enumerate what is dangerous and is wrong the moment a new secret variable
    appears, while an allowlist is wrong only in the direction of a missing
    affordance -- which shows up as a failed run, not as a leaked credential.
    """
    env: dict[str, str] = {
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    for name in _INHERITED_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", env["LANG"])
    if _is_windows():
        for name in _WINDOWS_ENV_NAMES:
            value = os.environ.get(name)
            if value:
                env[name] = value
        env.setdefault("PATH", str(Path(sys.executable).parent))
    else:
        env["PATH"] = _POSIX_PATH
    for name in ("TMP", "TEMP"):
        if "TMPDIR" in env:
            env.setdefault(name, env["TMPDIR"])
    interpreter = _canonical_interpreter(repo_root)
    if interpreter is not None:
        env[CANONICAL_PYTHON_ENV] = str(interpreter)
    venv_bin = repo_root / ".venv" / ("Scripts" if _is_windows() else "bin")
    suffix = ".exe" if _is_windows() else ""
    for tool, key in (("ruff", CANONICAL_RUFF_ENV), ("mypy", CANONICAL_MYPY_ENV)):
        candidate = venv_bin / f"{tool}{suffix}"
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                env[key] = str(candidate)
        except OSError:
            continue
    return env


def _timeout_for(recipe: tr.Recipe) -> tuple[float, str]:
    declared = recipe.resource_bounds.max_runtime_seconds
    if isinstance(declared, (int, float)) and not isinstance(declared, bool):
        value = float(declared)
        if value > 0:
            return (
                max(MIN_TIMEOUT_SECONDS, min(value, MAX_TIMEOUT_SECONDS)),
                "recipe_resource_bounds",
            )
    return DEFAULT_TIMEOUT_SECONDS, "runner_default"


def _tail(text: str) -> tuple[str, bool, int]:
    """Return the last ``MAX_TAIL_CHARS`` characters, plus whether it is a tail."""
    length = len(text)
    if length <= MAX_TAIL_CHARS:
        return text, False, length
    return text[-MAX_TAIL_CHARS:], True, length


def _runs_dir(repo_root: Path) -> Path:
    return repo_root.joinpath(*RECIPE_RUNS_REL)


def _write_output_file(
    repo_root: Path, run_id: str, payload: Mapping[str, Any]
) -> tuple[Path, str, int]:
    """Write one run's full output and return ``(path, sha256, byte_count)``.

    The digest is taken over the bytes actually written, so a caller can verify
    the file it later reads is the file this run produced.
    """
    directory = _runs_dir(repo_root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Request-private where the platform supports it. A sandbox that
        # refuses chmod is a known, measured condition in this repository and
        # is not a reason to fail a run that already succeeded.
        try:
            os.chmod(directory, 0o700)
        except (OSError, NotImplementedError):
            pass
        path = directory / f"{run_id}.output.json"
        data = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True).encode(
            "utf-8"
        )
        path.write_bytes(data)
        try:
            os.chmod(path, 0o600)
        except (OSError, NotImplementedError):
            pass
    except OSError as exc:
        raise RecipeRunError(
            REASON_OUTPUT_UNWRITABLE, f"cannot write recipe run output: {exc}"
        ) from exc
    return path, hashlib.sha256(data).hexdigest(), len(data)


def _decode(stream: bytes | str | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def run_recipe(
    repo_root: str | Path,
    recipe: tr.Recipe,
    params: Mapping[str, Any] | None = None,
    *,
    grant_capabilities: Iterable[str] = (),
    platform: str | None = None,
    head: str | None = None,
    run_id: str | None = None,
    persist_receipt: bool = True,
    actor: tr.ActorIdentity | None = None,
) -> dict[str, Any]:
    """Validate, execute and receipt one recipe invocation.

    ``actor`` is the verified route this run is attributable to, and it comes
    from the SERVER -- ``manager_recipe_tools.run`` builds it from the manager
    route ``core.manager_bootstrap`` verified, and a launched worker would
    build it from its own card-bound identity. It is not a parameter any MCP
    caller can reach: no recipe tool exposes it, and
    :func:`tool_recipes.build_receipt` refuses anything that is not an
    :class:`~aiworkhub.tool_recipes.ActorIdentity`. Omitted, the run is
    recorded as ``unattributed``, which is the honest reading of a run whose
    route could not be verified.

    Raises :class:`~aiworkhub.tool_recipes.RecipeError` (with its stable
    ``reason``) when the manifest refuses the invocation -- an unknown or
    out-of-range parameter, an unsupported platform, an ungranted capability --
    and :class:`RecipeRunError` when the run itself cannot be attempted.
    A non-zero exit is NOT an error: it is a measured result and comes back in
    the digest, because "the tool ran and said no" is exactly the evidence the
    caller asked for.
    """
    root = Path(repo_root)
    if not root.is_dir():
        raise RecipeRunError(
            REASON_INVALID_REPOSITORY, f"repository root {root} is not a directory"
        )
    root = root.resolve()
    if not isinstance(recipe, tr.Recipe):
        raise tr.RecipeError(tr.REASON_BAD_MANIFEST, "run_recipe requires a Recipe")
    supplied = dict(params or {})

    # (a)+(c) One gate, owned by tool_recipes: platform, capabilities and every
    # typed parameter. An ungranted ``write``/``network`` capability fails HERE
    # -- the runner has no second policy that could disagree with ``discover``.
    validated = tr.validate_invocation(
        recipe, supplied, platform=platform, capabilities=tuple(grant_capabilities)
    )

    # (b) The argv the manifest renders, cross-checked against the one-step
    # ``build_argv`` path. Two renderings of one manifest that disagree would
    # mean the vector executed is not the vector the receipt binds, so the
    # disagreement is fatal rather than resolved in favour of either.
    rendered = tuple(tr.build_argv(recipe, supplied))
    if rendered != validated.argv:
        raise RecipeRunError(
            REASON_NONDETERMINISTIC_ARGV,
            f"recipe {recipe.id} rendered two different argv vectors",
        )

    executable = resolve_executable(root, validated.argv[0])
    env = build_environment(root)
    timeout_seconds, timeout_source = _timeout_for(recipe)
    identifier = str(run_id or uuid.uuid4().hex)

    # (d) execve semantics: a list, never a string; ``shell=False`` is the
    # default and is passed explicitly so the posture is visible at the call.
    argv = [str(executable), *validated.argv[1:]]
    started_at = _utcnow_iso()
    started = time.monotonic()
    timed_out = False
    spawn_error = ""
    returncode: int | None = None
    stdout_text = ""
    stderr_text = ""
    try:
        completed = subprocess.run(  # noqa: S603 - execve vector, shell=False, fixed argv[0]
            argv,
            cwd=str(root),
            env=env,
            shell=False,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        returncode = int(completed.returncode)
        stdout_text = _decode(completed.stdout)
        stderr_text = _decode(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = _decode(exc.stdout)
        stderr_text = _decode(exc.stderr)
    except OSError as exc:
        spawn_error = f"{type(exc).__name__}: {exc}"
    duration = time.monotonic() - started
    finished_at = _utcnow_iso()

    if spawn_error:
        exit_status = tr.EXIT_STATUS_SPAWN_FAILED
    elif timed_out:
        exit_status = tr.EXIT_STATUS_TIMEOUT
    else:
        exit_status = tr.EXIT_STATUS_COMPLETED

    # (e) The full streams land on disk; only bounded tails travel back.
    stdout_tail, stdout_truncated, stdout_chars = _tail(stdout_text)
    stderr_tail, stderr_truncated, stderr_chars = _tail(stderr_text)
    output_path, output_sha256, output_bytes = _write_output_file(
        root,
        identifier,
        {
            "schema_id": SCHEMA_ID,
            "run_id": identifier,
            "recipe_id": recipe.id,
            "recipe_version": recipe.version,
            "recipe_digest": recipe.digest,
            "argv": list(validated.argv),
            "executed_argv": argv,
            "cwd": str(root),
            "environment_names": sorted(env),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration,
            "timeout_seconds": timeout_seconds,
            "exit_status": exit_status,
            "returncode": returncode,
            "spawn_error": spawn_error,
            "stdout": stdout_text,
            "stderr": stderr_text,
        },
    )

    digest: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "run_id": identifier,
        "recipe_id": recipe.id,
        "recipe_version": recipe.version,
        "recipe_digest": recipe.digest,
        "argv": list(validated.argv),
        "executable": str(executable),
        "cwd": str(root),
        "required_capabilities": list(recipe.capabilities),
        "granted_capabilities": list(validated.capabilities),
        "exit_status": exit_status,
        "returncode": returncode,
        "timed_out": timed_out,
        "spawn_error": spawn_error,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration, 6),
        "timeout_seconds": timeout_seconds,
        "timeout_source": timeout_source,
        "stdout_tail": stdout_tail,
        "stdout_truncated": stdout_truncated,
        "stdout_chars": stdout_chars,
        "stderr_tail": stderr_tail,
        "stderr_truncated": stderr_truncated,
        "stderr_chars": stderr_chars,
        "output_path": str(output_path),
        "output_sha256": output_sha256,
        "output_bytes": output_bytes,
    }

    # (f) What this run COST the caller, measured rather than estimated. The
    # usage surface reports it per recipe, because "22 recipes registered" says
    # nothing about whether using them is cheap or ruinous, and a recipe exists
    # to be cheaper than the ad-hoc heredoc it replaces.
    #
    # Measured here, before the three receipt-identity keys below are appended:
    # ``receipt_digest`` cannot be known until the receipt exists, and the
    # receipt binds this number, so measuring the complete dict would require
    # the digest of a receipt that binds the size of a dict containing that
    # digest. The excluded keys are two fixed-width fields and an error string
    # that is normally absent, and the field name says what it covers.
    returned_digest_bytes = len(
        json.dumps(digest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )

    # (g) The receipt tool_recipes already knows how to bind, now carrying what
    # was measured instead of the not-executed placeholders -- including WHO
    # ran it, which is the difference between "rows exist" and "this is used".
    receipt = tr.build_receipt(
        validated,
        repository=str(root),
        head=head,
        timing=tr.TimingPlaceholder(
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
        ),
        exit_state=tr.ExitPlaceholder(status=exit_status, exit_code=returncode),
        actor=actor,
        returned_digest_bytes=returned_digest_bytes,
    )
    receipt_persisted = False
    receipt_error = ""
    if persist_receipt:
        try:
            store.put_receipt(root, receipt)
            receipt_persisted = True
        except Exception as exc:  # noqa: BLE001 - a store failure must not lose the run
            receipt_error = f"{type(exc).__name__}: {exc}"[:240]

    digest["returned_digest_bytes"] = returned_digest_bytes
    digest["actor_kind"] = receipt.actor.kind
    digest["receipt_digest"] = receipt.digest
    digest["receipt_persisted"] = receipt_persisted
    if receipt_error:
        digest["receipt_error"] = receipt_error
    return digest


def run_registered_recipe(
    repo_root: str | Path,
    recipe_id: str,
    *,
    version: str | None = None,
    params: Mapping[str, Any] | None = None,
    grant_capabilities: Iterable[str] = (),
    platform: str | None = None,
    head: str | None = None,
    run_id: str | None = None,
    actor: tr.ActorIdentity | None = None,
) -> dict[str, Any]:
    """Resolve one PERSISTED manifest and run it.

    The manifest comes from the store, so what runs is the immutable, digest-
    verified row -- not a mapping the caller typed at call time. An unknown id
    or version raises :class:`~aiworkhub.tool_recipes.RecipeError` with the
    registry's own stable reason.
    """
    root = Path(repo_root)
    registry = store.load_registry(root)
    recipe = registry.get(recipe_id, version)
    return run_recipe(
        root,
        recipe,
        params,
        grant_capabilities=grant_capabilities,
        platform=platform,
        head=head,
        run_id=run_id,
        actor=actor,
    )


__all__ = [
    "CANONICAL_MYPY_ENV",
    "CANONICAL_PYTHON_ENV",
    "CANONICAL_RUFF_ENV",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TAIL_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "REASON_EXECUTABLE_UNAVAILABLE",
    "REASON_INVALID_REPOSITORY",
    "REASON_NONDETERMINISTIC_ARGV",
    "REASON_OUTPUT_UNWRITABLE",
    "REASON_SPAWN_FAILED",
    "RECIPE_RUNS_REL",
    "SCHEMA_ID",
    "RecipeRunError",
    "build_environment",
    "resolve_executable",
    "run_recipe",
    "run_registered_recipe",
]
