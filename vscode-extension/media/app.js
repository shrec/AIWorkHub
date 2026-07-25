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
};

function persistState() {
  vscode.setState({
    selectedTaskId: state.selectedTaskId,
    status: state.status,
    sort: state.sort,
  });
}

const elements = {
  connectionState: document.querySelector("#connection-state"),
  connectionLabel: document.querySelector("#connection-label"),
  lastSync: document.querySelector("#last-sync"),
  autoRefresh: document.querySelector("#auto-refresh"),
  refreshInterval: document.querySelector("#refresh-interval"),
  refreshButton: document.querySelector("#refresh-button"),
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
  usageList: document.querySelector("#usage-list"),
  returnList: document.querySelector("#return-list"),
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
  const target = state.snapshot && state.snapshot.manager_identity_target
    ? state.snapshot.manager_identity_target
    : null;
  if (target && typeof target === "object") {
    if (target.capability_state) pieces.push(`route=${target.capability_state}`);
    if (target.reason) pieces.push(`route_reason=${target.reason}`);
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
  elements.identityAlert.hidden = false;
  elements.identityAlert.classList.toggle("identity-ok", isManager);
  elements.identityAlert.classList.toggle("identity-warn", !isManager);
  elements.identityAlertTitle.textContent = isManager ? "Manager identity verified" : "Manager identity unverified";
  elements.identityAlertMessage.textContent = compactManagerIdentityReason(identity);
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
  if (reviewQueue.length === 0) {
    elements.returnList.replaceChildren(createElement("div", "panel-list-empty", "No worker returns awaiting review"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of reviewQueue.slice(0, 40)) {
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
  renderKnownRepositories(snapshot);
  if (storageReady) {
    renderSourceHealth(snapshot);
  }
  renderFilterOptions();
  renderTaskTable();
  renderStats(elements.topicStats, snapshot.summaries && snapshot.summaries.topics);
  renderStats(elements.runnerStats, snapshot.summaries && snapshot.summaries.runners);
  renderUsage(snapshot);
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
  if (payload.reloadRequired) {
    elements.reloadAlert.hidden = false;
    elements.reloadAlertMessage.textContent =
      `AIWorkHub ${extensionVersion} expects MCP ${expectedMcpVersion}, but the live child reports ${runtimeVersion}. Run Developer: Reload Window or restart the extension host.`;
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
  if (!info) {
    elements.detailAiInfraBlock.hidden = true;
    elements.detailAiInfra.replaceChildren();
    return;
  }
  const fragment = document.createDocumentFragment();
  const overview = [
    ["Injected", info.injected ? "yes" : "no"],
    ["Acknowledged", info.acknowledged ? "yes" : "no"],
  ];
  if (info.estimate && info.estimate.label) {
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
    const section = info[key] && typeof info[key] === "object" ? info[key] : {};
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

// ── Fixed-enum inbound message handling from the extension host ───────────
window.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || typeof message !== "object") {
    return;
  }
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

elements.refreshButton.addEventListener("click", requestRefresh);
elements.offlineRetryButton.addEventListener("click", requestRefresh);

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

// Restore the last-selected status filter button state (selection itself is
// restored below via vscode.getState()).
for (const item of elements.statusFilters.querySelectorAll("[data-status]")) {
  const active = item.dataset.status === state.status;
  item.classList.toggle("is-active", active);
  item.setAttribute("aria-pressed", String(active));
}
elements.sortOrder.value = state.sort;

vscode.postMessage({ type: "ready" });
if (state.selectedTaskId) {
  requestTaskDetail(state.selectedTaskId);
}
