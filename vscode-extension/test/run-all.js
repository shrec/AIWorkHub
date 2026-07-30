"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const testRoot = __dirname;
const tests = fs.readdirSync(testRoot, { withFileTypes: true })
  .filter((entry) => entry.isFile() && entry.name.endsWith(".test.js"))
  .map((entry) => entry.name)
  .sort();

if (tests.length === 0) {
  console.error("AIWorkHub extension test discovery found no *.test.js files");
  process.exit(1);
}

for (const test of tests) {
  const absolute = path.join(testRoot, test);
  const result = spawnSync(process.execPath, [absolute], {
    cwd: path.resolve(testRoot, ".."),
    env: process.env,
    stdio: "inherit",
    shell: false,
  });
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
