"""Sandbox launch extras for Linux workers: /dev/shm SemLock grant and CPU budget.

NF-2026-02239.  Inside the Linux landlock sandbox a worker's own
``multiprocessing`` build fell back to one process because POSIX semaphores
(``sem_open`` beneath ``/dev/shm``) were denied, while NF-2026-01740 had granted
the /dev/shm beneath-rule only to VALIDATION commands that need SemLock.  This
module owns the parent-side half of the fix so ``worker_workspace`` (at its line
ratchet ceiling) only forwards the result:

* :func:`landlock_semlock_dev_shm_flags` -- the landlock-exec flag pair.  A
  validation launch keeps the NF-2026-01740 behaviour byte for byte
  (``--semlock-dev-shm`` only when requested, refused without an exec
  scratch); a provider/worker launch now carries ``--worker-semlock-dev-shm``.
  Both are decided by the ONE existing gate,
  :func:`validation_runner.landlock_semlock_dev_shm_root` (a real, non-symlink
  directory), and the child grants both through the same
  ``_landlock_semlock_dev_shm_rule`` with the same sem_open-only rights.
* :func:`cpu_budget_env` -- ``AIWORKHUB_CPU_BUDGET`` for the worker and
  validation environments, derived from the observed core count with the
  repository's own pool headroom (:func:`platform_io.sandbox_slot_count`).

The Windows AppContainer path never reaches the landlock branch and receives no
budget variable, so its launch arguments and environment stay unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from . import validation_runner
from .platform_io import sandbox_slot_count

CPU_BUDGET_ENV = "AIWORKHUB_CPU_BUDGET"
VALIDATION_SEMLOCK_DEV_SHM_FLAG = "--semlock-dev-shm"
WORKER_SEMLOCK_DEV_SHM_FLAG = "--worker-semlock-dev-shm"
# Backends whose launch contract this module must leave untouched.
_UNCHANGED_BACKENDS = frozenset({"windows_appcontainer"})
# An override longer than this is not a core count on any real host; refusing
# it before ``int()`` keeps the parse bounded.
_MAX_OVERRIDE_DIGITS = 6


def landlock_semlock_dev_shm_flags(
    requested: bool, validation_exec_scratch: Path | None
) -> list[str]:
    """Return the /dev/shm SemLock landlock-exec flag pair, or ``[]``.

    A validation launch (one with an exec scratch) gains the rule only when
    ``run_validations`` requested it for a SemLock-needing command -- exactly the
    NF-2026-01740 contract.  Requesting it without an exec scratch still fails
    closed.  A provider/worker launch (no exec scratch, not requested) gains
    the worker flag.  Either way the root comes from the single existing gate,
    so the rule exists exactly where the SemLock preflight would grant it.
    """
    if validation_exec_scratch is None:
        if requested:
            from .worker_workspace import WorkspaceError

            raise WorkspaceError("landlock_semlock_dev_shm_requires_validation")
        flag = WORKER_SEMLOCK_DEV_SHM_FLAG
    elif requested:
        flag = VALIDATION_SEMLOCK_DEV_SHM_FLAG
    else:
        return []
    root = validation_runner.landlock_semlock_dev_shm_root()
    return [flag, root] if root is not None else []


def observed_cpu_count(cpu_count: int | None = None) -> int:
    """The observed core count (``os.cpu_count()`` unless supplied), at least 1."""
    observed = os.cpu_count() if cpu_count is None else cpu_count
    return max(1, int(observed or 1))


def derived_cpu_budget(cpu_count: int | None = None) -> int:
    """``max(1, observed cores - headroom)`` with the repository's pool headroom.

    The headroom is the one ``platform_io.sandbox_slot_count`` already applies
    to sandbox slots: one core stays with the interactive MCP server.
    """
    return sandbox_slot_count(observed_cpu_count(cpu_count))


def parse_cpu_budget_override(raw: str | None, cpu_count: int | None = None) -> int | None:
    """Return a valid operator override, or ``None`` when absent or invalid.

    Valid means plain ASCII decimal digits naming a whole number of cores from
    1 up to the observed core count; anything else (empty, signed, zero, a
    float, more cores than the host has) is not a budget and is ignored so the
    derived value applies instead.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text or len(text) > _MAX_OVERRIDE_DIGITS:
        return None
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    if value < 1 or value > observed_cpu_count(cpu_count):
        return None
    return value


def resolve_cpu_budget(
    environ: Mapping[str, str] | None = None, cpu_count: int | None = None
) -> int:
    """The operator's valid ``AIWORKHUB_CPU_BUDGET``, else the derived budget."""
    source = os.environ if environ is None else environ
    override = parse_cpu_budget_override(source.get(CPU_BUDGET_ENV), cpu_count)
    return override if override is not None else derived_cpu_budget(cpu_count)


def cpu_budget_env(
    backend: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    cpu_count: int | None = None,
) -> dict[str, str]:
    """``{AIWORKHUB_CPU_BUDGET: <n>}`` for a worker or validation environment.

    Empty for the Windows AppContainer backend, whose launch contract is
    deliberately unchanged.  ``environ`` defaults to the coordinator's own
    environment, which is where an operator override is set.
    """
    if backend in _UNCHANGED_BACKENDS:
        return {}
    return {CPU_BUDGET_ENV: str(resolve_cpu_budget(environ, cpu_count))}
