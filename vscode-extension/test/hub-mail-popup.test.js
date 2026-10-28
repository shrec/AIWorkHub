"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

// ---- static contract: markup, bridge tools, message types, validation
for (const marker of [
  'id="header-mail"',
  'id="mail-dialog"',
  'id="mail-inbox-list"',
  'id="mail-peers-list"',
  'id="mail-detail-panel"',
  'id="mail-refresh"',
]) assert.ok(extension.includes(marker), `missing WorkHub Mail UI marker: ${marker}`);

for (const tool of [
  "aiworkhub_dashboard_hub_mail",
  "aiworkhub_dashboard_hub_mail_detail",
]) assert.ok(extension.includes(`"${tool}"`), `missing WorkHub Mail bridge tool: ${tool}`);

assert.ok(extension.includes('"requestHubMail"'));
assert.ok(extension.includes('"requestHubMailDetail"'));
assert.ok(extension.includes('hubMail: "hubMail"'));
assert.ok(extension.includes('hubMailDetail: "hubMailDetail"'));
assert.ok(extension.includes("const MESSAGE_ID_RE = /^[0-9a-f]{32}$/;"));
const detailCase = extension.slice(extension.indexOf('case "requestHubMailDetail"'));
assert.ok(
  /^case "requestHubMailDetail": \{\s*const messageId = String\(message\.messageId \|\| ""\);\s*if \(MESSAGE_ID_RE\.test\(messageId\)\) pushHubMailDetail\(view, messageId\);/.test(detailCase),
  "the detail request must be validated with MESSAGE_ID_RE before any bridge call",
);
assert.ok(css.includes("#header-mail:hover"));

assert.ok(app.includes('type: "requestHubMail"'));
assert.ok(app.includes('type: "requestHubMailDetail"'));
assert.ok(app.includes('elements.headerMail.addEventListener("click", openHubMailDialog)'));

// The webview never touches mail storage; everything comes from the bridge.
// Storage names are matched only as path/string literals, never as payload
// property reads such as payload.peers or value.machine_id.
for (const forbidden of [
  /\.aiworkhub[\\/]mail/,
  /\bmail_root\b/,
  /local\.key/,
  /["'`\/\\]\.listener\.json/,
  /["'`\/\\]\.peers\b/,
  /["'`\/\\]\.machine_id\b/,
  /["'`\/\\]\.node\.json/,
  /["'`\/\\]\.pairing\.json/,
  /["'`\/\\]inbox[\\/]/,
]) {
  assert.ok(!forbidden.test(app), `Webview must not read mail storage directly: ${forbidden}`);
}
// The popup is read-only: no send/archive path exists in the Webview.
assert.ok(!/hub_mail_(send|archive)/.test(app), "read-only popup must not mutate mail state");

// Fetched on open/refresh only, never from the periodic snapshot.
const callSites = app.split("\n").filter((line) => /requestHubMail\b(?!Detail)/.test(line) && !/function requestHubMail\(/.test(line) && !/type: "requestHubMail"/.test(line));
assert.deepStrictEqual(callSites.map((line) => line.trim()), [
  "requestHubMail();",
  'elements.mailRefresh.addEventListener("click", requestHubMail);',
  'elements.mailIncludeArchived.addEventListener("change", requestHubMail);',
], "WorkHub Mail must only be requested on dialog open, refresh or the archived toggle");
const openFn = app.slice(app.indexOf("function openHubMailDialog()"), app.indexOf("elements.headerMail.addEventListener"));
assert.ok(openFn.includes("requestHubMail();"), "the dialog open must request the data");
const hostSnapshot = extension.slice(extension.indexOf("case \"requestHubMail\":"), extension.indexOf("case \"requestHubMailDetail\""));
assert.ok(!/snapshot/i.test(hostSnapshot), "the host handler must not ride on the snapshot");

// ---- the rendering block never uses an HTML sink
const begin = app.indexOf("// WorkHub Mail popup (read-only).");
const end = app.indexOf("// End WorkHub Mail popup.");
assert.ok(begin > 0 && end > begin, "WorkHub Mail render block markers missing");
const block = app.slice(begin, end);
for (const sink of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "setAttribute("]) {
  assert.ok(!block.includes(sink), `WorkHub Mail rendering must not use ${sink}`);
}
const openBlock = app.slice(app.indexOf("// WorkHub Mail: fetched when"), app.indexOf("elements.mailInboxList.addEventListener"));
assert.ok(!openBlock.includes("innerHTML"));

// ---- behavioural harness: untrusted text lands in textContent verbatim
function sliceFunction(source, name, next) {
  const start = source.indexOf(`function ${name}(`);
  const stop = source.indexOf(`function ${next}(`, start);
  assert.ok(start >= 0 && stop > start, `cannot extract ${name}`);
  return source.slice(start, stop);
}

class MockNode {
  constructor(tag) {
    this.tagName = String(tag || "#fragment").toUpperCase();
    this.children = [];
    this.className = "";
    this.dataset = {};
    this._text = "";
    this.open = false;
  }
  set innerHTML(_value) { throw new Error("innerHTML is forbidden in the WorkHub Mail popup"); }
  get innerHTML() { throw new Error("innerHTML is forbidden in the WorkHub Mail popup"); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  append(...nodes) { for (const node of nodes) this.appendChild(node); }
  appendChild(node) {
    if (node.tagName === "#FRAGMENT") this.children.push(...node.children);
    else this.children.push(node);
    return node;
  }
  replaceChildren(...nodes) { this.children = []; this._text = ""; this.append(...nodes); }
  descendants() { return this.children.flatMap((child) => [child, ...child.descendants()]); }
}

const posted = [];
const elements = {
  mailSummary: new MockNode("span"),
  mailListener: new MockNode("span"),
  mailIncludeArchived: Object.assign(new MockNode("input"), { checked: false }),
  mailInboxList: new MockNode("div"),
  mailPeersList: new MockNode("div"),
  mailDetailPanel: new MockNode("section"),
};
const sandbox = {
  elements,
  vscode: { postMessage: (message) => posted.push(message) },
  document: {
    createElement: (tag) => new MockNode(tag),
    createDocumentFragment: () => new MockNode("#fragment"),
  },
  formatCount: (value) => String(Number(value) || 0),
};
vm.createContext(sandbox);
vm.runInContext(
  [
    sliceFunction(app, "createElement", "asArray"),
    sliceFunction(app, "asArray", "numberValue"),
    block,
    "this.renderHubMail = renderHubMail; this.renderHubMailDetail = renderHubMailDetail; this.requestHubMail = requestHubMail;",
  ].join("\n"),
  sandbox,
);

const hostile = '<img src=x onerror="alert(1)">';
const peerA = "a1b2c3d4" + "0".repeat(24);
const peerB = "e5f6a7b8" + "1".repeat(24);
sandbox.renderHubMail({
  ok: true,
  hub: { hub_id: "repo_" + "ab12cd34" + "f".repeat(24), repo_name: "geoai" },
  node: { machine_id: "9".repeat(32), machine_label: "workstation" },
  total: 1,
  invalid_count: 2,
  quarantined_count: 3,
  scan_truncated: true,
  unscanned_count: 4,
  outbound: { queued: 5, delivery_failed: 6 },
  mails: [{
    untrusted_data: true,
    message_id: "c".repeat(32),
    kind: "note",
    subject: hostile,
    created_at: "2026-10-28T00:00:00Z",
    from_hub: { hub_id: "repo_" + "12345678" + "e".repeat(24), repo_name: hostile },
    from_node: { machine_id: peerA, machine_label: hostile },
    attachment_count: 1,
  }],
  peers: [
    { machine_id: peerA, machine_label: "lab", address: "10.0.0.2:8765", last_seen: "2026-10-27T00:00:00Z",
      hubs: [{ hub_id: "repo_" + "deadbeef" + "0".repeat(24), repo_name: "geoai" }], tls_pinned: true },
    { machine_id: peerB, machine_label: "lab", address: "10.0.0.3:8765", last_seen: "",
      hubs: [{ hub_id: "repo_" + "cafebabe" + "0".repeat(24), repo_name: "geoai" }], tls_pinned: false },
  ],
  listener: { state: "running", bind: "0.0.0.0:8765", pid: 4242 },
});

const inboxText = elements.mailInboxList.textContent;
assert.ok(inboxText.includes(hostile), "hostile subject must be rendered verbatim as text");
assert.ok(inboxText.includes(`${hostile} · 12345678`), "a hub renders as repo_name plus 8-hex repo_id prefix");
assert.ok(inboxText.includes(`${hostile} · a1b2c3d4`), "a node renders as machine_label plus 8-hex machine_id prefix");
const [mailButton] = elements.mailInboxList.children;
assert.strictEqual(mailButton.dataset.mailId, "c".repeat(32));
assert.ok(elements.mailInboxList.descendants().every((node) => node.tagName !== "IMG"), "no element may be built from mail text");

const peerRows = elements.mailPeersList.children;
assert.strictEqual(peerRows.length, 2, "two peers with the same label are two distinct rows");
assert.ok(peerRows[0].textContent.includes("lab · a1b2c3d4"));
assert.ok(peerRows[1].textContent.includes("lab · e5f6a7b8"));
assert.ok(peerRows[0].textContent.includes("geoai · deadbeef"));
assert.ok(peerRows[1].textContent.includes("geoai · cafebabe"));
assert.ok(peerRows[0].textContent.includes("last seen 2026-10-27T00:00:00Z"));

const summary = elements.mailSummary.textContent;
for (const part of ["geoai · ab12cd34", "workstation · 99999999", "2 invalid", "3 quarantined", "5 queued", "6 delivery failed", "4 unscanned"]) {
  assert.ok(summary.includes(part), `summary missing ${part}: ${summary}`);
}
assert.ok(elements.mailListener.textContent.includes("running on 0.0.0.0:8765"));

sandbox.renderHubMail({ ok: true, mails: [], peers: [], listener: { state: "stopped" } });
assert.strictEqual(elements.mailListener.textContent, "Listener: stopped");
assert.ok(elements.mailPeersList.textContent.includes("No paired machines"));

sandbox.renderHubMailDetail({
  ok: true,
  untrusted_data: true,
  mail: {
    untrusted_data: true, message_id: "c".repeat(32), kind: "note", subject: "s", body: hostile,
    from_hub: { hub_id: "repo_" + "12345678" + "e".repeat(24), repo_name: "geoai" },
    attachments: [{ name: hostile, text: hostile }],
  },
});
const detail = elements.mailDetailPanel;
assert.ok(detail.textContent.includes(hostile));
assert.ok(detail.descendants().some((node) => node.tagName === "PRE" && node.textContent === hostile), "body is a text-only <pre>");

posted.length = 0;
sandbox.requestHubMail();
assert.deepStrictEqual(JSON.parse(JSON.stringify(posted)), [{ type: "requestHubMail", includeArchived: false }]);

console.log("WorkHub Mail popup contract verified");
