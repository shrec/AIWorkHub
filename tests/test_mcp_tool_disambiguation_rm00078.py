"""RM-2026-00078 / NF-2026-02144: sibling tools tell the model which one to pick.

(a) Within one profile (manager with writes on and off, worker, dashboard) no two listed tools open with the
    same first sentence, so a client that shows only the lead sentence can still tell them apart.
(b) Every member of a near-duplicate cluster that the manager profile lists names at least one other listed
    member of its own cluster, i.e. carries a "Use this for X; use <sibling> for Y" pointer.

The listings come from the NF-02138 child runner (the published stdlib tools/list per profile).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from test_mcp_tool_description_refs_nf02138 import _references, listings  # noqa: E402,F401

_PROFILE_CASES = (("manager", "1"), ("manager", "0"), ("worker", "1"), ("dashboard", "1"))
_MANAGER_CASES = (("manager", "1"), ("manager", "0"))

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _cluster(*names: str) -> frozenset:
    return frozenset(name if name.startswith(("needfix_", "roadmap_")) else f"aiworkhub_{name}" for name in names)


CLUSTERS = {
    "REVIEW": _cluster(
        "review_packet", "task_review_summarize", "task_codex_handoff", "task_codex_handoff_markdown",
        "completion_inbox", "task_review_queue", "agent_collect_result", "agent_accept_preview",
    ),
    "STATUS": _cluster(
        "task_show", "agent_task_status", "task_list", "agent_list_processes", "supervisor_loop_status",
        "task_plan_snapshot",
    ),
    "HEALTH": _cluster(
        "task_health", "doctor_diagnose", "source_graph_health", "dispatcher_health", "environment_preflight",
    ),
    "NEEDFIX": _cluster(
        "needfix_update", "needfix_archive", "needfix_restore", "needfix_add", "needfix_list", "needfix_show",
        "needfix_count", "needfix_events", "manager_needfix_draft", "dashboard_needfix_transition",
    ),
    "ROADMAP": _cluster(
        "roadmap_list", "roadmap_show", "roadmap_snapshot", "roadmap_transition", "roadmap_link_task",
    ),
}


def _first_sentence(description: str) -> str:
    text = " ".join((description or "").split())
    return _SENTENCE_END.split(text, maxsplit=1)[0].strip()


def test_cluster_members_are_registered_tools(listings):
    # Fail closed on a misspelled member: a name that is not registered would make (b) vacuous for it.
    registered = {entry["name"] for entry in listings[("all", "1")]}
    unknown = sorted(name for members in CLUSTERS.values() for name in members if name not in registered)
    assert not unknown, f"cluster members that are not registered tools: {unknown}"


def test_no_two_listed_tools_share_a_first_sentence(listings):
    violations = set()
    for key in _PROFILE_CASES:
        entries = listings[key]
        # The NF-02138 runner measures an empty tools/list for this server's worker surface, so it has
        # nothing to compare; an empty manager or dashboard list is a broken measurement and fails.
        assert entries or key[0] == "worker", f"{key}: empty tools/list"
        seen: dict[str, list[str]] = {}
        for entry in entries:
            sentence = _first_sentence(entry.get("description", ""))
            assert sentence, f"{key}: {entry['name']} has no first sentence"
            seen.setdefault(sentence.casefold(), []).append(entry["name"])
        for sentence, names in seen.items():
            if len(names) > 1:
                violations.add(f"{sorted(names)} share first sentence {sentence[:80]!r}")
    if violations:
        pytest.fail("\n".join(sorted(violations)), pytrace=False)


def test_every_listed_cluster_member_names_a_listed_sibling(listings):
    registered = frozenset(entry["name"] for entry in listings[("all", "1")])
    listed_by_case = {key: {entry["name"] for entry in listings[key]} for key in _PROFILE_CASES}
    violations: dict[str, list[str]] = {}
    no_shared_sibling = set()
    checked = 0
    for key in _MANAGER_CASES:
        by_name = {entry["name"]: entry for entry in listings[key]}
        for cluster, members in sorted(CLUSTERS.items()):
            listed_members = members & by_name.keys()
            for name in sorted(listed_members):
                checked += 1
                # One docstring is published on every profile that lists the tool, and NF-02138 forbids naming
                # a tool a profile does not list, so only a sibling listed on all of those profiles is nameable.
                nameable = set(listed_members - {name})
                for case_listed in listed_by_case.values():
                    if name in case_listed:
                        nameable &= case_listed
                if not nameable:
                    no_shared_sibling.add(name)
                    continue
                named = _references(by_name[name].get("description", ""), registered)
                if not (named & (listed_members - {name})):
                    violations.setdefault(f"{cluster} {name}", []).append(f"writes={key[1]}")
    # Fail closed: the manager profile must list cluster members for this check to mean anything.
    assert checked >= sum(len(members) for members in CLUSTERS.values()), checked
    # Measured: the dashboard profile lists aiworkhub_dashboard_needfix_transition but none of its manager
    # NEEDFIX siblings (its own siblings are aiworkhub_dashboard_needfix_*, hidden on manager), so no name
    # satisfies both profiles. Pinned exactly, so a new case or a shared sibling appearing fails here.
    assert no_shared_sibling == {"aiworkhub_dashboard_needfix_transition"}, sorted(no_shared_sibling)
    if violations:
        pytest.fail(
            "cluster members naming no listed sibling:\n"
            + "\n".join(f"{member} ({', '.join(modes)})" for member, modes in sorted(violations.items())),
            pytrace=False,
        )
