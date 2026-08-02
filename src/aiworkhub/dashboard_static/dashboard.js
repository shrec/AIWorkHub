"use strict";

const ACTIVE_STATUSES = ["pending", "processing", "review"];
const STATUS_ORDER = { processing: 0, review: 1, blocked: 2, pending: 3, archived: 4, finished: 5 };

const state = {
  snapshot: null,
  tasks: [],
  selectedTaskId: null,
  status: "all",
  search: "",
  topic: "all",
  runner: "all",
  sort: "status",
  refreshing: false,
  refreshTimer: null,
  toastTimer: null,
  detailRequest: 0,
};

const elements = {
  connectionState: document.querySelector("#connection-state"),
  connectionLabel: document.querySelector("#connection-label"),
  lastSync: document.querySelector("#last-sync"),
  autoRefresh: document.querySelector("#auto-refresh"),
  refreshInterval: document.querySelector("#refresh-interval"),
  refreshButton: document.querySelector("#refresh-button"),
  headerStorageManaged: document.querySelector("#header-storage-managed"),
  headerStorageFree: document.querySelector("#header-storage-free"),
  sourceAlert: document.querySelector("#source-alert"),
  sourceAlertTitle: document.querySelector("#source-alert-title"),
  sourceAlertMessage: document.querySelector("#source-alert-message"),
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
  storageList: document.querySelector("#storage-list"),
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
  toast: document.querySelector("#toast"),
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

function escapeAttr(value) {
  return String(value).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
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

async function fetchJson(path) {
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = null;
  }
  if (!response.ok) {
    const message = payload && (payload.message || payload.error);
    throw new Error(message || `Request failed (${response.status})`);
  }
  return payload;
}

function flattenTasks(snapshot) {
  const byId = new Map();
  const groups = snapshot && snapshot.tasks ? snapshot.tasks : {};
  // Active statuses: pending, processing, review
  for (const status of ACTIVE_STATUSES) {
    for (const rawTask of asArray(groups[status])) {
      if (!rawTask || !rawTask.task_id) {
        continue;
      }
      const task = { ...rawTask, status: rawTask.status || status };
      byId.set(String(task.task_id), task);
    }
  }
  // Also include blocked, finished, archived population for full filtering
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
    const terminal = task.provider_terminal && typeof task.provider_terminal === "object" ? task.provider_terminal : null;
    if (terminal) {
      const terminalBadge = createElement("span", "signal-badge fail", terminal.state || terminal.category || "terminal failure");
      terminalBadge.title = [terminal.message, terminal.recommended_action]
        .filter(Boolean)
        .join(" | ");
      signalStack.appendChild(terminalBadge);
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
  for (const [label, value] of [
    ["Managed", formatBytes(usage.managed_total_bytes)],
    ["Repo data", formatBytes(usage.repo_data_bytes)],
    ["Worker trees", formatBytes(usage.worker_tree_bytes)],
    ["Free disk", formatBytes(usage.disk_free_bytes)],
  ]) {
    const metric = createElement("div", "usage-metric");
    metric.append(createElement("span", "usage-label", label), createElement("strong", "", value));
    overview.appendChild(metric);
  }
  fragment.appendChild(overview);
  for (const [label, value] of [
    ["Repository .aiworkhub", `${formatBytes(usage.repo_data_bytes)} · ${formatCount(usage.repo_data_files)} files`],
    ["Retained worker trees", `${formatBytes(usage.worker_tree_bytes)} · ${formatCount(usage.worker_tree_count)} trees`],
    ["Safe reclaimable", formatBytes(usage.safe_reclaimable_bytes)],
    ["Disk", `${formatBytes(usage.disk_used_bytes)} / ${formatBytes(usage.disk_total_bytes)} · ${numberValue(usage.disk_used_percent).toFixed(1)}% used`],
  ]) {
    const row = createElement("div", "storage-row");
    row.append(createElement("span", "storage-label", label), createElement("strong", "storage-value", value));
    fragment.appendChild(row);
  }
  const stateLabel = String(usage.scan_status || "unknown");
  const timestamp = usage.scanned_at ? new Date(usage.scanned_at).toLocaleString() : "calculating now";
  fragment.appendChild(createElement("div", "storage-scan-state", `${stateLabel} · ${timestamp}`));
  elements.storageList.replaceChildren(fragment);
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

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  state.tasks = flattenTasks(snapshot);
  renderSummary(snapshot);
  renderSourceHealth(snapshot);
  renderFilterOptions();
  renderTaskTable();
  renderStats(elements.topicStats, snapshot.summaries && snapshot.summaries.topics);
  renderStats(elements.runnerStats, snapshot.summaries && snapshot.summaries.runners);
  renderUsage(snapshot);
  renderStorage(snapshot);
  renderReturns(snapshot);
  renderRuns(snapshot);
  renderWarnings(snapshot);
}

function showToast(message) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 5000);
}

