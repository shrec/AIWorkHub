"use strict";

// Native VS Code Webview UI for AIWorkHub. Runs entirely inside
// the Webview sandbox: it never fetches a URL, never opens an embedded frame, and
// never receives a coordinator token, filesystem path, or MCP capability.
// The extension host is the only thing that talks to the Task MCP server;
// this script only ever posts the fixed message enum (ready / refresh /
// selectTask / setAutoRefresh / setRefreshInterval) and renders whatever the
// host posts back (snapshot / taskDetail / offline / error).

const vscode = acquireVsCodeApi();

const ACTIVE_STATUSES = ["pending", "processing", "review"];
const STATUS_ORDER = { processing: 0, review: 1, blocked: 2, pending: 3, archived: 4, finished: 5 };
const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
// Matches the server-side bound (MAX_LIVE_OUTPUT_BYTES in dashboard_mcp_app.py):
// the client-retained tail is capped at the same 64 KiB so a long-lived poll
// never grows the Webview's own memory past what one server response could
// ever contain.
const LIVE_OUTPUT_MAX_CLIENT_CHARS = 64 * 1024;
// Bounded, single-task-only poll cadence for the selected task's Live
// Output -- independent of the dashboard-wide snapshot refresh timer, and
// only ever running while exactly one task is selected (see
// startLiveOutputPolling/stopLiveOutputPolling).
const LIVE_OUTPUT_POLL_MS = 4000;
const READY_RETRY_MS = 1000;
const READY_MAX_ATTEMPTS = 30;

const persisted = vscode.getState() || {};

const state = {
  snapshot: null,
  tasks: [],
  selectedTaskId: persisted.selectedTaskId || null,
  status: persisted.status || "all",
  search: "",
  topic: "all",
  runner: "all",
  sort: persisted.sort || "status",
  detailRequest: 0,
  liveOutputTaskId: null,
  liveOutputCursor: 0,
  liveOutputText: "",
  liveOutputTimer: null,
  systemLogs: [],
  memoryEntries: [],
  sessionEntries: [],
  kbEntries: [],
  featureSettings: null,
  returnPage: 0,
  returnSearch: "",
  returnTopic: "all",
  returnRunner: "all",
  returnPageSize: 20,
};

let readyRetryTimer = null;
let readyAttempts = 0;
let hostConnected = false;

function persistState() {
  vscode.setState({
    selectedTaskId: state.selectedTaskId,
    status: state.status,
    sort: state.sort,
  });
}

function stopReadyRetry() {
  hostConnected = true;
  if (readyRetryTimer !== null) {
    window.clearTimeout(readyRetryTimer);
    readyRetryTimer = null;
  }
}

function signalReady() {
  if (hostConnected || readyAttempts >= READY_MAX_ATTEMPTS) return;
  readyAttempts += 1;
  vscode.postMessage({ type: "ready" });
  readyRetryTimer = window.setTimeout(signalReady, READY_RETRY_MS);
}

const elements = {
  connectionState: document.querySelector("#connection-state"),
  connectionLabel: document.querySelector("#connection-label"),
  lastSync: document.querySelector("#last-sync"),
  autoRefresh: document.querySelector("#auto-refresh"),
  refreshInterval: document.querySelector("#refresh-interval"),
  refreshButton: document.querySelector("#refresh-button"),
  headerStorage: document.querySelector("#header-storage"),
  headerStorageManaged: document.querySelector("#header-storage-managed"),
  headerStorageFree: document.querySelector("#header-storage-free"),
  headerSourceGraph: document.querySelector("#header-source-graph"),
  headerSourceGraphRate: document.querySelector("#header-source-graph-rate"),
  headerSourceGraphDetail: document.querySelector("#header-source-graph-detail"),
  headerSessionManager: document.querySelector("#header-session-manager"),
  headerSessionManagerValue: document.querySelector("#header-session-manager-value"),
  headerSessionManagerDetail: document.querySelector("#header-session-manager-detail"),
  headerAiMemory: document.querySelector("#header-ai-memory"),
  headerAiMemoryValue: document.querySelector("#header-ai-memory-value"),
  headerAiMemoryDetail: document.querySelector("#header-ai-memory-detail"),
  headerKb: document.querySelector("#header-kb"),
  headerKbValue: document.querySelector("#header-kb-value"),
  headerKbDetail: document.querySelector("#header-kb-detail"),
  headerPreflight: document.querySelector("#header-preflight"),
  headerPreflightValue: document.querySelector("#header-preflight-value"),
  headerPreflightDetail: document.querySelector("#header-preflight-detail"),
  sourceAlert: document.querySelector("#source-alert"),
  sourceAlertTitle: document.querySelector("#source-alert-title"),
  sourceAlertMessage: document.querySelector("#source-alert-message"),
  reloadAlert: document.querySelector("#reload-alert"),
  reloadAlertMessage: document.querySelector("#reload-alert-message"),
  offlineAlert: document.querySelector("#offline-alert"),
  offlineAlertMessage: document.querySelector("#offline-alert-message"),
  offlineRetryButton: document.querySelector("#offline-retry-button"),
  uninitializedAlert: document.querySelector("#uninitialized-alert"),
  uninitializedAlertMessage: document.querySelector("#uninitialized-alert-message"),
  initializeButton: document.querySelector("#initialize-button"),
  identityAlert: document.querySelector("#identity-alert"),
  identityAlertTitle: document.querySelector("#identity-alert-title"),
  identityAlertMessage: document.querySelector("#identity-alert-message"),
  callbackObservability: document.querySelector("#callback-observability"),
  callbackBacklog: document.querySelector("#callback-backlog"),
  callbackDelivery: document.querySelector("#callback-delivery"),
  callbackRetries: document.querySelector("#callback-retries"),
  callbackDegraded: document.querySelector("#callback-degraded"),
  filteredCount: document.querySelector("#filtered-count"),
  statusFilters: document.querySelector("#status-filters"),
  taskSearch: document.querySelector("#task-search"),
  topicFilter: document.querySelector("#topic-filter"),
  runnerFilter: document.querySelector("#runner-filter"),
  sortOrder: document.querySelector("#sort-order"),
  tableBody: document.querySelector("#task-table-body"),
  tableEmpty: document.querySelector("#table-empty"),
  tableLoading: document.querySelector("#table-loading"),
  topicStats: document.querySelector("#topic-stats"),
  runnerStats: document.querySelector("#runner-stats"),
  planDag: document.querySelector("#plan-dag"),
  workforceList: document.querySelector("#workforce-list"),
  toolUseList: document.querySelector("#tool-use-list"),
  usageList: document.querySelector("#usage-list"),
  storageList: document.querySelector("#storage-list"),
  storageCleanupPreview: document.querySelector("#storage-cleanup-preview"),
  storageRegistrationPrune: document.querySelector("#storage-registration-prune"),
  terminalLogCleanupPreview: document.querySelector("#terminal-log-cleanup-preview"),
  runtimeCleanupPreview: document.querySelector("#runtime-cleanup-preview"),
  systemLogList: document.querySelector("#system-log-list"),
  systemLogCopy: document.querySelector("#system-log-copy"),
  systemLogClear: document.querySelector("#system-log-clear"),
  systemLogCount: document.querySelector("#system-log-count"),
  lastSystemLog: document.querySelector("#last-system-log"),
  openSystemLog: document.querySelector("#open-system-log"),
  systemLogDialog: document.querySelector("#system-log-dialog"),
  openAiMemory: document.querySelector("#open-ai-memory"),
  aiMemoryDialog: document.querySelector("#ai-memory-dialog"),
  aiMemorySummary: document.querySelector("#ai-memory-summary"),
  aiMemorySearch: document.querySelector("#ai-memory-search"),
  aiMemoryList: document.querySelector("#ai-memory-list"),
  openSessions: document.querySelector("#open-sessions"),
  sessionsDialog: document.querySelector("#sessions-dialog"),
  sessionsSummary: document.querySelector("#sessions-summary"),
  sessionsSearch: document.querySelector("#sessions-search"),
  sessionsList: document.querySelector("#sessions-list"),
  openKb: document.querySelector("#open-kb"),
  kbDialog: document.querySelector("#kb-dialog"),
  kbSummary: document.querySelector("#kb-summary"),
  kbSearch: document.querySelector("#kb-search"),
  kbList: document.querySelector("#kb-list"),
  openSettings: document.querySelector("#open-settings"),
  settingsDialog: document.querySelector("#settings-dialog"),
  settingsSummary: document.querySelector("#settings-summary"),
  settingsList: document.querySelector("#settings-list"),
  returnList: document.querySelector("#return-list"),
  returnSearch: document.querySelector("#return-search"),
  returnTopic: document.querySelector("#return-topic"),
  returnRunner: document.querySelector("#return-runner"),
  returnPrevious: document.querySelector("#return-previous"),
  returnNext: document.querySelector("#return-next"),
  returnPage: document.querySelector("#return-page"),
  returnTab: document.querySelector("#tab-returns"),
  runList: document.querySelector("#run-list"),
  runTab: document.querySelector("#tab-runs"),
  warningList: document.querySelector("#warning-list"),
  warningTab: document.querySelector("#tab-warnings"),
  detailHeading: document.querySelector("#detail-heading"),
  detailStatus: document.querySelector("#detail-status"),
  detailLoading: document.querySelector("#detail-loading"),
  detailError: document.querySelector("#detail-error"),
  detailEmpty: document.querySelector("#detail-empty"),
  detailContent: document.querySelector("#detail-content"),
  detailObjective: document.querySelector("#detail-objective"),
  detailMetadata: document.querySelector("#detail-metadata"),
  detailValidation: document.querySelector("#detail-validation"),
  detailResult: document.querySelector("#detail-result"),
  detailAiInfraBlock: document.querySelector("#detail-ai-infra-block"),
  detailAiInfra: document.querySelector("#detail-ai-infra"),
  detailEvidenceBlock: document.querySelector("#detail-evidence-block"),
  detailEvidence: document.querySelector("#detail-evidence"),
  detailWritesBlock: document.querySelector("#detail-writes-block"),
  detailWrites: document.querySelector("#detail-writes"),
  detailLiveOutputBlock: document.querySelector("#detail-live-output-block"),
  detailLiveOutputState: document.querySelector("#detail-live-output-state"),
  detailLiveOutputContainer: document.querySelector("#detail-live-output-container"),
  detailLiveOutputStderr: document.querySelector("#detail-live-output-stderr"),
  detailLiveOutputRaw: document.querySelector("#detail-live-output-raw"),
  detailLiveOutputRawContent: document.querySelector("#detail-live-output-raw-content"),
  toast: document.querySelector("#toast"),
  extensionVersion: document.querySelector("#extension-version"),
  mcpRuntimeVersion: document.querySelector("#mcp-runtime-version"),
  targetState: document.querySelector("#target-state"),
  repoRouter: document.querySelector("#repo-router"),
  repoRouterList: document.querySelector("#repo-router-list"),
  targetButtons: Array.from(document.querySelectorAll("[data-provider]")),
};

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined && text !== null) {
    element.textContent = String(text);
  }
  return element;
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function numberValue(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatCount(value) {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(numberValue(value));
}

function formatMoney(value) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: numberValue(value) < 1 ? 4 : 2,
  }).format(numberValue(value));
}

function formatRelativeTime(value) {
  if (!value) {
    return "Unknown";
  }
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) {
    return String(value);
  }
  const elapsed = Math.max(0, Date.now() - timestamp);
  const minutes = Math.floor(elapsed / 60000);
  if (minutes < 1) {
    return "Now";
  }
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 48) {
    return `${hours}h ago`;
  }
  return `${Math.floor(hours / 24)}d ago`;
}

function canonicalStatus(task) {
  if (String(task.archived_at || "").trim()) {
    return "archived";
  }
  const status = String(task.status || "").trim().toLowerCase();
  const worker = String(task.worker_status || "").trim().toLowerCase();
  if (["finished", "completed", "stale_already_done"].includes(status) || worker === "done") {
    return "finished";
  }
  if (status.startsWith("blocked") || worker.startsWith("blocked") || worker.startsWith("deferred")) {
    return "blocked";
  }
  if (["review", "ready_for_review", "codex_review", "awaiting_review"].includes(status) ||
      ["review", "ready_for_review", "codex_review", "awaiting_review"].includes(worker)) {
    return "review";
  }
  if (["processing", "in_progress"].includes(status) || ["claimed", "in_progress"].includes(worker)) {
    return "processing";
  }
  return "pending";
}

function flattenTasks(snapshot) {
  const byId = new Map();
  const groups = snapshot && snapshot.tasks ? snapshot.tasks : {};
  for (const status of ACTIVE_STATUSES) {
    for (const rawTask of asArray(groups[status])) {
      if (!rawTask || !rawTask.task_id) {
        continue;
      }
      const task = { ...rawTask, status: rawTask.status || status };
      byId.set(String(task.task_id), task);
    }
  }
  for (const status of ["blocked", "finished", "archived"]) {
    for (const rawTask of asArray(groups[status])) {
      if (!rawTask || !rawTask.task_id) {
        continue;
      }
      const task = { ...rawTask, status: rawTask.status || status };
      if (!byId.has(String(task.task_id))) {
        byId.set(String(task.task_id), task);
      }
    }
  }
  for (const stale of asArray(groups.stale)) {
    if (!stale || !stale.task_id) {
      continue;
    }
    const taskId = String(stale.task_id);
    const existing = byId.get(taskId) || { ...stale, task_id: taskId, status: "processing" };
    byId.set(taskId, { ...existing, ...stale, status: existing.status || "processing", stale: true });
  }
  return Array.from(byId.values());
}

function setConnection(mode, label) {
  elements.connectionState.classList.remove("is-live", "is-degraded", "is-offline");
  elements.connectionState.classList.add(`is-${mode}`);
  elements.connectionLabel.textContent = label;
}

