const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const dist = path.join(root, "dist");
const staging = path.join(dist, "vsix-staging");
const extensionDir = path.join(staging, "extension");

// Canonical, single source of truth for the bundled Python MCP runtime: the
// same src/aiworkhub package this repo tests/ships everywhere else. Copied
// wholesale (including dashboard_static/* assets)
// into an extension-local runtime/ directory so the packaged extension never
// needs a repository checkout, an editable install, user/site-packages, or a
// network install to run `python -m aiworkhub.server` -- see extension.js's
// McpStdioClient._start(), which points both PYTHONPATH and the child's cwd
// at this directory.
const PY_RUNTIME_SRC = path.join(root, "..", "src", "aiworkhub");
const PY_RUNTIME_DEST = path.join(extensionDir, "runtime", "aiworkhub");
const MUX_LAUNCHER_SRC = path.join(root, "..", "scripts", "aiworkhub-app-server-mux");
const MUX_LAUNCHER_DEST = path.join(extensionDir, "bin", "aiworkhub-app-server-mux");
const MUX_LAUNCHER_CMD_SRC = path.join(root, "..", "scripts", "aiworkhub-app-server-mux.cmd");
const MUX_LAUNCHER_CMD_DEST = path.join(extensionDir, "bin", "aiworkhub-app-server-mux.cmd");
const NATIVE_LAUNCHER_SRC = path.join(root, "native-launcher", "main.go");
// Never bundle bytecode caches or OS cruft -- only the real package source
// and its data assets (e.g. dashboard_static/*.css/.js/.html).
const PY_RUNTIME_SKIP_DIRS = new Set(["__pycache__", ".pytest_cache"]);
const PY_RUNTIME_SKIP_FILE_SUFFIXES = [".pyc", ".pyo", ".DS_Store"];
const MAX_PACKAGE_JSON_BYTES = 1024 * 1024;
const PACKAGE_JSON_READ_CHUNK_BYTES = 64 * 1024;
const RELEASE_VERSION = /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$/;
const PYTHON_VERSION_LITERAL = /^__version__\s*=\s*["']([^"']+)["']\s*$/gm;
const EXTENSION_VERSION_LITERAL = /^const EXPECTED_MCP_PACKAGE_VERSION\s*=\s*["']([^"']+)["'];\s*$/gm;

function readVersionLiteral(filePath, sourceName, pattern) {
  const source = fs.readFileSync(filePath, "utf8");
  const matches = Array.from(source.matchAll(pattern));
  if (matches.length !== 1) {
    throw new Error(`Release version consistency check failed: ${sourceName} must contain exactly one version literal`);
  }
  const version = matches[0][1];
  if (!RELEASE_VERSION.test(version)) {
    throw new Error(`Release version consistency check failed: ${sourceName} has invalid version ${JSON.stringify(version)}`);
  }
  return version;
}

function statValue(stat, field) {
  return stat[field] === undefined ? "unavailable" : String(stat[field]);
}

function sameFileIdentity(left, right) {
  const leftDevice = statValue(left, "dev");
  const rightDevice = statValue(right, "dev");
  const deviceMatches = leftDevice === rightDevice || leftDevice === "0" || rightDevice === "0";
  return deviceMatches && statValue(left, "ino") === statValue(right, "ino");
}

function snapshotChanged(left, right) {
  return !sameFileIdentity(left, right)
    || statValue(left, "size") !== statValue(right, "size")
    || statValue(left, "mtimeNs") !== statValue(right, "mtimeNs")
    || statValue(left, "ctimeNs") !== statValue(right, "ctimeNs");
}

