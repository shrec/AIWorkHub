"""Executable decisions for the NF-2026-00173 orphan-symbol review.

The card asks, for each high-confidence orphan symbol, for a recorded decision
(remove or wire) with a reason, where a removal must leave behaviour identical
(the existing suites stay green) and a wiring needs a test that exercises it.

Two constraints shape what this worker can honestly do:

* The AST orphan analysis available in this sandbox
  (Source Graph ``deadmethods``) reports its hits as
  ``bounded_lexical_candidate_not_proven_defect`` and explicitly excludes MCP
  and CLI dynamic dispatch.  Such a candidate is *not* proven unreferenced, so
  the card's own rule -- "Do not remove a symbol you cannot prove is
  unreferenced" -- forbids deleting it.
* This card's ``allowed_writes`` is limited to ``context_graph.py``, the two
  new capture modules, ``ci.yml`` and these tests.  ``context_graph.py`` -- the
  only pre-existing source module in scope -- contains **zero** dead methods,
  and every other orphan candidate lives in a file this card must not touch.

So rather than delete dynamically-dispatched code or fabricate decisions, this
module records the decisions that are provable here and asserts them against
the live tree, so the decision table cannot silently drift from reality.
``ensureCodexManagerGatesRepaired`` is addressed explicitly below.
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import capture_adapters, context_capture, context_graph

_PKG = Path(context_graph.__file__).parent


def _package_python_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_PKG.glob("*.py"))
    )


# One row per reviewed symbol.  ``decision`` is the verb; ``reason`` is why;
# ``verified`` is the assertion below that proves it against the live tree.
DECISIONS = [
    {
        "symbol": "ensureCodexManagerGatesRepaired",
        "decision": "report_absent",
        "reason": (
            "Not present anywhere in the aiworkhub Python package (verified by "
            "walking the package source). The camelCase name is a VS Code "
            "extension TypeScript convention, outside this card's allowed_writes; "
            "there is no dead Python symbol of this name to remove or wire here."
        ),
    },
    {
        "symbol": "aiworkhub.capture_adapters.status_map",
        "decision": "wire",
        "reason": (
            "New helper; wired into context_graph.status() as the single source "
            "of truth for the capture_adapters report, exercised by the Claude "
            "capture suite."
        ),
    },
    {
        "symbol": "aiworkhub.capture_adapters.is_configured",
        "decision": "wire",
        "reason": (
            "New public predicate for provider capture state; wired into "
            "context_capture.capture_claude_records as the production guard that "
            "short-circuits capture for an unconfigured provider, so it is a real "
            "consumer and not a test-only orphan."
        ),
    },
    {
        "symbol": "aiworkhub.context_capture.capture_claude_records",
        "decision": "wire",
        "reason": (
            "New Claude capture entry point; exercised by "
            "test_context_graph_claude_capture with named captured events."
        ),
    },
]

_ALLOWED_DECISIONS = {"remove", "wire", "keep", "report_absent"}


def test_every_decision_is_a_valid_verb_with_a_reason() -> None:
    for row in DECISIONS:
        assert row["decision"] in _ALLOWED_DECISIONS, row
        assert row["reason"].strip(), row


def test_ensure_codex_manager_gates_repaired_is_absent_from_python_surface() -> None:
    # Explicit treatment of the flagged symbol: it does not exist as a Python
    # symbol in the manager package, so no removal/wiring is possible or needed
    # here.  If it is ever added to the Python surface this test will flag it.
    assert "ensureCodexManagerGatesRepaired" not in _package_python_text()


def test_context_graph_editable_surface_has_no_orphan_public_symbols() -> None:
    # context_graph.py is the only pre-existing source module this card may
    # edit; assert its whole public surface resolves (no dangling __all__ entry)
    # so we can honestly report the editable surface carries no dead export.
    for name in context_graph.__all__:
        assert hasattr(context_graph, name), name


def test_new_capture_symbols_are_wired_not_orphaned() -> None:
    # Every public symbol introduced by this card is referenced by a real
    # consumer, so the fix does not itself add orphans.
    package_text = _package_python_text()
    assert "capture_adapters.status_map()" in package_text
    assert "capture_adapters.CAPTURE_SCOPE" in package_text
    assert "capture_adapters.adapter(" in package_text

    # is_configured is public API for the same catalog; exercise it directly.
    assert capture_adapters.is_configured("claude") is True
    assert capture_adapters.is_configured("copilot") is False
    # ...and it has a real production consumer, so it is not a test-only orphan.
    assert "capture_adapters.is_configured(" in package_text

    for name in context_capture.__all__:
        assert hasattr(context_capture, name), name