function renderSummary(snapshot) {
  const counts = snapshot.status_counts || {};
  for (const metric of ["active", "pending", "processing", "review", "blocked", "finished", "stale"]) {
    const target = document.querySelector(`#metric-${metric}`);
    target.textContent = formatCount(counts[metric]);
    target.title = String(numberValue(counts[metric]));
  }
  const totals = snapshot.cost_usage && snapshot.cost_usage.totals ? snapshot.cost_usage.totals : {};
  elements.lastSync.textContent = `Synced ${formatRelativeTime(snapshot.generated_at)}`;
  document.querySelector("#metric-tokens").textContent = formatCount(totals.total_tokens);
  document.querySelector("#metric-tokens").title = `${numberValue(totals.total_tokens)} tokens`;
  document.querySelector("#metric-cost").textContent = formatMoney(totals.cost_usd);
  const storage = snapshot && snapshot.storage_usage && typeof snapshot.storage_usage === "object"
    ? snapshot.storage_usage
    : null;
  elements.headerStorageManaged.textContent = storage
    ? (storage.scan_status === "scanning" ? "Calculating" : formatBytes(storage.managed_total_bytes))
    : "Unavailable";
  elements.headerStorageFree.textContent = storage
    ? `Free ${formatBytes(storage.disk_free_bytes)}`
    : "Free —";
  const sourceGraph = snapshot && snapshot.source_graph_telemetry
    && typeof snapshot.source_graph_telemetry === "object"
    ? snapshot.source_graph_telemetry
    : null;
  if (elements.headerSourceGraphRate && elements.headerSourceGraphDetail) {
    const gated = sourceGraph ? numberValue(sourceGraph.gated_tasks) : 0;
    const live = sourceGraph ? numberValue(sourceGraph.source_graph_live_tasks) : 0;
    const injected = sourceGraph ? numberValue(sourceGraph.source_graph_injected_only_tasks) : 0;
    const violations = sourceGraph ? numberValue(sourceGraph.policy_violations) : 0;
    elements.headerSourceGraphRate.textContent = gated ? `${numberValue(sourceGraph.live_rate).toFixed(1)}% live` : "No sample";
    elements.headerSourceGraphDetail.textContent = gated
      ? `${live}/${gated} live · ${injected} injected-only`
      : "No gated tasks";
    elements.headerSourceGraph.title = sourceGraph
      ? `${numberValue(sourceGraph.source_graph_calls)} calls · ${numberValue(sourceGraph.source_graph_hit_count)} hits · ${numberValue(sourceGraph.source_graph_zero_hit_calls)} zero-hit · ${formatBytes(sourceGraph.source_graph_bytes)} measured return bytes · ${violations} MCP contract violations`
      : "Source Graph telemetry unavailable";
  }
  const contextTelemetry = snapshot && snapshot.project_context_telemetry
    && typeof snapshot.project_context_telemetry === "object"
    ? snapshot.project_context_telemetry
    : {};
  for (const [name, card, value, detail] of [
    ["session_current_state", elements.headerSessionManager, elements.headerSessionManagerValue, elements.headerSessionManagerDetail],
    ["ai_memory", elements.headerAiMemory, elements.headerAiMemoryValue, elements.headerAiMemoryDetail],
    ["kb", elements.headerKb, elements.headerKbValue, elements.headerKbDetail],
  ]) {
    if (!value || !detail) continue;
    const telemetry = contextTelemetry[name] && typeof contextTelemetry[name] === "object"
      ? contextTelemetry[name]
      : null;
    const executed = telemetry ? numberValue(telemetry.executed_tasks) : 0;
    const hits = telemetry ? numberValue(telemetry.hit_count) : 0;
    const bytes = telemetry ? numberValue(telemetry.bytes) : 0;
    const degraded = telemetry ? numberValue(telemetry.degraded_tasks) : 0;
    value.textContent = telemetry ? `${formatCount(hits)} hits` : "No sample";
    detail.textContent = telemetry ? `${formatCount(executed)} runs · ${formatBytes(bytes)}` : "No evidence";
    if (card) card.title = telemetry
      ? `${formatCount(telemetry.requested_tasks)} requested · ${formatCount(executed)} executed · ${formatCount(hits)} hits · ${degraded} degraded`
      : "Context telemetry unavailable";
  }
  const preflight = snapshot && snapshot.environment_preflight
    && typeof snapshot.environment_preflight === "object"
    ? snapshot.environment_preflight
    : null;
  if (elements.headerPreflightValue && elements.headerPreflightDetail) {
    const errors = preflight ? asArray(preflight.errors) : [];
    const providers = preflight ? asArray(preflight.providers) : [];
    const readyProviders = providers.filter((item) => item && item.launchable).length;
    elements.headerPreflightValue.textContent = preflight
      ? (preflight.ok ? "Ready" : "Blocked")
      : "Unavailable";
    elements.headerPreflightDetail.textContent = preflight
      ? `${readyProviders}/${providers.length} adapters · ${errors.length} blockers`
      : "No evidence";
    if (elements.headerPreflight) {
      elements.headerPreflight.title = preflight
        ? (errors.length ? errors.join(" · ") : "Repository, policy and runtime preflight passed")
        : "Unified preflight unavailable";
    }
  }
}

function renderSourceHealth(snapshot) {
  const errors = asArray(snapshot.errors);
  if (errors.length === 0) {
    elements.sourceAlert.hidden = true;
    setConnection("live", "Live");
    return;
  }
  elements.sourceAlert.hidden = false;
  elements.sourceAlertTitle.textContent = "Partial data";
  const sources = Array.from(new Set(errors.map((error) => error.source).filter(Boolean)));
  elements.sourceAlertMessage.textContent = `${errors.length} read source issue${errors.length === 1 ? "" : "s"}: ${sources.slice(0, 4).join(", ")}`;
  setConnection("degraded", "Degraded");
}

function compactManagerIdentityReason(identity) {
  const payload = identity && typeof identity === "object" ? identity : {};
  const provider = String(payload.provider || "unknown");
  const role = String(payload.role || "unknown");
  const route = payload.manager_route && typeof payload.manager_route === "object" ? payload.manager_route : {};
  const pieces = [`role=${role}`, `provider=${provider}`];
  if (payload.reason) {
    pieces.push(`reason=${payload.reason}`);
  }
  if (route.window_id) {
    pieces.push(`window=${route.window_id}`);
  }
  if (route.thread_id) {
    pieces.push(`thread=${route.thread_id}`);
  }
  return pieces.join(" · ");
}

function renderManagerIdentity(snapshot) {
  if (!elements.identityAlert) {
    return;
  }
  const identity = snapshot && typeof snapshot.manager_identity === "object" ? snapshot.manager_identity : null;
  if (!identity) {
    elements.identityAlert.hidden = true;
    return;
  }
  const role = String(identity.role || "unknown");
  const route = identity.manager_route && typeof identity.manager_route === "object" ? identity.manager_route : {};
  const isManager = role === "manager" && Boolean(route.session_id || route.thread_id);
  const delivery = snapshot && typeof snapshot.callback_delivery === "object" ? snapshot.callback_delivery : {};
  const managerInboxReady = delivery.status === "manager_inbox" && delivery.healthy === true;
  const dispatcherReady = (
    delivery.dispatch_expected === true
    && delivery.healthy === true
    && delivery.dispatcher_running === true
    && delivery.registered === true
    && Boolean(delivery.repo_id)
    && delivery.repo_id === delivery.dispatcher_repo_id
  );
  const callbackReady = managerInboxReady || dispatcherReady;
  const target = snapshot && snapshot.manager_identity_target && typeof snapshot.manager_identity_target === "object"
    ? snapshot.manager_identity_target
    : {};
  const routeState = String(target.capability_state || route.route_state || "unknown").toLowerCase();
  const routeReason = String(target.reason || "");
  const routeReady = ["available", "ready"].includes(routeState);
  const fullyReady = isManager && routeReady && callbackReady;
  elements.identityAlert.hidden = false;
  elements.identityAlert.classList.toggle("identity-ok", fullyReady);
  elements.identityAlert.classList.toggle("identity-warn", !fullyReady);
  if (!isManager) {
    elements.identityAlertTitle.textContent = "Manager identity unverified";
  } else if (!routeReady) {
    elements.identityAlertTitle.textContent = "Manager verified · callback route pending";
  } else if (!callbackReady) {
    elements.identityAlertTitle.textContent = "Manager verified · callback unavailable";
  } else {
    elements.identityAlertTitle.textContent = "Manager identity, route and callback verified";
  }
  const identityReason = compactManagerIdentityReason(identity);
  const deliveryProblems = Array.isArray(delivery.problems) ? delivery.problems.filter(Boolean) : [];
  const deliveryReason = callbackReady
    ? `callback=${String(delivery.status || "ready")}`
    : `callback=${String(delivery.status || "unknown")}${deliveryProblems.length ? ` · ${deliveryProblems.join(",")}` : ""}`;
  const routeStatus = `route=${routeState}${routeReason ? ` · route_reason=${routeReason}` : ""}`;
  elements.identityAlertMessage.textContent = `${identityReason} · ${routeStatus} · ${deliveryReason}`;
}

function renderCallbackObservability(snapshot) {
  if (!elements.callbackObservability) return;
  const health = snapshot && typeof snapshot.callback_bridge_health === "object"
    ? snapshot.callback_bridge_health
    : {};
  const delivery = snapshot && typeof snapshot.callback_delivery === "object"
    ? snapshot.callback_delivery
    : {};
  const states = health.by_state && typeof health.by_state === "object" ? health.by_state : {};
  const backlog = Number.isFinite(Number(health.backlog_count))
    ? numberValue(health.backlog_count)
    : numberValue(states.pending) + numberValue(states.inflight);
  const lastDelivery = health.last_delivered_at || delivery.last_delivery_at || delivery.last_claude_delivery_at || "";
  const retries = numberValue(health.retry_count);
  const problems = asArray(delivery.problems).filter(Boolean);
  const deadLetters = numberValue(states.dead_letter);
  const degradedReason = health.last_dead_letter_error || delivery.last_deferral_reason || problems.join(", ") || "none";
  elements.callbackBacklog.textContent = formatCount(backlog);
  elements.callbackBacklog.title = `pending=${numberValue(states.pending)}, inflight=${numberValue(states.inflight)}`;
  elements.callbackDelivery.textContent = lastDelivery ? formatRelativeTime(lastDelivery) : "Never";
  elements.callbackDelivery.title = lastDelivery || "No delivered callback recorded";
  elements.callbackRetries.textContent = formatCount(retries);
  elements.callbackRetries.title = `max attempts=${numberValue(health.max_attempts)}`;
  elements.callbackDegraded.textContent = deadLetters || problems.length ? degradedReason : "Healthy";
  elements.callbackDegraded.title = degradedReason;
  elements.callbackObservability.classList.toggle("is-degraded", Boolean(deadLetters || problems.length));
}

function replaceSelectOptions(select, values, allLabel, currentValue) {
  const fragment = document.createDocumentFragment();
  const allOption = document.createElement("option");
  allOption.value = "all";
  allOption.textContent = allLabel;
  fragment.appendChild(allOption);
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    fragment.appendChild(option);
  }
  select.replaceChildren(fragment);
  select.value = values.includes(currentValue) ? currentValue : "all";
}

function renderFilterOptions() {
  const topics = Array.from(new Set(state.tasks.map((task) => String(task.topic || "unknown")))).sort((a, b) => a.localeCompare(b));
  const runners = Array.from(new Set(state.tasks.map((task) => String(task.runner || "unassigned")))).sort((a, b) => a.localeCompare(b));
  replaceSelectOptions(elements.topicFilter, topics, "All topics", state.topic);
  replaceSelectOptions(elements.runnerFilter, runners, "All runners", state.runner);
  state.topic = elements.topicFilter.value;
  state.runner = elements.runnerFilter.value;
}

function filteredTasks() {
  const needle = state.search.trim().toLowerCase();
  const tasks = state.tasks.filter((task) => {
    if (state.status === "stale" && !task.stale) {
      return false;
    }
    if (state.status !== "all" && state.status !== "stale" && canonicalStatus(task) !== state.status) {
      return false;
    }
    if (state.topic !== "all" && String(task.topic || "unknown") !== state.topic) {
      return false;
    }
    if (state.runner !== "all" && String(task.runner || "unassigned") !== state.runner) {
      return false;
    }
    if (!needle) {
      return true;
    }
    const searchable = [task.task_id, task.topic, task.runner, task.claimed_by, task.objective, task.validation_status]
      .map((value) => String(value || "").toLowerCase())
      .join(" ");
    return searchable.includes(needle);
  });

  tasks.sort((left, right) => {
    if (state.sort === "task") {
      return String(left.task_id).localeCompare(String(right.task_id));
    }
    if (state.sort === "topic") {
      return String(left.topic || "").localeCompare(String(right.topic || "")) || String(left.task_id).localeCompare(String(right.task_id));
    }
    if (state.sort === "updated") {
      const leftTime = new Date(left.updated_at || left.last_activity_at || 0).getTime() || 0;
      const rightTime = new Date(right.updated_at || right.last_activity_at || 0).getTime() || 0;
      return rightTime - leftTime || String(left.task_id).localeCompare(String(right.task_id));
    }
    const leftOrder = STATUS_ORDER[canonicalStatus(left)] ?? 99;
    const rightOrder = STATUS_ORDER[canonicalStatus(right)] ?? 99;
    return leftOrder - rightOrder || Number(Boolean(right.stale)) - Number(Boolean(left.stale)) || String(left.task_id).localeCompare(String(right.task_id));
  });
  return tasks;
}

function statusBadge(status) {
  return createElement("span", `status-badge ${status}`, status);
}

function validationClass(value) {
  const normalized = String(value || "").toLowerCase();
  if (["pass", "passed", "ok", "success"].includes(normalized)) {
    return "pass";
  }
  if (["fail", "failed", "error", "blocked"].includes(normalized)) {
    return "fail";
  }
  return "";
}

function renderTaskTable() {
  const tasks = filteredTasks();
  const fragment = document.createDocumentFragment();

  for (const task of tasks) {
    const row = document.createElement("tr");
    const taskId = String(task.task_id);
    if (taskId === state.selectedTaskId) {
      row.classList.add("is-selected");
    }

    const statusCell = document.createElement("td");
    statusCell.appendChild(statusBadge(canonicalStatus(task)));
    row.appendChild(statusCell);

    const taskCell = document.createElement("td");
    const taskButton = createElement("button", "task-link", taskId);
    taskButton.type = "button";
    taskButton.dataset.taskId = taskId;
    taskButton.title = taskId;
    taskCell.appendChild(taskButton);
    if (task.objective) {
      const objective = createElement("span", "cell-secondary", task.objective);
      objective.title = String(task.objective);
      taskCell.appendChild(objective);
    }
    row.appendChild(taskCell);

    const topicCell = document.createElement("td");
    const topic = createElement("span", "cell-primary", task.topic || "unknown");
    topic.title = topic.textContent;
    topicCell.appendChild(topic);
    row.appendChild(topicCell);

    const runnerCell = document.createElement("td");
    const runner = createElement("span", "cell-primary", task.runner || "unassigned");
    runner.title = runner.textContent;
    runnerCell.appendChild(runner);
    if (task.claimed_by && task.claimed_by !== task.runner) {
      runnerCell.appendChild(createElement("span", "cell-secondary", `claimed: ${task.claimed_by}`));
    }
    row.appendChild(runnerCell);

    const modelCell = document.createElement("td");
    const modelName = task.model || task.recommended_model || "";
    const model = createElement("span", modelName ? "cell-primary" : "cell-secondary", modelName || "unknown");
    model.title = modelName || "No model recorded";
    modelCell.appendChild(model);
    row.appendChild(modelCell);

    const activityCell = createElement("td", "activity-cell", formatRelativeTime(task.updated_at || task.last_activity_at));
    activityCell.title = String(task.updated_at || task.last_activity_at || "Unknown");
    row.appendChild(activityCell);

    const signalCell = document.createElement("td");
    const signalStack = createElement("div", "signal-stack");
    if (task.stale) {
      signalStack.appendChild(createElement("span", "signal-badge stale", "stale"));
    }
    const validation = String(task.validation_status || "").trim();
    if (validation && validation.toLowerCase() !== "unreported") {
      signalStack.appendChild(createElement("span", `signal-badge ${validationClass(validation)}`, validation));
    }
    if (!signalStack.childNodes.length) {
      signalStack.appendChild(createElement("span", "cell-secondary", "None"));
    }
    signalCell.appendChild(signalStack);
    row.appendChild(signalCell);
    fragment.appendChild(row);
  }

  elements.tableBody.replaceChildren(fragment);
  elements.tableLoading.hidden = true;
  elements.tableEmpty.hidden = tasks.length !== 0;
  elements.filteredCount.textContent = `${tasks.length} shown`;
}

function renderStats(target, values) {
  const rows = asArray(values);
  if (rows.length === 0) {
    target.replaceChildren(createElement("div", "panel-list-empty", "No active queue data"));
    return;
  }
  const maximum = Math.max(...rows.map((row) => numberValue(row.total)), 1);
  const fragment = document.createDocumentFragment();
  for (const item of rows.slice(0, 40)) {
    const row = createElement("div", "stat-row");
    const main = createElement("div", "stat-main");
    const labels = createElement("div", "stat-labels");
    const name = createElement("span", "stat-name", item.name || "unknown");
    name.title = name.textContent;
    const breakdown = createElement(
      "span",
      "stat-breakdown",
      `${numberValue(item.processing)} run | ${numberValue(item.review)} review | ${numberValue(item.blocked)} blocked`,
    );
    labels.append(name, breakdown);
    const track = createElement("div", "stat-track");
    const fill = createElement("span", "stat-fill");
    fill.style.width = `${Math.max(3, Math.round((numberValue(item.total) / maximum) * 100))}%`;
    track.appendChild(fill);
    main.append(labels, track);
    row.append(main, createElement("strong", "stat-total", numberValue(item.total)));
    fragment.appendChild(row);
  }
  target.replaceChildren(fragment);
}

