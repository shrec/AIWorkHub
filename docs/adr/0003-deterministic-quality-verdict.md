# ADR 0003: Deterministic quality verdict and independent review

- Status: Accepted
- Date: 2026-07-30

## Context

A coding worker can write code, tests and a persuasive completion report while
still missing the defect it created. Running more models does not by itself
create independence when they share prompts, context or blind spots. AIWorkHub
already verifies exact scope, hashes, required outputs, validations and
destructive diffs, but its read-only quality-reviewer contract is not yet a
complete acceptance authority and concurrent accepted changes can introduce a
combined-tree regression that was absent in each isolated worktree.

## Decision

AIWorkHub will use six falsifiable quality lenses: correctness, does-it-run,
test adequacy, security, code quality, and requirements/scope. Mechanical tools
and model reviewers emit normalized findings, but a pure deterministic fold
owns the final verdict. A worker and a reviewer cannot promote code.

The universal deterministic floor remains mandatory. Additional review and
verification are selected from a risk profile that a worker cannot lower.
Medium and higher code tasks require a combined-tree differential before final
acceptance. High-risk work uses an isolated cross-provider reviewer when an
independent provider is available; unavailable required evidence fails closed.

Every blocking predicate requires a known-good fixture and a deliberately
broken fixture. Gate calibration reports both false greens and false reds.

## Consequences

- A passing worker test run is necessary evidence, not an acceptance verdict.
- Reviewer prose cannot silently override deterministic failures.
- Expensive mutation, security, platform and cross-provider checks are spent
  where risk justifies them rather than on every mechanical task.
- Integration quality is measured on the dependency/concurrency union, not
  inferred from isolated greens.
- The dashboard and evidence bundle must distinguish deterministic proof,
  reviewer judgment, unavailable checks and residual risk.

## Validation

The closure gate requires a pure verdict implementation, risk-profile schema,
negative-fixture matrix, combined-tree regression tests, reviewer isolation
tests, evidence-bundle/dashboard projection and Linux/Windows/macOS/Remote-SSH
qualification for applicable predicates.
