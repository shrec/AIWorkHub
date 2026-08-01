# AIWorkHub Quality Control

AIWorkHub treats a worker result as a candidate, never as proof that the work
is correct. Acceptance belongs to a verified manager and is based on bounded,
reproducible evidence from the exact repository, task, claim episode, inputs,
workspace and changed-path identities.

## Quality objective

The system optimizes for two error rates, not for the number of green checks:

- **False green:** incorrect or unsafe work is accepted.
- **False red:** correct work is rejected or repeatedly reworked.

The first is the primary safety risk. The second is a real cost and throughput
risk. Every new gate therefore needs both a positive fixture that remains green
and a targeted negative fixture that the gate must reject.

## Acceptance pipeline

Quality is controlled by five independent layers. A later layer may strengthen
an earlier one, but it may not turn missing or failed evidence into a pass.

1. **Frozen task contract**
   - Exact repository/task/runner/topic/claim identity.
   - Explicit success criteria, allowed writes, required outputs, validation,
     immutable inputs and dependencies.
   - Source Graph and context receipts when policy requires them.

2. **Worker isolation and self-validation**
   - Work is produced in an isolated, scope-constrained worktree.
   - The worker runs the card's exact validations and records artifacts, tool
     use and terminal truth.
   - Worker self-reports are evidence only; they never own PASS/FAIL.

3. **Deterministic quality floor**
   - Syntax/static parsing, exact scope and output checks, input/output hashes,
     repository-declared test/build/lint/type/security checks and destructive
     diff detection.
   - Required unavailable checks fail closed. `not_available` is never
     converted to `passed`.
   - Mechanical checks use argv arrays with `shell=False`; configuration is
     non-executable repository data.

### Change-sensitive Known Bug Scanner

The dependency-free Known Bug Scanner runs only against declared changed
paths. Its initial rule packs cover C/C++/CUDA, Python, JavaScript/TypeScript,
Go, Java/Kotlin, C#, PHP and crypto-sensitive code. High-confidence defects
such as disabled TLS verification, literal divide-by-zero, unsafe shell mode,
weak cryptographic RNG use and exact CUDA rotation-claim mismatch block the
deterministic floor. Lower-confidence lifetime, raw-pointer, unsafe-copy,
dynamic-code and shell-boundary findings remain warnings for manager review.

Every finding includes rule identity, severity, category, exact path/line,
bounded snippet and stable fingerprint. Source Graph risk modes are broader
candidate discovery; they never silently promote a heuristic into this
blocking gate.

4. **Independent judgment lenses**
   - A read-only reviewer, preferably a different provider for high-risk work,
     examines correctness, code quality and security without seeing the
     worker's rationale or being allowed to mutate the repository.
   - Findings must name a lens, severity, path/evidence and a falsifiable
     reason. Reviewer prose never directly computes the final verdict.

5. **Manager acceptance and integration proof**
   - The verified manager re-reads current canonical inputs, re-runs required
     checks, verifies changed-path hashes and promotes only the exact approved
     delta.
   - A combined-tree differential must validate the candidate together with
     every accepted dependency and concurrent change before final acceptance.
   - Acceptance records the deterministic verdict, reviewer disposition,
     rollback identity and complete bounded evidence bundle.

## Six canonical lenses

Each lens is a yes/no claim with a concrete way to falsify it. A numeric score
may summarize history, but may not replace the blocking verdict.

| Lens | Claim | Deterministic floor | Judgment residual |
| --- | --- | --- | --- |
| Correctness | Every success criterion and important boundary behaves correctly. | Exact validations, test collection, regression/differential checks. | Subtle logic and domain errors. |
| Does it run | The changed system builds and executes in the supported environment. | Exit status, timeout, collection and platform qualification. | None for the observed environment. |
| Test adequacy | Tests exercise the changed behavior and at least one relevant failure/boundary path. | Changed-test collection, change-sensitive coverage and risk-based mutation/revert-to-red probes. | Whether assertions truly capture intent. |
| Security | No unsafe input-to-sink flow, secret, traversal, injection or authority bypass is introduced. | Declared SAST/secret/dependency/memory-safety checks plus exact authority guards. | Novel or semantic vulnerabilities. |
| Code quality | The change is maintainable, cohesive and introduces no unjustified abstraction or dead path. | Lint, type checks, API-loss and repository hygiene checks. | Architectural clarity and long-term design cost. |
| Requirements and scope | Every frozen criterion is addressed and no undeclared path/behavior is changed. | Allowed-write enforcement, required outputs, immutable inputs and criteria evidence. | Semantic completeness of the implementation. |

## Risk-proportional profiles

The quality floor is universal; expensive checks are proportional to risk.

- **Low:** deterministic floor, focused tests, exact manager revalidation.
- **Medium:** low profile plus independent correctness review, change-sensitive
  coverage and combined-tree differential.
- **High:** medium profile plus cross-provider correctness/security review,
  required SAST/secret/dependency gates, mutation or revert-to-red probes where
  supported, full dependency-union validation and explicit human approval.
- **Critical/release:** high profile plus supported-platform matrix,
  reproducible artifacts/checksums, fresh-install E2E and rollback rehearsal.

Risk is derived from touched authority boundaries, destructive change,
security-sensitive sinks, public API/schema/storage migrations, concurrency,
release scope and task declarations. A worker cannot lower its own risk.

## Gate trustworthiness

AIWorkHub must test its gates, not only the code behind them:

- A committed negative-fixture matrix contains one known-good candidate and
  deliberately broken candidates for every blocking predicate.
- The reference implementation must pass; every targeted broken variant must
  fail for the expected reason.
- Benchmarks track false-green rate, false-red rate, unavailable-evidence rate,
  reviewer disagreement, rework yield and post-accept regression escapes.
- A new predicate is not blocking until its positive and negative fixtures are
  stable on Linux, Windows, macOS and the Remote-SSH contract where relevant.
- Historical calibration may tune reviewer/model selection, but never rewrite
  old evidence or silently weaken a repository policy.

The shipped `aiworkhub_quality_calibration_report` runs this fold-only matrix
without repository or model side effects. Its current baseline is 26 cases
(3 positive, 23 targeted negative), with reviewer overflow treated as a
blocking schema error rather than silently truncated. Cross-platform CI runs
the same matrix so a platform-specific false green or false red blocks release.

## What we adopt from kimi-atlas

The useful transferable mechanisms are its falsifiable named lenses, pure
deterministic final gate, isolated adversarial critics, negative-gate fixture
matrix, combined-tree differential, bounded refinement and explicit honest
limits. AIWorkHub adapts these ideas to its repository-native multi-provider
authority model; it does not copy Kimi-specific skills, prompts or runtime
state machinery.

Sources:

- [kimi-atlas quality overview](https://github.com/null0xxx/kimi-atlas#how-atlas-guarantees-quality)
- [kimi-atlas six-lens rubric](https://github.com/null0xxx/kimi-atlas/blob/main/references/rubric.md)
- [kimi-atlas developing gate](https://github.com/null0xxx/kimi-atlas#developing)

## Honest limits

No gate proves semantic correctness in the general case. Tests can omit an
interaction, static analysis can miss a novel vulnerability, and model critics
can share blind spots. AIWorkHub therefore reports exactly which layers ran,
which were unavailable, and what remains judgment-based. A green verdict means
the declared and observed evidence passed; it is never described as a proof of
all possible behavior.