function renderUsage(snapshot) {
  const costUsage = snapshot.cost_usage || {};
  const totals = costUsage.totals || {};
  const ledger = costUsage.ledger || {};
  const aggregates = ledger.aggregates || {};
  const byRunner = aggregates.by_runner && typeof aggregates.by_runner === "object"
    ? aggregates.by_runner
    : {};

  if (!totals.available) {
    elements.usageList.replaceChildren(createElement("div", "panel-list-empty", "Usage data unavailable"));
    return;
  }

  const fragment = document.createDocumentFragment();
  const overview = createElement("div", "usage-overview");
  const overviewValues = [
    ["Records", formatCount(totals.records)],
    ["Input", formatCount(totals.input_tokens)],
    ["Output", formatCount(totals.output_tokens)],
    ["Cost", formatMoney(totals.cost_usd)],
  ];
  for (const [label, value] of overviewValues) {
    const metric = createElement("div", "usage-metric");
    metric.title = `${label}: ${value}`;
    metric.append(createElement("span", "usage-label", label), createElement("strong", "", value));
    overview.appendChild(metric);
  }
  fragment.appendChild(overview);

  const rows = Object.entries(byRunner)
    .filter(([, value]) => value && typeof value === "object")
    .map(([name, value]) => ({ name, ...value }))
    .sort((left, right) => numberValue(right.total_tokens) - numberValue(left.total_tokens) || left.name.localeCompare(right.name));
  if (rows.length === 0) {
    fragment.appendChild(createElement("div", "panel-list-empty compact", "No usage recorded"));
    elements.usageList.replaceChildren(fragment);
    return;
  }

  const maximum = Math.max(...rows.map((row) => numberValue(row.total_tokens)), 1);
  for (const item of rows.slice(0, 40)) {
    const row = createElement("div", "stat-row usage-row");
    row.title = `${numberValue(item.records)} records, ${numberValue(item.total_tokens)} total tokens, ${formatMoney(item.cost_usd)}`;
    const main = createElement("div", "stat-main");
    const labels = createElement("div", "stat-labels");
    const name = createElement("span", "stat-name", item.name || "unknown");
    name.title = name.textContent;
    const breakdown = createElement(
      "span",
      "stat-breakdown",
      `${formatCount(item.input_tokens)} in | ${formatCount(item.output_tokens)} out | ${formatMoney(item.cost_usd)}`,
    );
    labels.append(name, breakdown);
    const track = createElement("div", "stat-track");
    const fill = createElement("span", "stat-fill usage-fill");
    fill.style.width = `${Math.max(3, Math.round((numberValue(item.total_tokens) / maximum) * 100))}%`;
    track.appendChild(fill);
    main.append(labels, track);
    row.append(main, createElement("strong", "stat-total", formatCount(item.total_tokens)));
    fragment.appendChild(row);
  }
  elements.usageList.replaceChildren(fragment);
}

function renderToolUse(snapshot) {
  const telemetry = snapshot && snapshot.source_graph_telemetry
    && typeof snapshot.source_graph_telemetry === "object"
    ? snapshot.source_graph_telemetry
    : null;
  if (!telemetry || !numberValue(telemetry.gated_tasks)) {
    elements.toolUseList.replaceChildren(
      createElement("div", "panel-list-empty", "No authenticated worker tool-use evidence yet"),
    );
    return;
  }

  const fragment = document.createDocumentFragment();
  const overview = createElement("div", "usage-overview tool-use-overview");
  const overviewValues = [
    ["Live", `${numberValue(telemetry.live_rate).toFixed(1)}%`],
    ["Live tasks", `${formatCount(telemetry.source_graph_live_tasks)}/${formatCount(telemetry.gated_tasks)}`],
    ["Injected only", formatCount(telemetry.source_graph_injected_only_tasks)],
    ["Missing/stale", formatCount(
      numberValue(telemetry.source_graph_missing_tasks)
      + numberValue(telemetry.source_graph_stale_or_cached_tasks),
    )],
    ["SG calls", formatCount(telemetry.source_graph_calls)],
    ["Entity hits", formatCount(telemetry.source_graph_hit_count)],
    ["Zero-hit calls", formatCount(telemetry.source_graph_zero_hit_calls)],
    ["Failed calls", formatCount(telemetry.source_graph_failed_calls)],
    ["SG bytes", formatBytes(telemetry.source_graph_bytes)],
    ["MCP violations", formatCount(telemetry.policy_violations)],
    ["Raw discovery denied", formatCount(telemetry.raw_discovery_denials)],
    ["Denial evidence", `${formatCount(telemetry.provider_denial_evidence_tasks)}/${formatCount(telemetry.gated_tasks)} tasks`],
    ["Tampered", formatCount(telemetry.tampered_ledger_tasks)],
  ];
  for (const [label, value] of overviewValues) {
    const metric = createElement("div", "usage-metric");
    metric.append(createElement("span", "usage-label", label), createElement("strong", "", value));
    overview.appendChild(metric);
  }
  fragment.appendChild(overview);
  fragment.appendChild(createElement(
    "div",
    "telemetry-note",
    "Bytes are authenticated bounded tool return bytes. They are not inferred token or cost savings. Denial counts appear only when the provider emitted structured denial evidence.",
  ));

  const blockedReasons = telemetry.blocked_reason_counts
    && typeof telemetry.blocked_reason_counts === "object"
    ? Object.entries(telemetry.blocked_reason_counts)
      .map(([reason, count]) => ({ reason, count: numberValue(count) }))
      .filter((item) => item.count > 0)
      .sort((left, right) => right.count - left.count || left.reason.localeCompare(right.reason))
      .slice(0, 5)
    : [];
  for (const item of blockedReasons) {
    const row = createElement("div", "stat-row tool-use-row");
    const main = createElement("div", "stat-main");
    const labels = createElement("div", "stat-labels");
    labels.append(
      createElement("span", "stat-name", "Blocked tool-use gate"),
      createElement("span", "stat-breakdown", item.reason),
    );
    main.appendChild(labels);
    row.append(main, createElement("strong", "stat-total", formatCount(item.count)));
    fragment.appendChild(row);
  }

  const byAdapter = telemetry.by_adapter && typeof telemetry.by_adapter === "object"
    ? telemetry.by_adapter
    : {};
  const rows = Object.entries(byAdapter)
    .filter(([, value]) => value && typeof value === "object")
    .map(([name, value]) => ({ name, ...value }))
    .sort((left, right) => numberValue(right.gated_tasks) - numberValue(left.gated_tasks) || left.name.localeCompare(right.name));
  for (const item of rows) {
    const gated = numberValue(item.gated_tasks);
    const live = numberValue(item.live_tasks);
    const row = createElement("div", "stat-row tool-use-row");
    row.title = `${numberValue(item.source_graph_calls)} Source Graph calls · ${numberValue(item.source_graph_hit_count)} hits · ${numberValue(item.source_graph_zero_hit_calls)} zero-hit · ${numberValue(item.source_graph_failed_calls)} failed · ${numberValue(item.raw_discovery_denials)} raw discovery denials · ${numberValue(item.policy_violations)} MCP contract violations`;
    const main = createElement("div", "stat-main");
    const labels = createElement("div", "stat-labels");
    labels.append(
      createElement("span", "stat-name", item.name || "unknown"),
      createElement(
        "span",
        "stat-breakdown",
        `${live} live | ${numberValue(item.injected_only_tasks)} injected | ${numberValue(item.missing_or_stale_tasks)} missing/stale`,
      ),
    );
    const track = createElement("div", "stat-track");
    const fill = createElement("span", "stat-fill");
    fill.style.width = `${gated ? Math.max(3, Math.round((live / gated) * 100)) : 0}%`;
    track.appendChild(fill);
    main.append(labels, track);
    row.append(main, createElement("strong", "stat-total", gated ? `${Math.round((live / gated) * 100)}%` : "0%"));
    fragment.appendChild(row);
  }
  elements.toolUseList.replaceChildren(fragment);
}

function renderPlanDag(snapshot) {
  if (!elements.planDag) return;
  const plan = snapshot && snapshot.task_plan && typeof snapshot.task_plan === "object"
    ? snapshot.task_plan
    : null;
  if (!plan || !Array.isArray(plan.layers) || !plan.task_ids || !plan.task_ids.length) {
    elements.planDag.replaceChildren(
      createElement("div", "panel-list-empty", "No task dependency graph yet"),
    );
    return;
  }

  const fragment = document.createDocumentFragment();
  const overview = createElement("div", "usage-overview plan-overview");
  for (const [label, value] of [
    ["Nodes", formatCount(plan.task_ids.length)],
    ["Edges", formatCount(plan.edge_count)],
    ["Ready capacity", formatCount(plan.ready_capacity)],
    ["Blocked total", formatCount(plan.blocked_count)],
    ["DAG-blocked", formatCount(plan.dependency_blocked_count)],
    ["Lifecycle-blocked", formatCount(plan.lifecycle_blocked_count)],
    ["Critical path", formatCount(plan.critical_path_length)],
    ["DAG valid", plan.dag_valid ? "yes" : "no"],
  ]) {
    const metric = createElement("div", "usage-metric");
    metric.append(createElement("span", "usage-label", label), createElement("strong", "", value));
    overview.appendChild(metric);
  }
  fragment.appendChild(overview);

  if (Array.isArray(plan.cycle_nodes) && plan.cycle_nodes.length) {
    fragment.appendChild(createElement(
      "div", "plan-alert", `Cycle detected: ${plan.cycle_nodes.join(", ")}`,
    ));
  }

  const dependencies = plan.dependencies && typeof plan.dependencies === "object" ? plan.dependencies : {};
  const blockers = plan.blockers && typeof plan.blockers === "object" ? plan.blockers : {};
  const lifecycle = plan.lifecycle && typeof plan.lifecycle === "object" ? plan.lifecycle : {};
  const ready = new Set(Array.isArray(plan.ready) ? plan.ready : []);
  const critical = new Set(Array.isArray(plan.critical_path) ? plan.critical_path : []);
  const dag = createElement("div", "plan-dag-grid");
  for (const layer of plan.layers) {
    if (!layer || !Array.isArray(layer.task_ids)) continue;
    const column = createElement("section", "plan-layer");
    column.appendChild(createElement("h3", "plan-layer-title", `Stage ${numberValue(layer.index) + 1}`));
    for (const taskId of layer.task_ids) {
      const card = createElement("div", "plan-node");
      if (critical.has(taskId)) card.classList.add("critical");
      if (ready.has(taskId)) card.classList.add("ready");
      if (Array.isArray(blockers[taskId]) && blockers[taskId].length) card.classList.add("blocked");
      card.append(
        createElement("strong", "plan-node-id", taskId),
        createElement("span", "plan-node-state", String(lifecycle[taskId] || "pending")),
      );
      const deps = Array.isArray(dependencies[taskId]) ? dependencies[taskId] : [];
      if (deps.length) card.appendChild(createElement("span", "plan-node-deps", `← ${deps.join(", ")}`));
      const blockedBy = Array.isArray(blockers[taskId]) ? blockers[taskId] : [];
      if (blockedBy.length) card.appendChild(createElement("span", "plan-node-blockers", `blocked: ${blockedBy.join(", ")}`));
      column.appendChild(card);
    }
    dag.appendChild(column);
  }
  fragment.appendChild(dag);

  if (Array.isArray(plan.critical_path) && plan.critical_path.length) {
    fragment.appendChild(createElement(
      "div", "telemetry-note", `Critical path: ${plan.critical_path.join(" → ")}`,
    ));
  }
  elements.planDag.replaceChildren(fragment);
}

function renderWorkforce(snapshot) {
  if (!elements.workforceList) return;
  const catalog = snapshot && snapshot.workforce_catalog
    && typeof snapshot.workforce_catalog === "object"
    ? snapshot.workforce_catalog
    : null;
  const workers = catalog ? asArray(catalog.workers) : [];
  if (!workers.length) {
    elements.workforceList.replaceChildren(
      createElement("div", "panel-list-empty", "No configured worker models"),
    );
    return;
  }
  const fragment = document.createDocumentFragment();
  const summary = catalog.summary && typeof catalog.summary === "object" ? catalog.summary : {};
  const overview = createElement("div", "usage-overview workforce-overview");
  for (const [label, value] of [
    ["Models", formatCount(summary.workers)],
    ["Enabled", formatCount(summary.enabled)],
    ["Available", formatCount(summary.available)],
    ["Observed", formatCount(summary.observed)],
  ]) {
    const metric = createElement("div", "usage-metric");
    metric.append(createElement("span", "usage-label", label), createElement("strong", "", value));
    overview.appendChild(metric);
  }
  fragment.appendChild(overview);
  const list = createElement("div", "workforce-grid");
  for (const worker of workers) {
    if (!worker || typeof worker !== "object") continue;
    const outcomes = worker.outcomes && typeof worker.outcomes === "object" ? worker.outcomes : {};
    const row = createElement("article", "workforce-row");
    if (worker.available) row.classList.add("available");
    if (!worker.enabled) row.classList.add("disabled");
    const heading = createElement("div", "workforce-heading");
    heading.append(
      createElement("strong", "", worker.worker_id || worker.model || "worker"),
      createElement("span", "", `${worker.provider || "provider"} · ${worker.adapter_id || "adapter"}`),
    );
    const score = createElement(
      "strong",
      "workforce-score",
      `${numberValue(worker.effective_score).toFixed(1)}`,
    );
    score.title = worker.observed_score === null || worker.observed_score === undefined
      ? `Conservative prior + manager adjustment ${numberValue(worker.manager_score_adjustment)}`
      : `Observed ${numberValue(worker.observed_score).toFixed(1)} + manager adjustment ${numberValue(worker.manager_score_adjustment)}`;
    row.append(heading, score);
    row.appendChild(createElement(
      "div",
      "workforce-metrics",
      `${worker.readiness_status || (worker.available ? "ready" : "unavailable")} · ${worker.availability_observed ? "access observed" : "access unverified"} · ${formatCount(outcomes.sample_count)} samples · ${outcomes.accepted_rate === null || outcomes.accepted_rate === undefined ? "accept n/a" : `${(numberValue(outcomes.accepted_rate) * 100).toFixed(1)}% accepted`} · ${formatCount(outcomes.retry_count)} retries`,
    ));
    list.appendChild(row);
  }
  fragment.appendChild(list);
  fragment.appendChild(createElement(
    "div",
    "telemetry-note",
    "Scores use canonical outcomes; missing samples use a labeled conservative prior. Provider quota is never fabricated.",
  ));
  elements.workforceList.replaceChildren(fragment);
}

