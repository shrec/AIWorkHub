"use strict";

// The dashboard's terminal-outcome palette used to be GENERATED: each state's
// hue came from its index in the list via a golden-angle formula
// (`hsl(index * 137.508 % 360 65% 55%)`). Against the canonical
// dashboard_kpis.DAILY_STATE_ORDER that produced, at index 0, a pure red
// review_ready -- the successful outcome, 58.7% of 6,232 measured terminal
// runs -- and green for validation_failed (25.9%) and scope_rejected. The two
// signals an operator most needs were exactly inverted, and the generated set
// also failed the palette validator's lightness band on 4 of its 9 slots in
// both light and dark mode.
//
// These tests hold the properties that make the replacement correct, rather
// than pinning the specific colours (the colours come from the user's VS Code
// theme, so they are not ours to pin):
//
//   * fills are keyed by the ENTITY, never by its rank in a list
//   * the successful outcome reads as success and owns that slot alone
//   * no failure wears the success colour, and a cancellation is not a failure
//   * every fill is a var(--vscode-charts-*) token, so the panel follows the
//     user's theme instead of painting over it
//   * the fallbacks -- the only hexes in the file -- are the values the
//     validator was actually run against, per mode
//   * identity is never colour-alone

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const appSource = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const cssSource = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

// Slot ids in their fixed render order. The order is not cosmetic: it decides
// which fills touch inside a stacked column, and only those touching pairs
// were validated.
const SLOT_ORDER = [
  "review_ready",
  "decided",
  "validation_failed",
  "worker_failed",
  "run_failed",
  "blocked",
  "other",
];

// Every state dashboard_kpis.DAILY_STATE_ORDER emits, plus the terminal
// substatuses observed in the measured distribution that are not in it.
const FAILURES = [
  "validation_failed",
  "worker_failed",
  "launch_failed",
  "timed_out",
  "exited",
  "finalize_failed",
  "liveness_lost",
  "output_budget_exceeded",
  "token_budget_exceeded",
];
const DECISIONS = ["cancelled", "scope_rejected"];

function outcomeSlots() {
  const block = appSource.match(/const OUTCOME_SLOTS = \[([\s\S]*?)\n  \];/);
  assert.ok(block, "app.js must declare a fixed OUTCOME_SLOTS map");
  const slots = [];
  const re = /\["([a-z_]+)",\s*"([^"]+)",\s*\[([^\]]*)\]\]/g;
  let m;
  while ((m = re.exec(block[1])) !== null) {
    slots.push({
      id: m[1],
      label: m[2],
      states: m[3].split(",").map((s) => s.trim().replace(/^"|"$/g, "")).filter(Boolean),
    });
  }
  assert.ok(slots.length > 0, "OUTCOME_SLOTS must parse");
  return slots;
}

function slotOf(stateName) {
  for (const slot of outcomeSlots()) {
    if (slot.states.includes(stateName)) return slot.id;
  }
  return /_failed$/.test(stateName) ? "run_failed" : "other";
}