function scheduleRefresh() {
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = null;
  if (!elements.autoRefresh.checked) {
    return;
  }
  const delay = Math.max(5000, numberValue(elements.refreshInterval.value));
  state.refreshTimer = window.setTimeout(async () => {
    await refreshSnapshot(true);
    scheduleRefresh();
  }, delay);
}

async function refreshSnapshot(quiet = false) {
  if (state.refreshing) {
    return;
  }
  state.refreshing = true;
  elements.refreshButton.disabled = true;
  elements.refreshButton.textContent = "Refreshing";
  if (!state.snapshot) {
    setConnection("offline", "Connecting");
    elements.tableLoading.hidden = false;
  }
  try {
    const snapshot = await fetchJson("/api/snapshot");
    if (!snapshot || !snapshot.tasks || !snapshot.status_counts) {
      throw new Error("Snapshot response is incomplete");
    }
    renderSnapshot(snapshot);
    if (state.selectedTaskId && !state.tasks.some((task) => String(task.task_id) === state.selectedTaskId)) {
      clearTaskDetail();
    }
  } catch (error) {
    setConnection("offline", "Offline");
    if (!quiet || !state.snapshot) {
      showToast(error.message || "Snapshot refresh failed");
    }
    if (!state.snapshot) {
      elements.tableLoading.hidden = true;
      elements.tableEmpty.hidden = false;
      elements.tableEmpty.textContent = "Queue unavailable";
    }
  } finally {
    state.refreshing = false;
    elements.refreshButton.disabled = false;
    elements.refreshButton.textContent = "Refresh";
  }
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

  // Archive / Restore controls
  const isArchived = String(card.archived_at || "").trim();
  const detailActions = document.querySelector("#detail-actions");
  if (detailActions) {
    let archiveHtml = "";
    if (isArchived) {
      archiveHtml = `<span class="archived-label">Archived ${card.archived_at}</span>
        <button onclick="handleRestore('${escapeAttr(card.task_id)}')" class="btn-restore">Restore</button>`;
    } else {
      archiveHtml = `<button onclick="handleArchive('${escapeAttr(card.task_id)}')" class="btn-archive">Archive</button>`;
    }
    detailActions.innerHTML = archiveHtml;
  }

  elements.detailLoading.hidden = true;
  elements.detailError.hidden = true;
  elements.detailEmpty.hidden = true;
  elements.detailContent.hidden = false;
}

function clearTaskDetail() {
  state.selectedTaskId = null;
  state.detailRequest += 1;
  elements.detailHeading.textContent = "No task selected";
  elements.detailStatus.hidden = true;
  elements.detailLoading.hidden = true;
  elements.detailError.hidden = true;
  elements.detailContent.hidden = true;
  elements.detailEmpty.hidden = false;
  elements.detailAiInfraBlock.hidden = true;
  elements.detailAiInfra.replaceChildren();
  const detailActions = document.querySelector("#detail-actions");
  if (detailActions) {
    detailActions.innerHTML = "";
  }
  renderTaskTable();
}

