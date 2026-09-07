"""Contract tests for the protocol-first provider route capability registry.

These assert the property NF-2026-00669 was filed about: a route's ability to
be STARTED must never be readable as its ability to COMPLETE a unit of work,
and an unmeasured capability must never read as a supported one.

Nothing here forces a platform or a host binary.  Release qualification runs
these on real Windows and macOS, so every assertion is either a pure registry
fact or a structural invariant of the preflight report that holds whatever
routes happen to resolve on the machine running the suite.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import provider_route_contracts as contracts
from aiworkhub import repo_policy, runtime_adapters

_EDITOR_FAMILY = runtime_adapters.ROUTE_FAMILY_EDITOR_VSCODE_LM
_EDITOR_ADAPTERS = (
    runtime_adapters.VSCODE_LM_ADAPTER,
    runtime_adapters.GLM_VSCODE_LM_ADAPTER,
    runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
)


# ---------------------------------------------------------------------------
# Fail-closed semantics.
# ---------------------------------------------------------------------------


def test_editor_family_reviewer_submit_is_unknown_never_supported() -> None:
    """The defect itself: bridge presence must not imply a completed submit.

    Every signal preflight had for this family -- bridge module present, host
    count, access consent -- is a fact about the bridge.  No round trip has
    been observed, so the honest state is unknown.
    """

    record = contracts.capability_record(
        _EDITOR_FAMILY, contracts.CAPABILITY_REVIEWER_SUBMIT
    )
    assert record.state == contracts.CAPABILITY_UNKNOWN
    assert record.evidence_class == contracts.EVIDENCE_UNVERIFIED
    assert record.reason == contracts.REASON_NO_OBSERVED_ROUND_TRIP
    # The fail-closed reader must refuse it.
    assert (
        contracts.route_can_complete(
            _EDITOR_FAMILY, contracts.CAPABILITY_REVIEWER_SUBMIT
        )
        is False
    )


def test_editor_family_packet_read_is_measured_unsupported_with_exact_reason() -> None:
    """The one editor-family capability that WAS measured, and is negative.

    ``aiworkhub_worker_quality_review_packet_read`` is absent from the bridge
    dispatch allowlist, so it falls through to ``worker_bridge_tool_not_allowed``.
    """

    record = contracts.capability_record(
        _EDITOR_FAMILY, contracts.CAPABILITY_REVIEWER_PACKET_READ
    )
    assert record.state == contracts.CAPABILITY_UNSUPPORTED
    assert record.evidence_class == contracts.EVIDENCE_DECLARED_FROM_CODE_PATH
    assert record.reason == contracts.REASON_BRIDGE_TOOL_NOT_ALLOWED
    assert "process_launcher.py" in record.evidence


def test_unsupported_and_unknown_are_distinguishable_not_collapsed() -> None:
    """"Measured and negative" and "never measured" are different facts.

    Collapsing them into one boolean is what made a bridge fact readable as a
    round-trip fact in the first place; they call for different fixes.
    """

    measured_negative = contracts.capability_record(
        _EDITOR_FAMILY, contracts.CAPABILITY_REVIEWER_PACKET_READ
    )
    never_measured = contracts.capability_record(
        _EDITOR_FAMILY, contracts.CAPABILITY_REVIEWER_SUBMIT
    )
    assert measured_negative.state != never_measured.state
    # Both are refused by the fail-closed reader all the same.
    assert measured_negative.state != contracts.CAPABILITY_SUPPORTED
    assert never_measured.state != contracts.CAPABILITY_SUPPORTED


def test_undeclared_capability_on_declared_family_is_unknown() -> None:
    """Silence in a contract is unknown, never an inherited yes."""

    record = contracts.capability_record(
        runtime_adapters.ROUTE_FAMILY_CODEX_CLI,
        contracts.CAPABILITY_REVIEWER_PACKET_READ,
    )
    assert record.state == contracts.CAPABILITY_UNKNOWN
    assert record.reason == contracts.REASON_NOT_DECLARED


def test_unknown_family_and_unknown_capability_fail_closed() -> None:
    """A route or capability nobody registered can never read as supported."""

    unknown_family = contracts.capability_record(
        "some_family_that_does_not_exist", contracts.CAPABILITY_REVIEWER_SUBMIT
    )
    assert unknown_family.state == contracts.CAPABILITY_UNKNOWN
    assert unknown_family.reason == contracts.REASON_FAMILY_UNKNOWN

    unknown_capability = contracts.capability_record(
        _EDITOR_FAMILY, "teleportation"
    )
    assert unknown_capability.state == contracts.CAPABILITY_UNKNOWN
    assert unknown_capability.reason == contracts.REASON_CAPABILITY_UNKNOWN

    assert contracts.route_can_complete("nope", "teleportation") is False
    assert contracts.adapter_can_complete("not_an_adapter", "teleportation") is False


def test_unregistered_adapter_resolves_to_unknown_family() -> None:
    """A new adapter must not silently inherit some other family's contract."""

    assert runtime_adapters.route_family("brand_new_cli") == (
        runtime_adapters.ROUTE_FAMILY_UNKNOWN
    )
    assert (
        contracts.adapter_can_complete(
            "brand_new_cli", contracts.CAPABILITY_REVIEWER_SUBMIT
        )
        is False
    )


