"""Canonical, repository-bound task-authority reads (B863).

``core.show_task`` resolves its own repository ambiently, via
``core.repo_root()`` (the ``AIWORKHUB_REPO`` env var, or a ``DEFAULT_REPO``
fallback). A caller that already knows exactly which repository's isolated
workspace it launched a worker against -- e.g. ``ProcessManager.repo``, or an
explicit ``--repo`` passed to the reconciler daemon -- has no way to make
``core.show_task`` honor that binding: it always re-resolves the repo on its
own, independently, at call time. When the ambient resolution and the
caller's already-known repo diverge (multiple repositories handled by one
process, a reconciler invoked with an explicit ``--repo``, or a nested
independent repository misresolved to its outer checkout), the launcher and
the finalizer end up reading two different ``.aiworkhub/tasking/task_queue.sqlite``
files for the same claim/finalization decision -- the exact disagreement that
produces a false ``claim_ownership_lost``.

Every read here takes ``repo`` explicitly and never falls back to ambient
env/cwd state, so a caller bound to one repository can never have its
claim/finalization authority silently answered by a different repository's
queue. This is also, by construction, immune to "legacy JSONL/card_json
claim field" override: the only data source is ``task_store.get_task``,
whose canonical SQLite row always wins over any stale ``card_json`` copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import task_store


def show_task(repo: Path, task_id: str) -> dict[str, Any]:
    """Same wire contract as ``core.show_task`` (a ``TaskCtlResult.as_dict()``
    envelope whose ``stdout`` is the canonical card JSON), but bound to an
    explicit ``repo`` instead of an ambiently re-resolved one."""
    command = ["show", task_id]
    try:
        card = task_store.get_task(repo, task_id)
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    if card is None:
        return {
            "ok": True,
            "returncode": 0,
            "command": command,
            "stdout": f"Task not found: {task_id}",
            "stderr": "",
        }
    stdout = json.dumps(card, indent=2, ensure_ascii=False, default=str)
    return {"ok": True, "returncode": 0, "command": command, "stdout": stdout, "stderr": ""}


__all__ = ["show_task"]