async function loadTaskDetail(taskId) {
  state.selectedTaskId = String(taskId);
  const requestNumber = ++state.detailRequest;
  renderTaskTable();
  elements.detailHeading.textContent = state.selectedTaskId;
  elements.detailStatus.hidden = true;
  elements.detailEmpty.hidden = true;
  elements.detailContent.hidden = true;
  elements.detailError.hidden = true;
  elements.detailLoading.hidden = false;
  try {
    const query = new URLSearchParams({ id: state.selectedTaskId });
    const payload = await fetchJson(`/api/task?${query.toString()}`);
    if (requestNumber !== state.detailRequest) {
      return;
    }
    renderTaskDetail(payload.task || {});
  } catch (error) {
    if (requestNumber !== state.detailRequest) {
      return;
    }
    elements.detailLoading.hidden = true;
    elements.detailError.textContent = error.message || "Task detail unavailable";
    elements.detailError.hidden = false;
  }
}

elements.refreshButton.addEventListener("click", async () => {
  await refreshSnapshot(false);
  scheduleRefresh();
});

elements.autoRefresh.addEventListener("change", scheduleRefresh);
elements.refreshInterval.addEventListener("change", scheduleRefresh);

elements.statusFilters.addEventListener("click", (event) => {
  const button = event.target.closest("[data-status]");
  if (!button) {
    return;
  }
  state.status = button.dataset.status;
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
  renderTaskTable();
});

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-task-id]");
  if (target) {
    loadTaskDetail(target.dataset.taskId);
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

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && elements.autoRefresh.checked) {
    refreshSnapshot(true).then(scheduleRefresh);
  }
});

// ── Archive / Restore ─────────────────────────────────────────────────────

function coordinatorToken() {
  // Prompt the owner for the token; discard it immediately after use.
  // Never stored in localStorage, sessionStorage, URL, or DOM.
  const token = window.prompt("Coordinator token required for archive/restore:");
  if (token === null || token === "") {
    return null;
  }
  return token.trim();
}

async function archiveTask(taskId, token, reason) {
  const body = JSON.stringify({ task_id: taskId, reason: reason || "" });
  const response = await fetch("/api/archive", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Coordinator-Token": token,
      Accept: "application/json",
    },
    body,
    cache: "no-store",
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = null; }
  if (!response.ok) {
    throw new Error((payload && payload.error) || `Archive failed (${response.status})`);
  }
  return payload;
}

async function restoreTask(taskId, token, reason) {
  const body = JSON.stringify({ task_id: taskId, reason: reason || "" });
  const response = await fetch("/api/restore", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Coordinator-Token": token,
      Accept: "application/json",
    },
    body,
    cache: "no-store",
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = null; }
  if (!response.ok) {
    throw new Error((payload && payload.error) || `Restore failed (${response.status})`);
  }
  return payload;
}

async function handleArchive(taskId) {
  const token = coordinatorToken();
  if (!token) {
    showToast("Cancelled: coordinator token required");
    return;
  }
  const reason = (window.prompt("Reason (optional, max 200 chars):") || "").substring(0, 200);
  if (!window.confirm(`Archive task "${taskId}"? This is reversible.`)) {
    return;
  }
  try {
    const result = await archiveTask(taskId, token, reason);
    showToast(result.message || "Archived");
    refreshSnapshot(true).then(scheduleRefresh);
    clearTaskDetail();
  } catch (error) {
    showToast(error.message || "Archive failed");
  }
}

async function handleRestore(taskId) {
  const token = coordinatorToken();
  if (!token) {
    showToast("Cancelled: coordinator token required");
    return;
  }
  const reason = (window.prompt("Reason (optional, max 200 chars):") || "").substring(0, 200);
  if (!window.confirm(`Restore task "${taskId}"?`)) {
    return;
  }
  try {
    const result = await restoreTask(taskId, token, reason);
    showToast(result.message || "Restored");
    refreshSnapshot(true).then(scheduleRefresh);
    clearTaskDetail();
  } catch (error) {
    showToast(error.message || "Restore failed");
  }
}

refreshSnapshot(false).then(scheduleRefresh);