function formatBytes(value) {
  let amount = Math.max(0, numberValue(value));
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[unit]}`;
}

function renderStorage(snapshot) {
  const usage = snapshot && snapshot.storage_usage && typeof snapshot.storage_usage === "object"
    ? snapshot.storage_usage
    : null;
  if (!usage) {
    elements.storageList.replaceChildren(createElement("div", "panel-list-empty", "Storage data unavailable"));
    return;
  }
  const fragment = document.createDocumentFragment();
  const overview = createElement("div", "usage-overview storage-overview");
  const overviewValues = [
    ["Managed", formatBytes(usage.managed_total_bytes)],
    ["Repo data", formatBytes(usage.repo_data_bytes)],
    ["Worker trees", formatBytes(usage.worker_tree_bytes)],
    ["Quarantine", formatBytes(usage.quarantine_bytes)],
  ];
  for (const [label, value] of overviewValues) {
    const metric = createElement("div", "usage-metric");
    metric.title = `${label}: ${value}`;
    metric.append(createElement("span", "usage-label", label), createElement("strong", "", value));
    overview.appendChild(metric);
  }
  fragment.appendChild(overview);

  const rows = [
    ["Repository .aiworkhub", `${formatBytes(usage.repo_data_bytes)} · ${formatCount(usage.repo_data_files)} files`],
    ["Retained worker trees", `${formatBytes(usage.worker_tree_bytes)} · ${formatCount(usage.worker_tree_count)} trees`],
    ["Safe reclaimable", formatBytes(usage.safe_reclaimable_bytes)],
    ["Quarantine", formatBytes(usage.quarantine_bytes)],
    ["Disk", `${formatBytes(usage.disk_used_bytes)} / ${formatBytes(usage.disk_total_bytes)} · ${numberValue(usage.disk_used_percent).toFixed(1)}% used`],
  ];
  for (const [label, value] of rows) {
    const row = createElement("div", "storage-row");
    row.append(createElement("span", "storage-label", label), createElement("strong", "storage-value", value));
    fragment.appendChild(row);
  }
  const components = asArray(usage.components)
    .filter((item) => item && typeof item === "object")
    .sort((left, right) => numberValue(right.bytes) - numberValue(left.bytes));
  if (components.length) {
    fragment.appendChild(createElement("div", "storage-section-title", "Repository components"));
    for (const component of components) {
      const row = createElement("div", "storage-row storage-component-row");
      row.append(
        createElement("span", "storage-label", String(component.id || "component")),
        createElement(
          "strong",
          "storage-value",
          `${formatBytes(component.bytes)} · ${formatCount(component.files)} files`,
        ),
      );
      fragment.appendChild(row);
    }
  }
  const retention = usage.retention_preview && typeof usage.retention_preview === "object"
    ? usage.retention_preview
    : null;
  if (retention) {
    fragment.appendChild(createElement("div", "storage-section-title", "Retention dry run"));
    for (const [label, value] of [
      ["Policy", `keep ${formatCount(retention.policy_days)} days`],
      ["Size cap", `${formatBytes(retention.current_bytes)} / ${formatBytes(retention.max_bytes)}`],
      ["Eligible", `${formatCount(retention.eligible_count)} trees · ${formatBytes(retention.eligible_bytes)}`],
      ["After quarantine", formatBytes(retention.projected_bytes)],
      ["Protected", `${formatCount(retention.protected_count)} trees`],
      ["Scope", retention.repository_scoped ? "This repository only" : "Unavailable"],
    ]) {
      const row = createElement("div", "storage-row storage-retention-row");
      row.append(createElement("span", "storage-label", label), createElement("strong", "storage-value", value));
      fragment.appendChild(row);
    }
    fragment.appendChild(createElement(
      "div",
      "telemetry-note",
      "Preview only. Unsaved, unpushed, active, foreign-repository and orphaned worktrees are protected.",
    ));
    const registrations = retention.registrations && typeof retention.registrations === "object"
      ? retention.registrations
      : null;
    if (registrations) {
      for (const [label, value] of [
        ["Git registrations", `${formatCount(registrations.aiworkhub_registered_count)} AIWorkHub / ${formatCount(registrations.registered_count)} total`],
        ["Stale registrations", `${formatCount(registrations.stale_candidate_count)} owned · ${formatCount(registrations.foreign_stale_count)} foreign`],
        ["Preview overflow", formatCount(registrations.candidate_overflow_count)],
        ["Prune status", registrations.safe_to_prune ? "Ready for confirmation" : "Protected / nothing eligible"],
      ]) {
        const row = createElement("div", "storage-row storage-retention-row");
        row.append(createElement("span", "storage-label", label), createElement("strong", "storage-value", value));
        fragment.appendChild(row);
      }
    }
  }
  const terminalLogs = usage.terminal_log_retention && typeof usage.terminal_log_retention === "object"
    ? usage.terminal_log_retention
    : null;
  if (terminalLogs) {
    fragment.appendChild(createElement("div", "storage-section-title", "Terminal log retention"));
    for (const [label, value] of [
      ["Policy", `keep ${formatCount(terminalLogs.logs_days)} days · latest ${formatCount(terminalLogs.keep_last_per_task)} runs/task`],
      ["Current", formatBytes(terminalLogs.current_bytes)],
      ["Eligible", `${formatCount(terminalLogs.candidate_count)} runs · ${formatBytes(terminalLogs.candidate_bytes)}`],
      ["After quarantine", formatBytes(terminalLogs.projected_bytes)],
      ["Protected", `${formatCount(terminalLogs.protected_count)} runs`],
    ]) {
      const row = createElement("div", "storage-row storage-retention-row");
      row.append(createElement("span", "storage-label", label), createElement("strong", "storage-value", value));
      fragment.appendChild(row);
    }
    fragment.appendChild(createElement(
      "div",
      "telemetry-note",
      terminalLogs.ok === false
        ? `Terminal log scan degraded: ${String(terminalLogs.error || "unknown")}`
        : "The append-only process ledger and active/review task output are never cleanup candidates.",
    ));
  }
  const runtimeStorage = snapshot && snapshot.extension_runtime_storage
    && typeof snapshot.extension_runtime_storage === "object"
    ? snapshot.extension_runtime_storage
    : null;
  if (runtimeStorage) {
    fragment.appendChild(createElement("div", "storage-section-title", "Extension runtime cache"));
    for (const [label, value] of [
      ["Scope", "Shared AIWorkHub extension storage"],
      ["Generations", `${formatCount(runtimeStorage.generation_count)} · keep latest ${formatCount(runtimeStorage.keep_generations)}`],
      ["Current", formatBytes(runtimeStorage.current_bytes)],
      ["Eligible", `${formatCount(runtimeStorage.candidate_count)} generations · ${formatBytes(runtimeStorage.candidate_bytes)}`],
      ["Protected", `${formatCount(runtimeStorage.protected_count)} generations`],
    ]) {
      const row = createElement("div", "storage-row storage-retention-row");
      row.append(createElement("span", "storage-label", label), createElement("strong", "storage-value", value));
      fragment.appendChild(row);
    }
    fragment.appendChild(createElement(
      "div",
      "telemetry-note",
      runtimeStorage.ok === false
        ? `Runtime cache scan degraded: ${String(runtimeStorage.error || "unknown")}`
        : "Current, latest-three rollback and every live or invalid-lease generation are protected.",
    ));
  }
  const batches = asArray(usage.quarantine_batches)
    .filter((item) => item && typeof item === "object");
  if (batches.length) {
    fragment.appendChild(createElement("div", "storage-section-title", "Quarantine batches"));
    for (const batch of batches) {
      const row = createElement("div", "storage-batch-row");
      const detail = createElement("div", "storage-batch-detail");
      detail.append(
        createElement("strong", "", String(batch.batch_id || "batch")),
        createElement(
          "span",
          "storage-label",
          `${formatCount(batch.quarantined_count)} quarantined · ${formatBytes(batch.bytes)} · restore until ${batch.restore_deadline ? new Date(batch.restore_deadline).toLocaleString() : "unknown"}`,
        ),
      );
      const actions = createElement("div", "storage-batch-actions");
      if (numberValue(batch.quarantined_count) > 0) {
        const restore = createElement("button", "secondary-button", "Restore");
        restore.type = "button";
        restore.dataset.restoreBatch = String(batch.batch_id || "");
        actions.appendChild(restore);
      }
      if (batch.purge_eligible) {
        const purge = createElement("button", "danger-button", "Purge");
        purge.type = "button";
        purge.dataset.purgeBatch = String(batch.batch_id || "");
        actions.appendChild(purge);
      }
      row.append(detail, actions);
      fragment.appendChild(row);
    }
  }
  const terminalBatches = asArray(usage.terminal_log_quarantine_batches)
    .filter((item) => item && typeof item === "object");
  if (terminalBatches.length) {
    fragment.appendChild(createElement("div", "storage-section-title", "Terminal log quarantine"));
    for (const batch of terminalBatches) {
      const row = createElement("div", "storage-batch-row");
      const detail = createElement("div", "storage-batch-detail");
      detail.append(
        createElement("strong", "", String(batch.batch_id || "batch")),
        createElement("span", "storage-label", `${formatCount(batch.quarantined_count)} runs · ${formatBytes(batch.bytes)} · restore until ${batch.restore_deadline ? new Date(batch.restore_deadline).toLocaleString() : "unknown"}`),
      );
      const actions = createElement("div", "storage-batch-actions");
      if (numberValue(batch.quarantined_count) > 0) {
        const restore = createElement("button", "secondary-button", "Restore logs");
        restore.type = "button";
        restore.dataset.restoreTerminalLogBatch = String(batch.batch_id || "");
        actions.appendChild(restore);
      }
      if (batch.purge_eligible) {
        const purge = createElement("button", "danger-button", "Purge logs");
        purge.type = "button";
        purge.dataset.purgeTerminalLogBatch = String(batch.batch_id || "");
        actions.appendChild(purge);
      }
      row.append(detail, actions);
      fragment.appendChild(row);
    }
  }
  const runtimeBatches = asArray(runtimeStorage && runtimeStorage.quarantine_batches)
    .filter((item) => item && typeof item === "object");
  if (runtimeBatches.length) {
    fragment.appendChild(createElement("div", "storage-section-title", "Runtime cache quarantine"));
    for (const batch of runtimeBatches) {
      const row = createElement("div", "storage-batch-row");
      const detail = createElement("div", "storage-batch-detail");
      detail.append(
        createElement("strong", "", String(batch.batch_id || "batch")),
        createElement("span", "storage-label", `${formatCount(batch.quarantined_count)} generations · ${formatBytes(batch.bytes)} · restore until ${batch.restore_deadline ? new Date(batch.restore_deadline).toLocaleString() : "unknown"}`),
      );
      const actions = createElement("div", "storage-batch-actions");
      if (numberValue(batch.quarantined_count) > 0) {
        const restore = createElement("button", "secondary-button", "Restore runtimes");
        restore.type = "button";
        restore.dataset.restoreRuntimeBatch = String(batch.batch_id || "");
        actions.appendChild(restore);
      }
      if (batch.purge_eligible) {
        const purge = createElement("button", "danger-button", "Purge runtimes");
        purge.type = "button";
        purge.dataset.purgeRuntimeBatch = String(batch.batch_id || "");
        actions.appendChild(purge);
      }
      row.append(detail, actions);
      fragment.appendChild(row);
    }
  }
  const stateLabel = String(usage.scan_status || "unknown");
  const timestamp = usage.scanned_at ? new Date(usage.scanned_at).toLocaleString() : "calculating now";
  fragment.appendChild(createElement("div", "storage-scan-state", `${stateLabel} · ${timestamp}`));
  elements.storageList.replaceChildren(fragment);
}

function renderSystemLogs(snapshotOrEntries) {
  const entries = Array.isArray(snapshotOrEntries)
    ? snapshotOrEntries
    : asArray(snapshotOrEntries && snapshotOrEntries.system_logs);
  state.systemLogs = entries.slice(0, 1200);
  elements.systemLogCount.textContent = `${entries.length} event${entries.length === 1 ? "" : "s"}`;
  const latest = entries[0];
  elements.lastSystemLog.textContent = latest
    ? `${String(latest.level || "info").toUpperCase()} · ${String(latest.component || "system")} · ${String(latest.message || "")}`
    : "No system events yet";
  elements.lastSystemLog.title = elements.lastSystemLog.textContent;
  if (!entries.length) {
    elements.systemLogList.replaceChildren(createElement("div", "panel-list-empty compact", "No system events yet"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const entry of entries.slice(0, 1200)) {
    const row = createElement("div", `system-log-row level-${String(entry.level || "info")}`);
    const timestamp = entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : "—";
    row.append(
      createElement("time", "system-log-time", timestamp),
      createElement("span", "system-log-level", String(entry.level || "info").toUpperCase()),
      createElement("span", "system-log-component", String(entry.component || "system")),
      createElement("span", "system-log-message", String(entry.message || "")),
    );
    fragment.appendChild(row);
  }
  elements.systemLogList.replaceChildren(fragment);
}

function renderMemoryEntries() {
  const query = String(elements.aiMemorySearch.value || "").trim().toLocaleLowerCase();
  const filtered = state.memoryEntries.filter((entry) => {
    if (!query) return true;
    return [entry.key, entry.value, entry.tags, entry.scope]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query));
  });
  if (!filtered.length) {
    elements.aiMemoryList.replaceChildren(createElement("div", "panel-list-empty", query ? "No matching memory" : "No saved memory"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const entry of filtered) {
    const article = createElement("article", "memory-entry");
    const heading = createElement("div", "memory-entry-heading");
    heading.append(
      createElement("strong", "memory-key", String(entry.key || "Untitled memory")),
      createElement("span", "memory-scope", `${String(entry.scope || "persistent")} · ${String(entry.status || "active")}`),
    );
    const meta = [entry.tags, entry.updated_at ? new Date(entry.updated_at).toLocaleString() : ""].filter(Boolean).join(" · ");
    article.append(heading, createElement("p", "memory-value", String(entry.value || "")));
    if (meta) article.appendChild(createElement("span", "memory-meta", meta));
    fragment.appendChild(article);
  }
  elements.aiMemoryList.replaceChildren(fragment);
}

function renderMemory(payload) {
  if (!payload || payload.ok === false) {
    state.memoryEntries = [];
    elements.aiMemorySummary.textContent = (payload && payload.error) || "Memory unavailable";
    renderMemoryEntries();
    return;
  }
  state.memoryEntries = asArray(payload.entries);
  elements.aiMemorySummary.textContent = `${formatCount(payload.total)} stored · showing ${formatCount(payload.count)}`;
  renderMemoryEntries();
}

function renderSessionEntries() {
  const query = String(elements.sessionsSearch.value || "").trim().toLocaleLowerCase();
  const filtered = state.sessionEntries.filter((entry) => {
    if (!query) return true;
    return [entry.source_id, entry.kind, entry.content]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query));
  });
  if (!filtered.length) {
    elements.sessionsList.replaceChildren(createElement("div", "panel-list-empty", query ? "No matching session evidence" : "No session evidence"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const entry of filtered) {
    const article = createElement("article", "memory-entry context-entry");
    const heading = createElement("div", "memory-entry-heading");
    heading.append(
      createElement("strong", "memory-key", String(entry.source_id || "Session event")),
      createElement("span", "memory-scope", String(entry.kind || "event")),
    );
    article.append(heading, createElement("p", "memory-value", String(entry.content || "")));
    if (entry.timestamp) article.appendChild(createElement("time", "memory-meta", new Date(entry.timestamp).toLocaleString()));
    fragment.appendChild(article);
  }
  elements.sessionsList.replaceChildren(fragment);
}

function renderSessions(payload) {
  if (!payload || payload.ok === false) {
    state.sessionEntries = [];
    elements.sessionsSummary.textContent = (payload && payload.error) || "Session Manager unavailable";
    renderSessionEntries();
    return;
  }
  state.sessionEntries = asArray(payload.entries);
  elements.sessionsSummary.textContent = `${formatCount(payload.total)} stored · showing ${formatCount(payload.count)}`;
  renderSessionEntries();
}

function renderKbEntries() {
  const query = String(elements.kbSearch.value || "").trim().toLocaleLowerCase();
  const filtered = state.kbEntries.filter((entry) => {
    if (!query) return true;
    return [entry.key, entry.title, entry.category, entry.tags, entry.body, entry.source_refs]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query));
  });
  if (!filtered.length) {
    elements.kbList.replaceChildren(createElement("div", "panel-list-empty", query ? "No matching knowledge" : "Knowledge Base is empty"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const entry of filtered) {
    const article = createElement("article", "memory-entry context-entry");
    const heading = createElement("div", "memory-entry-heading");
    heading.append(
      createElement("strong", "memory-key", String(entry.title || entry.key || "Knowledge entry")),
      createElement("span", "memory-scope", `${String(entry.category || "knowledge")} · ${String(entry.status || "active")}`),
    );
    const key = entry.title && entry.key ? createElement("span", "memory-meta", String(entry.key)) : null;
    if (key) article.append(heading, key);
    else article.appendChild(heading);
    article.appendChild(createElement("p", "memory-value", String(entry.body || "")));
    const meta = [entry.tags, entry.source_refs].filter(Boolean).join(" · ");
    if (meta) article.appendChild(createElement("span", "memory-meta", meta));
    fragment.appendChild(article);
  }
  elements.kbList.replaceChildren(fragment);
}

function renderKb(payload) {
  if (!payload || payload.ok === false) {
    state.kbEntries = [];
    elements.kbSummary.textContent = (payload && payload.error) || "Knowledge Base unavailable";
    renderKbEntries();
    return;
  }
  state.kbEntries = asArray(payload.entries);
  elements.kbSummary.textContent = `${formatCount(payload.total)} stored · showing ${formatCount(payload.count)}`;
  renderKbEntries();
}

function taskSignalRow(task, badgeText, badgeClass) {
  const row = createElement("div", "signal-row");
  const top = createElement("div", "signal-topline");
  const button = createElement("button", "task-link signal-title", task.task_id || "Unknown task");
  button.type = "button";
  if (task.task_id) {
    button.dataset.taskId = String(task.task_id);
  }
  top.append(button, createElement("span", `signal-badge ${badgeClass || ""}`, badgeText));
  row.appendChild(top);
  const meta = [task.topic, task.runner || task.claimed_by].filter(Boolean).join(" | ");
  if (meta) {
    row.appendChild(createElement("span", "signal-meta", meta));
  }
  return row;
}

function renderReturns(snapshot) {
  const inbox = snapshot.completion_inbox || {};
  const reviewQueue = asArray(inbox.review_queue);
  elements.returnTab.title = `${reviewQueue.length} worker return${reviewQueue.length === 1 ? "" : "s"} awaiting review`;
  elements.returnTab.setAttribute("aria-label", `Returns, ${elements.returnTab.title}`);
  const topics = Array.from(new Set(reviewQueue.map((item) => String(item.topic || "unknown")))).sort();
  const runners = Array.from(new Set(reviewQueue.map((item) => String(item.runner || item.claimed_by || "unassigned")))).sort();
  replaceSelectOptions(elements.returnTopic, topics, "All topics", state.returnTopic);
  replaceSelectOptions(elements.returnRunner, runners, "All runners", state.returnRunner);
  state.returnTopic = elements.returnTopic.value;
  state.returnRunner = elements.returnRunner.value;
  const needle = state.returnSearch.trim().toLocaleLowerCase();
  const filtered = reviewQueue.filter((item) => {
    const topic = String(item.topic || "unknown");
    const runner = String(item.runner || item.claimed_by || "unassigned");
    if (state.returnTopic !== "all" && topic !== state.returnTopic) return false;
    if (state.returnRunner !== "all" && runner !== state.returnRunner) return false;
    if (!needle) return true;
    return [item.task_id, item.objective, item.validation_status, topic, runner]
      .some((value) => String(value || "").toLocaleLowerCase().includes(needle));
  });
  if (reviewQueue.length === 0) {
    state.returnPage = 0;
    elements.returnPage.textContent = "0 / 0";
    elements.returnPrevious.disabled = true;
    elements.returnNext.disabled = true;
    elements.returnList.replaceChildren(createElement("div", "panel-list-empty", "No worker returns awaiting review"));
    return;
  }
  const pageCount = Math.max(1, Math.ceil(filtered.length / state.returnPageSize));
  state.returnPage = Math.min(state.returnPage, pageCount - 1);
  const start = state.returnPage * state.returnPageSize;
  const page = filtered.slice(start, start + state.returnPageSize);
  elements.returnPage.textContent = `${filtered.length ? state.returnPage + 1 : 0} / ${filtered.length ? pageCount : 0} · ${filtered.length} shown`;
  elements.returnPrevious.disabled = state.returnPage <= 0;
  elements.returnNext.disabled = state.returnPage >= pageCount - 1 || filtered.length === 0;
  if (!page.length) {
    elements.returnList.replaceChildren(createElement("div", "panel-list-empty", "No matching worker returns"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of page) {
    const row = taskSignalRow(item, item.validation_status || "review", validationClass(item.validation_status));
    if (item.objective) {
      row.appendChild(createElement("span", "signal-message", item.objective));
    }
    fragment.appendChild(row);
  }
  elements.returnList.replaceChildren(fragment);
}

function renderRuns(snapshot) {
  const report = snapshot.agent_processes || {};
  const runs = asArray(report.processes);
  elements.runTab.title = `${runs.length} process run${runs.length === 1 ? "" : "s"}`;
  elements.runTab.setAttribute("aria-label", `Runs, ${elements.runTab.title}`);
  if (runs.length === 0) {
    elements.runList.replaceChildren(createElement("div", "panel-list-empty", "No MCP-launched agent runs"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const run of runs.slice(0, 60)) {
    const stateName = run.observed_state || run.state || "unknown";
    const stateClass = ["review_ready", "exited"].includes(stateName)
      ? "pass"
      : ["blocked", "timed_out", "monitor_error", "exited_without_review"].includes(stateName)
        ? "fail"
        : stateName === "running" ? "" : "stale";
    const row = taskSignalRow(run, stateName, stateClass);
    const details = [
      run.adapter_id,
      run.model,
      run.observed_state && run.state ? `event ${run.state}` : "",
      run.pid ? `pid ${run.pid}` : "",
      run.exit_code !== undefined ? `exit ${run.exit_code}` : "",
    ]
      .filter(Boolean)
      .join(" | ");
    if (details) {
      row.appendChild(createElement("span", "signal-message", details));
    }
    if (run.blocked_reason || run.error) {
      row.appendChild(createElement("span", "signal-message", run.blocked_reason || run.error));
    }
    fragment.appendChild(row);
  }
  elements.runList.replaceChildren(fragment);
}

function renderWarnings(snapshot) {
  const warnings = snapshot.warnings || {};
  const sourceErrors = asArray(snapshot.errors);
  const rows = [];

  for (const item of asArray(warnings.stale)) {
    rows.push({ type: "stale", item });
  }
  for (const item of asArray(warnings.collisions)) {
    rows.push({ type: "collision", item });
  }
  for (const item of asArray(warnings.runner_mismatches)) {
    rows.push({ type: "mismatch", item });
  }
  for (const item of sourceErrors) {
    rows.push({ type: "source", item });
  }

  elements.warningTab.title = `${rows.length} active warning${rows.length === 1 ? "" : "s"}`;
  elements.warningTab.setAttribute("aria-label", `Warnings, ${elements.warningTab.title}`);
  if (rows.length === 0) {
    elements.warningList.replaceChildren(createElement("div", "panel-list-empty", "No active warnings"));
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const warning of rows.slice(0, 60)) {
    const item = warning.item || {};
    if (warning.type === "stale") {
      const row = taskSignalRow(item, `${numberValue(item.stale_hours).toFixed(1)}h`, "stale");
      row.classList.add("warning");
      row.appendChild(createElement("span", "signal-message", `Last activity ${formatRelativeTime(item.last_activity_at)}`));
      fragment.appendChild(row);
      continue;
    }
    const row = createElement("div", `signal-row ${warning.type === "source" ? "error" : "warning"}`);
    const top = createElement("div", "signal-topline");
    if (warning.type === "collision") {
      top.append(
        createElement("span", "signal-title", item.file || "Write-path collision"),
        createElement("span", "signal-badge fail", "collision"),
      );
      row.append(top, createElement("span", "signal-message", asArray(item.conflicting_tasks).join(", ") || "Conflicting active tasks"));
    } else if (warning.type === "mismatch") {
      const taskButton = createElement("button", "task-link signal-title", item.task_id || "Runner mismatch");
      taskButton.type = "button";
      if (item.task_id) {
        taskButton.dataset.taskId = String(item.task_id);
      }
      top.append(taskButton, createElement("span", "signal-badge fail", "mismatch"));
      row.append(top, createElement("span", "signal-message", item.warning || "Runner and task batch tokens differ"));
    } else {
      top.append(
        createElement("span", "signal-title", item.source || "Read source"),
        createElement("span", "signal-badge fail", "source"),
      );
      row.append(top, createElement("span", "signal-message", item.message || "Source unavailable"));
    }
    fragment.appendChild(row);
  }
  elements.warningList.replaceChildren(fragment);
}

// The storage gate: an uninitialized/degraded repository always arrives
// with a zero-count, zero-row snapshot (see dashboard.build_snapshot), but
// this still renders the one explicit "Initialize AIWorkHub" action instead
// of a silent empty queue, so the user is never left guessing why nothing
// shows up.
function renderStorageState(snapshot) {
  const storage = snapshot && typeof snapshot.storage === "object" ? snapshot.storage : null;
  const ready = !storage || storage.ready !== false;
  elements.uninitializedAlert.hidden = ready;
  if (!ready) {
    const reason = String((storage && storage.reason) || "uninitialized");
    elements.uninitializedAlertMessage.textContent = `Storage is not ready (${reason}). Initialize to create the canonical task store for this repository.`;
    setConnection("degraded", "Uninitialized");
  }
  return ready;
}

function renderSnapshot(snapshot) {
  elements.offlineAlert.hidden = true;
  state.snapshot = snapshot;
  state.tasks = flattenTasks(snapshot);
  const storageReady = renderStorageState(snapshot);
  renderSummary(snapshot);
  renderManagerIdentity(snapshot);
  renderCallbackObservability(snapshot);
  renderKnownRepositories(snapshot);
  if (storageReady) {
    renderSourceHealth(snapshot);
  }
  renderFilterOptions();
  renderTaskTable();
  renderStats(elements.topicStats, snapshot.summaries && snapshot.summaries.topics);
  renderStats(elements.runnerStats, snapshot.summaries && snapshot.summaries.runners);
  renderUsage(snapshot);
  renderPlanDag(snapshot);
  renderWorkforce(snapshot);
  renderToolUse(snapshot);
  renderStorage(snapshot);
  renderSystemLogs(snapshot);
  renderReturns(snapshot);
  renderRuns(snapshot);
  renderWarnings(snapshot);
  if (state.selectedTaskId && !state.tasks.some((task) => String(task.task_id) === state.selectedTaskId)) {
    clearTaskDetail();
  }
}

let toastTimer = null;
function showToast(message) {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 5000);
}

function showOffline(reason) {
  elements.offlineAlert.hidden = false;
  elements.offlineAlertMessage.textContent = reason || "The Task MCP connection is unavailable.";
  setConnection("offline", "Offline");
  if (!state.snapshot) {
    elements.tableLoading.hidden = true;
    elements.tableEmpty.hidden = false;
    elements.tableEmpty.textContent = "Queue unavailable";
  }
}

function renderRuntimeInfo(info) {
  const payload = info && typeof info === "object" ? info : {};
  const extensionVersion = String(payload.extensionVersion || "unknown");
  const runtimeVersion = String(payload.runtimeVersion || "unavailable");
  const expectedMcpVersion = String(payload.expectedMcpVersion || "unknown");
  elements.extensionVersion.textContent = `Extension ${extensionVersion}`;
  elements.mcpRuntimeVersion.textContent = `MCP runtime ${runtimeVersion}`;
  if (payload.degraded || payload.reloadRequired) {
    elements.reloadAlert.hidden = false;
    const attempts = Number(payload.attempts || 0);
    const maxAttempts = Number(payload.maxAttempts || 3);
    elements.reloadAlertMessage.textContent =
      `MCP runtime degraded: ${String(payload.reason || "unavailable")} (${attempts}/${maxAttempts}).`;
  } else {
    elements.reloadAlert.hidden = true;
    elements.reloadAlertMessage.textContent = "";
  }
}

function renderKnownRepositories(snapshot) {
  if (!elements.repoRouter || !elements.repoRouterList) {
    return;
  }
  const payload = snapshot && snapshot.known_repositories && typeof snapshot.known_repositories === "object"
    ? snapshot.known_repositories
    : null;
  const repos = payload ? asArray(payload.repositories).slice(0, 8) : [];
  elements.repoRouter.hidden = repos.length === 0;
  if (!repos.length) {
    elements.repoRouterList.replaceChildren();
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const repo of repos) {
    const classes = ["repo-route"];
    if (repo.current_repo) classes.push("current");
    if (repo.stale) classes.push("stale");
    const item = createElement("span", classes.join(" "));
    const name = String(repo.repo_name || "repo");
    const selected = String(repo.selected_provider || "unknown");
    const alive = repo.extension_host_alive ? "live" : "not-live";
    item.textContent = `${repo.current_repo ? "● " : ""}${name} · ${selected} · ${alive}${repo.stale ? " · stale" : ""}`;
    item.title = JSON.stringify({
      repo_id: repo.repo_id,
      window_id: repo.window_id,
      selected_provider: selected,
      extension_host_alive: repo.extension_host_alive,
      stale: repo.stale,
      updated_at: repo.updated_at,
    });
    fragment.appendChild(item);
  }
  elements.repoRouterList.replaceChildren(fragment);
}

function renderCoordinatorTargets(info) {
  const payload = info && typeof info === "object" ? info : {};
  const selected = String(payload.selected_provider || "codex");
  const target = payload.targets && payload.targets[selected] ? payload.targets[selected] : {};
  const route = target.route && typeof target.route === "object" ? target.route : {};
  const wake = target.wake && typeof target.wake === "object" ? target.wake : {};
  if (elements.targetState) {
    const stateText = String(target.capability_state || "automatic");
    const reason = String(wake.reason || wake.action || "");
    elements.targetState.textContent = `automatic: ${selected} · ${stateText}${reason ? ` · ${reason}` : ""}`;
    elements.targetState.title = JSON.stringify({
      repo_id: payload.repo_id,
      window_id: payload.window_id,
      claim_episode: payload.claim_episode,
      selected_provider: selected,
      capability_state: target.capability_state,
      thread_id: route.thread_id,
      session_id: route.session_id,
      wake,
      routing: "per-task coordinator_provider + thread/session identity",
    });
  }
}

function requestRefresh() {
  elements.refreshButton.disabled = true;
  elements.refreshButton.textContent = "Refreshing";
  if (!state.snapshot) {
    setConnection("offline", "Connecting");
    elements.tableLoading.hidden = false;
  }
  vscode.postMessage({ type: "refresh" });
  window.setTimeout(() => {
    elements.refreshButton.disabled = false;
    elements.refreshButton.textContent = "Refresh";
  }, 600);
}

function detailMetadata(card) {
  const fields = [
    ["Topic", card.topic || "unknown"],
    ["Runner", card.runner || "unassigned"],
    ["Claimed by", card.claimed_by || "unclaimed"],
    ["Priority", card.priority || "normal"],
    ["Mode", card.mode || "unspecified"],
    ["Updated", card.updated_at || card.review_at || "unknown"],
  ];
  const fragment = document.createDocumentFragment();
  for (const [label, value] of fields) {
    const group = document.createElement("div");
    group.append(createElement("dt", "", label), createElement("dd", "", value));
    group.querySelector("dd").title = String(value);
    fragment.appendChild(group);
  }
  elements.detailMetadata.replaceChildren(fragment);
}

function resultPayload(card) {
  const keys = [
    "result",
    "worker_result",
    "completion_summary",
    "review_summary",
    "review_notes",
    "validation_error",
    "blocker_reason",
    "validation_output",
    "artifacts",
    "artifact_paths",
  ];
  const result = {};
  for (const key of keys) {
    if (card[key] !== undefined && card[key] !== null && card[key] !== "") {
      result[key] = card[key];
    }
  }
  if (Object.keys(result).length === 0) {
    return "No result recorded";
  }
  if (Object.keys(result).length === 1) {
    const value = result[Object.keys(result)[0]];
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }
  return JSON.stringify(result, null, 2);
}

function renderAiInfra(card) {
  const info = card.ai_infra_context && typeof card.ai_infra_context === "object" ? card.ai_infra_context : null;
  const quality = card.quality_gate && typeof card.quality_gate === "object" ? card.quality_gate : null;
  if (!info && !quality) {
    elements.detailAiInfraBlock.hidden = true;
    elements.detailAiInfra.replaceChildren();
    return;
  }
  const fragment = document.createDocumentFragment();
  const overview = info ? [
    ["Injected", info.injected ? "yes" : "no"],
    ["Acknowledged", info.acknowledged ? "yes" : "no"],
  ] : [];
  if (quality) {
    overview.push(["Quality gate", quality.passed ? "passed" : "failed"]);
    overview.push(["Quality checks", String(asArray(quality.checks).length)]);
    const verdict = quality.quality_verdict && typeof quality.quality_verdict === "object"
      ? quality.quality_verdict
      : null;
    if (verdict) {
      overview.push(["Verdict", verdict.status || (verdict.passed ? "verified" : "unverified")]);
      overview.push(["Risk", verdict.risk_tier || "unknown"]);
      overview.push(["Refinement", verdict.refine_required ? "required" : "clear"]);
      const lenses = asArray(verdict.lenses);
      overview.push([
        "Quality lenses",
        `${lenses.filter((item) => item && item.status === "passed").length}/${lenses.length} passed`,
      ]);
    }
  }
  const toolUse = info && info.tool_use && typeof info.tool_use === "object" ? info.tool_use : null;
  if (toolUse && toolUse.gated) {
    overview.push(["SG live", `${numberValue(toolUse.source_graph_live_calls)} calls`]);
    overview.push(["SG total", `${numberValue(toolUse.source_graph_calls)} calls`]);
    overview.push(["SG bytes", formatBytes(toolUse.source_graph_bytes)]);
    overview.push(["Violations", String(numberValue(toolUse.policy_violations))]);
  }
  if (info && info.estimate && info.estimate.label) {
    overview.push(["Raw est.", `${numberValue(info.estimate.raw_context_bytes)} B`]);
    overview.push(["Bundle", `${numberValue(info.estimate.bundle_bytes)} B`]);
  }
  for (const [label, value] of overview) {
    const item = createElement("div", "ai-infra-item");
    item.append(createElement("span", "ai-infra-label", label), createElement("strong", "", value));
    fragment.appendChild(item);
  }
  for (const [label, key] of [
    ["Source Graph", "source_graph"],
    ["Session", "session_current_state"],
    ["AI Memory", "ai_memory"],
    ["KB", "kb"],
  ]) {
    const section = info && info[key] && typeof info[key] === "object" ? info[key] : {};
    if (!Object.keys(section).length) {
      continue;
    }
    const item = createElement("div", "ai-infra-item wide");
    const status = section.degraded_reason ? `degraded: ${section.degraded_reason}` : `${numberValue(section.hit_count)} hits`;
    item.title = `${label}: ${numberValue(section.bytes)} bytes, sha ${section.sha256 || "none"}`;
    item.append(
      createElement("span", "ai-infra-label", label),
      createElement("strong", "", status),
      createElement("span", "cell-secondary", `${numberValue(section.bytes)} B${section.truncated ? " | truncated" : ""}`),
    );
    fragment.appendChild(item);
  }
  elements.detailAiInfra.replaceChildren(fragment);
  elements.detailAiInfraBlock.hidden = false;
}

function appendEvidenceList(fragment, title, values) {
  const items = asArray(values).filter((value) => String(value || "").trim());
  if (!items.length) {
    return;
  }
  const section = createElement("section", "review-evidence-section");
  section.appendChild(createElement("h4", "", title));
  const list = createElement("ul", "path-list");
  list.replaceChildren(...items.map((value) => createElement("li", "", String(value))));
  section.appendChild(list);
  fragment.appendChild(section);
}

function renderReviewEvidence(evidence) {
  if (!evidence || typeof evidence !== "object") {
    elements.detailEvidence.replaceChildren();
    elements.detailEvidenceBlock.hidden = true;
    elements.detailEvidenceBlock.open = false;
    return;
  }
  const terminal = evidence.terminal && typeof evidence.terminal === "object" ? evidence.terminal : {};
  const diff = evidence.diff && typeof evidence.diff === "object" ? evidence.diff : {};
  const tests = asArray(evidence.tests);
  const artifacts = asArray(evidence.artifacts);
  const approvals = asArray(evidence.approvals);
  const changedPaths = asArray(diff.changed_paths);
  const overview = createElement("div", "ai-infra-grid review-evidence-overview");
  for (const [label, value] of [
    ["Status", terminal.status || "unknown"],
    ["Outcome", terminal.substatus || terminal.worker_status || "unreported"],
    ["Process", terminal.process_state || "unreported"],
    ["Changed", String(changedPaths.length)],
    ["Tests", `${tests.filter((item) => item && item.ok).length}/${tests.length} passed`],
    ["Artifacts", String(artifacts.length)],
    ["Approvals", String(approvals.length)],
  ]) {
    const item = createElement("div", "ai-infra-item");
    item.append(createElement("span", "ai-infra-label", label), createElement("strong", "", value));
    overview.appendChild(item);
  }

  const fragment = document.createDocumentFragment();
  fragment.appendChild(overview);
  if (terminal.error) {
    const error = createElement("section", "review-evidence-section evidence-error");
    error.append(createElement("h4", "", "Terminal error"), createElement("p", "", terminal.error));
    fragment.appendChild(error);
  }
  const hashes = diff.changed_path_hashes && typeof diff.changed_path_hashes === "object"
    ? diff.changed_path_hashes
    : {};
  appendEvidenceList(fragment, "Changed paths", changedPaths.map((path) => {
    const hash = hashes[path] ? ` · ${String(hashes[path]).slice(0, 12)}` : "";
    return `${path}${hash}`;
  }));
  if (tests.length) {
    const section = createElement("section", "review-evidence-section");
    section.appendChild(createElement("h4", "", "Validation"));
    const list = createElement("ul", "review-evidence-list");
    for (const test of tests) {
      if (!test || typeof test !== "object") continue;
      const row = createElement("li", test.ok ? "evidence-pass" : "evidence-fail");
      row.append(
        createElement("strong", "", test.ok ? "Passed" : "Failed"),
        createElement("span", "", test.command || "unnamed check"),
      );
      if (test.summary) row.appendChild(createElement("small", "", test.summary));
      list.appendChild(row);
    }
    section.appendChild(list);
    fragment.appendChild(section);
  }
  appendEvidenceList(fragment, "Artifacts", artifacts);
  if (approvals.length) {
    const section = createElement("section", "review-evidence-section");
    section.appendChild(createElement("h4", "", "Approval history"));
    const list = createElement("ul", "review-evidence-list");
    for (const approval of approvals) {
      if (!approval || typeof approval !== "object") continue;
      const transition = [approval.from_state, approval.to_state].filter(Boolean).join(" → ");
      const row = createElement("li", "");
      row.append(
        createElement("strong", "", approval.event || "event"),
        createElement("span", "", transition || approval.actor || "recorded"),
      );
      if (approval.reason) row.appendChild(createElement("small", "", approval.reason));
      list.appendChild(row);
    }
    section.appendChild(list);
    fragment.appendChild(section);
  }
  const summary = evidence.logs && evidence.logs.result_summary;
  if (summary) {
    const section = createElement("section", "review-evidence-section");
    section.append(createElement("h4", "", "Result summary"), createElement("p", "", summary));
    fragment.appendChild(section);
  }
  elements.detailEvidence.replaceChildren(fragment);
  elements.detailEvidenceBlock.hidden = false;
}

function renderTaskDetail(card) {
  const status = canonicalStatus(card);
  elements.detailHeading.textContent = card.task_id || state.selectedTaskId;
  elements.detailStatus.textContent = status;
  elements.detailStatus.className = `status-badge ${status}`;
  elements.detailStatus.hidden = false;
  elements.detailObjective.textContent = card.objective || "No objective recorded";
  detailMetadata(card);

  const validation = String(card.validation_status || "unreported");
  elements.detailValidation.textContent = validation;
  elements.detailValidation.className = `validation-label ${validationClass(validation)}`;
  elements.detailResult.textContent = resultPayload(card);
  renderAiInfra(card);
  renderReviewEvidence(card.review_evidence_bundle);

  const allowedWrites = asArray(card.allowed_writes);
  elements.detailWritesBlock.hidden = allowedWrites.length === 0;
  elements.detailWrites.replaceChildren(...allowedWrites.map((path) => createElement("li", "", path)));

  elements.detailLoading.hidden = true;
  elements.detailError.hidden = true;
  elements.detailEmpty.hidden = true;
  elements.detailContent.hidden = false;
}

// ── Selected-task Live Output: bounded, single-task poll loop ─────────────
// requestLiveOutput/liveOutput is a fixed-enum pair the extension host
// forwards, unchanged, to exactly one read-only MCP tool call for the
// currently selected task (see extension.js pushLiveOutput). This panel
// never fans out across other tasks and never starts polling except while a
// task is selected -- selecting a different task or clearing the selection
// always stops the previous poll first (stopLiveOutputPolling).
function stopLiveOutputPolling() {
  if (state.liveOutputTimer !== null) {
    window.clearTimeout(state.liveOutputTimer);
    state.liveOutputTimer = null;
  }
}

function resetLiveOutputPanel() {
  stopLiveOutputPolling();
  state.liveOutputTaskId = null;
  state.liveOutputCursor = 0;
  state.liveOutputText = "";
  elements.detailLiveOutputBlock.hidden = true;
  elements.detailLiveOutputState.textContent = "";
  elements.detailLiveOutputState.className = "validation-label";
  elements.detailLiveOutputContainer.replaceChildren();
  elements.detailLiveOutputStderr.hidden = true;
  elements.detailLiveOutputStderr.textContent = "";
  elements.detailLiveOutputRaw.hidden = true;
  elements.detailLiveOutputRaw.open = false;
  elements.detailLiveOutputRawContent.textContent = "";
}

function startLiveOutputPolling(taskId) {
  stopLiveOutputPolling();
  state.liveOutputTaskId = taskId;
  state.liveOutputCursor = 0;
  state.liveOutputText = "";
  elements.detailLiveOutputBlock.hidden = false;
  elements.detailLiveOutputState.textContent = "Loading";
  elements.detailLiveOutputState.className = "validation-label";
  elements.detailLiveOutputContainer.replaceChildren();
  elements.detailLiveOutputStderr.hidden = true;
  elements.detailLiveOutputStderr.textContent = "";
  elements.detailLiveOutputRaw.hidden = true;
  elements.detailLiveOutputRaw.open = false;
  elements.detailLiveOutputRawContent.textContent = "";
  vscode.postMessage({ type: "requestLiveOutput", taskId, cursor: 0 });
}

function scheduleNextLiveOutputPoll(taskId) {
  if (state.liveOutputTaskId !== taskId) {
    return;
  }
  state.liveOutputTimer = window.setTimeout(() => {
    state.liveOutputTimer = null;
    if (state.liveOutputTaskId === taskId) {
      vscode.postMessage({ type: "requestLiveOutput", taskId, cursor: state.liveOutputCursor });
    }
  }, LIVE_OUTPUT_POLL_MS);
}

// The stdout tail arrives pre-escaped/redacted from
// process_launcher._sanitize_live_output_text -- appended here as HTML
// (never as a template built from unsanitized input) so entities render as
// the literal characters they represent. The client-side buffer is trimmed
// from the front at a newline boundary once it exceeds
// LIVE_OUTPUT_MAX_CLIENT_CHARS, matching the server's own bound.
function decodeLiveOutput(text) {
  const textarea = document.createElement("textarea");
  textarea.innerHTML = String(text || "");
  return textarea.value;
}

function resultEnvelope(text) {
  const lines = String(text || "").split(/\r?\n/).filter((line) => line.trim());
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const parsed = JSON.parse(lines[index]);
      if (parsed && typeof parsed === "object" &&
          (parsed.type === "result" || parsed.result !== undefined || parsed.terminal_reason)) {
        return parsed;
      }
    } catch (_error) {
      // Streaming providers may emit non-JSON progress lines before the
      // terminal result. Keep walking backwards to the last result envelope.
    }
  }
  return null;
}

function limitText(value, maxLength = 120) {
  const text = String(value === undefined || value === null ? "" : value)
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}...`;
}

