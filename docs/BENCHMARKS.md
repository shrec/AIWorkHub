# AIWorkHub Benchmarks

AIWorkHub publishes measured evidence only when its population, denominator and
limitations are explicit. Structural byte ratios, provider tokens, elapsed
time, cost and accepted quality are separate measurements; one is never used as
a substitute for another.

## Semantic-edit pilot

The first checked-in pilot contains two paired Codex GPT-5.5 runs on small
docstring edits to existing Python files. Both variants reached the worker
`review_ready` state. This is not the same as an explicit manager acceptance
decision.

| Observed aggregate, n=2 pairs | Focused workflow | Direct-read baseline | Observed delta |
| --- | ---: | ---: | ---: |
| Input tokens | 254,194 | 350,954 | -27.571% |
| Output tokens | 2,190 | 2,918 | -24.949% |
| Total tokens | 256,384 | 353,872 | -27.549% |
| Elapsed wall time | 73.117 s | 93.186 s | -21.537% |
| Uncached input tokens | 50,162 | 41,706 | **+20.275%** |

The uncached result moved in the opposite direction because cache composition
differed between variants. The sample is not randomized or order-balanced, one
focused run predates authenticated semantic-edit receipts, cost was unavailable,
and only one model/task family was measured. Therefore this pilot is
**not eligible for a public savings multiplier or causal quality claim**.

The machine-readable evidence is
[`benchmarks/semantic-edit-pilot-v1.json`](../benchmarks/semantic-edit-pilot-v1.json).
CI recomputes every total and percentage with
[`scripts/check_semantic_edit_benchmark.py`](../scripts/check_semantic_edit_benchmark.py).

## Live structural telemetry

The Operations KPI view separately aggregates authenticated semantic-edit byte
receipts: existing file bytes, selected old-region bytes, replacement bytes and
old bytes re-emitted by the model. A ratio such as `file_bytes /
replacement_bytes` describes output shape only. It is not a token, cost,
latency or quality estimate.

## Promotion gate for a public benchmark claim

A result can move from pilot evidence to a product claim only after:

1. a preregistered task population and frozen pair protocol;
2. randomized or order-balanced variants across multiple task families;
3. identical model, adapter, context budget and validation contract per pair;
4. authenticated semantic-edit/read receipts and provider usage;
5. explicit manager acceptance or an equivalent blind quality adjudication;
6. cache-aware reporting, failure/retry accounting and confidence intervals;
7. enough independent pairs to report a stable distribution, not only a mean.

Until then, AIWorkHub reports the observed sample and its limitations verbatim.
