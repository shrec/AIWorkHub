# AIWorkHub production defect ledger — 2026-08-02

Status: confirmed from live GeoAI orchestration, ready for implementation.

This ledger records defects observed while AIWorkHub coordinated the B1439
five-shard Georgian representation annotation wave. It is an implementation
handoff, not a speculative feature list. GeoAI task state remains in GeoAI's
own `.aiworkhub/` store; no task cards or canonical context databases are
copied into this repository.

## P0 — review and residual-rework integrity

### 1. Rejected review artifact is garbage-collected before rework

`reject-review -> pending` immediately ran workspace GC and removed the
retained reviewed worktree. The next isolated worktree contained an empty
allowed-output file instead of the previously reviewed 64-row artifact.

Observed evidence:

- each reject scanned roughly 76–78 retained worktrees;
- the prior reviewed artifact disappeared before successor launch;
- recovery was possible only by parsing the durable process stdout log;
- a worker correctly reported that it could not preserve the other 57 rows
  because its output path was empty.

Required invariant: keep the exact reviewed generation, artifact hash and
artifact bytes pinned until a replacement generation is accepted, rejected,
archived or explicitly superseded.

### 2. Rework has no exact residual identity contract

The task card can say “change only rows X/Y/Z”, but the isolated workspace and
acceptance gate do not encode or enforce that constraint. In the first rework
attempt, four workers regenerated 64/64 row objects even though only 3–9 rows
were rejected. Mechanical validation still passed.

Required implementation:

- add typed `residual_identities` (row IDs, paths/symbols or bounded JSON
  pointers) to the rework generation;
- pin the prior reviewed artifact as an immutable rework input;
- record hashes for every non-residual object/path;
- make `accept_review` fail if any non-residual hash changes;
- surface the unexpected identities in bounded review evidence.

Acceptance test: a 64-row artifact with a three-row residual must accept when
and only when those three row objects change. A fourth changed object must fail
without modifying the canonical repository.

### 3. Prior output is not materialized into the successor worktree

A rework launch receives a fresh empty allowed-write path. It does not receive
the prior reviewed output as a declared input, even when residual-only review
depends on it.

Required invariant: successor worktree creation atomically materializes the
exact pinned predecessor artifact read-only, then creates the editable
candidate from that baseline. The predecessor identity and hash must appear in
the Task MCP receipt and completion evidence.

### 4. Required and forbidden inputs can contradict each other

The B1439 cards required annotation from the HPLT JSONL source and B1344
protocol, while the same paths were also present in `forbidden`. Task creation
and launch preflight accepted the contradictory card.

Required invariant: task creation fails closed when an objective,
validation command, required output derivation or declared input requires a
path that is also forbidden. Return the exact conflicting paths and fields.

## P1 — Source Graph, bounded output and lifecycle latency

### 5. Source Graph rejects canonical JSON/JSONL task inputs

Worker `file` queries for the exact canonical HPLT `.jsonl` source and `.json`
protocol returned `target_not_allowed` (or zero hits). This makes the mandatory
Source Graph gate unusable for data-classification tasks even though the
inputs are repository-local and explicitly named.

Required implementation:

- support configured JSON, JSONL, XML and other enabled data languages;
- distinguish source-code indexing from declared task-input access;
- allow an exact declared-input query without broad repository discovery;
- include an explicit unsupported/unindexed reason and safe exact-read receipt
  when fallback is necessary.

### 6. `collect_result(max_log_bytes=...)` is not actually bounded

Small `max_log_bytes` requests still returned very large payloads because
`task_card`, `card_json`, `terminal_review` and evidence recursively contained
overlapping copies of the same card. Responses reached tens of thousands of
tokens and were truncated by the outer transport.

Required implementation:

- return a projection, not the recursively embedded raw card;
- bound stdout/stderr, validation evidence and card metadata independently;
- replace repeated nested documents with stable IDs/hashes;
- expose an explicit `truncated_fields` list and retrieval cursor/tool.

### 7. Review transitions synchronously scan most retained worktrees

Individual `reject-review` calls took about 25–40 seconds. Multiple independent
rejections serialized and exceeded a minute because each transition invoked a
broad workspace-GC scan.

Required implementation:

- remove broad GC from the critical review-transition transaction;
- enqueue bounded asynchronous cleanup after the new generation is pinned;
- scan only generations made eligible by the current transition;
- expose transition time, lock wait and GC time separately.

### 8. `validation_failed` needs another expensive review rejection

A rework that could not produce an output entered `validation_failed` while
remaining in review. Relaunch required a second full `reject-review -> pending`
transition and its GC cost.

Required implementation: provide an explicit coordinator rework disposition
for terminal validation failure that preserves evidence and pinned baselines
without pretending the task is review-ready or rerunning broad GC.

## P1 — canonical context write reliability

### 9. AI Memory manager write returns opaque `IntegrityError`

After the Session Manager handoff succeeded, the corresponding
`aiworkhub_manager_ai_memory_write(action=remember)` failed with:

`context_write_failed:IntegrityError`

No constraint name, conflicting key, idempotency state or safe recovery action
was returned.

Required implementation:

- diagnose the canonical memory schema/write path;
- make `remember` idempotent under its idempotency key;
- return structured, redacted constraint and recovery diagnostics;
- add repository/session/task/provider binding tests and duplicate-key tests.

## Reproduction summary

Wave: GeoAI B1439 A–E, five isolated GPT-5.5 workers, 64 rows each.

- initial mechanical validation: 320/320 rows passed;
- coordinator semantic residual: 32/320 rows;
- first residual rework: predecessor worktrees were gone;
- one worker failed because the baseline was empty;
- four workers regenerated all 64 objects;
- coordinator recovered and pinned prior artifacts from durable stdout before
  a later bounded rework could succeed.

This proves the safety gap is independent of model quality: the system did not
make the requested residual-only operation mechanically enforceable.

## GeoAI pause-state handoff

At the time this ledger was written:

- B1439 A, B and E were accepted/done;
- B1439 C was pending with only row 1215 evidence-span repair remaining;
- B1439 D was pending with only rows 1428, 1442, 1443 and 1462
  `negative_reason` repairs remaining;
- GeoAI commits preserving the work are `04c342998`, `56059e1fb` and
  `fa4c00cc9`;
- the owner's pre-existing dirty B1341 ELTEC authority manifest was not
  modified or committed by this work.

## Definition of done

The defect set is closed only when focused unit/integration tests prove all of
the following on Linux, Windows and macOS:

1. rejected artifacts survive until successor disposition;
2. exact residual changes pass and any extra change fails;
3. required/forbidden input contradictions are rejected at creation;
4. declared JSON/JSONL inputs have a bounded Source Graph or explicit safe
   fallback receipt;
5. `collect_result` honors byte/token bounds without recursive duplication;
6. review transition latency is bounded and GC is outside the critical path;
7. AI Memory writes are idempotent and return actionable structured errors.