# ---------------------------------------------------------------------------
# Registry invariants: the registry must not be able to lie about itself.
# ---------------------------------------------------------------------------


def test_supported_never_rests_on_unverified_evidence() -> None:
    """A supported claim must cite a real evidence class and a real citation.

    This is the invariant that stops the registry becoming the very thing it
    replaced: a declared fact standing in for a measured one.
    """

    for family, contract in contracts.ROUTE_CONTRACTS.items():
        for name, record in contract.capabilities.items():
            if record.state != contracts.CAPABILITY_SUPPORTED:
                continue
            assert record.evidence_class != contracts.EVIDENCE_UNVERIFIED, (
                f"{family}.{name} claims supported on unverified evidence"
            )
            assert record.evidence, f"{family}.{name} claims supported with no citation"


def test_every_record_uses_the_declared_vocabulary() -> None:
    valid_states = {
        contracts.CAPABILITY_SUPPORTED,
        contracts.CAPABILITY_UNSUPPORTED,
        contracts.CAPABILITY_UNKNOWN,
    }
    for family, contract in contracts.ROUTE_CONTRACTS.items():
        assert contract.route_family == family
        for name, record in contract.capabilities.items():
            assert name in contracts.CAPABILITY_VOCABULARY
            assert record.capability == name
            assert record.state in valid_states
            # An unsupported claim is useless without a reason to act on.
            if record.state == contracts.CAPABILITY_UNSUPPORTED:
                assert record.reason


def test_unretrieved_documentation_is_explicitly_unknown() -> None:
    """No contract may imply it verified documentation it never fetched."""

    for contract in contracts.ROUTE_CONTRACTS.values():
        assert contract.documentation_urls
        # Nothing in this repository has retrieved or digested these sources,
        # so both fields must say so rather than sitting empty.
        assert contract.documentation_retrieved_at == contracts.UNKNOWN_VALUE
        assert contract.documentation_digest == contracts.UNKNOWN_VALUE
        assert contract.last_verification == contracts.VERIFICATION_NEVER_RUN


# ---------------------------------------------------------------------------
# Protocol-first, not model-name-first.
# ---------------------------------------------------------------------------


def test_every_editor_hosted_model_shares_one_contract() -> None:
    """GLM and DeepSeek over the editor bridge are one protocol, not two.

    A new model on this family therefore inherits the whole contract with no
    code change -- which is the point of keying on route family.
    """

    families = {runtime_adapters.route_family(a) for a in _EDITOR_ADAPTERS}
    assert families == {_EDITOR_FAMILY}

    baseline = contracts.describe_adapter_capabilities(_EDITOR_ADAPTERS[0])
    for adapter_id in _EDITOR_ADAPTERS[1:]:
        assert (
            contracts.describe_adapter_capabilities(adapter_id)["capabilities"]
            == baseline["capabilities"]
        )


def test_every_catalog_adapter_is_inventoried() -> None:
    report = contracts.registry_report()
    for adapter_id in runtime_adapters.LOCAL_ADAPTERS:
        assert adapter_id in report["adapters"]
        # Every catalog adapter must land on a family that has a contract, so
        # no route is simply missing from the inventory.
        assert report["adapters"][adapter_id] in contracts.ROUTE_CONTRACTS


