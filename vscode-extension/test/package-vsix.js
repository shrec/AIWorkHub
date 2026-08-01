const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const dist = path.join(root, "dist");
const staging = path.join(dist, "vsix-staging");
const extensionDir = path.join(staging, "extension");
const out = path.join(dist, `${pkg.name}-${pkg.version}.vsix`);

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

function copyFile(rel) {
  const target = path.join(extensionDir, rel);
  fs.mkdirSync(path.dirname(target), { recursive: true });
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

fs.rmSync(staging, { recursive: true, force: true });
fs.rmSync(out, { force: true });
fs.mkdirSync(extensionDir, { recursive: true });

for (const rel of [
  "package.json",
  "README.md",
  "extension.js",
  "runtime-retention.js",
  "media/app.js",
  "media/app.css",
  "media/aiworkhub-icon.png",
  "media/aiworkhub-activity.svg",
  // The README hero must ship as a raster image: VS Code rejects SVG as an
  // image source on the extension details page. The .svg remains the
  // editable master and is bundled alongside it.
  "media/aiworkhub-hero.svg",
  "media/aiworkhub-hero.png",
]) {
  copyFile(rel);
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
