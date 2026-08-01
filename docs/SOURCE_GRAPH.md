# Source Graph

AIWorkHub's Source Graph is a repository-local structural index built under
`.aiworkhub/source_graph/`. It gives managers and workers bounded code evidence
without repeatedly scanning the repository tree. Initialization builds the
first index; the repository daemon refreshes changed files incrementally.

## Query modes

The manager and worker MCP surfaces expose the same six bounded modes:

| Mode | Use |
| --- | --- |
| `focus` | Ranked symbols, hot paths, tests, ownership and churn around a target |
| `slice` | Minimal dependency and call slice needed to change a target |
| `context` | File or symbol structure with neighboring entities and edges |
| `impact` | Bidirectional callers, dependencies and affected files |
| `trace` | Compact incoming/outgoing execution trace with supporting evidence |
| `bundle` | Task-shaped evidence bundle for bugfix, feature, refactor, audit, optimize or explore work |

Every response is row- and byte-bounded. Evidence labels distinguish directly
extracted facts (`EXTRACTED`), deterministic unresolved relations
(`INFERRED`), ambiguous identities (`AMBIGUOUS`) and truthful file-only facts
(`FILE_EVIDENCE`). Empty, stale and unsupported results remain explicit.

## Language coverage

AIWorkHub registers 33 language families. Semantic adapters currently extract
modules, imports, declarations, functions or methods, inheritance and observed
calls for:

- Python through the standard-library AST;
- PHP through a conservative lexical adapter;
- C, C++, CUDA, OpenCL and Metal through a C-family lexical adapter;
- JavaScript and TypeScript (including JSX/TSX);
- Rust, Go, Java and C#.

The remaining registered families, including JSON, XML, YAML and TOML, are
indexed with truthful path/language/size/hash evidence instead of fabricated
symbols. Generated trees, dependency caches and build outputs are excluded by
the repository ignore policy before indexing.

## Agent evidence

Ranked results combine deterministic structural signals with repository-local
history captured during indexing: callers and callees, related tests, TODO or
risk markers, churn and ownership. Git history is never invoked at query time.
Ambiguous cross-file targets are left unresolved rather than attached to an
arbitrary symbol.

For code tasks, Source Graph usage is continuous rather than a one-time prompt
injection. The authenticated tool-use ledger records query stage, hits, bytes,
latency, generation and any bounded fallback reason. Review can therefore
distinguish continuous graph use from injected-only context and unsupported
targets.

## Repository isolation

The database, generations and settings belong to the selected repository's
`.aiworkhub/` directory. No global graph database is shared across workspaces.
Changing repository binding changes the graph authority atomically with the
task, callback, session, memory and KB authorities.
