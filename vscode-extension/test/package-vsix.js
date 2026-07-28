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
// Never bundle bytecode caches or OS cruft -- only the real package source
// and its data assets (e.g. dashboard_static/*.css/.js/.html).
const PY_RUNTIME_SKIP_DIRS = new Set(["__pycache__", ".pytest_cache"]);
const PY_RUNTIME_SKIP_FILE_SUFFIXES = [".pyc", ".pyo", ".DS_Store"];

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
  "media/app.js",
  "media/app.css",
  "media/aiworkhub-icon.png",
  "media/aiworkhub-activity.svg",
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
</Types>
`;

fs.writeFileSync(path.join(staging, "extension.vsixmanifest"), manifest, "utf8");
fs.writeFileSync(path.join(staging, "[Content_Types].xml"), contentTypes, "utf8");

childProcess.execFileSync("zip", ["-qr", out, ".", "-x", "*.DS_Store"], {
  cwd: staging,
  stdio: "inherit",
});
fs.rmSync(staging, { recursive: true, force: true });
console.log(out);