def test_repo_policy_and_registry_cannot_disagree_about_family() -> None:
    """One classifier decides which contract a route inherits."""

    for adapter_id in runtime_adapters.LOCAL_ADAPTERS:
        assert repo_policy._adapter_route_family(adapter_id) == (
            contracts.route_family_for_adapter(adapter_id)
        )


# ---------------------------------------------------------------------------
# Preflight wiring.
# ---------------------------------------------------------------------------


def _preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Preflight with its slow probes stubbed; provider routes stay real.

    Nothing here injects a platform or replaces a host capability: the route
    set is whatever this machine actually resolves, and every assertion below
    is written to hold for any such set.
    """

    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")

    from aiworkhub import task_reconciler

    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy.task_store, "callback_bridge_health", lambda _root: {"ok": True}
    )
    monkeypatch.setattr(
        repo_policy.workspace_hygiene,
        "inventory",
        lambda _root, refresh_sizes=False: {},
    )
    monkeypatch.setattr(
        repo_policy.source_graph_daemon,
        "daemon_health",
        lambda _root: {
            "ok": True,
            "status": "ready",
            "running": True,
            "registered": True,
            "readable_generation": 7,
            "last_success_at": "2026-09-07T00:00:00Z",
            "build_revision": "rev",
            "files_seen": 12,
            "index_age_seconds": 1,
            "stale_after_seconds": 600,
        },
    )
    monkeypatch.setattr(
        task_reconciler, "reconciler_health", lambda _root: {"ok": True, "running": True}
    )
    return repo_policy.build_preflight(root)


def test_preflight_publishes_the_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _preflight(monkeypatch, tmp_path)
    registry = report["provider_route_contracts"]
    assert registry["schema_id"] == contracts.PROVIDER_ROUTE_CONTRACT_SCHEMA_ID
    assert _EDITOR_FAMILY in registry["route_families"]


def test_no_editor_route_is_offered_for_a_reviewer_submit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The contradiction the owner reported, asserted directly.

    An editor-hosted route may well be launchable on this host -- that is a
    true statement about starting it.  It must still never appear among the
    routes offered for a capability the registry has not verified.
    """

    report = _preflight(monkeypatch, tmp_path)
    offered = report["provider_summary"]["capability_launchable_routes"][
        contracts.CAPABILITY_REVIEWER_SUBMIT
    ]
    for adapter_id in _EDITOR_ADAPTERS:
        assert adapter_id not in offered


def test_capability_offers_are_a_subset_of_launchable_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A capability offer can only ever narrow the launchable set."""

    report = _preflight(monkeypatch, tmp_path)
    launchable = {
        str(item["adapter_id"])
        for item in report["providers"]
        if item.get("launchable") and item.get("coverage_required", True)
    }
    summary = report["provider_summary"]
    for capability in contracts.CAPABILITY_VOCABULARY:
        offered = set(summary["capability_launchable_routes"][capability])
        assert offered <= launchable
        excluded = {e["adapter_id"] for e in summary["capability_exclusions"][capability]}
        # Every launchable route is accounted for exactly once: offered or
        # excluded with a reason, never silently dropped.
        assert offered.isdisjoint(excluded)
        assert offered | excluded == launchable


def test_every_capability_exclusion_names_an_exact_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _preflight(monkeypatch, tmp_path)
    summary = report["provider_summary"]
    for capability in contracts.CAPABILITY_VOCABULARY:
        for exclusion in summary["capability_exclusions"][capability]:
            assert exclusion["reason"], exclusion
            assert exclusion["state"] in {
                contracts.CAPABILITY_UNSUPPORTED,
                contracts.CAPABILITY_UNKNOWN,
            }
            assert exclusion["capability"] == capability


def test_observability_carries_capabilities_beside_reachability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reachability evidence and capability truth must travel together.

    The block that says ``bridge_module_present`` is the same block that must
    say what a bridge being present does and does not prove.
    """

    report = _preflight(monkeypatch, tmp_path)
    adapters = report["provider_observability"]["adapters"]
    assert adapters
    for entry in adapters:
        assert set(entry["capabilities"]) == set(contracts.CAPABILITY_VOCABULARY)
        if entry["route_family"] != _EDITOR_FAMILY:
            continue
        submit = entry["capabilities"][contracts.CAPABILITY_REVIEWER_SUBMIT]
        assert submit["state"] == contracts.CAPABILITY_UNKNOWN
        assert submit["reason"] == contracts.REASON_NO_OBSERVED_ROUND_TRIP
