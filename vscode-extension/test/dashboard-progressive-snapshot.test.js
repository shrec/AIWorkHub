"use strict";

const assert = require("assert");
const Module = require("module");
const path = require("path");

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

(async () => {
  const messages = [];
  const calls = [];
  let convergenceCalls = 0;
  const client = {
    repositoryIdentity: { uriStr: "file:///work/repo", repoId: "repo_test" },
    claimEpisode: "episode_progressive_snapshot",
    recovery: { open: false },
    callTool: async (_name, args, timeoutMs) => {
      calls.push({ args, timeoutMs });
      return args.full
        ? { snapshot_mode: "full", status_counts: { active: 2 }, outcome_counts: { accepted: 3, rejected: 1 }, tasks: { pending: [{ task_id: "T1" }] } }
        : { snapshot_mode: "summary", status_counts: { active: 2 }, outcome_counts: { accepted: 3, rejected: 1 } };
    },
    _convergeBackgroundServices: () => { convergenceCalls += 1; },
  };
  const view = new extension.__testInternals.ViewState((message) => messages.push(message));
  view.bindClient(client);

  await extension.__testInternals.pushSnapshotOnce(view, { client });

  assert.deepStrictEqual(calls.map((call) => call.args), [{ full: false }, { full: true }]);
  assert.strictEqual(
    calls[0].timeoutMs,
    extension.__testInternals.constants.MCP_REQUEST_TIMEOUT_MS,
  );
  assert.strictEqual(
    calls[1].timeoutMs,
    extension.__testInternals.constants.MCP_DASHBOARD_SNAPSHOT_TIMEOUT_MS,
  );
  assert.deepStrictEqual(
    messages
      .filter((message) => message.type === "snapshotSummary" || message.type === "snapshot")
      .map((message) => [message.type, message.payload.snapshot_mode]),
    [["snapshotSummary", "summary"], ["snapshot", "full"]],
  );
  assert.strictEqual(convergenceCalls, 1);
  assert.deepStrictEqual(messages[0].payload.outcome_counts, { accepted: 3, rejected: 1 });

  console.log("dashboard progressive snapshot: ok");

  const OPENCODE_IDENTITIES = ["opencode/glm-4.5-free", "openai/gpt-4o"];
  const SETTINGS_TOOL = "aiworkhub_dashboard_settings";
  const SNAPSHOT_TOOL = "aiworkhub_dashboard_snapshot";
  const settingsPayload = {
    catalog: {
      workers: [
        {
          provider: "opencode",
          adapter: "opencode_cli",
          model: "opencode/glm-4.5-free",
          vendor_provider: "opencode",
          declared_adapter: "opencode_cli",
          catalog_enabled: true,
          effective_enabled: true,
          inventory_only: true,
          discovered_from_opencode: true,
        },
        {
          provider: "openai",
          adapter: "opencode_cli",
          model: "openai/gpt-4o",
          vendor_provider: "openai",
          declared_adapter: "opencode_cli",
          catalog_enabled: true,
          effective_enabled: true,
          inventory_only: true,
          discovered_from_opencode: true,
        },
      ],
      opencode_discovered_model_count: 2,
    },
  };

  function makeRaceClient(calls, fullGate, onFullSnapshot) {
    let fullResolved = false;
    return {
      repositoryIdentity: { uriStr: "file:///work/repo", repoId: "repo_test" },
      claimEpisode: "episode_request_settings_race",
      recovery: { open: false },
      callTool: async (name, args, timeoutMs) => {
        calls.push({ name, args, timeoutMs, fullResolved });
        if (name === SNAPSHOT_TOOL) {
          if (args && args.full) {
            if (onFullSnapshot) onFullSnapshot();
            await fullGate;
            fullResolved = true;
            return {
              snapshot_mode: "full",
              status_counts: { active: 2 },
              outcome_counts: { accepted: 3, rejected: 1 },
              opencode_identities: OPENCODE_IDENTITIES.slice(),
              tasks: { pending: [{ task_id: "T1" }] },
            };
          }
          return {
            snapshot_mode: "summary",
            status_counts: { active: 2 },
            outcome_counts: { accepted: 3, rejected: 1 },
          };
        }
        if (name === SETTINGS_TOOL) {
          return settingsPayload;
        }
        throw new Error(`unexpected tool ${name}`);
      },
      _convergeBackgroundServices: () => {},
    };
  }

  {
    const calls = [];
    const messages = [];
    let releaseFull;
    const fullGate = new Promise((resolve) => { releaseFull = resolve; });
    let sawFullSnapshot;
    const fullStarted = new Promise((resolve) => { sawFullSnapshot = resolve; });
    const client = makeRaceClient(calls, fullGate, sawFullSnapshot);
    const view = new extension.__testInternals.ViewState((message) => messages.push(message));
    view.bindClient(client);
    const inFlight = extension.__testInternals.pushSnapshotOnce(view, { client }).finally(() => {
      if (view.snapshotInFlight === inFlight) view.snapshotInFlight = null;
    });
    view.snapshotInFlight = inFlight;
    await fullStarted;
    const oldRequestSettings = (async () => {
      const payload = await client.callTool(SETTINGS_TOOL, {});
      view.postMessage({ type: "settings", payload });
    })();
    await Promise.resolve();
    assert.ok(calls.some((call) => call.name === SETTINGS_TOOL && call.fullResolved === false));
    releaseFull();
    await Promise.all([inFlight, oldRequestSettings]);
    const types = messages.map((message) => message.type);
    assert.ok(types.indexOf("settings") >= 0 && types.indexOf("snapshot") >= 0);
    assert.ok(types.indexOf("settings") < types.indexOf("snapshot"));
  }

  {
    const calls = [];
    const messages = [];
    let releaseFull;
    const fullGate = new Promise((resolve) => { releaseFull = resolve; });
    let sawFullSnapshot;
    const fullStarted = new Promise((resolve) => { sawFullSnapshot = resolve; });
    const client = makeRaceClient(calls, fullGate, sawFullSnapshot);
    const view = new extension.__testInternals.ViewState((message) => messages.push(message));
    view.bindClient(client);
    const inFlight = extension.__testInternals.pushSnapshotOnce(view, { client }).finally(() => {
      if (view.snapshotInFlight === inFlight) view.snapshotInFlight = null;
    });
    view.snapshotInFlight = inFlight;
    await fullStarted;
    const settingsDone = extension.__testInternals.pushSettings(view, { client });
    await Promise.resolve();
    assert.ok(!calls.some((call) => call.name === SETTINGS_TOOL));
    releaseFull();
    await Promise.all([inFlight, settingsDone]);
    const settingsCalls = calls.filter((call) => call.name === SETTINGS_TOOL);
    assert.strictEqual(settingsCalls.length, 1);
    assert.deepStrictEqual(settingsCalls[0].args, {});
    assert.strictEqual(settingsCalls[0].fullResolved, true);
    assert.deepStrictEqual(
      messages
        .filter((message) => message.type === "snapshotSummary" || message.type === "snapshot" || message.type === "settings")
        .map((message) => message.type),
      ["snapshotSummary", "snapshot", "settings"],
    );
    const settingsMessage = messages.find((message) => message.type === "settings");
    assert.deepStrictEqual(
      settingsMessage.payload.catalog.workers.map((row) => row.model),
      OPENCODE_IDENTITIES,
    );
    assert.strictEqual(settingsMessage.payload.catalog.opencode_discovered_model_count, 2);
  }

  {
    const calls = [];
    const client = makeRaceClient(calls, Promise.resolve());
    const view = new extension.__testInternals.ViewState(() => {});
    view.bindClient(client);
    assert.strictEqual(view.snapshotInFlight, null);
    await extension.__testInternals.pushSettings(view, { client });
    assert.deepStrictEqual(calls.map((call) => call.name), [SETTINGS_TOOL]);
  }

  console.log("dashboard requestSettings snapshot ordering: ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
