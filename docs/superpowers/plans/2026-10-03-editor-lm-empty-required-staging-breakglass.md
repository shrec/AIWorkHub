# NF-2026-01258: writable empty-output staging

The manager uses self-hosting break-glass only for the Task MCP editor-LM transport.

The production staged collector treated explicit `required_outputs: []` as a whitelist with no permitted paths. An allowed, hash-bound edit returned `semantic_edit_stage_rejected:path_not_required`. The Python bridge had already been repaired for the same writable card shape; the JavaScript collector still rejected it.

One guard now restricts staging to required paths only when that required set is nonempty. Allowed-write, path-contract, hash/range, overlap, placeholder and final-envelope checks remain unchanged. Read-only/out-of-scope and nonempty-required-set negative cases stay rejected. No provider, token, discovery or timeout limits change.

`node vscode-extension/test/vscode-lm-discovery-transition.test.js` is red on the old collector and green after the one-line change. It exercises the real text transport after twelve distinct bounded Source Graph calls, stages and finalizes an existing-file range, and checks empty-output creates and negative scopes. The individual existing bridge harness also passes.

Recent GLM and DeepSeek U2/root workers ended in Source Graph-only loops with no edits. This impossible staging path is proven; causation for every prior loop and improvement on a live model remain unproved until replacement activation and replay. The soft-discovery-nudge hypothesis was removed, not shipped. New regression/document files use the new-file semantic-edit exception; the existing production line was edited with manager prepare/apply.
