"use strict";

// NF-2026-01731: an idle refresh asks for only the changed snapshot sections
// and falls back to one plain full request on any digest miss or merge error.

const assert = require("assert");
const Module = require("module");
const path = require("path");
const test = require("node:test");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const originalLoad = Module._load;
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: () => 10000, inspect: () => ({}) }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
  },
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((part) => part.fsPath || part).join("/") }) },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};

Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};

let extension;
try {
  extension = require(extensionPath);
} finally {
  Module._load = originalLoad;
}

const internals = extension.__testInternals;

// Pinned in tests/test_dashboard_idle_burn_nf01731.py: the server's
// _snapshot_section_digest must agree byte-for-byte.
const CROSS_LANGUAGE_SAMPLE = { b: [1, 2.5, "é ✓", null, true], a: { z: 1, y: "x\n\"q\"" }, n: 3 };
const CROSS_LANGUAGE_DIGEST = "ad9978065bfeb578a6a0c5ec2b201e594d04cc018d28635ea5ba304b9feddf32";

function fullSnapshot(overrides = {}) {
  return {
    schema_version: 1,
    snapshot_mode: "full",
    status_counts: { pending: 1, active: 1 },
    tasks: { pending: [{ task_id: "T-NF01731" }] },
    agent_processes: { processes: [{ request_id: "r1" }, { request_id: "r2" }] },
    ...overrides,
  };
}

function digestsOf(payload) {
  const digests = {};
  for (const key of Object.keys(payload)) digests[key] = internals.snapshotSectionDigest(payload[key]);
  return digests;
}

// A server double that speaks the delta channel the way snapshot_view does.
function makeClient({ converged = true, reply } = {}) {
  const calls = [];
  const state = { current: fullSnapshot() };
  const client = {
    repositoryIdentity: { uriStr: "file:///work/repo", repoId: "repo_delta" },
    claimEpisode: "episode_delta",
    recovery: { open: false },
    runtimePreflight: converged ? { matches: true, missing: [], runtimeVersion: "x" } : undefined,
    callTool: async (name, args) => {
      calls.push({ name, args: JSON.parse(JSON.stringify(args)) });
      if (reply) {
        const custom = reply(args, state, calls.length);
        if (custom !== undefined) return custom;
      }
      const known = args.previous && args.previous.section_digests;
      if (!known) return JSON.parse(JSON.stringify(state.current));
      const digests = digestsOf(state.current);
      const changed = {};
      for (const [key, digest] of Object.entries(digests)) {
        if (known[key] !== digest) changed[key] = state.current[key];
      }
      return { snapshot_mode: "delta", section_digests: digests, changed_sections: changed };
    },
    _convergeBackgroundServices: () => {},
  };
  return { client, calls, state };
}

function makeView(client) {
  const messages = [];
  const view = new internals.ViewState((message) => messages.push(message));
  view.bindClient(client);
  return { view, messages };
}

function postedSnapshots(messages) {
  return messages.filter((message) => message.type === "snapshot").map((message) => message.payload);
}

test("canonical section digest matches the server's pinned digest", () => {
  assert.strictEqual(internals.snapshotSectionDigest(CROSS_LANGUAGE_SAMPLE), CROSS_LANGUAGE_DIGEST);
  assert.strictEqual(
    internals.canonicalSnapshotJson({ b: 1, a: [true, null] }),
    "{\"a\":[true,null],\"b\":1}",
  );
});

test("an idle refresh sends digests and merges an empty delta into the held snapshot", async () => {
  const { client, calls } = makeClient();
  const { view, messages } = makeView(client);

  await internals.pushSnapshotNoRetry(view, { client });
  await internals.pushSnapshotNoRetry(view, { client });

  assert.deepStrictEqual(calls[0].args, { full: true });
  assert.deepStrictEqual(calls[1].args, {
    full: true,
    previous: { section_digests: digestsOf(fullSnapshot()) },
  });
  const [first, second] = postedSnapshots(messages);
  assert.strictEqual(second.snapshot_mode, "full");
  assert.deepStrictEqual(second.tasks, first.tasks);
  assert.deepStrictEqual(second.agent_processes, first.agent_processes);
});

