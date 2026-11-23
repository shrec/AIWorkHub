"""NF-2026-02393: the VS Code Manager Chat panel's verified repo-local manager-console identity.

The panel's MCP clients are children of the VS Code extension host, not of a
provider chat. They are bound to this repository by the window route record
the extension writes (``coordinator-targets.json``): ``AIWORKHUB_WINDOW_ID``
equals the route ``window_id``, the storage ``repo_id`` equals the route
``repo_id`` and the route's ``extension_host_pid`` is in this process's
same-uid ancestor chain. Before this module that binding was honoured only
when the window had selected Codex, so in Claude-selected mode the panel had
no manager identity and every ``aiworkhub_manager_loop_*`` call refused
``verified_manager_identity_required``.

:func:`bound_window_route` is the ONE owner of that binding check; the Codex
extension route (``core._codex_extension_route_manager_identity``) narrows it
to ``selected_provider == "codex"`` and :func:`verified_identity` grants the
provider-independent console identity. The console identity is deliberately
NOT a provider route: provider ``manager_console``, ``callback_supported``
``"false"``, no ``thread_id``. A process with Claude ancestry is refused (a
Claude chat owns its own route and callback lane).

Audit of every consumer of the identity's ``provider`` field (handoff record):

- ``core.manager_bootstrap``: ``reply["provider"]`` becomes ``manager_console``
  and ``role`` ``manager``. The ``== "claude"`` seat branch never matches; with an
  open Manager Chat record the generic seat branch projects the seat route,
  exactly as for a Codex route; the NF-01817 branch is only for ``identity is
  None`` and is untouched. ``claude_route_probe`` is still reported (provider is
  not ``claude``).
- ``core._bootstrap_dispatcher``: admits only ``claude``/``codex``/``manager_chat``,
  so this identity gets ``not_applicable`` -- no dispatcher start, no inbox rebind.
- ``core._adopt_callbacks_on_bootstrap``: admits only callback-capable
  ``claude``/``codex``; ``callback_supported`` ``"false"`` refuses -- adopts nothing.
- ``core.stamped_manager_identity``: the ``!= "claude"`` rule lets an open seat
  stamp its own route, the same as for Codex; otherwise this dict is stamped.
- ``core.create_task``: ``provider`` is recorded on the card; with no origin a
  callback-required card refuses ``callback_route_pending:manager_console_origin_id_not_observed``.
- ``core._repository_switch_locked`` and ``core.repository_current`` resolve
  their own Claude/Codex chain, never this one; a switch still needs Codex.
- ``core.dispatcher_health`` and ``manager_callback_wait.claude_callback_wait``/
  ``claude_callback_ack`` read ``core._claude_manager_identity`` only, so the
  dispatch/manager_inbox decision and the Claude callback lane are unchanged.
- ``manager_ai_tools._manager_context``: runner label ``manager_console_manager``.
- ``server._manager_loop_repo_root`` / ``manager_loop._manager_authority``: read
  ``role`` and the session id only, never ``provider``; ``manager_loop``'s own
  ``provider`` values are event-log labels from the backend, not this identity.
"""

from __future__ import annotations

from typing import Any

MANAGER_CONSOLE_PROVIDER = "manager_console"
EXTENSION_HOST_ANCESTRY_MAX_DEPTH = 4


def bound_window_route(selected_provider: str = "") -> tuple[str, str, dict[str, Any]] | None:
    """``(window_id, repo_id, target)`` when this process is the window route's extension-host child.

    ``selected_provider``, when given, must equal the route's selected provider
    (checked before the process walk). Every unreadable or mismatched fact
    yields None: a missing route file reads as the no-provider default, which
    carries no ``repo_id`` and so never binds.
    """
    from . import core  # lazy: core imports this module

    # Resolved through core's ``os`` so core's process seams (tests patch ``core.os``) stay one seam.
    window_id = str(core.os.environ.get("AIWORKHUB_WINDOW_ID", "") or "").strip()
    if not window_id:
        return None
    try:
        root = core.repo_root()
        readiness = core.task_store.storage_readiness(root)
        if not readiness.ready or not readiness.repo_id:
            return None
        target = core.read_selected_coordinator_target(root)
        selected = str(target.get("selected_provider") or "").strip().lower()
        extension_host_pid = int(target.get("extension_host_pid") or 0)
    except (OSError, RuntimeError, core.task_store.TaskStoreError, AttributeError, KeyError, TypeError, ValueError):
        return None
    if not selected or (selected_provider and selected != selected_provider):
        return None
    repo_id = str(readiness.repo_id)
    if str(target.get("repo_id") or "").strip() != repo_id:
        return None
    if str(target.get("window_id") or "").strip() != window_id:
        return None
    if extension_host_pid <= 1 or not core._pid_in_same_uid_ancestor_chain(
        extension_host_pid, max_depth=EXTENSION_HOST_ANCESTRY_MAX_DEPTH
    ):
        return None
    return window_id, repo_id, target


def verified_identity() -> dict[str, str] | None:
    """The panel's repo-local manager-console identity, or None (fail closed)."""
    from . import core  # lazy: core imports this module

    bound = bound_window_route()
    if bound is None:
        return None
    if core.manager_route_probe.claude_ancestry_present():  # a Claude chat holds its own route
        return None
    window_id, _repo_id, target = bound
    session_id = str(target.get("claim_episode") or "").strip() or window_id
    return {
        "provider": MANAGER_CONSOLE_PROVIDER,
        "session_id": session_id,
        "thread_id": "",
        "window_id": window_id,
        "callback_supported": "false",
        "route_state": "repo_local",
    }
