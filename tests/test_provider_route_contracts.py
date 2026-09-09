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

import ast
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


def test_editor_family_reviewer_submit_is_measured_supported_from_the_bridge() -> None:
    """Submit is a route/tool-contract fact, never an installation fact.

    The editor family dispatches ``aiworkhub_worker_quality_review_submit``
    through the same worker bridge as packet_read. That is what ``supported``
    records here; a model being installed or startable does not enter it.
    """

    record = contracts.capability_record(
        _EDITOR_FAMILY, contracts.CAPABILITY_REVIEWER_SUBMIT
    )
    assert record.state == contracts.CAPABILITY_SUPPORTED
    assert record.evidence_class == contracts.EVIDENCE_DECLARED_FROM_CODE_PATH
    assert "process_launcher.py" in record.evidence
    assert (
        contracts.route_can_complete(
            _EDITOR_FAMILY, contracts.CAPABILITY_REVIEWER_SUBMIT
        )
        is True
    )


def test_editor_family_packet_read_is_measured_supported_from_the_bridge() -> None:
    """The one editor-family capability measured against real code, now positive.

    It was measured and NEGATIVE for about an hour:
    ``aiworkhub_worker_quality_review_packet_read`` was absent from the bridge
    dispatch allowlist and fell through to ``worker_bridge_tool_not_allowed``,
    so a reviewer handed a file-transport packet could not read its own
    evidence. a86934d added the dispatch, and this record follows the code.

    The state is asserted here; that it still MATCHES the allowlist is asserted
    by ``test_declared_code_path_claims_match_the_bridge_allowlist``, which
    derives the allowlist rather than restating it. This test alone would rot
    exactly as its predecessor did.
    """

    record = contracts.capability_record(
        _EDITOR_FAMILY, contracts.CAPABILITY_REVIEWER_PACKET_READ
    )
    assert record.state == contracts.CAPABILITY_SUPPORTED
    assert record.evidence_class == contracts.EVIDENCE_DECLARED_FROM_CODE_PATH
    assert "process_launcher.py" in record.evidence


def test_unsupported_and_unknown_are_distinguishable_not_collapsed() -> None:
    """"Measured and negative" and "never measured" are different facts.

    Collapsing them into one boolean is what let a bridge fact read as a
    round-trip fact in the first place; they call for different fixes, so the
    model must keep them apart and the fail-closed reader must refuse both.

    Asserted against constructed records on purpose. The previous version of
    this test reached for whichever live capability happened to be negative
    that day, and went red when the code was FIXED -- a test that fails on good
    news is testing the wrong thing. This is a property of the state model, and
    it holds whether or not any route is currently unsupported.
    """

    measured_negative = contracts._record(
        contracts.CAPABILITY_REVIEWER_PACKET_READ,
        contracts.CAPABILITY_UNSUPPORTED,
        contracts.EVIDENCE_DECLARED_FROM_CODE_PATH,
        evidence="src/aiworkhub/process_launcher.py:1-2",
        reason=contracts.REASON_BRIDGE_TOOL_NOT_ALLOWED,
    )
    never_measured = contracts._record(
        contracts.CAPABILITY_REVIEWER_SUBMIT,
        contracts.CAPABILITY_UNKNOWN,
        contracts.EVIDENCE_UNVERIFIED,
        reason=contracts.REASON_NO_OBSERVED_ROUND_TRIP,
    )
    assert never_measured.state == contracts.CAPABILITY_UNKNOWN
    assert measured_negative.state != never_measured.state
    # Different facts, and the reader refuses both all the same.
    assert measured_negative.state != contracts.CAPABILITY_SUPPORTED
    assert never_measured.state != contracts.CAPABILITY_SUPPORTED
    # The negative one carries an actionable reason; the unmeasured one says
    # only that nothing was observed. That difference is the point.
    assert measured_negative.reason == contracts.REASON_BRIDGE_TOOL_NOT_ALLOWED
    assert never_measured.reason == contracts.REASON_NO_OBSERVED_ROUND_TRIP

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