function readStableBoundedTextFile(filePath, sourceName, fileOps = fs) {
  let descriptor;
  try {
    const pathBefore = fileOps.lstatSync(filePath, { bigint: true });
    if (pathBefore.isSymbolicLink() || !pathBefore.isFile()) {
      throw new Error(
        `Release version consistency check failed: ${sourceName} must be a non-symlink regular file`,
      );
    }
    const noFollow = typeof fileOps.constants.O_NOFOLLOW === "number"
      ? fileOps.constants.O_NOFOLLOW
      : 0;
    descriptor = fileOps.openSync(filePath, fileOps.constants.O_RDONLY | noFollow);
    const descriptorBefore = fileOps.fstatSync(descriptor, { bigint: true });
    if (!descriptorBefore.isFile()) {
      throw new Error(`Release version consistency check failed: ${sourceName} descriptor is not a regular file`);
    }
    if (!sameFileIdentity(pathBefore, descriptorBefore)) {
      throw new Error(`Release version consistency check failed: ${sourceName} changed while being opened`);
    }
    if (BigInt(descriptorBefore.size) > BigInt(MAX_PACKAGE_JSON_BYTES)) {
      throw new Error(
        `Release version consistency check failed: ${sourceName} exceeds ${MAX_PACKAGE_JSON_BYTES} bytes; `
        + `actual size=${descriptorBefore.size}`,
      );
    }

    const buffer = Buffer.allocUnsafe(MAX_PACKAGE_JSON_BYTES + 1);
    let total = 0;
    while (total < buffer.length) {
      const requested = Math.min(PACKAGE_JSON_READ_CHUNK_BYTES, buffer.length - total);
      const read = fileOps.readSync(descriptor, buffer, total, requested, total);
      if (read === 0) break;
      if (!Number.isSafeInteger(read) || read < 0 || read > requested) {
        throw new Error(`Release version consistency check failed: ${sourceName} returned an invalid read length`);
      }
      total += read;
    }

    const descriptorAfter = fileOps.fstatSync(descriptor, { bigint: true });
    const pathAfter = fileOps.lstatSync(filePath, { bigint: true });
    if (pathAfter.isSymbolicLink() || !pathAfter.isFile()) {
      throw new Error(`Release version consistency check failed: ${sourceName} path was replaced while being read`);
    }
    if (snapshotChanged(descriptorBefore, descriptorAfter)) {
      throw new Error(`Release version consistency check failed: ${sourceName} descriptor changed while being read`);
    }
    if (snapshotChanged(descriptorAfter, pathAfter)) {
      throw new Error(`Release version consistency check failed: ${sourceName} path identity changed while being read`);
    }
    if (total > MAX_PACKAGE_JSON_BYTES) {
      throw new Error(
        `Release version consistency check failed: ${sourceName} exceeds ${MAX_PACKAGE_JSON_BYTES} bytes while being read`,
      );
    }
    if (BigInt(total) !== BigInt(descriptorAfter.size)) {
      throw new Error(
        `Release version consistency check failed: ${sourceName} read size ${total} does not match `
        + `descriptor size ${descriptorAfter.size}`,
      );
    }
    return buffer.subarray(0, total).toString("utf8");
  } finally {
    if (descriptor !== undefined) fileOps.closeSync(descriptor);
  }
}

function skipJsonWhitespace(source, start) {
  let index = start;
  while (index < source.length && /[\t\n\r ]/.test(source[index])) index += 1;
  return index;
}

function jsonStringEnd(source, start, sourceName) {
  if (source[start] !== '"') {
    throw new Error(`Release version consistency check failed: ${sourceName} contains an invalid object key`);
  }
  for (let index = start + 1; index < source.length; index += 1) {
    if (source[index] === "\\") {
      index += 1;
      continue;
    }
    if (source[index] === '"') return index + 1;
  }
  throw new Error(`Release version consistency check failed: ${sourceName} contains an unterminated object key`);
}

function topLevelJsonMemberNames(source, sourceName) {
  if (Buffer.byteLength(source, "utf8") > MAX_PACKAGE_JSON_BYTES) {
    throw new Error(
      `Release version consistency check failed: ${sourceName} exceeds ${MAX_PACKAGE_JSON_BYTES} bytes`,
    );
  }
  const parsed = JSON.parse(source);
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    const actualType = parsed === null ? "null" : (Array.isArray(parsed) ? "array" : typeof parsed);
    throw new Error(
      `Release version consistency check failed: ${sourceName} root must be an object; actual type=${actualType}`,
    );
  }

  const names = [];
  let index = skipJsonWhitespace(source, 0);
  if (source[index] !== "{") {
    throw new Error(`Release version consistency check failed: ${sourceName} root must be an object`);
  }
  index += 1;
  while (index < source.length) {
    index = skipJsonWhitespace(source, index);
    if (source[index] === "}") return { names, parsed };
    const keyEnd = jsonStringEnd(source, index, sourceName);
    names.push(JSON.parse(source.slice(index, keyEnd)));
    index = skipJsonWhitespace(source, keyEnd);
    if (source[index] !== ":") {
      throw new Error(`Release version consistency check failed: ${sourceName} contains an invalid object member`);
    }
    index += 1;

    let nestedDepth = 0;
    while (index < source.length) {
      const token = source[index];
      if (token === '"') {
        index = jsonStringEnd(source, index, sourceName);
        continue;
      }
      if (token === "{" || token === "[") {
        nestedDepth += 1;
      } else if (token === "}" && nestedDepth === 0) {
        return { names, parsed };
      } else if (token === "}" || token === "]") {
        nestedDepth -= 1;
      } else if (token === "," && nestedDepth === 0) {
        index += 1;
        break;
      }
      index += 1;
    }
  }
  throw new Error(`Release version consistency check failed: ${sourceName} has no closing object delimiter`);
}

