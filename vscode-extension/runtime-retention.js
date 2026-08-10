"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const GENERATION_SCHEMA = "aiworkhub.stable_runtime.v1";
const LEASE_SCHEMA = "aiworkhub.stable_runtime_lease.v1";
const RETENTION_SCHEMA = "aiworkhub.runtime_retention.v1";
const RETENTION_MARKER_SCHEMA = "aiworkhub.runtime_retention_marker.v1";
const KEEP_GENERATIONS = 3;
const MIN_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const LEASE_FRESH_MS = 10 * 60 * 1000;
const UNDO_MS = 7 * 24 * 60 * 60 * 1000;
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const BATCH_RE = /^r[0-9]{8}T[0-9]{6}-[a-f0-9]{12}$/;

function atomicJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  const temp = `${filePath}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  fs.writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temp, filePath);
}

function safeJson(filePath) {
  try {
    const info = fs.lstatSync(filePath);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 1024 * 1024) return null;
    const value = JSON.parse(fs.readFileSync(filePath, "utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch (_err) {
    return null;
  }
}

function treeSize(root) {
  let bytes = 0;
  let files = 0;
  function visit(directory) {
    let entries;
    try {
      entries = fs.readdirSync(directory, { withFileTypes: true });
    } catch (err) {
      if (err && err.code === "ENOENT") return;
      throw err;
    }
    for (const entry of entries) {
      const full = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) visit(full);
      else if (entry.isFile()) {
        let info;
        try {
          info = fs.statSync(full);
        } catch (err) {
          if (err && err.code === "ENOENT") continue;
          throw err;
        }
        bytes += info.size;
        files += 1;
      }
    }
  }
  visit(root);
  return { bytes, files };
}

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return Boolean(err && err.code === "EPERM");
  }
}

function currentGeneration(storageRoot) {
  const current = safeJson(path.join(storageRoot, "runtime", "current.json"));
  const name = current && String(current.generation || "");
  return NAME_RE.test(name) ? name : "";
}

function quarantineRoot(storageRoot, create = false) {
  const root = path.join(path.resolve(storageRoot), "runtime", "quarantine");
  if (create) fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  let info;
  try {
    info = fs.lstatSync(root);
  } catch (err) {
    if (err && err.code === "ENOENT") return null;
    throw err;
  }
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new Error("runtime_quarantine_root_invalid");
  }
  return root;
}

function leaseState(generationRoot, nowMs, isPidAlive = pidAlive) {
  const leasesRoot = path.join(generationRoot, "leases");
  if (!fs.existsSync(leasesRoot)) return { protected: false, live: 0, stale: 0, invalid: 0 };
  const info = fs.lstatSync(leasesRoot);
  if (!info.isDirectory() || info.isSymbolicLink()) {
    return { protected: true, live: 0, stale: 0, invalid: 1 };
  }
  let live = 0;
  let stale = 0;
  let invalid = 0;
  for (const entry of fs.readdirSync(leasesRoot, { withFileTypes: true })) {
    if (!entry.isFile() || entry.isSymbolicLink() || !entry.name.endsWith(".json")) {
      invalid += 1;
      continue;
    }
    const value = safeJson(path.join(leasesRoot, entry.name));
    const updatedMs = value && Date.parse(String(value.updated_at || ""));
    const valid = value && value.schema_id === LEASE_SCHEMA
      && NAME_RE.test(String(value.window_id || ""))
      && Number.isInteger(value.pid) && value.pid > 0
      && Number.isFinite(updatedMs);
    if (!valid) {
      invalid += 1;
      continue;
    }
    if (nowMs - updatedMs <= LEASE_FRESH_MS || isPidAlive(value.pid)) live += 1;
    else stale += 1;
  }
  return { protected: live > 0 || invalid > 0, live, stale, invalid };
}

function scan(options) {
  const storageRoot = path.resolve(options.storageRoot);
  const nowMs = Number.isFinite(options.nowMs) ? options.nowMs : Date.now();
  const isPidAlive = options.isPidAlive || pidAlive;
  const generationsRoot = path.join(storageRoot, "runtime", "generations");
  const current = currentGeneration(storageRoot);
  const marker = safeJson(path.join(storageRoot, "runtime", "retention.json"));
  const enabledMs = marker && marker.schema_id === RETENTION_MARKER_SCHEMA
    ? Date.parse(String(marker.enabled_at || ""))
    : Number.NaN;
  const rolloutReady = Number.isFinite(enabledMs) && nowMs - enabledMs >= MIN_AGE_MS;
  const generations = [];
  if (fs.existsSync(generationsRoot)) {
    const rootInfo = fs.lstatSync(generationsRoot);
    if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) {
      return { ok: false, error: "runtime_generations_root_invalid", candidates: [] };
    }
    for (const entry of fs.readdirSync(generationsRoot, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.isSymbolicLink() || !NAME_RE.test(entry.name)) continue;
      const root = path.join(generationsRoot, entry.name);
      const manifest = safeJson(path.join(root, "manifest.json"));
      if (!manifest || manifest.schema_id !== GENERATION_SCHEMA
        || !/^[a-f0-9]{64}$/.test(String(manifest.fingerprint || ""))) {
        generations.push({ name: entry.name, protected: true, reason: "manifest_invalid", bytes: 0, files: 0 });
        continue;
      }
      const createdMs = Date.parse(String(manifest.created_at || ""));
      const info = fs.lstatSync(root);
      const size = treeSize(root);
      const leases = leaseState(root, nowMs, isPidAlive);
      generations.push({
        name: entry.name,
        version: String(manifest.version || ""),
        fingerprint: String(manifest.fingerprint),
        created_at: Number.isFinite(createdMs) ? new Date(createdMs).toISOString() : "",
        created_ms: Number.isFinite(createdMs) ? createdMs : 0,
        modified_ms: info.mtimeMs,
        bytes: size.bytes,
        files: size.files,
        current: entry.name === current,
        lease: leases,
        protected: entry.name === current || leases.protected || !Number.isFinite(createdMs),
        reason: entry.name === current ? "current_generation" : leases.protected ? "live_or_invalid_lease" : !Number.isFinite(createdMs) ? "created_at_invalid" : "",
      });
    }
  }
  const ranked = generations
    .filter((item) => item.fingerprint)
    .slice()
    .sort((left, right) => (right.created_ms - left.created_ms) || right.name.localeCompare(left.name));
  const protectedNewest = new Set(ranked.slice(0, KEEP_GENERATIONS).map((item) => item.name));
  const candidates = [];
  for (const item of generations) {
    if (protectedNewest.has(item.name)) {
      item.protected = true;
      item.reason = item.reason || "rollback_generation";
    }
    if (!item.protected && rolloutReady && nowMs - item.created_ms >= MIN_AGE_MS) candidates.push({
      name: item.name,
      version: item.version,
      fingerprint: item.fingerprint,
      created_at: item.created_at,
      modified_ms: item.modified_ms,
      bytes: item.bytes,
      files: item.files,
    });
  }
  candidates.sort((left, right) => left.name.localeCompare(right.name));
  const digest = crypto.createHash("sha256").update(JSON.stringify({
    schema_id: RETENTION_SCHEMA,
    current,
    keep_generations: KEEP_GENERATIONS,
    min_age_ms: MIN_AGE_MS,
    candidates,
  })).digest("hex");
  const quarantine = listBatches({ storageRoot, nowMs });
  return {
    ok: true,
    schema_id: RETENTION_SCHEMA,
    dry_run: true,
    global_extension_scope: true,
    current_generation: current,
    keep_generations: KEEP_GENERATIONS,
    min_age_days: MIN_AGE_MS / 86400000,
    rollout_ready: rolloutReady,
    rollout_protected_until: Number.isFinite(enabledMs)
      ? new Date(enabledMs + MIN_AGE_MS).toISOString()
      : "retention_marker_missing",
    current_bytes: generations.reduce((total, item) => total + Number(item.bytes || 0), 0),
    generation_count: generations.length,
    protected_count: generations.length - candidates.length,
    candidate_count: candidates.length,
    candidate_bytes: candidates.reduce((total, item) => total + item.bytes, 0),
    preview_digest: digest,
    candidates,
    generations: generations.map(({ created_ms, modified_ms, fingerprint, ...item }) => item),
    quarantine_batches: quarantine.batches,
  };
}

function acquireLease(options) {
  const generationRoot = path.resolve(options.generationRoot);
  const windowId = String(options.windowId || "");
  const pid = Number(options.pid || process.pid);
  if (!NAME_RE.test(windowId) || !Number.isInteger(pid) || pid <= 0) {
    throw new Error("runtime_lease_identity_invalid");
  }
  const manifest = safeJson(path.join(generationRoot, "manifest.json"));
  if (!manifest || manifest.schema_id !== GENERATION_SCHEMA) throw new Error("runtime_generation_invalid");
  const leasePath = path.join(generationRoot, "leases", `${windowId}.json`);
  const markerPath = path.join(path.dirname(path.dirname(generationRoot)), "retention.json");
  if (!fs.existsSync(markerPath)) {
    atomicJson(markerPath, {
      schema_id: RETENTION_MARKER_SCHEMA,
      enabled_at: new Date().toISOString(),
    });
  }
  const write = () => atomicJson(leasePath, {
    schema_id: LEASE_SCHEMA,
    window_id: windowId,
    pid,
    generation: path.basename(generationRoot),
    updated_at: new Date().toISOString(),
  });
  write();
  return {
    path: leasePath,
    heartbeat: write,
    dispose() {
      try { fs.rmSync(leasePath, { force: true }); } catch (_err) { /* fail closed as stale lease */ }
    },
  };
}

function quarantine(options) {
  if (options.confirm !== true) throw new Error("explicit_confirmation_required");
  const storageRoot = path.resolve(options.storageRoot);
  const nowMs = Number.isFinite(options.nowMs) ? options.nowMs : Date.now();
  const current = scan({ ...options, storageRoot, nowMs });
  if (!current.ok) throw new Error(current.error || "runtime_retention_scan_failed");
  if (String(options.previewDigest || "") !== current.preview_digest) throw new Error("runtime_retention_preview_stale");
  if (!current.candidates.length) return { ok: true, no_op: true, quarantined: 0, bytes: 0, batch_id: "" };
  const stamp = new Date(nowMs).toISOString().replace(/[-:]/g, "").slice(0, 15);
  const batchId = `r${stamp}-${current.preview_digest.slice(0, 12)}`;
  const qroot = quarantineRoot(storageRoot, true);
  const batch = path.join(qroot, batchId);
  fs.mkdirSync(batch, { recursive: false, mode: 0o700 });
  const manifest = {
    schema_id: RETENTION_SCHEMA,
    batch_id: batchId,
    created_at: new Date(nowMs).toISOString(),
    restore_deadline: new Date(nowMs + UNDO_MS).toISOString(),
    status: "quarantining",
    items: current.candidates.map((item) => ({ ...item, state: "planned" })),
  };
  const manifestPath = path.join(batch, "manifest.json");
  atomicJson(manifestPath, manifest);
  let moved = 0;
  let bytes = 0;
  for (const item of manifest.items) {
    const fresh = scan({ ...options, storageRoot, nowMs });
    const exact = fresh.ok && fresh.candidates.find((candidate) => candidate.name === item.name
      && candidate.fingerprint === item.fingerprint && candidate.bytes === item.bytes);
    if (!exact) {
      item.state = "skipped_identity_or_lease_changed";
      atomicJson(manifestPath, manifest);
      continue;
    }
    const source = path.join(storageRoot, "runtime", "generations", item.name);
    const destination = path.join(batch, item.name);
    fs.renameSync(source, destination);
    item.state = "quarantined";
    moved += 1;
    bytes += item.bytes;
    atomicJson(manifestPath, manifest);
  }
  manifest.status = moved ? "quarantined" : "empty";
  manifest.quarantined_count = moved;
  manifest.quarantined_bytes = bytes;
  atomicJson(manifestPath, manifest);
  return { ok: true, no_op: false, batch_id: batchId, quarantined: moved, bytes };
}

function listBatches(options) {
  const storageRoot = path.resolve(options.storageRoot);
  const nowMs = Number.isFinite(options.nowMs) ? options.nowMs : Date.now();
  const root = quarantineRoot(storageRoot, false);
  if (!root) return { ok: true, count: 0, batches: [] };
  const rows = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.isSymbolicLink() || !BATCH_RE.test(entry.name)) continue;
    const value = safeJson(path.join(root, entry.name, "manifest.json"));
    if (!value || value.schema_id !== RETENTION_SCHEMA || value.batch_id !== entry.name) continue;
    const deadlineMs = Date.parse(String(value.restore_deadline || ""));
    const states = Array.isArray(value.items) ? value.items.map((item) => item && item.state) : [];
    rows.push({
      batch_id: entry.name,
      created_at: String(value.created_at || ""),
      restore_deadline: String(value.restore_deadline || ""),
      status: String(value.status || "unknown"),
      quarantined_count: states.filter((state) => state === "quarantined").length,
      restored_count: states.filter((state) => state === "restored").length,
      bytes: Number(value.quarantined_bytes || 0),
      purge_eligible: Number.isFinite(deadlineMs) && nowMs >= deadlineMs,
    });
  }
  rows.sort((left, right) => right.batch_id.localeCompare(left.batch_id));
  return { ok: true, count: Math.min(rows.length, 100), batches: rows.slice(0, 100) };
}

function exactBatch(storageRoot, batchId) {
  if (!BATCH_RE.test(batchId)) throw new Error("runtime_retention_batch_id_invalid");
  const root = quarantineRoot(storageRoot, false);
  if (!root) throw new Error("runtime_retention_batch_not_found");
  const batch = path.join(root, batchId);
  const info = fs.lstatSync(batch);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("runtime_retention_batch_invalid");
  const manifest = safeJson(path.join(batch, "manifest.json"));
  if (!manifest || manifest.schema_id !== RETENTION_SCHEMA || manifest.batch_id !== batchId) {
    throw new Error("runtime_retention_manifest_invalid");
  }
  return { batch, manifest };
}

function restore(options) {
  if (options.confirm !== true) throw new Error("explicit_confirmation_required");
  const storageRoot = path.resolve(options.storageRoot);
  const { batch, manifest } = exactBatch(storageRoot, String(options.batchId || ""));
  const manifestPath = path.join(batch, "manifest.json");
  const generationsRoot = path.join(storageRoot, "runtime", "generations");
  fs.mkdirSync(generationsRoot, { recursive: true, mode: 0o700 });
  let restored = 0;
  for (const item of manifest.items || []) {
    if (!item || item.state !== "quarantined" || !NAME_RE.test(String(item.name || ""))) continue;
    const source = path.join(batch, item.name);
    const destination = path.join(generationsRoot, item.name);
    if (!fs.existsSync(source) || fs.existsSync(destination)) {
      item.state = "restore_conflict";
      atomicJson(manifestPath, manifest);
      continue;
    }
    fs.renameSync(source, destination);
    item.state = "restored";
    restored += 1;
    atomicJson(manifestPath, manifest);
  }
  manifest.status = restored ? "restored" : manifest.status;
  atomicJson(manifestPath, manifest);
  return { ok: true, batch_id: manifest.batch_id, restored };
}

function purge(options) {
  if (options.confirm !== true) throw new Error("explicit_confirmation_required");
  const nowMs = Number.isFinite(options.nowMs) ? options.nowMs : Date.now();
  const { batch, manifest } = exactBatch(options.storageRoot, String(options.batchId || ""));
  const deadlineMs = Date.parse(String(manifest.restore_deadline || ""));
  if (!Number.isFinite(deadlineMs)) throw new Error("runtime_retention_deadline_invalid");
  if (nowMs < deadlineMs) throw new Error("runtime_retention_undo_window_active");
  const bytes = Number(manifest.quarantined_bytes || 0);
  fs.rmSync(batch, { recursive: true, force: false });
  return { ok: true, batch_id: manifest.batch_id, purged: true, bytes };
}

module.exports = {
  GENERATION_SCHEMA,
  KEEP_GENERATIONS,
  LEASE_SCHEMA,
  MIN_AGE_MS,
  RETENTION_SCHEMA,
  RETENTION_MARKER_SCHEMA,
  UNDO_MS,
  acquireLease,
  listBatches,
  purge,
  quarantine,
  restore,
  scan,
};
