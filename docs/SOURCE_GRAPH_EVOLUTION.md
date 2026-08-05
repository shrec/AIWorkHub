# Source Graph evolution: measured semantic depth

AIWorkHub Source Graph is an operational repository-intelligence layer. Its
success criterion is not the size of the graph or an estimated compression
ratio. A change is useful only when the same coding tasks consume less
provider input, retain or improve verified outcome quality, and preserve exact
source provenance.

## Current delivery status

The original wrapper/economy defects are closed: exact `focus → slice` target
propagation, manager/worker mode parity, metadata-safe byte fitting, compact
hot-symbol references, SHA-only cache reuse, generated-eval exclusions,
incremental stat short-circuiting and extractor-generation refresh all ship on
the canonical repository graph. Exact-symbol slice also excludes unrelated
same-file calls, exact qualnames win retrieval ties, lexical cross-file binding
is language-bounded, and `deps` is distinct from execution `trace`.

Every committed index generation now carries two independently useful health
receipts:

- `index_quality` recomputes resolved/unresolved and cross-language edges,
  artifact entity share, per-language structural density, database bytes and
  freelist ratio directly from canonical SQLite rows. The last 100 generations
  are retained for ratio/density regression detection.
- `recommendation_resolvability` samples high-confidence authorities and
  replays every emitted `recommended_next_steps` and `candidate_files` item
  through the shared manager/worker MCP wrapper. A miss is replayed directly
  against the engine to attribute the defect to `wrapper` or
  `engine_or_emission`.

Both receipts are bound to `build_revision + finished_at`; stale scorecards
are never presented as measurements of the current generation. Operations →
Tool Use renders these structural metrics with an explicit boundary: they are
not provider-token savings or model-correctness claims.

The remaining tracks below are deliberately not labeled complete. A general
Source Graph economy claim still requires the 30-pair raw-vs-graph ledger;
semantic parser depth beyond the optional JavaScript/TypeScript backend and
cross-file resolver precision must advance one fixture-gated language at a
time; deterministic communities remain an optional experiment rather than
source authority.

## Non-negotiable measurement contract

No token-saving multiplier is published from `corpus_bytes / result_bytes`.
That ratio measures retrieval compression, not an agent task.

Every optimization is evaluated with paired runs:

- the same frozen repository revision and task specification;
- the same provider, model, timeout, validation and token-budget policy;
- normal benchmarks are uncapped; a ceiling is allowed only when the owner or
  the frozen benchmark protocol explicitly pre-registers the same exact cap
  for both arms, and capped evidence is reported separately from natural use;
- a fresh isolated worktree and provider session for each arm;
- `raw` arm: bounded file discovery/read tools, Source Graph unavailable;
- `graph` arm: Source Graph available under the normal worker policy;
- provider-reported input/output/cache tokens, never byte-to-token estimates;
- exact raw-read bytes, repeated-read bytes, graph-result bytes and tool calls;
- validation, review, acceptance, rework and timeout outcomes;
- complete raw per-run records retained beside the aggregate.

An aggregate remains `descriptive_only` until at least 30 complete pairs exist.
Any comparison with missing provider usage, mismatched model/budget/revision, or
different validation is excluded with a machine-readable reason. Economy is
reported only beside quality: lower tokens with worse acceptance is not a win.

## Strengthening tracks

### 1. Semantic extractor depth

Current authority is intentionally explicit:

- Python: stdlib semantic AST;
- PHP, C/C++/CUDA, JavaScript/TypeScript, Rust, Go, Java and C#: conservative
  lexical structure;
- other registered families: exact file-level evidence.

The next extractor layer is an optional, cross-platform semantic backend. It
must preserve the dependency-free fallback and report its actual capability
per language and file. No language is advertised as semantic merely because a
parser grammar can load.

The first parser-backed adapter is JavaScript/TypeScript through the optional
`source-graph-semantic` installation extra. It records the active backend and
version in the language capability receipt. If the native wheel is absent or
cannot load, Source Graph stays operational with the existing lexical adapter
and reports that fallback; it never silently labels lexical evidence as a
tree-sitter parse.

The universal VSIX remains dependency-free today. Therefore Marketplace-only
installs use the lexical adapter unless the selected Python runtime already
has the optional package. Shipping native parsers inside platform-specific
VSIX artifacts is a separate release decision and must pass Windows, Linux,
macOS and Remote SSH qualification before it becomes the default.

Acceptance gates per language fixture:

- declaration precision and recall;
- exact line-range accuracy;
- import/include resolution precision;
- call-edge precision, including ambiguous receiver handling;
- malformed/generated-file fail-closed behavior;
- Linux, Windows and macOS packaging tests.

### 2. Cross-file resolution

Resolution is added as deterministic, separately evidenced passes:

1. module/package/import/include identity;
2. declaration-definition reconciliation;
3. alias and configured path mapping;
4. type/member candidates;
5. call target selection.

Every edge remains `EXTRACTED`, `INFERRED` or `AMBIGUOUS`; multiple plausible
targets are never silently collapsed into one. Each resolver exposes precision
fixtures before it may affect `focus`, `trace`, `impact` or review evidence.

### 3. Retrieval and graph traversal

The existing exact/FTS retrieval remains the precision floor. Enhancements are
introduced as explainable scoring components:

- identifier and natural-language query normalization;
- exact-name, prefix, FTS/BM25 and bounded trigram candidate stages;
- edge-aware seed expansion with depth and visit caps;
- induced edges among returned nodes;
- caller-selected context filters;
- response budgets recorded in bytes and provider-observed tokens;
- score decomposition in debug evidence.

The benchmark compares final task outcomes, not only whether a gold symbol was
retrieved. A ranking change is rejected when it increases false candidate reads
or rework despite improving retrieval recall.

### 4. Stable repository communities

Community detection is useful for orientation and unfamiliar repositories, but
it is not source authority. A deterministic local topology pass may expose:

- stable community identifiers tied to index revision;
- representative symbols and files;
- intra/inter-community edges;
- changed-community impact between revisions.

Communities are hints labelled `INFERRED`. They cannot satisfy exact-source,
validation or review gates by themselves. The feature stays optional until the
paired benchmark shows lower discovery reads without quality loss.

## Scope boundary

Documents, PDFs, images and transcripts belong in KB/Manager Context ingestion
with their own provenance. They may link to Source Graph entities, but they do
not become code-call or definition authority. This keeps multimodal knowledge
from weakening exact repository evidence.

## Delivery order

1. Extend the paired raw-vs-graph benchmark ledger to 30 reproducible pairs.
2. Add a live wrapper-path retrieval corpus with precision@k/MRR evidence.
3. Improve semantic extractor backends one language family at a time.
4. Add cross-file resolvers behind measured precision gates.
5. Import runtime coverage only through provenance-bound evidence.
6. Evaluate deterministic communities as an optional orientation layer.

Each step must leave a reproducible evidence bundle. Unmeasured improvement is
recorded as a hypothesis, not a product claim.
