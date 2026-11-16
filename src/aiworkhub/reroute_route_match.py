"""NF-2026-02303: select the catalog route a launch-identity reroute targets."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional, Tuple

_ACCEPTED_CAP = 20


def match_reroute_route(
    workers: Iterable[dict],
    *,
    to_runner: str,
    to_adapter_id: str,
    canonical_model: str,
    normalize_model: Callable[[str, str], str],
) -> Tuple[Optional[dict], Optional[str]]:
    """Return ``(route, None)`` or ``(None, error)`` for a reroute target.

    ``to_runner`` may be the catalog ``worker_id`` or the ``execution_runner``.
    No match is ``route_not_found`` (naming every accepted name on the adapter,
    whatever the model); the role error is reserved for a matched route that is
    not an implementation worker.
    """
    wanted_model = normalize_model(canonical_model, to_adapter_id)
    adapter_all: list[dict[str, Any]] = [
        route for route in workers if route.get("effective_adapter_id") == to_adapter_id
    ]
    routes = [
        route
        for route in adapter_all
        if to_runner in (route.get("worker_id"), route.get("execution_runner"))
        and normalize_model(str(route.get("model") or ""), to_adapter_id) == wanted_model
    ]
    if not routes:
        accepted = sorted(
            {
                str(route.get(key))
                for route in adapter_all
                for key in ("execution_runner", "worker_id")
                if route.get(key)
            }
        )[:_ACCEPTED_CAP]
        return None, (
            f"reroute_target_rejected:route_not_found:runner={to_runner}:"
            f"adapter={to_adapter_id}:accepted_execution_runners={','.join(accepted)}"
        )
    if len(routes) != 1:
        return None, (
            "reroute_target_rejected:workforce_route_ambiguous:"
            f"runner={to_runner}:adapter={to_adapter_id}"
        )
    if routes[0].get("implementation_worker") is not True:
        return None, "reroute_target_rejected:implementation_worker_role_required"
    return routes[0], None
