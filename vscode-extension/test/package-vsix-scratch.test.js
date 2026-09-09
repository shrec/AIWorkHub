"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");

const packager = require("./package-vsix.js");

const ordinaryDist = packager.DEFAULT_DIST;
const scratchEnv = packager.VALIDATION_SCRATCH_ENV;
let fixtureSeq = 0;

function fixtureRoot() {
  fixtureSeq += 1;
  const envRoot = process.env[scratchEnv];
  const parent = envRoot && String(envRoot).trim() !== ""
    ? path.resolve(String(envRoot).trim())
    : os.tmpdir();
  const root = path.join(parent, `vsix-scratch-routing-${process.pid}-${fixtureSeq}`);
  fs.mkdirSync(root, { recursive: true });
  return root;
}

function assertInside(parent, child) {
  const relative = path.relative(path.resolve(parent), path.resolve(child));
  assert.ok(
    relative !== ""
      && relative !== ".."
      && !relative.startsWith(`..${path.sep}`)
      && !path.isAbsolute(relative),
    `${child} must stay under ${parent}`,
  );
}

function assertOutside(parent, child) {
  const relative = path.relative(path.resolve(parent), path.resolve(child));
  assert.ok(
    relative.startsWith("..") || path.isAbsolute(relative),
    `${child} must not be under ${parent}`,
  );
}

function assertOrdinary(layout) {
  assert.strictEqual(layout.usesScratch, false);
  assert.strictEqual(path.resolve(layout.outputRoot), path.resolve(ordinaryDist));
  assert.strictEqual(layout.staging, path.join(ordinaryDist, "vsix-staging"));
  assert.strictEqual(layout.extensionDir, path.join(ordinaryDist, "vsix-staging", "extension"));
  const vsix = packager.resolveOutputPath(layout, "aiworkhub-9.9.9.vsix");
  assert.strictEqual(vsix, path.join(ordinaryDist, "aiworkhub-9.9.9.vsix"));
  assertInside(ordinaryDist, layout.staging);
  assertInside(ordinaryDist, vsix);
}

function assertScratch(layout, scratchRoot) {
  const resolvedScratch = path.resolve(scratchRoot);
  assert.strictEqual(layout.usesScratch, true);
  assert.strictEqual(path.resolve(layout.outputRoot), resolvedScratch);
  assertInside(resolvedScratch, layout.staging);
  assertInside(resolvedScratch, layout.extensionDir);
  const vsix = packager.resolveOutputPath(layout, "aiworkhub-9.9.9.vsix");
  assertInside(resolvedScratch, vsix);
  assertOutside(ordinaryDist, layout.staging);
  assertOutside(ordinaryDist, layout.extensionDir);
  assertOutside(ordinaryDist, vsix);
  assertOutside(ordinaryDist, layout.outputRoot);
}

assertOrdinary(packager.resolvePackagingLayout({}));
assertOrdinary(packager.resolvePackagingLayout({ [scratchEnv]: "" }));
assertOrdinary(packager.resolvePackagingLayout({ [scratchEnv]: "   " }));

const scratchRoot = fixtureRoot();
try {
  const layout = packager.resolvePackagingLayout({ [scratchEnv]: scratchRoot });
  assertScratch(layout, scratchRoot);
  assert.throws(
    () => packager.resolveOutputPath(layout, "..", "vscode-extension", "dist", "escape.vsix"),
    /escaped authenticated output root/,
  );
  assert.throws(
    () => packager.resolvePackagingLayout({ [scratchEnv]: "relative-scratch" }),
    /absolute directory/,
  );
  const nested = path.join(scratchRoot, "creatable-child");
  const created = packager.resolvePackagingLayout({ [scratchEnv]: nested });
  assertScratch(created, nested);
  assert.ok(fs.statSync(nested).isDirectory());
  const notDir = path.join(scratchRoot, "not-a-directory");
  fs.writeFileSync(notDir, "x");
  assert.throws(
    () => packager.resolvePackagingLayout({ [scratchEnv]: notDir }),
    /absolute directory/,
  );
} finally {
  fs.rmSync(scratchRoot, { recursive: true, force: true });
}

console.log("package-vsix scratch routing ok");
