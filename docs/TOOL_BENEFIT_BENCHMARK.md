# Tool Benefit Benchmark

This protocol measures AIWorkHub's actual effect on worker outcomes and cost.
It does not convert bytes to tokens, infer hidden provider usage, or publish a
multiplier before matched observations exist.

## Arms

| Arm | Available context/tools | Purpose |
| --- | --- | --- |
| A0 | No AIWorkHub context injection or AIWorkHub worker tools | Benchmark-only control |
| A1 | Source Graph injection and worker queries only | Isolate code-navigation benefit |
| A2 | Session Manager, AI Memory and KB only | Optional durable-context isolation |
| A3 | Normal production AIWorkHub policy | Measure the complete system |

A0 is created by the benchmark harness's explicit tool registry and prompt
assembly. It is never a production setting and does not weaken the production
fail-closed policy. The harness must prove each arm from its delivered-context
receipt and observed tool-call ledger; an environment variable or task label
alone is not evidence that an arm was isolated.

## Matched unit

One matched unit pins all of the following before random assignment:

- repository, immutable revision and isolated-worktree baseline;
- task/fixture identity and SHA-256 of its exact prompt and declared inputs;
- model, provider, adapter and adapter revision;
- timeout and live-token/output budgets;
- validation commands and expected behavioral evidence;
- repeat ID and randomized execution-order seed.

Only the arm/tool registry may differ inside a matched unit. A comparison is
called *matched* only when the report contains the exact paired run IDs. Arm
aggregates without paired identities remain descriptive aggregates.

## Fixtures

Use small real repair, audit and navigation tasks that have deterministic
validators and bounded write scopes. Pin fixture identities from the current
target repository immediately before the run. Do not invent or carry fixture
paths between AIWorkHub, GeoAI or another repository. Start with at least:

1. exact-symbol/local repair;
2. cross-file call/impact repair;
3. test-ownership discovery and repair;
4. read-only architectural audit with a checkable answer key.

Pilot with three randomized matched repeats per fixture and arm. Increase the
sample only after the harness, denominators and provider telemetry are shown
to be stable.

## Source Graph modes

A1 and A3 begin with `focus` or `slice`. Use `context`, `calls`, `trace`,
`impact`, `testmap`, `coverage` or `bundle` only when the preceding evidence
requires it. Record the ordered mode sequence, hits, zero hits, bytes returned,
latency, cache status and index revision.

A0 has no Source Graph mode. It is paired to the same fixture and budget, not
crossed with a tool it deliberately lacks.

## Per-attempt record

Record raw observations and explicit denominators:

- terminal state, validator result, manager accept/reject and rework count;
- wall time and timeout/cancellation state;
- provider-reported input, cached-input, cache-creation and output tokens;
- whether each token/cost field is known, unknown or posthoc-only;
- tool calls by tool and mode, success/zero-hit/error, bytes and latency;
- total file reads, bounded reads, unbounded reads, exact unchanged rereads,
  overlapping unchanged rereads and unknown-hash repetitions;
- delivered-context receipt and worker acknowledgement;
- exact matched-run identity.

`unbounded_read_share = unbounded_reads / total_reads`.
`exact_reread_share = exact_unchanged_rereads / total_reads`.
These are distinct measures. Missing hashes are unknown, never unchanged
rereads. A read following Source Graph inside a bounded window is a temporal
association only, not proof that Source Graph caused or prevented the read.

## Denominators and statistics

- Use intention-to-treat outcome denominators: launch failures, timeouts and
  validation failures stay in the arm's terminal-outcome denominator.
- Report a separate per-protocol view only when the exclusion rule was frozen
  before execution; never replace the intention-to-treat result with it.
- Report Wilson 95% intervals for binary outcomes.
- Report median, minimum and maximum for continuous outcomes. Bootstrap
  intervals require at least 20 matched units per arm.
- Report known and unknown denominators separately for every token/cost metric.
  Cache-only reports count as observed cache telemetry; absent fields do not
  become zero.
- Structural zero is valid only when the arm contract proves a tool was
  disabled. No observed calls without such proof means unknown, not zero.

## Abort and contamination rules

Discard a matched unit and rerun it with the same pinned identity when:

- an A0 run receives injected AIWorkHub context or executes an AIWorkHub tool;
- repository revision, fixture bytes, model, adapter or budget drifts;
- one arm can observe another arm's candidate/worktree;
- the validator or measurement ledger is missing/corrupt;
- the task contract differs between arms beyond the declared tool registry.

Timeouts are recorded outcomes, not contamination and not automatic
denominator exclusions.

## Claim rule

Before matched data exists, report only the protocol and historical
observations. After execution, publish the raw run ledger, paired identities,
known/unknown denominators and uncertainty intervals. A statement such as
"10x" or "71x" is allowed only if the matched estimator directly supports it;
otherwise say that no causal multiplier has been established.

The current checked-in preliminary result and its explicit non-claim status are
documented in [AIWorkHub Benchmarks](BENCHMARKS.md). The raw pilot ledger is
machine checked in CI; it does not yet satisfy this protocol's public claim
gate.
