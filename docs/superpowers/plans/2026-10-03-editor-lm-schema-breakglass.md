# Editor-LM schema repair: NF-2026-01252

Manager break-glass restores the blocked Task MCP transport, not chat features.

- DeepSeek U1 request `d3588f408fc242f6b6eae780e5791ba7` exhausted the bridge turn budget after repeatedly sending flat final edits rejected as `final_edit_invalid`; no edits or validations ran.
- GLM read-only audit `7b8fd2ea2ab74fbfb342d318a4185758` was independently inspected and accepted. Both native and text transports share `glmAgentProtocolPrompt` and the v3 validator.
- The shared prompt now gives one concrete final `path/ranges` example, distinguishing final fields from stage `operation/file_path` fields. The validator names missing final keys; all path, hash, range, create and fidelity checks remain intact.
- `node test/glm-vscode-lm-bridge.test.js` failed against the old diagnostic, then passed after the correction. The regression exercises both real transport functions with a rejected flat response followed by the correct response in exactly two turns.

Separate U1 finding, NF-2026-01253: the retained pre-edit artifact already contained CRLF. The GLM version rebase was an exact one-line change; it did not translate newlines. The existing writer uses `newline=''` and must not be changed for this symptom. Nine Node source-slice assertions fail because two test readers assume LF. The complete union passed 59 Python tests and built the 0.12.17 VSIX, but U1 was returned blocked until the test-reader correction; it was not accepted on partial evidence.

Resume via the same U1 card: retain the renderer delta, normalize CRLF only at the two test source readers, preserve the canonical shared bridge correction, and rerun affected Node checks on the complete union. U2/U3 remain sequential. Do not launch Claude, run the full extension suite, push, or treat tests as live chat acceptance. Build/install 0.12.18 after the required fixes using an external hidden terminal.
