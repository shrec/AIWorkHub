"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const sideEffectProbe = spawnSync(process.execPath, ["-e", `
  const fs = require("fs");
  const path = require("path");
  const originalRead = fs.readFileSync;
  const originalOpen = fs.openSync;
  fs.readFileSync = function(filePath, ...args) {
    if (path.basename(String(filePath)) === "package.json") {
      throw new Error("package.json was read while requiring the packager");
    }
    return originalRead.call(this, filePath, ...args);
  };
  fs.openSync = function(filePath, ...args) {
    if (path.basename(String(filePath)) === "package.json") {
      throw new Error("package.json was opened while requiring the packager");
    }
    return originalOpen.call(this, filePath, ...args);
  };
  fs.rmSync = () => { throw new Error("build cleanup ran while requiring the packager"); };
  fs.writeFileSync = () => { throw new Error("build output ran while requiring the packager"); };
  require("child_process").execFileSync = () => {
    throw new Error("native build ran while requiring the packager");
  };
  require("./package-vsix");
`], {
  cwd: __dirname,
  encoding: "utf8",
  shell: false,
});
assert.strictEqual(
  sideEffectProbe.status,
  0,
  `requiring the packager must not read package.json or build artifacts: ${sideEffectProbe.stderr}`,
);

const {
  MAX_PACKAGE_JSON_BYTES,
  PACKAGE_JSON_READ_CHUNK_BYTES,
  packageWithVersionGate,
} = require("./package-vsix");

function assertCanonicalRepositoryVersionsMatch() {
  const repositoryRoot = path.resolve(__dirname, "..", "..");
  const canonicalSource = fs.readFileSync(
    path.join(repositoryRoot, "src", "aiworkhub", "_version.py"),
    "utf8",
  );
  const extensionSource = fs.readFileSync(
    path.join(repositoryRoot, "vscode-extension", "extension.js"),
    "utf8",
  );
  const packageJson = JSON.parse(fs.readFileSync(
    path.join(repositoryRoot, "vscode-extension", "package.json"),
    "utf8",
  ));
  const packageLock = JSON.parse(fs.readFileSync(
    path.join(repositoryRoot, "vscode-extension", "package-lock.json"),
    "utf8",
  ));
  const canonical = canonicalSource.match(/^__version__ = "([^"]+)"$/m);
  const expectedRuntime = extensionSource.match(
    /^const EXPECTED_MCP_PACKAGE_VERSION = "([^"]+)";$/m,
  );
  assert.ok(canonical, "canonical Python version must be present");
  assert.ok(expectedRuntime, "extension runtime version must be present");
  assert.deepStrictEqual(
    {
      packageJson: packageJson.version,
      packageLock: packageLock.version,
      packageLockRoot: packageLock.packages && packageLock.packages[""].version,
      expectedRuntime: expectedRuntime[1],
    },
    {
      packageJson: canonical[1],
      packageLock: canonical[1],
      packageLockRoot: canonical[1],
      expectedRuntime: canonical[1],
    },
    "all release version surfaces must match src/aiworkhub/_version.py",
  );
}

assertCanonicalRepositoryVersionsMatch();

function fakeStat({ dev = 1n, ino = 2n, size, mtimeNs = 3n, ctimeNs = 4n } = {}) {
  return {
    dev,
    ino,
    size: BigInt(size),
    mtimeNs,
    ctimeNs,
    isFile: () => true,
    isSymbolicLink: () => false,
  };
}

function raceFileOps(source, { growDuringRead = false, swapPathAfterRead = false } = {}) {
  const content = Buffer.from(source, "utf8");
  const metrics = {
    closeCalls: 0,
    maxBufferLength: 0,
    maxRequested: 0,
    totalRequested: 0,
  };
  let fstatCalls = 0;
  let lstatCalls = 0;
  const stable = fakeStat({ size: content.length });
  const grown = fakeStat({ size: MAX_PACKAGE_JSON_BYTES + 1, mtimeNs: 30n, ctimeNs: 40n });
  return {
    constants: { O_RDONLY: 0, O_NOFOLLOW: 0x20000 },
    metrics,
    lstatSync() {
      lstatCalls += 1;
      if (lstatCalls === 1) return stable;
      if (swapPathAfterRead) return fakeStat({ ino: 99n, size: content.length });
      return growDuringRead ? grown : stable;
    },
    openSync() { return 123; },
    fstatSync() {
      fstatCalls += 1;
      return fstatCalls === 1 ? stable : (growDuringRead ? grown : stable);
    },
    readSync(_descriptor, buffer, offset, length, position) {
      metrics.maxBufferLength = Math.max(metrics.maxBufferLength, buffer.length);
      metrics.maxRequested = Math.max(metrics.maxRequested, length);
      metrics.totalRequested += length;
      if (growDuringRead) {
        buffer.fill(0x20, offset, offset + length);
        return length;
      }
      const available = Math.max(0, content.length - position);
      const copied = Math.min(length, available);
      if (copied > 0) content.copy(buffer, offset, position, position + copied);
      return copied;
    },
    closeSync() { metrics.closeCalls += 1; },
  };
}

function writeFixture(root, { canonical, packageVersion, extensionVersion, rawPackageJson }) {
  fs.mkdirSync(path.join(root, "src", "aiworkhub"), { recursive: true });
  fs.mkdirSync(path.join(root, "vscode-extension"), { recursive: true });
  fs.writeFileSync(
    path.join(root, "src", "aiworkhub", "_version.py"),
    `__version__ = "${canonical}"\n`,
    "utf8",
  );
  fs.writeFileSync(
    path.join(root, "vscode-extension", "package.json"),
    rawPackageJson === undefined
      ? `${JSON.stringify(packageVersion === undefined ? {} : { version: packageVersion })}\n`
      : `${rawPackageJson}\n`,
    "utf8",
  );
  fs.writeFileSync(
    path.join(root, "vscode-extension", "extension.js"),
    `const EXPECTED_MCP_PACKAGE_VERSION = "${extensionVersion}";\n`,
    "utf8",
  );
}

const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-version-gate-"));
try {
  writeFixture(fixture, {
    canonical: "0.8.22",
    packageVersion: "0.9.39",
    extensionVersion: "0.9.39",
  });
  let packageCalls = 0;
  assert.throws(
    () => packageWithVersionGate({
      repositoryRoot: fixture,
      packageAction: () => { packageCalls += 1; },
    }),
    (error) => {
      assert.match(error.message, /expected src\/aiworkhub\/_version\.py=0\.8\.22/);
      assert.match(error.message, /vscode-extension\/package\.json=0\.9\.39/);
      assert.match(error.message, /extension\.js EXPECTED_MCP_PACKAGE_VERSION=0\.9\.39/);
      return true;
    },
  );
  assert.strictEqual(packageCalls, 0, "mismatched metadata must fail before packaging starts");

  writeFixture(fixture, {
    canonical: "0.9.39",
    packageVersion: "unused",
    extensionVersion: "0.9.39",
    rawPackageJson: '{"vers\\u0069on":"0.8.22","nested":{"version":"deceptive"},"note":"\\\"version\\\":not-a-key","version":"0.9.39"}',
  });
  let duplicatePackageCalls = 0;
  assert.throws(
    () => packageWithVersionGate({
      repositoryRoot: fixture,
      packageAction: () => { duplicatePackageCalls += 1; },
    }),
    (error) => {
      assert.match(error.message, /vscode-extension\/package\.json must contain exactly one top-level version member/);
      assert.match(error.message, /actual count=2/);
      return true;
    },
  );
  assert.strictEqual(duplicatePackageCalls, 0, "duplicate top-level version keys must fail before packaging starts");

  const validPackageSource = '{"version":"0.9.39"}';
  const growingFileOps = raceFileOps(validPackageSource, { growDuringRead: true });
  let growthPackageCalls = 0;
  assert.throws(
    () => packageWithVersionGate({
      repositoryRoot: fixture,
      packageFileOps: growingFileOps,
      packageAction: () => { growthPackageCalls += 1; },
    }),
    /vscode-extension\/package\.json descriptor changed while being read/,
  );
  assert.strictEqual(growthPackageCalls, 0, "growth during descriptor reads must fail before packaging");
  assert.strictEqual(growingFileOps.metrics.closeCalls, 1, "racing descriptors must close exactly once");
  assert.strictEqual(
    growingFileOps.metrics.maxBufferLength,
    MAX_PACKAGE_JSON_BYTES + 1,
    "the growth probe must use one bounded MAX+1 buffer",
  );
  assert.ok(
    growingFileOps.metrics.maxRequested <= PACKAGE_JSON_READ_CHUNK_BYTES,
    "each descriptor read must remain chunk-bounded",
  );
  assert.strictEqual(
    growingFileOps.metrics.totalRequested,
    MAX_PACKAGE_JSON_BYTES + 1,
    "growth probing must stop after MAX+1 bytes",
  );

  const swappedFileOps = raceFileOps(validPackageSource, { swapPathAfterRead: true });
  let swapPackageCalls = 0;
  assert.throws(
    () => packageWithVersionGate({
      repositoryRoot: fixture,
      packageFileOps: swappedFileOps,
      packageAction: () => { swapPackageCalls += 1; },
    }),
    /vscode-extension\/package\.json path identity changed while being read/,
  );
  assert.strictEqual(swapPackageCalls, 0, "path replacement during descriptor reads must fail before packaging");
  assert.strictEqual(swappedFileOps.metrics.closeCalls, 1, "swapped descriptors must close exactly once");

  for (const invalid of [
    { label: "array", value: ["0.9.39"], type: "array", rendered: '["0.9.39"]' },
    { label: "object", value: { release: "0.9.39" }, type: "object", rendered: '{"release":"0.9.39"}' },
    { label: "number", value: 939, type: "number", rendered: "939" },
    { label: "null", value: null, type: "null", rendered: "null" },
    { label: "missing", value: undefined, type: "undefined", rendered: "missing" },
  ]) {
    writeFixture(fixture, {
      canonical: "0.9.39",
      packageVersion: invalid.value,
      extensionVersion: "0.9.39",
    });
    let invalidPackageCalls = 0;
    assert.throws(
      () => packageWithVersionGate({
        repositoryRoot: fixture,
        packageAction: () => { invalidPackageCalls += 1; },
      }),
      (error) => {
        assert.match(error.message, /vscode-extension\/package\.json version must be a primitive string/);
        assert.match(error.message, new RegExp(`actual type=${invalid.type}`));
        assert.ok(
          error.message.includes(`value=${invalid.rendered}`),
          `${invalid.label} error must report its exact value: ${error.message}`,
        );
        return true;
      },
    );
    assert.strictEqual(
      invalidPackageCalls,
      0,
      `${invalid.label} package version must fail before packaging starts`,
    );
  }

  writeFixture(fixture, {
    canonical: "0.9.39",
    packageVersion: "0.9.39",
    extensionVersion: "0.9.39",
  });
  const packaged = packageWithVersionGate({
    repositoryRoot: fixture,
    packageAction: (snapshot) => {
      packageCalls += 1;
      fs.writeFileSync(
        path.join(fixture, "vscode-extension", "package.json"),
        '{"version":"9.9.9"}\n',
        "utf8",
      );
      assert.strictEqual(snapshot.json.version, "0.9.39");
      assert.strictEqual(JSON.parse(snapshot.source).version, "0.9.39");
      return "packaged";
    },
  });
  assert.strictEqual(packageCalls, 1, "matching metadata must proceed to packaging");
  assert.strictEqual(packaged.versions.canonicalVersion, "0.9.39");
  assert.strictEqual(packaged.result, "packaged");
} finally {
  fs.rmSync(fixture, { recursive: true, force: true });
}

console.log("package-vsix release version gate tests passed");
