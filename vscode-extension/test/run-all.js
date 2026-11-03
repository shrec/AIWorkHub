"use strict";

const fs = require("fs");
const path = require("path");
const { runIsolatedTest } = require("./isolated-test-env");

// NF-2026-01644: one hung file used to hold the whole suite until the 1800 s
// validation timeout without naming itself, so every file now runs under a bound.
const TEST_TIMEOUT_ENV = "AIWORKHUB_EXTENSION_TEST_TIMEOUT_MS";
const DEFAULT_TEST_TIMEOUT_MS = 300000;

function testTimeoutMs(raw) {
  if (raw === undefined) return DEFAULT_TEST_TIMEOUT_MS;
  const value = /^[0-9]+$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isSafeInteger(value) || value <= 0) {
    console.error(`${TEST_TIMEOUT_ENV} must be a positive integer of milliseconds, got ${JSON.stringify(raw)}`);
    process.exit(1);
  }
  return value;
}

// NF-2026-01986: a suite-level timeout tail used to say only "timeout"; one
// start line and one done line per file let that tail name the slow file.
function startLine(test) {
  return `[run-all] start ${test}`;
}

function doneLine(test, ms) {
  return `[run-all] done ${test} ${ms}ms`;
}

function main(testRoot = __dirname, runTest = runIsolatedTest, log = console.log) {
  const timeoutMs = testTimeoutMs(process.env[TEST_TIMEOUT_ENV]);
  const tests = fs.readdirSync(testRoot, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".test.js"))
    .map((entry) => entry.name)
    .sort();

  if (tests.length === 0) {
    console.error("AIWorkHub extension test discovery found no *.test.js files");
    process.exit(1);
  }
  if (!tests.includes("package-vsix-scratch.test.js")) {
    console.error("AIWorkHub extension test discovery missing package-vsix-scratch.test.js");
    process.exit(1);
  }
  for (const test of tests) {
    const absolute = path.join(testRoot, test);
    let result;
    log(startLine(test));
    const started = Date.now();
    try {
      result = runTest([absolute], { timeoutMs });
    } catch (error) {
      log(doneLine(test, Date.now() - started));
      // A descendant that outlived a timed-out child can still hold its scratch. runIsolatedTest
      // attaches the child's own result to that failure, so the hung file is still named as such.
      if (error.result && error.result.error && error.result.error.code === "ETIMEDOUT") {
        console.error(`${test} timed out after ${timeoutMs}ms (scratch cleanup failed: ${error.message})`);
        process.exit(1);
      }
      console.error(`${test} could not run in isolation: ${error.message}`);
      process.exit(1);
    }
    log(doneLine(test, Date.now() - started));
    if (result.error && result.error.code === "ETIMEDOUT") {
      console.error(`${test} timed out after ${timeoutMs}ms`);
      process.exit(1);
    }
    if (result.error) {
      console.error(`Failed to start ${test}: ${result.error.message}`);
      process.exit(1);
    }
    if (result.status !== 0) {
      console.error(`${test} failed with exit code ${result.status}`);
      process.exit(result.status || 1);
    }
  }

  console.log(`AIWorkHub extension test discovery passed (${tests.length} files)`);
}

// extension-teardown-nf1644 evaluates this source in a vm sandbox without a
// CommonJS `module`, so main() also runs when no module object exists.
if (typeof module === "undefined" || require.main === module) {
  main();
}

if (typeof module !== "undefined") module.exports = { main, startLine, doneLine, testTimeoutMs };