def test_editor_route_is_offered_for_reviewer_submit_from_the_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Launchability still does not invent the capability; the contract does.

    An editor-hosted route may be unlaunchable on this host. When it is
    launchable, reviewer_submit is offered because the route/tool contract
    dispatches the authenticated submit tool, not because a model is installed.
    """

    report = _preflight(monkeypatch, tmp_path)
    offered = report["provider_summary"]["capability_launchable_routes"][
        contracts.CAPABILITY_REVIEWER_SUBMIT
    ]
    launchable = {
        str(item["adapter_id"])
        for item in report["providers"]
        if item.get("launchable") and item.get("coverage_required", True)
    }
    for adapter_id in _EDITOR_ADAPTERS:
        if adapter_id in launchable:
            assert adapter_id in offered
        else:
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
        assert submit["state"] == contracts.CAPABILITY_SUPPORTED
        assert submit["evidence_class"] == contracts.EVIDENCE_DECLARED_FROM_CODE_PATH
        assert "process_launcher.py" in submit["evidence"]


def _bridge_dispatch_citation() -> tuple[Path, str]:
    """Split ``_EDITOR_BRIDGE_DISPATCH`` into the file and the function it names.

    The citation names a function, not a line range, and this helper refuses a
    line range outright. The range form moved three times in a single day --
    any insertion anywhere above the dispatch chain re-broke the test below
    while the fact it asserts had not changed at all. A test that fails on
    unrelated edits teaches people to edit the citation until it goes green,
    which is precisely how the hand-written copy this file was written to
    catch rotted in the first place.
    """

    path_text, separator, symbol = contracts._EDITOR_BRIDGE_DISPATCH.partition("::")
    assert separator and symbol, (
        "the dispatch citation must name a function as 'path::qualname'; a "
        f"line range rots on every unrelated edit: {contracts._EDITOR_BRIDGE_DISPATCH!r}"
    )
    root = Path(__file__).resolve().parents[1]
    return root / path_text, symbol


def _dispatched_worker_tools(
    source_path: Path, symbol: str
) -> tuple[set[str], dict[str, int], tuple[int, int]]:
    """Every tool name the bridge dispatches, read from the source itself.

    Derived, never restated. The registry claims a fact ABOUT this dispatch
    chain, so the claim has to be checked against the chain rather than against
    a second hand-written copy of it -- a hand-written copy is what rotted.

    Also returns the span the citation resolves to, so a failure can say where
    the chain actually is instead of leaving the reader to find it.
    """

    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    defined = [
        found
        for found in ast.walk(tree)
        if isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef))
        and found.name == symbol
    ]
    assert defined, f"{symbol} not found in the cited file {source_path}"
    # A second definition would make the lookup silently pick one of them, and
    # the citation could then describe a function nobody calls.
    assert len(defined) == 1, (
        f"{symbol} is defined {len(defined)} times in {source_path} at lines "
        f"{[found.lineno for found in defined]}; the citation is ambiguous"
    )
    node = defined[0]
    span = (node.lineno, node.end_lineno or node.lineno)

    names: set[str] = set()
    lines: dict[str, int] = {}
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Compare) or len(inner.comparators) != 1:
            continue
        left, right = inner.left, inner.comparators[0]
        if not (isinstance(left, ast.Name) and left.id == "tool_name"):
            continue
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            continue
        names.add(right.value)
        lines.setdefault(right.value, inner.lineno)
    return names, lines, span


def test_declared_code_path_claims_match_the_bridge_allowlist():
    """A ``declared_from_code_path`` claim must be re-derivable from that path.

    This record was ``unsupported`` and became false within the hour, when
    ``aiworkhub_worker_quality_review_packet_read`` was added to the bridge
    allowlist (a86934d). Nothing failed: the registry restated a fact about a
    code path, and no test tied the restatement to the path. That is the
    silent-rot hazard of the entire evidence class, so it is closed here by
    deriving the allowlist from the source instead of trusting the comment.
    """

    source_path, symbol = _bridge_dispatch_citation()
    assert source_path.is_file(), f"cited evidence file is missing: {source_path}"
    dispatched, at_line, (first_line, last_line) = _dispatched_worker_tools(
        source_path, symbol
    )

    packet_read = "aiworkhub_worker_quality_review_packet_read"
    record = contracts.capability_record(
        runtime_adapters.ROUTE_FAMILY_EDITOR_VSCODE_LM,
        contracts.CAPABILITY_REVIEWER_PACKET_READ,
    )
    assert record.evidence_class == contracts.EVIDENCE_DECLARED_FROM_CODE_PATH

    # The claim and the code path must agree in BOTH directions: supported iff
    # dispatched. Either half alone would let the pair drift again.
    expected = (
        contracts.CAPABILITY_SUPPORTED
        if packet_read in dispatched
        else contracts.CAPABILITY_UNSUPPORTED
    )
    assert record.state == expected, (
        f"registry says {record.state!r} for {packet_read}, but the bridge "
        f"{'dispatches' if packet_read in dispatched else 'refuses'} it"
    )

    # The citation must point at the code it claims to describe. That used to
    # be a line-range check against a hand-copied span, and the copy is what
    # went stale -- so the span is now derived from the named function and the
    # bracketing holds by construction. What is left to check is that the name
    # resolves to a function that really is the dispatch chain: a citation
    # pointed at some other function still parses, and would quietly describe
    # code the bridge never runs.
    assert last_line > first_line, (
        f"{symbol} resolves to a single line at {first_line}; that is not a "
        "dispatch chain"
    )
    if packet_read in dispatched:
        assert first_line <= at_line[packet_read] <= last_line

    submit = "aiworkhub_worker_quality_review_submit"
    submit_record = contracts.capability_record(
        runtime_adapters.ROUTE_FAMILY_EDITOR_VSCODE_LM,
        contracts.CAPABILITY_REVIEWER_SUBMIT,
    )
    assert submit_record.evidence_class == contracts.EVIDENCE_DECLARED_FROM_CODE_PATH
    submit_expected = (
        contracts.CAPABILITY_SUPPORTED
        if submit in dispatched
        else contracts.CAPABILITY_UNSUPPORTED
    )
    assert submit_record.state == submit_expected
    assert submit in dispatched
    assert first_line <= at_line[submit] <= last_line
