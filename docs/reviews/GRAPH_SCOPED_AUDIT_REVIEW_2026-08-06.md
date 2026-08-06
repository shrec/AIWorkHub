# Graph-scoped audit review

Source: owner-supplied design review, attachment
`14c96f1b-c4c3-4470-9618-e01bec8f9126`, received 2026-08-06.

## Core conclusion

Scoped audit is the read/review counterpart of bounded semantic writes. A
reviewer should receive a graph-derived behavior boundary, not an arbitrary
repository dump: changed symbols, callers/callees, affected tests, persisted
state, applicable contracts, relevant prior failures, and one explicit lens.

## Proposed pipeline

1. Resolve scope from task intent, diff, and semantic boundaries.
2. Expand only evidence-backed impact, tests, contracts, and hotspots.
3. Build a compact reviewer packet with changes, forbidden changes,
   invariants, validation evidence, history, and exact lens.
4. Execute independent correctness, security, concurrency, data-loss,
   API-contract, test-adequacy, performance, migration, UI-state, or
   tool-protocol lenses selected by risk.
5. Normalize findings into structured severity, confidence, evidence level,
   symbol, claim, reproduction, and required validation.
6. Gate blockers by evidence level and persist manager-adjudicated outcomes.

`findings: []` is not automatically evidence of a clean review. A reviewer must
show the examined scope and checks, or return a truthful `inconclusive` reason.
High-severity findings require at least exact static evidence; reproduction is
preferred for P0/P1 claims.

## Intended composition

Source Graph plus semantic replay provides bounded editing. Source Graph plus
scoped audit provides bounded review. Session, Memory, Context Graph, and KB
then provide evidence-gated bounded learning.
