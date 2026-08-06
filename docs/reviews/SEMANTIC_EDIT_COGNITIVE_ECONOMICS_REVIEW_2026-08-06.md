# Semantic edit cognitive and economic review

Source: owner-supplied assessment, attachment
`fd9c6ffa-628a-45fe-b6ca-61c3a6d8908f`, received 2026-08-06.

## Hypothesis

Whole-file editing makes a model identify the relevant region, solve the local
problem, and regenerate the unrelated remainder exactly. The third obligation
is not useful reasoning, but consumes attention, output, time, and error
surface. Graph-narrowed semantic replay removes that obligation.

Expected direct effects are smaller relevant input and smaller replacement
output. Expected indirect effects are less unrelated churn, fewer failed
applies, less rework, smaller reviewer context, fewer conflicts, and lower human
correction. These indirect effects are plausible but require paired outcome
measurement.

## Required A/B benchmark

Compare whole-file editing with Source Graph capsule plus deterministic replay
on matched tasks and identical model/adapter settings. Record input, cached
input, output and reasoning tokens; elapsed time; apply failures; unrelated
diff lines; first-pass validation; reviewer findings; rework count; manager
acceptance; and cost/time per accepted outcome.

Include single-function fixes, import-plus-function changes, class methods,
multi-caller fixes, tests, and high-churn files. Use graph-informed capsules so
narrowing does not omit caller invariants or hidden dependencies.

No token, cost, speed, or quality multiplier is eligible for public use until
the paired artifact and checker reproduce it.
