# UltrafastSecp256k1 assurance-port review

Source: owner-supplied donor-capability review, attachment
`4915077a-b4e2-4d31-a10b-ab60555c2621`, received 2026-08-06.

## Port boundary

UltrafastSecp256k1 is an evidence donor, not a second runtime authority. Do not
copy its crypto engine, GPU backends, legacy graph/session databases, or
parallel script interfaces. AIWorkHub already owns canonical Source Graph,
task isolation, callbacks, memory, KB, and known-bug scanning.

## Recommended assurance capabilities

### P0

- Assurance-as-code gate joining public tool surfaces, graph freshness,
  documentation claims, tests/evals, policy knobs, and replay artifacts.
- Release-blocking Source Graph retrieval goldens and required-surface checks.
- Normalized validation failure classes and bounded diagnostic rework.
- Negative fixtures for placeholders, behavioral collapse, empty reviews, and
  dormant quality adapters.

### P1

- File summaries and authority/sensitivity tags for cheap orientation.
- Structural test-map completeness and fragile-surface queues.
- Enforced graph-WHERE plus KB-WHY workflow receipts.
- Tool schema/projection/documentation consistency checks.
- Replayable release evidence pack.

### P2

- Adapter/route parity matrix analogous to backend parity.
- Risk-selected quality profile packs.
- Hostile MCP input and write-gate fuzzing.
- Optional high-risk mutation profiles after core closure.

## Claim boundary

Estimated gains in accepted outcomes or token economics are roadmap hypotheses.
The port is complete only when its own negative fixtures fail before the fix,
pass after it, and the relevant release gate consumes the result.