test("a changed section replaces only that section of the held snapshot", async () => {
  const { client, calls, state } = makeClient();
  const { view, messages } = makeView(client);

  await internals.pushSnapshotNoRetry(view, { client });
  state.current = fullSnapshot({ status_counts: { pending: 2, active: 2 } });
  const { payload } = await internals.pushSnapshotNoRetry(view, { client });

  assert.deepStrictEqual(payload, state.current);
  assert.deepStrictEqual(postedSnapshots(messages)[1].status_counts, { pending: 2, active: 2 });
  // The next refresh offers the merged payload's digests.
  await internals.pushSnapshotNoRetry(view, { client });
  assert.deepStrictEqual(calls[2].args.previous.section_digests, digestsOf(state.current));
});

test("a digest miss falls back to one plain full request", async () => {
  const { client, calls, state } = makeClient({
    reply: (args, current, callNumber) => {
      if (callNumber !== 2) return undefined;
      const digests = digestsOf(current.current);
      // Claims tasks is unchanged under a digest the view does not hold.
      digests.tasks = "0".repeat(64);
      return { snapshot_mode: "delta", section_digests: digests, changed_sections: {} };
    },
  });
  const { view, messages } = makeView(client);

  await internals.pushSnapshotNoRetry(view, { client });
  const { posted, payload } = await internals.pushSnapshotNoRetry(view, { client });

  assert.strictEqual(posted, true);
  assert.strictEqual(calls.length, 3);
  assert.ok(calls[1].args.previous);
  assert.deepStrictEqual(calls[2].args, { full: true });
  assert.deepStrictEqual(payload, state.current);
  assert.strictEqual(postedSnapshots(messages).length, 2);
});

test("a malformed delta falls back to one plain full request", async () => {
  const { client, calls } = makeClient({
    reply: (_args, _state, callNumber) => (callNumber === 2 ? { snapshot_mode: "delta" } : undefined),
  });
  const { view } = makeView(client);

  await internals.pushSnapshotNoRetry(view, { client });
  const { payload } = await internals.pushSnapshotNoRetry(view, { client });

  assert.deepStrictEqual(calls.map((call) => Boolean(call.args.previous)), [false, true, false]);
  assert.strictEqual(payload.snapshot_mode, "full");
});

test("a full reply to a delta request replaces the held snapshot", async () => {
  const { client, calls, state } = makeClient({
    reply: (_args, current, callNumber) => (
      callNumber === 2 ? JSON.parse(JSON.stringify(current.current)) : undefined
    ),
  });
  const { view } = makeView(client);

  await internals.pushSnapshotNoRetry(view, { client });
  state.current = fullSnapshot({ tasks: { pending: [] } });
  const { payload } = await internals.pushSnapshotNoRetry(view, { client });
  await internals.pushSnapshotNoRetry(view, { client });

  assert.strictEqual(calls.length, 3);
  assert.deepStrictEqual(payload, state.current);
  assert.deepStrictEqual(calls[2].args.previous.section_digests, digestsOf(state.current));
});

test("a runtime that has not converged is never asked for a delta", async () => {
  const { client, calls } = makeClient({ converged: false });
  const { view } = makeView(client);

  for (let push = 0; push < 3; push += 1) {
    await internals.pushSnapshotOnce(view, { client });
  }

  assert.deepStrictEqual(calls.map((call) => call.args), [{ full: false }, { full: true }, { full: true }, { full: true }]);
  assert.strictEqual(view.snapshotDeltaCache, null);
});

test("a repository switch drops the held snapshot", async () => {
  const { client, calls } = makeClient();
  const { view } = makeView(client);

  await internals.pushSnapshotNoRetry(view, { client });
  const other = { ...client, repositoryIdentity: { uriStr: "file:///work/other", repoId: "repo_other" } };
  await internals.pushSnapshotNoRetry(view, { client: other });

  assert.deepStrictEqual(calls[1].args, { full: true });
});