function readReleaseVersions(repositoryRoot, { packageFileOps = fs } = {}) {
  const canonicalSource = "src/aiworkhub/_version.py";
  const packageSource = "vscode-extension/package.json";
  const extensionSource = "vscode-extension/extension.js EXPECTED_MCP_PACKAGE_VERSION";
  const canonicalVersion = readVersionLiteral(
    path.join(repositoryRoot, "src", "aiworkhub", "_version.py"),
    canonicalSource,
    PYTHON_VERSION_LITERAL,
  );
  const packagePath = path.join(repositoryRoot, "vscode-extension", "package.json");
  const packageSourceText = readStableBoundedTextFile(packagePath, packageSource, packageFileOps);
  const { names: packageMemberNames, parsed: packageJson } = topLevelJsonMemberNames(
    packageSourceText,
    packageSource,
  );
  const versionMemberCount = packageMemberNames.filter((name) => name === "version").length;
  if (versionMemberCount === 0) {
    throw new Error(
      `Release version consistency check failed: ${packageSource} version must be a primitive string `
      + "and appear exactly once as a top-level member; actual type=undefined value=missing; "
      + "actual top-level member count=0",
    );
  }
  if (versionMemberCount !== 1) {
    throw new Error(
      `Release version consistency check failed: ${packageSource} must contain exactly one top-level version member; `
      + `actual count=${versionMemberCount}`,
    );
  }
  const packageVersion = packageJson.version;
  if (typeof packageVersion !== "string") {
    const actualType = packageVersion === null
      ? "null"
      : (Array.isArray(packageVersion) ? "array" : typeof packageVersion);
    const actualValue = packageVersion === undefined ? "missing" : JSON.stringify(packageVersion);
    throw new Error(
      `Release version consistency check failed: ${packageSource} version must be a primitive string; `
      + `actual type=${actualType} value=${actualValue}`,
    );
  }
  if (!RELEASE_VERSION.test(packageVersion)) {
    throw new Error(
      `Release version consistency check failed: ${packageSource} has invalid version ${JSON.stringify(packageVersion)}`,
    );
  }
  const extensionVersion = readVersionLiteral(
    path.join(repositoryRoot, "vscode-extension", "extension.js"),
    extensionSource,
    EXTENSION_VERSION_LITERAL,
  );
  return {
    canonicalSource,
    canonicalVersion,
    packageSnapshot: {
      json: packageJson,
      source: packageSourceText,
    },
    projections: {
      [packageSource]: packageVersion,
      [extensionSource]: extensionVersion,
    },
  };
}

function assertReleaseVersionConsistency(
  repositoryRoot = path.resolve(root, ".."),
  options = {},
) {
  const versions = readReleaseVersions(repositoryRoot, options);
  const mismatches = Object.entries(versions.projections)
    .filter(([, actual]) => actual !== versions.canonicalVersion);
  if (mismatches.length > 0) {
    const actuals = mismatches.map(([source, actual]) => `${source}=${actual}`).join(", ");
    throw new Error(
      `Release version consistency check failed: expected ${versions.canonicalSource}=${versions.canonicalVersion}; actual ${actuals}`,
    );
  }
  return versions;
}

function packageWithVersionGate({
  repositoryRoot = path.resolve(root, ".."),
  packageAction = buildVsix,
  packageFileOps = fs,
} = {}) {
  const versions = assertReleaseVersionConsistency(repositoryRoot, { packageFileOps });
  const result = packageAction(versions.packageSnapshot);
  return { versions, result };
}