function redactDisplayText(value, maxLength = 120) {
  return limitText(value, maxLength)
    .replace(/\b(?:[A-Za-z]:)?\/(?:[\w .:@-]+\/){1,}[\w .:@-]+/g, "[path]")
    .replace(/\b(?:sk|pk|ghp|github_pat|xox[baprs]|ya29|hf)_[A-Za-z0-9._-]{12,}\b/g, "[token]")
    .replace(/\b[A-Za-z0-9+/]{32,}={0,2}\b/g, "[token]");
}

function firstText(...values) {
  for (const value of values) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      const text = String(value).trim();
      if (text) {
        return text;
      }
    }
  }
  return "";
}

function eventDuration(event) {
  return firstText(
    event.duration_ms,
    event.elapsed_ms,
    event.durationMs,
    event.duration,
    event.result && event.result.duration_ms,
    event.metrics && event.metrics.duration_ms,
  );
}

function eventUsage(event) {
  const modelUsageLabel = "Model usage";
  const usage = event.usage && typeof event.usage === "object" ? event.usage : {};
  const metrics = event.metrics && typeof event.metrics === "object" ? event.metrics : {};
  const modelUsage = event.modelUsage && typeof event.modelUsage === "object" ? event.modelUsage : {};
  const turns = firstText(event.num_turns, event.turns, metrics.turns);
  const cost = firstText(event.total_cost_usd, event.cost_usd, metrics.cost_usd);
  const totalTokens = firstText(
    usage.total_tokens,
    usage.totalTokens,
    metrics.total_tokens,
    event.total_tokens,
    numberValue(usage.input_tokens) + numberValue(usage.output_tokens),
  );
  const modelUsageSummary = Object.entries(modelUsage).slice(0, 4).map(([name, values]) => {
    const item = values && typeof values === "object" ? values : {};
    return `${redactDisplayText(name, 40)} ${formatCount(item.inputTokens)} in/${formatCount(item.outputTokens)} out/${formatMoney(item.costUSD)}`;
  }).join(", ");
  return [
    eventDuration(event) !== "" ? `duration ${formatDuration(eventDuration(event))}` : "",
    turns !== "" ? `${turns} turns` : "",
    numberValue(totalTokens) ? `${formatCount(totalTokens)} tokens` : "",
    cost !== "" ? formatMoney(cost) : "",
    modelUsageSummary ? `${modelUsageLabel}: ${modelUsageSummary}` : "",
  ].filter(Boolean);
}

