# Semantic Delta Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Review only changed candidate ranges and the bounded behavior they can affect, while retaining fail-closed escalation for unknown impact.

**Architecture:** The coordinator already seals changed-hunk `source_evidence`, candidate hashes, prior-review deltas and Source Graph scoped audits. Keep those authorities. Replace the single min-to-max envelope in `quality_review_scope._line_span` with exact normalized segment intervals for symbol and edge selection; then make the reviewer prompt and receipt state the same boundary. Do not make a semantic-edit receipt a substitute for the final diff: it is supplementary provenance, while the final changed bytes remain authoritative.

**Tech Stack:** Python 3.12–3.14, pytest, Source Graph, existing sealed quality-review packet.

**Spec:** Owner requirement in the current AIWorkHub conversation: review the model's changed output plus affected context, not unchanged previously accepted code.

## Global Constraints

- Preserve the sealed packet digest and existing reviewer-ingest identity checks.
- Changed candidate bytes and their final diff are authoritative even if semantic-edit receipts are absent.
- Keep `known_unknowns` explicit; unresolved impact must never be silently reported clean.
- Do not read or scan the repository root as part of reviewer preparation.
- Keep tests and production call sites in each task's `allowed_writes`.

## Review Focus

- Two distant hunks in one large file must not select symbols between them.
- A deletion-only hunk must still identify a relevant baseline/canonical symbol or an explicit unknown.
- A changed public symbol's bounded callers and tests must remain visible even outside changed lines.
- Missing or malformed segment evidence must fail closed, not become a claim of zero impact.
- Packet/receipt hashes and reviewer scope must stay stable under different row orderings.

---

### Task 1: Exact changed-segment scope

**Files:**
- Modify: `src/aiworkhub/quality_review_scope.py:85-105,172-504`
- Test: `tests/test_quality_review_scope.py`

**Interfaces:**
- Consumes: `source_evidence[path]["segments"]` with candidate/baseline start/end lines and the existing candidate extraction.
- Produces: a normalized, sorted union of changed intervals for selecting target symbols and candidate edges; the packet's existing `ChangedPath` summary may remain a presentation envelope, but it must not drive selection.

- [ ] **Step 1: Add a failing regression test.** Create a source file with functions at lines near 10, 500 and 1000, change only the first and last, and assert `build_scoped_audits` targets the first/last but never the middle function. Add a deletion-only segment and malformed-segment case with explicit unknown/refusal expectations.
- [ ] **Step 2: Run `python3 -m pytest -q tests/test_quality_review_scope.py` and confirm the new test fails because the middle function is selected.**
- [ ] **Step 3: Implement one bounded interval normalizer.** Sort and merge only overlapping/adjacent valid segments; select an entity or edge when it intersects any interval. For a deletion-only segment use its baseline range against canonical entities. Preserve the current summary envelope only where the existing schema requires it.
- [ ] **Step 4: Run `python3 -m pytest -q tests/test_quality_review_scope.py tests/test_scoped_audit.py`, `python3 -m ruff check src/aiworkhub/quality_review_scope.py tests/test_quality_review_scope.py`, and `git diff --check`.**
- [ ] **Step 5: Stop at independent manager review.** Do not commit or accept your own task.

### Task 2: Reviewer boundary and measurement

**Files:**
- Modify: `src/aiworkhub/quality_reviewer.py:693-809`
- Test: `tests/test_quality_reviewer.py`
- Test: `tests/test_aiworkhub_quality_reviewer.py`

**Interfaces:**
- Consumes: the Task 1 scoped audit, sealed candidate `source_evidence` and `candidate.delta`.
- Produces: a reviewer instruction to inspect changed hunks first, then only graph-connected callers/tests and explicitly named unknowns; a bounded, deterministic packet comparison for unchanged candidate paths.

- [ ] **Step 1: Add a failing prompt/packet test.** Assert the prompt identifies changed hunks and their bounded impact as the primary review boundary, preserves known unknown escalation, and does not ask for a whole-file/whole-repository re-review. Assert unchanged paths are recognized from `candidate.delta` without dropping their contract evidence.
- [ ] **Step 2: Run `python3 -m pytest -q tests/test_quality_reviewer.py tests/test_aiworkhub_quality_reviewer.py` and confirm the new assertion fails.**
- [ ] **Step 3: Change only the prompt wording and any required bounded packet projection.** Do not weaken packet hashes, reviewer submit identity or mechanical gates. Do not claim a token saving without measured usage.
- [ ] **Step 4: Run the exact tests above, `python3 -m ruff check src/aiworkhub/quality_reviewer.py tests/test_quality_reviewer.py tests/test_aiworkhub_quality_reviewer.py`, and `git diff --check`.**
- [ ] **Step 5: Stop at independent manager review.** Compare target-symbol count and packet bytes on the same discontiguous-hunk fixture before/after; report measured values, not an assumed percentage.