// VSIX is a regular ZIP container.  Build it with Node primitives so a clean
// Windows host does not need a separately-installed `zip` executable.  Stored
// entries are deliberate: the two native launcher binaries are already dense,
// while a dependency-free writer keeps local and CI packaging identical.
const CRC32_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
    }
    table[index] = value >>> 0;
  }
  return table;
})();

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) crc = CRC32_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function collectFiles(directory, prefix = "") {
  const files = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })
    .sort((left, right) => left.name.localeCompare(right.name))) {
    if (entry.name === ".DS_Store") continue;
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...collectFiles(absolute, relative));
    else if (entry.isFile()) files.push({ absolute, relative });
  }
  return files;
}

function writePortableZip(sourceDirectory, destination) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;
  for (const file of collectFiles(sourceDirectory)) {
    const name = Buffer.from(file.relative.replaceAll(path.sep, "/"), "utf8");
    const data = fs.readFileSync(file.absolute);
    const checksum = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6); // UTF-8 file names
    local.writeUInt16LE(0, 8); // stored, no compression
    local.writeUInt16LE(0, 10); // deterministic DOS time/date
    local.writeUInt16LE(0x0021, 12);
    local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(name.length, 26);
    localParts.push(local, name, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt16LE(0, 12);
    central.writeUInt16LE(0x0021, 14);
    central.writeUInt32LE(checksum, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, name);
    offset += local.length + name.length + data.length;
  }
  const centralSize = centralParts.reduce((size, part) => size + part.length, 0);
  const entryCount = collectFiles(sourceDirectory).length;
  if (entryCount > 0xffff || offset > 0xffffffff || centralSize > 0xffffffff) {
    throw new Error("VSIX exceeds the supported non-ZIP64 package bounds");
  }
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entryCount, 8);
  end.writeUInt16LE(entryCount, 10);
  end.writeUInt32LE(centralSize, 12);
  end.writeUInt32LE(offset, 16);
  fs.writeFileSync(destination, Buffer.concat([...localParts, ...centralParts, end]));
}

function copyFile(rel, packageSourceText) {
  const target = path.join(extensionDir, rel);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  if (rel === "package.json") {
    fs.writeFileSync(target, packageSourceText, "utf8");
    return;
  }
  // README.md is the one bundled asset that lives one directory up (this
  // extension shares this repo's top-level README.md as its packaged
  // description rather than duplicating a second copy inside
  // vscode-extension/) -- fall back to it only when no local override exists.
  const localPath = path.join(root, rel);
  const source = rel === "README.md" && !fs.existsSync(localPath) ? path.join(root, "..", rel) : localPath;
  fs.writeFileSync(target, fs.readFileSync(source));
}

function copyPythonRuntime(srcDir, destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  for (const entry of fs.readdirSync(srcDir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (PY_RUNTIME_SKIP_DIRS.has(entry.name)) continue;
      copyPythonRuntime(path.join(srcDir, entry.name), path.join(destDir, entry.name));
      continue;
    }
    if (!entry.isFile()) continue;
    if (PY_RUNTIME_SKIP_FILE_SUFFIXES.some((suffix) => entry.name.endsWith(suffix))) continue;
    fs.writeFileSync(path.join(destDir, entry.name), fs.readFileSync(path.join(srcDir, entry.name)));
  }
}

