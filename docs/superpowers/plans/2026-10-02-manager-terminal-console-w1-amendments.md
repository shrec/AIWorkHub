# Manager Terminal Console W1 — measured amendments

Amends `docs/superpowers/plans/2026-09-27-manager-terminal-console-w1.md`. Every line number below
was measured on commit `c1597aa`; where this file and the plan disagree, this file wins. Each edit
was applied in a scratch copy of the repository and the tests named here were run against it.

## W1-U1 — console renderer extraction (plan lines 1226-1345)

A pure move: no behaviour, markup, class name or style value changes. All line numbers are the
numbers of the unmodified file at `c1597aa`, so apply each file's edits bottom-up.

### What the plan missed

- `media/app.js` calls `renderManagerChatEvents()` while it loads (`startManagerChatSidebar()`), so every
  harness that loads `app.js` alone breaks once the function lives in another file. Five harnesses do:
  three Node tests through `require`, `context-viewers.test.js` through `vm.runInContext`, and the
  Node script embedded in `tests/test_live_output_poll_rearm.py`.
- `require` gives each file its own module scope, so `manager_console.js` could not see `state`,
  `elements` or `createElement`. The harnesses therefore evaluate both files as classic scripts in one
  global scope, in the order the webview loads them.
- Two overrides of the moved rules live outside the moved CSS block (`app.css` lines 3695 and 4052).
  They move too; otherwise the base rule in `manager_console.css`, which loads after `app.css`, would
  win over them.
- The four-line section comment at `app.js` 6971-6974 stays in `app.js`.
- The plan's validation runs `npm test`, which must not be run (NF-2026-01190). Node tests are run one
  file at a time, by the manager, on the host.

### 1. New file `vscode-extension/media/manager_console.js` (213 lines)

Create it before editing `app.js`. Content, in order: the eight header lines below (the eighth is
empty), then `app.js` lines 6975-7134, then `app.js` lines 7244-7288, unchanged.

```js
"use strict";

// Manager console block renderers (spec 2026-09-26 §3). Loaded before app.js;
// every app.js global these use (createElement, state, elements) is read at
// call time, never at load time. Every event payload is untrusted model/tool
// output: strings go through createElement's textContent or
// document.createTextNode, never innerHTML.
```

Verified recipe (run from the repository root, before any edit of `app.js`): write the eight header
lines, then append the two ranges:

```sh
sed -n '6975,7134p;7244,7288p' vscode-extension/media/app.js >> vscode-extension/media/manager_console.js
```

### 2. New file `vscode-extension/media/manager_console.css` (145 lines)

Create it before editing `app.css`. Content: `app.css` lines 3276-3412 unchanged, then exactly:

```css

@media (max-width: 620px) {
  .manager-chat-bubble { max-width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .manager-chat-thinking-dots span { animation: none; opacity: 0.8; }
}
```

(the first of those lines is empty; the file ends with one newline).

### 3. `vscode-extension/media/app.js` — delete two ranges

Delete lines 7244-7289 (`renderManagerChatEvents` and the empty line after it), then lines 6975-7134
(`managerChatEventNode` … `managerChatTurnEndNode` and the empty line after them). Nothing else changes;
the result has 8295 lines and line 6975 reads `function managerChatTaskStatus(task) {`.

### 4. `vscode-extension/media/app.css` — delete three ranges

Delete line 4052 (`  .manager-chat-thinking-dots span { animation: none; opacity: 0.8; }`), then line 3695
(`  .manager-chat-bubble { max-width: 100%; }`), then lines 3276-3413 (the moved block and the empty line
after it). The result has 4313 lines.

### 5. Literal edits of the remaining files

Unified diffs against `c1597aa`; apply them as written.

