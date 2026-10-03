# NF-2026-01308: allowed optional-output staging

The manager uses self-hosting break-glass only to restore the Task MCP editor-worker transport.

Native OpenCode binding attempt `6dfa59554e7d45829947197ecd9855d0` failed before validation: the finalizer requested the allowed new helper, but staging rejected it as `path_not_required`. The exact card allowed three paths and required changes to two. The collector incorrectly used the nonempty mandatory-output set as a second authorization whitelist. The earlier NF01258 empty-set correction did not repair this nonempty case.

The smallest production correction removes that second whitelist. Exact allowed writes and per-path action contracts still authorize staging; content fidelity, existing-file authenticated semantic prepare/apply, hashes, ranges, overlaps and mandatory-output completion stay unchanged. No provider, time, token, filesystem or sandbox authority is widened.

The new optional-output regression was red on the original production source and green after the correction. It exercises the actual collector and text transport, allowed optional creates/edits, missing mandatory output, denied scope/action/contracts, placeholders and range conflicts. Its provider is a deterministic mock, not live-model or HMAC acceptance evidence. The production transport terminates after the second stage without an extra model call.

The existing extra-path negative has no create contract and remains rejected as action mismatch. The discovery harness separately verifies that an allowed create cannot satisfy an absent mandatory output. Three bridge harnesses and the six individual handoff UI harnesses pass. Repository `.venv` Python gates passed 83 invariant/range/platform/poll checks and 145 bridge/release checks. System Python lacked xdist; no gate was weakened to accommodate that environment.

The new regression originally carried a synthetic relative require inside virtual create content. Native validation scanned it as a host dependency, stopping NF1307 attempt `51dfbf39617148e289d30ac5369511dd` before any tests. The manager corrected that fixture, recorded the cause, and returned the same card pending with its sealed two-file delta, rather than discard work or claim correctness. Seal: `20fa56b28d3a35ad3250da2884ef304a7d47ada7eefac76d464b3912b6ce6392`.

Release 0.12.22 packages this restoration. Build, hidden external installation, activation and genuine worker replay require independent receipts. The installed 0.12.21 process is not fixed merely by changing canonical source. Active workers must not be interrupted solely for reload.

Full Manager Chat, real no-drive sandboxed OpenCode/Muse response and MCP, uniform context/cost policy and Skills outcome/actor integration are unfinished and are not claimed here. Removing the five owner-authorized legacy drive mappings did not delete files or establish the production no-drive fix.
