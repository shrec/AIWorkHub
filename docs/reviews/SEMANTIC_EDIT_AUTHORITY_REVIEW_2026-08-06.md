# Semantic edit authority review

Source: owner-supplied architecture review, attachment
`e18ca550-45f6-410d-9970-61aa729b8173`, received 2026-08-06.

## Core conclusion

Source Graph plus structured semantic edits is an authority boundary, not only
a compression technique. The model proposes a bounded replacement; a
deterministic replay layer retains write authority and verifies path, current
hash, range, parseability, scope, and atomic application.

This removes whole-file preservation and unrelated-code authority from the
model, reduces blast radius and merge noise, protects dirty worktrees, improves
review clarity, and permits precise residual rework.

## Target protocol

1. `semantic_edit_prepare` selects a graph-grounded boundary, relevant context,
   callers/callees, tests, and invariants.
2. The worker returns structured replacement ranges or creates only.
3. `semantic_edit_replay` validates hashes and applies atomically.
4. `semantic_edit_validate` checks syntax/AST, scope, formatting, impact-owned
   tests, and behavioral evidence.
5. `semantic_edit_record` emits bounded outcome telemetry.

Whole-file rewriting should be forbidden by default when a change fits within
one or a small transaction of semantic boundaries. Multi-boundary edits require
hash-bound transaction semantics; architecture-wide changes require impact
analysis and manager approval.

## Claim boundary

Bounded application proves edit scope and byte preservation. It does not by
itself prove behavioral correctness or a provider-token multiplier.
