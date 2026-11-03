// NF-2026-01986: run-all.js names every file it starts and finishes, so a
// suite-level timeout tail names the slow file. The runner is stubbed, so no
// test file actually runs and nothing is written.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const runAll = require("./run-all.js");

const fs = require("node:fs");

const testRoot = __dirname;
const LISTING = ["alpha.test.js", "helper.js", "package-vsix-scratch.test.js", "zeta.test.js"];

// A fixed listing keeps the test independent of which files a (sparse) checkout holds.
function withStubbedExit(fn) {
  const originalExit = process.exit;
  const originalError = console.error;
  const originalReaddirSync = fs.readdirSync;
  const errors = [];
  process.exit = (code) => {
    const err = new Error(`process.exit(${code})`);
    err.exitCode = code;
    throw err;
  };
  console.error = (message) => { errors.push(String(message)); };
  fs.readdirSync = (dir, options) => {
    if (dir !== testRoot) return originalReaddirSync(dir, options);
    return LISTING.map((name) => ({ name, isFile: () => true }));
  };
  try {
    return { value: fn(), errors };
  } catch (err) {
    if (err && err.exitCode !== undefined) return { exitCode: err.exitCode, errors };
    throw err;
  } finally {
    process.exit = originalExit;
    console.error = originalError;
    fs.readdirSync = originalReaddirSync;
  }
}

test("start and done lines have the documented shape", () => {
  assert.equal(runAll.startLine("a.test.js"), "[run-all] start a.test.js");
  assert.equal(runAll.doneLine("a.test.js", 42), "[run-all] done a.test.js 42ms");
});

test("every file gets a start line before it runs and a done line after", () => {
  const lines = [];
  const ran = [];
  const runner = (argv) => {
    const name = path.basename(argv[0]);
    lines.push(`RUN ${name}`);
    ran.push(name);
    return { status: 0 };
  };
  const originalLog = console.log;
  console.log = () => {};
  let outcome;
  try {
    outcome = withStubbedExit(() => runAll.main(testRoot, runner, (line) => lines.push(line)));
  } finally {
    console.log = originalLog;
  }
  assert.equal(outcome.exitCode, undefined, JSON.stringify(outcome.errors));
  assert.deepEqual(ran, ["alpha.test.js", "package-vsix-scratch.test.js", "zeta.test.js"]);
  for (const name of ran) {
    const start = lines.indexOf(`[run-all] start ${name}`);
    const runAt = lines.indexOf(`RUN ${name}`);
    const done = lines.findIndex((line) => new RegExp(`^\\[run-all\\] done ${name.replace(/\./g, "\\.")} \\d+ms$`).test(line));
    assert.ok(start >= 0 && start < runAt && runAt < done, `${name}: ${start} ${runAt} ${done}`);
  }
});

test("a failing file still gets its done line and keeps the old exit code and message", () => {
  const lines = [];
  let first = null;
  const runner = (argv) => {
    first = first || path.basename(argv[0]);
    return { status: 3 };
  };
  const outcome = withStubbedExit(() => runAll.main(testRoot, runner, (line) => lines.push(line)));
  assert.equal(outcome.exitCode, 3);
  assert.deepEqual(outcome.errors, [`${first} failed with exit code 3`]);
  assert.equal(lines[0], `[run-all] start ${first}`);
  assert.match(lines[1], new RegExp(`^\\[run-all\\] done ${first.replace(/\./g, "\\.")} \\d+ms$`));
});

test("a timed-out file keeps the old timeout message after its done line", () => {
  const lines = [];
  let first = null;
  const runner = (argv) => {
    first = first || path.basename(argv[0]);
    const error = new Error("spawnSync node ETIMEDOUT");
    error.code = "ETIMEDOUT";
    return { status: null, error };
  };
  const outcome = withStubbedExit(() => runAll.main(testRoot, runner, (line) => lines.push(line)));
  assert.equal(outcome.exitCode, 1);
  assert.equal(outcome.errors.length, 1);
  assert.match(outcome.errors[0], new RegExp(`^${first.replace(/\./g, "\\.")} timed out after \\d+ms$`));
  assert.equal(lines.length, 2);
  assert.ok(lines[1].startsWith(`[run-all] done ${first} `));
});
