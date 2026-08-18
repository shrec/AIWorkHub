"""Fidelity of the shipped MCP summary surfaces in ``server.py``.

Every test here drives the shipped ``@mcp.tool()`` entry point -- never the
canonical summariser behind it -- so the class of drift these tests guard
(a summary that asserts more than it carries, or a private field list that
diverges from the canonical one) cannot recur unnoticed.  See
``SCAN-E3FF-MCP-SUMMARY-FIDELITY``.
"""

from __future__ import annotations

import importlib.util
import io
import json

from aiworkhub import server, task_plan


def _load_stdlib_backed_server():
    """A fresh ``server`` module forced onto the bundled stdlib stdio backend.

    ``_run_stdio_fallback_server`` and the rest of the dependency-free stdio
    fallback are only *defined* when the optional ``mcp`` package is absent --
    they live inside ``except ModuleNotFoundError``.  Binding the test to
    ``server._run_stdio_fallback_server`` on the already-imported module only
    works by accident on hosts where ``mcp`` happens to be missing; where it is
    installed the attribute does not exist and the test errors before it can
    prove anything.  Re-executing the module with ``AIWORKHUB_MCP_STDIO_BACKEND``
    pinned to ``stdlib`` activates that branch deterministically, and does so in
    an isolated module object so the process-wide ``server`` import is untouched.
    """

    spec = importlib.util.spec_from_file_location("aiworkhub.server", server.__file__)
    module = importlib.util.module_from_spec(spec)
    # Relative imports in server.py resolve against this package name.
    module.__package__ = "aiworkhub"
    spec.loader.exec_module(module)
    assert module._MCP_SDK_AVAILABLE is False, "stdlib stdio fallback must be active"
    return module


def _colliding_cards() -> list[dict[str, object]]:
    """Two unclaimed cards that both write the same path -> a real collision."""

    return [
        {
            "task_id": "A",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": ["src/shared.py"],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "B",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": ["src/shared.py"],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
        },
    ]


# ---------------------------------------------------------------------------
# Defect ONE (HIGH): the default task_plan_snapshot dropped every write-scope
# collision field, so it could report a card ready and collision-free while
# full=true reported it in a global collision.
# ---------------------------------------------------------------------------


def test_plan_default_and_full_agree_on_collision_state(monkeypatch):
    full = task_plan.build_snapshot(_colliding_cards())
    monkeypatch.setattr(server.core, "task_plan_snapshot", lambda: full)

    summary = server.aiworkhub_task_plan_snapshot()
    full_view = server.aiworkhub_task_plan_snapshot(full=True)

    # The shipped default receipt carries the collision evidence.
    assert summary["global_collision_free"] is False
    assert summary["global_collision_count"] == 1
    assert summary["global_collision_pairs"] == [["A", "B"]]
    assert summary["card_collision_free"] == {"A": False, "B": False}
    assert summary["card_collision_task_ids"] == {"A": ["B"], "B": ["A"]}

    # A default snapshot and full=true agree about whether cards collide --
    # the property a manager relies on before launching cards in parallel.
    for field in (
        "global_collision_free",
        "global_collision_count",
        "global_collision_pairs",
        "card_collision_free",
        "card_collision_task_ids",
        "card_collision_paths",
    ):
        assert summary[field] == full_view[field]


# ---------------------------------------------------------------------------
# Defect TWO (MEDIUM): omitted_fields was a hardcoded five-element literal
# while the projection dropped more, so an empty invalid_depends_on read as
# "dependencies are fine" while the real reason -- a dropped
# dependency_resolution_errors -- was among the fields never declared.  The
# receipt must be derived from the projection so it names every dropped field.
# ---------------------------------------------------------------------------


def test_plan_omitted_fields_is_derived_and_names_every_drop(monkeypatch):
    snapshot = task_plan.build_snapshot(_colliding_cards())
    # The archived-dependency-not-superseded reason lives in a field the
    # summary drops.  A hand-maintained literal (dependencies/dependents/
    # layers/lifecycle/task_ids) never named it, so an empty
    # invalid_depends_on masked the real failure.
    snapshot["dependency_resolution_errors"] = [
        {"task_id": "A", "reason": "archived_dependency_not_superseded"}
    ]
    # Return a fresh copy per call so a full-mode read cannot mutate the
    # snapshot the summary is derived from.
    monkeypatch.setattr(server.core, "task_plan_snapshot", lambda: dict(snapshot))

    summary = server.aiworkhub_task_plan_snapshot()

    # omitted_fields is derived: exactly the fields present in the full
    # snapshot but dropped from the summary, never a fixed literal.
    expected = sorted(key for key in snapshot if key not in summary)
    assert summary["omitted_fields"] == expected

    # The dropped dependency_resolution_errors is named, so it can no longer
    # hide behind an empty invalid_depends_on.
    assert "dependency_resolution_errors" not in summary
    assert "dependency_resolution_errors" in summary["omitted_fields"]

    # The historical DAG is still declared as dropped.
    for field in ("dependencies", "dependents", "layers", "lifecycle", "task_ids"):
        assert field not in summary
        assert field in summary["omitted_fields"]