function commandLabelFrom(value) {
  const source = value && typeof value === "object" ? value : {};
  const input = source.input && typeof source.input === "object" ? source.input : {};
  const result = source.result && typeof source.result === "object" ? source.result : {};
  const item = source.item && typeof source.item === "object" ? source.item : {};
  const itemInput = item.input && typeof item.input === "object" ? item.input : {};
  return redactDisplayText(firstText(
    source.command,
    source.cmd,
    source.tool_name,
    source.tool,
    source.name,
    source.subtype,
    input.command,
    input.cmd,
    result.command,
    item.command,
    item.name,
    item.tool_name,
    itemInput.command,
  ) || "event", 80);
}

function lifecycleFromType(type, event) {
  const normalized = String(type || "").toLowerCase();
  if (normalized.includes("started") || normalized.includes("start") || normalized.includes("delta")) return "running";
  if (normalized.includes("completed") || normalized.includes("finish") || normalized.includes("result")) return "completed";
  if (normalized.includes("error") || normalized.includes("failed") || event.is_error) return "error";
  if (normalized.includes("warning")) return "warning";
  return firstText(event.status, event.state, event.lifecycle) || "event";
}

function resultTextFrom(event) {
  const result = event.result && typeof event.result === "object" ? event.result : {};
  const message = event.message && typeof event.message === "object" ? event.message : {};
  const item = event.item && typeof event.item === "object" ? event.item : {};
  return redactDisplayText(firstText(
    event.error,
    event.warning,
    event.verdict,
    event.output,
    event.summary,
    event.stop_reason,
    event.terminal_reason,
    event.result,
    result.error,
    result.verdict,
    result.output,
    result.summary,
    message.error,
    message.content,
    item.error,
    item.output,
    item.message,
  ), 180);
}