test("the generated index-keyed palette is gone", () => {
  assert.doesNotMatch(appSource, /137\.508/, "the golden-angle formula must not come back");
  assert.doesNotMatch(appSource, /hue \* |\* 137|const hue =/,
    "no hue may be computed from a position");
  assert.doesNotMatch(appSource, /hsl\(/, "no generated hsl() fill may remain in the chart code");
});

test("every outcome slot is declared once, in a fixed order, keyed by state", () => {
  const slots = outcomeSlots();
  assert.deepEqual(slots.map((s) => s.id), SLOT_ORDER,
    "the slot order is the colour-safety mechanism and must not drift");

  const seen = new Set();
  for (const slot of slots) {
    for (const state of slot.states) {
      assert.ok(!seen.has(state), `${state} is assigned to two slots`);
      seen.add(state);
    }
  }
  // The last slot is the fold and owns no state of its own.
  assert.deepEqual(slots[slots.length - 1].states, [],
    "the neutral fold catches what is left over; it must not claim a named state");
});

test("review_ready owns the success slot and nothing else enters it", () => {
  assert.equal(slotOf("review_ready"), "review_ready");
  for (const state of [...FAILURES, ...DECISIONS, "blocked"]) {
    assert.notEqual(slotOf(state), "review_ready",
      `${state} must not share the successful outcome's colour`);
  }
});

test("no failure wears a success or decision colour, and a cancellation is not a failure", () => {
  const failureSlots = new Set(["validation_failed", "worker_failed", "run_failed"]);
  for (const state of FAILURES) {
    const slot = slotOf(state);
    assert.ok(failureSlots.has(slot), `${state} must render as a failure, got slot ${slot}`);
  }
  for (const state of DECISIONS) {
    const slot = slotOf(state);
    assert.equal(slot, "decided", `${state} is a human decision, not a failure (got ${slot})`);
    assert.ok(!failureSlots.has(slot));
  }
  assert.equal(slotOf("blocked"), "blocked", "blocked is waiting on something, not an outcome that failed");
});

test("an unrecognised state folds; it never receives an invented colour", () => {
  assert.equal(slotOf("some_state_the_backend_adds_later"), "other");
  // ...unless it names itself a failure in the convention the backend already
  // uses for every failure it emits.
  assert.equal(slotOf("brand_new_failed"), "run_failed");
  assert.match(appSource, /\/_failed\$\/\.test/,
    "the only inference allowed is the backend's own *_failed naming convention");
});

test("the top three measured outcomes each keep their own slot", () => {
  // Measured over 6,232 terminal outcomes: review_ready 58.7%,
  // validation_failed 25.9%, worker_failed 8.7% -- 93.3% of everything. These
  // three carry the operator's decisions and must never be folded together.
  assert.equal(slotOf("review_ready"), "review_ready");
  assert.equal(slotOf("validation_failed"), "validation_failed");
  assert.equal(slotOf("worker_failed"), "worker_failed");
  assert.notEqual(slotOf("validation_failed"), slotOf("worker_failed"),
    "the model wrote bad code and the plumbing broke are different problems");
});

test("no hue is ever generated, and the categorical budget is respected", () => {
  const slots = outcomeSlots();
  assert.ok(slots.length <= 8,
    `at most eight categorical slots; got ${slots.length}. A ninth series is never a generated hue.`);
});

test("resolution is never spent subdividing success, and success is never subdivided", () => {
  // review_ready is the largest slice (58.7%) and the least informative: it is
  // the baseline, what happens when nothing goes wrong. It gets exactly one
  // colour and one slot.
  const success = outcomeSlots().find((s) => s.id === "review_ready");
  assert.deepEqual(success.states, ["review_ready"],
    "success takes one slot and is never subdivided");
  const successSlots = outcomeSlots().filter((s) => s.states.includes("review_ready"));
  assert.equal(successSlots.length, 1);
});

test("nothing folds on the failure side; the only folds are non-failures", () => {
  // The 41.3% that did not reach review_ready is the entire signal, and seven
  // of those modes sit at or under 0.5%. Rare is not the same as unimportant:
  // each one names a different thing to go and fix. So a fill band may be
  // shared, but an IDENTITY may not be folded away.
  const slots = outcomeSlots();
  const multi = slots.filter((s) => s.states.length > 1);
  for (const slot of multi) {
    const failures = slot.states.filter((state) => FAILURES.includes(state));
    if (failures.length > 1) {
      // A failure slot with several members is allowed ONLY because the
      // ordinal gate caps the ramp at three steps -- and only if every member
      // still gets its own legend row and its own row in the failure panel.
      assert.equal(slot.id, "run_failed",
        "only the measured three-step ramp limit may put several failures in one band");
    }
  }
  const decided = slots.find((s) => s.id === "decided");
  assert.deepEqual(decided.states.slice().sort(), ["cancelled", "scope_rejected"],
    "the fold that exists is between two human decisions, not between two failures");

  // The legend carries one row per STATE, never one per colour, so a mode that
  // shares a band still has a name, a count and a share.
  assert.match(appSource, /ONE ROW PER\n\s*\/\/ STATE/,
    "the legend must be documented and built per state");
  assert.match(appSource, /for \(const name of orderedStateNames\) \{[\s\S]*?createElement\("span", `kpi-legend-item state-\$\{slot\}`\)/,
    "the legend loop must iterate states, not slots");
  assert.doesNotMatch(appSource, /"Other failures"/,
    "no failure may be rendered under an anonymous label");
});

test("the failure taxonomy gets its own uncapped composition", () => {
  // A stacked column cannot carry nine failure modes: severity is ordinal, and
  // the ordinal gate (adjacent dL >= 0.06, light end >= 2:1) admits exactly
  // three steps -- a four-step ramp fails in both modes. The resolution
  // therefore moves to a ranked bar, where a row is a row and a six-event mode
  // is as legible as a sixteen-hundred-event one.
  assert.match(appSource, /createElement\("h3", "kpi-chart-title", "Failure modes"\)/,
    "a dedicated failure-mode panel must exist");
  assert.match(appSource, /const isFailureState = \(name\) => FAILURE_SLOTS\.has\(slotForState\(name\)\)/);
  assert.match(appSource, /const failureOutcomes = outcomes\.filter\(\(item\) => isFailureState\(String\(item\.state \|\| ""\)\)\)/);

  // Uncapped: the old panel ranked every outcome together and then cut the
  // list at eight, which -- ranked by count with review_ready leading -- removed
  // precisely the rare failure modes.
  assert.doesNotMatch(appSource, /outcomes\.slice\(0, 8\)/,
    "the failure list must never be truncated; the rows a cut removes are the rows worth reading");
  assert.match(appSource, /for \(const item of failureOutcomes\) \{/,
    "every failure mode gets a row");

  // Shares are of the failures, so a rare mode is not flattened against the
  // successful runs it is not competing with.
  assert.match(appSource, /const share = failureTotal \? Math\.round\(\(count \/ failureTotal\) \* 1000\) \/ 10 : 0;/);

  // Success and the decisions collapse to one line each -- the only fold, and
  // it is on the side that needs no action.
  assert.match(appSource, /const otherOutcomes = outcomes\.filter\(\(item\) => !isFailureState/);
  assert.match(appSource, /kpi-outcome-rest/);

  // It is first in the grid, so it is the panel that gets the extra column.
  assert.match(appSource, /chartGrid\.insertBefore\(failurePanel, chartGrid\.firstChild\)/);
  assert.match(cssSource, /\.kpi-chart-grid > \.kpi-chart-panel:first-child \{ grid-column: span 2; \}/);
});

test("every fill is a VS Code theme token, and hexes appear only as fallbacks", () => {
  const tokens = ["--outcome-good", "--outcome-decided", "--outcome-failed", "--outcome-waiting"];
  for (const token of tokens) {
    const decl = cssSource.match(new RegExp(`${token}:\\s*([^;]+);`));
    assert.ok(decl, `${token} must be declared`);
    assert.match(decl[1], /^var\(--vscode-charts-[a-z]+, #[0-9a-f]{6}\)$/,
      `${token} must follow the var(--vscode-charts-*, #fallback) idiom this stylesheet already keeps`);
  }
  // The derived failure steps mix the same token toward the panel surface, so
  // severity is one hue in three steps rather than three competing hues.
  for (const token of ["--outcome-failed-2", "--outcome-failed-3"]) {
    const decl = cssSource.match(new RegExp(`${token}:\\s*([^;]+);`));
    assert.ok(decl, `${token} must be declared`);
    assert.match(decl[1], /color-mix\(in srgb, var\(--outcome-failed\) \d+%, var\(--surface-subtle\)\)/,
      `${token} must be a step of --outcome-failed toward the validated panel surface`);
  }

  // Every slot the renderer can emit has a fill rule in both the stacked chart
  // and the outcome-mix bars, so the two panels read as one system.
  for (const slot of SLOT_ORDER) {
    assert.match(cssSource, new RegExp(`\\.kpi-day-segment\\.state-${slot} \\{`), `chart fill missing for ${slot}`);
    assert.match(cssSource, new RegExp(`\\.kpi-legend-item\\.state-${slot} i \\{`), `legend swatch missing for ${slot}`);
    assert.match(cssSource, new RegExp(`\\.kpi-bar-fill\\.state-${slot} \\{`), `outcome-mix fill missing for ${slot}`);
  }
});

test("light themes get their own validated step of the same hues", () => {
  // The base fallbacks sit in the dark lightness band; a light panel needs its
  // own step, not an automatic flip. VS Code stamps the theme kind on <body>.
  const block = cssSource.match(/body\.vscode-light,\s*\nbody\.vscode-high-contrast-light \{([^}]*)\}/);
  assert.ok(block, "light themes must get their own fallback steps");
  for (const token of ["--outcome-decided", "--outcome-failed", "--outcome-waiting"]) {
    assert.match(block[1], new RegExp(`${token}:\\s*var\\(--vscode-charts-[a-z]+, #[0-9a-f]{6}\\)`),
      `${token} must still defer to the theme in light mode -- only the fallback changes`);
  }
});

test("the fallback hexes are the values the palette validator was run against", () => {
  // node scripts/validate_palette.js on the dataviz skill, against the real
  // panel surface (--vscode-editorWidget-background: #f8f8f8 light, #202020
  // dark). Identity slots PASS every categorical check in both modes; the
  // three-step failure ramp PASSES the ordinal gate in both; the worst pair
  // that actually touches in the stack measures CVD dE 15.9 / normal dE 17.0.
  // Changing a hex here invalidates that run, so the run must be redone.
  const VALIDATED = {
    dark: { "--outcome-good": "#008300", "--outcome-decided": "#9085e9", "--outcome-failed": "#e66767", "--outcome-waiting": "#3987e5" },
    light: { "--outcome-decided": "#4a3aa7", "--outcome-failed": "#e34948", "--outcome-waiting": "#2a78d6" },
  };
  const rootBlock = cssSource.slice(cssSource.indexOf(":root {"), cssSource.indexOf("\n}", cssSource.indexOf(":root {")));
  for (const [token, hex] of Object.entries(VALIDATED.dark)) {
    assert.ok(rootBlock.includes(`${token}: var(--vscode-charts-`) && rootBlock.includes(hex),
      `${token} dark fallback must stay ${hex} (validated value)`);
  }
  const lightBlock = cssSource.match(/body\.vscode-light,\s*\nbody\.vscode-high-contrast-light \{([^}]*)\}/)[1];
  for (const [token, hex] of Object.entries(VALIDATED.light)) {
    assert.ok(lightBlock.includes(token) && lightBlock.includes(hex),
      `${token} light fallback must stay ${hex} (validated value)`);
  }
});

test("the failure steps are derived on <body>, not frozen on :root", () => {
  // A custom property substitutes var() using the values on its OWN element.
  // Deriving --outcome-failed-2 on :root would freeze it at the dark step even
  // when body.vscode-light overrides --outcome-failed underneath it.
  const bodyBlock = cssSource.match(/\nbody \{([\s\S]*?)\n\}/);
  assert.ok(bodyBlock, "body rule must exist");
  for (const token of ["--outcome-failed-2", "--outcome-failed-3", "--outcome-other"]) {
    assert.match(bodyBlock[1], new RegExp(`${token}:`),
      `${token} must be declared on body so the light-theme override reaches it`);
  }
});

test("identity is never colour-alone", () => {
  // A legend for two or more series, the exact state name on every segment for
  // tooltip and screen reader, and counts plus shares in the legend so the
  // chart survives greyscale, colour blindness and a screen reader.
  assert.match(appSource, /segment\.setAttribute\("aria-label", `\$\{label\}: \$\{count\} on \$\{day\.date\}`\)/);
  assert.match(appSource, /item\.setAttribute\("aria-label", `\$\{label\}: \$\{count\} outcomes, \$\{share\}%`\)/);
  assert.match(appSource, /createElement\("div", "kpi-legend"\)/);
  // A 2px surface gap between stacked fills, so two segments sharing a folded
  // colour are still countable. box-shadow so it costs no layout.
  assert.match(cssSource, /\.kpi-day-segment \+ \.kpi-day-segment \{ box-shadow: inset 0 2px 0 var\(--surface-subtle\); \}/);
});

test("the chart never writes a colour that would override the user's theme", () => {
  assert.doesNotMatch(appSource, /\.style\.background\s*=/,
    "no inline background may be written anywhere in the dashboard renderer");
  // Every colour literal in the stylesheet must be a var() fallback, never a
  // bare value that wins over the theme. Comments are stripped first: a hex
  // quoted in a comment is documentation, not a declaration.
  const declarations = cssSource.replace(/\/\*[\s\S]*?\*\//g, "");
  const bareHexRules = [];
  for (const m of declarations.matchAll(/(^|[\s:,(])(#[0-9a-fA-F]{3,8})\b/g)) {
    const at = m.index;
    const before = declarations.slice(Math.max(0, at - 120), at);
    if (/var\(\s*--vscode-[a-zA-Z-]+\s*,\s*(?:var\([^)]*\)\s*,\s*)?$/.test(before)) continue;
    if (/var\(\s*--[a-zA-Z-]+\s*,\s*$/.test(before)) continue;
    bareHexRules.push(m[2]);
  }
  assert.deepEqual(bareHexRules, [],
    `every hex must sit inside a var(--vscode-*, fallback); bare literals found: ${bareHexRules.join(", ")}`);
});