```diff
--- a/vscode-extension/extension.js
+++ b/vscode-extension/extension.js
@@ -11314,4 +11314,6 @@
   const scriptUri = dashboardAssetUri(webview, mediaUri, "app.js");
   const styleUri = dashboardAssetUri(webview, mediaUri, "app.css");
+  const consoleScriptUri = dashboardAssetUri(webview, mediaUri, "manager_console.js");
+  const consoleStyleUri = dashboardAssetUri(webview, mediaUri, "manager_console.css");
   const logoUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaUri, "aiworkhub-icon.png"));
   const nonceValue = nonce();
@@ -11336,4 +11338,5 @@
 <meta name="viewport" content="width=device-width, initial-scale=1">
 <link rel="stylesheet" href="${styleUri}">
+<link rel="stylesheet" href="${consoleStyleUri}">
 <title>AIWorkHub</title>
 </head>
@@ -11980,4 +11983,5 @@
   <div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
   <script nonce="${nonceValue}">${codingFoundationDashboardSource()}</script>
+  <script nonce="${nonceValue}" src="${consoleScriptUri}"></script>
   <script nonce="${nonceValue}" src="${scriptUri}"></script>
 </body>
```

```diff
--- a/vscode-extension/test/package-vsix.js
+++ b/vscode-extension/test/package-vsix.js
@@ -512,4 +512,6 @@
   "media/app.js",
   "media/app.css",
+  "media/manager_console.js",
+  "media/manager_console.css",
   "media/aiworkhub-icon.png",
   "media/aiworkhub-marketplace-icon.svg",
```

```diff
--- a/vscode-extension/test/manager-chat-panel.test.js
+++ b/vscode-extension/test/manager-chat-panel.test.js
@@ -13,4 +13,6 @@
 const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");
 const cssSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.css"), "utf8");
+const consoleSource = fs.readFileSync(path.join(__dirname, "..", "media", "manager_console.js"), "utf8");
+const consoleCss = fs.readFileSync(path.join(__dirname, "..", "media", "manager_console.css"), "utf8");
 
 // Extracts an exact shipped block (verbatim, not reimplemented) so assertions
@@ -399,5 +401,5 @@
   const managerChat = extractSlice(
     appSource,
-    "function managerChatEventNode(event) {",
+    "function managerChatTaskStatus(task) {",
     "  showManagerChatNotice(null);\n}",
     "manager chat render/poll functions",
@@ -480,5 +482,5 @@
   vm.createContext(context);
   vm.runInContext(
-    `"use strict";\n${utilities}\n${constants}\n${managerChat}\n${managerChatWiring}\n` +
+    `"use strict";\n${utilities}\n${constants}\n${consoleSource}\n${managerChat}\n${managerChatWiring}\n` +
       "this.api = { managerChatEventNode, renderManagerChatEvents, renderManagerChatEventsResponse, " +
       "renderManagerChatAction, renderManagerChatStatus, requestManagerChatEvents, scheduleManagerChatPoll, " +
@@ -1301,4 +1303,9 @@
     "manager chat CSS must reuse existing theme tokens, never a new colour literal",
   );
+  assert.doesNotMatch(
+    consoleCss,
+    /#[0-9a-fA-F]{3,8}\b/,
+    "manager console CSS must reuse existing theme tokens, never a new colour literal",
+  );
 });
 
@@ -1311,8 +1318,23 @@
   );
   assert.match(managerCss, /#manager-chat-model \{[^}]*field-sizing:\s*content/);
-  assert.match(managerCss, /\.manager-chat-tool-row > summary \{[^}]*overflow-wrap:\s*anywhere/);
-  assert.match(managerCss, /\.manager-chat-tool-row-body \{[^}]*overflow:\s*visible/);
-  assert.match(managerCss, /\.manager-chat-tool-row-body \{[^}]*max-height:\s*none/);
-  assert.doesNotMatch(managerCss, /text-overflow:\s*ellipsis/);
-  assert.doesNotMatch(managerCss, /line-clamp/);
-});
+  assert.match(consoleCss, /\.manager-chat-tool-row > summary \{[^}]*overflow-wrap:\s*anywhere/);
+  assert.match(consoleCss, /\.manager-chat-tool-row-body \{[^}]*overflow:\s*visible/);
+  assert.match(consoleCss, /\.manager-chat-tool-row-body \{[^}]*max-height:\s*none/);
+  for (const css of [managerCss, consoleCss]) {
+    assert.doesNotMatch(css, /text-overflow:\s*ellipsis/);
+    assert.doesNotMatch(css, /line-clamp/);
+  }
+});
+
+test("manager console assets load before app.js and ship in the VSIX", () => {
+  const html = extensionSource.slice(extensionSource.indexOf("function getHtmlForWebview("));
+  const consoleScript = html.indexOf('src="${consoleScriptUri}"');
+  const appScript = html.indexOf('src="${scriptUri}"');
+  assert.ok(consoleScript !== -1 && consoleScript < appScript, "console script must load before app.js");
+  assert.ok(html.indexOf('href="${styleUri}"') < html.indexOf('href="${consoleStyleUri}"'), "console css loads after app.css");
+  const packager = fs.readFileSync(path.join(__dirname, "package-vsix.js"), "utf8");
+  assert.match(packager, /"media\/manager_console\.js"/);
+  assert.match(packager, /"media\/manager_console\.css"/);
+  assert.doesNotMatch(appSource, /function managerChatEventNode\(/);
+  assert.doesNotMatch(cssSource, /\.manager-chat-bubble \{/);
+});
```

