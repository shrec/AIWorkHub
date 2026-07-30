"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(ROOT, "..");

// ---------------------------------------------------------------------------
// Static validation of the .cmd Windows launcher content
// ---------------------------------------------------------------------------
function testCmdSemantics() {
  const cmd = fs.readFileSync(
    path.join(REPO_ROOT, "scripts", "aiworkhub-app-server-mux.cmd"),
    "utf8"
  ).replace(/\r\n/g, "\n");

  // preamble
  if (!cmd.includes("@echo off\nsetlocal")) {
    throw new Error(".cmd must begin with @echo off and setlocal");
  }

  // runtime-first PYTHONPATH
  if (
    !cmd.includes(
      'if exist "%ROOT%\\runtime\\aiworkhub\\app_server_mux.py"'
    )
  ) {
    throw new Error(".cmd must check runtime path first");
  }
  if (
    !cmd.includes(
      'set "PYTHONPATH=%ROOT%\\runtime;%PYTHONPATH%"'
    )
  ) {
    throw new Error('.cmd must prepend runtime to PYTHONPATH');
  }

  // dev src fallback
  if (
    !cmd.includes(
      'if exist "%ROOT%\\src\\aiworkhub\\app_server_mux.py"'
    )
  ) {
    throw new Error(".cmd must fall back to src path");
  }
  if (
    !cmd.includes(
      'set "PYTHONPATH=%ROOT%\\src;%PYTHONPATH%"'
    )
  ) {
    throw new Error('.cmd must prepend src to PYTHONPATH');
  }

  // explicit error + exit /b 1 for missing runtime
  if (!cmd.includes("AIWorkHub mux runtime is missing")) {
    throw new Error(".cmd must emit error message when runtime is missing");
  }
  if (!cmd.includes("exit /b 1")) {
    throw new Error(".cmd must exit /b 1 when runtime is missing");
  }

  // python -m forward and ERRORLEVEL capture + propagation
  if (!cmd.includes("python -m aiworkhub.app_server_mux %*")) {
    throw new Error(".cmd must forward all args via python -m");
  }
  if (!cmd.includes('set "EXIT_CODE=%ERRORLEVEL%"')) {
    throw new Error(".cmd must capture ERRORLEVEL immediately");
  }
  if (!cmd.includes("endlocal & exit /b %EXIT_CODE%")) {
    throw new Error(".cmd must propagate exit code through endlocal");
  }

  console.log("PASS: .cmd launcher semantics");
}

// ---------------------------------------------------------------------------
// Static validation of the strict packaging logic (no dist created)
// ---------------------------------------------------------------------------
function testStrictPackagingLogic() {
  const pkgVsix = fs.readFileSync(
    path.join(ROOT, "test", "package-vsix.js"),
    "utf8"
  );

  // Must copy both launchers
  if (!pkgVsix.includes("MUX_LAUNCHER_CMD_SRC")) {
    throw new Error("package-vsix must reference .cmd launcher source path");
  }
  if (!pkgVsix.includes("MUX_LAUNCHER_CMD_DEST")) {
    throw new Error("package-vsix must reference .cmd launcher dest path");
  }
  if (!pkgVsix.includes("fs.copyFileSync(MUX_LAUNCHER_CMD_SRC, MUX_LAUNCHER_CMD_DEST)")) {
    throw new Error("package-vsix must copy .cmd launcher into extension/bin");
  }

  // Must use fs.statSync with isFile() — not fs.existsSync alone
  if (!pkgVsix.includes(".isFile()")) {
    throw new Error(
      "package-vsix must use statSync + isFile for launcher validation (not existsSync)"
    );
  }

  // Non-Windows must chmod + check executable bit
  if (!pkgVsix.includes('if (process.platform !== "win32")')) {
    throw new Error("package-vsix must guard chmod/mode checks behind platform !== win32");
  }

  // Must throw hard on missing launcher (no swallow)
  if (!pkgVsix.includes('throw new Error("bundled App Server mux launcher is missing or not a regular file")')) {
    throw new Error("package-vsix must throw on missing POSIX launcher");
  }
  if (!pkgVsix.includes('throw new Error("bundled App Server mux .cmd launcher is missing or not a regular file")')) {
    throw new Error("package-vsix must throw on missing .cmd launcher");
  }

  // The old existsSync check must be gone
  const oldPattern =
    "fs.existsSync(MUX_LAUNCHER_DEST) || !(fs.statSync(MUX_LAUNCHER_DEST).mode & 0o111)";
  if (pkgVsix.includes(oldPattern)) {
    throw new Error("old existsSync-based check must be removed");
  }

  console.log("PASS: strict packaging logic (no-swallow, isFile)");
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------
function main() {
  testCmdSemantics();
  testStrictPackagingLogic();
  console.log("PASS: mux-launcher-packaging-b1038 — all static validations passed");
}

main();
