# Product and architecture assessment

Source: owner-supplied assessment, attachment
`0411f04b-daf8-4eb0-bc10-d193378ddd5c`, received 2026-08-06.

## Assessment

AIWorkHub is already a strong foundation for an autonomous software-engineering
control plane, but it is not yet a fully unattended “launch and trust” system.
Its strongest shipped properties are repository authority, task and review
lifecycle, Source Graph context, multi-model control, durable continuity, and
evidence-oriented acceptance.

The assessment's central conclusion is that the next bottleneck is not another
provider or dashboard feature. It is truth quality: distinguishing a claimed
result from a tested, reproduced, fixed, and independently verified result.

## Recommended work

1. Introduce a common evidence ladder: `claimed`, `static_evidence`, `tested`,
   `reproduced`, `fixed_and_verified`, and `inconclusive`.
2. Reject placeholders, generic summaries, and content-free `review_ready`
   results with a meaningful-output gate.
3. Define task-type behavioral gates for bug fixes, refactors, performance,
   security, and data/ML tasks.
4. Standardize each attempt into durable artifacts: attempt metadata, diff,
   validation, usage, review, and SARIF when applicable.
5. Isolate provider/VS Code LM failure circuits from the MCP control plane.
6. Rank routes by measured cost per accepted outcome, initially in advisory
   and shadow modes.

## Claim boundary

The original numerical maturity ratings and projected automation percentages
are expert judgments, not benchmark results. They must not be published as
measured product performance.