```diff
--- a/vscode-extension/test/context-viewers.test.js
+++ b/vscode-extension/test/context-viewers.test.js
@@ -468,4 +468,5 @@
   });
   const context = vm.createContext(sandbox);
+  vm.runInContext(fs.readFileSync(path.join(root, "media", "manager_console.js"), "utf8"), context, { filename: "manager_console.js" });
   vm.runInContext(app, context, { filename: "app.js" });
   const run = (code) => vm.runInContext(code, context);
```

```diff
--- a/vscode-extension/test/claude-stream-events.test.js
+++ b/vscode-extension/test/claude-stream-events.test.js
@@ -2,5 +2,7 @@
 
 const assert = require("assert");
+const fs = require("fs");
 const path = require("path");
+const vm = require("vm");
 
 class FakeElement {
@@ -97,5 +99,10 @@
   });
   global.Intl = Intl;
-  require(path.join(__dirname, "../media/app.js"));
+  // Two classic scripts in one global scope, in the order the webview loads them: app.js
+  // top-level bindings (state, elements, createElement) stay visible to manager_console.js.
+  for (const name of ["manager_console.js", "app.js"]) {
+    const file = path.join(__dirname, "../media", name);
+    vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
+  }
   return { api: global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__, document };
 }
```

```diff
--- a/vscode-extension/test/live-output-formatting.test.js
+++ b/vscode-extension/test/live-output-formatting.test.js
@@ -2,5 +2,7 @@
 
 const assert = require("assert");
+const fs = require("fs");
 const path = require("path");
+const vm = require("vm");
 
 class FakeElement {
@@ -123,5 +125,10 @@
   });
   global.Intl = Intl;
-  require(path.join(__dirname, "../media/app.js"));
+  // Two classic scripts in one global scope, in the order the webview loads them: app.js
+  // top-level bindings (state, elements, createElement) stay visible to manager_console.js.
+  for (const name of ["manager_console.js", "app.js"]) {
+    const file = path.join(__dirname, "../media", name);
+    vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
+  }
   return {
     api: global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__,
```

