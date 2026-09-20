# Source Graph LSP batch-resolution plan

**Goal:** Add bounded, reproducible language-server resolution of missing in-repository Python and JavaScript/TypeScript call edges during Source Graph index construction. A higher raw resolved ratio is not the goal; correct `calls` and `impact` answers are.

**Spec:** `docs/superpowers/specs/2026-09-20-model-neutral-sdlc-design.md`.

**Measured baseline:** The prior 715-definition Pyright spike classified a 300-edge random unresolved sample as 74.3% stdlib/typeshed and 16.0% in-repository, with 99.2% in-repository control precision on 120 already-resolved edges. These are sampling estimates, not a claim that an LSP backend is installed. The present graph stores Python `source_col`, while the JS/TS semantic extractor emits call edges without it. `source_graph.py:_build_index_locked` currently performs the lexical/AST resolver inside the SQLite merge transaction. Do not launch an LSP subprocess while that transaction is open. `NF-2026-00864-r1` owns the retrieval-eval production call site until its manager decision.

The protocol baseline is the official [LSP 3.17 specification](https://github.com/Microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/specification.md). The Python server candidate is [Pyright](https://github.com/microsoft/pyright/blob/main/docs/installation.md); the JS/TS candidate is [typescript-language-server](https://github.com/typescript-language-server/typescript-language-server/blob/master/README.md). Server commands and versions are explicit configuration, not silently downloaded during an index refresh. Missing servers yield typed UNKNOWN evidence.

## Task 1 — exact JS/TS call-site coordinates

**Write scope:** `src/aiworkhub/source_graph_semantic.py`, `tests/test_source_graph_tree_sitter_semantic.py`, plus an exact extractor regression file if needed. This does not overlap the active NF864 worker.

1. TDD: a fixture with two calls to the same property on one line, a multibyte prefix, an aliased import, and a locally shadowed name must preserve distinct name-token byte positions. The indexed edge's `source_col` must point to the called identifier, not the receiver or opening parenthesis. Existing Python coordinate semantics stay unchanged.
2. Extend the JS/TS semantic extraction to emit an exact source column for each call. Determine whether its persisted unit is UTF-8 byte offset or Unicode scalar position and make that explicit in the adapter contract; convert to LSP's negotiated position encoding at the boundary. No guessed column on an unsupported parser fallback.
3. Run the extractor and Source Graph JS/TS tests. Preserve deterministic edge identities and avoid duplicate rows.

## Task 2 — bounded LSP transport and classification

**Write scope:** new `src/aiworkhub/source_graph_lsp.py`, new `tests/test_source_graph_lsp.py`. No production index writes in this task.

1. TDD a fake LSP subprocess that exercises `initialize`, `initialized`, `textDocument/didOpen`, `textDocument/definition`, response IDs, `Location` and `LocationLink`, `shutdown`/`exit`, ordinary notifications between responses, malformed framing, timeout, cancellation, and child cleanup. Do not mistake diagnostic notifications for a definition response.
2. Create only a bounded private workspace/configuration from the indexed source set. Exclude `.aiworkhub`, nested worktrees, generated storage, `node_modules` and the graph database; do not aim Pyright at the repository root that contains 11 GB of `.aiworkhub` state. Absolute include paths in Pyright config are not trusted; fixture-test the exact workspace shape.
3. Every proposed result carries request source path/hash/line/column, target URI/range, server command/version/config digest, and a typed classification: `repo_internal`, `external_stdlib`, `external_dependency`, `unresolved`, `ambiguous`, or `server_unavailable`. Reject paths escaping the verified repository, symlink escapes, stale source hashes, multi-target ambiguity, and unbounded output. External targets are evidence, not resolved in-repo edges.
4. Bound process count from observed cores with headroom, per-request time, total batch time, input/output bytes, and file count. Sorting and hashing make the same inputs produce identical result records regardless of completion order. No network installation or global editor config mutation.

## Task 3 — index integration and durable provenance

**Depends on:** Tasks 1–2 and NF864 manager decision. **Write scope:** `src/aiworkhub/source_graph.py`, new/targeted Source Graph tests. `src/aiworkhub/source_graph_lsp.py` only if the integration reveals a documented transport contract defect.

1. Run existing lexical/AST resolution first. Build a bounded plan for unresolved, indexed Python/JS/TS call edges with authenticated source hashes and exact call-site coordinates. No LSP subprocess runs inside the SQLite write transaction. A true no-op incremental refresh reuses matching server/version/config/tree receipts rather than reopening every document.
2. Match one internal LSP definition to one canonical entity by exact repository path and declaration range; retain unresolved/ambiguous on mismatch. Never mutate an already-resolved lexical edge merely to improve a score. Persist edge-to-target provenance including source/target hashes and server/config version in a migration-safe table. Invalidated files or changed server/config revoke stale enrichment.
3. Publish typed health: attempted, skipped, external, internal, ambiguous, unavailable, stale, and latency with denominators. Do not collapse absent server or zero query coverage into `degraded:false`/green. Callers and impact queries consume only verified canonical target identities.
4. Test full build, incremental changed target and caller, deletion, concurrent reader, missing server, stale result, symlink escape, Python and JS/TS fixtures, and a C++ include-root/PCH negative control that remains unaffected.

## Task 4 — qualification and gate

Use the production `aiworkhub_source_graph_retrieval_eval` path after NF864 is accepted. Add deterministic Python and JS/TS `calls`/`impact` cases, plus external-library controls. Compare before/after answers on the same indexed revision; report in-repo precision and recall, false-zero count, latency, server availability and sample coverage. Fail the gate if the answer is query-ignoring or false-zero despite a higher raw resolved ratio. A live refresh on this repository and a separate fixture repository must prove repo identity and bounded workspace behavior. Only then mark LSP-backed Source Graph complete.

## Manager review checkpoints

- Task 1 may be launched in parallel with NF864 because files do not overlap.
- Task 2 may be launched in parallel with Task 1 only if its adapter contract does not depend on the final coordinate representation; otherwise sequence it.
- Task 3 must wait until NF864 is accepted/rejected and its `source_graph.py` scope is free.
- For each candidate, run exact card validation, inspect the changed code and receipts, accept or return with precise findings in the same review turn. Do not call a test-only adapter a deployed LSP feature.