# ---------------------------------------------------------------------------
# Defect THREE (MEDIUM): the cost ledger summary dropped by_role and named
# only by_topic and by_runner in omitted_dimensions, so worker vs reviewer
# spend vanished with no trace.  The receipt must be derived from the real
# aggregate map, and the fixture must match the production aggregate shape.
# ---------------------------------------------------------------------------


def _production_shaped_ledger(**kwargs):
    """The six-dimension aggregate shape build_cost_ledger actually emits."""

    return {
        "tool": "aiworkhub_task_cost_ledger",
        "counts": {"union_rows": 3},
        "cost_quality": {"known_records": 3},
        "cache_quality": {"observed_records": 2},
        "aggregates": {
            "by_topic": {"topic-a": {"records": 3}},
            "by_runner": {"runner-a": {"records": 3}},
            "by_model": {"model-a": {"records": 3}},
            "by_provider": {"provider-a": {"records": 3}},
            "by_role": {"worker": {"records": 2}, "reviewer": {"records": 1}},
            "by_day": {"2026-08-04": {"records": 3}},
        },
        "tasks": [{"task_id": "TASK_A"}] if kwargs.get("include_tasks") else [],
    }


def test_cost_ledger_names_by_role_in_derived_omitted_dimensions(monkeypatch):
    monkeypatch.setattr(
        server.cost_ledger, "build_cost_ledger", _production_shaped_ledger
    )

    summary = server.aiworkhub_task_cost_ledger()
    full = server.aiworkhub_task_cost_ledger(full=True)

    # by_role is dropped from the summary aggregates but named in the receipt,
    # which is derived from the real aggregate map.
    assert set(summary["aggregates"]) == {"by_model", "by_provider", "by_day"}
    assert "by_role" not in summary["aggregates"]
    assert "by_role" in summary["omitted_dimensions"]

    full_aggregate_keys = set(full["aggregates"])
    kept = set(summary["aggregates"])
    assert summary["omitted_dimensions"] == sorted(full_aggregate_keys - kept)

    # Full mode keeps the worker vs reviewer split for inspection.
    assert full["aggregates"]["by_role"] == {
        "worker": {"records": 2},
        "reviewer": {"records": 1},
    }


# ---------------------------------------------------------------------------
# Defect FOUR (MEDIUM): the stdlib stdio fallback executed a tools/call sent
# without an id, starting a worker synchronously inside the read loop.
# ---------------------------------------------------------------------------


class _BufferStream:
    """A minimal stand-in exposing the ``.buffer`` the fallback reads/writes."""

    def __init__(self, buffer):
        self.buffer = buffer


def _run_fallback(monkeypatch, tools, messages):
    monkeypatch.setenv("AIWORKHUB_MCP_STDIO_BACKEND", "stdlib")
    stdlib_server = _load_stdlib_backed_server()
    stdin = io.BytesIO(
        b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages)
    )
    stdout = io.BytesIO()
    # ``stdlib_server.sys`` is the shared ``sys`` singleton; monkeypatch restores
    # the real streams (and the env var) at teardown.
    monkeypatch.setattr(stdlib_server.sys, "stdin", _BufferStream(stdin))
    monkeypatch.setattr(stdlib_server.sys, "stdout", _BufferStream(stdout))
    stdlib_server._run_stdio_fallback_server("test-fallback", tools)
    return [
        json.loads(line.decode("utf-8"))
        for line in stdout.getvalue().splitlines()
        if line.strip()
    ]


def test_stdio_idless_tools_call_does_not_start_a_worker(monkeypatch):
    launches: list[dict] = []

    def fake_launch_tool():
        launches.append({"started": True})
        return {"ok": True, "started": True}

    tools = {"aiworkhub_task_auto_pickup": fake_launch_tool}
    call_params = {"name": "aiworkhub_task_auto_pickup", "arguments": {}}

    responses = _run_fallback(
        monkeypatch,
        tools,
        [
            # A genuine notification is still handled and starts nothing.
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            # An id-less tools/call must be refused, never launched.
            {"jsonrpc": "2.0", "method": "tools/call", "params": call_params},
            # Control: the same call WITH an id proves the tool is callable, so
            # the refusal above is specific to the missing id, not a broken tool.
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": call_params},
        ],
    )

    # Exactly one launch: the id-carrying request, never the id-less one.
    assert launches == [{"started": True}]

    by_id = {}
    for response in responses:
        by_id[response.get("id")] = response

    # The id-less tools/call was refused with an explicit error, no result.
    refusal = by_id[None]
    assert "result" not in refusal
    assert refusal["error"]["code"] == -32600
    assert refusal["error"]["message"] == "id_required"

    # The notification produced no response line of its own.
    assert set(by_id) == {None, 7}

    # The id-carrying control call succeeded.
    assert by_id[7]["result"]["structuredContent"] == {"ok": True, "started": True}