```diff
--- a/vscode-extension/test/provider-event-shapes.test.js
+++ b/vscode-extension/test/provider-event-shapes.test.js
@@ -11,5 +11,7 @@
 
 const assert = require("assert");
+const fs = require("fs");
 const path = require("path");
+const vm = require("vm");
 
 class FakeElement {
@@ -131,5 +133,10 @@
   });
   global.Intl = Intl;
-  require(path.join(__dirname, "../media/app.js"));
+  // Two classic scripts in one global scope, in the order the webview loads them: app.js
+  // top-level bindings (state, elements, createElement) stay visible to manager_console.js.
+  for (const name of ["manager_console.js", "app.js"]) {
+    const file = path.join(__dirname, "../media", name);
+    vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
+  }
   return { api: global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__, document };
 }
```

```diff
--- a/tests/test_live_output_poll_rearm.py
+++ b/tests/test_live_output_poll_rearm.py
@@ -97,5 +97,11 @@
 global.acquireVsCodeApi = () => ({ getState: () => ({}), setState: () => {}, postMessage: (m) => { posted.push(m); } });
 global.Intl = Intl;
-require(APP_JS);
+// Two classic scripts in one global scope, in the order the webview loads them: app.js
+// top-level bindings (state, elements, createElement) stay visible to manager_console.js.
+const fs = require("fs"); const path = require("path"); const vm = require("vm");
+for (const name of ["manager_console.js", "app.js"]) {
+  const file = path.join(path.dirname(APP_JS), name);
+  vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
+}
 const api = global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__;
 assert(api, "formatter/test hook exposed");
```

### 6. Expected result

`git hash-object <path>` of each finished file (line endings normalised by git):

| path | lines | blob |
| --- | --- | --- |
| `vscode-extension/media/manager_console.js` | 213 | `525856f0c7623665533fad7ced9c5ba623b29c14` |
| `vscode-extension/media/manager_console.css` | 145 | `4e5edc1a41b6608fcd1d8b8196d570f2308854f7` |
| `vscode-extension/media/app.js` | 8295 | `9499a7ea868576f6b634632a724151709e4c9301` |
| `vscode-extension/media/app.css` | 4313 | `57f5155d221809fd60bc15c45273b7eb34990ae2` |
| `vscode-extension/extension.js` | 12784 | `65f922aafd3ec839e14e697efe417bc6858a3089` |
| `vscode-extension/test/package-vsix.js` | 646 | `1d19c8c452a241e91d21fcca5a5832a48cc52f0d` |
| `vscode-extension/test/manager-chat-panel.test.js` | 1340 | `ba6e330fb2e10efcda0e888be76003e736917c7d` |
| `vscode-extension/test/context-viewers.test.js` | 1080 | `d7dc51b08879c7b27d8cbe32e8c599069d5b151d` |
| `vscode-extension/test/claude-stream-events.test.js` | 175 | `a3a257d007aaca31368142a60eeda444848dd55b` |
| `vscode-extension/test/live-output-formatting.test.js` | 516 | `1a313cab2ee6b28b66c9648593569bc9d5ad803a` |
| `vscode-extension/test/provider-event-shapes.test.js` | 213 | `31cbdcafec1bf0df475a1226f98efd066019970e` |
| `tests/test_live_output_poll_rearm.py` | 280 | `e763b2be79764d243f75958dd8588ebf0da1b558` |

### 7. Checks

- Worker, in the sandbox: `python -m pytest -q tests/test_live_output_poll_rearm.py tests/test_aiworkhub_vscode_release_b853.py`
  (the Node-driven cases skip where `node` is unavailable).
- Manager, on the host, before accept: every file under `vscode-extension/test/*.test.js` with
  `node --test <file>`, one file per run; the four Python files `tests/test_live_output_poll_rearm.py`,
  `tests/test_aiworkhub_vscode_live_output_b896.py`, `tests/test_aiworkhub_selected_task_live_output_b855.py`,
  `tests/test_aiworkhub_vscode_release_b853.py`; `npm run package` and a listing of the VSIX showing both
  new media files.
- Reproduction of the harness defect: the unmodified `claude-stream-events.test.js` against the moved
  code fails with `ReferenceError: renderManagerChatEvents is not defined`.