function parseNestedJson(value, depth = 0) {
  if (depth > 3) {
    return value;
  }
  if (typeof value !== "string") {
    return value;
  }
  const text = value.trim();
  if (!text || !["{", "[", "\""].includes(text[0])) {
    return value;
  }
  try {
    const parsed = JSON.parse(text);
    return parsed === value ? value : parseNestedJson(parsed, depth + 1);
  } catch (_error) {
    return value;
  }
}

function safeRawEvent(value) {
  let text;
  try {
    text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  } catch (_error) {
    text = String(value || "");
  }
  return redactDisplayText(text, 2000);
}

function jsonSummary(value) {
  const parsed = parseNestedJson(value);
  if (Array.isArray(parsed)) {
    return `${parsed.length} structured item${parsed.length === 1 ? "" : "s"}`;
  }
  if (parsed && typeof parsed === "object") {
    const keys = Object.keys(parsed).slice(0, 6);
    return keys.length ? `Structured event: ${keys.join(", ")}` : "Structured event";
  }
  return redactDisplayText(parsed, 180);
}

// Bounds for the tool_result envelope extraction below: at most one
// JSON-string decode of message.content, an explicit recursion depth for
// unwrapping content-block shapes, an item cap for multi-result messages, and
// a UTF-8 byte cap (with deterministic "...[truncated]" fallback) so a large
// excerpt can never grow the rendered row past what one poll response could
// ever contain.
const TOOL_RESULT_MAX_DEPTH = 2;
const TOOL_RESULT_MAX_ITEMS = 20;
const TOOL_RESULT_DECODE_MAX_BYTES = 4000;
const TOOL_RESULT_TEXT_MAX_BYTES = 8000;

function boundedUtf8Slice(text, maxBytes) {
  const source = String(text || "");
  if (typeof TextEncoder === "undefined" || typeof TextDecoder === "undefined") {
    return source.length > maxBytes ? source.slice(0, maxBytes) : source;
  }
  const encoded = new TextEncoder().encode(source);
  if (encoded.length <= maxBytes) {
    return source;
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(encoded.slice(0, maxBytes));
}

function boundedToolResultText(text) {
  const sliced = boundedUtf8Slice(text, TOOL_RESULT_TEXT_MAX_BYTES);
  return sliced.length < text.length ? `${sliced}\n...[truncated]` : sliced;
}

// Redaction that preserves line breaks (unlike redactDisplayText/limitText,
// which collapse whitespace) so a numbered source excerpt stays readable.
function redactPreserveLines(value) {
  const text = String(value === undefined || value === null ? "" : value).replace(
    /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,
    " ",
  );
  return text
    .replace(/\b(?:[A-Za-z]:)?\/(?:[\w .:@-]+\/){1,}[\w .:@-]+/g, "[path]")
    .replace(/\b(?:sk|pk|ghp|github_pat|xox[baprs]|ya29|hf)_[A-Za-z0-9._-]{12,}\b/g, "[token]")
    .replace(/\b[A-Za-z0-9+/]{32,}={0,2}\b/g, "[token]");
}

// Unwraps a tool_result content payload. First-class: a string directly on
// the item (the exact reported envelope). Also recognized: an array of
// content blocks ({type:"text", text:...} or nested {content:...}), and the
// legacy nested {content: {content: "..."}} shape -- one level only, not a
// generic deep unwrap.
function toolResultBlockText(content, depth = 0) {
  if (depth > TOOL_RESULT_MAX_DEPTH) {
    return "";
  }
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content)) {
    return content
      .slice(0, TOOL_RESULT_MAX_ITEMS)
      .map((item) => {
        if (typeof item === "string") {
          return item;
        }
        if (item && typeof item === "object" && typeof item.text === "string") {
          return item.text;
        }
        return toolResultBlockText(item && item.content, depth + 1);
      })
      .filter(Boolean)
      .join("\n");
  }
  if (content && typeof content === "object" && typeof content.content === "string") {
    return content.content;
  }
  return "";
}

// Claude CLI --output-format stream-json wraps a tool_result turn as
// {type:"user", message:{role:"user", content:[{type:"tool_result", ...}]}}.
// This renders the numbered source excerpt as the row's readable text
// instead of falling through to a generic "Structured event: type, message"
// summary, and never surfaces the outer envelope keys (type/message/role/
// tool_use_id) in that display -- they remain visible only inside the
// existing collapsed "Raw event" details.
function toolResultEventFromUserMessage(event, rawLine) {
  if (!event || typeof event !== "object" || event.type !== "user") {
    return null;
  }
  const message = event.message && typeof event.message === "object" ? event.message : null;
  if (!message || message.role !== "user") {
    return null;
  }
  let content = message.content;
  if (typeof content === "string") {
    const candidate = boundedUtf8Slice(content.trim(), TOOL_RESULT_DECODE_MAX_BYTES);
    if (!candidate || (candidate[0] !== "[" && candidate[0] !== "{")) {
      return null;
    }
    try {
      content = JSON.parse(candidate);
    } catch (_error) {
      return null;
    }
  }
  if (!Array.isArray(content)) {
    return null;
  }
  const texts = [];
  for (const block of content.slice(0, TOOL_RESULT_MAX_ITEMS)) {
    if (!block || typeof block !== "object" || block.type !== "tool_result") {
      continue;
    }
    const text = toolResultBlockText(block.content);
    if (text) {
      texts.push(text);
    }
  }
  if (!texts.length) {
    return null;
  }
  return {
    kind: "event",
    title: texts.length > 1 ? `Tool result (${texts.length})` : "Tool result",
    label: "tool result",
    state: "completed",
    message: redactPreserveLines(boundedToolResultText(texts.join("\n\n"))),
    metrics: [],
    raw: safeRawEvent(rawLine),
  };
}

// Claude CLI --output-format stream-json wraps each provider-level SDK event
// as {type:"stream_event", event:{type:"content_block_delta", delta:{...}}}.
// delta.type "signature_delta" carries an opaque cryptographic signature over
// prior "thinking" content -- internal protocol bookkeeping a human never
// reads -- so callers must treat it as supported, ignorable metadata, never
// as an unrecognized shape. delta.type "text_delta" carries user-visible
// assistant text and must keep rendering normally.
function claudeStreamContentBlockDelta(event) {
  if (!event || typeof event !== "object" || event.type !== "stream_event") {
    return null;
  }
  const inner = event.event && typeof event.event === "object" ? event.event : null;
  if (!inner || inner.type !== "content_block_delta") {
    return null;
  }
  return inner.delta && typeof inner.delta === "object" ? inner.delta : null;
}

function timelineEventFromObject(event, rawLine) {
  const streamDelta = claudeStreamContentBlockDelta(event);
  if (streamDelta && streamDelta.type === "signature_delta") {
    // Internal protocol metadata -- silently dropped from the human-readable
    // feed. Returning null (not a placeholder row) keeps the signature
    // payload out of every rendered surface, including the per-row "Raw
    // event" details; the full unredacted provider stream is still available
    // behind the existing top-level "Raw provider output" details affordance
    // (see appendLiveOutputText/detailLiveOutputRawContent).
    return null;
  }
  if (streamDelta && streamDelta.type === "text_delta") {
    return {
      kind: "event",
      title: "Text",
      label: "text delta",
      state: "running",
      message: redactDisplayText(firstText(streamDelta.text), 180) || "(empty text delta)",
      metrics: [],
      raw: safeRawEvent(rawLine),
    };
  }
  const toolResultEvent = toolResultEventFromUserMessage(event, rawLine);
  if (toolResultEvent) {
    return toolResultEvent;
  }
  const nestedResult = parseNestedJson(event.result);
  if (nestedResult && typeof nestedResult === "object" && !Array.isArray(nestedResult)) {
    event = {
      ...nestedResult,
      ...event,
      result: nestedResult.result ?? nestedResult.output ?? nestedResult.summary ?? nestedResult.verdict ?? "",
    };
  }
  const type = firstText(event.type, event.event, event.kind, event.subtype) || "json";
  const normalized = type.toLowerCase();
  const isResult = normalized === "result" || event.result !== undefined || event.terminal_reason;
  const isWarning = normalized.includes("warning") || event.warning;
  const isError = Boolean(event.is_error || event.error) || normalized.includes("error") || normalized.includes("failed");
  const item = event.item && typeof event.item === "object" ? event.item : {};
  const itemType = firstText(item.type, item.kind);
  let title = isResult ? "Result" : limitText(type.replace(/[._-]+/g, " "), 48);
  if (itemType) {
    title = limitText(`${title} | ${itemType.replace(/[._-]+/g, " ")}`, 64);
  }
  return {
    kind: isError ? "error" : isWarning ? "warning" : isResult ? "result" : "event",
    title,
    label: commandLabelFrom(event),
    state: lifecycleFromType(type, event),
    message: resultTextFrom(event) || jsonSummary(nestedResult || event),
    metrics: eventUsage(event),
    raw: safeRawEvent(rawLine),
  };
}

