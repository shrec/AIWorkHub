"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const retention = require("../runtime-retention");

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value)}\n`);
}

function generation(storageRoot, name, createdAt) {
  const root = path.join(storageRoot, "runtime", "generations", name);
  fs.mkdirSync(path.join(root, "runtime", "aiworkhub"), { recursive: true });
  fs.writeFileSync(path.join(root, "runtime", "aiworkhub", "server.py"), name.repeat(3));
  writeJson(path.join(root, "manifest.json"), {
    schema_id: retention.GENERATION_SCHEMA,
    version: name.split("-")[0],
    fingerprint: name.slice(-64).padStart(64, "a").slice(0, 64),
    created_at: createdAt,
  });
  return root;
}

function fixture() {
  const storageRoot = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-runtime-retention-"));
  const nowMs = Date.parse("2026-07-30T00:00:00.000Z");
  const names = [
    `0.7.4-${"1".repeat(64)}`,
    `0.7.5-${"2".repeat(64)}`,
    `0.7.6-${"3".repeat(64)}`,
    `0.7.7-${"4".repeat(64)}`,
    `0.8.0-${"5".repeat(64)}`,
  ];
  names.forEach((name, index) => generation(
    storageRoot,
    name,
    new Date(nowMs - (20 - index) * 86400000).toISOString(),
  ));
  writeJson(path.join(storageRoot, "runtime", "current.json"), {
    schema_id: retention.GENERATION_SCHEMA,
    generation: names[4],
  });
  writeJson(path.join(storageRoot, "runtime", "retention.json"), {
    schema_id: retention.RETENTION_MARKER_SCHEMA,
    enabled_at: new Date(nowMs - 10 * 86400000).toISOString(),
  });
  return { storageRoot, nowMs, names };
}

{
  const value = fixture();
  try {
    const preview = retention.scan({ ...value, isPidAlive: () => false });
    assert.equal(preview.ok, true);
    assert.equal(preview.generation_count, 5);
    assert.deepEqual(preview.candidates.map((item) => item.name), value.names.slice(0, 2));
    assert.equal(preview.protected_count, 3);
  } finally {
    fs.rmSync(value.storageRoot, { recursive: true, force: true });
  }
}

{
  const value = fixture();
  try {
    fs.rmSync(path.join(value.storageRoot, "runtime", "retention.json"));
    const preview = retention.scan({ ...value, isPidAlive: () => false });
    assert.equal(preview.rollout_ready, false);
    assert.equal(preview.candidate_count, 0, "pre-feature generations need one full safety window");
  } finally {
    fs.rmSync(value.storageRoot, { recursive: true, force: true });
  }
}

{
  const value = fixture();
  try {
    const leases = path.join(value.storageRoot, "runtime", "generations", value.names[0], "leases");
    fs.mkdirSync(leases, { recursive: true });
    fs.writeFileSync(path.join(leases, "unknown.json"), "not-json\n");
    const preview = retention.scan({ ...value, isPidAlive: () => false });
    assert.deepEqual(preview.candidates.map((item) => item.name), [value.names[1]]);
  } finally {
    fs.rmSync(value.storageRoot, { recursive: true, force: true });
  }
}

{
  const value = fixture();
  try {
    const lease = retention.acquireLease({
      generationRoot: path.join(value.storageRoot, "runtime", "generations", value.names[0]),
      windowId: "window_test",
      pid: process.pid,
    });
    const preview = retention.scan({ ...value, isPidAlive: () => true });
    assert.deepEqual(preview.candidates.map((item) => item.name), [value.names[1]]);
    lease.dispose();
  } finally {
    fs.rmSync(value.storageRoot, { recursive: true, force: true });
  }
}

{
  const value = fixture();
  try {
    const preview = retention.scan({ ...value, isPidAlive: () => false });
    const moved = retention.quarantine({
      ...value,
      isPidAlive: () => false,
      previewDigest: preview.preview_digest,
      confirm: true,
    });
    assert.equal(moved.quarantined, 2);
    assert.equal(retention.listBatches(value).count, 1);
    assert.ok(!fs.existsSync(path.join(value.storageRoot, "runtime", "generations", value.names[0])));
    const restored = retention.restore({
      storageRoot: value.storageRoot,
      batchId: moved.batch_id,
      confirm: true,
    });
    assert.equal(restored.restored, 2);
    assert.ok(fs.existsSync(path.join(value.storageRoot, "runtime", "generations", value.names[0])));
  } finally {
    fs.rmSync(value.storageRoot, { recursive: true, force: true });
  }
}

{
  const value = fixture();
  try {
    const preview = retention.scan({ ...value, isPidAlive: () => false });
    const moved = retention.quarantine({ ...value, isPidAlive: () => false, previewDigest: preview.preview_digest, confirm: true });
    assert.throws(
      () => retention.purge({ storageRoot: value.storageRoot, batchId: moved.batch_id, confirm: true, nowMs: value.nowMs }),
      /runtime_retention_undo_window_active/,
    );
    const purged = retention.purge({ storageRoot: value.storageRoot, batchId: moved.batch_id, confirm: true, nowMs: value.nowMs + retention.UNDO_MS + 1 });
    assert.equal(purged.purged, true);
  } finally {
    fs.rmSync(value.storageRoot, { recursive: true, force: true });
  }
}

console.log("runtime retention lifecycle: ok");