function buildVsix(packageSnapshot) {
const pkg = packageSnapshot.json;
const out = path.join(dist, `${pkg.name}-${pkg.version}.vsix`);
fs.rmSync(staging, { recursive: true, force: true });
fs.rmSync(out, { force: true });
fs.mkdirSync(extensionDir, { recursive: true });

for (const rel of [
  "package.json",
  "README.md",
  "CHANGELOG.md",
  "extension.js",
  "runtime-retention.js",
  "media/app.js",
  "media/app.css",
  "media/aiworkhub-icon.png",
  "media/aiworkhub-marketplace-icon.svg",
  "media/aiworkhub-marketplace-icon.png",
  "media/aiworkhub-activity.svg",
  // The README hero must ship as a raster image: VS Code rejects SVG as an
  // image source on the extension details page. The .svg remains the
  // editable master and is bundled alongside it.
  "media/aiworkhub-hero.svg",
  "media/aiworkhub-hero.png",
]) {
  copyFile(rel, packageSnapshot.source);
}

if (!fs.existsSync(path.join(PY_RUNTIME_SRC, "__init__.py"))) {
  throw new Error(`aiworkhub Python package not found at ${PY_RUNTIME_SRC}`);
}
copyPythonRuntime(PY_RUNTIME_SRC, PY_RUNTIME_DEST);
fs.mkdirSync(path.dirname(MUX_LAUNCHER_DEST), { recursive: true });
fs.copyFileSync(MUX_LAUNCHER_SRC, MUX_LAUNCHER_DEST);
fs.copyFileSync(MUX_LAUNCHER_CMD_SRC, MUX_LAUNCHER_CMD_DEST);
for (const [goarch, folder] of [["amd64", "windows-x86_64"], ["arm64", "windows-aarch64"]]) {
  const nativeDest = path.join(extensionDir, "bin", folder, "aiworkhub-app-server-mux.exe");
  fs.mkdirSync(path.dirname(nativeDest), { recursive: true });
  childProcess.execFileSync("go", ["build", "-trimpath", "-ldflags=-s -w", "-o", nativeDest, NATIVE_LAUNCHER_SRC], {
    cwd: root,
    env: { ...process.env, GOOS: "windows", GOARCH: goarch, CGO_ENABLED: "0" },
    stdio: "inherit",
  });
  if (!fs.statSync(nativeDest).isFile() || fs.statSync(nativeDest).size < 1024) {
    throw new Error(`native Windows mux launcher is missing or invalid: ${folder}`);
  }
}
if (process.platform !== "win32") {
  fs.chmodSync(MUX_LAUNCHER_DEST, 0o755);
}
if (!fs.existsSync(path.join(PY_RUNTIME_DEST, "server.py"))) {
  throw new Error("bundled aiworkhub runtime is missing server.py after copy");
}
if (!fs.existsSync(path.join(PY_RUNTIME_DEST, "callback_store.py"))) {
  // B859: the package-local callback outbox/batch store must always ship --
  // without it CallbackBridge falls back to importing AITools/taskdb.py,
  // which does not exist in a packaged VSIX runtime.
  throw new Error("bundled aiworkhub runtime is missing callback_store.py after copy");
}
if (!fs.existsSync(path.join(PY_RUNTIME_DEST, "dashboard_static", "index.html"))) {
  throw new Error("bundled aiworkhub runtime is missing dashboard_static assets after copy");
}
{
  const muxStat = fs.statSync(MUX_LAUNCHER_DEST);
  if (!muxStat.isFile()) {
    throw new Error("bundled App Server mux launcher is missing or not a regular file");
  }
  if (process.platform !== "win32" && !(muxStat.mode & 0o111)) {
    throw new Error("bundled App Server mux launcher is missing or not executable");
  }
}
{
  const cmdStat = fs.statSync(MUX_LAUNCHER_CMD_DEST);
  if (!cmdStat.isFile()) {
    throw new Error("bundled App Server mux .cmd launcher is missing or not a regular file");
  }
}

const manifest = `<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="${pkg.name}" Version="${pkg.version}" Publisher="${pkg.publisher}"/>
    <DisplayName>${pkg.displayName}</DisplayName>
    <Description xml:space="preserve">${pkg.description}</Description>
    <Tags>${(pkg.keywords || []).join(",")}</Tags>
    <Categories>${(pkg.categories || []).join(",")}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="${pkg.engines.vscode}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace"/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="extension/${pkg.icon}" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Changelog" Path="extension/CHANGELOG.md" Addressable="true"/>
  </Assets>
</PackageManifest>
`;

const contentTypes = `<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="css" ContentType="text/css"/>
  <Default Extension="md" ContentType="text/markdown"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="svg" ContentType="image/svg+xml"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="py" ContentType="text/x-python"/>
  <Default Extension="html" ContentType="text/html"/>
  <Default Extension="exe" ContentType="application/octet-stream"/>
</Types>
`;

fs.writeFileSync(path.join(staging, "extension.vsixmanifest"), manifest, "utf8");
fs.writeFileSync(path.join(staging, "[Content_Types].xml"), contentTypes, "utf8");

writePortableZip(staging, out);
fs.rmSync(staging, { recursive: true, force: true });
console.log(out);
return out;
}

if (require.main === module) {
  packageWithVersionGate();
}

module.exports = {
  MAX_PACKAGE_JSON_BYTES,
  PACKAGE_JSON_READ_CHUNK_BYTES,
  assertReleaseVersionConsistency,
  packageWithVersionGate,
  readStableBoundedTextFile,
};