function timelineEventsFromText(decoded) {
  const events = [];
  const seenJsonLines = new Set();
  for (const line of String(decoded || "").split(/\r?\n/)) {
    if (!line.trim()) {
      continue;
    }
    try {
      const parsed = parseNestedJson(line);
      if (parsed && typeof parsed === "object") {
        const parsedEvents = Array.isArray(parsed) ? parsed : [parsed];
        for (const parsedEvent of parsedEvents) {
          if (!parsedEvent || typeof parsedEvent !== "object") {
            continue;
          }
          const key = JSON.stringify(parsedEvent);
          if (seenJsonLines.has(key)) {
            continue;
          }
          seenJsonLines.add(key);
          const timelineEvent = timelineEventFromObject(parsedEvent, parsedEvent);
          if (timelineEvent) {
            events.push(timelineEvent);
          }
        }
        continue;
      }
    } catch (_error) {
      // Malformed provider output degrades to a text-only row.
    }
    const looksStructured = /^[\s]*[\[{]/.test(line) || /\\"(?:type|event|result)\\"\s*:/.test(line);
    events.push({
      kind: "text",
      title: looksStructured ? "Unrecognized event" : "Output",
      label: looksStructured ? "structured output" : "plain text",
      state: "line",
      message: looksStructured ? "Provider emitted an unsupported event shape; open Raw event for details." : redactDisplayText(line, 220),
      metrics: [],
      raw: safeRawEvent(line),
    });
  }
  return events;
}

function formatDuration(milliseconds) {
  const value = numberValue(milliseconds);
  if (!value) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

function addLiveOutputSection(parent, title, content, className = "") {
  if (content === undefined || content === null || content === "") return;
  const section = createElement("section", `live-output-section ${className}`.trim());
  section.append(createElement("h4", "", title));
  const body = createElement("div", "live-output-section-body");
  body.textContent = String(content);
  section.appendChild(body);
  parent.appendChild(section);
}

function renderFormattedLiveOutput(decoded) {
  const fragment = document.createDocumentFragment();
  const events = timelineEventsFromText(decoded);
  if (!events.length) {
    fragment.appendChild(createElement("div", "panel-list-empty compact", "Waiting for model output..."));
    elements.detailLiveOutputContainer.replaceChildren(fragment);
    return;
  }

  const timeline = createElement("div", "live-output-timeline");
  // Newest-first: current activity must be visible without scrolling.
  for (const event of events.slice(-200).reverse()) {
    const row = createElement("article", `live-output-row is-${event.kind}`);
    const head = createElement("div", "live-output-row-head");
    head.append(
      createElement("strong", "live-output-row-title", event.title),
      createElement("span", "live-output-row-state", event.state),
    );
    row.append(
      head,
      createElement("div", "live-output-row-label", event.label),
      createElement("div", "live-output-row-message", event.message || "No message"),
    );
    if (event.metrics.length) {
      row.appendChild(createElement("div", "live-output-row-meta", event.metrics.join(" | ")));
    }
    const raw = createElement("details", "live-output-row-raw");
    raw.append(createElement("summary", "", "Raw event"), createElement("pre", "", event.raw));
    row.appendChild(raw);
    timeline.appendChild(row);
  }
  fragment.appendChild(timeline);
  elements.detailLiveOutputContainer.replaceChildren(fragment);
}

function appendLiveOutputText(chunk) {
  if (!chunk) {
    return;
  }
  state.liveOutputText += chunk;
  if (state.liveOutputText.length > LIVE_OUTPUT_MAX_CLIENT_CHARS) {
    const excess = state.liveOutputText.length - LIVE_OUTPUT_MAX_CLIENT_CHARS;
    const newlineIndex = state.liveOutputText.indexOf("\n", excess);
    state.liveOutputText = state.liveOutputText.slice(newlineIndex !== -1 ? newlineIndex + 1 : excess);
  }
  const decoded = decodeLiveOutput(state.liveOutputText);
  renderFormattedLiveOutput(decoded);
  elements.detailLiveOutputRaw.hidden = decoded.length === 0;
  elements.detailLiveOutputRawContent.textContent = decoded;
}

function renderLiveOutput(payload) {
  const info = payload && typeof payload === "object" ? payload : {};
  const taskId = String(info.task_id || "");
  // A response for a task no longer selected/polled is stale -- drop it and
  // never let it restart or extend polling for the wrong task.
  if (!state.liveOutputTaskId || taskId !== state.liveOutputTaskId) {
    return;
  }
  if (info.ok === false) {
    const reason = info.reason ? `: ${String(info.reason)}` : "";
    elements.detailLiveOutputState.textContent = `${info.error || "output_unavailable"}${reason}`;
    elements.detailLiveOutputState.className = "validation-label fail";
    return;
  }

  appendLiveOutputText(String(info.output || ""));
  if (Number.isFinite(info.next_cursor)) {
    state.liveOutputCursor = info.next_cursor;
  }

  const stderrTail = String(info.stderr_tail || "");
  elements.detailLiveOutputStderr.hidden = stderrTail.length === 0;
  if (stderrTail) {
    elements.detailLiveOutputStderr.textContent = decodeLiveOutput(stderrTail);
  }

  const liveness = String(info.liveness_state || info.state || "unknown");
  const activity = info.last_activity_at ? formatRelativeTime(info.last_activity_at) : "unknown";
  const exitSuffix = Number.isFinite(info.exit_code) ? ` | exit ${info.exit_code}` : "";
  elements.detailLiveOutputState.textContent =
    `${liveness} | last activity ${activity}${info.truncated ? " | truncated" : ""}${exitSuffix}`;
  elements.detailLiveOutputState.className = `validation-label ${["exited", "completed"].includes(liveness) ? "pass" : ""}`;

  scheduleNextLiveOutputPoll(taskId);
}

if (typeof globalThis !== "undefined") {
  globalThis.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__ = {
    decodeLiveOutput,
    redactDisplayText,
    timelineEventsFromText,
    renderFormattedLiveOutput,
    appendLiveOutputText,
    startLiveOutputPolling,
    renderLiveOutput,
  };
}

function clearTaskDetail() {
  state.selectedTaskId = null;
  state.detailRequest += 1;
  persistState();
  elements.detailHeading.textContent = "No task selected";
  elements.detailStatus.hidden = true;
  elements.detailLoading.hidden = true;
  elements.detailError.hidden = true;
  elements.detailContent.hidden = true;
  elements.detailEmpty.hidden = false;
  elements.detailAiInfraBlock.hidden = true;
  elements.detailAiInfra.replaceChildren();
  resetLiveOutputPanel();
  renderTaskTable();
}

function requestTaskDetail(taskId) {
  if (!TASK_ID_RE.test(taskId)) {
    showToast("Invalid task id");
    return;
  }
  state.selectedTaskId = taskId;
  state.detailRequest += 1;
  persistState();
  renderTaskTable();
  elements.detailHeading.textContent = taskId;
  elements.detailStatus.hidden = true;
  elements.detailEmpty.hidden = true;
  elements.detailContent.hidden = true;
  elements.detailError.hidden = true;
  elements.detailLoading.hidden = false;
  vscode.postMessage({ type: "selectTask", taskId });
  startLiveOutputPolling(taskId);
}

const FEATURE_LABELS = Object.freeze({
  source_graph: ["Source Graph", "Indexes and retrieves repository source structure. Disabling stops the repository daemon."],
  session_manager: ["Session Manager", "Preserves checkpoints and active development continuity."],
  ai_memory: ["AI Memory", "Stores and retrieves durable project lessons and decisions."],
  knowledge_base: ["Knowledge Base", "Provides authoritative repository facts and contracts."],
  context_graph: ["Manager Context Graph", "Manager-only append-only conversation ledger with deterministic graph projection and bounded retrieval. Worker sessions are excluded."],
});

function renderSettings(payload) {
  if (!payload || payload.ok !== true || !payload.features) {
    state.featureSettings = null;
    elements.settingsSummary.textContent = (payload && payload.error) || "Settings unavailable";
    elements.settingsList.replaceChildren(Object.assign(document.createElement("div"), {
      className: "panel-state error-state",
      textContent: "Repository settings could not be loaded",
    }));
    return;
  }
  state.featureSettings = payload;
  elements.settingsSummary.textContent = `Repository-local · revision ${Number(payload.revision || 0)}`;
  const fragment = document.createDocumentFragment();
  for (const [key, [label, description]] of Object.entries(FEATURE_LABELS)) {
    const row = document.createElement("label");
    row.className = "settings-row";
    const copy = document.createElement("span");
    copy.className = "settings-copy";
    const title = document.createElement("strong");
    title.textContent = label;
    const detail = document.createElement("small");
    if (key === "context_graph" && payload.context_graph_runtime?.ok === true) {
      const runtime = payload.context_graph_runtime;
      const codexCapture = runtime.capture_adapters?.codex === "final_items" ? "Codex auto-capture" : "manual capture";
      detail.textContent = `${description} ${codexCapture} · ${Number(runtime.events || 0)} events · ${Number(runtime.nodes || 0)} nodes · ${Number(runtime.edges || 0)} edges.`;
    } else {
      detail.textContent = description;
    }
    copy.append(title, detail);
    const control = document.createElement("span");
    control.className = "switch-control";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(payload.features[key]);
    input.dataset.featureSetting = key;
    input.setAttribute("aria-label", `${label} enabled`);
    const track = document.createElement("span");
    track.className = "switch-track";
    control.append(input, track);
    row.append(copy, control);
    fragment.append(row);
  }
  elements.settingsList.replaceChildren(fragment);
}

// ── Fixed-enum inbound message handling from the extension host ───────────
window.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || typeof message !== "object") {
    return;
  }
  stopReadyRetry();
  switch (message.type) {
    case "snapshot":
      renderSnapshot(message.payload);
      break;
    case "taskDetail": {
      const payload = message.payload;
      if (!payload || payload.ok === false) {
        elements.detailLoading.hidden = true;
        elements.detailError.textContent = (payload && payload.error) || "Task detail unavailable";
        elements.detailError.hidden = false;
        break;
      }
      renderTaskDetail(payload.task || {});
      break;
    }
    case "offline":
      showOffline(message.reason);
      break;
    case "error":
      showToast(message.message || "Request failed");
      break;
    case "repositoryInfo": {
      const repoEl = document.querySelector("#repo-label");
      if (repoEl) {
        const repoName = String(message.repoName || message.label || "Unknown repository");
        const repoId = String(message.repoId || "repo unavailable");
        // Identity only -- the authoritative storage-ready verdict comes
        // from each snapshot's "storage" field (see renderStorageState),
        // never from this identity-only message.
        repoEl.textContent = `${repoName} | ${repoId}`;
      }
      renderRuntimeInfo({
        extensionVersion: message.extensionVersion,
        expectedMcpVersion: message.expectedMcpVersion,
        runtimeVersion: "checking",
        reloadRequired: false,
      });
      break;
    }
    case "runtimeInfo": {
      renderRuntimeInfo(message.payload);
      break;
    }
    case "systemLogs":
      renderSystemLogs(message.payload);
      break;
    case "notification":
      showToast(message.message || "Done");
      break;
    case "memory":
      renderMemory(message.payload);
      break;
    case "sessions":
      renderSessions(message.payload);
      break;
    case "kb":
      renderKb(message.payload);
      break;
    case "settings":
      renderSettings(message.payload);
      break;
    case "coordinatorTargets": {
      renderCoordinatorTargets(message.payload);
      break;
    }
    case "liveOutput": {
      renderLiveOutput(message.payload);
      break;
    }
    default:
      break;
  }
});

// A fresh panel may not have user-mutated state yet. Persist immediately so
// VS Code can deserialize and rebind it after an extension-host restart.
// Retry the ready handshake until the first host response so a listener
// registration race cannot leave the panel permanently on Connecting.
persistState();
signalReady();

elements.refreshButton.addEventListener("click", requestRefresh);
elements.offlineRetryButton.addEventListener("click", () => {
  vscode.postMessage({ type: "retry" });
});

// The sole initialization trigger in the UI: one click posts the fixed
// "initializeStorage" message; the extension host is the only thing that
// ever turns this into the one bounded aiworkhub_dashboard_initialize MCP
// tool call.
elements.initializeButton.addEventListener("click", () => {
  elements.initializeButton.disabled = true;
  elements.initializeButton.textContent = "Initializing";
  vscode.postMessage({ type: "initializeStorage" });
  window.setTimeout(() => {
    elements.initializeButton.disabled = false;
    elements.initializeButton.textContent = "Initialize AIWorkHub";
  }, 4000);
});

elements.autoRefresh.addEventListener("change", () => {
  vscode.postMessage({ type: "setAutoRefresh", enabled: Boolean(elements.autoRefresh.checked) });
});

elements.refreshInterval.addEventListener("change", () => {
  vscode.postMessage({ type: "setRefreshInterval", ms: Number(elements.refreshInterval.value) });
});

for (const button of elements.targetButtons) {
  button.addEventListener("click", () => {
    vscode.postMessage({ type: "selectCoordinatorTarget", provider: button.dataset.provider });
  });
}

elements.statusFilters.addEventListener("click", (event) => {
  const button = event.target.closest("[data-status]");
  if (!button) {
    return;
  }
  state.status = button.dataset.status;
  persistState();
  for (const item of elements.statusFilters.querySelectorAll("[data-status]")) {
    const active = item === button;
    item.classList.toggle("is-active", active);
    item.setAttribute("aria-pressed", String(active));
  }
  renderTaskTable();
});

elements.taskSearch.addEventListener("input", () => {
  state.search = elements.taskSearch.value;
  renderTaskTable();
});

elements.topicFilter.addEventListener("change", () => {
  state.topic = elements.topicFilter.value;
  renderTaskTable();
});

elements.runnerFilter.addEventListener("change", () => {
  state.runner = elements.runnerFilter.value;
  renderTaskTable();
});

elements.sortOrder.addEventListener("change", () => {
  state.sort = elements.sortOrder.value;
  persistState();
  renderTaskTable();
});

elements.returnSearch.addEventListener("input", () => {
  state.returnSearch = elements.returnSearch.value;
  state.returnPage = 0;
  if (state.snapshot) renderReturns(state.snapshot);
});

elements.returnTopic.addEventListener("change", () => {
  state.returnTopic = elements.returnTopic.value;
  state.returnPage = 0;
  if (state.snapshot) renderReturns(state.snapshot);
});

elements.returnRunner.addEventListener("change", () => {
  state.returnRunner = elements.returnRunner.value;
  state.returnPage = 0;
  if (state.snapshot) renderReturns(state.snapshot);
});

elements.returnPrevious.addEventListener("click", () => {
  state.returnPage = Math.max(0, state.returnPage - 1);
  if (state.snapshot) renderReturns(state.snapshot);
});

elements.returnNext.addEventListener("click", () => {
  state.returnPage += 1;
  if (state.snapshot) renderReturns(state.snapshot);
});

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-task-id]");
  if (target && target.dataset.taskId) {
    requestTaskDetail(String(target.dataset.taskId));
  }
});

const operationTabs = Array.from(document.querySelectorAll("[role='tab'][data-tab]"));

function activateOperationTab(tab, focus = false) {
  for (const candidate of operationTabs) {
    const active = candidate === tab;
    candidate.setAttribute("aria-selected", String(active));
    candidate.tabIndex = active ? 0 : -1;
    document.querySelector(`#panel-${candidate.dataset.tab}`).hidden = !active;
  }
  if (focus) {
    tab.focus();
  }
}

operationTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => activateOperationTab(tab));
  tab.addEventListener("keydown", (event) => {
    let targetIndex = null;
    if (event.key === "ArrowRight") {
      targetIndex = (index + 1) % operationTabs.length;
    } else if (event.key === "ArrowLeft") {
      targetIndex = (index - 1 + operationTabs.length) % operationTabs.length;
    } else if (event.key === "Home") {
      targetIndex = 0;
    } else if (event.key === "End") {
      targetIndex = operationTabs.length - 1;
    }
    if (targetIndex !== null) {
      event.preventDefault();
      activateOperationTab(operationTabs[targetIndex], true);
    }
  });
});

elements.headerStorage.addEventListener("click", () => {
  const storageTab = operationTabs.find((tab) => tab.dataset.tab === "storage");
  if (storageTab) {
    activateOperationTab(storageTab, true);
    document.querySelector("#panel-storage").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
});
elements.storageCleanupPreview.addEventListener("click", () => {
  vscode.postMessage({ type: "requestStorageCleanup" });
});
elements.storageRegistrationPrune.addEventListener("click", () => {
  vscode.postMessage({ type: "requestStorageRegistrationPrune" });
});
elements.terminalLogCleanupPreview.addEventListener("click", () => {
  vscode.postMessage({ type: "requestTerminalLogCleanup" });
});
elements.runtimeCleanupPreview.addEventListener("click", () => {
  vscode.postMessage({ type: "requestRuntimeCleanup" });
});
elements.storageList.addEventListener("click", (event) => {
  const restoreRuntimes = event.target.closest("[data-restore-runtime-batch]");
  if (restoreRuntimes) {
    vscode.postMessage({ type: "requestRuntimeRestore", batchId: restoreRuntimes.dataset.restoreRuntimeBatch });
    return;
  }
  const purgeRuntimes = event.target.closest("[data-purge-runtime-batch]");
  if (purgeRuntimes) {
    vscode.postMessage({ type: "requestRuntimePurge", batchId: purgeRuntimes.dataset.purgeRuntimeBatch });
    return;
  }
  const restoreLogs = event.target.closest("[data-restore-terminal-log-batch]");
  if (restoreLogs) {
    vscode.postMessage({ type: "requestTerminalLogRestore", batchId: restoreLogs.dataset.restoreTerminalLogBatch });
    return;
  }
  const purgeLogs = event.target.closest("[data-purge-terminal-log-batch]");
  if (purgeLogs) {
    vscode.postMessage({ type: "requestTerminalLogPurge", batchId: purgeLogs.dataset.purgeTerminalLogBatch });
    return;
  }
  const restore = event.target.closest("[data-restore-batch]");
  if (restore) {
    vscode.postMessage({ type: "requestStorageRestore", batchId: restore.dataset.restoreBatch });
    return;
  }
  const purge = event.target.closest("[data-purge-batch]");
  if (purge) {
    vscode.postMessage({ type: "requestStoragePurge", batchId: purge.dataset.purgeBatch });
  }
});

elements.systemLogCopy.addEventListener("click", () => {
  vscode.postMessage({ type: "copySystemLogs" });
});

elements.systemLogClear.addEventListener("click", () => {
  vscode.postMessage({ type: "clearSystemLogs" });
});

elements.openSystemLog.addEventListener("click", () => {
  elements.systemLogDialog.showModal();
});

elements.openAiMemory.addEventListener("click", () => {
  elements.aiMemoryDialog.showModal();
  elements.aiMemorySummary.textContent = "Loading repository memory";
  vscode.postMessage({ type: "requestMemory" });
});

elements.aiMemorySearch.addEventListener("input", renderMemoryEntries);

elements.openSessions.addEventListener("click", () => {
  elements.sessionsDialog.showModal();
  elements.sessionsSummary.textContent = "Loading repository sessions";
  vscode.postMessage({ type: "requestSessions" });
});

elements.sessionsSearch.addEventListener("input", renderSessionEntries);

elements.openKb.addEventListener("click", () => {
  elements.kbDialog.showModal();
  elements.kbSummary.textContent = "Loading repository knowledge";
  vscode.postMessage({ type: "requestKb" });
});

elements.openSettings.addEventListener("click", () => {
  elements.settingsDialog.showModal();
  elements.settingsSummary.textContent = "Loading repository feature settings";
  vscode.postMessage({ type: "requestSettings" });
});

elements.settingsList.addEventListener("change", (event) => {
  const input = event.target.closest("[data-feature-setting]");
  if (!input || !state.featureSettings) return;
  input.disabled = true;
  vscode.postMessage({
    type: "updateFeatureSetting",
    feature: input.dataset.featureSetting,
    enabled: Boolean(input.checked),
    expectedRevision: Number(state.featureSettings.revision || 0),
  });
});

elements.kbSearch.addEventListener("input", renderKbEntries);

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => document.querySelector(`#${button.dataset.closeDialog}`).close());
});

// Restore the last-selected status filter button state (selection itself is
// restored below via vscode.getState()).
for (const item of elements.statusFilters.querySelectorAll("[data-status]")) {
  const active = item.dataset.status === state.status;
  item.classList.toggle("is-active", active);
  item.setAttribute("aria-pressed", String(active));
}
elements.sortOrder.value = state.sort;

if (state.selectedTaskId) {
  requestTaskDetail(state.selectedTaskId);
}
