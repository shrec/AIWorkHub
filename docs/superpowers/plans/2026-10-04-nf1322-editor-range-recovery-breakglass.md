# NF-2026-01322: authenticated same-run range repair

This is the smallest owner-authorized self-hosting break-glass restoration of
the Task MCP editor-worker transport, not ordinary feature development.

Current NF1319 request `e6d5f0d4ccda4b76bb49cef809b77102`, claim 8, retained
seal `7bc5ffa8e56fde65214b011155226081a7792a075b79eaeb106f2989672eab18`
failed Python collection. Sanitized tool log sequence 56 supplied an incorrectly
eight-space outer `elif` at learning_commit_store.py:1004; the bridge preserved
that input. Sequence 60 supplied the correct four-space replacement, but sequence
61 rejected it as `semantic_edit_stage_rejected:range_conflict`. The worker had
already identified the correction; a same-run repair was blocked by staging.
The correct repair is dedenting the `elif`, not further indenting its `raise`:
the inconclusive-outcome guard must remain a sibling of accepted/rejected guards.

The production correction keeps refusing a changed previously staged range.
Only a previously authenticated applied exact range in a worker request, outside
an original-coordinate batch and without unsupported newline semantics, opens
the existing corrective recovery path. It requires a fresh bounded Source Graph
body with the current disk hash, then the native worker semantic prepare/apply
pair. No mutation occurs at the rejected stage. Scope, current preimage, target,
receipt and range checks remain authoritative; partial overlaps, unauthenticated
conflicts and conflicting original-coordinate batches remain denied.

The new regression was red on the original production implementation with
`same-range correction must open fresh-pair recovery`. After the correction the
complete GLM bridge harness passes, including no-mutation refusal, no prepare
before fresh Source Graph, exact corrected content, unrelated-target refusal,
recovery revocation, batch/overlap/newline negatives and unchanged existing
unauthenticated-conflict tests. Its provider is deterministic, not a live-model
or genuine sandbox qualification receipt. Python invariant/size gates passed
55 tests. All existing-file edits use authenticated manager prepare/apply.

The baseline full Python qualification on commit 3d5d170 passed 15,835 tests,
with 445 skips, three subtests and two warnings. Fresh serial mux qualification
passed 70 tests with two skips. Neither establishes unfinished live features.

NF1305 current fa42b406e05d4a629f06165d08badf60 and NF1307 current
645403735eb94833b126ad176f9e1d4e reviews were both closed through native reject
to pending with their exact sealed candidate findings. Their fixes are not
included or represented as accepted by this restoration. No drives, grants,
owner configuration or sandbox authority were changed. No Claude was launched.

Native custom-card creation was denied by contract validation (including the
reserved coordinator runner); no worker task or acceptance receipt is fabricated.
The measured restoration is tracked by native NeedFix NF-2026-01322 and session
checkpoints. Release 0.12.24 packages this replacement; build, installation,
activation and genuine worker replay are separate receipts. After installing
the replacement, return to canonical Task MCP flow. Full Manager Chat M1,
OpenCode/Muse sandboxed MCP response, shared cost/context policy and skills
accepted/actor integration remain unfinished.
