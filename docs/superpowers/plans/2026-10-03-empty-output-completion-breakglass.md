# NF-2026-01263: stop phase-count completion of tests-only work

Owner authorized semantic-editor continuation without Reload while the installed
Task bridge blocks progress. This repair is limited to shared worker completion;
it implements none of the U2 feature itself.

## Measured failure

U2 request `dae5ace739da427eb62a66468cbe4c69` created only the new
`vscode-extension/test/manager-console.test.js` (8753 bytes). Its production files
were unchanged. With `required_outputs: []`, the bridge nevertheless generated
`Applied validated staged semantic edits.` and finalized at the phase-count limit.
The empty output list specifies no completion obligations; it does not prove that
one staged file completes the feature.

Independent candidate-union execution: the new console tests failed 0/7 passed
(`managerConsoleMergeCommands` undefined); the other five host harnesses, 59
Python polling/release tests and package construction passed. The incomplete
candidate was rejected and blocked, not promoted. Session event 1024 and Learning
Commit `6fafdca72366817352e4a007a8bb9c99f4bf501046855e37c374a9586e765eea`
correct the initial inaccurate zero-delta description: one test file was created.

## Small shared repair

Exclude writable requests with an explicit empty required-output list from both
native and text protocol phase-count force-stage/force-final transitions. They
continue to explicit worker finalization. Declared nonempty and unspecified
legacy output contracts retain their former automatic completion semantics.

The regression reproduced four failures before the repair: early staging stopped
after 12/15 provider turns, late staging after 13/15, in both transports. All nine
tests now pass, including late first staging in native mode and legacy controls.
The independent read-only reviewer verified the same before/after behavior and
reported no blocking correctness or security findings.

Unchanged: authority, allowed paths, hashes, bounded ranges, payload limits,
cancellation, duplicate protection, global 24-turn limit and Source Graph ceiling.
Long work may still hit the latter bounds; this fix prevents false completion,
not all execution-capacity failures.

No installed-plugin activation or completed U2/M1/live-chat result is claimed.
Owner cannot Reload until return; continue independently validated canonical
high-ROI work, preserving truthful installed/runtime evidence.
